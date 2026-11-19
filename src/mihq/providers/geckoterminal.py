"""GeckoTerminal adapter.

API: https://api.geckoterminal.com/api/v2  - free public tier, no key.
Rate limit: roughly 30 calls/minute. We stay below it.

WHY BOTH PROVIDERS
------------------
GeckoTerminal has the endpoint DEX Screener lacks: `/networks/{n}/new_pools`,
a proper feed of newly created pools. That is what the discovery scanner needs
to find tokens in the first place. DEX Screener is better for re-checking
known tokens in batches of 30.

Having two also gives a cross-check: when their numbers disagree sharply,
that disagreement is itself research-relevant, and the schema stores both
(`market_snapshots.source`) rather than picking a winner.

NETWORK IDS ARE NOT THE SAME AS DEX SCREENER'S
----------------------------------------------
Ethereum is `eth` here and `ethereum` there; Avalanche is `avax` here and
`avalanche` there. Both verified 2026-11-15. The mapping lives in
`mihq.chains`, never inline - getting it wrong returns an empty list rather
than an error, which is the worst kind of bug to chase.

RESPONSE SHAPE (JSON:API)
-------------------------
    {"data": [{"id": "...", "type": "pool",
               "attributes": {...},
               "relationships": {"base_token": {"data": {"id": "solana_EPjF..."}}}}],
     "included": [...]}

Token addresses hide inside relationship ids as "{network}_{address}", which
`_address_from_relationship` unpacks.
"""

from __future__ import annotations

from typing import Any, Iterable

from mihq.chains import (
    ChainSpec,
    InvalidAddressError,
    get_chain,
    normalize_address,
)
from mihq.config import Settings
from mihq.logging_setup import get_logger
from mihq.models import (
    MarketObservation,
    TokenRef,
    iso_to_datetime,
    to_float,
    to_int,
    utcnow,
)
from mihq.providers.errors import ProviderBadResponse, ProviderNotFound
from mihq.providers.http import HttpClient, Transport

log = get_logger(__name__)

PROVIDER_NAME = "geckoterminal"


class GeckoTerminalProvider:
    """Reads pool data from GeckoTerminal and normalizes it."""

    def __init__(self, settings: Settings, *, transport: Transport | None = None, **client_kwargs: Any) -> None:
        self.settings = settings
        self.client = HttpClient(
            name=PROVIDER_NAME,
            base_url=settings.geckoterminal_base_url,
            rate_limit_per_min=settings.geckoterminal_rate_limit_per_min,
            cache_ttl_seconds=settings.provider_cache_ttl_seconds,
            timeout_seconds=settings.http_timeout_seconds,
            max_retries=settings.http_max_retries,
            transport=transport,
            **client_kwargs,
        )

    # -- endpoints -------------------------------------------------------

    def new_pools(self, chain_key: str, *, page: int = 1) -> list[MarketObservation]:
        """Newly created pools on one chain. The discovery scanner's main feed."""
        spec = get_chain(chain_key)
        payload = self.client.get_json(
            f"/networks/{spec.geckoterminal_network}/new_pools", {"page": page}
        )
        return self._normalize_many(self._data_list(payload, context="new_pools"), spec)

    def trending_pools(self, chain_key: str, *, page: int = 1) -> list[MarketObservation]:
        """Pools GeckoTerminal currently ranks as trending on one chain."""
        spec = get_chain(chain_key)
        payload = self.client.get_json(
            f"/networks/{spec.geckoterminal_network}/trending_pools", {"page": page}
        )
        return self._normalize_many(self._data_list(payload, context="trending_pools"), spec)

    def pool(self, chain_key: str, pool_address: str) -> MarketObservation:
        """One specific pool. Raises `ProviderNotFound` if it is gone."""
        spec = get_chain(chain_key)
        payload = self.client.get_json(
            f"/networks/{spec.geckoterminal_network}/pools/{pool_address}"
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise ProviderNotFound(
                f"pool {chain_key}:{pool_address} not found", provider=PROVIDER_NAME
            )
        observation = self._normalize_pool(data, spec)
        if observation is None:
            raise ProviderBadResponse(
                f"could not parse pool {chain_key}:{pool_address}", provider=PROVIDER_NAME
            )
        return observation

    def token_pools(self, chain_key: str, token_address: str) -> list[MarketObservation]:
        """Every pool trading a given token on one chain."""
        spec = get_chain(chain_key)
        address = normalize_address(chain_key, token_address)
        payload = self.client.get_json(
            f"/networks/{spec.geckoterminal_network}/tokens/{address}/pools"
        )
        return self._normalize_many(self._data_list(payload, context="token_pools"), spec)

    # -- parsing ---------------------------------------------------------

    @staticmethod
    def _data_list(payload: Any, *, context: str) -> list[dict[str, Any]]:
        """Extract the `data` array from a JSON:API response."""
        if not isinstance(payload, dict):
            raise ProviderBadResponse(
                f"expected an object for {context}, got {type(payload).__name__}",
                provider=PROVIDER_NAME,
            )
        data = payload.get("data")
        if data is None:
            return []
        if isinstance(data, dict):      # a single-resource response
            return [data]
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        raise ProviderBadResponse(
            f"unexpected 'data' type for {context}: {type(data).__name__}",
            provider=PROVIDER_NAME,
        )

    @staticmethod
    def _address_from_relationship(relationships: Any, name: str, spec: ChainSpec) -> str | None:
        """Unpack a token address from a relationship id.

        Ids look like `solana_EPjFWdd5...` or `eth_0xa0b8...`: the network id,
        an underscore, then the address. We strip the known network prefix
        rather than splitting on the first underscore, because some network
        ids contain one themselves (`polygon_pos`).
        """
        if not isinstance(relationships, dict):
            return None
        node = relationships.get(name)
        if not isinstance(node, dict):
            return None
        data = node.get("data")
        if not isinstance(data, dict):
            return None
        raw_id = data.get("id")
        if not isinstance(raw_id, str) or not raw_id:
            return None

        prefix = f"{spec.geckoterminal_network}_"
        if raw_id.startswith(prefix):
            return raw_id[len(prefix):] or None
        # Unprefixed ids appear occasionally; take them as-is and let
        # normalize_address decide whether they are usable.
        return raw_id

    def _normalize_many(
        self, pools: Iterable[dict[str, Any]], spec: ChainSpec
    ) -> list[MarketObservation]:
        out: list[MarketObservation] = []
        for pool in pools:
            observation = self._normalize_pool(pool, spec)
            if observation is not None:
                out.append(observation)
        return out

    def _normalize_pool(self, pool: dict[str, Any], spec: ChainSpec) -> MarketObservation | None:
        """Turn one raw pool into a `MarketObservation`, or None to skip it."""
        attributes = pool.get("attributes")
        if not isinstance(attributes, dict):
            return None

        raw_address = self._address_from_relationship(
            pool.get("relationships"), "base_token", spec
        )
        if not raw_address:
            return None

        try:
            address = normalize_address(spec.key, raw_address)
        except InvalidAddressError:
            log.debug("skipping pool with malformed base token address",
                      extra={"provider": PROVIDER_NAME, "chain": spec.key})
            return None

        # Pool names look like "WIF / SOL". The base token symbol is the
        # left-hand side; absent or oddly formatted names simply yield None.
        name = attributes.get("name")
        symbol = None
        quote_symbol = None
        if isinstance(name, str) and "/" in name:
            left, _, right = name.partition("/")
            symbol = left.strip() or None
            quote_symbol = right.strip() or None

        token = TokenRef(chain=spec.key, address=address, symbol=symbol, name=None)

        volume = attributes.get("volume_usd") if isinstance(attributes.get("volume_usd"), dict) else {}
        change = (
            attributes.get("price_change_percentage")
            if isinstance(attributes.get("price_change_percentage"), dict)
            else {}
        )
        txns = attributes.get("transactions") if isinstance(attributes.get("transactions"), dict) else {}

        def window(bucket: Any, key: str) -> Any:
            return bucket.get(key) if isinstance(bucket, dict) else None

        dex_id = None
        relationships = pool.get("relationships")
        if isinstance(relationships, dict):
            dex_node = relationships.get("dex")
            if isinstance(dex_node, dict) and isinstance(dex_node.get("data"), dict):
                dex_id = dex_node["data"].get("id")

        pool_address = attributes.get("address") or None
        source_url = (
            f"https://www.geckoterminal.com/{spec.geckoterminal_network}/pools/{pool_address}"
            if pool_address
            else None
        )

        return MarketObservation(
            token=token,
            source=PROVIDER_NAME,
            captured_at=utcnow(),
            pool_address=pool_address,
            dex_id=dex_id,
            quote_symbol=quote_symbol,
            pair_created_at=iso_to_datetime(attributes.get("pool_created_at")),
            price_usd=to_float(attributes.get("base_token_price_usd")),
            price_native=to_float(attributes.get("base_token_price_native_currency")),
            # GeckoTerminal calls total pool liquidity "reserve_in_usd".
            liquidity_usd=to_float(attributes.get("reserve_in_usd")),
            fdv_usd=to_float(attributes.get("fdv_usd")),
            market_cap_usd=to_float(attributes.get("market_cap_usd")),
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
            holder_count=None,   # not reported on the free tier
            source_url=source_url,
            raw=pool,
        )

    def stats(self) -> dict[str, Any]:
        return self.client.stats()
