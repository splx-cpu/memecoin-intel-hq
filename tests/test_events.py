"""Tests for event keys, detectors and risk heuristics.

The dedupe tests matter most. Everything else here degrades the product;
duplicate alerts destroy it, because a feed that cries wolf sixty times an
hour stops being read at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mihq.config import Settings
from mihq.db.repositories import StoredSnapshot
from mihq.events import risk
from mihq.events.detectors import (
    ChurnRatioDetector,
    DetectionContext,
    LiquidityDrainDetector,
    NewTokenDetector,
    PriceMoveDetector,
    VolumeSpikeDetector,
    default_detectors,
    run_detectors,
)
from mihq.events.keys import build_event_key, time_bucket
from mihq.models import MarketObservation, TokenRef, utcnow

T0 = datetime(2026, 11, 15, 4, 37, 12, tzinfo=timezone.utc)
ADDR = "0x" + "a" * 40


def snap(captured_at=T0, price=None, liquidity=None, volume=None) -> StoredSnapshot:
    return StoredSnapshot(captured_at=captured_at, price_usd=price,
                          liquidity_usd=liquidity, volume_24h_usd=volume)


def obs(captured_at=T0, chain="base", address=ADDR, symbol="TEST", **metrics):
    return MarketObservation(
        token=TokenRef(chain=chain, address=address, symbol=symbol),
        source="dexscreener", captured_at=captured_at,
        pool_address="0x" + "p" * 40, **metrics,
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(security_checks_enabled=False)


# ===========================================================================
# Event keys - the duplicate-alert guard
# ===========================================================================

class TestTimeBucket:
    @pytest.mark.parametrize("minutes,expected", [
        (1, "2026-11-15T04:37"), (5, "2026-11-15T04:35"),
        (15, "2026-11-15T04:30"), (30, "2026-11-15T04:30"),
        (60, "2026-11-15T04:00"),
    ])
    def test_rounds_down(self, minutes, expected):
        assert time_bucket(T0, minutes) == expected

    def test_two_moments_in_one_bucket_match(self):
        """This is exactly why a sustained spike alerts once."""
        a = datetime(2026, 11, 15, 4, 31, tzinfo=timezone.utc)
        b = datetime(2026, 11, 15, 4, 44, 59, tzinfo=timezone.utc)
        assert time_bucket(a, 15) == time_bucket(b, 15)

    def test_crossing_a_boundary_produces_a_new_bucket(self):
        a = datetime(2026, 11, 15, 4, 44, tzinfo=timezone.utc)
        b = datetime(2026, 11, 15, 4, 45, tzinfo=timezone.utc)
        assert time_bucket(a, 15) != time_bucket(b, 15)

    def test_naive_input_is_converted_not_rejected(self):
        aware = datetime(2026, 11, 15, 4, 37, tzinfo=timezone.utc)
        assert time_bucket(aware, 15) == "2026-11-15T04:30"

    def test_rejects_zero_minutes(self):
        with pytest.raises(ValueError):
            time_bucket(T0, 0)


class TestEventKey:
    def test_identical_conditions_produce_identical_keys(self):
        """The whole mechanism rests on this."""
        kwargs = dict(category="momentum", chain="base", token_address=ADDR,
                      event_type="volume_spike", bucket_minutes=15)
        first = build_event_key(moment=T0, **kwargs)
        later = build_event_key(moment=T0 + timedelta(minutes=7), **kwargs)
        assert first == later

    def test_a_new_bucket_allows_a_fresh_alert(self):
        kwargs = dict(category="momentum", chain="base", token_address=ADDR,
                      event_type="volume_spike", bucket_minutes=15)
        assert build_event_key(moment=T0, **kwargs) != build_event_key(
            moment=T0 + timedelta(minutes=30), **kwargs)

    def test_address_case_cannot_split_a_key(self):
        """Two keys differing only by case would defeat deduplication."""
        base = dict(category="risk", chain="base", event_type="liquidity_drain",
                    moment=T0, bucket_minutes=10)
        assert (build_event_key(token_address="0xABCdef", **base)
                == build_event_key(token_address="0xabcdef", **base))

    def test_different_detectors_do_not_collide(self):
        base = dict(category="momentum", chain="base", token_address=ADDR,
                    moment=T0, bucket_minutes=15)
        assert (build_event_key(event_type="volume_spike", **base)
                != build_event_key(event_type="price_surge", **base))

    def test_different_chains_do_not_collide(self):
        base = dict(category="momentum", token_address=ADDR,
                    event_type="volume_spike", moment=T0, bucket_minutes=15)
        assert (build_event_key(chain="base", **base)
                != build_event_key(chain="bsc", **base))

    def test_no_bucket_means_once_ever(self):
        key = build_event_key(category="new_coin", chain="solana",
                              token_address="Abc", event_type="first_seen",
                              bucket_minutes=None)
        assert key.count(":") == 3
        later = build_event_key(category="new_coin", chain="solana",
                                token_address="Abc", event_type="first_seen",
                                moment=T0 + timedelta(days=30), bucket_minutes=None)
        assert key == later

    def test_rejects_an_invalid_category(self):
        with pytest.raises(ValueError, match="category"):
            build_event_key(category="moonshot", chain="base",
                            token_address=ADDR, event_type="x")

    @pytest.mark.parametrize("missing", [
        {"chain": ""}, {"token_address": ""}, {"event_type": ""},
    ])
    def test_rejects_missing_parts(self, missing):
        kwargs = dict(category="risk", chain="base", token_address=ADDR,
                      event_type="x")
        kwargs.update(missing)
        with pytest.raises(ValueError):
            build_event_key(**kwargs)


# ===========================================================================
# Detectors
# ===========================================================================

class TestNoiseFloors:
    """Without these, the feed fills with dust and nobody reads it."""

    def test_dust_token_spiking_400pct_is_ignored(self):
        detector = VolumeSpikeDetector(min_liquidity=5_000, min_volume=10_000)
        previous = snap(volume=100.0, liquidity=200.0)
        current = obs(volume_24h_usd=500.0, liquidity_usd=200.0)
        assert detector.detect(previous, current, DetectionContext()) is None

    def test_unknown_liquidity_fails_the_floor(self):
        """We cannot confirm it is substantial, so we do not alert."""
        detector = VolumeSpikeDetector()
        previous = snap(volume=10_000.0)
        current = obs(volume_24h_usd=50_000.0, liquidity_usd=None)
        assert detector.detect(previous, current, DetectionContext()) is None

    def test_substantial_token_does_alert(self):
        detector = VolumeSpikeDetector(min_liquidity=5_000, min_volume=10_000)
        previous = snap(volume=20_000.0, liquidity=50_000.0)
        current = obs(volume_24h_usd=80_000.0, liquidity_usd=50_000.0)
        assert detector.detect(previous, current, DetectionContext()) is not None


class TestVolumeSpike:
    def test_fires_above_threshold(self):
        d = VolumeSpikeDetector(threshold_pct=150.0)
        event = d.detect(snap(volume=20_000.0, liquidity=50_000.0),
                         obs(volume_24h_usd=60_000.0, liquidity_usd=50_000.0),
                         DetectionContext())
        assert event is not None
        assert event.category == "momentum"
        assert event.event_type == "volume_spike"
        assert event.payload["change_pct"] == pytest.approx(200.0)
        assert event.payload["before"] == 20_000.0
        assert event.payload["after"] == 60_000.0

    def test_does_not_fire_below_threshold(self):
        d = VolumeSpikeDetector(threshold_pct=150.0)
        assert d.detect(snap(volume=20_000.0, liquidity=50_000.0),
                        obs(volume_24h_usd=40_000.0, liquidity_usd=50_000.0),
                        DetectionContext()) is None

    def test_severity_escalates_with_magnitude(self):
        d = VolumeSpikeDetector(threshold_pct=100.0)
        modest = d.detect(snap(volume=20_000.0, liquidity=50_000.0),
                          obs(volume_24h_usd=50_000.0, liquidity_usd=50_000.0),
                          DetectionContext())
        extreme = d.detect(snap(volume=20_000.0, liquidity=50_000.0),
                           obs(volume_24h_usd=500_000.0, liquidity_usd=50_000.0),
                           DetectionContext())
        assert modest.severity == "medium"
        assert extreme.severity == "high"

    def test_no_previous_snapshot_means_no_spike(self):
        d = VolumeSpikeDetector()
        assert d.detect(None, obs(volume_24h_usd=1_000_000.0,
                                  liquidity_usd=50_000.0),
                        DetectionContext()) is None


class TestLiquidityDrain:
    def test_fires_on_a_large_drop(self):
        d = LiquidityDrainDetector(threshold_pct=30.0)
        event = d.detect(snap(liquidity=100_000.0),
                         obs(liquidity_usd=40_000.0), DetectionContext())
        assert event is not None
        assert event.category == "risk"
        assert event.severity == "high"          # 60% drop
        assert event.payload["change_pct"] == pytest.approx(-60.0)

    def test_near_total_drain_is_critical(self):
        d = LiquidityDrainDetector(threshold_pct=30.0)
        event = d.detect(snap(liquidity=100_000.0),
                         obs(liquidity_usd=5_000.0), DetectionContext())
        assert event.severity == "critical"

    def test_liquidity_increase_is_not_a_drain(self):
        d = LiquidityDrainDetector()
        assert d.detect(snap(liquidity=50_000.0),
                        obs(liquidity_usd=150_000.0), DetectionContext()) is None

    def test_liquidity_vanishing_from_the_feed_is_its_own_event(self):
        """No percentage exists for this, but it may be the most important case."""
        d = LiquidityDrainDetector()
        event = d.detect(snap(liquidity=100_000.0),
                         obs(liquidity_usd=None), DetectionContext())
        assert event is not None
        assert event.event_type == "liquidity_unreported"
        assert event.severity == "high"
        assert event.payload["after"] is None

    def test_tiny_pool_draining_is_below_the_floor(self):
        d = LiquidityDrainDetector(min_liquidity_before=5_000.0)
        assert d.detect(snap(liquidity=800.0),
                        obs(liquidity_usd=100.0), DetectionContext()) is None

    def test_headline_states_both_numbers(self):
        d = LiquidityDrainDetector()
        event = d.detect(snap(liquidity=100_000.0),
                         obs(liquidity_usd=40_000.0), DetectionContext())
        assert "$100.0K" in event.headline and "$40.0K" in event.headline


class TestPriceMove:
    def test_surge_is_momentum(self):
        d = PriceMoveDetector(surge_pct=25.0)
        event = d.detect(snap(price=1.0, liquidity=50_000.0, volume=20_000.0),
                         obs(price_usd=1.5, liquidity_usd=50_000.0,
                             volume_24h_usd=20_000.0), DetectionContext())
        assert event.category == "momentum" and event.event_type == "price_surge"

    def test_collapse_is_risk(self):
        d = PriceMoveDetector(collapse_pct=30.0)
        event = d.detect(snap(price=1.0, liquidity=50_000.0, volume=20_000.0),
                         obs(price_usd=0.4, liquidity_usd=50_000.0,
                             volume_24h_usd=20_000.0), DetectionContext())
        assert event.category == "risk" and event.event_type == "price_collapse"
        assert event.severity == "high"

    def test_small_move_fires_nothing(self):
        d = PriceMoveDetector(surge_pct=25.0, collapse_pct=30.0)
        assert d.detect(snap(price=1.0, liquidity=50_000.0, volume=20_000.0),
                        obs(price_usd=1.1, liquidity_usd=50_000.0,
                            volume_24h_usd=20_000.0), DetectionContext()) is None


class TestChurnRatio:
    def test_fires_on_high_churn(self):
        d = ChurnRatioDetector(threshold=10.0)
        event = d.detect(None, obs(volume_24h_usd=600_000.0,
                                   liquidity_usd=50_000.0), DetectionContext())
        assert event is not None
        assert event.payload["ratio"] == pytest.approx(12.0)

    def test_presents_itself_as_an_observation_not_a_verdict(self):
        """The spec forbids presenting speculation as fact."""
        d = ChurnRatioDetector(threshold=10.0)
        event = d.detect(None, obs(volume_24h_usd=600_000.0,
                                   liquidity_usd=50_000.0), DetectionContext())
        interpretation = event.payload["interpretation"].lower()
        assert "not a conclusion" in interpretation
        assert "can indicate" in interpretation

    def test_normal_churn_is_quiet(self):
        d = ChurnRatioDetector(threshold=10.0)
        assert d.detect(None, obs(volume_24h_usd=100_000.0,
                                  liquidity_usd=50_000.0),
                        DetectionContext()) is None


class TestNewToken:
    def test_fires_only_for_a_genuinely_new_token(self):
        d = NewTokenDetector(min_liquidity=1_000.0)
        assert d.detect(None, obs(liquidity_usd=5_000.0),
                        DetectionContext(is_new_token=True)) is not None
        assert d.detect(None, obs(liquidity_usd=5_000.0),
                        DetectionContext(is_new_token=False)) is None

    def test_key_has_no_time_component_so_it_fires_once_ever(self):
        d = NewTokenDetector(min_liquidity=1_000.0)
        event = d.detect(None, obs(liquidity_usd=5_000.0),
                         DetectionContext(is_new_token=True))
        assert event.event_key.endswith("first_seen")

    def test_uses_a_lower_floor_than_momentum_detectors(self):
        """A brand-new pool has no volume history yet, by definition."""
        d = NewTokenDetector(min_liquidity=1_000.0)
        event = d.detect(None, obs(liquidity_usd=2_000.0, volume_24h_usd=0.0),
                         DetectionContext(is_new_token=True))
        assert event is not None


class TestRunDetectors:
    def test_several_can_fire_on_one_observation(self, settings):
        detectors = default_detectors(settings)
        previous = snap(price=1.0, liquidity=100_000.0, volume=20_000.0)
        current = obs(price_usd=2.0, liquidity_usd=30_000.0, volume_24h_usd=400_000.0)
        events = run_detectors(detectors, previous, current)
        types = {e.event_type for e in events}
        assert "liquidity_drain" in types
        assert "volume_spike" in types
        assert "price_surge" in types

    def test_a_broken_detector_does_not_blind_the_others(self, settings):
        class Exploding:
            name = "exploding"
            def detect(self, previous, current, context):
                raise RuntimeError("bug in a rule")

        detectors = [Exploding(), VolumeSpikeDetector(threshold_pct=10.0)]
        events = run_detectors(
            detectors, snap(volume=20_000.0, liquidity=50_000.0),
            obs(volume_24h_usd=80_000.0, liquidity_usd=50_000.0))
        assert len(events) == 1

    def test_quiet_token_produces_nothing(self, settings):
        detectors = default_detectors(settings)
        previous = snap(price=1.0, liquidity=50_000.0, volume=20_000.0)
        current = obs(price_usd=1.01, liquidity_usd=50_100.0, volume_24h_usd=20_500.0)
        assert run_detectors(detectors, previous, current) == []


# ===========================================================================
# Risk heuristics
# ===========================================================================

class TestRiskAssessment:
    def test_thin_liquidity_is_flagged(self, settings):
        findings = risk.assess(obs(liquidity_usd=800.0, volume_24h_usd=5_000.0), settings)
        types = {f.flag_type for f in findings}
        assert "low_liquidity" in types

    def test_unknown_liquidity_is_flagged_separately(self, settings):
        findings = risk.assess(obs(liquidity_usd=None), settings)
        assert "liquidity_unknown" in {f.flag_type for f in findings}

    def test_one_sided_buying_is_flagged(self, settings):
        findings = risk.assess(
            obs(liquidity_usd=50_000.0, volume_24h_usd=20_000.0,
                buys_24h=400, sells_24h=3), settings)
        flag = next(f for f in findings if f.flag_type == "sells_nearly_absent")
        assert flag.severity == "high"
        assert "not determined" in flag.detail.lower()

    def test_one_sided_selling_is_flagged(self, settings):
        findings = risk.assess(
            obs(liquidity_usd=50_000.0, volume_24h_usd=20_000.0,
                buys_24h=2, sells_24h=300), settings)
        assert "buys_nearly_absent" in {f.flag_type for f in findings}

    def test_small_sample_is_not_flagged_as_one_sided(self, settings):
        """3 buys and 0 sells is not evidence of anything."""
        findings = risk.assess(
            obs(liquidity_usd=50_000.0, volume_24h_usd=20_000.0,
                buys_24h=3, sells_24h=0), settings)
        types = {f.flag_type for f in findings}
        assert "sells_nearly_absent" not in types

    def test_missing_verification_is_always_recorded(self, settings):
        """What we could NOT check is itself honest information."""
        findings = risk.assess(obs(liquidity_usd=500_000.0, volume_24h_usd=100_000.0),
                               settings)
        flag = next(f for f in findings if f.flag_type == "missing_verification")
        assert flag.severity == "info"
        assert "not evidence of safety" in flag.detail

    def test_everything_is_marked_as_our_own_derivation(self, settings):
        """Heuristics must never masquerade as provider-asserted facts."""
        findings = risk.assess(obs(liquidity_usd=800.0), settings)
        assert all(f.source == "heuristic" for f in findings)

    def test_healthy_token_has_only_the_informational_flag(self, settings):
        findings = risk.assess(
            obs(liquidity_usd=500_000.0, volume_24h_usd=200_000.0,
                price_usd=1.0, price_change_24h=3.0, fdv_usd=2_000_000.0,
                buys_24h=400, sells_24h=380), settings)
        assert risk.actionable(findings) == []
        assert len(findings) == 1

    def test_highest_severity_picks_the_worst(self, settings):
        findings = risk.assess(obs(liquidity_usd=100.0, volume_24h_usd=50_000.0),
                               settings)
        assert risk.highest_severity(findings) in ("high", "critical")

    def test_highest_severity_of_nothing_is_info(self):
        assert risk.highest_severity([]) == "info"
