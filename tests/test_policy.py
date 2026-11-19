"""Tests for the snapshot write policy.

This is the code standing between you and a full database. The arithmetic in
the module docstring is not hypothetical: without it, 100 watchlist tokens
produce 144,000 rows a day.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from mihq.config import Settings
from mihq.models import MarketObservation, TokenRef, utcnow
from mihq.scanners.policy import SnapshotPolicy, estimate_daily_rows, percent_change

TOKEN = TokenRef(chain="base", address="0x" + "a" * 40, symbol="TEST")


def obs(minutes_ago: float = 0, **metrics) -> MarketObservation:
    return MarketObservation(
        token=TOKEN,
        source="dexscreener",
        captured_at=utcnow() - timedelta(minutes=minutes_ago),
        **metrics,
    )


@pytest.fixture
def policy() -> SnapshotPolicy:
    return SnapshotPolicy(
        price_change_pct=2.0, liquidity_change_pct=5.0,
        volume_change_pct=10.0, heartbeat_seconds=900,
    )


class TestPercentChange:
    @pytest.mark.parametrize("before,after,expected", [
        (100.0, 110.0, 10.0),
        (100.0, 90.0, -10.0),
        (100.0, 100.0, 0.0),
        (50.0, 100.0, 100.0),
        (0.001, 0.002, 100.0),
    ])
    def test_computes_change(self, before, after, expected):
        assert percent_change(before, after) == pytest.approx(expected)

    def test_negative_baseline_uses_absolute_magnitude(self):
        assert percent_change(-100.0, -50.0) == pytest.approx(50.0)

    @pytest.mark.parametrize("before,after", [(None, 5.0), (5.0, None), (None, None), (0.0, 5.0)])
    def test_returns_none_where_meaningless(self, before, after):
        assert percent_change(before, after) is None


class TestFirstAndEventWrites:
    def test_first_observation_always_writes_with_raw(self, policy):
        decision = policy.decide(None, obs(price_usd=1.0))
        assert decision.write
        assert decision.reason == "first_observation"
        assert decision.include_raw, "the first sighting is worth keeping in full"

    def test_alert_evidence_always_writes_with_raw(self, policy):
        previous = obs(10, price_usd=1.0)
        current = obs(price_usd=1.0)      # identical, would normally be skipped
        decision = policy.decide(previous, current, has_event=True)
        assert decision.write and decision.include_raw
        assert decision.reason == "alert_evidence"


class TestMaterialChange:
    def test_quiet_token_is_skipped(self, policy):
        previous = obs(1, price_usd=1.0, liquidity_usd=50000.0, volume_24h_usd=10000.0)
        current = obs(price_usd=1.005, liquidity_usd=50100.0, volume_24h_usd=10200.0)
        decision = policy.decide(previous, current)
        assert not decision.write
        assert decision.reason == "no_material_change"

    @pytest.mark.parametrize("field,before,after,label", [
        ("price_usd", 1.0, 1.05, "price"),            # +5% vs 2% threshold
        ("price_usd", 1.0, 0.90, "price"),            # -10%
        ("liquidity_usd", 50000.0, 56000.0, "liquidity"),   # +12% vs 5%
        ("liquidity_usd", 50000.0, 40000.0, "liquidity"),   # -20%, a drain
        ("volume_24h_usd", 10000.0, 12000.0, "volume_24h"), # +20% vs 10%
    ])
    def test_threshold_breach_writes(self, policy, field, before, after, label):
        previous = obs(1, **{field: before})
        current = obs(**{field: after})
        decision = policy.decide(previous, current)
        assert decision.write
        assert label in decision.reason

    def test_change_exactly_at_threshold_writes(self, policy):
        previous = obs(1, price_usd=1.0)
        current = obs(price_usd=1.02)     # exactly +2%
        assert policy.decide(previous, current).write

    def test_change_just_under_threshold_skips(self, policy):
        previous = obs(1, price_usd=1.0)
        current = obs(price_usd=1.019)
        assert not policy.decide(previous, current).write

    def test_metric_disappearing_is_material(self, policy):
        """A provider dropping liquidity may mean the pool was pulled."""
        previous = obs(1, liquidity_usd=50000.0)
        current = obs(liquidity_usd=None)
        decision = policy.decide(previous, current)
        assert decision.write
        assert "disappeared" in decision.reason

    def test_metric_appearing_is_material(self, policy):
        previous = obs(1, liquidity_usd=None)
        current = obs(liquidity_usd=50000.0)
        decision = policy.decide(previous, current)
        assert decision.write
        assert "appeared" in decision.reason

    def test_movement_away_from_zero_is_material(self, policy):
        """Percent change is undefined from zero, but first volume matters."""
        previous = obs(1, volume_24h_usd=0.0)
        current = obs(volume_24h_usd=5000.0)
        decision = policy.decide(previous, current)
        assert decision.write
        assert "from_zero" in decision.reason

    def test_both_values_absent_is_not_a_change(self, policy):
        previous = obs(1, price_usd=None)
        current = obs(price_usd=None)
        assert not policy.decide(previous, current).write


class TestHeartbeat:
    def test_writes_once_the_heartbeat_elapses(self, policy):
        previous = obs(20, price_usd=1.0)    # 20 min ago vs 15 min heartbeat
        current = obs(price_usd=1.0)
        decision = policy.decide(previous, current)
        assert decision.write
        assert decision.reason.startswith("heartbeat")

    def test_does_not_write_before_the_heartbeat(self, policy):
        previous = obs(10, price_usd=1.0)
        current = obs(price_usd=1.0)
        assert not policy.decide(previous, current).write

    def test_heartbeat_can_be_disabled(self):
        policy = SnapshotPolicy(heartbeat_seconds=0)
        previous = obs(600, price_usd=1.0)
        assert not policy.decide(previous, obs(price_usd=1.0)).write


class TestRawMode:
    def test_events_only_is_the_default_and_withholds_raw(self, policy):
        previous = obs(1, price_usd=1.0)
        decision = policy.decide(previous, obs(price_usd=1.10))
        assert decision.write
        assert not decision.include_raw, "raw payload is the bulk of row size"

    def test_always_keeps_raw(self):
        policy = SnapshotPolicy(raw_mode="always")
        decision = policy.decide(obs(1, price_usd=1.0), obs(price_usd=1.10))
        assert decision.include_raw

    def test_never_withholds_raw_even_for_events(self):
        policy = SnapshotPolicy(raw_mode="never")
        decision = policy.decide(obs(1, price_usd=1.0), obs(price_usd=1.0), has_event=True)
        assert decision.write
        assert not decision.include_raw

    def test_first_observation_keeps_raw_under_events_only(self):
        policy = SnapshotPolicy(raw_mode="events_only")
        assert policy.decide(None, obs(price_usd=1.0)).include_raw

    def test_never_overrides_even_the_first_observation(self):
        """An explicit storage decision is not ours to override."""
        policy = SnapshotPolicy(raw_mode="never")
        decision = policy.decide(None, obs(price_usd=1.0))
        assert decision.write
        assert not decision.include_raw

    def test_invalid_mode_is_rejected_loudly(self):
        with pytest.raises(ValueError, match="raw_mode"):
            SnapshotPolicy(raw_mode="sometimes")


class TestFromSettings:
    def test_reads_configuration(self):
        settings = Settings(
            snapshot_price_change_pct=7.5, snapshot_liquidity_change_pct=1.0,
            snapshot_volume_change_pct=25.0, snapshot_heartbeat_seconds=600,
            snapshot_raw_mode="always",
        )
        policy = SnapshotPolicy.from_settings(settings)
        assert policy.price_change_pct == 7.5
        assert policy.heartbeat.total_seconds() == 600
        assert policy.raw_mode == "always"

    def test_negative_thresholds_are_treated_as_magnitudes(self):
        policy = SnapshotPolicy(price_change_pct=-5.0)
        assert policy.price_change_pct == 5.0


class TestStorageImpact:
    def test_naive_approach_would_exhaust_the_free_tier(self):
        """The number that motivates this entire module."""
        rows = estimate_daily_rows(100, interval_seconds=60, write_fraction=1.0)
        assert rows == 144_000

    def test_policy_cuts_it_by_roughly_an_order_of_magnitude(self):
        naive = estimate_daily_rows(100, interval_seconds=60, write_fraction=1.0)
        filtered = estimate_daily_rows(100, interval_seconds=60, write_fraction=0.15)
        assert filtered < naive / 5

    def test_measured_skip_rate_on_a_realistic_quiet_market(self, policy):
        """Simulate 500 minute-by-minute checks of a token drifting slightly.

        Drift is +/-0.5% per minute, well under the 2% threshold, with the
        15-minute heartbeat still guaranteeing coverage.
        """
        import random
        random.seed(11)

        price = 1.0
        last_written = obs(0, price_usd=price, liquidity_usd=50000.0,
                           volume_24h_usd=10000.0)
        writes = 0
        for minute in range(1, 501):
            price *= 1 + random.uniform(-0.005, 0.005)
            current = MarketObservation(
                token=TOKEN, source="dexscreener",
                captured_at=last_written.captured_at + timedelta(minutes=1),
                price_usd=price, liquidity_usd=50000.0, volume_24h_usd=10000.0,
            )
            # captured_at must advance from the real clock for the heartbeat
            current = MarketObservation(
                token=TOKEN, source="dexscreener",
                captured_at=utcnow() + timedelta(minutes=minute),
                price_usd=price, liquidity_usd=50000.0, volume_24h_usd=10000.0,
            )
            if policy.decide(last_written, current).write:
                writes += 1
                last_written = current

        write_rate = writes / 500
        # With a 15-minute heartbeat the floor is ~1/15 = 6.7% of passes.
        assert 0.05 <= write_rate <= 0.35, f"write rate {write_rate:.1%} outside expected band"
