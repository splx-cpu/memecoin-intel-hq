"""Takes detected events and gets them onto Telegram.

THE FLOW, AND WHY IT IS ORDERED THIS WAY
----------------------------------------
For each event awaiting notification:

  1. Load the event, its token and its report in one query.
  2. Produce a report if none exists (the free template analyzer, so this
     costs nothing and cannot be blocked by a missing API key).
  3. Format the message.
  4. Send it.
  5. Log the attempt - ALWAYS, success or failure.
  6. Mark the event notified ONLY on success.

Step 6 is last and conditional on purpose. If the process dies between
sending and marking, the event stays pending and is retried, which risks a
duplicate message. The alternative - marking first - risks silently losing an
alert forever. A rare duplicate is a far better failure than a missed risk
warning, so the ordering is deliberate rather than accidental.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from mihq.analysis import build_analyzer
from mihq.analysis.base import ResearchReport
from mihq.config import Settings
from mihq.db.repositories import Repository
from mihq.logging_setup import get_logger
from mihq.notify.formatters import (
    format_alert,
    format_alert_compact,
    format_system_alert,
)
from mihq.notify.telegram import TelegramClient

log = get_logger(__name__)


@dataclass
class DispatchResult:
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    considered: int = 0
    digest_only: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        line = (
            f"notifications: {self.sent} sent, {self.failed} failed, "
            f"{self.skipped} skipped (dry run), of {self.considered} pending"
        )
        # Reported separately from `skipped`, which means DRY_RUN. These were
        # stored and will appear in the daily summary; they were simply not
        # worth interrupting anyone for.
        if self.digest_only:
            line += f"; {self.digest_only} held for the daily digest"
        return line


def _as_list(value: Any) -> list[Any]:
    """Columns come back as lists from psycopg, or as JSON text elsewhere."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except ValueError:
            return []
    return []


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


class NotificationDispatcher:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        client: TelegramClient | None = None,
    ) -> None:
        self.settings = settings
        self.repo = repository
        self.client = client or TelegramClient(settings)
        self.analyzer = build_analyzer(settings, repository=repository)

    def dispatch_pending(self, limit: int = 10) -> DispatchResult:
        """Send up to `limit` pending alerts. Never raises."""
        result = DispatchResult()
        digest_only = tuple(self.settings.notify_digest_only_categories)

        # Clear these first and in one statement. They never enter the send
        # queue, so a day's worth of new-token events cannot delay a
        # liquidity-drain alert sitting behind them.
        if digest_only:
            try:
                result.digest_only = self.repo.resolve_digest_only(digest_only)
            except Exception as exc:  # noqa: BLE001 - housekeeping must not
                                      # stop real alerts from going out
                result.errors.append(f"resolve_digest_only failed: {exc}")
                log.warning("could not resolve digest-only events",
                            extra={"error": str(exc)})

        try:
            pending = self.repo.pending_notification(
                limit, exclude_categories=digest_only)
        except Exception as exc:  # noqa: BLE001
            log.error("could not read pending notifications",
                      extra={"error": str(exc)})
            result.errors.append(f"pending_notification failed: {exc}")
            return result

        result.considered = len(pending)
        if not pending:
            if result.digest_only:
                log.info(result.summary())
            return result

        for row in pending:
            event_id = row[0]
            try:
                self._dispatch_one(event_id, result)
            except Exception as exc:  # noqa: BLE001 - one bad alert must not
                                      # stop the rest of the queue
                result.failed += 1
                result.errors.append(f"event {event_id}: {exc}")
                log.error("failed to dispatch an alert",
                          extra={"event_id": event_id,
                                 "error": f"{type(exc).__name__}: {exc}"})

        log.info(result.summary(), extra=self.client.stats())
        return result

    def _dispatch_one(self, event_id: int, result: DispatchResult) -> None:
        context = self.repo.event_with_context(event_id)
        if context is None:
            log.warning("event vanished before it could be sent",
                        extra={"event_id": event_id})
            return

        token = {
            "symbol": context.get("symbol"),
            "name": context.get("name"),
            "chain": context.get("chain"),
            "token_address": context.get("token_address"),
        }
        event = {
            "id": context["id"],
            "category": context.get("category"),
            "event_type": context.get("event_type"),
            "severity": context.get("severity"),
            "chain": context.get("chain"),
            "triggered_at": context.get("triggered_at"),
            "payload": _as_dict(context.get("payload")),
        }

        report = self._report_for(context, token, event)

        if self.settings.alert_format == "compact":
            message = format_alert_compact(
                report=report, token=token, event=event,
                security=self._security_summary(context.get("token_id")),
                timezone=self.settings.timezone,
            )
        else:
            message = format_alert(
                report=report, token=token, event=event,
                timezone=self.settings.timezone,
            )

        send = self.client.send_message(message)

        # Log the attempt before anything else can go wrong.
        try:
            self.repo.log_notification(
                alert_event_id=event_id,
                message_type=str(event.get("category") or "system"),
                status=send.status,
                attempt=send.attempt,
                chat_id=send.chat_id,
                telegram_message_id=send.telegram_message_id,
                error_code=send.error_code,
                error_message=send.error_message,
                request_payload={"chars": len(send.rendered_text)},
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("could not write a notification log row",
                        extra={"event_id": event_id, "error": str(exc)})

        if send.status == "sent":
            self.repo.mark_event_notified(event_id)
            result.sent += 1
        elif send.status == "skipped":
            # DRY_RUN: deliberately NOT marked as notified, so the same alert
            # will send for real once dry run is turned off.
            result.skipped += 1
        else:
            result.failed += 1
            if send.error_message:
                result.errors.append(send.error_message)

    def _report_for(
        self, context: dict[str, Any], token: dict[str, Any], event: dict[str, Any]
    ) -> ResearchReport:
        """Use the stored report if there is one, otherwise make one now."""
        if context.get("summary"):
            return ResearchReport(
                category=str(event.get("category") or "momentum"),
                summary=str(context["summary"]),
                model=str(context.get("model") or "unknown"),
                headline=context.get("headline"),
                what_changed=context.get("what_changed"),
                verified_facts=[str(x) for x in _as_list(context.get("verified_facts"))],
                assumptions=[str(x) for x in _as_list(context.get("assumptions"))],
                missing_data=[str(x) for x in _as_list(context.get("missing_data"))],
                risks=[str(x) for x in _as_list(context.get("risks"))],
                sources=[s for s in _as_list(context.get("sources")) if isinstance(s, dict)],
            )

        risk_flags = self._risk_flags(context.get("token_id"))
        report = self.analyzer.analyze(event=event, token=token, risk_flags=risk_flags)

        # Store it so the alert can be traced back later, and so a retry does
        # not regenerate it.
        try:
            report_id = self.repo.insert_research_report(
                report.to_row(token_id=context["token_id"], alert_event_id=context["id"])
            )
            self.repo.mark_event_analyzed(context["id"], report_id)
        except Exception as exc:  # noqa: BLE001 - the message still sends
            log.warning("could not store the research report",
                        extra={"event_id": context.get("id"), "error": str(exc)})

        return report

    def _risk_flags(self, token_id: int | None) -> list[dict[str, Any]]:
        if token_id is None:
            return []
        try:
            rows = self.repo.active_risk_flags(token_id)
        except Exception:  # noqa: BLE001
            return []
        return [
            {"flag_type": r[0], "severity": r[1], "detail": r[2], "evidence": r[3]}
            for r in rows
        ]

    def _security_summary(self, token_id: int | None):
        """Rebuild a minimal security view from the stored risk flags.

        The flags carry `source`, so provider-asserted facts stay
        distinguishable from our own heuristics even after a round trip
        through the database.
        """
        if token_id is None:
            return None
        try:
            rows = self.repo.active_risk_flags(token_id)
        except Exception:  # noqa: BLE001
            return None

        from mihq.providers.security import SecurityReport

        flags = {r[0] for r in rows}
        if not flags:
            return None

        report = SecurityReport(chain="", address="", checked=True)
        if "honeypot" in flags:
            report.is_honeypot = True
        if "mint_authority_active" in flags:
            report.is_mintable = True
        if "freeze_authority_active" in flags:
            report.is_freezable = True
        if "transfers_pausable" in flags:
            report.transfer_pausable = True
        if "holder_concentration" in flags:
            for r in rows:
                if r[0] == "holder_concentration" and isinstance(r[3], dict):
                    report.top_holder_percent = r[3].get("top_holder_percent")
        if "liquidity_unlocked" in flags:
            for r in rows:
                if r[0] == "liquidity_unlocked" and isinstance(r[3], dict):
                    report.lp_locked_percent = r[3].get("lp_locked_percent")
        return report

    def send_system_alert(self, title: str, detail: str, *, severity: str = "high") -> bool:
        """Health notice to the admin chat. Returns True if it went out."""
        message = format_system_alert(title, detail, severity=severity)
        send = self.client.send_admin(message)
        try:
            self.repo.log_notification(
                alert_event_id=None, message_type="system", status=send.status,
                attempt=send.attempt, chat_id=send.chat_id,
                telegram_message_id=send.telegram_message_id,
                error_code=send.error_code, error_message=send.error_message,
            )
        except Exception:  # noqa: BLE001
            pass
        return send.was_sent
