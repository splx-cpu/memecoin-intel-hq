"""The shape every analyzer produces, matching `research_reports`.

WHY FACTS, ASSUMPTIONS AND MISSING DATA ARE SEPARATE FIELDS
-----------------------------------------------------------
The project spec requires verified information to stay separable from
inference. Keeping them in one block of prose makes that impossible to
enforce - a reader cannot tell which sentence rests on a fetched number and
which is a guess.

Three lists keep the distinction structural rather than stylistic, so the
Telegram formatter can label each section honestly and a reader can judge
them differently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ResearchReport:
    """One analysis of one event. Maps onto the `research_reports` table."""

    category: str
    summary: str
    model: str
    prompt_version: str = "v1"

    headline: str | None = None
    what_changed: str | None = None

    # Claims backed by data we actually fetched, each naming its source.
    verified_facts: list[str] = field(default_factory=list)
    # Explicitly labelled inference. The template analyzer leaves this EMPTY,
    # because a template that invented an interpretation would be lying about
    # where it came from.
    assumptions: list[str] = field(default_factory=list)
    # What could not be checked. Honest absence, not silence.
    missing_data: list[str] = field(default_factory=list)
    # Observed risk indicators, with their evidence.
    risks: list[str] = field(default_factory=list)
    # Links a reader can use to verify every number above.
    sources: list[dict[str, str]] = field(default_factory=list)

    confidence: str | None = None

    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None
    raw_response: dict[str, Any] = field(default_factory=dict)

    @property
    def is_free(self) -> bool:
        return not self.cost_usd

    def to_row(self, token_id: int, alert_event_id: int | None) -> dict[str, Any]:
        return {
            "token_id": token_id,
            "alert_event_id": alert_event_id,
            "category": self.category,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "headline": self.headline,
            "summary": self.summary,
            "what_changed": self.what_changed,
            "verified_facts": self.verified_facts,
            "assumptions": self.assumptions,
            "missing_data": self.missing_data,
            "risks": self.risks,
            "sources": self.sources,
            "confidence": self.confidence,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
            "raw_response": self.raw_response,
        }


class Analyzer(Protocol):
    """Anything that can turn an event into a report."""

    name: str

    def analyze(
        self,
        *,
        event: dict[str, Any],
        token: dict[str, Any],
        risk_flags: list[dict[str, Any]],
    ) -> ResearchReport: ...
