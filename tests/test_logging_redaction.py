"""Tests that credentials cannot reach the log output.

Section 9 of the spec requires this. A leak here is the kind of bug that is
invisible until the day you paste your logs into a support thread.
"""

from __future__ import annotations

import logging

from mihq.config import Settings
from mihq.logging_setup import REDACTED, JsonFormatter, SecretRedactingFilter, setup_logging

BOT_TOKEN = "1000000001:AAHxyzSECRETtokenMATERIALabcdefghijklmno"
API_KEY = "sk-ant-api03-REALKEYMATERIALgoeshere1234567890"
DB_URL = "postgresql://postgres.abcdef:MyDbPassw0rd@aws-0.pooler.supabase.com:5432/postgres"


def _record(msg: str, **extra) -> logging.LogRecord:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, msg, (), None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_known_secret_values_are_redacted():
    flt = SecretRedactingFilter([BOT_TOKEN, API_KEY, DB_URL])
    record = _record(f"calling {BOT_TOKEN} with key {API_KEY} against {DB_URL}")
    flt.filter(record)
    out = record.getMessage()
    assert BOT_TOKEN not in out
    assert API_KEY not in out
    assert DB_URL not in out
    assert REDACTED in out


def test_telegram_url_pattern_redacted_even_if_secret_unknown():
    """The Telegram API puts the token in the URL path, so this is the most
    likely accidental leak of all. Pattern matching catches it even when the
    filter was never told the token."""
    flt = SecretRedactingFilter([])  # deliberately given no secrets
    record = _record(f"POST https://api.telegram.org/bot{BOT_TOKEN}/sendMessage")
    flt.filter(record)
    out = record.getMessage()
    assert "AAHxyzSECRET" not in out
    assert REDACTED in out


def test_anthropic_key_pattern_redacted_without_being_registered():
    flt = SecretRedactingFilter([])
    record = _record(f"auth header used {API_KEY}")
    flt.filter(record)
    assert "REALKEYMATERIAL" not in record.getMessage()


def test_db_password_in_connection_string_redacted_but_host_kept():
    """Keeping the host visible matters: you still need to debug connectivity."""
    flt = SecretRedactingFilter([])
    record = _record(f"connect failed: {DB_URL}")
    flt.filter(record)
    out = record.getMessage()
    assert "MyDbPassw0rd" not in out
    assert "aws-0.pooler.supabase.com" in out


def test_secrets_in_percent_style_args_are_redacted():
    """log.info('token %s', tok) renders lazily; the filter must still catch it."""
    flt = SecretRedactingFilter([BOT_TOKEN])
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "token is %s", (BOT_TOKEN,), None)
    flt.filter(record)
    assert BOT_TOKEN not in record.getMessage()


def test_secrets_in_structured_extras_are_redacted():
    flt = SecretRedactingFilter([API_KEY])
    record = _record("request done", endpoint=f"/v1/messages?key={API_KEY}")
    flt.filter(record)
    assert "REALKEYMATERIAL" not in record.endpoint


def test_secrets_in_exception_text_are_redacted():
    flt = SecretRedactingFilter([DB_URL])
    record = _record("boom")
    record.exc_text = f"OperationalError connecting to {DB_URL}"
    flt.filter(record)
    assert "MyDbPassw0rd" not in record.exc_text


def test_short_values_are_not_treated_as_secrets():
    """Redacting a 3-character value would mangle ordinary log lines."""
    flt = SecretRedactingFilter(["dev", "a", ""])
    record = _record("running in dev mode with a plan")
    flt.filter(record)
    assert record.getMessage() == "running in dev mode with a plan"


def test_filter_built_from_settings_covers_every_secret_field():
    settings = Settings(
        database_url=DB_URL, telegram_bot_token=BOT_TOKEN, anthropic_api_key=API_KEY
    )
    setup_logging(settings)
    root = logging.getLogger()
    installed = [h for h in root.handlers if getattr(h, "_mihq_handler", False)]
    assert len(installed) == 1, "setup_logging must install exactly one handler"

    flt = installed[0].filters[0]
    assert isinstance(flt, SecretRedactingFilter)
    for secret in (DB_URL, BOT_TOKEN, API_KEY):
        assert secret not in flt.scrub(f"leaking {secret} here")


def test_setup_logging_is_idempotent():
    """Called twice, it must not duplicate every log line."""
    settings = Settings(database_url=DB_URL)
    setup_logging(settings)
    setup_logging(settings)
    setup_logging(settings)
    root = logging.getLogger()
    assert len([h for h in root.handlers if getattr(h, "_mihq_handler", False)]) == 1


def test_json_formatter_emits_valid_json_with_extras():
    import json

    record = _record("scan finished", chain="base", tokens_seen=37)
    rendered = JsonFormatter().format(record)
    parsed = json.loads(rendered)
    assert parsed["message"] == "scan finished"
    assert parsed["chain"] == "base"
    assert parsed["tokens_seen"] == 37
    assert parsed["level"] == "INFO"
