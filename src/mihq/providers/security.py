"""Contract security checks - the things that actually predict rug pulls.

WHY THIS EXISTS
---------------
Price, volume and liquidity say nothing about whether a contract can mint
infinite supply, whether the LP is locked, or whether you can sell at all.
A token can look flawless on every market metric and be unsellable by design.

The four checks that matter:

  HONEYPOT        Can you sell it, or does the contract only permit buying?
  MINT AUTHORITY  Can the creator print more supply and dilute you to zero?
  LP LOCK         Is the liquidity locked, or withdrawable at will? An
                  unlocked LP is the rug-pull mechanism itself.
  CONCENTRATION   Does one wallet hold most of the supply? One sell ends it.

SOURCE
------
GoPlus Security, free and keyless. EVM chains use
`/api/v1/token_security/{chain_id}`; Solana has its own endpoint with
different field names. Both are normalized into `SecurityReport` below.

Field names verified against a live API response on 2026-11-18.

"UNKNOWN" IS NOT "SAFE"
-----------------------
Every field is optional, and a missing value stays None. A token the API has
never analysed must never read as having passed - that inversion is exactly
the mistake that gets people rugged, so `SecurityReport.summary_flags()`
reports unknowns explicitly rather than omitting them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mihq.chains import get_chain
from mihq.config import Settings
from mihq.logging_setup import get_logger
from mihq.models import to_float, to_int
from mihq.providers.errors import ProviderBadResponse, ProviderError
from mihq.providers.http import HttpClient, Transport

log = get_logger(__name__)

PROVIDER_NAME = "goplus"
GOPLUS_BASE_URL = "https://api.gopluslabs.io/api/v1"

# Conservative: GoPlus does not publish a hard keyless limit, and we only
# need one call per newly discovered token.
GOPLUS_RATE_LIMIT_PER_MIN = 20


def _flag(value: Any) -> bool | None:
    """GoPlus returns "1"/"0" strings. Anything else means 'not assessed'.

    Returning None rather than False for an absent field is the whole point:
    "we did not check" and "we checked and it is fine" are different answers.
    """
    if value is None:
        return None
    text = str(value).strip()
    if text == "1":
        return True
    if text == "0":
        return False
    return None


def _pct(value: Any) -> float | None:
    """GoPlus percentages arrive as fractional strings ('0.67' = 67%)."""
    number = to_float(value)
    if number is None:
        return None
    return number * 100.0 if number <= 1.0 else number


@dataclass
class SecurityReport:
    """Normalized security facts for one token. None means 'not determined'."""

    chain: str
    address: str
    source: str = PROVIDER_NAME
    checked: bool = False

    # --- Can you sell it? ---
    is_honeypot: bool | None = None
    cannot_buy: bool | None = None
    buy_tax: float | None = None
    sell_tax: float | None = None

    # --- Can supply change under you? ---
    is_mintable: bool | None = None
    is_freezable: bool | None = None          # Solana
    balance_mutable: bool | None = None       # Solana
    transfer_pausable: bool | None = None
    can_take_back_ownership: bool | None = None
    hidden_owner: bool | None = None

    # --- Who holds it ---
    holder_count: int | None = None
    top_holder_percent: float | None = None
    top10_percent: float | None = None
    owner_percent: float | None = None

    # --- Liquidity ---
    lp_holder_count: int | None = None
    lp_locked_percent: float | None = None

    # --- Contract ---
    is_open_source: bool | None = None
    is_proxy: bool | None = None

    raw: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    # -- derived -------------------------------------------------------

    @property
    def sellable(self) -> bool | None:
        """True only if we positively confirmed it is not a honeypot."""
        if self.is_honeypot is None:
            return None
        return not self.is_honeypot

    @property
    def mint_renounced(self) -> bool | None:
        if self.is_mintable is None:
            return None
        return not self.is_mintable

    def critical_findings(self) -> list[str]:
        """Conditions severe enough to lead an alert. Confirmed only."""
        found: list[str] = []
        if self.is_honeypot is True:
            found.append("HONEYPOT: the contract appears to block selling")
        if self.cannot_buy is True:
            found.append("Buying appears to be blocked by the contract")
        if self.is_mintable is True:
            found.append("Mint authority is ACTIVE - supply can be increased")
        if self.is_freezable is True:
            found.append("Freeze authority is ACTIVE - your account can be frozen")
        if self.can_take_back_ownership is True:
            found.append("Ownership can be reclaimed after being renounced")
        if self.hidden_owner is True:
            found.append("Contract has a hidden owner")
        if self.transfer_pausable is True:
            found.append("Transfers can be paused by the contract owner")
        if self.top_holder_percent is not None and self.top_holder_percent >= 50:
            found.append(
                f"Top holder controls {self.top_holder_percent:.0f}% of supply"
            )
        if self.sell_tax is not None and self.sell_tax >= 15:
            found.append(f"Sell tax is {self.sell_tax:.0f}%")
        return found

    def passed_checks(self) -> list[str]:
        """Checks that positively came back clean. Never includes unknowns."""
        passed: list[str] = []
        if self.is_honeypot is False:
            passed.append("Not a honeypot - selling appears possible")
        if self.is_mintable is False:
            passed.append("Mint authority renounced - supply is fixed")
        if self.is_freezable is False:
            passed.append("Freeze authority renounced")
        if self.is_open_source is True:
            passed.append("Contract source is verified")
        if self.lp_locked_percent is not None and self.lp_locked_percent >= 50:
            passed.append(f"{self.lp_locked_percent:.0f}% of liquidity is locked")
        return passed

    def unknown_checks(self) -> list[str]:
        """What we could NOT determine. Stated, never silently omitted."""
        unknown: list[str] = []
        if self.is_honeypot is None:
            unknown.append("honeypot / sellability")
        if self.is_mintable is None:
            unknown.append("mint authority")
        if self.lp_locked_percent is None:
            unknown.append("liquidity lock")
        if self.top_holder_percent is None:
            unknown.append("holder concentration")
        return unknown

    def risk_score(self) -> tuple[int, int]:
        """(failed, checked) - a plain count, deliberately not a rating.

        A score out of 100 would invite reading it as a quality grade. It is
        not one: it counts mechanical red flags, and a token with zero flags
        can still be worthless.
        """
        checks = [
            self.is_honeypot, self.is_mintable, self.is_freezable,
            self.transfer_pausable, self.can_take_back_ownership,
            self.hidden_owner, self.cannot_buy,
        ]
        performed = [c for c in checks if c is not None]
        failed = [c for c in performed if c is True]
        concentration_known = self.top_holder_percent is not None
        if concentration_known:
            performed.append(False)
            if self.top_holder_percent >= 50:
                failed.append(True)
        return (len(failed), len(performed))


class SecurityProvider:
    """Fetches and normalizes contract security data."""

    def __init__(
        self, settings: Settings, *, transport: Transport | None = None, **client_kwargs: Any
    ) -> None:
        self.settings = settings
        self.client = HttpClient(
            name=PROVIDER_NAME,
            base_url=GOPLUS_BASE_URL,
            rate_limit_per_min=GOPLUS_RATE_LIMIT_PER_MIN,
            # Security facts change rarely; a longer cache saves calls.
            cache_ttl_seconds=max(300.0, settings.provider_cache_ttl_seconds),
            timeout_seconds=settings.http_timeout_seconds,
            max_retries=settings.http_max_retries,
            transport=transport,
            **client_kwargs,
        )

    def check(self, chain_key: str, address: str) -> SecurityReport:
        """Security report for one token. Never raises.

        A provider failure returns a report with `checked=False`, which
        downstream code must treat as "unknown", not "clean".
        """
        report = SecurityReport(chain=chain_key, address=address)
        try:
            spec = get_chain(chain_key)
        except Exception as exc:  # noqa: BLE001
            report.error = str(exc)
            return report

        try:
            if spec.goplus_chain_id is None:
                payload = self.client.get_json(
                    "/solana/token_security", {"contract_addresses": address}
                )
                self._fill_solana(report, payload, address)
            else:
                payload = self.client.get_json(
                    f"/token_security/{spec.goplus_chain_id}",
                    {"contract_addresses": address.lower()},
                )
                self._fill_evm(report, payload, address)
        except ProviderError as exc:
            report.error = str(exc)
            log.debug("security check failed",
                      extra={"chain": chain_key, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - never break a scan for this
            report.error = f"{type(exc).__name__}: {exc}"
            log.warning("unexpected error during security check",
                        extra={"chain": chain_key, "error": report.error})

        return report

    @staticmethod
    def _result_for(payload: Any, address: str) -> dict[str, Any] | None:
        """GoPlus keys its result by the address, in unpredictable case."""
        if not isinstance(payload, dict):
            raise ProviderBadResponse(
                f"expected an object, got {type(payload).__name__}",
                provider=PROVIDER_NAME,
            )
        result = payload.get("result")
        if not isinstance(result, dict) or not result:
            return None
        for key, value in result.items():
            if key.lower() == address.lower() and isinstance(value, dict):
                return value
        first = next(iter(result.values()), None)
        return first if isinstance(first, dict) else None

    def _fill_evm(self, report: SecurityReport, payload: Any, address: str) -> None:
        data = self._result_for(payload, address)
        if data is None:
            report.error = "token not found in the security database"
            return

        report.checked = True
        report.raw = data

        report.is_honeypot = _flag(data.get("is_honeypot"))
        report.cannot_buy = _flag(data.get("cannot_buy"))
        report.is_mintable = _flag(data.get("is_mintable"))
        report.transfer_pausable = _flag(data.get("transfer_pausable"))
        report.can_take_back_ownership = _flag(data.get("can_take_back_ownership"))
        report.hidden_owner = _flag(data.get("hidden_owner"))
        report.is_open_source = _flag(data.get("is_open_source"))
        report.is_proxy = _flag(data.get("is_proxy"))

        report.buy_tax = _pct(data.get("buy_tax"))
        report.sell_tax = _pct(data.get("sell_tax"))
        report.holder_count = to_int(data.get("holder_count"))
        report.owner_percent = _pct(data.get("owner_percent"))

        holders = data.get("holders")
        if isinstance(holders, list) and holders:
            percents = [_pct(h.get("percent")) for h in holders if isinstance(h, dict)]
            percents = [p for p in percents if p is not None]
            if percents:
                report.top_holder_percent = max(percents)
                report.top10_percent = sum(sorted(percents, reverse=True)[:10])

        lp_holders = data.get("lp_holders")
        if isinstance(lp_holders, list):
            report.lp_holder_count = len(lp_holders)
            locked = [
                _pct(h.get("percent")) or 0.0
                for h in lp_holders
                if isinstance(h, dict) and str(h.get("is_locked")) == "1"
            ]
            if lp_holders:
                report.lp_locked_percent = sum(locked)

    def _fill_solana(self, report: SecurityReport, payload: Any, address: str) -> None:
        data = self._result_for(payload, address)
        if data is None:
            report.error = "token not found in the security database"
            return

        report.checked = True
        report.raw = data

        # Solana nests these as {"status": "1", "authority": [...]}
        def nested(name: str) -> bool | None:
            node = data.get(name)
            if isinstance(node, dict):
                return _flag(node.get("status"))
            return _flag(node)

        report.is_mintable = nested("mintable")
        report.is_freezable = nested("freezable")
        report.balance_mutable = nested("balance_mutable_authority")
        report.transfer_pausable = nested("closable")

        holders = data.get("holders")
        if isinstance(holders, list) and holders:
            percents = [_pct(h.get("percent")) for h in holders if isinstance(h, dict)]
            percents = [p for p in percents if p is not None]
            if percents:
                report.top_holder_percent = max(percents)
                report.top10_percent = sum(sorted(percents, reverse=True)[:10])

        lp_holders = data.get("lp_holders")
        if isinstance(lp_holders, list):
            report.lp_holder_count = len(lp_holders)
            locked = [
                _pct(h.get("percent")) or 0.0
                for h in lp_holders
                if isinstance(h, dict) and str(h.get("is_locked")) == "1"
            ]
            if lp_holders:
                report.lp_locked_percent = sum(locked)

    def stats(self) -> dict[str, Any]:
        return self.client.stats()
