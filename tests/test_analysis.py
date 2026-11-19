"""Tests for the free template analyzer.

The theme: it must state what it knows, say plainly what it does not, and
never invent an interpretation. A template that editorialises is worse than
no analyzer, because it launders a guess into something that looks systematic.
"""

from __future__ import annotations

import pytest

from mihq.analysis import build_analyzer
from mihq.analysis.base import ResearchReport
from mihq.analysis.template import TemplateAnalyzer
from mihq.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(security_checks_enabled=False)


@pytest.fixture
def analyzer(settings) -> TemplateAnalyzer:
    return TemplateAnalyzer(settings)


TOKEN = {"symbol": "TESTCOIN", "chain": "base",
         "token_address": "0x" + "a" * 40}

DRAIN_EVENT = {
    "category": "risk",
    "event_type": "liquidity_drain",
    "severity": "high",
    "chain": "base",
    "payload": {
        "headline": "TESTCOIN liquidity down 60% ($100.0K to $40.0K)",
        "source": "dexscreener",
        "captured_at": "2026-11-15T04:35:00+00:00",
        "metric": "liquidity_usd",
        "before": 100_000.0,
        "after": 40_000.0,
        "change_pct": -60.0,
        "price_usd": 0.001112,
        "liquidity_usd": 40_000.0,
        "volume_24h_usd": 119_942.98,
        "buys_24h": 812,
        "sells_24h": 833,
        "missing_fields": ["holder_count", "fdv_usd"],
        "source_url": "https://dexscreener.com/base/0xpair",
    },
}

RISK_FLAGS = [
    {"flag_type": "low_liquidity", "severity": "high",
     "detail": "Pool liquidity is $40,000, below the $5,000 threshold."},
    {"flag_type": "missing_verification", "severity": "info",
     "detail": "These checks could not be performed..."},
]


class TestSelection:
    def test_template_is_the_default(self, settings):
        assert build_analyzer(settings).name == "template"

    def test_claude_mode_without_a_key_falls_back_to_free(self):
        """The system must never be blocked by a missing API key."""
        assert build_analyzer(Settings(analysis_mode="claude")).name == "template"

    def test_template_costs_nothing(self, analyzer):
        report = analyzer.analyze(event=DRAIN_EVENT, token=TOKEN, risk_flags=[])
        assert report.cost_usd == 0.0
        assert report.is_free
        assert report.model == "template-v1"


class TestHonesty:
    def test_makes_no_assumptions_at_all(self, analyzer):
        """A template has no basis for inference and must not pretend to."""
        report = analyzer.analyze(event=DRAIN_EVENT, token=TOKEN,
                                  risk_flags=RISK_FLAGS)
        assert report.assumptions == []

    def test_states_no_confidence(self, analyzer):
        report = analyzer.analyze(event=DRAIN_EVENT, token=TOKEN, risk_flags=[])
        assert report.confidence is None

    def test_every_verified_fact_names_its_source(self, analyzer):
        report = analyzer.analyze(event=DRAIN_EVENT, token=TOKEN, risk_flags=[])
        sourced = [f for f in report.verified_facts if "via dexscreener" in f]
        assert len(sourced) >= 3

    def test_records_what_could_not_be_checked(self, analyzer):
        report = analyzer.analyze(event=DRAIN_EVENT, token=TOKEN, risk_flags=[])
        joined = " ".join(report.missing_data).lower()
        assert "holder count" in joined
        assert "mint authority" in joined
        assert "not evidence that the token is safe" in joined

    def test_absence_of_flags_is_not_called_safe(self, analyzer):
        report = analyzer.analyze(event=DRAIN_EVENT, token=TOKEN, risk_flags=[])
        assert "not the same as the token being safe" in report.summary

    def test_carries_the_not_advice_disclaimer(self, analyzer):
        report = analyzer.analyze(event=DRAIN_EVENT, token=TOKEN, risk_flags=[])
        lowered = report.summary.lower()
        assert "not investment advice" in lowered
        assert "no buy or sell recommendation" in lowered

    def test_never_recommends_an_action(self, analyzer):
        """The spec forbids buy/sell signals outright."""
        report = analyzer.analyze(event=DRAIN_EVENT, token=TOKEN,
                                  risk_flags=RISK_FLAGS)
        text = " ".join([
            report.summary, report.what_changed or "",
            *report.verified_facts, *report.risks,
        ]).lower()
        for banned in ("you should buy", "you should sell", "strong buy",
                       "recommend buying", "recommend selling", "will moon",
                       "guaranteed", "price target"):
            assert banned not in text

    def test_churn_is_described_without_asserting_a_cause(self, analyzer):
        event = {**DRAIN_EVENT, "event_type": "high_churn",
                 "payload": {**DRAIN_EVENT["payload"], "ratio": 18.4}}
        report = analyzer.analyze(event=event, token=TOKEN, risk_flags=[])
        assert "does not establish why" in (report.what_changed or "")


class TestContent:
    def test_reports_the_measured_change(self, analyzer):
        report = analyzer.analyze(event=DRAIN_EVENT, token=TOKEN, risk_flags=[])
        assert "$100.0K" in (report.what_changed or "")
        assert "$40.0K" in (report.what_changed or "")
        assert "-60.0%" in (report.what_changed or "")

    def test_includes_verifiable_source_links(self, analyzer):
        report = analyzer.analyze(event=DRAIN_EVENT, token=TOKEN, risk_flags=[])
        urls = [s["url"] for s in report.sources]
        assert any("dexscreener.com" in u for u in urls)
        assert any("basescan.org" in u for u in urls)

    def test_risk_flags_appear_with_severity(self, analyzer):
        report = analyzer.analyze(event=DRAIN_EVENT, token=TOKEN,
                                  risk_flags=RISK_FLAGS)
        assert any("[HIGH]" in r for r in report.risks)

    def test_missing_verification_is_not_listed_as_a_risk(self, analyzer):
        """It belongs in missing_data; as a 'risk' it would be misleading."""
        report = analyzer.analyze(event=DRAIN_EVENT, token=TOKEN,
                                  risk_flags=RISK_FLAGS)
        assert not any("could not be performed" in r for r in report.risks)

    def test_first_seen_says_there_is_no_history_yet(self, analyzer):
        event = {**DRAIN_EVENT, "event_type": "first_seen", "category": "new_coin",
                 "payload": {**DRAIN_EVENT["payload"], "before": None, "after": None}}
        report = analyzer.analyze(event=event, token=TOKEN, risk_flags=[])
        assert "no earlier observation" in (report.what_changed or "")

    def test_unreported_liquidity_does_not_claim_a_reason(self, analyzer):
        event = {**DRAIN_EVENT, "event_type": "liquidity_unreported",
                 "payload": {**DRAIN_EVENT["payload"], "after": None}}
        report = analyzer.analyze(event=event, token=TOKEN, risk_flags=[])
        assert "not known from this data alone" in (report.what_changed or "")


class TestRobustness:
    def test_survives_an_almost_empty_payload(self, analyzer):
        event = {"category": "momentum", "event_type": "volume_spike",
                 "chain": "base", "payload": {}}
        report = analyzer.analyze(event=event, token=TOKEN, risk_flags=[])
        assert report.summary
        assert report.missing_data

    def test_survives_an_unknown_chain(self, analyzer):
        report = analyzer.analyze(
            event=DRAIN_EVENT,
            token={"symbol": "X", "chain": "notachain", "token_address": "0xabc"},
            risk_flags=[])
        assert report.summary

    def test_handles_a_missing_symbol(self, analyzer):
        report = analyzer.analyze(
            event=DRAIN_EVENT,
            token={"chain": "base", "token_address": "0x" + "a" * 40},
            risk_flags=[])
        assert "Unknown token" in report.summary or report.headline


class TestRowMapping:
    def test_maps_onto_the_research_reports_columns(self, analyzer):
        report = analyzer.analyze(event=DRAIN_EVENT, token=TOKEN, risk_flags=[])
        row = report.to_row(token_id=7, alert_event_id=3)
        expected = {
            "token_id", "alert_event_id", "category", "model", "prompt_version",
            "headline", "summary", "what_changed", "verified_facts",
            "assumptions", "missing_data", "risks", "sources", "confidence",
            "input_tokens", "output_tokens", "cost_usd", "latency_ms",
            "raw_response",
        }
        assert set(row) == expected
        assert row["token_id"] == 7
        assert row["alert_event_id"] == 3
