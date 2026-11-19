"""Tests for the normalized model and its parsing helpers.

The theme throughout: missing data must stay missing. Turning an absent
value into 0.0 would make a token with no reported liquidity look identical
to a token with genuinely zero liquidity, and the project spec forbids
inventing data.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mihq.models import (
    MarketObservation,
    TokenRef,
    iso_to_datetime,
    ms_to_datetime,
    to_float,
    to_int,
    utcnow,
)


class TestToFloat:
    @pytest.mark.parametrize("raw,expected", [
        ("0.001112", 0.001112),     # DEX Screener sends prices as strings
        (119942.98, 119942.98),     # ...and volumes as numbers
        ("119942.98", 119942.98),
        (0, 0.0),
        ("0", 0.0),
        (-5.5, -5.5),
        ("1,234.56", 1234.56),      # thousands separators appear occasionally
        ("  42  ", 42.0),
        ("1e-9", 1e-9),             # tiny memecoin prices use exponent form
    ])
    def test_parses_valid_numbers(self, raw, expected):
        assert to_float(raw) == pytest.approx(expected)

    @pytest.mark.parametrize("raw", [None, "", "   ", "N/A", "null", "abc", [], {}, object()])
    def test_returns_none_for_unusable_values(self, raw):
        assert to_float(raw) is None

    @pytest.mark.parametrize("raw", [True, False])
    def test_rejects_booleans(self, raw):
        """A bool where a price belongs means the payload changed shape."""
        assert to_float(raw) is None

    @pytest.mark.parametrize("raw", [float("nan"), float("inf"), float("-inf"),
                                     "nan", "inf", "-inf"])
    def test_rejects_nan_and_infinity(self, raw):
        """Postgres numeric columns cannot store these."""
        assert to_float(raw) is None

    def test_zero_is_preserved_not_treated_as_missing(self):
        """Zero volume is a real observation, not an absent one."""
        assert to_float(0) == 0.0
        assert to_float("0.0") == 0.0


class TestToInt:
    @pytest.mark.parametrize("raw,expected", [(812, 812), ("812", 812), (812.0, 812),
                                              (812.7, 812), (0, 0)])
    def test_parses_counts(self, raw, expected):
        assert to_int(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "abc", True, float("nan")])
    def test_returns_none_for_unusable(self, raw):
        assert to_int(raw) is None


class TestTimestamps:
    def test_parses_dexscreener_milliseconds(self):
        """pairCreatedAt is milliseconds, not seconds."""
        result = ms_to_datetime(1786009343000)
        assert result is not None
        assert result.tzinfo is not None
        assert result.year == 2026

    def test_rejects_seconds_mistaken_for_milliseconds(self):
        """1786009343 as ms would be 1970, which is obviously wrong."""
        assert ms_to_datetime(1786009343) is None

    @pytest.mark.parametrize("raw", [None, 0, -1, "", "abc", 99_999_999_999_999_999])
    def test_rejects_nonsense(self, raw):
        assert ms_to_datetime(raw) is None

    def test_parses_geckoterminal_iso_with_z(self):
        result = iso_to_datetime("2026-11-14T22:13:05Z")
        assert result == datetime(2026, 11, 14, 22, 13, 5, tzinfo=timezone.utc)

    def test_parses_iso_with_offset(self):
        result = iso_to_datetime("2026-11-14T22:13:05+00:00")
        assert result is not None and result.tzinfo is not None

    def test_naive_iso_is_assumed_utc(self):
        result = iso_to_datetime("2026-11-14T22:13:05")
        assert result is not None and result.tzinfo == timezone.utc

    @pytest.mark.parametrize("raw", [None, "", "not a date", 123, []])
    def test_rejects_bad_iso(self, raw):
        assert iso_to_datetime(raw) is None

    def test_utcnow_is_timezone_aware(self):
        """Naive datetimes silently break timestamptz comparisons."""
        assert utcnow().tzinfo is not None


class TestTokenRef:
    def test_key_matches_the_database_unique_constraint(self):
        ref = TokenRef(chain="base", address="0xabc", symbol="X")
        assert ref.key == ("base", "0xabc")

    def test_str_is_readable_without_a_symbol(self):
        assert "unknown" in str(TokenRef(chain="base", address="0x" + "a" * 40))


def observation(**overrides) -> MarketObservation:
    defaults = dict(
        token=TokenRef(chain="base", address="0x" + "a" * 40, symbol="TEST"),
        source="dexscreener",
        captured_at=utcnow(),
    )
    defaults.update(overrides)
    return MarketObservation(**defaults)


class TestMarketObservation:
    def test_everything_optional_defaults_to_none(self):
        obs = observation()
        assert obs.price_usd is None
        assert obs.liquidity_usd is None
        assert obs.holder_count is None

    def test_total_txns_requires_both_sides(self):
        assert observation(buys_24h=812, sells_24h=833).total_txns_24h == 1645
        assert observation(buys_24h=812).total_txns_24h is None
        assert observation(sells_24h=833).total_txns_24h is None

    def test_volume_to_liquidity_ratio(self):
        obs = observation(volume_24h_usd=120000.0, liquidity_usd=60000.0)
        assert obs.volume_to_liquidity_24h == pytest.approx(2.0)

    def test_ratio_is_none_when_liquidity_unknown_or_zero(self):
        """Never divide by zero, and never pretend unknown means zero."""
        assert observation(volume_24h_usd=120000.0).volume_to_liquidity_24h is None
        assert observation(volume_24h_usd=120000.0,
                           liquidity_usd=0.0).volume_to_liquidity_24h is None

    def test_completeness_scores_what_was_supplied(self):
        assert observation().completeness() == 0.0
        full = observation(price_usd=1.0, liquidity_usd=2.0, volume_24h_usd=3.0,
                           price_change_24h=4.0, fdv_usd=5.0, buys_24h=6)
        assert full.completeness() == 1.0

    def test_missing_fields_names_the_gaps(self):
        obs = observation(price_usd=1.0, liquidity_usd=2.0)
        missing = obs.missing_fields()
        assert "holder_count" in missing
        assert "volume_24h_usd" in missing
        assert "price_usd" not in missing

    def test_snapshot_row_matches_the_table_columns(self):
        row = observation(price_usd=0.001112, liquidity_usd=83243.39).to_snapshot_row(42)
        assert row["token_id"] == 42
        assert row["chain"] == "base"
        assert row["source"] == "dexscreener"
        assert row["price_usd"] == 0.001112
        # Columns the provider did not fill must be None, never 0.
        assert row["holder_count"] is None
        assert row["market_cap_usd"] is None

    def test_snapshot_row_keys_are_all_real_columns(self):
        """Guards against a typo producing a silent INSERT failure."""
        columns = {
            "token_id", "chain", "pool_address", "source", "captured_at",
            "provider_updated_at", "price_usd", "price_native", "liquidity_usd",
            "fdv_usd", "market_cap_usd", "volume_5m_usd", "volume_1h_usd",
            "volume_6h_usd", "volume_24h_usd", "price_change_5m", "price_change_1h",
            "price_change_6h", "price_change_24h", "buys_1h", "sells_1h",
            "buys_24h", "sells_24h", "holder_count", "raw",
        }
        assert set(observation().to_snapshot_row(1)) == columns

    def test_summary_says_unknown_rather_than_zero(self):
        text = observation().summary()
        assert "unknown" in text
        assert "$0.00" not in text
