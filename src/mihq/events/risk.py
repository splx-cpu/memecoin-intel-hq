"""Risk heuristics: standing conditions recorded in `risk_flags`.

HOW THIS DIFFERS FROM A DETECTOR
--------------------------------
A detector fires on a CHANGE between two moments ("liquidity fell 40%").
A risk flag describes a STATE that is either true or not right now ("this
pool has under $5,000 of liquidity"). States persist, so flags are raised
once and resolved when the condition clears, rather than re-alerting.

WHY source='heuristic' MATTERS
------------------------------
The project spec requires verified facts to stay separable from inferences.
Everything in this module is OUR derivation from provider numbers, not
something a provider asserted. Writing `source='heuristic'` into the row
keeps that distinction in the database, so a Telegram alert can honestly say
"our calculation" rather than implying DEX Screener declared it risky.

WHAT IS NOT HERE
----------------
Contract permissions, mint authority, LP lock status and holder distribution
are all listed in the project spec, and none of them are available from DEX
Screener or GeckoTerminal. They need a chain-specific source (Helius or
similar for Solana, an explorer API for EVM). Rather than guess at them,
`missing_verification` records that they could not be checked - which is
itself honest information for the reader.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from mihq.config import Settings
from mihq.models import MarketObservation

# Checks that are impossible with the current data sources. Recorded
# explicitly so an alert can state what was NOT verified.
UNVERIFIABLE_WITH_CURRENT_PROVIDERS = (
    "contract_mint_authority",
    "liquidity_lock_status",
    "holder_concentration",
    "contract_verified_source",
)


@dataclass(frozen=True)
class RiskFinding:
    """One risk indicator, ready to be written to `risk_flags`."""

    flag_type: str
    severity: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)
    source: str = "heuristic"

    def __str__(self) -> str:
        return f"[{self.severity}] {self.flag_type}: {self.detail}"


def assess(
    observation: MarketObservation, settings: Settings
) -> list[RiskFinding]:
    """Every risk condition currently true for this observation.

    Returns an empty list when nothing is flagged, which is a meaningful
    answer: it means the checks we CAN run all passed, not that the token is
    safe. `missing_verification` below keeps that distinction visible.
    """
    findings: list[RiskFinding] = []
    liquidity = observation.liquidity_usd
    volume = observation.volume_24h_usd

    # --- Thin liquidity -------------------------------------------------
    if liquidity is not None and liquidity < settings.event_min_liquidity_usd:
        severity = "high" if liquidity < settings.event_min_liquidity_usd / 5 else "medium"
        findings.append(RiskFinding(
            flag_type="low_liquidity",
            severity=severity,
            detail=(
                f"Pool liquidity is ${liquidity:,.0f}, below the "
                f"${settings.event_min_liquidity_usd:,.0f} threshold. Thin pools "
                "move sharply on small trades and can be difficult to exit."
            ),
            evidence={"liquidity_usd": liquidity,
                      "threshold_usd": settings.event_min_liquidity_usd},
        ))

    # --- Liquidity not reported at all ----------------------------------
    if liquidity is None:
        findings.append(RiskFinding(
            flag_type="liquidity_unknown",
            severity="medium",
            detail=(
                "The data provider did not report liquidity for this pool, so "
                "pool depth could not be assessed."
            ),
            evidence={"source": observation.source},
        ))

    # --- Churn ----------------------------------------------------------
    ratio = observation.volume_to_liquidity_24h
    if ratio is not None and ratio >= settings.event_churn_ratio:
        findings.append(RiskFinding(
            flag_type="high_churn",
            severity="high" if ratio >= settings.event_churn_ratio * 5 else "medium",
            detail=(
                f"24h volume is {ratio:.1f}x pool liquidity. This can indicate "
                "artificial trading activity, though it is also consistent with "
                "genuine interest in a small pool. Not a conclusion either way."
            ),
            evidence={"volume_to_liquidity_24h": round(ratio, 2),
                      "volume_24h_usd": volume, "liquidity_usd": liquidity,
                      "threshold": settings.event_churn_ratio},
        ))

    # --- One-sided trading ----------------------------------------------
    # Heavily lopsided buy/sell counts can mean a bot, or a pool where selling
    # is blocked. Both are worth a reader's attention.
    buys, sells = observation.buys_24h, observation.sells_24h
    if buys is not None and sells is not None and (buys + sells) >= 50:
        total = buys + sells
        buy_share = buys / total
        if buy_share >= 0.95:
            findings.append(RiskFinding(
                flag_type="sells_nearly_absent",
                severity="high",
                detail=(
                    f"{buys} buys against {sells} sells in 24h ({buy_share:.0%} buys). "
                    "Near-absent selling can indicate a contract that restricts "
                    "selling, or coordinated buying. Cause not determined."
                ),
                evidence={"buys_24h": buys, "sells_24h": sells,
                          "buy_share": round(buy_share, 4)},
            ))
        elif buy_share <= 0.05:
            findings.append(RiskFinding(
                flag_type="buys_nearly_absent",
                severity="medium",
                detail=(
                    f"{sells} sells against {buys} buys in 24h. Sustained one-way "
                    "selling often accompanies a pool being wound down."
                ),
                evidence={"buys_24h": buys, "sells_24h": sells,
                          "buy_share": round(buy_share, 4)},
            ))

    # --- Very little data at all ----------------------------------------
    missing = observation.missing_fields()
    if observation.completeness() <= 0.34:
        findings.append(RiskFinding(
            flag_type="sparse_data",
            severity="medium",
            detail=(
                "The provider returned very little data for this token "
                f"({len(missing)} core fields missing), so most checks could "
                "not be performed."
            ),
            evidence={"missing_fields": missing,
                      "completeness": round(observation.completeness(), 2)},
        ))

    # --- What we structurally cannot check ------------------------------
    # NOTE: with SECURITY_CHECKS_ENABLED most of this list IS now checked for
    # newly discovered tokens; see `from_security`. This flag covers the case
    # where the security provider had no data, which is common for pools only
    # minutes old.
    findings.append(RiskFinding(
        flag_type="missing_verification",
        severity="info",
        detail=(
            "These checks could not be performed with the current data sources: "
            + ", ".join(UNVERIFIABLE_WITH_CURRENT_PROVIDERS).replace("_", " ")
            + ". Their absence is not evidence of safety."
        ),
        evidence={"unverifiable": list(UNVERIFIABLE_WITH_CURRENT_PROVIDERS),
                  "providers_used": [observation.source]},
    ))

    return findings


def from_security(security: Any) -> list[RiskFinding]:
    """Turn a contract security report into risk flags.

    IMPORTANT: these are stamped `source="goplus"`, NOT "heuristic". They are
    facts a security provider asserted about the contract, not derivations of
    ours, and the alert says so. Blurring the two would misrepresent how much
    weight each deserves.
    """
    if security is None or not getattr(security, "checked", False):
        return []

    findings: list[RiskFinding] = []

    if security.is_honeypot is True:
        findings.append(RiskFinding(
            flag_type="honeypot",
            severity="critical",
            detail=("The contract appears to prevent selling. Buying is "
                    "possible but selling may not be."),
            evidence={"is_honeypot": True},
            source="goplus",
        ))

    if security.is_mintable is True:
        findings.append(RiskFinding(
            flag_type="mint_authority_active",
            severity="high",
            detail=("Mint authority has not been renounced. The creator can "
                    "increase supply, diluting existing holders."),
            evidence={"is_mintable": True},
            source="goplus",
        ))

    if security.is_freezable is True:
        findings.append(RiskFinding(
            flag_type="freeze_authority_active",
            severity="high",
            detail=("Freeze authority is active. Token accounts can be frozen, "
                    "preventing transfers."),
            evidence={"is_freezable": True},
            source="goplus",
        ))

    if security.transfer_pausable is True:
        findings.append(RiskFinding(
            flag_type="transfers_pausable",
            severity="high",
            detail="Transfers can be paused by the contract owner.",
            evidence={"transfer_pausable": True},
            source="goplus",
        ))

    if security.can_take_back_ownership is True:
        findings.append(RiskFinding(
            flag_type="ownership_reclaimable",
            severity="high",
            detail=("Ownership can be reclaimed after being renounced, so a "
                    "renouncement here does not mean what it appears to."),
            evidence={"can_take_back_ownership": True},
            source="goplus",
        ))

    if security.hidden_owner is True:
        findings.append(RiskFinding(
            flag_type="hidden_owner",
            severity="high",
            detail="The contract has a hidden owner address.",
            evidence={"hidden_owner": True},
            source="goplus",
        ))

    top = security.top_holder_percent
    if top is not None and top >= 50:
        findings.append(RiskFinding(
            flag_type="holder_concentration",
            severity="critical" if top >= 80 else "high",
            detail=(f"The largest holder controls {top:.0f}% of supply. A single "
                    "sale of that size would move the price sharply."),
            evidence={"top_holder_percent": round(top, 2)},
            source="goplus",
        ))

    sell_tax = security.sell_tax
    if sell_tax is not None and sell_tax >= 10:
        findings.append(RiskFinding(
            flag_type="high_sell_tax",
            severity="critical" if sell_tax >= 30 else "high",
            detail=f"Selling incurs a {sell_tax:.0f}% tax taken by the contract.",
            evidence={"sell_tax_percent": round(sell_tax, 2)},
            source="goplus",
        ))

    lp_locked = security.lp_locked_percent
    if lp_locked is not None and lp_locked < 50:
        findings.append(RiskFinding(
            flag_type="liquidity_unlocked",
            severity="high",
            detail=(f"Only {lp_locked:.0f}% of liquidity appears locked. Unlocked "
                    "liquidity can be withdrawn by whoever holds it."),
            evidence={"lp_locked_percent": round(lp_locked, 2)},
            source="goplus",
        ))

    if security.is_open_source is False:
        findings.append(RiskFinding(
            flag_type="unverified_contract",
            severity="medium",
            detail=("The contract source is not verified, so its behaviour "
                    "cannot be inspected."),
            evidence={"is_open_source": False},
            source="goplus",
        ))

    return findings


def highest_severity(findings: Sequence[RiskFinding]) -> str:
    """The most serious severity present, or 'info' for none."""
    order = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
    if not findings:
        return "info"
    return max(findings, key=lambda f: order.get(f.severity, 0)).severity


def actionable(findings: Sequence[RiskFinding]) -> list[RiskFinding]:
    """Findings worth surfacing in an alert, i.e. everything above 'info'.

    `missing_verification` is deliberately 'info': it belongs in the report's
    "what we could not check" section, not as a warning in its own right.
    """
    return [f for f in findings if f.severity != "info"]
