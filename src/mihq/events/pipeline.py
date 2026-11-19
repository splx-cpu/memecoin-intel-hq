"""The per-token pipeline both scanners run.

Keeping this in one place matters because the ORDER of operations is subtle
and getting it wrong in one scanner but not the other would produce bugs that
only appear on some tokens.

THE ORDER, AND WHY
------------------
1. Read the previous snapshot.
2. Run the detectors.
3. Insert the events FIRST, with no snapshot attached yet. The UNIQUE
   constraint on event_key decides here which of them are genuinely new.
4. Decide whether to store the observation, using **newly created** events
   as the signal that it is evidence.
5. If stored, link the new events back to that snapshot.
6. Update risk flags, resolving any that no longer apply.

WHY EVENTS ARE INSERTED BEFORE THE STORAGE DECISION
---------------------------------------------------
The obvious order - decide storage from "did any detector fire" - has a
subtle flaw. State-based detectors such as churn fire on a condition being
true, not on a change, so they fire on EVERY pass while the condition holds.
Treating that as evidence would force a snapshot write every single minute
and quietly defeat the storage policy that keeps the database inside the
free tier.

Inserting first means the duplicate suppression has already happened, so
only a genuinely NEW event marks the observation as evidence. A repeat of
something already alerted does not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from mihq.config import Settings
from mihq.db.repositories import Repository
from mihq.events import risk
from mihq.events.detectors import (
    DetectedEvent,
    DetectionContext,
    Detector,
    run_detectors,
)
from mihq.logging_setup import get_logger
from mihq.models import MarketObservation
from mihq.providers.security import SecurityProvider, SecurityReport
from mihq.scanners.policy import SnapshotPolicy

log = get_logger(__name__)


@dataclass
class TokenOutcome:
    """What happened for one token in one pass."""

    token_id: int
    snapshot_written: bool = False
    snapshot_id: int | None = None
    events_created: int = 0
    events_suppressed: int = 0
    risk_flags_raised: int = 0
    risk_flags_resolved: int = 0
    security_checked: bool = False
    security: SecurityReport | None = None
    headlines: list[str] = field(default_factory=list)

    def merge_into(self, totals: dict[str, int]) -> None:
        totals["snapshots_written"] = totals.get("snapshots_written", 0) + int(
            self.snapshot_written
        )
        totals["snapshots_skipped"] = totals.get("snapshots_skipped", 0) + int(
            not self.snapshot_written
        )
        totals["events_created"] = totals.get("events_created", 0) + self.events_created
        totals["events_suppressed"] = (
            totals.get("events_suppressed", 0) + self.events_suppressed
        )
        totals["risk_flags_raised"] = (
            totals.get("risk_flags_raised", 0) + self.risk_flags_raised
        )
        totals["risk_flags_resolved"] = (
            totals.get("risk_flags_resolved", 0) + self.risk_flags_resolved
        )


def process_observation(
    *,
    repo: Repository,
    settings: Settings,
    policy: SnapshotPolicy,
    detectors: Sequence[Detector],
    token_id: int,
    observation: MarketObservation,
    is_new_token: bool = False,
    assess_risk: bool = True,
    security_provider: SecurityProvider | None = None,
) -> TokenOutcome:
    """Detect, store, and record risk for a single observation."""
    outcome = TokenOutcome(token_id=token_id)

    previous = repo.latest_snapshot(token_id)

    # 1. Detect.
    events = run_detectors(
        detectors,
        previous,
        observation,
        DetectionContext(
            is_new_token=is_new_token,
            token_id=token_id,
            previous_captured_at=previous.captured_at if previous else None,
        ),
    )

    # 2. Insert events first, with no snapshot yet. A None id means the
    #    event_key already existed - the duplicate guard working, not a
    #    failure. Only ids that come back are genuinely new.
    new_event_ids: list[int] = []
    for event in events:
        event_id = _record_event(repo, token_id, event, snapshot_id=None)
        if event_id is None:
            outcome.events_suppressed += 1
            log.debug("duplicate event suppressed",
                      extra={"event_key": event.event_key})
        else:
            new_event_ids.append(event_id)
            outcome.events_created += 1
            outcome.headlines.append(event.headline)
            log.info("event detected",
                     extra={"event_id": event_id, "severity": event.severity,
                            "headline": event.headline})

    # 3. Store. Only a genuinely NEW event makes this observation evidence;
    #    a repeat of something already alerted does not, which is what stops
    #    a persistently-true condition forcing a write every pass.
    decision = policy.decide(previous, observation, has_event=bool(new_event_ids))
    if decision.write:
        outcome.snapshot_id = repo.insert_snapshot(
            token_id, observation, include_raw=decision.include_raw
        )
        outcome.snapshot_written = True

        # 4. Link the new events to the evidence behind them.
        if outcome.snapshot_id is not None:
            for event_id in new_event_ids:
                try:
                    repo.link_event_snapshot(event_id, outcome.snapshot_id)
                except Exception as exc:  # noqa: BLE001
                    log.warning("could not link an event to its snapshot",
                                extra={"event_id": event_id, "error": str(exc)})

    # 5. Contract security, for newly discovered tokens only.
    #
    # Only on first sight because these facts rarely change and the call is
    # not free in rate-limit terms. A token whose mint authority is renounced
    # stays renounced; re-checking every minute would spend quota to learn
    # nothing.
    security: SecurityReport | None = None
    if security_provider is not None and is_new_token:
        security = security_provider.check(
            observation.token.chain, observation.token.address
        )
        outcome.security = security
        outcome.security_checked = security.checked

    # 6. Risk flags describe standing conditions, updated every pass.
    if assess_risk:
        raised, resolved = _sync_risk_flags(
            repo, settings, token_id, observation, security
        )
        outcome.risk_flags_raised = raised
        outcome.risk_flags_resolved = resolved

    return outcome


def _record_event(
    repo: Repository, token_id: int, event: DetectedEvent,
    snapshot_id: int | None = None,
) -> int | None:
    return repo.insert_alert_event(
        event_key=event.event_key,
        token_id=token_id,
        chain=event.payload.get("chain") or _chain_from_key(event.event_key),
        category=event.category,
        event_type=event.event_type,
        severity=event.severity,
        payload={**event.payload, "headline": event.headline},
        snapshot_id=snapshot_id,
    )


def _chain_from_key(event_key: str) -> str:
    """Second segment of `category:chain:address:type[:bucket]`."""
    parts = event_key.split(":")
    return parts[1] if len(parts) > 1 else "unknown"


def _sync_risk_flags(
    repo: Repository,
    settings: Settings,
    token_id: int,
    observation: MarketObservation,
    security: SecurityReport | None = None,
) -> tuple[int, int]:
    """Raise current risk conditions and resolve ones that no longer hold."""
    findings = risk.assess(observation, settings)
    if security is not None:
        findings.extend(risk.from_security(security))
    raised = 0

    for finding in findings:
        try:
            _, was_new = repo.upsert_risk_flag(
                token_id,
                flag_type=finding.flag_type,
                severity=finding.severity,
                detail=finding.detail,
                evidence=finding.evidence,
                source=finding.source,
            )
            if was_new:
                raised += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("could not record a risk flag",
                        extra={"flag": finding.flag_type, "error": str(exc)})

    resolved = 0
    try:
        current_types = [f.flag_type for f in findings]
        resolved_rows = repo.resolve_absent_risk_flags(token_id, current_types)
        resolved = len(resolved_rows)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not resolve stale risk flags",
                    extra={"token_id": token_id, "error": str(exc)})

    return raised, resolved
