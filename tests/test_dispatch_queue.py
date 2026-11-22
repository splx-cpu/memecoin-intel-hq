"""Tests for the notification queue.

THIS FILE EXISTS BECAUSE THE QUEUE HAD NO TESTS AT ALL.

`NotificationDispatcher.dispatch_pending` - the thing that decides which
alerts reach the user's phone - was never called by the suite. Changes to it
could not fail a test, and the `try/except` around the queue read meant even
a TypeError would be swallowed and reported as "0 pending" rather than as an
error. A green suite said nothing about this code.

The property these tests are really protecting: **a real alert must never be
stuck behind a backlog of routine ones.** The queue is ordered oldest-first,
and on real data the system produced about 545 new-token events a day against
roughly 10 risk and momentum events. Without separation, a liquidity drain
would arrive days late, after hundreds of "a new pool exists" messages - and
nothing would look broken while it happened.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mihq.config import Settings
from mihq.notify.dispatcher import NotificationDispatcher
from mihq.notify.telegram import SendResult


def settings(**overrides: Any) -> Settings:
    defaults = dict(
        telegram_bot_token="123456789:AAHtesttokenvaluethatislongenough12345",
        telegram_chat_id="1000",
        security_checks_enabled=False,
        alert_format="compact",
    )
    defaults.update(overrides)
    return Settings(**defaults)


class FakeRepo:
    """Models the queue honestly: rows, a notified flag, and the filters the
    real SQL applies."""

    def __init__(self, events: list[dict[str, Any]], failing: set[str] | None = None):
        self.events = events
        self.failing = failing or set()
        self.resolved_categories: list[tuple[str, ...]] = []
        self.notified: list[int] = []
        self.logged: list[dict[str, Any]] = []

    # -- the two queue statements ---------------------------------------

    def resolve_digest_only(self, categories):
        if "resolve" in self.failing:
            raise RuntimeError("simulated failure")
        self.resolved_categories.append(tuple(categories))
        count = 0
        for e in self.events:
            if not e["is_notified"] and e["category"] in categories:
                e["is_notified"] = True
                e["resolved_as_digest"] = True
                count += 1
        return count

    def pending_notification(self, limit=20, *, exclude_categories=()):
        if "pending" in self.failing:
            raise RuntimeError("simulated failure")
        rows = [e for e in self.events
                if not e["is_notified"]
                and not e.get("is_suppressed")
                and e["category"] not in exclude_categories]
        rows.sort(key=lambda e: e["triggered_at"])
        return [(e["id"],) for e in rows[:limit]]

    # -- what _dispatch_one needs ---------------------------------------

    def event_with_context(self, event_id):
        for e in self.events:
            if e["id"] == event_id:
                return {
                    "id": e["id"], "token_id": 1, "category": e["category"],
                    "event_type": "volume_spike", "severity": e.get("severity", "high"),
                    "chain": "base", "triggered_at": e["triggered_at"],
                    "payload": {}, "symbol": "TKN", "name": "Token",
                    "token_address": "0xabc", "summary": "something measured",
                    "model": "template-v1",
                }
        return None

    def mark_event_notified(self, event_id):
        self.notified.append(event_id)
        for e in self.events:
            if e["id"] == event_id:
                e["is_notified"] = True

    def log_notification(self, **kwargs):
        self.logged.append(kwargs)

    def active_risk_flags(self, token_id):
        return []

    def latest_security_report(self, token_id):
        return None


class FakeClient:
    def __init__(self, result: SendResult | None = None):
        self.result = result or SendResult(ok=True, status="sent",
                                           telegram_message_id=1, chat_id="1000")
        self.sent: list[str] = []

    def send_message(self, text, **_kw):
        self.sent.append(text)
        return self.result

    def stats(self):
        return {}


def event(id_: int, category: str, minutes_ago: int, **extra: Any) -> dict[str, Any]:
    base = {
        "id": id_, "category": category, "is_notified": False,
        "triggered_at": datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc).timestamp()
                        - minutes_ago * 60,
    }
    base.update(extra)
    return base


class TestDigestOnlySeparation:
    def test_the_important_alert_is_not_stuck_behind_the_feed(self):
        """The whole point. 300 older new-token events and one risk alert:
        with a batch size of 5 and oldest-first ordering, a naive queue sends
        five "new pool" messages and leaves the drain for tomorrow."""
        events = [event(i, "new_coin", minutes_ago=500 - i) for i in range(300)]
        events.append(event(999, "risk", minutes_ago=1, severity="critical"))

        repo = FakeRepo(events)
        client = FakeClient()
        result = NotificationDispatcher(settings(), repo, client).dispatch_pending(limit=5)

        assert result.sent == 1
        assert repo.notified == [999]
        assert result.digest_only == 300

    def test_an_event_arriving_mid_pass_still_cannot_enter_the_queue(self):
        """This is the only thing the category filter on the queue actually
        buys, and it is worth knowing that.

        The bulk resolve runs first and marks existing digest-only events
        notified, so in the ordinary path the queue would skip them even
        without the filter - a mutation removing it passes every other test
        here. What it protects is the gap BETWEEN the two statements: at ~545
        new-token events a day, one can be written after the resolve and
        before the queue read, and without the filter it would be sent.

        Named for what it checks, not for the bigger claim."""
        risk = event(999, "risk", minutes_ago=1)
        repo = FakeRepo([risk])

        original = repo.pending_notification

        def arrive_then_read(limit=20, *, exclude_categories=()):
            # A new pool is discovered in the instant between the two
            # statements, so the bulk resolve never saw it.
            repo.events.append(event(1000, "new_coin", minutes_ago=99))
            return original(limit, exclude_categories=exclude_categories)

        repo.pending_notification = arrive_then_read

        client = FakeClient()
        result = NotificationDispatcher(settings(), repo, client).dispatch_pending(limit=5)

        assert result.sent == 1
        assert repo.notified == [999]

    def test_digest_only_events_are_never_sent(self):
        repo = FakeRepo([event(1, "new_coin", 10), event(2, "new_coin", 5)])
        client = FakeClient()
        result = NotificationDispatcher(settings(), repo, client).dispatch_pending()
        assert client.sent == []
        assert result.sent == 0
        assert result.digest_only == 2

    def test_they_are_resolved_so_the_queue_does_not_grow_forever(self):
        """Left pending, they accumulate without limit - and would all
        become sendable at once if the setting were ever cleared."""
        events = [event(1, "new_coin", 10)]
        repo = FakeRepo(events)
        NotificationDispatcher(settings(), repo, FakeClient()).dispatch_pending()
        assert events[0]["is_notified"] is True

    def test_resolving_does_not_mark_them_suppressed(self):
        """The daily summary excludes suppressed events. These must still be
        counted there - being quiet about something is not the same as
        pretending it did not happen."""
        events = [event(1, "new_coin", 10)]
        repo = FakeRepo(events)
        NotificationDispatcher(settings(), repo, FakeClient()).dispatch_pending()
        assert events[0].get("is_suppressed") is None

    def test_the_configured_categories_are_the_ones_excluded(self):
        repo = FakeRepo([event(1, "momentum", 10)])
        NotificationDispatcher(
            settings(notify_digest_only_categories=("momentum", "new_coin")),
            repo, FakeClient(),
        ).dispatch_pending()
        assert repo.resolved_categories == [("momentum", "new_coin")]

    def test_an_empty_list_restores_the_old_behaviour(self):
        """Clearing the setting must send everything, with no special case."""
        repo = FakeRepo([event(1, "new_coin", 10)])
        client = FakeClient()
        result = NotificationDispatcher(
            settings(notify_digest_only_categories=()), repo, client,
        ).dispatch_pending()
        assert result.sent == 1
        assert result.digest_only == 0
        assert repo.resolved_categories == []

    def test_digest_only_is_counted_apart_from_dry_run_skips(self):
        """`skipped` means DRY_RUN and is temporary; `digest_only` is a
        deliberate policy. Merging them would hide a misconfigured token."""
        repo = FakeRepo([event(1, "new_coin", 10), event(2, "risk", 5)])
        client = FakeClient(SendResult(ok=True, status="skipped"))
        result = NotificationDispatcher(settings(), repo, client).dispatch_pending()
        assert result.digest_only == 1
        assert result.skipped == 1
        assert "held for the daily digest" in result.summary()
        assert "skipped (dry run)" in result.summary()


class TestQueueRobustness:
    def test_a_failed_resolve_does_not_stop_real_alerts(self):
        """Housekeeping failing must never silence a liquidity drain."""
        repo = FakeRepo([event(1, "risk", 5)], failing={"resolve"})
        client = FakeClient()
        result = NotificationDispatcher(settings(), repo, client).dispatch_pending()
        assert result.sent == 1
        assert any("resolve_digest_only" in e for e in result.errors)

    def test_a_failed_queue_read_is_reported_not_raised(self):
        repo = FakeRepo([event(1, "risk", 5)], failing={"pending"})
        result = NotificationDispatcher(
            settings(), repo, FakeClient()).dispatch_pending()
        assert result.sent == 0
        assert any("pending_notification" in e for e in result.errors)

    def test_one_bad_event_does_not_stop_the_queue(self):
        repo = FakeRepo([event(1, "risk", 10), event(2, "momentum", 5)])
        original = repo.event_with_context

        def explode(event_id):
            if event_id == 1:
                raise RuntimeError("bad row")
            return original(event_id)

        repo.event_with_context = explode
        result = NotificationDispatcher(
            settings(), repo, FakeClient()).dispatch_pending()
        assert result.sent == 1
        assert result.failed == 1

    def test_the_limit_is_respected(self):
        repo = FakeRepo([event(i, "risk", 100 - i) for i in range(20)])
        result = NotificationDispatcher(
            settings(), repo, FakeClient()).dispatch_pending(limit=3)
        assert result.sent == 3

    def test_oldest_first(self):
        repo = FakeRepo([event(1, "risk", 5), event(2, "risk", 60),
                         event(3, "risk", 30)])
        NotificationDispatcher(settings(), repo, FakeClient()).dispatch_pending(limit=3)
        assert repo.notified == [2, 3, 1]

    def test_a_failed_send_leaves_the_event_pending_for_a_retry(self):
        repo = FakeRepo([event(1, "risk", 5)])
        client = FakeClient(SendResult(ok=False, status="failed", error_code=500))
        result = NotificationDispatcher(settings(), repo, client).dispatch_pending()
        assert result.failed == 1
        assert repo.notified == []

    def test_a_dry_run_leaves_the_event_pending(self):
        """So the alert sends for real once DRY_RUN is turned off."""
        repo = FakeRepo([event(1, "risk", 5)])
        client = FakeClient(SendResult(ok=True, status="skipped"))
        NotificationDispatcher(settings(), repo, client).dispatch_pending()
        assert repo.notified == []


class TestQueueSql:
    def test_the_pending_query_filters_by_category(self):
        from mihq.db.repositories import SQL_PENDING_NOTIFICATION

        assert "category <> all(%s)" in SQL_PENDING_NOTIFICATION

    def test_resolving_sets_notified_but_not_suppressed(self):
        from mihq.db.repositories import SQL_RESOLVE_DIGEST_ONLY

        assert "set is_notified = true" in SQL_RESOLVE_DIGEST_ONLY
        assert "is_suppressed = true" not in SQL_RESOLVE_DIGEST_ONLY
