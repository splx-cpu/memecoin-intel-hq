"""The free analyzer. Composes a report from structured data, inventing nothing.

WHAT IT CAN AND CANNOT DO
-------------------------
It CAN state every number the providers gave us, with the source and the
timestamp, say exactly what changed and by how much, list the risk flags with
their evidence, and name what could not be checked.

It CANNOT explain *why* something happened, compare against similar tokens, or
write flowing prose. Those need a language model.

That division is honest, and it means `assumptions` is always empty here. A
template producing interpretation would be presenting its author's guesses as
the system's analysis, which is exactly what the project's AI standards
forbid.

COST: zero. No network call, no API key, no rate limit.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mihq.chains import get_chain
from mihq.config import Settings
from mihq.events.risk import UNVERIFIABLE_WITH_CURRENT_PROVIDERS

MODEL_NAME = "template-v1"


def _fmt_money(value: Any) -> str:
    if value is None:
        return "not reported"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "not reported"
    if number >= 1_000_000:
        return f"${number / 1_000_000:,.2f}M"
    if number >= 1_000:
        return f"${number / 1_000:,.1f}K"
    if number >= 1:
        return f"${number:,.2f}"
    return f"${number:.10f}".rstrip("0").rstrip(".")


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "unknown"
    try:
        return f"{float(value):+.1f}%"
    except (TypeError, ValueError):
        return "unknown"


def _readable(field_name: str) -> str:
    return field_name.replace("_usd", " (USD)").replace("_", " ")


class TemplateAnalyzer:
    """Builds a report with no API call and no cost."""

    name = "template"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def analyze(
        self,
        *,
        event: dict[str, Any],
        token: dict[str, Any],
        risk_flags: list[dict[str, Any]] | None = None,
    ) -> "ResearchReport":
        from mihq.analysis.base import ResearchReport

        payload = event.get("payload") or {}
        category = event.get("category", "momentum")
        event_type = event.get("event_type", "unknown")
        symbol = token.get("symbol") or "Unknown token"
        chain_key = token.get("chain") or event.get("chain") or "unknown"
        address = token.get("token_address") or ""

        headline = payload.get("headline") or f"{symbol}: {_readable(event_type)}"

        verified = self._verified_facts(payload, event, symbol, chain_key)
        what_changed = self._what_changed(payload, event_type, symbol)
        risks = self._risks(risk_flags or [])
        missing = self._missing(payload)
        sources = self._sources(payload, chain_key, address)

        summary = self._summary(
            symbol=symbol, chain_key=chain_key, event_type=event_type,
            what_changed=what_changed, risk_count=len(risks),
            missing_count=len(missing),
        )

        return ResearchReport(
            category=category,
            summary=summary,
            model=MODEL_NAME,
            prompt_version="template-v1",
            headline=headline,
            what_changed=what_changed,
            verified_facts=verified,
            # Deliberately empty: a template has no basis for inference, and
            # pretending otherwise would blur the line this field exists to keep.
            assumptions=[],
            missing_data=missing,
            risks=risks,
            sources=sources,
            confidence=None,     # a template has no opinion to be confident about
            cost_usd=0.0,
            input_tokens=0,
            output_tokens=0,
        )

    # -- sections --------------------------------------------------------

    def _verified_facts(
        self, payload: dict[str, Any], event: dict[str, Any], symbol: str, chain: str
    ) -> list[str]:
        """Every claim here must trace to a number a provider gave us."""
        source = payload.get("source", "the data provider")
        captured = payload.get("captured_at", "")
        stamp = f" (via {source}{', ' + captured if captured else ''})"

        facts: list[str] = []

        if payload.get("price_usd") is not None:
            facts.append(f"Price: {_fmt_money(payload['price_usd'])}{stamp}")
        if payload.get("liquidity_usd") is not None:
            facts.append(f"Pool liquidity: {_fmt_money(payload['liquidity_usd'])}{stamp}")
        if payload.get("volume_24h_usd") is not None:
            facts.append(f"24h volume: {_fmt_money(payload['volume_24h_usd'])}{stamp}")

        buys, sells = payload.get("buys_24h"), payload.get("sells_24h")
        if buys is not None and sells is not None:
            facts.append(f"24h transactions: {buys:,} buys / {sells:,} sells{stamp}")

        if payload.get("before") is not None and payload.get("after") is not None:
            metric = _readable(str(payload.get("metric", "value")))
            facts.append(
                f"Measured change in {metric}: "
                f"{_fmt_money(payload['before'])} to {_fmt_money(payload['after'])} "
                f"({_fmt_pct(payload.get('change_pct'))})"
            )

        if payload.get("ratio") is not None:
            facts.append(
                f"24h volume was {payload['ratio']:.1f}x the pool's liquidity"
            )

        if payload.get("pair_created_at"):
            facts.append(f"Pool created: {payload['pair_created_at']} (provider-reported)")

        facts.append(f"Chain: {chain}")
        return facts

    def _what_changed(
        self, payload: dict[str, Any], event_type: str, symbol: str
    ) -> str:
        before, after = payload.get("before"), payload.get("after")
        metric = _readable(str(payload.get("metric", "a tracked metric")))

        if event_type == "first_seen":
            return (
                f"{symbol} was observed for the first time by this scanner. "
                "There is no earlier observation to compare against, so no "
                "trend can be described yet."
            )

        if event_type == "liquidity_unreported":
            return (
                f"The data provider previously reported "
                f"{_fmt_money(before)} of liquidity for this pool and is now "
                "not reporting liquidity at all. The reason is not known from "
                "this data alone."
            )

        if event_type == "high_churn":
            return (
                f"{symbol}'s 24h trading volume is "
                f"{payload.get('ratio', 0):.1f} times the size of its liquidity "
                "pool. This measurement describes trading activity relative to "
                "pool depth; it does not establish why."
            )

        if before is not None and after is not None:
            direction = "rose" if (payload.get("change_pct") or 0) > 0 else "fell"
            return (
                f"{symbol}'s {metric} {direction} from {_fmt_money(before)} to "
                f"{_fmt_money(after)}, a change of "
                f"{_fmt_pct(payload.get('change_pct'))}, between two consecutive "
                "observations."
            )

        return f"A {_readable(event_type)} condition was detected for {symbol}."

    def _risks(self, risk_flags: list[dict[str, Any]]) -> list[str]:
        risks: list[str] = []
        for flag in risk_flags:
            severity = str(flag.get("severity", "info")).upper()
            flag_type = str(flag.get("flag_type", "unknown"))
            if flag_type == "missing_verification":
                continue   # belongs in missing_data, not in risks
            detail = flag.get("detail", "")
            risks.append(f"[{severity}] {detail}")
        return risks

    def _missing(self, payload: dict[str, Any]) -> list[str]:
        missing: list[str] = []

        for field_name in payload.get("missing_fields") or []:
            missing.append(f"{_readable(str(field_name))} was not reported by the provider")

        missing.append(
            "The following could not be checked with the current data sources: "
            + ", ".join(c.replace("_", " ") for c in UNVERIFIABLE_WITH_CURRENT_PROVIDERS)
            + ". Their absence is not evidence that the token is safe."
        )
        missing.append(
            "This report is generated from market data only. No contract code, "
            "team, or project claim has been reviewed."
        )
        return missing

    def _sources(
        self, payload: dict[str, Any], chain_key: str, address: str
    ) -> list[dict[str, str]]:
        sources: list[dict[str, str]] = []

        if payload.get("source_url"):
            sources.append({"label": "DEX Screener pair", "url": payload["source_url"]})

        if address:
            try:
                spec = get_chain(chain_key)
                sources.append({"label": f"{spec.display_name} explorer",
                                "url": spec.token_explorer_link(address)})
                sources.append({"label": "DEX Screener token",
                                "url": spec.dexscreener_token_link(address)})
            except Exception:  # noqa: BLE001 - an unknown chain just means fewer links
                pass

        return sources

    def _summary(
        self, *, symbol: str, chain_key: str, event_type: str,
        what_changed: str, risk_count: int, missing_count: int,
    ) -> str:
        parts = [what_changed]

        if risk_count:
            parts.append(
                f"{risk_count} risk indicator{'s' if risk_count != 1 else ''} "
                "are currently flagged for this token, listed below with the "
                "measurements behind them."
            )
        else:
            parts.append(
                "No risk indicators are currently flagged by the checks this "
                "system can perform. That is not the same as the token being safe."
            )

        parts.append(
            "This is an automated description of market data for research and "
            "education. It is not investment advice and contains no buy or "
            "sell recommendation."
        )
        return " ".join(parts)
