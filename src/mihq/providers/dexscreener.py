"""DEX Screener adapter.

API: https://api.dexscreener.com  - free, no key required.

RATE LIMITS (published)
  300 requests/minute  for /latest/dex/*   (pairs, search - what we use most)
   60 requests/minute  for token-profile and token-boost endpoints

The schema below was read from a live response on 2026-11-15, not from memory.
Two details matter for parsing:

  - `priceUsd` and `priceNative` arrive as STRINGS ("0.001112"), while
    `volume` and `liquidity` arrive as NUMBERS. `to_float` handles both.
  - `pairCreatedAt` is milliseconds since the epoch, not seconds.

A real response also returns pairs on chains we do not track (one search for
"WIF" returned a pair on a chain called `robinhood`). `_normalize_pair`
returns None for those, and callers drop them silently - that is expected
traffic, not an error.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from mihq.chains import (
    InvalidAddressError,
    chain_from_dexscreener,
    get_chain,
    normalize_address,
)
from mihq.config import Settings
from mihq.logging_setup import get_logger
from mihq.models import (
    MarketObservation,
    TokenRef,
    ms_to_datetime,
    to_float,
    to_int,
    utcnow,
)
from mihq.providers.errors import ProviderBadResponse, ProviderNotFound
from mihq.providers.http import HttpClient, Transport

log = get_logger(__name__)

PROVIDER_NAME = "dexscreener"

# The API accepts at most 30 comma-separated addresses per request.
MAX_ADDRESSES_PER_REQUEST = 30


class DexScreenerProvider:
    """Reads market data from DEX Screener and normalizes it."""

    def __init__(self, settings: Settings, *, transport: Transport | None = None, **client_kwargs: Any) -> None:
        self.settings = settings
        self.client = HttpClient(
            name=PROVIDER_NAME,
            base_url=settings.dexscreener_base_url,
            rate_limit_per_min=settings.dexscreener_rate_limit_per_min,
            cache_ttl_seconds=settings.provider_cache_ttl_seconds,
            timeout_seconds=settings.http_timeout_seconds,
            max_retries=settings.http_max_retries,
            transport=transport,
            **client_kwargs,
        )

    # -- endpoints -------------------------------------------------------

    def search(self, query: str) -> list[MarketObservation]:
        """Free-text search across every chain DEX Screener covers.

        Returns only pairs on chains in our registry; everything else is
        dropped without comment.
        """
        payload = self.client.get_json("/latest/dex/search", {"q": query})
        return self._normalize_many(self._pairs_from(payload, context=f"search q={query!r}"))

    def pairs_for_token(self, chain_key: str, token_address: str) -> list[MarketObservation]:
        """Every pool trading one token on one chain.

        A memecoin often trades in several pools; the caller usually keeps the
        most liquid one, which `best_observation` picks.
        """
        spec = get_chain(chain_key)
        address = normalize_address(chain_key, token_address)
        payload = self.client.get_json(f"/tokens/v1/{spec.dexscreener_chain_id}/{address}")
        pairs = self._pairs_from(payload, context=f"token {chain_key}:{address}")
        if not pairs:
            raise ProviderNotFound(
                f"no pools found for {chain_key}:{address}", provider=PROVIDER_NAME
            )
        return self._normalize_many(pairs)

    def pairs_by_address(
        self, chain_key: str, pair_addresses: Sequence[str]
    ) -> list[MarketObservation]:
        """Look up specific pools, in batches of 30.

        This is the watchlist scanner's main call: one request covers 30
        tracked tokens instead of 30 separate requests.
        """
        spec = get_chain(chain_key)
        wanted = [a.strip() for a in pair_addresses if a and a.strip()]
        if not wanted:
            return []

        observations: list[MarketObservation] = []
        for start in range(0, len(wanted), MAX_ADDRESSES_PER_REQUEST):
            batch = wanted[start:start + MAX_ADDRESSES_PER_REQUEST]
            payload = self.client.get_json(
                f"/latest/dex/pairs/{spec.dexscreener_chain_id}/{','.join(batch)}"
            )
            observations.extend(
                self._normalize_many(
                    self._pairs_from(payload, context=f"pairs {chain_key} x{len(batch)}")
                )
            )
        return observations

    def latest_token_profiles(self) -> list[TokenRef]:
        """Recently profiled tokens - a discovery hint, not market data.

        Rate limited at 60/min rather than 300/min, so the discovery scanner
        calls this once per pass at most.
        """
        payload = self.client.get_json("/token-profiles/latest/v1")
        if not isinstance(payload, list):
            raise ProviderBadResponse(
                f"expected a list of profiles, got {type(payload).__name__}",
                provider=PROVIDER_NAME,
            )

        refs: list[TokenRef] = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            chain_key = chain_from_dexscreener(entry.get("chainId"))
            raw_address = entry.get("tokenAddress")
            if not chain_key or not raw_address:
                continue
            try:
                address = normalize_address(chain_key, str(raw_address))
            except InvalidAddressError:
                log.debug("skipping malformed address in token profiles",
                          extra={"provider": PROVIDER_NAME, "chain": chain_key})
                continue
            refs.append(TokenRef(chain=chain_key, address=address))
        return refs

    # -- parsing ---------------------------------------------------------

    @staticmethod
    def _pairs_from(payload: Any, *, context: str) -> list[dict[str, Any]]:
        """Pull the pair list out of a response.

        DEX Screener is inconsistent: `/latest/dex/*` wraps results in
        `{"pairs": [...]}` while `/tokens/v1/*` returns a bare list. Both are
        accepted. `{"pairs": null}` is a valid 'nothing found' answer.
        """
        if isinstance(payload, list):
            return [p for p in payload if isinstance(p, dict)]
        if isinstance(payload, dict):
            pairs = payload.get("pairs")
            if pairs is None:
                return []
            if isinstance(pairs, list):
                return [p for p in pairs if isinstance(p, dict)]
        raise ProviderBadResponse(
            f"unexpected response shape for {context}: {type(payload).__name__}",
            provider=PROVIDER_NAME,
        )

    def _normalize_many(self, pairs: Iterable[dict[str, Any]]) -> list[MarketObservation]:
        out: list[MarketObservation] = []
        for pair in pairs:
            observation = self._normalize_pair(pair)
            if observation is not None:
                out.append(observation)
        return out

    @staticmethod
    def _normalize_pair(pair: dict[str, Any]) -> MarketObservation | None:
        """Turn one raw pair into a `MarketObservation`, or None to skip it.

        Returns None (rather than raising) when the pair is on a chain we do
        not track or its address is malformed. Search results routinely
        contain both, and one bad entry must not abort a whole scan.
        """
        chain_key = chain_from_dexscreener(pair.get("chainId"))
        if chain_key is None:
            return None

        base = pair.get("baseToken")
        if not isinstance(base, dict):
            return None
        raw_address = base.get("address")
        if not raw_address:
            return None

        try:
            address = normalize_address(chain_key, str(raw_address))
        except InvalidAddressError:
            log.debug("skipping pair with malformed base token address",
                      extra={"provider": PROVIDER_NAME, "chain": chain_key})
            return None

        token = TokenRef(
            chain=chain_key,
            address=address,
            symbol=base.get("symbol") or None,
            name=base.get("name") or None,
        )

        # These sub-objects are absent on some pairs; default to {} so every
        # lookup below is safe without a pile of `if` statements.
        txns = pair.get("txns") if isinstance(pair.get("txns"), dict) else {}
        volume = pair.get("volume") if isinstance(pair.get("volume"), dict) else {}
        change = pair.get("priceChange") if isinstance(pair.get("priceChange"), dict) else {}
        liquidity = pair.get("liquidity") if isinstance(pair.get("liquidity"), dict) else {}
        quote = pair.get("quoteToken") if isinstance(pair.get("quoteToken"), dict) else {}

        def window(bucket: Any, key: str) -> Any:
            return bucket.get(key) if isinstance(bucket, dict) else None

        return MarketObservation(
            token=token,
            source=PROVIDER_NAME,
            captured_at=utcnow(),
            pool_address=pair.get("pairAddress") or None,
            dex_id=pair.get("dexId") or None,
            quote_symbol=quote.get("symbol") or None,
            pair_created_at=ms_to_datetime(pair.get("pairCreatedAt")),
            price_usd=to_float(pair.get("priceUsd")),
            price_native=to_float(pair.get("priceNative")),
            liquidity_usd=to_float(liquidity.get("usd")),
            fdv_usd=to_float(pair.get("fdv")),
            market_cap_usd=to_float(pair.get("marketCap")),
            volume_5m_usd=to_float(volume.get("m5")),
            volume_1h_usd=to_float(volume.get("h1")),
            volume_6h_usd=to_float(volume.get("h6")),
            volume_24h_usd=to_float(volume.get("h24")),
            price_change_5m=to_float(change.get("m5")),
            price_change_1h=to_float(change.get("h1")),
            price_change_6h=to_float(change.get("h6")),
            price_change_24h=to_float(change.get("h24")),
            buys_1h=to_int(window(txns.get("h1"), "buys")),
            sells_1h=to_int(window(txns.get("h1"), "sells")),
            buys_24h=to_int(window(txns.get("h24"), "buys")),
            sells_24h=to_int(window(txns.get("h24"), "sells")),
            holder_count=None,   # DEX Screener does not report holders
            source_url=pair.get("url") or None,
            raw=pair,
        )

    def stats(self) -> dict[str, Any]:
        return self.client.stats()


def best_observation(observations: Sequence[MarketObservation]) -> MarketObservation | None:
    """Pick the most trustworthy observation for a token from several pools.

    Deepest liquidity wins: a price from a $2,000 pool is easy to move and
    tells you little, while the same price from a $200,000 pool is far more
    meaningful. Pools with unknown liquidity rank last rather than being
    treated as zero, and completeness breaks ties.
    """
    if not observations:
        return None
    return max(
        observations,
        key=lambda o: (
            o.liquidity_usd is not None,
            o.liquidity_usd or 0.0,
            o.completeness(),
        ),
    )
