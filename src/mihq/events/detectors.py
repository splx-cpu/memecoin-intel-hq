"""Rules that turn a pair of observations into a notification-worthy event.

WHAT A DETECTOR IS AND IS NOT
-----------------------------
A detector says "this measurable thing changed by this much". It does not say
whether that is good, whether to buy, or what will happen next. Every payload
carries the before and after numbers so the alert can show its working and the
reader can disagree with it.

NOISE FLOORS ARE THE WHOLE GAME
-------------------------------
A token with $200 of liquidity going up 400% is not news - it is two people
and a bot. Without minimum liquidity and volume floors, the alerts are
overwhelmingly dust, and a feed of dust is a feed nobody reads. Every detector
below checks the floors before it checks the thresholds.

SEVERITY
--------
Scaled by magnitude, not by how exciting it sounds. A 90% liquidity drain is
`critical` because the pool is effectively gone; a 35% one is `high`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, Sequence

from mihq.config import Settings
from mihq.db.repositories import StoredSnapshot
from mihq.events.keys import build_event_key
from mihq.logging_setup import get_logger
from mihq.models import MarketObservation

log = get_logger(__name__)


@dataclass(frozen=True)
class DetectionContext:
    """Extra facts a detector may need beyond the two observations."""

    is_new_token: bool = False
    token_id: int | None = None
    previous_captured_at: datetime | None = None


@dataclass(frozen=True)
class DetectedEvent:
    """One thing worth telling the user about."""

    category: str
    event_type: str
    severity: str
    event_key: str
    headline: str
    payload: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"[{self.severity}] {self.headline}"


class Detector(Protocol):
    name: str

    def detect(
        self,
        previous: StoredSnapshot | None,
        current: MarketObservation,
        context: DetectionContext,
    ) -> DetectedEvent | None: ...


def _pct(before: float | None, after: float | None) -> float | None:
    if before is None or after is None or before == 0:
        return None
    return ((after - before) / abs(before)) * 100.0


def _money(value: float | None) -> str:
    """Format for a headline. Says 'unknown' rather than inventing a zero."""
    if value is None:
        return "unknown"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    if value >= 1:
        return f"${value:,.2f}"
    return f"${value:.8f}".rstrip("0")


def _passes_floors(
    observation: MarketObservation, *, min_liquidity: float, min_volume: float
) -> bool:
    """Is this token substantial enough to be worth an alert?

    Unknown liquidity fails the floor. That is deliberate: we cannot confirm
    the token is substantial, and alerting on something we cannot assess is
    how a feed fills with noise.
    """
    if observation.liquidity_usd is None or observation.liquidity_usd < min_liquidity:
        return False
    if observation.volume_24h_usd is None or observation.volume_24h_usd < min_volume:
        return False
    return True


def _base_payload(
    previous: StoredSnapshot | None, current: MarketObservation
) -> dict[str, Any]:
    """Facts every event carries, all of them provider-reported, none derived."""
    return {
        "source": current.source,
        "captured_at": current.captured_at.isoformat(),
        "previous_captured_at": (
            previous.captured_at.isoformat() if previous and previous.captured_at else None
        ),
        "price_usd": current.price_usd,
        "liquidity_usd": current.liquidity_usd,
        "volume_24h_usd": current.volume_24h_usd,
        "buys_24h": current.buys_24h,
        "sells_24h": current.sells_24h,
        "source_url": current.source_url,
        "missing_fields": current.missing_fields(),
    }


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------

# The event type the arrival detector writes. Named here, once, because
# other modules need to EXCLUDE it: every watched token has one, so counting
# it as activity makes "this token has never done anything" always false.
# It is deliberately not the same string as the detector's `name`, and that
# discrepancy already caused one silent bug.
FIRST_SEEN_EVENT_TYPE = "first_seen"


class NewTokenDetector:
    """Fires once, the first time we ever see a token.

    `bucket_minutes=None` means the key has no time component, so this can
    never fire twice for the same token however many times it is rescanned.
    """

    name = "new_token"

    def __init__(self, *, min_liquidity: float = 1_000.0) -> None:
        # A lower floor than the others: a brand-new pool legitimately has
        # little volume yet, so judging it on volume would hide every new
        # token, which is the opposite of what discovery is for.
        self.min_liquidity = min_liquidity

    def detect(self, previous, current, context) -> DetectedEvent | None:
        if not context.is_new_token:
            return None
        if current.liquidity_usd is None or current.liquidity_usd < self.min_liquidity:
            return None

        payload = _base_payload(previous, current)
        payload["pair_created_at"] = (
            current.pair_created_at.isoformat() if current.pair_created_at else None
        )
        payload["pool_address"] = current.pool_address
        payload["dex_id"] = current.dex_id

        return DetectedEvent(
            category="new_coin",
            event_type=FIRST_SEEN_EVENT_TYPE,
            severity="info",
            event_key=build_event_key(
                category="new_coin", chain=current.token.chain,
                token_address=current.token.address,
                event_type=FIRST_SEEN_EVENT_TYPE,
                bucket_minutes=None,
            ),
            headline=(
                f"New token discovered: {current.token.symbol or 'unknown'} on "
                f"{current.token.chain} with {_money(current.liquidity_usd)} liquidity"
            ),
            payload=payload,
        )


class VolumeSpikeDetector:
    """24h volume jumped sharply between two observations."""

    name = "volume_spike"

    def __init__(
        self, *, threshold_pct: float = 150.0,
        min_liquidity: float = 5_000.0, min_volume: float = 10_000.0,
        bucket_minutes: int = 15,
    ) -> None:
        self.threshold_pct = threshold_pct
        self.min_liquidity = min_liquidity
        self.min_volume = min_volume
        self.bucket_minutes = bucket_minutes

    def detect(self, previous, current, context) -> DetectedEvent | None:
        if previous is None:
            return None
        if not _passes_floors(current, min_liquidity=self.min_liquidity,
                              min_volume=self.min_volume):
            return None

        change = _pct(previous.volume_24h_usd, current.volume_24h_usd)
        if change is None or change < self.threshold_pct:
            return None

        severity = "high" if change >= self.threshold_pct * 3 else "medium"
        payload = _base_payload(previous, current)
        payload.update({
            "metric": "volume_24h_usd",
            "before": previous.volume_24h_usd,
            "after": current.volume_24h_usd,
            "change_pct": round(change, 2),
            "threshold_pct": self.threshold_pct,
        })

        return DetectedEvent(
            category="momentum",
            event_type="volume_spike",
            severity=severity,
            event_key=build_event_key(
                category="momentum", chain=current.token.chain,
                token_address=current.token.address, event_type="volume_spike",
                moment=current.captured_at, bucket_minutes=self.bucket_minutes,
            ),
            headline=(
                f"{current.token.symbol or 'Token'} 24h volume up {change:+.0f}% "
                f"({_money(previous.volume_24h_usd)} to {_money(current.volume_24h_usd)})"
            ),
            payload=payload,
        )


class LiquidityDrainDetector:
    """Liquidity fell sharply - the single most important risk signal here.

    A pool losing most of its liquidity is what a rug pull looks like from the
    outside. The alert states the numbers and does not assert intent, because
    liquidity also moves for ordinary reasons (migration, a large LP exiting).
    """

    name = "liquidity_drain"

    def __init__(
        self, *, threshold_pct: float = 30.0, min_liquidity_before: float = 5_000.0,
        bucket_minutes: int = 10,
    ) -> None:
        self.threshold_pct = threshold_pct
        self.min_liquidity_before = min_liquidity_before
        self.bucket_minutes = bucket_minutes

    def detect(self, previous, current, context) -> DetectedEvent | None:
        if previous is None or previous.liquidity_usd is None:
            return None
        if previous.liquidity_usd < self.min_liquidity_before:
            return None

        # Liquidity disappearing entirely from the feed is the most severe
        # version of this, and no percentage can be computed for it.
        if current.liquidity_usd is None:
            payload = _base_payload(previous, current)
            payload.update({
                "metric": "liquidity_usd",
                "before": previous.liquidity_usd,
                "after": None,
                "note": "provider stopped reporting liquidity for this pool",
            })
            return DetectedEvent(
                category="risk", event_type="liquidity_unreported", severity="high",
                event_key=build_event_key(
                    category="risk", chain=current.token.chain,
                    token_address=current.token.address,
                    event_type="liquidity_unreported",
                    moment=current.captured_at, bucket_minutes=self.bucket_minutes,
                ),
                headline=(
                    f"{current.token.symbol or 'Token'} liquidity no longer reported "
                    f"(was {_money(previous.liquidity_usd)})"
                ),
                payload=payload,
            )

        change = _pct(previous.liquidity_usd, current.liquidity_usd)
        if change is None or change > -self.threshold_pct:
            return None

        drop = abs(change)
        severity = "critical" if drop >= 80 else "high" if drop >= 50 else "medium"

        payload = _base_payload(previous, current)
        payload.update({
            "metric": "liquidity_usd",
            "before": previous.liquidity_usd,
            "after": current.liquidity_usd,
            "change_pct": round(change, 2),
            "threshold_pct": -self.threshold_pct,
        })

        return DetectedEvent(
            category="risk",
            event_type="liquidity_drain",
            severity=severity,
            event_key=build_event_key(
                category="risk", chain=current.token.chain,
                token_address=current.token.address, event_type="liquidity_drain",
                moment=current.captured_at, bucket_minutes=self.bucket_minutes,
            ),
            headline=(
                f"{current.token.symbol or 'Token'} liquidity down {drop:.0f}% "
                f"({_money(previous.liquidity_usd)} to {_money(current.liquidity_usd)})"
            ),
            payload=payload,
        )


class PriceMoveDetector:
    """A large price move in either direction, between two observations."""

    name = "price_move"

    def __init__(
        self, *, surge_pct: float = 25.0, collapse_pct: float = 30.0,
        min_liquidity: float = 5_000.0, min_volume: float = 10_000.0,
        bucket_minutes: int = 15,
    ) -> None:
        self.surge_pct = surge_pct
        self.collapse_pct = collapse_pct
        self.min_liquidity = min_liquidity
        self.min_volume = min_volume
        self.bucket_minutes = bucket_minutes

    def detect(self, previous, current, context) -> DetectedEvent | None:
        if previous is None:
            return None
        if not _passes_floors(current, min_liquidity=self.min_liquidity,
                              min_volume=self.min_volume):
            return None

        change = _pct(previous.price_usd, current.price_usd)
        if change is None:
            return None

        if change >= self.surge_pct:
            category, event_type, direction = "momentum", "price_surge", "up"
            severity = "high" if change >= self.surge_pct * 3 else "medium"
        elif change <= -self.collapse_pct:
            category, event_type, direction = "risk", "price_collapse", "down"
            drop = abs(change)
            severity = "critical" if drop >= 80 else "high" if drop >= 50 else "medium"
        else:
            return None

        payload = _base_payload(previous, current)
        payload.update({
            "metric": "price_usd",
            "before": previous.price_usd,
            "after": current.price_usd,
            "change_pct": round(change, 2),
            "direction": direction,
        })

        return DetectedEvent(
            category=category,
            event_type=event_type,
            severity=severity,
            event_key=build_event_key(
                category=category, chain=current.token.chain,
                token_address=current.token.address, event_type=event_type,
                moment=current.captured_at, bucket_minutes=self.bucket_minutes,
            ),
            headline=(
                f"{current.token.symbol or 'Token'} price {direction} {abs(change):.0f}% "
                f"({_money(previous.price_usd)} to {_money(current.price_usd)})"
            ),
            payload=payload,
        )


class ChurnRatioDetector:
    """24h volume far exceeding pool liquidity.

    This CAN indicate artificial volume, but it is equally consistent with
    genuine interest in a thin pool. The headline says "unusual" and the
    payload carries the ratio; the research report must present it as
    something to investigate, never as a verdict.
    """

    name = "churn_ratio"

    def __init__(
        self, *, threshold: float = 10.0, min_liquidity: float = 5_000.0,
        bucket_minutes: int = 60,
    ) -> None:
        self.threshold = threshold
        self.min_liquidity = min_liquidity
        self.bucket_minutes = bucket_minutes

    def detect(self, previous, current, context) -> DetectedEvent | None:
        ratio = current.volume_to_liquidity_24h
        if ratio is None or ratio < self.threshold:
            return None
        if current.liquidity_usd is None or current.liquidity_usd < self.min_liquidity:
            return None

        severity = "high" if ratio >= self.threshold * 5 else "medium"
        payload = _base_payload(previous, current)
        payload.update({
            "metric": "volume_to_liquidity_24h",
            "ratio": round(ratio, 2),
            "threshold": self.threshold,
            "interpretation": (
                "Volume far exceeding pool size can indicate artificial trading, "
                "but is also consistent with genuine interest in a thin pool. "
                "This is an observation to investigate, not a conclusion."
            ),
        })

        return DetectedEvent(
            category="risk",
            event_type="high_churn",
            severity=severity,
            event_key=build_event_key(
                category="risk", chain=current.token.chain,
                token_address=current.token.address, event_type="high_churn",
                moment=current.captured_at, bucket_minutes=self.bucket_minutes,
            ),
            headline=(
                f"{current.token.symbol or 'Token'} traded {ratio:.1f}x its pool size "
                f"in 24h ({_money(current.volume_24h_usd)} on "
                f"{_money(current.liquidity_usd)})"
            ),
            payload=payload,
        )


# ---------------------------------------------------------------------------
# Running them all
# ---------------------------------------------------------------------------

def default_detectors(settings: Settings) -> list[Detector]:
    """The standard set, configured from .env."""
    return [
        # Its own setting, not a fraction of the general floor. Deriving it
        # meant that raising the general threshold to quiet the change
        # detectors ALSO raised this one, and lowering it to catch smaller
        # moves flooded the phone with new pools - two unrelated decisions
        # tied to one number.
        NewTokenDetector(min_liquidity=settings.event_new_token_min_liquidity_usd),
        LiquidityDrainDetector(
            threshold_pct=settings.event_liquidity_drain_pct,
            min_liquidity_before=settings.event_min_liquidity_usd,
        ),
        VolumeSpikeDetector(
            threshold_pct=settings.event_volume_spike_pct,
            min_liquidity=settings.event_min_liquidity_usd,
            min_volume=settings.event_min_volume_usd,
        ),
        PriceMoveDetector(
            surge_pct=settings.event_price_surge_pct,
            collapse_pct=settings.event_price_collapse_pct,
            min_liquidity=settings.event_min_liquidity_usd,
            min_volume=settings.event_min_volume_usd,
        ),
        ChurnRatioDetector(
            threshold=settings.event_churn_ratio,
            min_liquidity=settings.event_min_liquidity_usd,
        ),
    ]


def run_detectors(
    detectors: Sequence[Detector],
    previous: StoredSnapshot | None,
    current: MarketObservation,
    context: DetectionContext | None = None,
) -> list[DetectedEvent]:
    """Run every detector, collecting whatever fires.

    A detector raising is logged and skipped rather than aborting the rest:
    one buggy rule must not blind the whole system.
    """
    ctx = context or DetectionContext()
    events: list[DetectedEvent] = []
    for detector in detectors:
        try:
            event = detector.detect(previous, current, ctx)
        except Exception as exc:  # noqa: BLE001
            log.warning("detector raised",
                        extra={"detector": getattr(detector, "name", "?"),
                               "error": f"{type(exc).__name__}: {exc}"})
            continue
        if event is not None:
            events.append(event)
    return events
