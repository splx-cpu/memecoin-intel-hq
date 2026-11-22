"""Tests for the two scanner loops and the repository's SQL shape.

Scanner logic is tested against an in-memory repository so the behaviour
under test is orchestration - what gets stored, what gets watchlisted, what
happens when a provider fails - rather than SQL. The SQL itself is verified
separately against a real PostgreSQL server (see docs/PROGRESS.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest

from mihq.config import Settings
from mihq.db.repositories import (
    ALL_STATEMENTS,
    Repository,
    StoredSnapshot,
    UpsertResult,
    WatchlistEntry,
)
from mihq.models import MarketObservation, TokenRef, utcnow
from mihq.providers.errors import ProviderUnavailable
from mihq.scanners.discovery import DiscoveryScanner
from mihq.scanners.policy import SnapshotPolicy
from mihq.scanners.watchlist import WatchlistScanner


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeRepo:
    """In-memory stand-in with the same interface as `Repository`."""

    def __init__(self) -> None:
        self.tokens: dict[tuple[str, str], int] = {}
        self.snapshots: dict[int, list[MarketObservation]] = {}
        self.raw_flags: list[bool] = []
        self.watchlist: dict[int, dict[str, Any]] = {}
        self.runs: list[dict[str, Any]] = []
        self.scanned: list[int] = []
        self.failed: list[int] = []
        self.deactivated: list[tuple[int, int]] = []
        self.events: dict[str, dict[str, Any]] = {}
        self.event_attempts: int = 0
        self.risk_flags: dict[tuple[int, str], dict[str, Any]] = {}
        self.resolved_flags: list[tuple[int, str]] = []
        self._next_id = 1
        self.fail_on: set[str] = set()

    def _check(self, name: str) -> None:
        if name in self.fail_on:
            raise RuntimeError(f"simulated database failure in {name}")

    def start_run(self, scanner: str, chains) -> int:
        self._check("start_run")
        self.runs.append({"scanner": scanner, "chains": list(chains), "status": "running"})
        return len(self.runs)

    def finish_run(self, run_id: int, **kwargs: Any) -> None:
        self._check("finish_run")
        self.runs[run_id - 1].update(kwargs)

    def upsert_token_from(self, observation: MarketObservation) -> UpsertResult:
        self._check("upsert_token")
        key = observation.token.key
        if key in self.tokens:
            return UpsertResult(self.tokens[key], was_inserted=False)
        token_id = self._next_id
        self._next_id += 1
        self.tokens[key] = token_id
        return UpsertResult(token_id, was_inserted=True)

    def latest_snapshot(self, token_id: int) -> StoredSnapshot | None:
        history = self.snapshots.get(token_id)
        if not history:
            return None
        last = history[-1]
        return StoredSnapshot(
            captured_at=last.captured_at, price_usd=last.price_usd,
            liquidity_usd=last.liquidity_usd, volume_24h_usd=last.volume_24h_usd,
        )

    def insert_snapshot(self, token_id, observation, *, include_raw=False):
        self._check("insert_snapshot")
        self.snapshots.setdefault(token_id, []).append(observation)
        self.raw_flags.append(include_raw)
        return len(self.raw_flags)

    def add_to_watchlist(self, token_id, *, category, reason=None, priority=100,
                         readd_cooldown_hours=0):
        self._check("add_to_watchlist")
        if token_id in self.watchlist:
            return None
        if readd_cooldown_hours and token_id in getattr(self, "recently_retired", ()):
            return None
        self.watchlist[token_id] = {"category": category, "priority": priority,
                                    "reason": reason, "failures": 0, "active": True}
        return token_id

    def due_watchlist(self, *, default_interval_seconds, limit):
        self._check("due_watchlist")
        return self._due[:limit]

    def insert_alert_event(self, *, event_key, token_id, chain, category,
                           event_type, severity, payload, snapshot_id=None):
        self._check("insert_alert_event")
        self.event_attempts += 1
        if event_key in self.events:
            return None          # the UNIQUE constraint doing its job
        event_id = len(self.events) + 1
        self.events[event_key] = {
            "id": event_id, "token_id": token_id, "chain": chain,
            "category": category, "event_type": event_type,
            "severity": severity, "payload": payload, "snapshot_id": snapshot_id,
        }
        return event_id

    def link_event_snapshot(self, event_id, snapshot_id):
        self._check("link_event_snapshot")
        for ev in self.events.values():
            if ev["id"] == event_id:
                ev["snapshot_id"] = snapshot_id

    def upsert_risk_flag(self, token_id, *, flag_type, severity, detail,
                         evidence=None, source="heuristic"):
        self._check("upsert_risk_flag")
        key = (token_id, flag_type)
        was_new = key not in self.risk_flags
        self.risk_flags[key] = {"severity": severity, "detail": detail,
                                "evidence": evidence or {}, "source": source}
        return (len(self.risk_flags), was_new)

    def resolve_absent_risk_flags(self, token_id, current_flag_types):
        self._check("resolve_absent_risk_flags")
        current = set(current_flag_types)
        gone = [(tid, ft) for (tid, ft) in list(self.risk_flags)
                if tid == token_id and ft not in current]
        for key in gone:
            del self.risk_flags[key]
        self.resolved_flags.extend(gone)
        return gone

    def mark_scanned(self, ids): self.scanned.extend(ids)
    def mark_scan_failed(self, ids):
        self.failed.extend(ids)
        return [(i, i, 1) for i in ids]
    def deactivate_persistently_failing(self, *, threshold, note="x"):
        return self.deactivated
    def active_watchlist_count(self) -> int: return len(self.watchlist)


class FakeGecko:
    def __init__(self, by_chain: dict[str, Any]) -> None:
        self.by_chain = by_chain
        self.calls: list[str] = []

    def new_pools(self, chain_key: str):
        self.calls.append(chain_key)
        result = self.by_chain.get(chain_key, [])
        if isinstance(result, Exception):
            raise result
        return result

    def stats(self): return {"provider": "geckoterminal", "requests": len(self.calls)}


class FakeDex:
    def __init__(self, by_chain: dict[str, Any]) -> None:
        self.by_chain = by_chain
        self.batched_calls: list[tuple[str, int]] = []

    def pairs_by_address(self, chain_key, pool_addresses):
        self.batched_calls.append((chain_key, len(pool_addresses)))
        result = self.by_chain.get(chain_key, [])
        if isinstance(result, Exception):
            raise result
        return result

    def pairs_for_token(self, chain_key, token_address):
        result = self.by_chain.get(chain_key, [])
        if isinstance(result, Exception):
            raise result
        return [o for o in result if o.token.address == token_address]

    def stats(self): return {"provider": "dexscreener", "requests": len(self.batched_calls)}


def observation(chain="base", address=None, source="geckoterminal", **metrics):
    address = address or ("0x" + "a" * 40)
    # Default to a pool large enough to clear the watchlist floor. Tests that
    # care about the floor pass their own figure.
    metrics.setdefault("liquidity_usd", 50_000.0)
    return MarketObservation(
        token=TokenRef(chain=chain, address=address, symbol="TEST"),
        source=source,
        captured_at=utcnow(),
        pool_address="0x" + "p" * 40,
        **metrics,
    )


@pytest.fixture
def scanner_settings() -> Settings:
    return Settings(
        enabled_chains=("solana", "base"),
        watchlist_interval_seconds=60,
        watchlist_batch_size=40,
        snapshot_heartbeat_seconds=900,
        security_checks_enabled=False,
    )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class TestDiscoveryScanner:
    def test_stores_new_tokens_and_watchlists_them(self, scanner_settings):
        repo = FakeRepo()
        gecko = FakeGecko({
            "solana": [observation("solana", "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
                                   price_usd=1.0)],
            "base": [observation("base", price_usd=2.0)],
        })
        result = DiscoveryScanner(scanner_settings, repo, gecko).run_once()

        assert result.status == "success"
        assert result.tokens_seen == 2
        assert result.new_tokens == 2
        assert result.snapshots_written == 2
        assert result.watchlist_added == 2
        assert all(e["category"] == "new_coin" for e in repo.watchlist.values())

    def test_new_tokens_get_higher_priority(self, scanner_settings):
        repo = FakeRepo()
        gecko = FakeGecko({"base": [observation(price_usd=1.0)], "solana": []})
        DiscoveryScanner(scanner_settings, repo, gecko).run_once()
        assert all(e["priority"] == 50 for e in repo.watchlist.values())

    def test_first_observation_keeps_the_raw_payload(self, scanner_settings):
        repo = FakeRepo()
        gecko = FakeGecko({"base": [observation(price_usd=1.0)], "solana": []})
        DiscoveryScanner(scanner_settings, repo, gecko).run_once()
        assert repo.raw_flags == [True]

    def test_already_known_token_is_not_recounted_or_rewatchlisted(self, scanner_settings):
        repo = FakeRepo()
        gecko = FakeGecko({"base": [observation(price_usd=1.0)], "solana": []})
        scanner = DiscoveryScanner(scanner_settings, repo, gecko)
        scanner.run_once()
        second = scanner.run_once()
        assert second.new_tokens == 0
        assert second.watchlist_added == 0
        assert len(repo.watchlist) == 1

    def test_unchanged_token_is_seen_but_not_rewritten(self, scanner_settings):
        """The write policy must apply on discovery too, not just watchlist."""
        repo = FakeRepo()
        same = observation(price_usd=1.0, liquidity_usd=50000.0)
        gecko = FakeGecko({"base": [same], "solana": []})
        scanner = DiscoveryScanner(scanner_settings, repo, gecko)
        scanner.run_once()
        second = scanner.run_once()
        assert second.tokens_seen == 1
        assert second.snapshots_written == 0
        assert second.snapshots_skipped == 1

    def test_one_chain_failing_does_not_abandon_the_others(self, scanner_settings):
        repo = FakeRepo()
        gecko = FakeGecko({
            "solana": ProviderUnavailable("gateway timeout", provider="geckoterminal"),
            "base": [observation("base", price_usd=1.0)],
        })
        result = DiscoveryScanner(scanner_settings, repo, gecko).run_once()

        assert result.status == "partial"
        assert result.provider_errors == 1
        assert result.tokens_seen == 1, "the healthy chain must still be scanned"
        assert "error" in result.per_chain["solana"]

    def test_every_chain_failing_is_reported_as_failed(self, scanner_settings):
        repo = FakeRepo()
        gecko = FakeGecko({
            "solana": ProviderUnavailable("down", provider="geckoterminal"),
            "base": ProviderUnavailable("down", provider="geckoterminal"),
        })
        result = DiscoveryScanner(scanner_settings, repo, gecko).run_once()
        assert result.status == "failed"
        assert result.provider_errors == 2

    def test_database_failure_on_one_token_does_not_abort_the_pass(self, scanner_settings):
        repo = FakeRepo()
        repo.fail_on.add("insert_snapshot")
        gecko = FakeGecko({"base": [observation(price_usd=1.0)], "solana": []})
        result = DiscoveryScanner(scanner_settings, repo, gecko).run_once()
        assert result.status == "partial"
        assert result.database_errors == 1

    def test_run_is_always_recorded_for_health_monitoring(self, scanner_settings):
        repo = FakeRepo()
        gecko = FakeGecko({"base": [], "solana": []})
        result = DiscoveryScanner(scanner_settings, repo, gecko).run_once()
        assert result.run_id is not None
        assert repo.runs[0]["scanner"] == "discovery"
        assert repo.runs[0]["status"] == "success"
        assert "provider_stats" in repo.runs[0]["details"]

    def test_cannot_open_a_run_means_failed_not_crashed(self, scanner_settings):
        repo = FakeRepo()
        repo.fail_on.add("start_run")
        gecko = FakeGecko({"base": [], "solana": []})
        result = DiscoveryScanner(scanner_settings, repo, gecko).run_once()
        assert result.status == "failed"
        assert result.database_errors == 1


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

def entry(watchlist_id: int, token_id: int, chain="base", address=None, pool=None):
    return WatchlistEntry(
        watchlist_id=watchlist_id, token_id=token_id, chain=chain,
        token_address=address or ("0x" + f"{token_id:040x}"),
        primary_pool_address=pool or ("0x" + f"{token_id:040x}"),
        symbol="TEST", category="momentum", priority=100,
        last_scanned_at=None, consecutive_failures=0,
    )


class TestWatchlistScanner:
    def test_checks_due_tokens_and_writes_changed_ones(self, scanner_settings):
        repo = FakeRepo()
        repo._due = [entry(1, 1), entry(2, 2)]
        addr1, addr2 = entry(1, 1).token_address, entry(2, 2).token_address
        dex = FakeDex({"base": [
            observation("base", addr1, "dexscreener", price_usd=1.0),
            observation("base", addr2, "dexscreener", price_usd=2.0),
        ]})
        result = WatchlistScanner(scanner_settings, repo, dex).run_once()

        assert result.status == "success"
        assert result.tokens_due == 2
        assert result.tokens_checked == 2
        assert result.snapshots_written == 2      # first observation for each
        assert sorted(repo.scanned) == [1, 2]

    def test_batches_one_request_per_chain(self, scanner_settings):
        """30 tokens must be one call, not 30."""
        repo = FakeRepo()
        repo._due = [entry(i, i) for i in range(1, 31)]
        dex = FakeDex({"base": []})
        WatchlistScanner(scanner_settings, repo, dex).run_once()
        assert len(dex.batched_calls) == 1
        assert dex.batched_calls[0] == ("base", 30)

    def test_groups_requests_by_chain(self, scanner_settings):
        repo = FakeRepo()
        repo._due = [
            entry(1, 1, "base"),
            entry(2, 2, "solana", "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
                  "8sLbNZoA1cfnvMJLPfp98ZLAnFSYCFApfJKMbiXNLwxj"),
        ]
        dex = FakeDex({"base": [], "solana": []})
        WatchlistScanner(scanner_settings, repo, dex).run_once()
        assert sorted(c[0] for c in dex.batched_calls) == ["base", "solana"]

    def test_unchanged_token_is_checked_but_not_rewritten(self, scanner_settings):
        repo = FakeRepo()
        repo._due = [entry(1, 1)]
        addr = entry(1, 1).token_address
        obs = observation("base", addr, "dexscreener", price_usd=1.0, liquidity_usd=50000.0)
        dex = FakeDex({"base": [obs]})
        scanner = WatchlistScanner(scanner_settings, repo, dex)
        scanner.run_once()
        second = scanner.run_once()
        assert second.tokens_checked == 1
        assert second.snapshots_written == 0
        assert second.snapshots_skipped == 1

    def test_missing_token_counts_as_a_failure_not_a_check(self, scanner_settings):
        repo = FakeRepo()
        repo._due = [entry(1, 1)]
        dex = FakeDex({"base": []})        # provider returns nothing for it
        result = WatchlistScanner(scanner_settings, repo, dex).run_once()
        assert result.tokens_missing == 1
        assert result.tokens_checked == 0
        assert repo.failed == [1]
        assert repo.scanned == []

    def test_provider_outage_is_not_blamed_on_individual_tokens(self, scanner_settings):
        """A down provider must not push every token toward deactivation."""
        repo = FakeRepo()
        repo._due = [entry(1, 1), entry(2, 2)]
        dex = FakeDex({"base": ProviderUnavailable("down", provider="dexscreener")})
        result = WatchlistScanner(scanner_settings, repo, dex).run_once()

        assert result.status == "partial"
        assert result.provider_errors == 1
        assert repo.failed == [], "an outage must not increment per-token failures"
        assert result.tokens_missing == 0

    def test_persistently_missing_entries_get_retired(self, scanner_settings):
        repo = FakeRepo()
        repo._due = [entry(1, 1)]
        repo.deactivated = [(1, 1)]
        dex = FakeDex({"base": []})
        result = WatchlistScanner(scanner_settings, repo, dex).run_once()
        assert result.deactivated == 1

    def test_picks_the_deepest_pool_when_several_are_returned(self, scanner_settings):
        repo = FakeRepo()
        repo._due = [entry(1, 1)]
        addr = entry(1, 1).token_address
        dex = FakeDex({"base": [
            observation("base", addr, "dexscreener", price_usd=1.0, liquidity_usd=2_000.0),
            observation("base", addr, "dexscreener", price_usd=1.1, liquidity_usd=200_000.0),
        ]})
        WatchlistScanner(scanner_settings, repo, dex).run_once()
        stored = repo.snapshots[1][0]
        assert stored.liquidity_usd == 200_000.0

    def test_empty_watchlist_is_a_clean_success(self, scanner_settings):
        repo = FakeRepo()
        repo._due = []
        dex = FakeDex({})
        result = WatchlistScanner(scanner_settings, repo, dex).run_once()
        assert result.status == "success"
        assert result.tokens_due == 0
        assert repo.runs[0]["status"] == "success"

    def test_batch_size_limit_is_respected(self, scanner_settings):
        repo = FakeRepo()
        repo._due = [entry(i, i) for i in range(1, 101)]
        settings = Settings(**{**scanner_settings.__dict__, "watchlist_batch_size": 10})
        dex = FakeDex({"base": []})
        result = WatchlistScanner(settings, repo, dex).run_once()
        assert result.tokens_due == 10


# ---------------------------------------------------------------------------
# Repository SQL shape
# ---------------------------------------------------------------------------

class RecordingCursor:
    def __init__(self, result): self.result = result; self.executed = []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self.executed.append((sql, params))
    def fetchone(self): return self.result
    def fetchall(self): return [self.result] if self.result else []


class RecordingConnection:
    def __init__(self, result=None): self.cursor_obj = RecordingCursor(result)
    def cursor(self): return self.cursor_obj


class TestRepositorySqlShape:
    def test_every_statement_uses_placeholders_not_formatting(self):
        """SQL injection guard: no f-strings or % formatting in any statement."""
        for name, sql in ALL_STATEMENTS.items():
            assert "{" not in sql, f"{name} looks like an f-string"
            assert "';" not in sql, f"{name} may contain an inlined literal"

    def test_upsert_passes_seven_parameters(self):
        conn = RecordingConnection((42, True))
        result = Repository(conn).upsert_token(chain="base", token_address="0xabc",
                                               symbol="X")
        assert result == UpsertResult(42, True)
        _, params = conn.cursor_obj.executed[0]
        assert len(params) == 7
        assert params[0] == "base"

    def test_insert_snapshot_passes_all_25_columns(self):
        conn = RecordingConnection((7,))
        obs = observation(price_usd=1.0)
        Repository(conn).insert_snapshot(1, obs, include_raw=True)
        _, params = conn.cursor_obj.executed[0]
        assert len(params) == 25

    def test_raw_is_withheld_when_not_requested(self):
        conn = RecordingConnection((7,))
        obs = observation(price_usd=1.0)
        obs.raw["big"] = "x" * 10_000
        Repository(conn).insert_snapshot(1, obs, include_raw=False)
        _, params = conn.cursor_obj.executed[0]
        assert params[-1] == "{}", "raw payload must not be stored when withheld"

    def test_raw_is_included_when_requested(self):
        conn = RecordingConnection((7,))
        obs = observation(price_usd=1.0)
        obs.raw["marker"] = "keep-me"
        Repository(conn).insert_snapshot(1, obs, include_raw=True)
        _, params = conn.cursor_obj.executed[0]
        assert "keep-me" in params[-1]

    def test_empty_id_lists_make_no_database_call(self):
        conn = RecordingConnection()
        repo = Repository(conn)
        repo.mark_scanned([])
        repo.mark_scan_failed([])
        assert conn.cursor_obj.executed == []

    def test_watchlist_entry_maps_columns_in_order(self):
        row = (1, 2, "base", "0xabc", "0xpool", "SYM", "momentum", 50, None, 3)
        e = WatchlistEntry.from_row(row)
        assert e.watchlist_id == 1 and e.token_id == 2
        assert e.chain == "base" and e.category == "momentum"
        assert e.consecutive_failures == 3
