"""Shared test helpers.

Everything here exists so the provider tests run with no network, no real
waiting, and identical results every time. Tests that depend on a live API
fail for reasons that have nothing to do with the code.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

import pytest

from mihq.config import Settings
from mihq.providers.http import HttpResponse, TransportError


class FakeClock:
    """A clock you control. `sleep` advances it instead of actually waiting."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds

    @property
    def total_slept(self) -> float:
        return sum(self.slept)


class FakeTransport:
    """Returns scripted responses, and records what was asked for.

    Pass a list of responses to play in order, or a callable for anything
    more involved. A response may be an `HttpResponse` or a
    `TransportError`, which is raised to simulate a network failure.
    """

    def __init__(
        self,
        responses: Sequence[HttpResponse | Exception] | Callable[..., HttpResponse],
    ) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None,
        timeout: float,
        headers: Mapping[str, str],
    ) -> HttpResponse:
        self.calls.append({"url": url, "params": dict(params or {}), "timeout": timeout})

        if callable(self._responses):
            return self._responses(url, params=params, timeout=timeout, headers=headers)

        index = min(len(self.calls) - 1, len(self._responses) - 1)
        result = self._responses[index]
        if isinstance(result, Exception):
            raise result
        return result

    @property
    def call_count(self) -> int:
        return len(self.calls)


def json_response(body: str, status: int = 200, **headers: str) -> HttpResponse:
    return HttpResponse(status_code=status, text=body, headers=headers)


@pytest.fixture
def settings() -> Settings:
    """Settings with no real credentials, safe for every test."""
    return Settings(
        database_url=None,
        dexscreener_base_url="https://api.dexscreener.com",
        dexscreener_rate_limit_per_min=240,
        geckoterminal_base_url="https://api.geckoterminal.com/api/v2",
        geckoterminal_rate_limit_per_min=25,
        provider_cache_ttl_seconds=20,
        http_timeout_seconds=15.0,
        http_max_retries=3,
        # Never let the unit suite touch the network. A test that silently
        # makes a live call is slow, flaky, and not testing what it claims.
        security_checks_enabled=False,
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


# ---------------------------------------------------------------------------
# Fixture payloads.
#
# The DEX Screener pair below is a REAL response captured from
# api.dexscreener.com on 2026-11-15, with the chain changed from the
# unsupported `robinhood` to `base` so it exercises the happy path. Field
# names, types and nesting are untouched - note priceUsd is a string while
# volume values are numbers.
# ---------------------------------------------------------------------------

DEXSCREENER_PAIR: dict[str, Any] = {
    "chainId": "base",
    "dexId": "uniswap",
    "url": "https://dexscreener.com/base/0x71ef4106340f0a3b9c19ec994eef4fc44a1d2513",
    "pairAddress": "0x71ef4106340f0a3b9c19ec994eef4fc44a1d2513",
    "labels": ["v4"],
    "baseToken": {
        "address": "0x5173b8eA6923c2d40267890D45C46a3480954773",
        "name": "World is Flat",
        "symbol": "WIF",
    },
    "quoteToken": {
        "address": "0x4200000000000000000000000000000000000006",
        "name": "Ether",
        "symbol": "ETH",
    },
    "priceNative": "0.0000006124",
    "priceUsd": "0.001112",
    "txns": {
        "m5": {"buys": 0, "sells": 1},
        "h1": {"buys": 9, "sells": 11},
        "h6": {"buys": 217, "sells": 237},
        "h24": {"buys": 812, "sells": 833},
    },
    "volume": {"h24": 119942.98, "h6": 46386.1, "h1": 171.19, "m5": 7.25},
    "priceChange": {"m5": -0.33, "h1": -1.17, "h6": 24.4, "h24": 8.13},
    "liquidity": {"usd": 83243.39, "base": 37411461, "quote": 22.9112},
    "fdv": 1112036,
    "marketCap": 1112036,
    "pairCreatedAt": 1786009343000,
    "info": {"websites": [{"url": "https://example.invalid", "label": "Website"}]},
}

# Same response shape, but on a chain our registry does not track. A real
# search for "WIF" returned exactly this situation.
DEXSCREENER_PAIR_UNSUPPORTED_CHAIN: dict[str, Any] = {
    **DEXSCREENER_PAIR,
    "chainId": "robinhood",
}

GECKOTERMINAL_POOL: dict[str, Any] = {
    "id": "solana_8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj",
    "type": "pool",
    "attributes": {
        "address": "8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj",
        "name": "BONK / SOL",
        "pool_created_at": "2026-11-14T22:13:05Z",
        "base_token_price_usd": "0.00002341",
        "base_token_price_native_currency": "0.00000012",
        "quote_token_price_usd": "195.42",
        "reserve_in_usd": "412880.55",
        "fdv_usd": "1623400.0",
        "market_cap_usd": None,
        "volume_usd": {"m5": "1820.4", "h1": "33120.9", "h6": "120400.1", "h24": "488210.7"},
        "price_change_percentage": {"m5": "0.8", "h1": "-3.2", "h6": "14.7", "h24": "42.1"},
        "transactions": {
            "m5": {"buys": 12, "sells": 9, "buyers": 10, "sellers": 8},
            "h1": {"buys": 240, "sells": 198, "buyers": 150, "sellers": 120},
            "h6": {"buys": 980, "sells": 870, "buyers": 600, "sellers": 500},
            "h24": {"buys": 4120, "sells": 3890, "buyers": 2200, "sellers": 1900},
        },
    },
    "relationships": {
        "base_token": {"data": {"id": "solana_DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
                                "type": "token"}},
        "quote_token": {"data": {"id": "solana_So11111111111111111111111111111111111111112",
                                 "type": "token"}},
        "dex": {"data": {"id": "raydium", "type": "dex"}},
    },
}
