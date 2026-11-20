"""Tests for the daily research summary.

Three things are worth testing here and the rest is bookkeeping:

1. **The day window**, because "yesterday" is 23 or 25 hours long twice a
   year and a wrong window silently double-counts or drops an hour of alerts.
2. **Not sending twice.** The job is run by a scheduler that can fire late,
   retry, or be triggered by hand on a day already covered.
3. **Silence must not read as calm.** A day where no scanner ran has to say
   so, not print a tidy column of zeros that looks like a quiet market.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pytest

from mihq.config import Settings
from mihq.notify.formatters import format_daily_summary
from mihq.notify.telegram import SendResult
from mihq.summary import (
    DailySummaryJob,
    build_stats,
    compose_narrative,
    day_window,
    default_summary_date,
    _headline_for,
)

TZ = "America/New_York"


def settings(**overrides: Any) -> Settings:
    defaults = dict(
        timezone=TZ,
        telegram_bot_token="123456789:AAHtesttokenvaluethatislongenough12345",
        telegram_chat_id="1000",
        # The provider is never wanted in a unit test - see the guard test at
        # the bottom of this file.
        security_checks_enabled=False,
    )
    defaults.update(overrides)
    return Settings(**defaults)


class FakeRepo:
    """Returns scripted rows and records what it was asked for."""

    def __init__(self, **overrides: Any) -> None:
        self.run_totals = overrides.get("run_totals", [])
        self.alerts = overrides.get("alerts", [])
        self.highlights = overrides.get("highlights", [])
        self.risk_flags = overrides.get("risk_flags", [])
        self.delta = overrides.get("delta", (0, 0))
        self.watchlist_size = overrides.get("watchlist_size", 0)
        self.existing_status = overrides.get("existing_status", "pending")
        self.failing: set[str] = set(overrides.get("failing", ()))

        self.windows: list[tuple[datetime, datetime]] = []
        self.upserts: list[dict[str, Any]] = []
        self.marked: list[dict[str, Any]] = []
        self.logged: list[dict[str, Any]] = []

    def _check(self, name: str) -> None:
        if name in self.failing:
            raise RuntimeError(f"simulated {name} failure")

    def summary_run_totals(self, start, end):
        self.windows.append((start, end))
        self._check("run_totals")
        return self.run_totals

    def summary_alerts(self, start, end):
        self._check("alerts")
        return self.alerts

    def summary_highlights(self, start, end, limit=5):
        self._check("highlights")
        return self.highlights[:limit]

    def summary_risk_flags_raised(self, start, end):
        self._check("risk_flags")
        return self.risk_flags

    def summary_watchlist_delta(self, start, end):
        self._check("delta")
        return self.delta

    def active_watchlist_count(self):
        self._check("watchlist_size")
        return self.watchlist_size

    def upsert_daily_summary(self, **kwargs):
        self._check("upsert")
        self.upserts.append(kwargs)
        return (7, self.existing_status, False)

    def mark_daily_summary_sent(self, summary_id, *, status, telegram_message_id):
        self.marked.append({"id": summary_id, "status": status,
                            "message_id": telegram_message_id})

    def log_notification(self, **kwargs):
        self.logged.append(kwargs)


class FakeClient:
    def __init__(self, result: SendResult | None = None) -> None:
        self.result = result or SendResult(ok=True, status="sent",
                                           telegram_message_id=55, chat_id="1000")
        self.sent: list[str] = []

    def send_message(self, text, **_kwargs):
        self.sent.append(text)
        return self.result


def run_totals(**overrides: Any) -> dict[str, Any]:
    row = dict(
        scanner="discovery", runs=10, successful=10, partial=0, failed=0,
        still_running=0, tokens_seen=400, new_tokens=37, snapshots_written=390,
        events_created=12, events_suppressed=3, alerts_sent=5,
        provider_errors=0, telegram_errors=0, database_errors=0,
        last_finished=datetime(2026, 11, 19, 23, 50, tzinfo=timezone.utc),
    )
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# The day window
# ---------------------------------------------------------------------------

class TestDayWindow:
    def test_an_ordinary_day_is_24_hours(self):
        start, end = day_window(date(2026, 6, 15), TZ)
        assert (end - start).total_seconds() == 24 * 3600

    def test_spring_forward_day_is_23_hours(self):
        """The clocks jump forward, so this local day is an hour short.

        A fixed `start + timedelta(hours=24)` would reach an hour into the
        next day and count its alerts twice - once here, once tomorrow.
        """
        start, end = day_window(date(2026, 3, 8), TZ)
        assert (end - start).total_seconds() == 23 * 3600

    def test_fall_back_day_is_25_hours(self):
        """An hour repeats, so this day is an hour long - and that hour's
        alerts belong to it, not to nobody."""
        start, end = day_window(date(2026, 11, 1), TZ)
        assert (end - start).total_seconds() == 25 * 3600

    def test_boundaries_are_local_midnight(self):
        start, end = day_window(date(2026, 11, 19), TZ)
        # EST is UTC-5, so local midnight is 05:00 UTC.
        assert start == datetime(2026, 11, 19, 5, 0, tzinfo=timezone.utc)
        assert end == datetime(2026, 11, 20, 5, 0, tzinfo=timezone.utc)

    def test_consecutive_windows_meet_exactly(self):
        """No gap and no overlap across a DST change, so every moment of the
        year falls in exactly one summary."""
        _, first_end = day_window(date(2026, 10, 31), TZ)
        second_start, _ = day_window(date(2026, 11, 1), TZ)
        assert first_end == second_start

    def test_utc_timezone_also_works(self):
        start, end = day_window(date(2026, 11, 19), "UTC")
        assert start == datetime(2026, 11, 19, 0, 0, tzinfo=timezone.utc)
        assert (end - start).total_seconds() == 24 * 3600


class TestDefaultDate:
    def test_defaults_to_yesterday_locally(self):
        now = datetime(2026, 11, 20, 13, 0, tzinfo=timezone.utc)  # 08:00 EST
        assert default_summary_date(now, TZ) == date(2026, 11, 19)

    def test_uses_the_local_date_not_the_utc_one(self):
        """03:00 UTC on the 20th is still the evening of the 19th in New York,
        so 'yesterday' is the 18th, not the 19th."""
        now = datetime(2026, 11, 20, 3, 0, tzinfo=timezone.utc)
        assert default_summary_date(now, TZ) == date(2026, 11, 18)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

class TestBuildStats:
    def test_totals_are_summed_across_scanners(self):
        repo = FakeRepo(run_totals=[
            run_totals(scanner="discovery", new_tokens=37, successful=10),
            run_totals(scanner="watchlist", new_tokens=0, successful=40,
                       snapshots_written=1200),
        ])
        stats, _, _ = build_stats(repo, *day_window(date(2026, 11, 19), TZ))
        assert stats["new_tokens"] == 37
        assert stats["snapshots_written"] == 390 + 1200
        assert stats["health"]["successful_runs"] == 50

    def test_alerts_are_counted_by_category_and_severity(self):
        repo = FakeRepo(alerts=[
            ("momentum", "medium", 4),
            ("momentum", "high", 2),
            ("risk", "critical", 1),
        ])
        stats, _, _ = build_stats(repo, *day_window(date(2026, 11, 19), TZ))
        assert stats["alerts"] == {"momentum": 6, "risk": 1}
        assert stats["alerts_by_severity"] == {"medium": 4, "high": 2, "critical": 1}

    def test_the_window_is_passed_to_the_queries(self):
        repo = FakeRepo()
        start, end = day_window(date(2026, 11, 19), TZ)
        build_stats(repo, start, end)
        assert repo.windows == [(start, end)]

    def test_one_failing_query_does_not_lose_the_rest(self):
        """A partial digest naming what is missing beats no digest."""
        repo = FakeRepo(run_totals=[run_totals()], failing={"highlights"})
        stats, highlights, _ = build_stats(repo, *day_window(date(2026, 11, 19), TZ))
        assert stats["new_tokens"] == 37          # survived
        assert highlights == []                   # lost
        assert any("highlights" in e for e in stats["query_errors"])

    def test_no_data_at_all_produces_zeros_not_an_exception(self):
        stats, highlights, changes = build_stats(
            FakeRepo(), *day_window(date(2026, 11, 19), TZ)
        )
        assert stats["new_tokens"] == 0
        assert stats["alerts"] == {}
        assert highlights == []
        assert changes == {"added": 0, "removed": 0}


class TestHeadlines:
    def test_names_the_token_and_what_was_measured(self):
        row = ("base", "PEPE", "0xabc", "volume_spike", "high",
               datetime(2026, 11, 19, 12, tzinfo=timezone.utc), {"change_pct": 240.0})
        headline = _headline_for(row)
        assert "PEPE" in headline and "base" in headline
        assert "volume spike" in headline
        assert "+240%" in headline

    def test_falls_back_to_a_short_address_when_there_is_no_symbol(self):
        row = ("solana", None, "So11111111111111111111111111111111111111112",
               "new_token", "info", datetime(2026, 11, 19, tzinfo=timezone.utc), {})
        assert "So1111...1112" in _headline_for(row)

    def test_a_missing_change_is_omitted_not_shown_as_zero(self):
        row = ("bsc", "X", "0x1", "churn_ratio", "medium",
               datetime(2026, 11, 19, tzinfo=timezone.utc), None)
        assert "0%" not in _headline_for(row)

    def test_payload_arriving_as_json_text_is_still_read(self):
        row = ("base", "Y", "0x2", "price_surge", "high",
               datetime(2026, 11, 19, tzinfo=timezone.utc), '{"change_pct": 31.5}')
        assert "+32%" in _headline_for(row)


# ---------------------------------------------------------------------------
# The narrative
# ---------------------------------------------------------------------------

class TestNarrative:
    def test_a_day_with_no_successful_run_says_so_first(self):
        text = compose_narrative({"health": {"successful_runs": 0, "failed_runs": 0}})
        assert "No scanner pass ran" in text
        assert "not a quiet market" in text

    def test_failures_are_named_rather_than_summarised_as_quiet(self):
        text = compose_narrative({"health": {"successful_runs": 0, "failed_runs": 6}})
        assert "6 failed" in text
        assert "not a picture of the market" in text

    def test_a_normal_day_states_the_counts(self):
        text = compose_narrative({
            "health": {"successful_runs": 48, "failed_runs": 0},
            "new_tokens": 37,
            "alerts": {"momentum": 6, "risk": 2},
            "alerts_by_severity": {"high": 3, "medium": 5},
        })
        assert "48 scanner passes" in text
        assert "37 tokens" in text
        assert "8 alerts" in text
        assert "3 of them high or critical" in text

    def test_a_quiet_but_healthy_day_is_described_as_measured(self):
        text = compose_narrative({
            "health": {"successful_runs": 48, "failed_runs": 0},
            "new_tokens": 0, "alerts": {}, "alerts_by_severity": {},
        })
        assert "No alerts crossed their thresholds" in text

    def test_partial_coverage_is_disclosed(self):
        text = compose_narrative({
            "health": {"successful_runs": 20, "failed_runs": 28},
            "new_tokens": 1, "alerts": {}, "alerts_by_severity": {},
        })
        assert "coverage for this period is incomplete" in text

    @pytest.mark.parametrize("banned", [
        "buy", "sell", "opportunity", "bullish", "bearish", "should",
        "recommend", "promising", "potential gain", "rug",
    ])
    def test_the_narrative_never_evaluates(self, banned):
        """It reports what was counted. It does not say what it means."""
        for stats in [
            {"health": {"successful_runs": 0, "failed_runs": 0}},
            {"health": {"successful_runs": 48, "failed_runs": 2},
             "new_tokens": 37, "alerts": {"momentum": 6},
             "alerts_by_severity": {"critical": 1},
             "risk_flags_raised": {"low_liquidity": 3}},
        ]:
            assert banned not in compose_narrative(stats).lower()


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------

class TestDailySummaryJob:
    def test_builds_stores_and_sends(self):
        repo = FakeRepo(run_totals=[run_totals()])
        client = FakeClient()
        result = DailySummaryJob(settings(), repo, client).run(
            for_date=date(2026, 11, 19)
        )
        assert result.stored and result.sent
        assert len(client.sent) == 1
        assert repo.marked == [{"id": 7, "status": "sent", "message_id": 55}]

    def test_does_not_send_twice_for_the_same_date(self):
        """The guard is the stored status, so it holds across processes -
        a retry on another runner cannot resend."""
        repo = FakeRepo(run_totals=[run_totals()], existing_status="sent")
        client = FakeClient()
        result = DailySummaryJob(settings(), repo, client).run(
            for_date=date(2026, 11, 19)
        )
        assert client.sent == []
        assert result.sent is False
        assert result.skipped_reason == "already sent for this date"
        assert result.stored is True       # numbers were still refreshed

    def test_force_resends_a_day_already_sent(self):
        repo = FakeRepo(run_totals=[run_totals()], existing_status="sent")
        client = FakeClient()
        DailySummaryJob(settings(), repo, client).run(
            for_date=date(2026, 11, 19), force=True
        )
        assert len(client.sent) == 1

    def test_a_dry_run_is_not_recorded_as_sent(self):
        """Otherwise the real message is suppressed forever once a token is
        configured, because the row already claims it went out."""
        repo = FakeRepo(run_totals=[run_totals()])
        client = FakeClient(SendResult(ok=True, status="skipped"))
        result = DailySummaryJob(settings(), repo, client).run(
            for_date=date(2026, 11, 19)
        )
        assert repo.marked == []
        assert result.sent is False
        assert "skipped" in (result.skipped_reason or "")

    def test_a_failed_send_is_recorded_as_failed(self):
        repo = FakeRepo(run_totals=[run_totals()])
        client = FakeClient(SendResult(ok=False, status="failed",
                                       error_code=400, error_message="bad request"))
        result = DailySummaryJob(settings(), repo, client).run(
            for_date=date(2026, 11, 19)
        )
        assert repo.marked == [{"id": 7, "status": "failed", "message_id": None}]
        assert any("bad request" in e for e in result.errors)

    def test_every_attempt_is_logged_including_failures(self):
        repo = FakeRepo(run_totals=[run_totals()])
        client = FakeClient(SendResult(ok=False, status="failed", error_code=429))
        DailySummaryJob(settings(), repo, client).run(for_date=date(2026, 11, 19))
        assert repo.logged[0]["message_type"] == "daily_summary"
        assert repo.logged[0]["status"] == "failed"

    def test_a_storage_failure_stops_before_sending(self):
        """Sending a message we could not record would make the next run
        send it again."""
        repo = FakeRepo(run_totals=[run_totals()], failing={"upsert"})
        client = FakeClient()
        result = DailySummaryJob(settings(), repo, client).run(
            for_date=date(2026, 11, 19)
        )
        assert client.sent == []
        assert result.stored is False
        assert any("could not store" in e for e in result.errors)

    def test_an_invalid_timezone_fails_without_raising(self):
        repo = FakeRepo()
        result = DailySummaryJob(settings(timezone="Mars/Olympus"), repo,
                                 FakeClient()).run(for_date=date(2026, 11, 19))
        assert result.stored is False
        assert any("Mars/Olympus" in e for e in result.errors)

    def test_the_window_is_recorded_in_the_stored_stats(self):
        """So a reader can later tell exactly what period a summary covered,
        including on the days that were not 24 hours long."""
        repo = FakeRepo(run_totals=[run_totals()])
        DailySummaryJob(settings(), repo, FakeClient()).run(
            for_date=date(2026, 11, 1)
        )
        stored = repo.upserts[0]["stats"]
        assert stored["window_hours"] == 25.0
        assert stored["window_start_utc"].startswith("2026-11-01")

    def test_defaults_to_yesterday_when_no_date_is_given(self):
        repo = FakeRepo(run_totals=[run_totals()])
        DailySummaryJob(settings(), repo, FakeClient()).run(
            now_utc=datetime(2026, 11, 20, 13, 0, tzinfo=timezone.utc)
        )
        assert repo.upserts[0]["summary_date"] == date(2026, 11, 19)


class TestRenderedMessage:
    def test_health_is_shown_even_on_a_silent_day(self):
        """The whole point of a daily message is that it arrives when there
        is nothing to say."""
        message = format_daily_summary(
            stats={"health": {"successful_runs": 0, "provider_errors": 0}},
            narrative="No scanner pass ran at all during this period.",
            summary_date="2026-11-19", timezone=TZ,
        )
        assert "System health" in message
        assert "Successful scanner runs: 0" in message

    def test_failed_runs_are_shown_when_there_are_any(self):
        message = format_daily_summary(
            stats={"health": {"successful_runs": 20, "failed_runs": 28,
                              "provider_errors": 3}},
            narrative="", summary_date="2026-11-19", timezone=TZ,
        )
        assert "FAILED scanner runs: 28" in message

    def test_a_clean_day_does_not_print_a_failed_line(self):
        message = format_daily_summary(
            stats={"health": {"successful_runs": 48, "failed_runs": 0,
                              "provider_errors": 0}},
            narrative="", summary_date="2026-11-19", timezone=TZ,
        )
        assert "FAILED" not in message

    def test_a_hostile_token_symbol_cannot_style_the_message(self):
        """Symbols are attacker-controlled; this is the same guard the alert
        formatter has, checked again on the path a summary takes."""
        message = format_daily_summary(
            stats={}, narrative="", summary_date="2026-11-19", timezone=TZ,
            highlights=[{"headline": "<b>SAFE</b> <a href='http://evil'>click</a>"}],
        )
        assert "&lt;b&gt;SAFE&lt;/b&gt;" in message
        assert "&lt;a href='http://evil'&gt;click&lt;/a&gt;" in message
        # Scoped to the attacker's own line: the message legitimately
        # contains our <b> section headers, so a whole-message check would
        # be testing the formatter's markup rather than the escaping.
        hostile_line = next(line for line in message.splitlines() if "SAFE" in line)
        assert "<" not in hostile_line
        assert ">" not in hostile_line

    def test_it_carries_the_disclaimer(self):
        message = format_daily_summary(stats={}, narrative="",
                                       summary_date="2026-11-19", timezone=TZ)
        assert "Not investment advice" in message

    def test_it_fits_in_one_telegram_message(self):
        """Telegram rejects an over-long message outright, so it would not
        arrive truncated - it would not arrive."""
        stats = {
            "new_tokens": 999, "tokens_seen": 99999, "snapshots_written": 99999,
            "watchlist_size": 9999,
            "alerts": {f"category_{i}": 99 for i in range(8)},
            "health": {"successful_runs": 9999, "failed_runs": 99,
                       "partial_runs": 99, "provider_errors": 999,
                       "last_success": "2026-11-19T23:59:00+00:00"},
        }
        message = format_daily_summary(
            stats=stats, narrative="x" * 600, summary_date="2026-11-19",
            timezone=TZ,
            highlights=[{"headline": "y" * 120} for _ in range(5)],
        )
        assert len(message) < 4096


class TestNoNetwork:
    def test_security_checks_are_off_in_these_tests(self):
        """A guard, not a behaviour test. When the security provider was
        wired in, the suite silently started making live HTTP calls and the
        runtime went from 0.3s to 46s. This fails loudly instead."""
        assert settings().security_checks_enabled is False


class TestGrammar:
    """Small, but the summary arrives every day - a visible typo in a daily
    message erodes trust in the numbers beside it."""

    def test_singular_pass_and_token(self):
        text = compose_narrative({
            "health": {"successful_runs": 1, "failed_runs": 1},
            "new_tokens": 1, "alerts": {"risk": 1},
            "alerts_by_severity": {"high": 1},
            "risk_flags_raised": {"low_liquidity": 1},
        })
        assert "1 scanner pass completed" in text
        assert "1 token not seen before" in text
        assert "1 alert was raised" in text
        assert "1 risk flag was raised across 1 category" in text
        assert "1 pass failed" in text
        assert "passes" not in text

    def test_plural_forms(self):
        text = compose_narrative({
            "health": {"successful_runs": 4, "failed_runs": 2},
            "new_tokens": 9, "alerts": {"risk": 3},
            "alerts_by_severity": {"high": 3},
            "risk_flags_raised": {"a": 1, "b": 1},
        })
        assert "4 scanner passes completed" in text
        assert "9 tokens not seen before" in text
        assert "3 alerts were raised" in text
        assert "2 risk flags were raised across 2 categories" in text
        assert "2 passes failed" in text
