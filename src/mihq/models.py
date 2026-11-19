"""The normalized shapes every provider must produce.

WHY NORMALIZE
-------------
DEX Screener gives us `priceUsd` (a string) and `volume.h24` (a number).
GeckoTerminal gives us `base_token_price_usd` and `volume_usd.h24`. If those
field names leaked into the scanner, adding a third provider would mean
touching every file that reads market data.

So each adapter converts its provider's response into `MarketObservation`
below, and nothing outside `mihq.providers` ever sees a raw payload.

THE MISSING-DATA RULE
---------------------
Every market metric is `float | None`. `None` means "the provider did not
give us this", and it is never silently turned into 0.0. The project spec
forbids fabricating data, and the difference matters: a token with zero
liquidity is a dead token, while a token with unknown liquidity is one we
cannot assess. Those must not look identical downstream.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    """Timezone-aware current time. Never use datetime.utcnow(), which is naive."""
    return datetime.now(timezone.utc)


def to_float(value: Any) -> float | None:
    """Best-effort number conversion that refuses to invent data.

    Providers are inconsistent: DEX Screener sends prices as strings
    ("0.001112") but volumes as numbers (119942.98). This accepts both and
    returns None for anything genuinely absent or unparseable.

    Note that False and True are rejected: a boolean where a price belongs
    means the payload changed shape, and silently reading True as 1.0 would
    hide that.

    >>> to_float("0.001112")
    0.001112
    >>> to_float(None) is None
    True
    >>> to_float("") is None
    True
    >>> to_float("N/A") is None
    True
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        # NaN and infinity are not storable in Postgres numeric columns.
        return result if result == result and abs(result) != float("inf") else None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            result = float(text)
        except ValueError:
            return None
        return result if result == result and abs(result) != float("inf") else None
    return None


def to_int(value: Any) -> int | None:
    """Same contract as `to_float`, for counts such as buys and sells."""
    result = to_float(value)
    if result is None:
        return None
    try:
        return int(result)
    except (ValueError, OverflowError):
        return None


def ms_to_datetime(value: Any) -> datetime | None:
    """Convert a millisecond epoch to an aware datetime, or None.

    DEX Screener sends `pairCreatedAt` as milliseconds since the epoch
    (for example 1786009343000). Values that are obviously not timestamps are
    rejected rather than producing a date in 1970 or the year 50000.
    """
    number = to_float(value)
    if number is None or number <= 0:
        return None
    seconds = number / 1000.0
    # Sanity window: 2009-01-01 (before Bitcoin) to 2100. Outside it, the
    # provider probably sent seconds instead of milliseconds, or garbage.
    if not (1_230_768_000 <= seconds <= 4_102_444_800):
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def iso_to_datetime(value: Any) -> datetime | None:
    """Parse an ISO-8601 string, as GeckoTerminal uses for `pool_created_at`."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class TokenRef:
    """Identifies one token on one chain.

    `address` is expected to be already normalized by
    `mihq.chains.normalize_address`, so it is safe to use as a database key.
    """

    chain: str
    address: str
    symbol: str | None = None
    name: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        """The pair the database uniquely indexes on."""
        return (self.chain, self.address)

    def __str__(self) -> str:
        label = self.symbol or self.name or "unknown"
        return f"{label} ({self.chain}:{self.address[:10]}...)"


@dataclass(frozen=True)
class MarketObservation:
    """One provider's view of one pool at one moment.

    Maps field-for-field onto the `market_snapshots` table. Every metric is
    optional because no provider returns all of them for every token.
    """

    token: TokenRef
    source: str                      # 'dexscreener' | 'geckoterminal'
    captured_at: datetime            # when WE fetched it

    pool_address: str | None = None
    dex_id: str | None = None
    quote_symbol: str | None = None

    provider_updated_at: datetime | None = None
    pair_created_at: datetime | None = None

    price_usd: float | None = None
    price_native: float | None = None
    liquidity_usd: float | None = None
    fdv_usd: float | None = None
    market_cap_usd: float | None = None

    volume_5m_usd: float | None = None
    volume_1h_usd: float | None = None
    volume_6h_usd: float | None = None
    volume_24h_usd: float | None = None

    price_change_5m: float | None = None
    price_change_1h: float | None = None
    price_change_6h: float | None = None
    price_change_24h: float | None = None

    buys_1h: int | None = None
    sells_1h: int | None = None
    buys_24h: int | None = None
    sells_24h: int | None = None

    holder_count: int | None = None

    # Links shown as sources in alerts, so a reader can verify every claim.
    source_url: str | None = None

    # The untouched provider payload, stored in market_snapshots.raw.
    raw: dict[str, Any] = field(default_factory=dict)

    # -- derived helpers, computed not stored ----------------------------

    @property
    def total_txns_24h(self) -> int | None:
        """Buys + sells over 24h, or None if either side is unknown."""
        if self.buys_24h is None or self.sells_24h is None:
            return None
        return self.buys_24h + self.sells_24h

    @property
    def volume_to_liquidity_24h(self) -> float | None:
        """24h volume divided by liquidity - a standard churn signal.

        A very high ratio can indicate wash trading, but it can equally mean
        genuine interest in a thin pool. It is an observation to investigate,
        never a conclusion, and the alert text must present it that way.
        """
        if self.volume_24h_usd is None or not self.liquidity_usd:
            return None
        return self.volume_24h_usd / self.liquidity_usd

    def completeness(self) -> float:
        """Fraction of core metrics the provider actually supplied (0.0-1.0).

        Used to choose between two providers that both answered, and to tell
        Claude how much of the picture is missing.
        """
        core = (
            self.price_usd, self.liquidity_usd, self.volume_24h_usd,
            self.price_change_24h, self.fdv_usd, self.buys_24h,
        )
        return sum(1 for value in core if value is not None) / len(core)

    def missing_fields(self) -> list[str]:
        """Names of the core metrics this observation lacks.

        Fed straight into `research_reports.missing_data`, so an alert can
        state plainly what could not be checked instead of staying silent.
        """
        core = {
            "price_usd": self.price_usd,
            "liquidity_usd": self.liquidity_usd,
            "volume_24h_usd": self.volume_24h_usd,
            "price_change_24h": self.price_change_24h,
            "fdv_usd": self.fdv_usd,
            "buys_24h": self.buys_24h,
            "holder_count": self.holder_count,
        }
        return sorted(name for name, value in core.items() if value is None)

    def to_snapshot_row(self, token_id: int) -> dict[str, Any]:
        """Shape this for an INSERT into `market_snapshots`."""
        return {
            "token_id": token_id,
            "chain": self.token.chain,
            "pool_address": self.pool_address,
            "source": self.source,
            "captured_at": self.captured_at,
            "provider_updated_at": self.provider_updated_at,
            "price_usd": self.price_usd,
            "price_native": self.price_native,
            "liquidity_usd": self.liquidity_usd,
            "fdv_usd": self.fdv_usd,
            "market_cap_usd": self.market_cap_usd,
            "volume_5m_usd": self.volume_5m_usd,
            "volume_1h_usd": self.volume_1h_usd,
            "volume_6h_usd": self.volume_6h_usd,
            "volume_24h_usd": self.volume_24h_usd,
            "price_change_5m": self.price_change_5m,
            "price_change_1h": self.price_change_1h,
            "price_change_6h": self.price_change_6h,
            "price_change_24h": self.price_change_24h,
            "buys_1h": self.buys_1h,
            "sells_1h": self.sells_1h,
            "buys_24h": self.buys_24h,
            "sells_24h": self.sells_24h,
            "holder_count": self.holder_count,
            "raw": self.raw,
        }

    def summary(self) -> str:
        """One readable line for logs. Unknown values say so rather than 0."""
        def show(value: float | None, prefix: str = "$") -> str:
            return f"{prefix}{value:,.2f}" if value is not None else "unknown"

        return (
            f"{self.token} via {self.source}: price={show(self.price_usd)} "
            f"liq={show(self.liquidity_usd)} vol24h={show(self.volume_24h_usd)}"
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
