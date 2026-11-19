"""Tests for the DEX Screener and GeckoTerminal adapters.

The DEX Screener fixture is a real captured response (see conftest.py), so
these tests check parsing against the shape the API actually returns rather
than the shape we hoped for.
"""

from __future__ import annotations

import json

import pytest

from conftest import (
    DEXSCREENER_PAIR,
    DEXSCREENER_PAIR_UNSUPPORTED_CHAIN,
    GECKOTERMINAL_POOL,
    FakeTransport,
    json_response,
)
from mihq.providers.dexscreener import DexScreenerProvider, best_observation
from mihq.providers.errors import ProviderBadResponse, ProviderNotFound
from mihq.providers.geckoterminal import GeckoTerminalProvider


def ds_provider(settings, clock, payload):
    transport = FakeTransport([json_response(json.dumps(payload))])
    provider = DexScreenerProvider(settings, transport=transport, clock=clock,
                                   sleep=clock.sleep)
    return provider, transport


def gt_provider(settings, clock, payload):
    transport = FakeTransport([json_response(json.dumps(payload))])
    provider = GeckoTerminalProvider(settings, transport=transport, clock=clock,
                                     sleep=clock.sleep)
    return provider, transport


# ===========================================================================
# DEX Screener
# ===========================================================================

class TestDexScreenerParsing:
    def test_parses_every_field_from_a_real_response(self, settings, clock):
        provider, _ = ds_provider(settings, clock, {"pairs": [DEXSCREENER_PAIR]})
        [obs] = provider.search("WIF")

        assert obs.source == "dexscreener"
        assert obs.token.chain == "base"
        assert obs.token.symbol == "WIF"
        assert obs.token.name == "World is Flat"
        assert obs.pool_address == "0x71ef4106340f0a3b9c19ec994eef4fc44a1d2513"
        assert obs.dex_id == "uniswap"
        assert obs.quote_symbol == "ETH"

        # priceUsd arrives as a STRING in the real payload.
        assert obs.price_usd == pytest.approx(0.001112)
        assert obs.price_native == pytest.approx(0.0000006124)
        # ...while liquidity and volume arrive as numbers.
        assert obs.liquidity_usd == pytest.approx(83243.39)
        assert obs.volume_24h_usd == pytest.approx(119942.98)
        assert obs.volume_5m_usd == pytest.approx(7.25)

        assert obs.price_change_24h == pytest.approx(8.13)
        assert obs.price_change_5m == pytest.approx(-0.33)
        assert obs.fdv_usd == 1112036
        assert obs.market_cap_usd == 1112036

        assert obs.buys_24h == 812 and obs.sells_24h == 833
        assert obs.buys_1h == 9 and obs.sells_1h == 11
        assert obs.total_txns_24h == 1645

        assert obs.pair_created_at is not None and obs.pair_created_at.year == 2026
        assert obs.source_url and obs.source_url.startswith("https://dexscreener.com/")

    def test_evm_address_is_lowercased_for_the_database(self, settings, clock):
        """The fixture's address is mixed-case; stored form must be canonical."""
        provider, _ = ds_provider(settings, clock, {"pairs": [DEXSCREENER_PAIR]})
        [obs] = provider.search("WIF")
        assert obs.token.address == "0x5173b8ea6923c2d40267890d45c46a3480954773"
        assert obs.token.address == DEXSCREENER_PAIR["baseToken"]["address"].lower()

    def test_holder_count_is_none_because_the_api_omits_it(self, settings, clock):
        provider, _ = ds_provider(settings, clock, {"pairs": [DEXSCREENER_PAIR]})
        [obs] = provider.search("WIF")
        assert obs.holder_count is None

    def test_raw_payload_is_preserved_for_audit(self, settings, clock):
        provider, _ = ds_provider(settings, clock, {"pairs": [DEXSCREENER_PAIR]})
        [obs] = provider.search("WIF")
        assert obs.raw["pairAddress"] == DEXSCREENER_PAIR["pairAddress"]


class TestDexScreenerSkipping:
    def test_unsupported_chain_is_skipped_silently(self, settings, clock):
        """A real WIF search returned a pair on chain 'robinhood'."""
        provider, _ = ds_provider(
            settings, clock, {"pairs": [DEXSCREENER_PAIR_UNSUPPORTED_CHAIN]}
        )
        assert provider.search("WIF") == []

    def test_supported_pairs_survive_alongside_unsupported_ones(self, settings, clock):
        provider, _ = ds_provider(
            settings, clock,
            {"pairs": [DEXSCREENER_PAIR_UNSUPPORTED_CHAIN, DEXSCREENER_PAIR]},
        )
        results = provider.search("WIF")
        assert len(results) == 1
        assert results[0].token.chain == "base"

    def test_malformed_address_is_skipped_not_raised(self, settings, clock):
        """One bad entry must not abort an entire scan."""
        broken = {**DEXSCREENER_PAIR,
                  "baseToken": {**DEXSCREENER_PAIR["baseToken"], "address": "not-an-address"}}
        provider, _ = ds_provider(settings, clock, {"pairs": [broken, DEXSCREENER_PAIR]})
        assert len(provider.search("WIF")) == 1

    @pytest.mark.parametrize("bad_pair", [
        {},                                    # empty object
        {"chainId": "base"},                   # no baseToken
        {"chainId": "base", "baseToken": {}},  # baseToken with no address
        {"chainId": "base", "baseToken": "nonsense"},
    ])
    def test_structurally_broken_pairs_are_skipped(self, settings, clock, bad_pair):
        provider, _ = ds_provider(settings, clock, {"pairs": [bad_pair]})
        assert provider.search("x") == []


class TestDexScreenerPartialData:
    def test_missing_sections_do_not_crash_and_stay_none(self, settings, clock):
        """Newly created pools often have no volume or liquidity yet."""
        sparse = {
            "chainId": "solana",
            "baseToken": {"address": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
                          "symbol": "BONK"},
            "pairAddress": "8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj",
        }
        provider, _ = ds_provider(settings, clock, {"pairs": [sparse]})
        [obs] = provider.search("BONK")
        assert obs.token.symbol == "BONK"
        assert obs.price_usd is None
        assert obs.liquidity_usd is None
        assert obs.volume_24h_usd is None
        assert obs.buys_24h is None
        assert obs.completeness() == 0.0

    def test_solana_address_case_is_preserved(self, settings, clock):
        """Lowercasing base58 would produce a different, wrong address."""
        original = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
        sparse = {"chainId": "solana", "baseToken": {"address": original}}
        provider, _ = ds_provider(settings, clock, {"pairs": [sparse]})
        [obs] = provider.search("BONK")
        assert obs.token.address == original

    def test_null_pairs_means_nothing_found(self, settings, clock):
        provider, _ = ds_provider(settings, clock, {"pairs": None})
        assert provider.search("nothing") == []

    def test_bare_list_response_is_accepted(self, settings, clock):
        """/tokens/v1/* returns a list, not a {"pairs": ...} wrapper."""
        provider, _ = ds_provider(settings, clock, [DEXSCREENER_PAIR])
        assert len(provider.search("WIF")) == 1

    def test_unexpected_shape_raises_bad_response(self, settings, clock):
        provider, _ = ds_provider(settings, clock, "a string, somehow")
        with pytest.raises(ProviderBadResponse):
            provider.search("x")


class TestDexScreenerEndpoints:
    def test_pairs_for_token_uses_the_provider_chain_id(self, settings, clock):
        provider, transport = ds_provider(settings, clock, [DEXSCREENER_PAIR])
        provider.pairs_for_token("bsc", "0x" + "A" * 40)
        url = transport.calls[0]["url"]
        assert "/tokens/v1/bsc/" in url
        assert "0x" + "a" * 40 in url, "address should be normalized before the request"

    def test_pairs_for_token_raises_when_nothing_is_trading(self, settings, clock):
        provider, _ = ds_provider(settings, clock, [])
        with pytest.raises(ProviderNotFound):
            provider.pairs_for_token("base", "0x" + "a" * 40)

    def test_pair_lookups_are_batched_in_thirties(self, settings, clock):
        """31 addresses must be 2 requests, not 31."""
        transport = FakeTransport([json_response(json.dumps({"pairs": []}))])
        provider = DexScreenerProvider(settings, transport=transport, clock=clock,
                                       sleep=clock.sleep)
        provider.pairs_by_address("base", [f"0x{i:040x}" for i in range(31)])
        assert transport.call_count == 2

    def test_empty_address_list_makes_no_request(self, settings, clock):
        transport = FakeTransport([json_response("{}")])
        provider = DexScreenerProvider(settings, transport=transport, clock=clock,
                                       sleep=clock.sleep)
        assert provider.pairs_by_address("base", []) == []
        assert transport.call_count == 0

    def test_token_profiles_returns_only_supported_chains(self, settings, clock):
        payload = [
            {"chainId": "base", "tokenAddress": "0x" + "b" * 40},
            {"chainId": "robinhood", "tokenAddress": "0x" + "c" * 40},
            {"chainId": "base", "tokenAddress": "garbage"},
            {"chainId": "base"},
            "not even an object",
        ]
        provider, _ = ds_provider(settings, clock, payload)
        refs = provider.latest_token_profiles()
        assert len(refs) == 1
        assert refs[0].chain == "base"

    def test_token_profiles_rejects_a_non_list_body(self, settings, clock):
        provider, _ = ds_provider(settings, clock, {"unexpected": "shape"})
        with pytest.raises(ProviderBadResponse):
            provider.latest_token_profiles()


class TestBestObservation:
    def test_prefers_the_deepest_liquidity(self, settings, clock):
        thin = {**DEXSCREENER_PAIR, "liquidity": {"usd": 2000.0},
                "pairAddress": "0x" + "1" * 40}
        deep = {**DEXSCREENER_PAIR, "liquidity": {"usd": 200000.0},
                "pairAddress": "0x" + "2" * 40}
        provider, _ = ds_provider(settings, clock, {"pairs": [thin, deep]})
        best = best_observation(provider.search("WIF"))
        assert best is not None
        assert best.liquidity_usd == 200000.0

    def test_known_liquidity_beats_unknown(self, settings, clock):
        """Unknown must not be treated as zero, nor as infinitely good."""
        unknown = {**DEXSCREENER_PAIR, "liquidity": {}, "pairAddress": "0x" + "1" * 40}
        known = {**DEXSCREENER_PAIR, "liquidity": {"usd": 10.0},
                 "pairAddress": "0x" + "2" * 40}
        provider, _ = ds_provider(settings, clock, {"pairs": [unknown, known]})
        best = best_observation(provider.search("WIF"))
        assert best is not None and best.liquidity_usd == 10.0

    def test_empty_input_returns_none(self):
        assert best_observation([]) is None


# ===========================================================================
# GeckoTerminal
# ===========================================================================

class TestGeckoTerminalParsing:
    def test_parses_a_pool(self, settings, clock):
        provider, _ = gt_provider(settings, clock, {"data": [GECKOTERMINAL_POOL]})
        [obs] = provider.new_pools("solana")

        assert obs.source == "geckoterminal"
        assert obs.token.chain == "solana"
        assert obs.token.address == "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
        assert obs.token.symbol == "BONK"
        assert obs.quote_symbol == "SOL"
        assert obs.dex_id == "raydium"

        assert obs.price_usd == pytest.approx(0.00002341)
        # GeckoTerminal calls pool liquidity "reserve_in_usd".
        assert obs.liquidity_usd == pytest.approx(412880.55)
        assert obs.volume_24h_usd == pytest.approx(488210.7)
        assert obs.price_change_24h == pytest.approx(42.1)
        assert obs.buys_24h == 4120 and obs.sells_24h == 3890
        assert obs.fdv_usd == pytest.approx(1623400.0)
        assert obs.market_cap_usd is None       # explicitly null in the payload
        assert obs.pair_created_at is not None

    def test_token_address_is_unpacked_from_the_relationship_id(self, settings, clock):
        """Ids look like 'solana_<address>'; the prefix must be stripped."""
        provider, _ = gt_provider(settings, clock, {"data": [GECKOTERMINAL_POOL]})
        [obs] = provider.new_pools("solana")
        assert not obs.token.address.startswith("solana_")

    def test_network_id_differs_from_our_chain_key(self, settings, clock):
        """Ethereum is 'eth' and Avalanche is 'avax' on GeckoTerminal."""
        provider, transport = gt_provider(settings, clock, {"data": []})
        provider.new_pools("ethereum")
        assert "/networks/eth/new_pools" in transport.calls[0]["url"]

        provider2, transport2 = gt_provider(settings, clock, {"data": []})
        provider2.new_pools("avalanche")
        assert "/networks/avax/new_pools" in transport2.calls[0]["url"]

    def test_prefixed_id_for_a_multiword_network_is_handled(self, settings, clock):
        """A network id containing an underscore must not break the split."""
        pool = {
            **GECKOTERMINAL_POOL,
            "relationships": {
                "base_token": {"data": {"id": "eth_0x" + "a" * 40}},
            },
        }
        provider, _ = gt_provider(settings, clock, {"data": [pool]})
        [obs] = provider.new_pools("ethereum")
        assert obs.token.address == "0x" + "a" * 40


class TestGeckoTerminalEdges:
    @pytest.mark.parametrize("pool", [
        {},                                         # nothing at all
        {"attributes": {}},                         # no relationships
        {"attributes": {}, "relationships": {}},    # no base_token
        {"attributes": {}, "relationships": {"base_token": {}}},
        {"attributes": "nonsense", "relationships": {}},
    ])
    def test_broken_pools_are_skipped(self, settings, clock, pool):
        provider, _ = gt_provider(settings, clock, {"data": [pool]})
        assert provider.new_pools("solana") == []

    def test_missing_metrics_stay_none(self, settings, clock):
        bare = {
            "attributes": {"address": "8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj"},
            "relationships": {
                "base_token": {"data": {"id": "solana_DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"}}
            },
        }
        provider, _ = gt_provider(settings, clock, {"data": [bare]})
        [obs] = provider.new_pools("solana")
        assert obs.price_usd is None
        assert obs.liquidity_usd is None
        assert obs.buys_24h is None

    def test_empty_data_is_not_an_error(self, settings, clock):
        provider, _ = gt_provider(settings, clock, {"data": []})
        assert provider.new_pools("solana") == []

    def test_missing_data_key_is_not_an_error(self, settings, clock):
        provider, _ = gt_provider(settings, clock, {})
        assert provider.new_pools("solana") == []

    def test_non_object_body_raises(self, settings, clock):
        provider, _ = gt_provider(settings, clock, ["unexpected"])
        with pytest.raises(ProviderBadResponse):
            provider.new_pools("solana")

    def test_single_pool_lookup(self, settings, clock):
        provider, _ = gt_provider(settings, clock, {"data": GECKOTERMINAL_POOL})
        obs = provider.pool("solana", "8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj")
        assert obs.token.symbol == "BONK"

    def test_missing_pool_raises_not_found(self, settings, clock):
        provider, _ = gt_provider(settings, clock, {"data": None})
        with pytest.raises(ProviderNotFound):
            provider.pool("solana", "whatever")


class TestProvidersSharePolicy:
    def test_both_providers_rate_limit_and_cache(self, settings, clock):
        for provider in (
            DexScreenerProvider(settings, transport=FakeTransport([json_response("{}")]),
                                clock=clock, sleep=clock.sleep),
            GeckoTerminalProvider(settings, transport=FakeTransport([json_response("{}")]),
                                  clock=clock, sleep=clock.sleep),
        ):
            assert provider.client.limiter.per_minute > 0
            assert provider.client.cache.ttl > 0

    def test_geckoterminal_is_limited_more_tightly_than_dexscreener(self, settings, clock):
        """Their published limits differ by an order of magnitude."""
        ds = DexScreenerProvider(settings, transport=FakeTransport([json_response("{}")]),
                                 clock=clock, sleep=clock.sleep)
        gt = GeckoTerminalProvider(settings, transport=FakeTransport([json_response("{}")]),
                                   clock=clock, sleep=clock.sleep)
        assert gt.client.limiter.per_minute < ds.client.limiter.per_minute

    def test_stats_are_exposed_for_scanner_runs(self, settings, clock):
        provider, _ = ds_provider(settings, clock, {"pairs": []})
        provider.search("x")
        stats = provider.stats()
        assert stats["provider"] == "dexscreener"
        assert stats["requests"] == 1
