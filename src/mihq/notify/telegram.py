"""Telegram Bot API client.

WHY NOT REUSE THE PROVIDER HTTP CLIENT
--------------------------------------
That one is GET-only and built around caching, which is actively wrong here:
re-sending a cached message would be a bug, not an optimisation. Telegram also
has its own rate-limit shape and its own error envelope.

RATE LIMITS
-----------
Telegram allows roughly 30 messages/second overall but only about 20 per
minute to a single group chat. We use the lower figure, because exceeding it
gets the bot throttled for everyone.

A 429 response carries `parameters.retry_after` with the exact number of
seconds to wait. We honour it precisely rather than guessing.

THE TOKEN IS IN THE URL
-----------------------
Telegram puts the bot token in the request path, which makes a logged URL the
most likely way to leak it. Every log line here goes through the redaction
filter in `logging_setup`, and this module never logs a full URL anyway.

DRY RUN
-------
With `DRY_RUN=true` the client renders the message, logs it, records it as
`skipped` in `notification_logs`, and sends nothing. Formatting can be checked
end to end without a bot token.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from mihq.config import Settings
from mihq.logging_setup import get_logger
from mihq.providers.http import RateLimiter

log = get_logger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"

# Telegram rejects anything longer. We truncate well before this so a message
# is never silently dropped for being one character too long.
MAX_MESSAGE_CHARS = 4096
SAFE_MESSAGE_CHARS = 3900

# Conservative: the per-group-chat limit, not the global one.
MESSAGES_PER_MINUTE = 18


class TelegramError(Exception):
    """A send failed. `retry_after` is set when Telegram told us to wait."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
        retriable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.retriable = retriable


@dataclass
class SendResult:
    """What happened when we tried to send one message."""

    ok: bool
    status: str                  # 'sent' | 'failed' | 'skipped'
    attempt: int = 1
    telegram_message_id: int | None = None
    error_code: int | None = None
    error_message: str | None = None
    chat_id: str | None = None
    rendered_text: str = ""

    @property
    def was_sent(self) -> bool:
        return self.status == "sent"


def truncate_for_telegram(text: str, limit: int = SAFE_MESSAGE_CHARS) -> str:
    """Cut a message to fit, breaking at a line boundary where possible.

    Telegram rejects over-long messages outright, so a message that is too
    long does not arrive truncated - it does not arrive at all. Cutting at a
    newline keeps the result readable rather than ending mid-word.
    """
    if len(text) <= limit:
        return text

    notice = "\n\n[truncated]"
    budget = limit - len(notice)
    cut = text[:budget]

    # Prefer the last paragraph break, then the last line break.
    for separator in ("\n\n", "\n"):
        index = cut.rfind(separator)
        if index > budget * 0.6:    # only if we are not throwing away too much
            cut = cut[:index]
            break

    return cut.rstrip() + notice


class TelegramClient:
    """Sends messages, with retries and rate limiting."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.token = settings.telegram_bot_token
        self.default_chat_id = settings.telegram_chat_id
        self.admin_chat_id = settings.admin_chat_id
        self.dry_run = settings.dry_run
        self._transport = transport
        self._sleep = sleep
        self.limiter = RateLimiter(MESSAGES_PER_MINUTE, clock=clock, sleep=sleep)
        self.sent_count = 0
        self.failed_count = 0
        self.skipped_count = 0

    @property
    def is_configured(self) -> bool:
        return bool(self.token and self.default_chat_id)

    def _url(self, method: str) -> str:
        return f"{TELEGRAM_API_BASE}/bot{self.token}/{method}"

    def _post(self, method: str, payload: Mapping[str, Any]) -> Any:
        """One HTTP POST. Separated so tests can replace the transport."""
        if self._transport is not None:
            return self._transport(self._url(method), payload)

        try:
            import requests  # noqa: PLC0415
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise TelegramError(
                "the 'requests' package is not installed", retriable=False
            ) from exc

        try:
            response = requests.post(
                self._url(method),
                json=dict(payload),
                timeout=self.settings.http_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - any network failure is retriable
            raise TelegramError(
                f"network failure: {type(exc).__name__}", retriable=True
            ) from exc

        return _TransportResponse(response.status_code, response.text)

    def send_message(
        self,
        text: str,
        *,
        chat_id: str | None = None,
        disable_preview: bool = True,
        max_attempts: int = 3,
    ) -> SendResult:
        """Send one message, retrying transient failures.

        Never raises: a failed notification must not take down a scanner pass.
        The outcome is reported in the result and recorded by the caller.
        """
        target = chat_id or self.default_chat_id
        rendered = truncate_for_telegram(text)

        if self.dry_run:
            self.skipped_count += 1
            log.info("DRY_RUN: message rendered but not sent",
                     extra={"chat_id": target, "chars": len(rendered)})
            print("\n" + "=" * 60)
            print("DRY RUN - this message would have been sent to Telegram:")
            print("=" * 60)
            print(rendered)
            print("=" * 60 + "\n")
            return SendResult(ok=True, status="skipped", chat_id=target,
                              rendered_text=rendered)

        if not self.is_configured:
            self.failed_count += 1
            return SendResult(
                ok=False, status="failed", chat_id=target, rendered_text=rendered,
                error_message=("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set; "
                               "set them in .env, or use DRY_RUN=true to preview"),
            )

        payload = {
            "chat_id": target,
            "text": rendered,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        }

        last_error: TelegramError | None = None

        for attempt in range(1, max_attempts + 1):
            self.limiter.acquire()
            try:
                response = self._post("sendMessage", payload)
                message_id = self._parse_ok(response)
            except TelegramError as exc:
                last_error = exc
                if exc.retriable and attempt < max_attempts:
                    wait = exc.retry_after if exc.retry_after is not None else 2.0 ** attempt
                    log.warning("telegram send failed, retrying",
                                extra={"attempt": attempt, "waiting": round(wait, 1),
                                       "error": str(exc)})
                    if exc.retry_after is not None:
                        # Telegram told us exactly how long; drain the bucket
                        # so the wait is not undone by banked allowance.
                        self.limiter.pause_for(wait)
                    else:
                        self._sleep(wait)
                    continue

                self.failed_count += 1
                log.error("telegram send failed",
                          extra={"attempt": attempt, "error": str(exc)})
                return SendResult(
                    ok=False, status="failed", attempt=attempt, chat_id=target,
                    error_code=exc.status_code, error_message=str(exc),
                    rendered_text=rendered,
                )
            else:
                self.sent_count += 1
                return SendResult(
                    ok=True, status="sent", attempt=attempt, chat_id=target,
                    telegram_message_id=message_id, rendered_text=rendered,
                )

        self.failed_count += 1
        return SendResult(
            ok=False, status="failed", attempt=max_attempts, chat_id=target,
            error_message=str(last_error) if last_error else "unknown failure",
            rendered_text=rendered,
        )

    @staticmethod
    def _parse_ok(response: Any) -> int | None:
        """Validate Telegram's reply, raising `TelegramError` on anything bad.

        Telegram answers 200 with `{"ok": false, ...}` for some errors, so the
        status code alone is not enough to decide success.
        """
        status = getattr(response, "status_code", None)
        text = getattr(response, "text", "") or ""

        try:
            body = json.loads(text) if text else {}
        except ValueError:
            raise TelegramError(
                f"response was not JSON (HTTP {status})",
                status_code=status, retriable=(status or 500) >= 500,
            ) from None

        if status == 429:
            parameters = body.get("parameters") or {}
            retry_after = parameters.get("retry_after")
            raise TelegramError(
                f"rate limited; retry after {retry_after}s",
                status_code=429,
                retry_after=float(retry_after) if retry_after is not None else None,
                retriable=True,
            )

        if status is not None and status >= 500:
            raise TelegramError(f"telegram server error {status}",
                                status_code=status, retriable=True)

        if not body.get("ok", False):
            description = body.get("description", "no description")
            code = body.get("error_code", status)
            # 400/401/403 mean the request or the token is wrong; retrying
            # cannot fix either, and a bad token would retry forever.
            raise TelegramError(f"telegram rejected the message: {description}",
                                status_code=code, retriable=False)

        result = body.get("result") or {}
        return result.get("message_id")

    def send_admin(self, text: str) -> SendResult:
        """Health and error notices go to the admin chat."""
        return self.send_message(text, chat_id=self.admin_chat_id)

    def stats(self) -> dict[str, Any]:
        return {
            "sent": self.sent_count,
            "failed": self.failed_count,
            "skipped": self.skipped_count,
            "rate_limit_waited_seconds": round(self.limiter.total_waited_seconds, 2),
        }


@dataclass
class _TransportResponse:
    status_code: int
    text: str
