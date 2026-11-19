"""Typed errors for provider calls.

WHY TYPED ERRORS
----------------
The scanner reacts differently depending on what went wrong:

  - rate limited      -> back off, try this token again next pass
  - provider down     -> skip this provider, maybe use the other one
  - bad response      -> log loudly; our parser may be out of date
  - not found         -> the pool is gone; stop watching this token

A bare `Exception` forces the caller to read error strings to decide. These
classes let it branch on type instead.
"""

from __future__ import annotations


class ProviderError(Exception):
    """Base class for every provider failure.

    `provider` is the short name ('dexscreener'), so log lines and the
    `scanner_runs.details` JSON can attribute the failure without parsing text.
    """

    def __init__(self, message: str, *, provider: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code

    def __str__(self) -> str:
        base = super().__str__()
        if self.status_code is not None:
            return f"[{self.provider}] {base} (HTTP {self.status_code})"
        return f"[{self.provider}] {base}"


class ProviderRateLimited(ProviderError):
    """HTTP 429. We asked too fast, or the provider tightened its limits.

    `retry_after_seconds` carries the provider's own Retry-After value when it
    sends one, so we wait exactly as long as asked instead of guessing.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message, provider=provider, status_code=429)
        self.retry_after_seconds = retry_after_seconds


class ProviderUnavailable(ProviderError):
    """Network failure, timeout, or a 5xx that survived every retry.

    Expected occasionally. The scanner records it and carries on rather than
    treating it as a crash - a free public API will have bad minutes.
    """


class ProviderBadResponse(ProviderError):
    """We reached the provider, but the body was not what we expected.

    Usually means the provider changed its schema and our parser needs
    updating. Worth alerting on, unlike a transient outage.
    """


class ProviderNotFound(ProviderError):
    """HTTP 404, or an empty result where exactly one was expected.

    For a watchlist token this usually means its pool was removed, which is
    itself research-relevant information rather than an error to hide.
    """

    def __init__(self, message: str, *, provider: str) -> None:
        super().__init__(message, provider=provider, status_code=404)
