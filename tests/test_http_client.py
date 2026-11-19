"""Tests for the shared HTTP client: rate limiting, caching, retries.

All of this runs with a fake clock, so the whole file executes in
milliseconds despite testing behaviour measured in seconds.
"""

from __future__ import annotations

import json

import pytest

from conftest import FakeClock, FakeTransport, json_response
from mihq.providers.errors import (
    ProviderBadResponse,
    ProviderNotFound,
    ProviderRateLimited,
    ProviderUnavailable,
)
from mihq.providers.http import HttpClient, RateLimiter, TTLCache, TransportError


def make_client(responses, clock: FakeClock, **kwargs):
    transport = FakeTransport(responses)
    client = HttpClient(
        name="test",
        base_url="https://api.example.invalid",
        rate_limit_per_min=kwargs.pop("rate_limit_per_min", 60),
        cache_ttl_seconds=kwargs.pop("cache_ttl_seconds", 20),
        timeout_seconds=5.0,
        max_retries=kwargs.pop("max_retries", 3),
        transport=transport,
        clock=clock,
        sleep=clock.sleep,
        jitter=lambda: 0.5,      # deterministic backoff
        **kwargs,
    )
    return client, transport


# --- RateLimiter -----------------------------------------------------------

class TestRateLimiter:
    def test_burst_up_to_capacity_without_waiting(self, clock):
        """An idle scanner should be able to fire a batch immediately."""
        limiter = RateLimiter(60, clock=clock, sleep=clock.sleep)
        for _ in range(60):
            assert limiter.acquire() == 0.0
        assert clock.total_slept == 0.0

    def test_waits_once_the_bucket_is_empty(self, clock):
        limiter = RateLimiter(60, clock=clock, sleep=clock.sleep)
        for _ in range(60):
            limiter.acquire()
        waited = limiter.acquire()
        assert waited == pytest.approx(1.0, abs=0.01)   # 60/min = 1 token/sec

    def test_bucket_refills_over_time(self, clock):
        limiter = RateLimiter(60, clock=clock, sleep=clock.sleep)
        for _ in range(60):
            limiter.acquire()
        clock.advance(10)            # 10 seconds -> 10 tokens back
        for _ in range(10):
            assert limiter.acquire() == 0.0

    def test_refill_never_exceeds_capacity(self, clock):
        """A long idle period must not bank unlimited requests."""
        limiter = RateLimiter(30, clock=clock, sleep=clock.sleep)
        clock.advance(3600)
        for _ in range(30):
            assert limiter.acquire() == 0.0
        assert limiter.acquire() > 0

    def test_long_run_rate_stays_under_limit(self, clock):
        """120 requests at 60/min must take at least ~60 seconds."""
        limiter = RateLimiter(60, clock=clock, sleep=clock.sleep)
        start = clock.now
        for _ in range(120):
            limiter.acquire()
        assert clock.now - start >= 59.0

    def test_pause_for_drains_the_bucket(self, clock):
        """After a 429, leftover tokens must not let us fire straight back."""
        limiter = RateLimiter(60, clock=clock, sleep=clock.sleep)
        limiter.pause_for(30)
        assert clock.total_slept == 30
        assert limiter.acquire() > 0      # bucket was emptied, so we wait

    def test_rejects_nonsense_limit(self):
        with pytest.raises(ValueError):
            RateLimiter(0)


# --- TTLCache --------------------------------------------------------------

class TestTTLCache:
    def test_returns_value_within_ttl(self, clock):
        cache = TTLCache(20, clock=clock)
        cache.set("k", {"a": 1})
        clock.advance(19)
        assert cache.get("k") == {"a": 1}
        assert cache.hits == 1

    def test_expires_after_ttl(self, clock):
        cache = TTLCache(20, clock=clock)
        cache.set("k", {"a": 1})
        clock.advance(21)
        assert cache.get("k") is None
        assert len(cache) == 0            # expired entry is evicted

    def test_ttl_zero_disables_caching(self, clock):
        cache = TTLCache(0, clock=clock)
        cache.set("k", {"a": 1})
        assert cache.get("k") is None

    def test_miss_on_unknown_key(self, clock):
        assert TTLCache(20, clock=clock).get("nope") is None


# --- HttpClient ------------------------------------------------------------

class TestHttpClientSuccess:
    def test_parses_json(self, clock):
        client, transport = make_client([json_response('{"ok": true}')], clock)
        assert client.get_json("/thing") == {"ok": True}
        assert transport.call_count == 1

    def test_identical_request_is_served_from_cache(self, clock):
        client, transport = make_client([json_response('{"n": 1}')], clock)
        client.get_json("/thing", {"q": "abc"})
        client.get_json("/thing", {"q": "abc"})
        assert transport.call_count == 1, "second identical call should not hit the network"
        assert client.cache.hits == 1

    def test_different_params_are_cached_separately(self, clock):
        client, transport = make_client(
            [json_response('{"n": 1}'), json_response('{"n": 2}')], clock
        )
        client.get_json("/thing", {"q": "a"})
        client.get_json("/thing", {"q": "b"})
        assert transport.call_count == 2

    def test_param_order_does_not_change_the_cache_key(self, clock):
        client, transport = make_client([json_response('{"n": 1}')], clock)
        client.get_json("/thing", {"a": 1, "b": 2})
        client.get_json("/thing", {"b": 2, "a": 1})
        assert transport.call_count == 1

    def test_cache_can_be_bypassed(self, clock):
        client, transport = make_client(
            [json_response('{"n": 1}'), json_response('{"n": 2}')], clock
        )
        client.get_json("/thing")
        client.get_json("/thing", allow_cache=False)
        assert transport.call_count == 2

    def test_cached_entry_expires(self, clock):
        client, transport = make_client(
            [json_response('{"n": 1}'), json_response('{"n": 2}')], clock
        )
        client.get_json("/thing")
        clock.advance(25)
        client.get_json("/thing")
        assert transport.call_count == 2


class TestHttpClientFailures:
    def test_retries_then_succeeds_after_server_error(self, clock):
        client, transport = make_client(
            [json_response("boom", 503), json_response("boom", 503), json_response('{"ok": 1}')],
            clock,
        )
        assert client.get_json("/thing") == {"ok": 1}
        assert transport.call_count == 3
        assert clock.total_slept > 0, "should have backed off between attempts"

    def test_backoff_grows_between_attempts(self, clock):
        client, _ = make_client([json_response("boom", 500)] * 4, clock, max_retries=3)
        with pytest.raises(ProviderUnavailable):
            client.get_json("/thing")
        waits = [w for w in clock.slept if w > 0]
        assert len(waits) == 3
        assert waits[0] < waits[1] < waits[2], f"expected growing backoff, got {waits}"

    def test_gives_up_after_max_retries(self, clock):
        client, transport = make_client([json_response("boom", 500)] * 10, clock, max_retries=2)
        with pytest.raises(ProviderUnavailable):
            client.get_json("/thing")
        assert transport.call_count == 3        # 1 initial + 2 retries

    def test_network_failure_is_retried_then_raises(self, clock):
        client, transport = make_client([TransportError("connection reset")] * 10, clock,
                                        max_retries=2)
        with pytest.raises(ProviderUnavailable, match="network failure"):
            client.get_json("/thing")
        assert transport.call_count == 3

    def test_network_failure_can_recover(self, clock):
        client, _ = make_client(
            [TransportError("reset"), json_response('{"ok": 1}')], clock
        )
        assert client.get_json("/thing") == {"ok": 1}

    def test_404_raises_immediately_without_retrying(self, clock):
        """A missing pool will still be missing on the third ask."""
        client, transport = make_client([json_response("", 404)] * 5, clock)
        with pytest.raises(ProviderNotFound):
            client.get_json("/gone")
        assert transport.call_count == 1

    def test_400_is_not_retried(self, clock):
        client, transport = make_client([json_response("bad", 400)] * 5, clock)
        with pytest.raises(ProviderUnavailable):
            client.get_json("/thing")
        assert transport.call_count == 1

    def test_non_json_body_raises_bad_response(self, clock):
        client, _ = make_client([json_response("<html>maintenance</html>")], clock)
        with pytest.raises(ProviderBadResponse, match="not valid JSON"):
            client.get_json("/thing")

    def test_bad_response_message_does_not_dump_the_whole_body(self, clock):
        client, _ = make_client([json_response("x" * 10_000)], clock)
        with pytest.raises(ProviderBadResponse) as exc:
            client.get_json("/thing")
        assert len(str(exc.value)) < 400


class TestRateLimitResponses:
    def test_honours_retry_after_header_exactly(self, clock):
        client, _ = make_client(
            [json_response("slow down", 429, **{"retry-after": "31"}),
             json_response('{"ok": 1}')],
            clock,
        )
        assert client.get_json("/thing") == {"ok": 1}
        assert 31 in clock.slept, f"expected a 31s wait, got {clock.slept}"

    def test_falls_back_to_backoff_without_retry_after(self, clock):
        client, _ = make_client(
            [json_response("slow down", 429), json_response('{"ok": 1}')], clock
        )
        client.get_json("/thing")
        assert clock.total_slept > 0

    def test_persistent_429_raises_with_retry_after_preserved(self, clock):
        client, _ = make_client(
            [json_response("no", 429, **{"retry-after": "5"})] * 10, clock, max_retries=1
        )
        with pytest.raises(ProviderRateLimited) as exc:
            client.get_json("/thing")
        assert exc.value.retry_after_seconds == 5.0
        assert exc.value.provider == "test"

    def test_malformed_retry_after_is_ignored_not_crashed(self, clock):
        client, _ = make_client(
            [json_response("no", 429, **{"retry-after": "tomorrow"}),
             json_response('{"ok": 1}')],
            clock,
        )
        assert client.get_json("/thing") == {"ok": 1}


class TestClientPlumbing:
    def test_error_carries_provider_name(self, clock):
        client, _ = make_client([json_response("", 404)], clock)
        with pytest.raises(ProviderNotFound) as exc:
            client.get_json("/x")
        assert exc.value.provider == "test"
        assert "[test]" in str(exc.value)

    def test_base_url_and_path_join_cleanly(self, clock):
        client, transport = make_client([json_response("{}")], clock)
        client.get_json("/latest/dex/search")
        assert transport.calls[0]["url"] == "https://api.example.invalid/latest/dex/search"

    def test_stats_report_activity(self, clock):
        client, _ = make_client([json_response('{"n": 1}')], clock)
        client.get_json("/thing")
        client.get_json("/thing")
        stats = client.stats()
        assert stats["requests"] == 1
        assert stats["cache_hits"] == 1
        assert stats["errors"] == 0

    def test_user_agent_and_accept_headers_are_sent(self, clock):
        captured = {}

        def transport(url, *, params, timeout, headers):
            captured.update(headers)
            return json_response("{}")

        client = HttpClient(
            name="test", base_url="https://x.invalid", rate_limit_per_min=60,
            transport=transport, clock=clock, sleep=clock.sleep,
        )
        client.get_json("/a")
        assert "MemeCoinIntelligenceHQ" in captured["user-agent"]
        assert captured["accept"] == "application/json"
