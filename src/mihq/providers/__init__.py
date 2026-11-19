"""Market data providers.

Each provider module knows how to talk to ONE external API and how to turn its
response into the common `MarketObservation` shape from `mihq.models`. Nothing
outside this package should ever see a provider's raw field names.

`http.py` holds the shared client: rate limiting, caching, retries. Every
provider uses it, so politeness toward the APIs is enforced in one place
rather than remembered in each adapter.
"""
