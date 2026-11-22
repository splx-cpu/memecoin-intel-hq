"""Taking tokens off the watchlist.

WHY THIS IS NEEDED
------------------
Discovery adds to the watchlist and, until now, nothing took anything off.
Two days of running reached 283 entries. Every entry costs an API call and a
database row on every pass, so an ever-growing list ends one of two ways: the
free tier fills, or the scanner takes so long per pass that it stops being
timely. Neither announces itself; both just slowly degrade.

WHY RETIREMENT IS NOT DELETION
------------------------------
A retired entry is `is_active = false` with `removed_at` set and a note
explaining why. The history stays, the research record stays, and because the
unique index covers only ACTIVE rows, discovery can re-add the same token
later if it comes back to life. Nothing here is irreversible.

THE RULE THAT IS NOT APPLIED, AND WHY
-------------------------------------
There is no rule that retires a token whose liquidity became UNKNOWN. That
looks like an obvious case - no data, stop watching - and it is exactly
backwards. Liquidity vanishing from a provider's feed is one of the strongest
signals the system has: it may be the pool being pulled. `liquidity_unreported`
exists precisely to alert on it. Retiring at that moment would drop the token
at the moment it became worth watching, and the SQL's NULL comparison is what
prevents it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mihq.config import Settings
from mihq.db.repositories import Repository
from mihq.events.detectors import FIRST_SEEN_EVENT_TYPE
from mihq.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class RetirementResult:
    low_liquidity: int = 0
    uneventful: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.low_liquidity + self.uneventful

    def summary(self) -> str:
        return (f"retired {self.total} watchlist entries "
                f"({self.low_liquidity} drained, {self.uneventful} uneventful)")


def retire_quiet_entries(settings: Settings, repo: Repository) -> RetirementResult:
    """Apply both retirement rules. Never raises.

    Run from the discovery pass rather than the watchlist pass: the loop that
    grows the list is the natural place to prune it, and discovery runs less
    often, which is plenty for a decision measured in hours.
    """
    result = RetirementResult()

    if not settings.watchlist_retire_enabled:
        return result

    note = (f"retired: liquidity below "
            f"${settings.watchlist_retire_below_liquidity_usd:,.0f}")
    try:
        rows = repo.retire_low_liquidity(
            below_usd=settings.watchlist_retire_below_liquidity_usd,
            grace_hours=settings.watchlist_retire_grace_hours,
            note=note,
        )
        result.low_liquidity = len(rows)
    except Exception as exc:  # noqa: BLE001 - housekeeping must never take
                              # down a scanner pass
        result.errors.append(f"low-liquidity retirement: {exc}")
        log.warning("low-liquidity retirement failed", extra={"error": str(exc)})

    note = (f"retired: no events in "
            f"{settings.watchlist_retire_uneventful_hours}h")
    try:
        rows = repo.retire_uneventful(
            older_than_hours=settings.watchlist_retire_uneventful_hours,
            note=note,
            arrival_event_type=FIRST_SEEN_EVENT_TYPE,
        )
        result.uneventful = len(rows)
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"uneventful retirement: {exc}")
        log.warning("uneventful retirement failed", extra={"error": str(exc)})

    if result.total:
        log.info(result.summary())
    return result
