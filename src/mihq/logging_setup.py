"""Logging with automatic secret redaction.

WHY THIS FILE EXISTS
--------------------
Section 9 of the project spec says credentials must never appear in logs. The
usual way they leak is accidental: an exception message containing a full
database URL, or a debug line printing a request URL that has a bot token in
the path (the Telegram API puts the token in the URL itself).

`SecretRedactingFilter` below scrubs known secret values out of every log
record before it is written, so a leak requires actively bypassing logging
rather than merely forgetting to be careful.

HOW TO USE IT
-------------
    from mihq.config import load_settings
    from mihq.logging_setup import setup_logging, get_logger

    settings = load_settings()
    setup_logging(settings)
    log = get_logger(__name__)
    log.info("scanner starting", extra={"chain": "base"})
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:  # avoids a circular import at runtime
    from mihq.config import Settings

REDACTED = "***REDACTED***"

# Log record attributes that belong to logging itself. Anything else attached
# to a record came from our own `extra={...}` and is worth emitting as JSON.
_STANDARD_RECORD_KEYS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "taskName", "thread", "threadName",
    }
)


class SecretRedactingFilter(logging.Filter):
    """Replaces known secret values with `***REDACTED***` in log output.

    Two layers of defence:
      1. Exact-value matching for the secrets we loaded from configuration.
      2. Pattern matching for credential shapes (Telegram tokens, Anthropic
         keys, postgres URLs with passwords) that catches secrets we were
         never told about, such as one pasted into a variable by mistake.
    """

    # Telegram bot tokens look like 123456789:AA... (digits, colon, 35ish chars).
    #
    # NOTE on the missing \b anchors: the most likely accidental leak is a
    # logged Telegram URL, ".../bot7584112233:AAH...". There is NO word
    # boundary between the "t" of "bot" and the first digit, so a leading \b
    # would make this pattern silently fail on the one case that matters most.
    # The character classes already stop at "/" and whitespace, so anchors add
    # nothing here.
    _PATTERNS: tuple[re.Pattern[str], ...] = (
        re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}"),
        re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),
        re.compile(r"(?i)(postgres(?:ql)?://[^:/\s]+:)([^@\s]+)(@)"),
    )

    def __init__(self, secrets: Iterable[str | None] = ()) -> None:
        super().__init__()
        # Only redact non-trivial values; a 1-character "secret" would
        # otherwise mangle every log line.
        self._secrets = sorted(
            {s for s in secrets if s and len(s) >= 8},
            key=len,
            reverse=True,  # longest first, so partial overlaps redact fully
        )

    def scrub(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        for pattern in self._PATTERNS:
            if pattern.groups == 3:
                text = pattern.sub(rf"\1{REDACTED}\3", text)
            else:
                text = pattern.sub(REDACTED, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        # Render the message now so %-style args are scrubbed too.
        try:
            rendered = record.getMessage()
        except Exception:  # noqa: BLE001 - never let logging break the app
            return True
        record.msg = self.scrub(rendered)
        record.args = ()

        if record.exc_text:
            record.exc_text = self.scrub(record.exc_text)

        for key, value in list(record.__dict__.items()):
            if key not in _STANDARD_RECORD_KEYS and isinstance(value, str):
                record.__dict__[key] = self.scrub(value)

        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Easier to search in hosted log viewers."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_KEYS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """Readable single-line output for local development."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _STANDARD_RECORD_KEYS and not k.startswith("_")
        }
        if extras:
            rendered = " ".join(f"{k}={v}" for k, v in extras.items())
            return f"{base}  [{rendered}]"
        return base


def setup_logging(settings: "Settings | None" = None) -> logging.Logger:
    """Configure the root logger once. Safe to call more than once.

    Passing `settings` enables exact-value redaction for your real secrets.
    Without it, only pattern-based redaction applies.
    """
    level_name = (settings.log_level if settings else "INFO").upper()
    fmt_name = (settings.log_format if settings else "text").lower()
    level = getattr(logging, level_name, logging.INFO)

    secrets: list[str | None] = []
    if settings is not None:
        from mihq.config import SECRET_FIELDS

        secrets = [getattr(settings, name, None) for name in SECRET_FIELDS]

    root = logging.getLogger()
    root.setLevel(level)

    # Remove handlers we previously installed so repeated calls do not
    # duplicate every log line.
    for handler in list(root.handlers):
        if getattr(handler, "_mihq_handler", False):
            root.removeHandler(handler)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(JsonFormatter() if fmt_name == "json" else TextFormatter())
    handler.addFilter(SecretRedactingFilter(secrets))
    handler._mihq_handler = True  # type: ignore[attr-defined]
    root.addHandler(handler)

    # These libraries are chatty at INFO and would bury our own messages.
    for noisy in ("urllib3", "httpx", "httpcore", "anthropic"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))

    return root


def get_logger(name: str) -> logging.Logger:
    """Standard way for a module to get its logger: `get_logger(__name__)`."""
    return logging.getLogger(name)
