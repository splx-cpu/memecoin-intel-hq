"""Discovery scanner: finds newly created pools. Runs roughly every 5 minutes.

WHAT IT DOES, IN ORDER
----------------------
1. Open a `scanner_runs` row so the pass is visible even if it crashes.
2. For each enabled chain, ask GeckoTerminal for new pools.
3. Upsert every token it finds, recording whether it was genuinely new.
4. Ask the write policy whether the observation is worth storing.
5. Put genuinely new tokens on the watchlist so the one-minute scanner
   picks them up from now on.
6. Close the `scanner_runs` row with the counts.

FAILURE PHILOSOPHY
------------------
One chain failing must not abandon the other four. Provider errors are
counted per chain and the pass continues, finishing with status 'partial'
rather than 'failed'. A scanner that gives up entirely because Avalanche had
a bad minute is worse than one that reports what it managed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mihq.chains import enabled_chains
from mihq.config import Settings
from mihq.db.repositories import Repository
from mihq.events.detectors import Detector, default_detectors
from mihq.events.pipeline import process_observation
from mihq.logging_setup import get_logger
from mihq.models import MarketObservation
from mihq.providers.errors import ProviderError
from mihq.providers.geckoterminal import GeckoTerminalProvider
from mihq.providers.security import SecurityProvider
from mihq.scanners.policy import SnapshotPolicy
from mihq.scanners.retire import retire_quiet_entries

log = get_logger(__name__)

SCANNER_NAME = "discovery"


@dataclass
class DiscoveryResult:
    """What one discovery pass accomplished."""

    run_id: int | None = None
    status: str = "success"
    tokens_seen: int = 0
    new_tokens: int = 0
    snapshots_written: int = 0
    snapshots_skipped: int = 0
    watchlist_added: int = 0
    watchlist_retired: int = 0
    security_checked: int = 0
    events_created: int = 0
    events_suppressed: int = 0
    risk_flags_raised: int = 0
    provider_errors: int = 0
    database_errors: int = 0
    error_message: str | None = None
    per_chain: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"discovery {self.status}: {self.tokens_seen} seen, "
            f"{self.new_tokens} new, {self.snapshots_written} snapshots "
            f"({self.snapshots_skipped} skipped), "
            f"{self.watchlist_added} watchlisted, "
            f"{self.watchlist_retired} retired, "
            f"{self.events_created} events ({self.events_suppressed} duplicates), "
            f"{self.security_checked} security-checked, "
            f"{self.provider_errors} provider errors"
        )


class DiscoveryScanner:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        geckoterminal: GeckoTerminalProvider,
        *,
        policy: SnapshotPolicy | None = None,
        detectors: list[Detector] | None = None,
        security: SecurityProvider | None = None,
    ) -> None:
        self.settings = settings
        self.repo = repository
        self.gecko = geckoterminal
        self.policy = policy or SnapshotPolicy.from_settings(settings)
        self.detectors = detectors if detectors is not None else default_detectors(settings)
        if security is not None:
            self.security = security
        elif settings.security_checks_enabled:
            self.security = SecurityProvider(settings)
        else:
            self.security = None

    def run_once(self) -> DiscoveryResult:
        """One complete discovery pass. Never raises for provider problems."""
        result = DiscoveryResult()
        chains = enabled_chains(self.settings.enabled_chains)
        chain_keys = [c.key for c in chains]

        try:
            result.run_id = self.repo.start_run(SCANNER_NAME, chain_keys)
        except Exception as exc:  # noqa: BLE001
            # Without a run row we cannot record health, so this is fatal for
            # the pass - but it is still reported rather than crashing a
            # long-running worker.
            log.error("could not open a scanner run", extra={"error": str(exc)})
            result.status = "failed"
            result.database_errors += 1
            result.error_message = f"start_run failed: {exc}"
            return result

        for spec in chains:
            try:
                observations = self.gecko.new_pools(spec.key)
            except ProviderError as exc:
                result.provider_errors += 1
                result.status = "partial"
                result.per_chain[spec.key] = {"error": str(exc)}
                log.warning("discovery failed for a chain",
                            extra={"chain": spec.key, "error": str(exc)})
                continue

            seen = written = skipped = new = added = 0
            for observation in observations:
                seen += 1
                try:
                    outcome = self._store(observation)
                except Exception as exc:  # noqa: BLE001
                    result.database_errors += 1
                    result.status = "partial"
                    log.warning("could not store an observation",
                                extra={"chain": spec.key, "error": str(exc)})
                    continue
                new += outcome["new_token"]
                written += outcome["written"]
                skipped += outcome["skipped"]
                added += outcome["watchlisted"]
                result.events_created += outcome["events_created"]
                result.events_suppressed += outcome["events_suppressed"]
                result.risk_flags_raised += outcome["risk_flags_raised"]
                result.security_checked += outcome["security_checked"]

            result.tokens_seen += seen
            result.new_tokens += new
            result.snapshots_written += written
            result.snapshots_skipped += skipped
            result.watchlist_added += added
            result.per_chain[spec.key] = {
                "seen": seen, "new": new, "written": written,
                "skipped": skipped, "watchlisted": added,
            }

        # Every chain failing means the pass achieved nothing.
        if result.provider_errors >= len(chains) and result.tokens_seen == 0:
            result.status = "failed"
            result.error_message = "every chain failed"

        # Prune after adding, in the same pass: the loop that grows the
        # watchlist is the one that should keep it bounded. Housekeeping
        # never changes the pass's status - a failed retirement is logged,
        # not escalated, because it has no bearing on whether the scan
        # itself worked.
        retired = retire_quiet_entries(self.settings, self.repo)
        result.watchlist_retired = retired.total

        self._finish(result)
        log.info(result.summary(), extra={"run_id": result.run_id})
        return result

    def _store(self, observation: MarketObservation) -> dict[str, int]:
        """Persist one observation. Returns counters for the caller to total."""
        upsert = self.repo.upsert_token_from(observation)

        outcome = process_observation(
            repo=self.repo,
            settings=self.settings,
            policy=self.policy,
            detectors=self.detectors,
            token_id=upsert.token_id,
            observation=observation,
            is_new_token=upsert.was_inserted,
            security_provider=self.security,
        )
        written = 1 if outcome.snapshot_written else 0
        skipped = 0 if outcome.snapshot_written else 1

        # Watch it if it has enough liquidity to be worth the API calls -
        # NOT merely because we have not seen it before.
        #
        # Two deliberate changes from the original rule:
        #
        # 1. The floor. Every discovered pool used to be watched at any size,
        #    which is how the list reached 2,367 entries in two days. Most
        #    were dust that never did anything.
        # 2. No longer gated on `was_inserted`. That fires exactly once per
        #    token, so a pool that launches at $3K and grows to $40K could
        #    never be picked up afterwards. Now any pass that sees it above
        #    the floor can add it, and `add_to_watchlist` is idempotent.
        #
        # Unknown liquidity does not qualify. A token we cannot size is not
        # one we can justify spending every pass on - and if it matters, the
        # next pass with a real figure will add it.
        watchlisted = 0
        liquidity = observation.liquidity_usd
        if liquidity is not None and liquidity >= self.settings.watchlist_min_liquidity_usd:
            added = self.repo.add_to_watchlist(
                upsert.token_id,
                category="new_coin",
                reason=f"discovered via {observation.source}",
                priority=50,     # new tokens are checked before older ones
                readd_cooldown_hours=self.settings.watchlist_readd_cooldown_hours,
            )
            watchlisted = 1 if added else 0

        return {
            "new_token": 1 if upsert.was_inserted else 0,
            "written": written,
            "skipped": skipped,
            "watchlisted": watchlisted,
            "events_created": outcome.events_created,
            "events_suppressed": outcome.events_suppressed,
            "risk_flags_raised": outcome.risk_flags_raised,
            "security_checked": 1 if outcome.security_checked else 0,
        }

    def _finish(self, result: DiscoveryResult) -> None:
        if result.run_id is None:
            return
        try:
            self.repo.finish_run(
                result.run_id,
                status=result.status,
                tokens_seen=result.tokens_seen,
                new_tokens=result.new_tokens,
                snapshots_written=result.snapshots_written,
                events_created=result.events_created,
                events_suppressed=result.events_suppressed,
                provider_errors=result.provider_errors,
                database_errors=result.database_errors,
                error_message=result.error_message,
                details={
                    "per_chain": result.per_chain,
                    "snapshots_skipped": result.snapshots_skipped,
                    "watchlist_added": result.watchlist_added,
                    "risk_flags_raised": result.risk_flags_raised,
                    "security_checked": result.security_checked,
                    "provider_stats": self.gecko.stats(),
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.error("could not close the scanner run",
                      extra={"run_id": result.run_id, "error": str(exc)})
