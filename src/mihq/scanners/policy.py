"""Decides whether an observation is worth storing.

THE PROBLEM THIS SOLVES
-----------------------
The watchlist scanner runs every minute. If it wrote a row every time it
looked at a token, the arithmetic is brutal:

    100 tokens x 1 pass/minute x 1,440 minutes = 144,000 rows/day

With the provider payload in `raw` that is roughly 144 MB/day, so Supabase's
free 500 MB is gone in under four days. And almost all of those rows would be
identical to the one before, because most tokens do nothing most of the time.

THE RULE
--------
Write a snapshot when something actually CHANGED, plus a guaranteed heartbeat
so the history never has a gap:

  1. First observation of a token      -> always write
  2. Attached to an alert event        -> always write (it is evidence)
  3. Nothing written for N minutes     -> write (the heartbeat)
  4. A metric moved past its threshold -> write
  5. Otherwise                         -> skip

In a quiet market this removes 80-90% of writes. In a volatile one it keeps
everything that matters, which is exactly the right way round.

WHY "APPEARED" AND "DISAPPEARED" COUNT AS CHANGES
-------------------------------------------------
A metric going from a number to None, or the reverse, is treated as material
even though no percentage can be computed. A provider that stops reporting
liquidity for a token is telling us something - possibly that the pool was
pulled - and that moment must not be silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from mihq.config import Settings
from mihq.models import MarketObservation

# How the provider payload in `market_snapshots.raw` is handled.
RAW_MODES = frozenset({"events_only", "always", "never"})


@dataclass(frozen=True)
class SnapshotDecision:
    """Whether to write, why, and whether to keep the raw payload."""

    write: bool
    reason: str
    include_raw: bool = False

    def __bool__(self) -> bool:
        return self.write


def percent_change(previous: float | None, current: float | None) -> float | None:
    """Percent change from `previous` to `current`.

    Returns None when a percentage is meaningless:
      - either value is missing (the caller treats that as material separately)
      - the previous value was zero, so every change is infinite

    >>> percent_change(100.0, 110.0)
    10.0
    >>> percent_change(100.0, 50.0)
    -50.0
    >>> percent_change(0.0, 5.0) is None
    True
    >>> percent_change(None, 5.0) is None
    True
    """
    if previous is None or current is None:
        return None
    if previous == 0:
        return None
    return ((current - previous) / abs(previous)) * 100.0


class SnapshotPolicy:
    """Applies the write rule above. Pure logic, no database, easy to test."""

    def __init__(
        self,
        *,
        price_change_pct: float = 2.0,
        liquidity_change_pct: float = 5.0,
        volume_change_pct: float = 10.0,
        heartbeat_seconds: int = 900,
        raw_mode: str = "events_only",
    ) -> None:
        if raw_mode not in RAW_MODES:
            raise ValueError(f"raw_mode must be one of {sorted(RAW_MODES)}, got {raw_mode!r}")
        self.price_change_pct = abs(price_change_pct)
        self.liquidity_change_pct = abs(liquidity_change_pct)
        self.volume_change_pct = abs(volume_change_pct)
        self.heartbeat = timedelta(seconds=max(0, heartbeat_seconds))
        self.raw_mode = raw_mode

    @classmethod
    def from_settings(cls, settings: Settings) -> "SnapshotPolicy":
        return cls(
            price_change_pct=settings.snapshot_price_change_pct,
            liquidity_change_pct=settings.snapshot_liquidity_change_pct,
            volume_change_pct=settings.snapshot_volume_change_pct,
            heartbeat_seconds=settings.snapshot_heartbeat_seconds,
            raw_mode=settings.snapshot_raw_mode,
        )

    def _include_raw(self, *, has_event: bool) -> bool:
        """Whether to store the provider payload.

        `raw_mode="never"` wins over everything, including first observations
        and alert evidence. If someone has explicitly said never store the
        payload, that is a storage decision we must honour rather than
        quietly overriding because we judged this row important.
        """
        if self.raw_mode == "never":
            return False
        if self.raw_mode == "always":
            return True
        return has_event

    def _material_metric_change(
        self, previous: MarketObservation, current: MarketObservation
    ) -> str | None:
        """Name and size of the first threshold breach found, or None."""
        checks = (
            ("price", previous.price_usd, current.price_usd, self.price_change_pct),
            ("liquidity", previous.liquidity_usd, current.liquidity_usd,
             self.liquidity_change_pct),
            ("volume_24h", previous.volume_24h_usd, current.volume_24h_usd,
             self.volume_change_pct),
        )

        for name, before, after, threshold in checks:
            # A value appearing or disappearing is material, even though no
            # percentage can be computed for it.
            if (before is None) != (after is None):
                verb = "appeared" if before is None else "disappeared"
                return f"{name}_{verb}"

            change = percent_change(before, after)
            if change is None:
                # Both None (nothing to compare), or previous was exactly zero.
                # The zero case is handled below.
                if before == 0 and after not in (None, 0):
                    return f"{name}_moved_from_zero"
                continue

            if abs(change) >= threshold:
                return f"{name}_moved_{change:+.1f}pct"

        return None

    def decide(
        self,
        previous: MarketObservation | None,
        current: MarketObservation,
        *,
        has_event: bool = False,
        now: datetime | None = None,
    ) -> SnapshotDecision:
        """Should `current` be written to `market_snapshots`?

        `previous` is the most recent stored snapshot for this token, or None
        if we have never stored one. `has_event` is True when this observation
        triggered an alert, which makes it evidence worth keeping in full.
        """
        include_raw = self._include_raw(has_event=has_event)

        if previous is None:
            # A token's first sighting is worth keeping in full - it is the
            # only record of what it looked like on arrival.
            return SnapshotDecision(
                True, "first_observation", include_raw=self._include_raw(has_event=True)
            )

        if has_event:
            return SnapshotDecision(
                True, "alert_evidence", include_raw=self._include_raw(has_event=True)
            )

        # Heartbeat: guarantees the history has no gaps even in dead markets.
        reference = now or current.captured_at
        if self.heartbeat and previous.captured_at is not None:
            age = reference - previous.captured_at
            if age >= self.heartbeat:
                return SnapshotDecision(
                    True, f"heartbeat_{int(age.total_seconds())}s", include_raw=include_raw
                )

        change_reason = self._material_metric_change(previous, current)
        if change_reason is not None:
            return SnapshotDecision(True, change_reason, include_raw=include_raw)

        return SnapshotDecision(False, "no_material_change", include_raw=False)


def estimate_daily_rows(
    watchlist_size: int,
    *,
    interval_seconds: int,
    write_fraction: float,
) -> int:
    """Rows/day this policy would produce. Used in docs and capacity checks.

    `write_fraction` is the share of passes that actually write. Measure it
    from `scanner_runs` once the scanner has been running; 0.1-0.2 is typical
    for a quiet market with the default thresholds.

    >>> estimate_daily_rows(100, interval_seconds=60, write_fraction=1.0)
    144000
    >>> estimate_daily_rows(100, interval_seconds=60, write_fraction=0.15)
    21600
    """
    passes_per_day = 86_400 / max(1, interval_seconds)
    return int(watchlist_size * passes_per_day * write_fraction)
