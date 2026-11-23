"""Tests for noise control: the new-token floor and watchlist retirement.

Both of these exist because of a measured problem, not a hypothetical one.
On 2026-11-20 the live system produced 66 new-token alerts out of 76 total,
and the watchlist had grown to 283 entries in two days with nothing ever
removing one.

The tests that matter most here are the negative ones - what retirement must
NOT do. Retiring the wrong token is silent: no error, no alert, the scanner
simply stops watching something and nobody finds out.
"""

from __future__ import annotations

from typing import Any

import pytest

from mihq.config import Settings
from mihq.events.detectors import NewTokenDetector, default_detectors
from mihq.scanners.retire import RetirementResult, retire_quiet_entries


def settings(**overrides: Any) -> Settings:
    defaults = dict(security_checks_enabled=False)
    defaults.update(overrides)
    return Settings(**defaults)


class FakeRepo:
    def __init__(self, low: int = 0, uneventful: int = 0, unknown: int = 0,
                 failing: set[str] | None = None) -> None:
        self._low = low
        self._uneventful = uneventful
        self._unknown = unknown
        self.failing = failing or set()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def retire_low_liquidity(self, **kwargs):
        self.calls.append(("low", kwargs))
        if "low" in self.failing:
            raise RuntimeError("simulated failure")
        return [(i, i) for i in range(self._low)]

    def retire_unknown_liquidity(self, **kwargs):
        self.calls.append(("unknown", kwargs))
        if "unknown" in self.failing:
            raise RuntimeError("simulated failure")
        return [(i, i) for i in range(self._unknown)]

    def retire_uneventful(self, **kwargs):
        self.calls.append(("uneventful", kwargs))
        if "uneventful" in self.failing:
            raise RuntimeError("simulated failure")
        return [(i, i) for i in range(self._uneventful)]


# ---------------------------------------------------------------------------
# The new-token floor
# ---------------------------------------------------------------------------

class TestNewTokenFloor:
    def test_the_detector_uses_its_own_setting(self):
        """Regression guard. This floor used to be derived as
        `event_min_liquidity_usd / 5`, and nothing tested the wiring - so
        reverting it would have gone unnoticed."""
        s = settings(event_new_token_min_liquidity_usd=25_000.0)
        detector = next(d for d in default_detectors(s)
                        if isinstance(d, NewTokenDetector))
        assert detector.min_liquidity == 25_000.0

    def test_it_is_independent_of_the_general_floor(self):
        """The two numbers answer different questions. Tying them together
        meant quieting the change detectors also hid new pools, and catching
        smaller moves flooded the phone."""
        s = settings(event_min_liquidity_usd=50_000.0,
                     event_new_token_min_liquidity_usd=25_000.0)
        detector = next(d for d in default_detectors(s)
                        if isinstance(d, NewTokenDetector))
        assert detector.min_liquidity == 25_000.0

        s2 = settings(event_min_liquidity_usd=1_000.0,
                      event_new_token_min_liquidity_usd=25_000.0)
        detector2 = next(d for d in default_detectors(s2)
                         if isinstance(d, NewTokenDetector))
        assert detector2.min_liquidity == 25_000.0

    def test_the_default_is_high_enough_to_exclude_dust(self):
        """$1,000 pools were two-thirds of the alert volume."""
        assert settings().event_new_token_min_liquidity_usd >= 10_000.0

    def test_a_non_positive_floor_is_rejected_with_an_explanation(self):
        from mihq.config import validate_settings

        problems = validate_settings(settings(event_new_token_min_liquidity_usd=0.0))
        assert any("EVENT_NEW_TOKEN_MIN_LIQUIDITY_USD" in p for p in problems)


# ---------------------------------------------------------------------------
# Retirement
# ---------------------------------------------------------------------------

class TestRetirement:
    def test_both_rules_run_and_are_counted_separately(self):
        repo = FakeRepo(low=4, uneventful=3, unknown=9)
        result = retire_quiet_entries(settings(), repo)
        assert result.low_liquidity == 4
        assert result.unknown_liquidity == 9
        assert result.uneventful == 3
        assert result.total == 16
        assert [name for name, _ in repo.calls] == ["low", "unknown", "uneventful"]

    def test_the_configured_thresholds_are_passed_through(self):
        repo = FakeRepo()
        retire_quiet_entries(
            settings(watchlist_retire_below_liquidity_usd=3_500.0,
                     watchlist_retire_grace_hours=8,
                     watchlist_retire_uneventful_hours=96),
            repo,
        )
        low = dict(repo.calls[0][1])
        assert low["below_usd"] == 3_500.0
        assert low["grace_hours"] == 8
        assert dict(repo.calls[2][1])["older_than_hours"] == 96

    def test_disabling_it_touches_nothing(self):
        """An operator who turns this off must get no retirements at all,
        not merely fewer."""
        repo = FakeRepo(low=9, uneventful=9)
        result = retire_quiet_entries(settings(watchlist_retire_enabled=False), repo)
        assert repo.calls == []
        assert result.total == 0

    def test_one_failing_rule_does_not_stop_the_other(self):
        repo = FakeRepo(low=0, uneventful=5, failing={"low"})
        result = retire_quiet_entries(settings(), repo)
        assert result.uneventful == 5
        assert any("low-liquidity" in e for e in result.errors)

    def test_a_failure_is_reported_rather_than_raised(self):
        """Housekeeping must never take down a scanner pass."""
        repo = FakeRepo(failing={"low", "unknown", "uneventful"})
        result = retire_quiet_entries(settings(), repo)   # must not raise
        assert result.total == 0
        assert len(result.errors) == 3

    def test_the_note_explains_why_a_row_was_retired(self):
        """Whoever finds a retired row months later needs to know what
        decided it, without reading this code."""
        repo = FakeRepo()
        retire_quiet_entries(settings(watchlist_retire_below_liquidity_usd=2_000.0,
                                      watchlist_retire_uneventful_hours=72), repo)
        assert "$2,000" in repo.calls[0][1]["note"]
        assert "72h" in repo.calls[2][1]["note"]

    def test_a_grace_period_under_an_hour_is_rejected(self):
        """A brand-new pool is still filling. Retiring it on arrival would
        drop exactly the tokens the scanner exists to catch."""
        from mihq.config import validate_settings

        problems = validate_settings(settings(watchlist_retire_grace_hours=0))
        assert any("WATCHLIST_RETIRE_GRACE_HOURS" in p for p in problems)

    def test_summary_names_both_counts(self):
        assert "2 drained" in RetirementResult(low_liquidity=2, uneventful=1).summary()
        assert "1 uneventful" in RetirementResult(low_liquidity=2, uneventful=1).summary()


class TestRetirementSqlShape:
    """The SQL itself is PREPARE-checked and behaviour-checked against a real
    server; these guard the properties a reader might innocently 'simplify'
    away."""

    def test_unknown_liquidity_is_not_treated_as_low(self):
        """`NULL < 2000` is NULL, not true, so a token whose liquidity
        vanished is NOT retired. That is the single most important line in
        this feature: disappearing liquidity may be the pool being pulled,
        and retiring then would drop the token at the moment it mattered.

        Rewriting the subquery with `coalesce(..., 0)` would look like a
        tidy-up and would silently invert this.

        Proven for real as well: seeded against PostgreSQL 16, a token whose
        liquidity went from $90,000 to NULL survived both rules while a
        genuinely drained one was retired."""
        from mihq.db.repositories import SQL_RETIRE_LOW_LIQUIDITY_WATCHLIST

        sql = SQL_RETIRE_LOW_LIQUIDITY_WATCHLIST.lower()
        # The two spellings of the mistake. The note's own coalesce is on
        # w.notes and is unrelated.
        assert "coalesce(s.liquidity_usd" not in sql
        assert "coalesce(liquidity_usd" not in sql
        assert "is not null" not in sql     # would also admit NULL rows

    def test_the_excluded_event_type_is_the_one_the_detector_writes(self):
        """This test replaces one that asserted the literal string
        `'new_token'` was in the SQL. It passed, and the feature was broken:
        `new_token` is the detector's CLASS name, while the event it writes
        is `first_seen`. The rule matched nothing, retired nothing, and
        raised no error - and the test agreed with the bug because both were
        written from the same wrong assumption.

        The fix is structural: the value is no longer written in two places.
        It comes from the detector, and this test fires it to see what it
        actually produces rather than trusting any constant."""
        from datetime import datetime, timezone

        from mihq.events.detectors import FIRST_SEEN_EVENT_TYPE, NewTokenDetector
        from mihq.events.detectors import DetectionContext
        from mihq.models import MarketObservation, TokenRef

        observation = MarketObservation(
            token=TokenRef(chain="base", address="0xabc", symbol="X"),
            source="geckoterminal",
            captured_at=datetime(2026, 11, 20, tzinfo=timezone.utc),
            liquidity_usd=50_000.0,
        )
        event = NewTokenDetector(min_liquidity=1_000.0).detect(
            None, observation, DetectionContext(is_new_token=True)
        )
        assert event is not None
        assert event.event_type == FIRST_SEEN_EVENT_TYPE

    def test_the_excluded_type_is_passed_in_not_hardcoded(self):
        """A literal in the SQL is what allowed the two to drift."""
        from mihq.db.repositories import SQL_RETIRE_UNEVENTFUL_WATCHLIST

        assert "event_type <> %s" in SQL_RETIRE_UNEVENTFUL_WATCHLIST
        assert "'new_token'" not in SQL_RETIRE_UNEVENTFUL_WATCHLIST
        assert "'first_seen'" not in SQL_RETIRE_UNEVENTFUL_WATCHLIST

    def test_retirement_passes_the_arrival_type_through(self):
        from mihq.events.detectors import FIRST_SEEN_EVENT_TYPE

        repo = FakeRepo()
        retire_quiet_entries(settings(), repo)
        assert repo.calls[2][1]["arrival_event_type"] == FIRST_SEEN_EVENT_TYPE

    def test_the_liquidity_sample_does_not_join_through_snapshot_id(self):
        """`snapshot_id` is null whenever the write policy skipped the
        snapshot, so joining through it silently drops those rows. The first
        version did, and returned nothing against a database holding 66
        matching events."""
        from mihq.db.repositories import SQL_NEW_TOKEN_LIQUIDITY_SAMPLE

        assert "snapshot_id" not in SQL_NEW_TOKEN_LIQUIDITY_SAMPLE
        assert "payload->>'liquidity_usd'" in SQL_NEW_TOKEN_LIQUIDITY_SAMPLE

    def test_retirement_deactivates_rather_than_deletes(self):
        """History and the research record must survive, and the partial
        unique index must allow the token to be re-added later."""
        from mihq.db.repositories import (
            SQL_RETIRE_LOW_LIQUIDITY_WATCHLIST,
            SQL_RETIRE_UNEVENTFUL_WATCHLIST,
        )

        for sql in (SQL_RETIRE_LOW_LIQUIDITY_WATCHLIST,
                    SQL_RETIRE_UNEVENTFUL_WATCHLIST):
            assert "delete" not in sql.lower()
            assert "is_active = false" in sql
            assert "removed_at = now()" in sql

    def test_both_rules_respect_a_minimum_age(self):
        from mihq.db.repositories import (
            SQL_RETIRE_LOW_LIQUIDITY_WATCHLIST,
            SQL_RETIRE_UNEVENTFUL_WATCHLIST,
        )

        for sql in (SQL_RETIRE_LOW_LIQUIDITY_WATCHLIST,
                    SQL_RETIRE_UNEVENTFUL_WATCHLIST):
            assert "added_at < now() - make_interval" in sql


# ---------------------------------------------------------------------------
# What gets watched in the first place
# ---------------------------------------------------------------------------

class TestWatchlistInflow:
    """The 2,367-entry problem was never really about retirement.

    Watchlisting was gated only on `was_inserted` - have we seen this token
    before - so every discovered pool joined the list at any size. Retirement
    can only ever churn against an unbounded inflow; the floor is what makes
    the list converge.
    """

    def test_a_dust_pool_is_not_watched(self):
        from tests.test_scanners import FakeGecko, FakeRepo, observation
        from mihq.scanners.discovery import DiscoveryScanner

        repo = FakeRepo()
        gecko = FakeGecko({"base": [observation(price_usd=1.0, liquidity_usd=800.0)],
                           "solana": []})
        result = DiscoveryScanner(
            settings(enabled_chains=("solana", "base"),
                     watchlist_min_liquidity_usd=10_000.0),
            repo, gecko,
        ).run_once()

        assert result.tokens_seen == 1      # still observed and stored
        assert result.watchlist_added == 0  # just not watched
        assert repo.watchlist == {}

    def test_a_funded_pool_is_watched(self):
        from tests.test_scanners import FakeGecko, FakeRepo, observation
        from mihq.scanners.discovery import DiscoveryScanner

        repo = FakeRepo()
        gecko = FakeGecko({"base": [observation(price_usd=1.0, liquidity_usd=60_000.0)],
                           "solana": []})
        result = DiscoveryScanner(
            settings(enabled_chains=("solana", "base"),
                     watchlist_min_liquidity_usd=10_000.0),
            repo, gecko,
        ).run_once()
        assert result.watchlist_added == 1

    def test_unknown_liquidity_is_not_watched(self):
        """A token we cannot size is not one we can justify spending every
        pass on. If it matters, the next pass with a real figure adds it -
        which is only possible because watching no longer depends on the
        token being new."""
        from tests.test_scanners import FakeGecko, FakeRepo, observation
        from mihq.models import MarketObservation, TokenRef
        from mihq.scanners.discovery import DiscoveryScanner
        from datetime import datetime, timezone

        obs = MarketObservation(
            token=TokenRef(chain="base", address="0x" + "a" * 40, symbol="T"),
            source="geckoterminal",
            captured_at=datetime(2026, 11, 22, tzinfo=timezone.utc),
            price_usd=1.0, liquidity_usd=None,
        )
        repo = FakeRepo()
        result = DiscoveryScanner(
            settings(enabled_chains=("solana", "base")),
            repo, FakeGecko({"base": [obs], "solana": []}),
        ).run_once()
        assert result.watchlist_added == 0

    def test_a_token_that_grows_past_the_floor_is_picked_up_later(self):
        """`was_inserted` fires exactly once, so gating on it meant a pool
        that launched small could never be watched afterwards however big it
        got. This is the bug the floor would have introduced if watching had
        stayed tied to novelty."""
        from tests.test_scanners import FakeGecko, FakeRepo, observation
        from mihq.scanners.discovery import DiscoveryScanner

        repo = FakeRepo()
        addr = "0x" + "b" * 40
        small = observation("base", addr, price_usd=1.0, liquidity_usd=3_000.0)
        grown = observation("base", addr, price_usd=1.4, liquidity_usd=40_000.0)

        s = settings(enabled_chains=("solana", "base"),
                     watchlist_min_liquidity_usd=10_000.0)

        first = DiscoveryScanner(s, repo, FakeGecko({"base": [small], "solana": []})).run_once()
        assert first.watchlist_added == 0

        second = DiscoveryScanner(s, repo, FakeGecko({"base": [grown], "solana": []})).run_once()
        assert second.new_tokens == 0          # not a new token any more
        assert second.watchlist_added == 1     # but now worth watching


class TestReAddCooldown:
    def test_the_insert_is_guarded_against_immediate_re_adding(self):
        """Without this, a token retired for being uneventful while still
        well funded is re-added on the next discovery pass and bounces
        between states forever.

        `on conflict do nothing` cannot prevent it: the retired row is
        inactive, so the partial unique index sees no conflict at all."""
        from mihq.db.repositories import SQL_ADD_TO_WATCHLIST

        assert "removed_at is not null" in SQL_ADD_TO_WATCHLIST
        assert "make_interval(hours =>" in SQL_ADD_TO_WATCHLIST

    def test_the_configured_cooldown_reaches_the_repository(self):
        from tests.test_scanners import FakeGecko, FakeRepo, observation
        from mihq.scanners.discovery import DiscoveryScanner

        repo = FakeRepo()
        captured: dict = {}
        original = repo.add_to_watchlist

        def spy(token_id, **kwargs):
            captured.update(kwargs)
            return original(token_id, **kwargs)

        repo.add_to_watchlist = spy
        DiscoveryScanner(
            settings(enabled_chains=("solana", "base"),
                     watchlist_readd_cooldown_hours=240),
            repo, FakeGecko({"base": [observation(price_usd=1.0)], "solana": []}),
        ).run_once()
        assert captured["readd_cooldown_hours"] == 240


class TestFloorsAreRelated:
    """The watchlist grew 1,717 -> 1,859 overnight because the entry floor
    ($10,000) and the retirement floor ($2,000) were set in different
    sessions and never related to each other. Everything between them was
    dead money that nothing would add today and nothing ever removed.
    """

    def test_the_retirement_floor_sits_below_the_entry_floor(self):
        s = settings()
        assert s.watchlist_retire_below_liquidity_usd < s.watchlist_min_liquidity_usd

    def test_the_gap_is_not_so_wide_it_leaves_a_dead_zone(self):
        """Hysteresis, not a chasm. Anything much below half the entry floor
        starts accumulating entries that will never be removed."""
        s = settings()
        ratio = (s.watchlist_retire_below_liquidity_usd
                 / s.watchlist_min_liquidity_usd)
        assert 0.4 <= ratio < 1.0

    def test_a_retirement_floor_at_or_above_the_entry_floor_is_rejected(self):
        """A token would qualify for retirement the instant it was added,
        and with the re-add cooldown it would then be locked out for a week."""
        from mihq.config import validate_settings

        problems = validate_settings(settings(
            watchlist_min_liquidity_usd=10_000.0,
            watchlist_retire_below_liquidity_usd=10_000.0))
        assert any("RETIRE_BELOW_LIQUIDITY_USD" in p for p in problems)

    def test_a_sensible_pair_is_accepted(self):
        from mihq.config import validate_settings

        problems = validate_settings(settings(
            watchlist_min_liquidity_usd=25_000.0,
            watchlist_retire_below_liquidity_usd=12_000.0))
        assert not any("RETIRE_BELOW_LIQUIDITY_USD" in p for p in problems)


class TestUnknownLiquidityRule:
    """Rule 1 never retires on unknown liquidity, because a pool being pulled
    is the moment you most want to be watching. That was right for the first
    few hours and wrong as a permanent exemption.

    The measurement that proved it: 990 of 1,859 active entries - 53% - had
    unknown liquidity. Not a rare emergency; the ordinary end state of a dead
    token. No adjustment to the other thresholds could have fixed that,
    because none of them could ever touch those rows.
    """

    def test_it_runs_and_is_counted_separately(self):
        repo = FakeRepo(unknown=990)
        result = retire_quiet_entries(settings(), repo)
        assert result.unknown_liquidity == 990
        assert "990 unreadable" in result.summary()

    def test_recently_unknown_is_still_protected(self):
        """The signal Rule 1 exists for survives: the SQL only retires once
        liquidity has been unreadable for the configured window, so a pool
        that just vanished from the feed is untouched."""
        from mihq.db.repositories import SQL_RETIRE_UNKNOWN_LIQUIDITY_WATCHLIST

        sql = SQL_RETIRE_UNKNOWN_LIQUIDITY_WATCHLIST
        assert "max(s.captured_at)" in sql
        assert "s.liquidity_usd is not null" in sql
        assert sql.count("make_interval(hours =>") == 2   # grace AND window

    def test_a_token_that_never_had_a_figure_falls_back_to_when_it_was_added(self):
        """Without the COALESCE the subquery returns NULL, the comparison is
        NULL, and the row is never retired - the same NULL trap that made
        Rule 1 too broad in the first place."""
        from mihq.db.repositories import SQL_RETIRE_UNKNOWN_LIQUIDITY_WATCHLIST

        assert "coalesce(" in SQL_RETIRE_UNKNOWN_LIQUIDITY_WATCHLIST
        assert "w.added_at" in SQL_RETIRE_UNKNOWN_LIQUIDITY_WATCHLIST

    def test_the_configured_window_is_passed_through(self):
        repo = FakeRepo()
        retire_quiet_entries(
            settings(watchlist_retire_unknown_after_hours=36), repo)
        assert dict(repo.calls[1][1])["unknown_for_hours"] == 36

    def test_the_note_says_why(self):
        repo = FakeRepo()
        retire_quiet_entries(
            settings(watchlist_retire_unknown_after_hours=24), repo)
        assert "unreadable for 24h" in repo.calls[1][1]["note"]

    def test_it_deactivates_rather_than_deletes(self):
        from mihq.db.repositories import SQL_RETIRE_UNKNOWN_LIQUIDITY_WATCHLIST

        sql = SQL_RETIRE_UNKNOWN_LIQUIDITY_WATCHLIST
        assert "delete" not in sql.lower()
        assert "is_active = false" in sql
        assert "removed_at = now()" in sql
