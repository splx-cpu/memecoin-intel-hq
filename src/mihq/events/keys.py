"""The deterministic event key: the single thing preventing duplicate alerts.

THE PROBLEM
-----------
The watchlist scanner runs every minute. A token whose volume spiked at 04:35
still looks spiked at 04:36, 04:37, 04:38... A naive detector fires on all of
them and sends the same alert sixty times an hour. That is the fastest way to
make a notification system worthless - you stop reading it.

THE SOLUTION
------------
Every event gets a key built deterministically from the facts:

    momentum:base:0xabc...:volume_spike:2026-11-15T04:30
    └──────┘ └──┘ └──────┘ └──────────┘ └──────────────┘
    category chain address   detector    rounded time bucket

Because the same spike produces the same key on every pass within the bucket,
and `alert_events.event_key` carries a UNIQUE constraint, the second and third
inserts fail harmlessly. Only one alert is ever sent.

WHY THE DATABASE DECIDES, NOT THE CODE
--------------------------------------
The obvious alternative - "SELECT to check whether we already alerted, then
INSERT" - has a race condition: two passes can both read "no" before either
writes. Letting a UNIQUE constraint reject the duplicate removes the race
entirely, because the database serialises it for us.

CHOOSING A BUCKET SIZE
----------------------
The bucket is how long a condition stays "the same event". Too short and you
get repeats; too long and a genuinely new spike an hour later is swallowed.
Each detector picks its own:

  - liquidity drain : 10 min  (urgent, want to hear about a second drain)
  - volume spike    : 15 min
  - new token       : once ever (no time component at all)
"""

from __future__ import annotations

from datetime import datetime, timezone

# The four notification types, matching the CHECK constraint on
# alert_events.category and watchlists.category.
CATEGORIES = frozenset({"momentum", "new_coin", "risk", "long_term"})

SEVERITIES = ("info", "low", "medium", "high", "critical")


def time_bucket(moment: datetime, minutes: int) -> str:
    """Round a time DOWN to the nearest `minutes` and format it compactly.

    Rounding down rather than to-nearest is deliberate: it makes the bucket a
    stable function of the timestamp, so two passes a few seconds apart on
    either side of a boundary cannot land in different buckets by accident.

    >>> from datetime import datetime, timezone
    >>> t = datetime(2026, 11, 15, 4, 37, 42, tzinfo=timezone.utc)
    >>> time_bucket(t, 15)
    '2026-11-15T04:30'
    >>> time_bucket(t, 60)
    '2026-11-15T04:00'
    >>> time_bucket(t, 1)
    '2026-11-15T04:37'
    """
    if minutes < 1:
        raise ValueError("bucket minutes must be at least 1")
    moment = moment.astimezone(timezone.utc)
    total = moment.hour * 60 + moment.minute
    floored = (total // minutes) * minutes
    return (
        f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}"
        f"T{floored // 60:02d}:{floored % 60:02d}"
    )


def build_event_key(
    *,
    category: str,
    chain: str,
    token_address: str,
    event_type: str,
    moment: datetime | None = None,
    bucket_minutes: int | None = 15,
) -> str:
    """Compose the dedupe key.

    `bucket_minutes=None` produces a key with no time component, so the event
    can only ever fire once for that token - which is what "we discovered this
    token" means.

    The address is lowercased here ONLY for key purposes. The stored address
    keeps its canonical form from `chains.normalize_address`, because Solana
    addresses are case-sensitive. Two keys differing only by case would defeat
    the whole mechanism, so the key normalises while the data does not.

    >>> from datetime import datetime, timezone
    >>> t = datetime(2026, 11, 15, 4, 37, tzinfo=timezone.utc)
    >>> build_event_key(category="momentum", chain="base",
    ...                 token_address="0xABC", event_type="volume_spike",
    ...                 moment=t, bucket_minutes=15)
    'momentum:base:0xabc:volume_spike:2026-11-15T04:30'
    >>> build_event_key(category="new_coin", chain="solana",
    ...                 token_address="Abc123", event_type="first_seen",
    ...                 bucket_minutes=None)
    'new_coin:solana:abc123:first_seen'
    """
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {sorted(CATEGORIES)}, got {category!r}")
    if not chain or not token_address or not event_type:
        raise ValueError("chain, token_address and event_type are all required")

    parts = [category, chain.lower(), token_address.lower(), event_type]

    if bucket_minutes is not None:
        when = moment or datetime.now(timezone.utc)
        parts.append(time_bucket(when, bucket_minutes))

    return ":".join(parts)
