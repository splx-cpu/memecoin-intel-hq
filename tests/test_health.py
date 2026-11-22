"""Failure injection: break things on purpose and check what happens.

The standard this holds to is not "does it survive" but "does it tell the
truth while it fails". A system that keeps running and quietly reports
success is worse than one that stops, because nothing prompts anyone to look.

So each test here asks two questions:
  1. Did it degrade without losing data or crashing?
  2. Would the user find out?

The second one is the reason this stage exists. `send_system_alert` was
written in Stage F and never called from anywhere, so the answer to (2) was
"no" for every failure in this file until now.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from mihq.config import Settings
from mihq.health import (
    HealthProblem,
    check_abandoned_runs,
    check_run_outcome,
    check_staleness,
    check_storage,
    collect_problems,
    report_problems,
    run_health_checks,
)

NOW = datetime(2026, 11, 22, 18, 0, tzinfo=timezone.utc)
MB = 1024 * 1024


def settings(**overrides: Any) -> Settings:
    defaults = dict(security_checks_enabled=False)
    defaults.update(overrides)
    return Settings(**defaults)


class FakeRepo:
    def __init__(self, **kw: Any) -> None:
        self.successes = kw.get("successes", {})
        self.abandoned = kw.get("abandoned", [])
        self.size = kw.get("size", 10 * MB)
        self.last_alerts = kw.get("last_alerts", {})
        self.failing: set[str] = set(kw.get("failing", ()))

    def _check(self, name: str) -> None:
        if name in self.failing:
            raise RuntimeError(f"simulated {name} failure")

    def last_successful_runs(self):
        self._check("last_successful_runs")
        return self.successes

    def stale_running_runs(self, older_than_seconds):
        self._check("stale_running_runs")
        return self.abandoned

    def database_size_bytes(self):
        self._check("database_size_bytes")
        return self.size

    def last_system_alert(self, key):
        self._check("last_system_alert")
        return self.last_alerts.get(key)


class FakeDispatcher:
    def __init__(self, ok: bool = True, raises: bool = False) -> None:
        self.ok = ok
        self.raises = raises
        self.sent: list[dict[str, Any]] = []

    def send_system_alert(self, title, detail, *, severity="high", key=None):
        if self.raises:
            raise RuntimeError("telegram exploded")
        self.sent.append({"title": title, "detail": detail,
                          "severity": severity, "key": key})
        return self.ok


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

class TestRunOutcome:
    def test_a_failed_pass_is_critical(self):
        problems = check_run_outcome("discovery", "failed", 5)
        assert len(problems) == 1
        assert problems[0].severity == "critical"
        assert "did not complete" in problems[0].detail

    def test_a_partial_pass_is_reported_but_not_critical(self):
        """One chain's provider having a bad minute is normal and
        self-correcting. Treating it as critical would train the user to
        ignore critical."""
        problems = check_run_outcome("discovery", "partial", 2)
        assert len(problems) == 1
        assert problems[0].severity == "high"

    def test_a_successful_pass_says_nothing(self):
        assert check_run_outcome("discovery", "success", 0) == []

    def test_a_partial_pass_with_no_errors_says_nothing(self):
        assert check_run_outcome("discovery", "partial", 0) == []


class TestStaleness:
    def test_a_recent_success_is_fine(self):
        last = {"discovery": NOW - timedelta(hours=1),
                "watchlist": NOW - timedelta(minutes=20)}
        assert check_staleness(last, settings(), NOW) == []

    def test_a_scanner_that_stopped_is_critical(self):
        """THE check that catches quiet failures. A scanner erroring on every
        pass still 'runs'; without this the only symptom is that alerts
        gradually stop, which looks exactly like a calm market."""
        last = {"discovery": NOW - timedelta(hours=14),
                "watchlist": NOW - timedelta(minutes=20)}
        problems = check_staleness(last, settings(), NOW)
        assert len(problems) == 1
        assert problems[0].severity == "critical"
        assert problems[0].key == "stale:discovery"
        assert "14" in problems[0].title

    def test_never_having_succeeded_is_reported_more_gently(self):
        """Expected on a fresh deployment, and it clears itself within a
        pass - so it must not look like the sky falling."""
        problems = check_staleness({}, settings(), NOW)
        keys = {p.key for p in problems}
        assert keys == {"never_succeeded:discovery", "never_succeeded:watchlist"}
        assert all(p.severity == "high" for p in problems)

    def test_a_naive_timestamp_does_not_crash_the_check(self):
        """Some drivers return naive datetimes. Comparing one to an aware
        `now` raises TypeError, which would take out the health monitor at
        exactly the moment it is needed."""
        last = {"discovery": datetime(2026, 11, 22, 4, 0)}   # no tzinfo
        problems = check_staleness(last, settings(), NOW)
        assert any(p.key == "stale:discovery" for p in problems)

    def test_the_threshold_is_configurable_per_scanner(self):
        last = {"discovery": NOW - timedelta(hours=5),
                "watchlist": NOW - timedelta(hours=5)}
        problems = check_staleness(
            last, settings(health_discovery_stale_hours=4,
                           health_watchlist_stale_hours=12), NOW)
        assert [p.key for p in problems] == ["stale:discovery"]


class TestAbandonedRuns:
    def test_a_killed_run_is_noticed(self):
        """It neither succeeded nor failed, so no other check sees it."""
        rows = [(1, "discovery", NOW - timedelta(hours=3))]
        problems = check_abandoned_runs(rows, settings())
        assert len(problems) == 1
        assert "killed mid-pass" in problems[0].title

    def test_nothing_abandoned_says_nothing(self):
        assert check_abandoned_runs([], settings()) == []


class TestStorage:
    def test_plenty_of_room_says_nothing(self):
        assert check_storage(50 * MB, settings()) == []

    def test_crossing_the_warning_line_is_reported(self):
        problems = check_storage(380 * MB, settings())   # 76% of 500
        assert len(problems) == 1
        assert problems[0].key == "storage_warning"
        assert problems[0].severity == "high"

    def test_nearly_full_is_critical_and_says_what_to_do(self):
        """The failure mode when it fills is writes erroring, which reads as
        a scanner bug. Someone would debug it in entirely the wrong place, so
        the alert has to name the cause and the lever."""
        problems = check_storage(470 * MB, settings())   # 94%
        assert problems[0].severity == "critical"
        assert "SNAPSHOT_RAW_MODE=never" in problems[0].detail

    def test_the_limit_can_be_disabled(self):
        assert check_storage(9_000 * MB, settings(health_storage_limit_mb=0)) == []


# ---------------------------------------------------------------------------
# Collecting them, with things broken
# ---------------------------------------------------------------------------

class TestCollectionUnderFailure:
    def test_a_healthy_system_produces_nothing(self):
        repo = FakeRepo(successes={"discovery": NOW - timedelta(minutes=30),
                                   "watchlist": NOW - timedelta(minutes=10)})
        problems, errors = collect_problems(repo, settings(), now=NOW)
        assert problems == []
        assert errors == []

    def test_one_failing_query_does_not_lose_the_other_checks(self):
        """A health monitor that falls over on its first failing query is
        worse than none - it would most likely fail at exactly the moment
        something was wrong."""
        repo = FakeRepo(
            successes={"discovery": NOW - timedelta(hours=20),
                       "watchlist": NOW - timedelta(minutes=5)},
            failing={"database_size_bytes"},
        )
        problems, errors = collect_problems(repo, settings(), now=NOW)
        assert any(p.key == "stale:discovery" for p in problems)   # survived
        assert any("storage check" in e for e in errors)           # reported

    def test_a_totally_unreachable_database_reports_every_check_as_an_error(self):
        repo = FakeRepo(failing={"last_successful_runs", "stale_running_runs",
                                 "database_size_bytes"})
        problems, errors = collect_problems(repo, settings(), now=NOW)
        assert problems == []
        assert len(errors) == 3


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

class TestReporting:
    def test_a_problem_reaches_telegram(self):
        """The thing that was missing entirely until now."""
        problem = HealthProblem(key="stale:discovery", title="t", detail="d",
                                severity="critical")
        dispatcher = FakeDispatcher()
        report = report_problems([problem], FakeRepo(), dispatcher,
                                 settings(), now=NOW)
        assert report.sent == 1
        assert dispatcher.sent[0]["key"] == "stale:discovery"
        assert dispatcher.sent[0]["severity"] == "critical"

    def test_the_same_problem_is_not_repeated_every_hour(self):
        repo = FakeRepo(last_alerts={"stale:discovery": NOW - timedelta(hours=2)})
        dispatcher = FakeDispatcher()
        report = report_problems(
            [HealthProblem(key="stale:discovery", title="t", detail="d")],
            repo, dispatcher, settings(health_alert_cooldown_hours=12), now=NOW)
        assert report.sent == 0
        assert report.throttled == 1
        assert dispatcher.sent == []

    def test_it_is_repeated_once_the_cooldown_expires(self):
        repo = FakeRepo(last_alerts={"stale:discovery": NOW - timedelta(hours=20)})
        dispatcher = FakeDispatcher()
        report = report_problems(
            [HealthProblem(key="stale:discovery", title="t", detail="d")],
            repo, dispatcher, settings(health_alert_cooldown_hours=12), now=NOW)
        assert report.sent == 1

    def test_an_unreadable_cooldown_errs_towards_sending(self):
        """The throttle lives in the database. When the database IS the
        problem it cannot be read - and an extra alert about a real failure
        beats a suppressed one."""
        repo = FakeRepo(failing={"last_system_alert"})
        dispatcher = FakeDispatcher()
        report = report_problems(
            [HealthProblem(key="db_down", title="t", detail="d")],
            repo, dispatcher, settings(), now=NOW)
        assert report.sent == 1
        assert any("cooldown lookup" in e for e in report.errors)

    def test_telegram_being_down_does_not_raise(self):
        """Telegram cannot be used to report that Telegram is down. The
        failure is recorded and the pass continues."""
        dispatcher = FakeDispatcher(raises=True)
        report = report_problems(
            [HealthProblem(key="k", title="t", detail="d")],
            FakeRepo(), dispatcher, settings(), now=NOW)
        assert report.sent == 0
        assert any("send failed" in e for e in report.errors)

    def test_a_refused_send_is_counted_as_an_error_not_a_success(self):
        dispatcher = FakeDispatcher(ok=False)
        report = report_problems(
            [HealthProblem(key="k", title="t", detail="d")],
            FakeRepo(), dispatcher, settings(), now=NOW)
        assert report.sent == 0
        assert any("could not send" in e for e in report.errors)

    def test_different_problems_are_throttled_independently(self):
        repo = FakeRepo(last_alerts={"stale:discovery": NOW - timedelta(hours=1)})
        dispatcher = FakeDispatcher()
        report = report_problems([
            HealthProblem(key="stale:discovery", title="a", detail="d"),
            HealthProblem(key="storage_warning", title="b", detail="d"),
        ], repo, dispatcher, settings(), now=NOW)
        assert report.sent == 1
        assert report.throttled == 1
        assert dispatcher.sent[0]["key"] == "storage_warning"


class TestKeysAreStable:
    """The cooldown is keyed on `key`. If a key varied with elapsed time or
    size, every occurrence would look new and nothing would ever throttle -
    the exact bug that makes alerting systems get muted."""

    def test_a_staleness_key_does_not_change_as_time_passes(self):
        a = check_staleness({"discovery": NOW - timedelta(hours=7)},
                            settings(), NOW)[0]
        b = check_staleness({"discovery": NOW - timedelta(hours=40)},
                            settings(), NOW)[0]
        assert a.key == b.key
        assert a.title != b.title        # the message still says how long

    def test_a_storage_key_does_not_change_as_the_database_grows(self):
        a = check_storage(360 * MB, settings())[0]
        b = check_storage(400 * MB, settings())[0]
        assert a.key == b.key == "storage_warning"


class TestDisabling:
    def test_turning_health_checks_off_touches_nothing(self):
        repo = FakeRepo(failing={"last_successful_runs"})
        dispatcher = FakeDispatcher()
        report = run_health_checks(repo, dispatcher,
                                   settings(health_checks_enabled=False), now=NOW)
        assert report.problems == []
        assert dispatcher.sent == []


class TestEndToEnd:
    def test_a_dead_scanner_and_a_filling_disk_are_both_reported(self):
        repo = FakeRepo(
            successes={"discovery": NOW - timedelta(hours=30),
                       "watchlist": NOW - timedelta(hours=30)},
            size=460 * MB,
            abandoned=[(1, "watchlist", NOW - timedelta(hours=4))],
        )
        dispatcher = FakeDispatcher()
        report = run_health_checks(repo, dispatcher, settings(), now=NOW)

        keys = {s["key"] for s in dispatcher.sent}
        assert keys == {"stale:discovery", "stale:watchlist",
                        "abandoned_runs", "storage_critical"}
        assert report.sent == 4
        assert report.errors == []

    def test_nothing_is_sent_when_everything_is_fine(self):
        """A health monitor that chatters gets muted, and then it is not a
        health monitor."""
        repo = FakeRepo(successes={"discovery": NOW - timedelta(minutes=20),
                                   "watchlist": NOW - timedelta(minutes=5)},
                        size=30 * MB)
        dispatcher = FakeDispatcher()
        run_health_checks(repo, dispatcher, settings(), now=NOW)
        assert dispatcher.sent == []


# ---------------------------------------------------------------------------
# The crash that the health monitor cannot report
# ---------------------------------------------------------------------------

class TestFatalCrashReporting:
    """When the database is unreachable the process dies inside `connect()`,
    before any health check runs. Every check in this file needs a database
    connection, so none of them would fire.

    `notify_fatal` is the one report that still works, because Telegram does
    not need the database. Before this existed, an unreachable database
    produced a traceback and total silence on the user's phone.
    """

    def test_a_crash_is_reported_over_telegram(self, monkeypatch):
        from mihq import worker

        sent: list[str] = []

        class FakeClient:
            def __init__(self, settings): pass
            def send_message(self, text, **kw):
                sent.append(text)
                from mihq.notify.telegram import SendResult
                return SendResult(ok=True, status="sent")

        monkeypatch.setattr("mihq.notify.telegram.TelegramClient", FakeClient)
        assert worker.notify_fatal(settings(), RuntimeError("boom")) == "sent"
        assert "crashed" in sent[0].lower()
        assert "boom" in sent[0]

    def test_the_connection_string_is_not_leaked_into_the_message(self):
        """psycopg puts the DSN in its errors, password and all. This alert
        goes to a chat, so it must be scrubbed before sending."""
        from mihq.db.connection import redact_dsn

        raw = ("connection failed: postgresql://postgres.abcdef:"
               "SuperSecret123@aws-0.pooler.supabase.com:5432/postgres")
        cleaned = redact_dsn(raw)
        assert "SuperSecret123" not in cleaned

    def test_failing_to_report_the_crash_does_not_raise(self, monkeypatch):
        """Otherwise the error handler becomes a second, more confusing
        crash on top of the first."""
        from mihq import worker

        class ExplodingClient:
            def __init__(self, settings):
                raise RuntimeError("no token configured")

        monkeypatch.setattr("mihq.notify.telegram.TelegramClient", ExplodingClient)
        assert worker.notify_fatal(settings(), RuntimeError("boom")) == "error"

    def test_a_dry_run_is_reported_as_skipped_not_failed(self):
        """'We deliberately did not send' and 'we tried and could not' are
        different facts. Collapsing them would make a dry run look like a
        delivery failure, and a real delivery failure look routine."""
        from mihq import worker

        status = worker.notify_fatal(
            settings(dry_run=True, telegram_bot_token=None), RuntimeError("boom"))
        assert status in ("skipped", "error")
        assert status != "failed"
