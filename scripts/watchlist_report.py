#!/usr/bin/env python3
"""Why is the watchlist the size it is?

    python scripts/watchlist_report.py

Reads only. Changes nothing.

WHY THIS EXISTS

The first set of retirement thresholds did not converge - the list went from
1,717 to 1,859 overnight - and it was not obvious which of the two rules was
failing, or whether the inflow was simply too high. Picking new numbers
without knowing that is how the original mismatch got created: an entry floor
of $10,000 and a retirement floor of $2,000, set in different sessions, with
nothing to remove anything in between.

This prints the arithmetic that decides whether the list grows or shrinks, so
the next threshold is chosen rather than guessed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mihq.config import ConfigError, load_settings  # noqa: E402
from mihq.db.connection import connect  # noqa: E402
from mihq.db.repositories import Repository  # noqa: E402
from mihq.events.detectors import FIRST_SEEN_EVENT_TYPE  # noqa: E402


def money(value: float) -> str:
    if value >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:.0f}"


def bar(n: int, total: int, width: int = 26) -> str:
    if total <= 0:
        return ""
    return "#" * max(0, round(n / total * width))


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"\nCannot read settings:\n{exc}\n", file=sys.stderr)
        return 1

    entry = settings.watchlist_min_liquidity_usd
    retire = settings.watchlist_retire_below_liquidity_usd
    grace = settings.watchlist_retire_grace_hours
    window = settings.watchlist_retire_uneventful_hours

    try:
        with connect(settings) as conn:
            repo = Repository(conn)
            d = repo.watchlist_diagnosis(
                grace_hours=grace, retire_floor=retire, entry_floor=entry,
                uneventful_hours=window,
                arrival_event_type=FIRST_SEEN_EVENT_TYPE,
            )
            events = repo.watchlist_event_types(FIRST_SEEN_EVENT_TYPE)
    except Exception as exc:  # noqa: BLE001
        print(f"\nCould not read the database: {exc}\n", file=sys.stderr)
        return 1

    total = d.get("total", 0)
    print()
    print("WATCHLIST")
    print("=" * 60)
    print(f"  {total} active entries")
    print(f"  added above {money(entry)}, retired below {money(retire)} "
          f"after {grace}h, or after {window}h with no events")

    if not total:
        print("\nNothing on the watchlist yet.\n")
        return 0

    print("\n\nWHY EACH ENTRY IS STILL HERE")
    print("=" * 60)

    rows = [
        ("too new to retire", d.get("in_grace", 0),
         f"added less than {grace}h ago"),
        ("liquidity unknown", d.get("unknown_liq", 0),
         "never retired on unknown - may be a pool being pulled"),
        (f"below {money(retire)}", d.get("below_retire", 0),
         "should be retiring on the next pass"),
        (f"{money(retire)} - {money(entry)}", d.get("dead_zone", 0),
         "NOT retired, would NOT be added today"),
        (f"above {money(entry)}", d.get("above_entry", 0),
         "still funded, legitimately watched"),
    ]
    for label, count, note in rows:
        pct = count / total * 100
        print(f"  {label:<20} {count:>6}  {pct:>5.1f}%  {bar(count, total)}")
        print(f"  {'':<20} {'':>6}         {note}")

    print("\n\nTHE UNEVENTFUL RULE")
    print("=" * 60)
    old = d.get("old_enough", 0)
    active = d.get("has_events", 0)
    print(f"  older than {window}h:        {old:>6}  (eligible on age)")
    print(f"  have had a real event:   {active:>6}  (exempt - counted as active)")
    print(f"  retirable by this rule:  {max(0, old - active):>6}")

    if events:
        print("\n  Events keeping tokens exempt:")
        for event_type, count in events[:8]:
            print(f"    {event_type:<22} {count:>6} tokens")
        print("\n  A detector firing for most of the watchlist makes this")
        print("  rule inert. If one dominates, exclude it or shorten the window.")

    print("\n\nWHAT TO CHANGE")
    print("=" * 60)
    dead = d.get("dead_zone", 0)
    if dead > total * 0.15:
        print(f"  {dead} entries ({dead / total * 100:.0f}%) sit between the")
        print(f"  retirement floor and the entry floor. They would not be added")
        print(f"  today and nothing removes them. Raise:")
        print(f"\n      WATCHLIST_RETIRE_BELOW_LIQUIDITY_USD")
        print(f"\n  Keep it below the entry floor so a token hovering at the")
        print(f"  boundary is not added and retired repeatedly - half of the")
        print(f"  entry floor is a reasonable gap.")
    else:
        print("  No large dead zone. If the list is still growing, the inflow")
        print("  is too high: raise WATCHLIST_MIN_LIQUIDITY_USD.")
    print("\n  Set these as GitHub Variables, not just in .env, or the")
    print("  deployed scanner keeps using the defaults.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
