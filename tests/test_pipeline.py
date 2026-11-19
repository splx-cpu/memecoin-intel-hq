"""Tests for the per-token pipeline shared by both scanners.

The ordering assertion here is the important one: detection must run BEFORE
the storage decision, because whether an event fired determines whether the
observation's raw payload is kept as evidence.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from conftest import FakeClock  # noqa: F401 - fixture module
from mihq.config import Settings
from mihq.events.detectors import default_detectors
from mihq.events.pipeline import process_observation
from mihq.models import MarketObservation, TokenRef
from mihq.scanners.policy import SnapshotPolicy
from test_scanners import FakeRepo

ADDR = "0x" + "a" * 40

# A FIXED base time, deliberately at the top of an hour.
#
# These tests were originally anchored on utcnow(), which made them flaky:
# the churn detector buckets by hour, so running the suite near :50 meant 12
# simulated minutes straddled two buckets and produced 2 alerts instead of 1.
# The test passed or failed depending on what time of day it ran, which is
# the worst kind of test - it is green until it is mysteriously not.
BASE = datetime(2026, 11, 18, 10, 0, 0, tzinfo=timezone.utc)


def obs(minutes_offset=0, **metrics):
    return MarketObservation(
        token=TokenRef(chain="base", address=ADDR, symbol="TEST"),
        source="dexscreener",
        captured_at=BASE + timedelta(minutes=minutes_offset),
        pool_address="0x" + "p" * 40,
        **metrics,
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(security_checks_enabled=False)


@pytest.fixture
def policy(settings) -> SnapshotPolicy:
    return SnapshotPolicy.from_settings(settings)


def run(repo, settings, policy, observation, *, is_new=False):
    return process_observation(
        repo=repo, settings=settings, policy=policy,
        detectors=default_detectors(settings),
        token_id=1, observation=observation, is_new_token=is_new,
    )


class TestPipelineBasics:
    def test_first_observation_is_stored_with_raw(self, settings, policy):
        repo = FakeRepo()
        outcome = run(repo, settings, policy,
                      obs(liquidity_usd=50_000.0, volume_24h_usd=20_000.0))
        assert outcome.snapshot_written
        assert repo.raw_flags == [True]

    def test_new_token_creates_a_discovery_event(self, settings, policy):
        repo = FakeRepo()
        outcome = run(repo, settings, policy,
                      obs(liquidity_usd=50_000.0, volume_24h_usd=20_000.0),
                      is_new=True)
        assert outcome.events_created == 1
        assert any("first_seen" in k for k in repo.events)

    def test_quiet_token_writes_nothing_and_alerts_nothing(self, settings, policy):
        repo = FakeRepo()
        first = obs(liquidity_usd=50_000.0, volume_24h_usd=20_000.0, price_usd=1.0)
        run(repo, settings, policy, first)
        second = run(repo, settings, policy,
                     obs(1, liquidity_usd=50_100.0, volume_24h_usd=20_200.0,
                         price_usd=1.004))
        assert not second.snapshot_written
        assert second.events_created == 0


class TestEvidencePreservation:
    def test_an_event_forces_the_raw_payload_to_be_kept(self, settings, policy):
        """Detection must run before the storage decision, or evidence is lost."""
        repo = FakeRepo()
        run(repo, settings, policy,
            obs(liquidity_usd=100_000.0, volume_24h_usd=20_000.0, price_usd=1.0))
        repo.raw_flags.clear()

        # A drain: would normally store without raw under events_only.
        outcome = run(repo, settings, policy,
                      obs(1, liquidity_usd=20_000.0, volume_24h_usd=20_000.0,
                          price_usd=1.0))
        assert outcome.events_created >= 1
        assert repo.raw_flags == [True], "evidence must keep its raw payload"

    def test_events_link_to_the_snapshot_that_triggered_them(self, settings, policy):
        repo = FakeRepo()
        run(repo, settings, policy,
            obs(liquidity_usd=100_000.0, volume_24h_usd=20_000.0, price_usd=1.0))
        run(repo, settings, policy,
            obs(1, liquidity_usd=20_000.0, volume_24h_usd=20_000.0, price_usd=1.0))
        stored = [e for e in repo.events.values() if e["event_type"] == "liquidity_drain"]
        assert stored and stored[0]["snapshot_id"] is not None


class TestDeduplication:
    """There are TWO independent defences against duplicate alerts.

    1. THE MOVING BASELINE. A change-detector compares against the latest
       STORED snapshot. Once a spike is stored, the next pass compares
       spike-to-spike and sees no change, so it stays quiet by itself.

    2. THE EVENT KEY. State-detectors (churn) fire on a condition being true
       rather than on a change, so the moving baseline does not quiet them.
       The UNIQUE constraint on event_key is what stops those.

    Both are tested below. Relying on only the first would be fragile: it
    fails the moment a snapshot write fails and the baseline does not advance.
    """

    def test_moving_baseline_quiets_a_change_detector(self, settings, policy):
        repo = FakeRepo()
        run(repo, settings, policy,
            obs(liquidity_usd=100_000.0, volume_24h_usd=20_000.0, price_usd=1.0))

        created = 0
        for minute in range(1, 13):
            outcome = run(repo, settings, policy,
                          obs(minute, liquidity_usd=100_000.0,
                              volume_24h_usd=200_000.0, price_usd=1.0))
            created += outcome.events_created

        assert created == 1, (
            f"expected exactly one volume_spike alert across 12 passes, got {created}"
        )

    def test_event_key_stops_a_state_detector_repeating(self, settings, policy):
        """Churn fires on a STATE, so it fires every pass. Only the key stops it.

        Values are identical on every pass, so the moving baseline offers no
        protection here - the condition stays true and the detector keeps
        firing. The event_key is the only thing preventing 12 alerts.
        """
        repo = FakeRepo()

        created = suppressed = 0
        for minute in range(12):
            outcome = run(repo, settings, policy,
                          obs(minute, liquidity_usd=50_000.0,
                              volume_24h_usd=900_000.0, price_usd=1.0))
            created += outcome.events_created
            suppressed += outcome.events_suppressed

        churn_attempts = repo.event_attempts
        churn_stored = [e for e in repo.events.values() if e["event_type"] == "high_churn"]

        assert churn_attempts >= 10, "the churn detector should fire on every pass"
        assert len(churn_stored) == 1, (
            f"only one churn alert should be stored, got {len(churn_stored)}"
        )
        assert suppressed >= 10, "the repeats must be recognised as duplicates"

    def test_a_repeat_event_does_not_force_a_snapshot_write(self, settings, policy):
        """The flaw this ordering exists to prevent.

        A persistently-true condition would otherwise set has_event on every
        pass, forcing a snapshot write every minute and defeating the storage
        policy entirely.
        """
        repo = FakeRepo()
        for minute in range(12):
            run(repo, settings, policy,
                obs(minute, liquidity_usd=50_000.0, volume_24h_usd=900_000.0,
                    price_usd=1.0))

        writes = len(repo.raw_flags)
        assert writes <= 2, (
            f"a repeating condition should not force a write per pass; got {writes}"
        )

    def test_a_later_time_bucket_allows_a_fresh_alert(self, settings, policy):
        """A genuinely new occurrence later must not be swallowed forever."""
        repo = FakeRepo()
        for minute in (0, 5, 10):
            run(repo, settings, policy,
                obs(minute, liquidity_usd=50_000.0, volume_24h_usd=900_000.0,
                    price_usd=1.0))
        first_batch = len([e for e in repo.events.values()
                           if e["event_type"] == "high_churn"])

        # Churn uses a 60-minute bucket, so 90 minutes later is a new one.
        run(repo, settings, policy,
            obs(90, liquidity_usd=50_000.0, volume_24h_usd=900_000.0, price_usd=1.0))
        second_batch = len([e for e in repo.events.values()
                            if e["event_type"] == "high_churn"])

        assert first_batch == 1
        assert second_batch == 2, "a new bucket must allow a new alert"


class TestRiskFlagLifecycle:
    def test_flags_are_raised_for_a_thin_pool(self, settings, policy):
        repo = FakeRepo()
        outcome = run(repo, settings, policy,
                      obs(liquidity_usd=800.0, volume_24h_usd=2_000.0))
        assert outcome.risk_flags_raised >= 1
        assert any(ft == "low_liquidity" for (_, ft) in repo.risk_flags)

    def test_a_flag_is_resolved_once_the_condition_clears(self, settings, policy):
        repo = FakeRepo()
        run(repo, settings, policy, obs(liquidity_usd=800.0, volume_24h_usd=2_000.0))
        assert any(ft == "low_liquidity" for (_, ft) in repo.risk_flags)

        run(repo, settings, policy,
            obs(20, liquidity_usd=500_000.0, volume_24h_usd=200_000.0,
                price_usd=1.0, price_change_24h=2.0, fdv_usd=1e6, buys_24h=100))
        assert not any(ft == "low_liquidity" for (_, ft) in repo.risk_flags)
        assert ("low_liquidity" in [ft for (_, ft) in repo.resolved_flags])

    def test_reraising_the_same_flag_is_not_counted_as_new(self, settings, policy):
        repo = FakeRepo()
        first = run(repo, settings, policy,
                    obs(liquidity_usd=800.0, volume_24h_usd=2_000.0))
        second = run(repo, settings, policy,
                     obs(1, liquidity_usd=810.0, volume_24h_usd=2_000.0))
        assert first.risk_flags_raised >= 1
        assert second.risk_flags_raised == 0

    def test_risk_assessment_can_be_skipped(self, settings, policy):
        repo = FakeRepo()
        process_observation(
            repo=repo, settings=settings, policy=policy,
            detectors=default_detectors(settings), token_id=1,
            observation=obs(liquidity_usd=800.0), assess_risk=False,
        )
        assert repo.risk_flags == {}


class TestResilience:
    def test_a_failing_risk_write_does_not_lose_the_snapshot(self, settings, policy):
        repo = FakeRepo()
        repo.fail_on.add("upsert_risk_flag")
        outcome = run(repo, settings, policy,
                      obs(liquidity_usd=50_000.0, volume_24h_usd=20_000.0))
        assert outcome.snapshot_written
        assert outcome.risk_flags_raised == 0
