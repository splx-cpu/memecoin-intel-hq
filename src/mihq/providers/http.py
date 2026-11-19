"""Shared HTTP client: rate limiting, caching, retries, backoff.

WHY THIS FILE EXISTS
--------------------
DEX Screener and GeckoTerminal are free services. If we hammer them we get
rate-limited, then blocked, and the project stops working. Being polite is not
optional, and it is far too easy to forget in one adapter out of two.

So every outbound call goes through `HttpClient`, which enforces four things:

  1. RATE LIMIT  - never exceed N requests per minute, per provider.
  2. CACHE       - an identical request within a few seconds reuses the first
                   response instead of asking again.
  3. RETRY       - transient failures (timeouts, 5xx) are retried with
                   exponential backoff plus jitter.
  4. RESPECT 429 - if the provider says "retry after 31 seconds", we wait
                   exactly that long rather than guessing.

TESTABILITY
-----------
The clock, the sleep function and the transport are all injectable. That means
the tests below run in microseconds with no network and no real waiting, while
production uses the real ones. Timing code that can only be tested by actually
waiting tends to go untested.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from mihq.logging_setup import get_logger
from mihq.providers.errors import (
    ProviderBadResponse,
    ProviderNotFound,
    ProviderRateLimited,
    ProviderUnavailable,
)

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Transport: the only part that actually touches the network.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HttpResponse:
    """What a transport returns. Deliberately tiny and easy to fake in tests."""

    status_code: int
    text: str
    headers: Mapping[str, str] = field(default_factory=dict)


class Transport(Protocol):
    """Anything that can perform a GET. Real one uses `requests`."""

    def __call__(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None,
        timeout: float,
        headers: Mapping[str, str],
    ) -> HttpResponse: ...


class TransportError(Exception):
    """Raised by a transport for a network-level failure (DNS, timeout, reset).

    Kept separate from HTTP status codes: a 500 means we reached the server, a
    TransportError means we did not.
    """


def requests_transport(
    url: str,
    *,
    params: Mapping[str, Any] | None,
    timeout: float,
    headers: Mapping[str, str],
) -> HttpResponse:
    """The production transport, backed by the `requests` library."""
    try:
        import requests  # noqa: PLC0415 - imported lazily so tests need no network stack
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise TransportError(
            "The 'requests' package is not installed. Run: pip install -r requirements.txt"
        ) from exc

    try:
        response = requests.get(url, params=params, timeout=timeout, headers=dict(headers))
    except Exception as exc:  # noqa: BLE001 - every requests failure is a transport failure
        raise TransportError(f"{type(exc).__name__}: {exc}") from exc

    return HttpResponse(
        status_code=response.status_code,
        text=response.text,
        headers={k.lower(): v for k, v in response.headers.items()},
    )


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class RateLimiter:
    """Token bucket: at most `per_minute` requests in any rolling minute.

    HOW A TOKEN BUCKET WORKS
    ------------------------
    Picture a bucket holding at most `per_minute` tokens. It refills steadily
    at `per_minute / 60` tokens per second. Each request takes one token; if
    the bucket is empty, the caller waits until one has refilled.

    Why this and not "sleep 60/N seconds between calls": the bucket allows a
    short burst when we have been idle (which is the common case for a scanner
    that wakes up, does a batch, then sleeps) while still holding the long-run
    average under the limit. Fixed sleeping would make every batch needlessly
    slow.
    """

    def __init__(
        self,
        per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if per_minute < 1:
            raise ValueError("per_minute must be at least 1")
        self.per_minute = per_minute
        self.capacity = float(per_minute)
        self.refill_per_second = per_minute / 60.0
        self._tokens = float(per_minute)   # start full: an idle scanner may burst
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self.total_waited_seconds = 0.0    # surfaced in scanner_runs.details
        self.total_requests = 0

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_second)

    def acquire(self) -> float:
        """Take one token, waiting if necessary. Returns seconds waited."""
        self._refill()
        waited = 0.0
        if self._tokens < 1.0:
            needed = (1.0 - self._tokens) / self.refill_per_second
            self._sleep(needed)
            waited = needed
            self.total_waited_seconds += needed
            self._refill()
        # Spend the token. max(0,...) guards against float dust after a refill.
        self._tokens = max(0.0, self._tokens - 1.0)
        self.total_requests += 1
        return waited

    def pause_for(self, seconds: float) -> None:
        """Empty the bucket and wait, after the provider returned a 429.

        Draining the tokens matters: without it, the burst allowance would let
        the next few calls fire immediately and earn another 429.
        """
        if seconds > 0:
            self._sleep(seconds)
            self.total_waited_seconds += seconds
        self._tokens = 0.0
        self._last = self._clock()


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

class TTLCache:
    """Remembers a response for a few seconds, keyed by URL and parameters.

    Both scanners can want the same token in the same moment (discovery finds
    it; the watchlist re-checks it). Without this, that is two identical calls
    a fraction of a second apart - wasted quota for identical data.
    """

    def __init__(self, ttl_seconds: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl = max(0.0, ttl_seconds)
        self._clock = clock
        self._entries: dict[str, tuple[float, Any]] = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Any | None:
        if self.ttl <= 0:
            self.misses += 1
            return None
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        expires_at, value = entry
        if self._clock() >= expires_at:
            del self._entries[key]
            self.misses += 1
            return None
        self.hits += 1
        return value

    def set(self, key: str, value: Any) -> None:
        if self.ttl > 0:
            self._entries[key] = (self._clock() + self.ttl, value)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------

# Status codes worth retrying. 5xx means the server had a bad moment;
# 408 and 425 are request-timing problems. A 400 or 404 will never improve
# by asking again, so those are raised immediately.
RETRYABLE_STATUS = frozenset({408, 425, 500, 502, 503, 504})

DEFAULT_USER_AGENT = "MemeCoinIntelligenceHQ/0.1 (educational research project)"


class HttpClient:
    """Polite JSON GET client for one provider."""

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        rate_limit_per_min: int,
        cache_ttl_seconds: float = 20.0,
        timeout_seconds: float = 15.0,
        max_retries: int = 3,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout_seconds
        self.max_retries = max(0, max_retries)
        self._transport = transport or requests_transport
        self._sleep = sleep
        self._jitter = jitter
        self._user_agent = user_agent
        self.limiter = RateLimiter(rate_limit_per_min, clock=clock, sleep=sleep)
        self.cache = TTLCache(cache_ttl_seconds, clock=clock)
        self.request_count = 0
        self.error_count = 0

    # -- helpers ---------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    @staticmethod
    def _cache_key(url: str, params: Mapping[str, Any] | None) -> str:
        if not params:
            return url
        ordered = "&".join(f"{k}={params[k]}" for k in sorted(params))
        return f"{url}?{ordered}"

    def _backoff_seconds(self, attempt: int) -> float:
        """Exponential backoff with jitter: ~1s, ~2s, ~4s, capped at 30s.

        The jitter matters. Without it, several tokens failing at the same
        moment would retry in perfect lockstep and hit the provider as a
        synchronised burst - the 'thundering herd' problem.
        """
        base = min(2.0 ** attempt, 30.0)
        return base * (0.5 + 0.5 * self._jitter())

    @staticmethod
    def _parse_retry_after(headers: Mapping[str, str]) -> float | None:
        """Read a Retry-After header. Seconds form only; HTTP-date is ignored."""
        raw = headers.get("retry-after") or headers.get("Retry-After")
        if raw is None:
            return None
        try:
            return max(0.0, float(str(raw).strip()))
        except ValueError:
            return None

    # -- the one public method -------------------------------------------

    def get_json(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        allow_cache: bool = True,
    ) -> Any:
        """GET a JSON document, politely.

        Raises
        ------
        ProviderNotFound      404, or the provider said the resource is gone
        ProviderRateLimited   429 that survived our retries
        ProviderUnavailable   network failure or 5xx after all retries
        ProviderBadResponse   reached the provider, but the body was not JSON
        """
        url = self._url(path)
        key = self._cache_key(url, params)

        if allow_cache:
            cached = self.cache.get(key)
            if cached is not None:
                log.debug("cache hit", extra={"provider": self.name, "url": url})
                return cached

        last_error: Exception | None = None

        # attempt 0 is the first try; the rest are retries.
        for attempt in range(self.max_retries + 1):
            self.limiter.acquire()
            self.request_count += 1

            try:
                response = self._transport(
                    url,
                    params=params,
                    timeout=self.timeout,
                    headers={"accept": "application/json", "user-agent": self._user_agent},
                )
            except TransportError as exc:
                self.error_count += 1
                last_error = exc
                log.warning(
                    "transport failure",
                    extra={"provider": self.name, "url": url, "attempt": attempt + 1,
                           "error": str(exc)},
                )
                if attempt < self.max_retries:
                    self._sleep(self._backoff_seconds(attempt))
                    continue
                raise ProviderUnavailable(
                    f"network failure after {attempt + 1} attempt(s): {exc}",
                    provider=self.name,
                ) from exc

            status = response.status_code

            if status == 429:
                self.error_count += 1
                retry_after = self._parse_retry_after(response.headers)
                wait = retry_after if retry_after is not None else self._backoff_seconds(attempt)
                log.warning(
                    "rate limited by provider",
                    extra={"provider": self.name, "url": url, "attempt": attempt + 1,
                           "retry_after": retry_after, "waiting": round(wait, 2)},
                )
                if attempt < self.max_retries:
                    self.limiter.pause_for(wait)
                    continue
                raise ProviderRateLimited(
                    f"rate limited after {attempt + 1} attempt(s)",
                    provider=self.name,
                    retry_after_seconds=retry_after,
                )

            if status == 404:
                self.error_count += 1
                raise ProviderNotFound(f"not found: {url}", provider=self.name)

            if status in RETRYABLE_STATUS:
                self.error_count += 1
                last_error = ProviderUnavailable(
                    f"server error {status}", provider=self.name, status_code=status
                )
                log.warning(
                    "retryable server error",
                    extra={"provider": self.name, "url": url, "status": status,
                           "attempt": attempt + 1},
                )
                if attempt < self.max_retries:
                    self._sleep(self._backoff_seconds(attempt))
                    continue
                raise last_error

            if status >= 400:
                # 400, 401, 403 and friends will not improve on retry.
                self.error_count += 1
                raise ProviderUnavailable(
                    f"request rejected with status {status}",
                    provider=self.name,
                    status_code=status,
                )

            try:
                payload = json.loads(response.text)
            except (ValueError, TypeError) as exc:
                self.error_count += 1
                preview = (response.text or "")[:200]
                raise ProviderBadResponse(
                    f"response was not valid JSON. First 200 chars: {preview!r}",
                    provider=self.name,
                    status_code=status,
                ) from exc

            if allow_cache:
                self.cache.set(key, payload)
            return payload

        # Only reachable if max_retries was somehow negative.
        raise ProviderUnavailable(
            f"request failed: {last_error}", provider=self.name
        )

    def stats(self) -> dict[str, Any]:
        """Counters for `scanner_runs.details`, so health is measurable."""
        return {
            "provider": self.name,
            "requests": self.request_count,
            "errors": self.error_count,
            "cache_hits": self.cache.hits,
            "cache_misses": self.cache.misses,
            "rate_limit_waited_seconds": round(self.limiter.total_waited_seconds, 3),
        }
