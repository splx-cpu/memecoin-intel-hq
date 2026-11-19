"""Tests for the Telegram client, formatters and dispatcher.

Two themes dominate:

1. **Escaping.** Token symbols are attacker-controlled. A token named
   `<b>SAFE</b>` must not be able to style our message, and one named with an
   `<a href>` must not be able to inject a link the reader trusts.
2. **Never losing an alert.** A failed send must leave the event pending so
   it retries, and must never take down the scanner pass.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from conftest import FakeClock
from mihq.analysis.base import ResearchReport
from mihq.config import Settings
from mihq.notify.dispatcher import NotificationDispatcher
from mihq.notify.formatters import (
    esc,
    format_alert,
    format_daily_summary,
    format_system_alert,
    local_time,
)
from mihq.notify.telegram import (
    MAX_MESSAGE_CHARS,
    SendResult,
    TelegramClient,
    TelegramError,
    truncate_for_telegram,
)

TRIGGERED = datetime(2026, 11, 18, 14, 17, tzinfo=timezone.utc)


def report(**overrides) -> ResearchReport:
    defaults = dict(
        category="risk", summary="Liquidity fell sharply.", model="template-v1",
        headline="TEST liquidity down 72%",
        what_changed="Liquidity fell from $412.9K to $115.6K.",
        verified_facts=["Pool liquidity: $115.6K (via dexscreener)"],
        assumptions=[], missing_data=["holder count was not reported"],
        risks=["[MEDIUM] 24h volume is 4.2x pool liquidity."],
        sources=[{"label": "DEX Screener", "url": "https://dexscreener.com/base/0xp"}],
    )
    defaults.update(overrides)
    return ResearchReport(**defaults)


TOKEN = {"symbol": "TEST", "name": "Test Token", "chain": "base",
         "token_address": "0x" + "a" * 40}
EVENT = {"id": 1, "category": "risk", "event_type": "liquidity_drain",
         "severity": "high", "chain": "base", "triggered_at": TRIGGERED,
         "payload": {}}


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.text = body if isinstance(body, str) else json.dumps(body)


class FakeTelegramTransport:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, url, payload):
        self.calls.append({"url": url, "payload": payload})
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        result = self.responses[index]
        if isinstance(result, Exception):
            raise result
        return result


def client(responses, *, dry_run=False, clock=None, **kwargs):
    clock = clock or FakeClock()
    settings = Settings(
        telegram_bot_token="1000000001:AAHtesttokenvaluethatislongenough12345",
        telegram_chat_id="-1001234567890", dry_run=dry_run, **kwargs,
    )
    return TelegramClient(settings, transport=FakeTelegramTransport(responses),
                          clock=clock, sleep=clock.sleep), clock


# ===========================================================================
# Escaping - the security-relevant part
# ===========================================================================

class TestEscaping:
    def test_html_in_a_token_symbol_cannot_style_the_message(self):
        """Anyone can deploy a token called <b>SAFE</b>."""
        token = {**TOKEN, "symbol": "<b>SAFE</b>"}
        message = format_alert(report=report(), token=token, event=EVENT)
        assert "<b>SAFE</b>" not in message
        assert "&lt;b&gt;SAFE&lt;/b&gt;" in message

    def test_a_link_cannot_be_injected_through_a_token_name(self):
        token = {**TOKEN, "name": '<a href="http://evil.invalid">Claim airdrop</a>'}
        message = format_alert(report=report(), token=token, event=EVENT)
        assert "evil.invalid" not in message or "&lt;a href" in message
        assert '<a href="http://evil.invalid">' not in message

    def test_escaping_applies_to_report_text_too(self):
        r = report(what_changed="price <script>alert(1)</script> moved")
        message = format_alert(report=r, token=TOKEN, event=EVENT)
        assert "<script>" not in message
        assert "&lt;script&gt;" in message

    def test_ampersands_are_escaped(self):
        token = {**TOKEN, "symbol": "A&B"}
        message = format_alert(report=report(), token=token, event=EVENT)
        assert "A&amp;B" in message

    def test_our_own_markup_still_works(self):
        message = format_alert(report=report(), token=TOKEN, event=EVENT)
        assert "<b>" in message and "</b>" in message

    def test_esc_handles_none(self):
        assert esc(None) == ""


# ===========================================================================
# Message content
# ===========================================================================

class TestAlertContent:
    def test_includes_everything_the_spec_requires(self):
        message = format_alert(report=report(), token=TOKEN, event=EVENT,
                               timezone="America/New_York")
        assert "TEST" in message                       # ticker
        assert "Test Token" in message                 # name
        assert "Base" in message                       # chain
        assert "0x" + "a" * 40 in message              # contract address
        assert "2026-11-18" in message                 # timestamp
        assert "liquidity" in message.lower()          # market data
        assert "Verified data" in message              # facts
        assert "Risk indicators" in message            # risk warnings
        assert "Sources" in message                    # source links

    def test_sections_stay_visually_separate(self):
        """The whole point of separate database columns."""
        message = format_alert(report=report(), token=TOKEN, event=EVENT)
        for header in ("Verified data", "Risk indicators", "Not checked"):
            assert f"<b>{header}</b>" in message

    def test_verified_data_is_labelled_as_provider_reported(self):
        message = format_alert(report=report(), token=TOKEN, event=EVENT)
        assert "reported by the provider" in message

    def test_assumptions_are_labelled_as_inference_when_present(self):
        r = report(assumptions=["The move may reflect profit taking."])
        message = format_alert(report=r, token=TOKEN, event=EVENT)
        assert "inference, not fact" in message

    def test_no_assumptions_section_when_there_are_none(self):
        """The template analyzer makes none, so the heading must not appear."""
        message = format_alert(report=report(assumptions=[]), token=TOKEN, event=EVENT)
        assert "Interpretation" not in message

    def test_carries_the_not_advice_disclaimer(self):
        message = format_alert(report=report(), token=TOKEN, event=EVENT)
        assert "Not investment advice" in message
        assert "buy or sell" in message

    def test_severity_marker_scales(self):
        critical = format_alert(report=report(), token=TOKEN,
                                event={**EVENT, "severity": "critical"})
        info = format_alert(report=report(), token=TOKEN,
                            event={**EVENT, "severity": "info"})
        assert "!!!" in critical
        assert "!!!" not in info

    def test_timestamp_is_rendered_in_local_time(self):
        message = format_alert(report=report(), token=TOKEN, event=EVENT,
                               timezone="America/New_York")
        assert "EST" in message or "EDT" in message

    def test_unknown_timezone_does_not_break_the_message(self):
        assert local_time(TRIGGERED, "Mars/Olympus") != ""

    def test_handles_a_missing_symbol(self):
        message = format_alert(report=report(),
                               token={"chain": "base", "token_address": "0xabc"},
                               event=EVENT)
        assert "Unknown" in message

    def test_only_http_links_are_rendered(self):
        r = report(sources=[{"label": "bad", "url": "javascript:alert(1)"},
                            {"label": "good", "url": "https://example.invalid"}])
        message = format_alert(report=r, token=TOKEN, event=EVENT)
        assert "javascript:" not in message
        assert "example.invalid" in message


class TestDailySummary:
    def test_includes_activity_and_health(self):
        message = format_daily_summary(
            stats={"new_tokens": 142, "tokens_seen": 1200, "snapshots_written": 900,
                   "watchlist_size": 85, "alerts": {"momentum": 6, "risk": 3},
                   "health": {"successful_runs": 288, "provider_errors": 4}},
            narrative="A busy day on Base.", summary_date="2026-11-18",
            timezone="America/New_York")
        assert "142" in message
        assert "MOMENTUM" in message
        assert "System health" in message
        assert "Provider errors: 4" in message

    def test_survives_empty_stats(self):
        message = format_daily_summary(stats={}, narrative=None,
                                       summary_date="2026-11-18",
                                       timezone="America/New_York")
        assert "DAILY RESEARCH SUMMARY" in message


class TestSystemAlert:
    def test_renders_and_escapes(self):
        message = format_system_alert("Scanner stalled", "No run since <b>09:00</b>")
        assert "Scanner stalled" in message
        assert "&lt;b&gt;09:00&lt;/b&gt;" in message


# ===========================================================================
# Truncation
# ===========================================================================

class TestTruncation:
    def test_short_messages_are_untouched(self):
        assert truncate_for_telegram("hello") == "hello"

    def test_long_messages_are_cut_below_the_hard_limit(self):
        text = "x" * 9000
        result = truncate_for_telegram(text)
        assert len(result) < MAX_MESSAGE_CHARS
        assert result.endswith("[truncated]")

    def test_cuts_at_a_line_boundary_where_possible(self):
        text = "\n".join(f"line {i} " + "y" * 60 for i in range(200))
        result = truncate_for_telegram(text)
        body = result.replace("\n\n[truncated]", "")
        assert not body.endswith("y" * 60) or "\n" in body

    def test_an_oversized_alert_is_sent_rather_than_rejected(self):
        """Telegram rejects over-long messages outright, so this matters."""
        r = report(verified_facts=["a fact " * 200] * 20,
                   missing_data=["something missing " * 100] * 10)
        message = format_alert(report=r, token=TOKEN, event=EVENT)
        assert len(truncate_for_telegram(message)) < MAX_MESSAGE_CHARS


# ===========================================================================
# Sending
# ===========================================================================

class TestSending:
    def test_successful_send(self):
        c, _ = client([FakeResponse(200, {"ok": True, "result": {"message_id": 42}})])
        result = c.send_message("hello")
        assert result.was_sent
        assert result.telegram_message_id == 42
        assert result.attempt == 1

    def test_dry_run_renders_without_sending(self, capsys):
        c, _ = client([], dry_run=True)
        result = c.send_message("hello world")
        assert result.status == "skipped"
        assert result.ok
        assert "hello world" in capsys.readouterr().out

    def test_missing_credentials_fails_with_a_useful_message(self):
        settings = Settings(telegram_bot_token=None, telegram_chat_id=None)
        result = TelegramClient(settings).send_message("hi")
        assert not result.ok
        assert "TELEGRAM_BOT_TOKEN" in (result.error_message or "")
        assert "DRY_RUN" in (result.error_message or "")

    def test_honours_retry_after_on_429(self):
        c, clock = client([
            FakeResponse(429, {"ok": False, "parameters": {"retry_after": 17}}),
            FakeResponse(200, {"ok": True, "result": {"message_id": 7}}),
        ])
        result = c.send_message("hi")
        assert result.was_sent
        assert 17 in clock.slept, f"expected a 17s wait, got {clock.slept}"

    def test_server_errors_are_retried(self):
        c, _ = client([FakeResponse(500, "boom"),
                       FakeResponse(200, {"ok": True, "result": {"message_id": 9}})])
        assert c.send_message("hi").was_sent

    def test_a_bad_token_is_not_retried_forever(self):
        """401 will never succeed; retrying would hammer Telegram."""
        transport = FakeTelegramTransport([
            FakeResponse(401, {"ok": False, "error_code": 401,
                               "description": "Unauthorized"})] * 5)
        clock = FakeClock()
        settings = Settings(telegram_bot_token="123456789:AAHbad",
                            telegram_chat_id="-100123")
        c = TelegramClient(settings, transport=transport, clock=clock,
                           sleep=clock.sleep)
        result = c.send_message("hi")
        assert not result.ok
        assert transport.calls and len(transport.calls) == 1

    def test_ok_false_with_http_200_is_still_a_failure(self):
        """Telegram answers 200 with ok:false for some errors."""
        c, _ = client([FakeResponse(200, {"ok": False, "error_code": 400,
                                          "description": "chat not found"})])
        result = c.send_message("hi")
        assert not result.ok
        assert "chat not found" in (result.error_message or "")

    def test_network_failure_is_retried_then_reported(self):
        c, _ = client([TelegramError("network failure", retriable=True)] * 5)
        result = c.send_message("hi")
        assert not result.ok
        assert result.status == "failed"

    def test_send_never_raises(self):
        c, _ = client([FakeResponse(500, "boom")] * 10)
        result = c.send_message("hi")       # must not raise
        assert isinstance(result, SendResult)

    def test_rate_limiting_applies(self):
        c, clock = client([FakeResponse(200, {"ok": True, "result": {"message_id": 1}})])
        for _ in range(25):
            c.send_message("hi")
        assert clock.total_slept > 0, "should have throttled past the per-minute limit"

    def test_message_uses_html_parse_mode(self):
        transport = FakeTelegramTransport([
            FakeResponse(200, {"ok": True, "result": {"message_id": 1}})])
        clock = FakeClock()
        settings = Settings(telegram_bot_token="123456789:AAHtoken",
                            telegram_chat_id="-100123")
        TelegramClient(settings, transport=transport, clock=clock,
                       sleep=clock.sleep).send_message("<b>hi</b>")
        assert transport.calls[0]["payload"]["parse_mode"] == "HTML"
