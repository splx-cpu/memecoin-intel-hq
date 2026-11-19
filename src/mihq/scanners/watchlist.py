"""Watchlist scanner: re-checks tokens we already track. Runs every minute.

WHY IT BATCHES
--------------
DEX Screener accepts 30 pool addresses in a single request. Checking 120
tokens is therefore 4 requests, not 120 - the difference between comfortably
inside the rate limit and being blocked.

WHAT A "FAILURE" MEANS HERE
---------------------------
If a token comes back from the provider, it is marked scanned and its failure
counter resets. If it does not, the counter increments. After enough
consecutive misses the entry is deactivated, because a pool that has vanished
from the provider is usually gone for good - the liquidity was pulled. The
row is deactivated rather than deleted, so the research history survives;
that disappearance is itself a finding.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Sequence

from mihq.config import Settings
from mihq.db.repositories import Repository, WatchlistEntry
from mihq.events.detectors import Detector, default_detectors
from mihq.events.pipeline import process_observation
from mihq.logging_setup import get_logger
from mihq.models import MarketObservation
from mihq.providers.dexscreener import DexScreenerProvider, best_observation
from mihq.providers.errors import ProviderError
from mihq.scanners.policy import SnapshotPolicy

log = get_logger(__name__)

SCANNER_NAME = "watchlist"

# Consecutive misses before an entry is retired. At a one-minute cadence this
# is about 20 minutes of a pool being invisible to the provider.
DEFAULT_FAILURE_THRESHOLD = 20


@dataclass
class WatchlistResult:
    run_id: int | None = None
    status: str = "success"
    tokens_due: int = 0
    tokens_checked: int = 0
    tokens_missing: int = 0
    snapshots_written: int = 0
    snapshots_skipped: int = 0
    events_created: int = 0
    events_suppressed: int = 0
    risk_flags_raised: int = 0
    deactivated: int = 0
    provider_errors: int = 0
    database_errors: int = 0
    error_message: str | None = None
    per_chain: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"watchlist {self.status}: {self.tokens_checked}/{self.tokens_due} checked, "
            f"{self.snapshots_written} snapshots ({self.snapshots_skipped} skipped), "
            f"{self.events_created} events ({self.events_suppressed} duplicates), "
            f"{self.tokens_missing} missing, {self.deactivated} retired, "
            f"{self.provider_errors} provider errors"
        )


class WatchlistScanner:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        dexscreener: DexScreenerProvider,
        *,
        policy: SnapshotPolicy | None = None,
        detectors: list[Detector] | None = None,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
    ) -> None:
        self.settings = settings
        self.repo = repository
        self.dex = dexscreener
        self.policy = policy or SnapshotPolicy.from_settings(settings)
        self.detectors = detectors if detectors is not None else default_detectors(settings)
        self.failure_threshold = failure_threshold

    def run_once(self) -> WatchlistResult:
        result = WatchlistResult()

        try:
            result.run_id = self.repo.start_run(SCANNER_NAME, self.settings.enabled_chains)
        except Exception as exc:  # noqa: BLE001
            log.error("could not open a scanner run", extra={"error": str(exc)})
            result.status = "failed"
            result.database_errors += 1
            result.error_message = f"start_run failed: {exc}"
            return result

        try:
            due = self.repo.due_watchlist(
                default_interval_seconds=self.settings.watchlist_interval_seconds,
                limit=self.settings.watchlist_batch_size,
            )
        except Exception as exc:  # noqa: BLE001
            result.status = "failed"
            result.database_errors += 1
            result.error_message = f"due_watchlist failed: {exc}"
            self._finish(result)
            return result

        result.tokens_due = len(due)
        if not due:
            self._finish(result)
            return result

        by_chain: dict[str, list[WatchlistEntry]] = defaultdict(list)
        for entry in due:
            by_chain[entry.chain].append(entry)

        scanned_ids: list[int] = []
        failed_ids: list[int] = []

        for chain, entries in by_chain.items():
            try:
                observations = self._fetch_chain(chain, entries)
            except ProviderError as exc:
                result.provider_errors += 1
                result.status = "partial"
                result.per_chain[chain] = {"error": str(exc)}
                log.warning("watchlist fetch failed for a chain",
                            extra={"chain": chain, "error": str(exc)})
                # Deliberately NOT counted as a per-token failure: the
                # provider being down says nothing about these pools.
                continue

            # Index by token address so each entry can find its own result.
            found: dict[str, list[MarketObservation]] = defaultdict(list)
            for observation in observations:
                found[observation.token.address].append(observation)

            checked = missing = written = skipped = 0
            for entry in entries:
                candidates = found.get(entry.token_address, [])
                observation = best_observation(candidates)
                if observation is None:
                    missing += 1
                    failed_ids.append(entry.watchlist_id)
                    continue

                checked += 1
                scanned_ids.append(entry.watchlist_id)
                try:
                    outcome = self._store(entry, observation)
                    if outcome.snapshot_written:
                        written += 1
                    else:
                        skipped += 1
                    result.events_created += outcome.events_created
                    result.events_suppressed += outcome.events_suppressed
                    result.risk_flags_raised += outcome.risk_flags_raised
                except Exception as exc:  # noqa: BLE001
                    result.database_errors += 1
                    result.status = "partial"
                    log.warning("could not store a watchlist observation",
                                extra={"chain": chain, "token": entry.token_address,
                                       "error": str(exc)})

            result.tokens_checked += checked
            result.tokens_missing += missing
            result.snapshots_written += written
            result.snapshots_skipped += skipped
            result.per_chain[chain] = {
                "due": len(entries), "checked": checked, "missing": missing,
                "written": written, "skipped": skipped,
            }

        try:
            self.repo.mark_scanned(scanned_ids)
            self.repo.mark_scan_failed(failed_ids)
            retired = self.repo.deactivate_persistently_failing(
                threshold=self.failure_threshold
            )
            result.deactivated = len(retired)
            if retired:
                log.info("retired watchlist entries whose pools vanished",
                         extra={"count": len(retired)})
        except Exception as exc:  # noqa: BLE001
            result.database_errors += 1
            result.status = "partial"
            log.warning("could not update watchlist bookkeeping",
                        extra={"error": str(exc)})

        self._finish(result)
        log.info(result.summary(), extra={"run_id": result.run_id})
        return result

    def _fetch_chain(
        self, chain: str, entries: Sequence[WatchlistEntry]
    ) -> list[MarketObservation]:
        """Fetch every due token on one chain in as few requests as possible.

        Entries with a known pool address go through the batched 30-per-request
        endpoint. The rest fall back to a per-token lookup, which is why
        `primary_pool_address` is worth recording at discovery time.
        """
        pool_addresses = [e.primary_pool_address for e in entries if e.primary_pool_address]
        observations: list[MarketObservation] = []

        if pool_addresses:
            observations.extend(self.dex.pairs_by_address(chain, pool_addresses))

        for entry in entries:
            if entry.primary_pool_address:
                continue
            try:
                observations.extend(self.dex.pairs_for_token(chain, entry.token_address))
            except ProviderError as exc:
                # A single token's lookup failing is normal (its pool may be
                # gone); the entry simply ends up in `missing`.
                log.debug("per-token lookup failed",
                          extra={"chain": chain, "token": entry.token_address,
                                 "error": str(exc)})

        return observations

    def _store(self, entry: WatchlistEntry, observation: MarketObservation):
        """Run detection and storage for one watched token."""
        return process_observation(
            repo=self.repo,
            settings=self.settings,
            policy=self.policy,
            detectors=self.detectors,
            token_id=entry.token_id,
            observation=observation,
            is_new_token=False,   # by definition: it is already watched
        )

    def _finish(self, result: WatchlistResult) -> None:
        if result.run_id is None:
            return
        try:
            self.repo.finish_run(
                result.run_id,
                status=result.status,
                tokens_seen=result.tokens_checked,
                snapshots_written=result.snapshots_written,
                events_created=result.events_created,
                events_suppressed=result.events_suppressed,
                provider_errors=result.provider_errors,
                database_errors=result.database_errors,
                error_message=result.error_message,
                details={
                    "per_chain": result.per_chain,
                    "tokens_due": result.tokens_due,
                    "tokens_missing": result.tokens_missing,
                    "snapshots_skipped": result.snapshots_skipped,
                    "risk_flags_raised": result.risk_flags_raised,
                    "deactivated": result.deactivated,
                    "provider_stats": self.dex.stats(),
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.error("could not close the scanner run",
                      extra={"run_id": result.run_id, "error": str(exc)})
