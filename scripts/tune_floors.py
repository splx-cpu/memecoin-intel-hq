#!/usr/bin/env python3
"""Picks your alert thresholds from YOUR data instead of a guess.

RUN IT LIKE THIS (from the project root):

    python scripts/tune_floors.py                # look at the last 7 days
    python scripts/tune_floors.py --days 2       # just the last 2 days

WHY THIS EXISTS

A liquidity floor is a trade-off between noise and coverage, and the right
number depends entirely on what the chains you watch actually produce. A
default chosen by somebody else is a starting point, not an answer.

This reads the new-token alerts already in your database and reports how many
notifications each candidate floor would have sent you. Nothing is changed -
it only reads, and it prints the setting to paste into `.env` when you have
decided.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mihq.config import ConfigError, load_settings  # noqa: E402
from mihq.db.connection import connect  # noqa: E402
from mihq.db.repositories import Repository  # noqa: E402
from mihq.events.detectors import FIRST_SEEN_EVENT_TYPE  # noqa: E402

# Candidate floors, in dollars. Spaced so each step is a meaningful change in
# what survives rather than a rounding difference.
CANDIDATES = [1_000, 2_500, 5_000, 10_000, 15_000, 25_000, 50_000, 100_000]


def _money(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:.0f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7,
                        help="How far back to look. Default 7.")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"\nCannot read settings:\n{exc}\n", file=sys.stderr)
        return 1

    since = datetime.now(timezone.utc) - timedelta(days=args.days)

    try:
        with connect(settings) as conn:
            repo = Repository(conn)
            sample = repo.new_token_liquidity_sample(
                since, arrival_event_type=FIRST_SEEN_EVENT_TYPE)
            watchlist_size = repo.active_watchlist_count()
    except Exception as exc:  # noqa: BLE001
        print(f"\nCould not read the database: {exc}\n", file=sys.stderr)
        return 1

    print()
    print(f"NEW-TOKEN ALERTS, last {args.days} day(s)")
    print("=" * 58)

    if not sample:
        print("\nNo new-token alerts with a recorded liquidity figure yet.")
        print("Let the scanner run for a day, then try again.\n")
        return 0

    total = len(sample)
    per_day = total / max(args.days, 1)
    sample.sort()

    print(f"\n{total} alerts  ({per_day:.0f} per day at the current floor of "
          f"{_money(settings.event_new_token_min_liquidity_usd)})")
    print(f"\nLiquidity of those pools:")
    print(f"  smallest  {_money(sample[0])}")
    print(f"  median    {_money(sample[len(sample) // 2])}")
    print(f"  largest   {_money(sample[-1])}")

    print("\n\nWHAT EACH FLOOR WOULD HAVE SENT YOU")
    print("=" * 58)
    print(f"{'floor':>10}  {'alerts':>7}  {'per day':>8}   ")
    print("-" * 58)

    current = settings.event_new_token_min_liquidity_usd
    for floor in CANDIDATES:
        kept = sum(1 for v in sample if v >= floor)
        marker = "  <- current" if abs(floor - current) < 1 else ""
        bar = "#" * min(28, round(kept / max(total, 1) * 28))
        print(f"{_money(floor):>10}  {kept:>7}  {kept / max(args.days, 1):>8.0f}   "
              f"{bar}{marker}")

    print("\nNote: this counts alerts that ALREADY passed the floor in force")
    print("at the time, so a floor below the current setting cannot show more")
    print("than you already received. Lower it and re-run to see the rest.")

    print(f"\n\nWATCHLIST")
    print("=" * 58)
    print(f"  {watchlist_size} active entries")
    print(f"  retiring below {_money(settings.watchlist_retire_below_liquidity_usd)}"
          f" after {settings.watchlist_retire_grace_hours}h, "
          f"and after {settings.watchlist_retire_uneventful_hours}h with no events")

    print("\n\nTO CHANGE IT")
    print("=" * 58)
    print("Edit .env (local) or add a GitHub Variable (deployed):")
    print("\n    EVENT_NEW_TOKEN_MIN_LIQUIDITY_USD=25000")
    print("\nAim for a number of alerts per day you will actually read.")
    print("Ten you look at beats sixty you swipe away.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
