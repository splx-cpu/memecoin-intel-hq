"""Turning a research report into a Telegram message.

ESCAPING IS A SECURITY CONCERN, NOT A COSMETIC ONE
--------------------------------------------------
Token symbols come from whoever deployed the contract. A token literally named
`<b>SAFE</b>` or `<a href="http://evil">` is trivial to create, and in HTML
parse mode it would either break the message or inject a link the reader
thinks came from us. EVERY piece of external text goes through `esc()`.

THE SECTIONS MUST STAY VISUALLY SEPARATE
----------------------------------------
Verified facts, risk indicators and "not checked" are different columns in the
database precisely so they can stay distinguishable. If the message blends
them into one block, that separation was pointless - the reader is back to
guessing which claims rest on fetched data. So each gets its own labelled
section with its own marker.
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from mihq.analysis.base import ResearchReport
from mihq.chains import get_chain

# Plain markers rather than a wall of emoji. Enough to scan, not so much that
# the message looks like a pump channel.
CATEGORY_HEADERS = {
    "momentum": "MOMENTUM",
    "new_coin": "NEW TOKEN",
    "risk": "RISK",
    "long_term": "DEVELOPMENT",
}

SEVERITY_MARKERS = {
    "critical": "!!!",
    "high": "!!",
    "medium": "!",
    "low": "-",
    "info": "-",
}


def esc(value: Any) -> str:
    """HTML-escape anything from outside our own code. Never skip this."""
    if value is None:
        return ""
    return html.escape(str(value), quote=False)


def local_time(moment: datetime | str | None, timezone: str) -> str:
    """Render a timestamp in the configured local zone.

    The summary and alerts are read by a person in one place, so UTC would
    make them do arithmetic. The zone is stated so there is no ambiguity.
    """
    if moment is None:
        return "unknown time"
    if isinstance(moment, str):
        try:
            text = moment.replace("Z", "+00:00")
            moment = datetime.fromisoformat(text)
        except ValueError:
            return esc(moment)
    try:
        local = moment.astimezone(ZoneInfo(timezone))
        return local.strftime("%Y-%m-%d %H:%M %Z")
    except Exception:  # noqa: BLE001 - a bad zone must not break a message
        return moment.strftime("%Y-%m-%d %H:%M UTC")


def _short_address(address: str) -> str:
    if len(address) <= 16:
        return address
    return f"{address[:8]}...{address[-6:]}"


def format_alert(
    *,
    report: ResearchReport,
    token: dict[str, Any],
    event: dict[str, Any],
    timezone: str = "America/New_York",
) -> str:
    """Build the message for one alert.

    Covers every field the project spec requires: name and ticker, chain,
    contract address, timestamp, market data, the reason it fired, the
    assessment, risk warnings, and source links.
    """
    category = event.get("category", "momentum")
    severity = str(event.get("severity", "info")).lower()
    header = CATEGORY_HEADERS.get(category, category.upper())
    marker = SEVERITY_MARKERS.get(severity, "-")

    symbol = esc(token.get("symbol") or "Unknown")
    name = esc(token.get("name") or "")
    chain_key = token.get("chain") or event.get("chain") or "unknown"
    address = str(token.get("token_address") or "")

    try:
        chain_label = esc(get_chain(chain_key).display_name)
    except Exception:  # noqa: BLE001
        chain_label = esc(chain_key)

    lines: list[str] = []

    # --- Header -------------------------------------------------------
    lines.append(f"<b>{marker} {header}</b>")
    title = f"<b>{symbol}</b>"
    if name and name.lower() != symbol.lower():
        title += f" - {name}"
    lines.append(title)
    lines.append(f"Chain: {chain_label}  |  Severity: {esc(severity)}")
    lines.append(f"Time: {local_time(event.get('triggered_at'), timezone)}")
    if address:
        lines.append(f"Contract: <code>{esc(address)}</code>")
    lines.append("")

    # --- Why it fired -------------------------------------------------
    if report.headline:
        lines.append(f"<b>What happened</b>")
        lines.append(esc(report.headline))
        lines.append("")

    if report.what_changed:
        lines.append(esc(report.what_changed))
        lines.append("")

    # --- Verified facts -----------------------------------------------
    if report.verified_facts:
        lines.append("<b>Verified data</b>  <i>(reported by the provider)</i>")
        for fact in report.verified_facts[:8]:
            lines.append(f"  - {esc(fact)}")
        lines.append("")

    # --- Assumptions, only if there are any ---------------------------
    # The template analyzer never produces these. Claude might, and when it
    # does they must be labelled as inference rather than fact.
    if report.assumptions:
        lines.append("<b>Interpretation</b>  <i>(inference, not fact)</i>")
        for item in report.assumptions[:5]:
            lines.append(f"  - {esc(item)}")
        lines.append("")

    # --- Risks --------------------------------------------------------
    if report.risks:
        lines.append("<b>Risk indicators</b>")
        for risk in report.risks[:6]:
            lines.append(f"  - {esc(risk)}")
        lines.append("")

    # --- What could not be checked ------------------------------------
    if report.missing_data:
        lines.append("<b>Not checked</b>")
        for item in report.missing_data[:4]:
            lines.append(f"  - {esc(item)}")
        lines.append("")

    # --- Sources ------------------------------------------------------
    if report.sources:
        lines.append("<b>Sources</b>")
        for source in report.sources[:4]:
            label = esc(source.get("label", "link"))
            url = source.get("url", "")
            if url.startswith(("http://", "https://")):
                lines.append(f'  - <a href="{html.escape(url, quote=True)}">{label}</a>')
        lines.append("")

    lines.append(
        "<i>Educational research only. Not investment advice, and not a "
        "recommendation to buy or sell anything.</i>"
    )

    return "\n".join(lines)


def format_daily_summary(
    *,
    stats: dict[str, Any],
    narrative: str | None,
    summary_date: str,
    timezone: str,
    highlights: list[dict[str, Any]] | None = None,
) -> str:
    """The once-a-day overview."""
    lines: list[str] = [
        f"<b>DAILY RESEARCH SUMMARY</b>",
        f"{esc(summary_date)} ({esc(timezone)})",
        "",
    ]

    if narrative:
        lines.extend([esc(narrative), ""])

    lines.append("<b>Scanner activity</b>")
    lines.append(f"  - New tokens discovered: {stats.get('new_tokens', 0)}")
    lines.append(f"  - Tokens observed: {stats.get('tokens_seen', 0)}")
    lines.append(f"  - Snapshots stored: {stats.get('snapshots_written', 0)}")
    lines.append(f"  - Watchlist size: {stats.get('watchlist_size', 0)}")
    lines.append("")

    alerts = stats.get("alerts") or {}
    if alerts:
        lines.append("<b>Alerts by category</b>")
        for category, count in sorted(alerts.items(), key=lambda kv: -kv[1]):
            label = CATEGORY_HEADERS.get(category, category)
            lines.append(f"  - {esc(label)}: {count}")
        lines.append("")

    if highlights:
        lines.append("<b>Notable</b>")
        for item in highlights[:5]:
            lines.append(f"  - {esc(item.get('headline', ''))}")
        lines.append("")

    # Health is part of the summary so silence is never mistaken for calm.
    health = stats.get("health") or {}
    lines.append("<b>System health</b>")
    runs = health.get("successful_runs")
    lines.append(f"  - Successful scanner runs: {runs if runs is not None else 'unknown'}")
    # Printed only when non-zero, because a permanent "Failed: 0" line is
    # one the eye learns to skip - and this is the line that most needs to
    # be noticed on the day it is not zero.
    if health.get("failed_runs"):
        lines.append(f"  - FAILED scanner runs: {health['failed_runs']}")
    if health.get("partial_runs"):
        lines.append(f"  - Partial runs: {health['partial_runs']}")
    errors = health.get("provider_errors", 0)
    lines.append(f"  - Provider errors: {errors}")
    if health.get("last_success"):
        lines.append(f"  - Last successful scan: {esc(health['last_success'])}")
    lines.append("")

    lines.append(
        "<i>Educational research only. Not investment advice.</i>"
    )
    return "\n".join(lines)


def format_system_alert(title: str, detail: str, *, severity: str = "high") -> str:
    """A health or error notice for the admin chat."""
    marker = SEVERITY_MARKERS.get(severity, "!")
    return (
        f"<b>{marker} SYSTEM: {esc(title)}</b>\n\n"
        f"{esc(detail)}\n\n"
        "<i>Sent by MemeCoin Intelligence HQ health monitoring.</i>"
    )


def format_alert_compact(
    *,
    report: ResearchReport,
    token: dict[str, Any],
    event: dict[str, Any],
    security: Any = None,
    timezone: str = "America/New_York",
) -> str:
    """A short alert: the decision-relevant facts and nothing else.

    Roughly a quarter the length of the full format. The full one is better
    for reading carefully; this one is better for a phone, where a long
    message gets skimmed and then ignored.

    What survives the cut: what moved and by how much, the numbers that give
    it scale, confirmed security findings, and what could NOT be checked.
    That last one stays because dropping it would turn silence into implied
    safety.
    """
    category = event.get("category", "momentum")
    severity = str(event.get("severity", "info")).lower()
    marker = SEVERITY_MARKERS.get(severity, "-")
    header = CATEGORY_HEADERS.get(category, category.upper())

    symbol = esc(token.get("symbol") or "Unknown")
    chain_key = token.get("chain") or event.get("chain") or "?"
    try:
        chain_label = esc(get_chain(chain_key).display_name)
    except Exception:  # noqa: BLE001
        chain_label = esc(chain_key)

    payload = event.get("payload") or {}

    def money(value: Any) -> str:
        number = _num(value)
        if number is None:
            return "?"
        if number >= 1_000_000:
            return f"${number / 1_000_000:.1f}M"
        if number >= 1_000:
            return f"${number / 1_000:.0f}K"
        return f"${number:,.0f}"

    lines = [f"<b>{marker} {header} · {symbol} · {chain_label}</b>"]

    # One line of scale: liquidity, volume, buy/sell split.
    scale = [
        f"{money(payload.get('liquidity_usd'))} liq",
        f"{money(payload.get('volume_24h_usd'))} vol",
    ]
    buys, sells = payload.get("buys_24h"), payload.get("sells_24h")
    if buys is not None and sells is not None:
        scale.append(f"{buys}/{sells} b/s")
    lines.append(" · ".join(scale))

    # What actually happened.
    if report.headline:
        lines.append(esc(report.headline))

    # Security, if we have it.
    if security is not None and getattr(security, "checked", False):
        critical = security.critical_findings()
        passed = security.passed_checks()
        unknown = security.unknown_checks()
        if critical:
            lines.append("<b>FLAGS:</b> " + esc("; ".join(critical[:3])))
        if passed:
            lines.append("OK: " + esc("; ".join(p.split(" - ")[0] for p in passed[:3])))
        if unknown:
            lines.append("<i>Unchecked: " + esc(", ".join(unknown)) + "</i>")
    elif report.missing_data:
        lines.append("<i>Security checks unavailable</i>")

    # One link is enough on a phone.
    for source in report.sources[:1]:
        url = source.get("url", "")
        if url.startswith(("http://", "https://")):
            lines.append(f'<a href="{html.escape(url, quote=True)}">'
                         f'{esc(source.get("label", "chart"))}</a>')

    lines.append("<i>Research only. Not advice.</i>")
    return "\n".join(lines)


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
