"""Tests for contract security checks and the compact alert format.

The governing principle: UNKNOWN IS NOT SAFE. A token the security provider
has never seen must never render as having passed its checks. That inversion
is precisely the mistake these checks exist to prevent, so most of this file
is about what happens when data is missing rather than when it is present.
"""

from __future__ import annotations

import json

import pytest

from conftest import FakeClock, FakeTransport, json_response
from mihq.analysis.base import ResearchReport
from mihq.chains import CHAINS
from mihq.config import Settings
from mihq.events.risk import from_security
from mihq.notify.formatters import format_alert_compact
from mihq.providers.security import SecurityProvider, SecurityReport, _flag, _pct

# Field names taken from a live GoPlus response on 2026-11-18.
EVM_CLEAN = {
    "is_honeypot": "0", "is_mintable": "0", "is_open_source": "1",
    "is_proxy": "0", "transfer_pausable": "0", "can_take_back_ownership": "0",
    "hidden_owner": "0", "cannot_buy": "0", "buy_tax": "0", "sell_tax": "0",
    "holder_count": "1842", "owner_percent": "0",
    "holders": [{"address": "0x1", "percent": "0.031", "is_locked": "0"},
                {"address": "0x2", "percent": "0.018", "is_locked": "0"}],
    "lp_holders": [{"address": "0x9", "percent": "0.92", "is_locked": "1"},
                   {"address": "0x8", "percent": "0.08", "is_locked": "0"}],
    "token_symbol": "CLEAN",
}

EVM_DANGEROUS = {
    "is_honeypot": "1", "is_mintable": "1", "is_open_source": "0",
    "transfer_pausable": "1", "can_take_back_ownership": "1",
    "hidden_owner": "1", "sell_tax": "0.45", "buy_tax": "0.05",
    "holder_count": "23",
    "holders": [{"address": "0xdead", "percent": "0.88", "is_locked": "0"}],
    "lp_holders": [{"address": "0x9", "percent": "1.0", "is_locked": "0"}],
}

SOLANA_RESPONSE = {
    "mintable": {"status": "1", "authority": ["Abc"]},
    "freezable": {"status": "0", "authority": []},
    "balance_mutable_authority": {"status": "0"},
    "closable": {"status": "0"},
    "holders": [{"account": "x", "percent": "0.71", "is_locked": 0}],
    "lp_holders": [{"address": "y", "percent": "1.0", "is_locked": "1"}],
}

ADDR_EVM = "0x" + "a" * 40
ADDR_SOL = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def provider(payload, clock=None):
    clock = clock or FakeClock()
    settings = Settings(http_timeout_seconds=5.0, http_max_retries=1)
    transport = FakeTransport([json_response(json.dumps(payload))])
    return SecurityProvider(settings, transport=transport, clock=clock,
                            sleep=clock.sleep), transport


class TestFieldParsing:
    @pytest.mark.parametrize("raw,expected", [
        ("1", True), ("0", False), (1, True), (0, False),
        (None, None), ("", None), ("unknown", None), ("null", None),
    ])
    def test_flag_distinguishes_false_from_unknown(self, raw, expected):
        """The single most important distinction in this module."""
        assert _flag(raw) is expected

    @pytest.mark.parametrize("raw,expected", [
        ("0.67", 67.0), ("0.031", 3.1), (0.5, 50.0),
        ("45", 45.0), (None, None), ("", None),
    ])
    def test_percent_handles_fractional_and_whole(self, raw, expected):
        result = _pct(raw)
        if expected is None:
            assert result is None
        else:
            assert result == pytest.approx(expected, abs=0.01)


class TestEvmParsing:
    def test_parses_a_clean_token(self):
        p, _ = provider({"result": {ADDR_EVM: EVM_CLEAN}})
        r = p.check("base", ADDR_EVM)
        assert r.checked
        assert r.is_honeypot is False
        assert r.is_mintable is False
        assert r.is_open_source is True
        assert r.holder_count == 1842
        assert r.top_holder_percent == pytest.approx(3.1, abs=0.1)
        assert r.lp_locked_percent == pytest.approx(92.0, abs=0.5)

    def test_parses_a_dangerous_token(self):
        p, _ = provider({"result": {ADDR_EVM: EVM_DANGEROUS}})
        r = p.check("base", ADDR_EVM)
        assert r.is_honeypot is True
        assert r.is_mintable is True
        assert r.top_holder_percent == pytest.approx(88.0, abs=0.5)
        assert r.sell_tax == pytest.approx(45.0, abs=0.5)

    def test_uses_the_right_chain_id(self):
        for chain, expected in (("base", "8453"), ("bsc", "56"),
                                ("ethereum", "1"), ("avalanche", "43114")):
            p, transport = provider({"result": {ADDR_EVM: EVM_CLEAN}})
            p.check(chain, ADDR_EVM)
            assert f"/token_security/{expected}" in transport.calls[0]["url"]

    def test_address_key_case_does_not_matter(self):
        """GoPlus echoes the address back in unpredictable case."""
        p, _ = provider({"result": {ADDR_EVM.upper(): EVM_CLEAN}})
        assert p.check("base", ADDR_EVM).checked


class TestSolanaParsing:
    def test_uses_the_solana_endpoint(self):
        p, transport = provider({"result": {ADDR_SOL: SOLANA_RESPONSE}})
        p.check("solana", ADDR_SOL)
        assert "/solana/token_security" in transport.calls[0]["url"]

    def test_unpacks_nested_status_objects(self):
        """Solana nests these as {"status": "1"}, EVM uses flat strings."""
        p, _ = provider({"result": {ADDR_SOL: SOLANA_RESPONSE}})
        r = p.check("solana", ADDR_SOL)
        assert r.is_mintable is True
        assert r.is_freezable is False
        assert r.top_holder_percent == pytest.approx(71.0, abs=0.5)


class TestUnknownIsNotSafe:
    def test_an_empty_result_is_not_checked(self):
        p, _ = provider({"result": {}})
        r = p.check("base", ADDR_EVM)
        assert not r.checked
        assert r.is_honeypot is None
        assert r.sellable is None

    def test_a_provider_failure_never_looks_clean(self):
        clock = FakeClock()
        settings = Settings(http_timeout_seconds=1.0, http_max_retries=0)
        transport = FakeTransport([json_response("", 500)])
        p = SecurityProvider(settings, transport=transport, clock=clock,
                             sleep=clock.sleep)
        r = p.check("base", ADDR_EVM)
        assert not r.checked
        assert r.error
        assert r.passed_checks() == []

    def test_check_never_raises(self):
        clock = FakeClock()
        settings = Settings(http_timeout_seconds=1.0, http_max_retries=0)
        transport = FakeTransport([json_response("not json at all")])
        p = SecurityProvider(settings, transport=transport, clock=clock,
                             sleep=clock.sleep)
        assert not p.check("base", ADDR_EVM).checked

    def test_unknown_fields_are_reported_as_unknown(self):
        r = SecurityReport(chain="base", address=ADDR_EVM, checked=True)
        unknown = r.unknown_checks()
        assert "honeypot / sellability" in unknown
        assert "mint authority" in unknown
        assert "liquidity lock" in unknown

    def test_passed_checks_excludes_unknowns(self):
        """Only positively-confirmed results may be reported as passing."""
        r = SecurityReport(chain="base", address=ADDR_EVM, checked=True,
                           is_honeypot=False, is_mintable=None)
        passed = r.passed_checks()
        assert any("honeypot" in p.lower() for p in passed)
        assert not any("mint" in p.lower() for p in passed)

    def test_an_unknown_chain_is_handled(self):
        p, _ = provider({"result": {}})
        assert not p.check("notachain", ADDR_EVM).checked


class TestFindings:
    def test_dangerous_token_produces_critical_findings(self):
        p, _ = provider({"result": {ADDR_EVM: EVM_DANGEROUS}})
        r = p.check("base", ADDR_EVM)
        findings = r.critical_findings()
        joined = " ".join(findings).lower()
        assert "honeypot" in joined
        assert "mint authority" in joined
        assert "88%" in joined

    def test_clean_token_produces_none(self):
        p, _ = provider({"result": {ADDR_EVM: EVM_CLEAN}})
        assert p.check("base", ADDR_EVM).critical_findings() == []

    def test_risk_score_counts_rather_than_rates(self):
        """Deliberately not a score out of 100 - that invites misreading."""
        p, _ = provider({"result": {ADDR_EVM: EVM_DANGEROUS}})
        failed, checked = p.check("base", ADDR_EVM).risk_score()
        assert failed > 0 and checked >= failed

    def test_unchecked_token_scores_nothing(self):
        failed, checked = SecurityReport(chain="base", address="x").risk_score()
        assert (failed, checked) == (0, 0)


class TestRiskFlagConversion:
    def test_security_flags_are_attributed_to_the_provider(self):
        """They are provider-asserted facts, not our heuristics."""
        p, _ = provider({"result": {ADDR_EVM: EVM_DANGEROUS}})
        findings = from_security(p.check("base", ADDR_EVM))
        assert findings
        assert all(f.source == "goplus" for f in findings)

    def test_honeypot_is_critical(self):
        p, _ = provider({"result": {ADDR_EVM: EVM_DANGEROUS}})
        findings = from_security(p.check("base", ADDR_EVM))
        honeypot = next(f for f in findings if f.flag_type == "honeypot")
        assert honeypot.severity == "critical"

    def test_clean_token_raises_no_security_flags(self):
        p, _ = provider({"result": {ADDR_EVM: EVM_CLEAN}})
        assert from_security(p.check("base", ADDR_EVM)) == []

    def test_unchecked_report_raises_nothing(self):
        assert from_security(SecurityReport(chain="base", address="x")) == []
        assert from_security(None) == []

    def test_ownership_reclaim_is_explained_not_just_named(self):
        p, _ = provider({"result": {ADDR_EVM: EVM_DANGEROUS}})
        findings = from_security(p.check("base", ADDR_EVM))
        flag = next(f for f in findings if f.flag_type == "ownership_reclaimable")
        assert "does not mean what it appears" in flag.detail


class TestCompactFormat:
    def _report(self):
        return ResearchReport(
            category="risk", summary="s", model="template-v1",
            headline="SON liquidity down 62%",
            sources=[{"label": "chart", "url": "https://dexscreener.com/base/0xp"}],
            missing_data=["holder count was not reported"],
        )

    def test_is_substantially_shorter_than_the_full_format(self):
        from mihq.notify.formatters import format_alert
        token = {"symbol": "SON", "chain": "base", "token_address": ADDR_EVM}
        event = {"category": "risk", "severity": "high", "chain": "base",
                 "payload": {"liquidity_usd": 8100, "volume_24h_usd": 42000,
                             "buys_24h": 112, "sells_24h": 89}}
        compact = format_alert_compact(report=self._report(), token=token, event=event)
        full = format_alert(report=self._report(), token=token, event=event)
        assert len(compact) < len(full) / 2

    def test_carries_the_numbers_that_give_scale(self):
        token = {"symbol": "SON", "chain": "base", "token_address": ADDR_EVM}
        event = {"category": "risk", "severity": "high", "chain": "base",
                 "payload": {"liquidity_usd": 8100, "volume_24h_usd": 42000,
                             "buys_24h": 112, "sells_24h": 89}}
        message = format_alert_compact(report=self._report(), token=token, event=event)
        assert "$8K liq" in message
        assert "$42K vol" in message
        assert "112/89" in message

    def test_shows_security_flags_when_available(self):
        p, _ = provider({"result": {ADDR_EVM: EVM_DANGEROUS}})
        security = p.check("base", ADDR_EVM)
        message = format_alert_compact(
            report=self._report(),
            token={"symbol": "SON", "chain": "base", "token_address": ADDR_EVM},
            event={"category": "risk", "severity": "critical", "payload": {}},
            security=security)
        assert "FLAGS:" in message
        assert "HONEYPOT" in message

    def test_states_what_was_not_checked(self):
        """Dropping this would turn silence into implied safety."""
        security = SecurityReport(chain="base", address=ADDR_EVM, checked=True,
                                  is_honeypot=False)
        message = format_alert_compact(
            report=self._report(),
            token={"symbol": "SON", "chain": "base", "token_address": ADDR_EVM},
            event={"category": "risk", "severity": "low", "payload": {}},
            security=security)
        assert "Unchecked:" in message
        assert "mint authority" in message

    def test_still_escapes_hostile_symbols(self):
        message = format_alert_compact(
            report=self._report(),
            token={"symbol": "<b>SAFE</b>", "chain": "base", "token_address": ADDR_EVM},
            event={"category": "risk", "severity": "low", "payload": {}})
        assert "<b>SAFE</b>" not in message
        assert "&lt;b&gt;SAFE&lt;/b&gt;" in message

    def test_keeps_the_disclaimer(self):
        message = format_alert_compact(
            report=self._report(),
            token={"symbol": "X", "chain": "base", "token_address": ADDR_EVM},
            event={"category": "risk", "severity": "low", "payload": {}})
        assert "Not advice" in message


class TestNoNetworkInTests:
    def test_every_chain_has_a_goplus_route(self):
        """Either an EVM chain id, or None meaning the Solana endpoint."""
        for spec in CHAINS.values():
            assert spec.goplus_chain_id is None or spec.goplus_chain_id.isdigit()

    def test_scanner_fixtures_disable_live_security_checks(self, settings):
        """Guards a regression: the suite once silently made real API calls.

        It went from 0.3s to 46s and nobody would have noticed except for the
        timing. A unit test that reaches the network is not a unit test.
        """
        assert settings.security_checks_enabled is False
