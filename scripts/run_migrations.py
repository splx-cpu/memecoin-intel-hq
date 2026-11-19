#!/usr/bin/env python3
"""Creates or updates the database tables.

RUN IT LIKE THIS (from the project root):

    python scripts/run_migrations.py --dry-run   # show what WOULD happen
    python scripts/run_migrations.py             # actually apply

Safe to run as many times as you like: each migration is applied at most once.
See src/mihq/db/migrate.py for how that is tracked.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mihq.config import ConfigError, load_and_require  # noqa: E402
from mihq.db.migrate import discover_migrations, run_migrations  # noqa: E402
from mihq.logging_setup import setup_logging  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply database migrations.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the migrations that would be applied, and change nothing.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Just show the migration files found on disk, no database needed.",
    )
    args = parser.parse_args()

    if args.list:
        print("Migration files found (applied in this order):\n")
        for migration in discover_migrations():
            print(f"  {migration.filename}   sha256:{migration.checksum[:12]}")
        return 0

    try:
        settings = load_and_require(stages=("database",))
    except ConfigError as exc:
        print(f"\nCannot continue:\n{exc}\n")
        return 1

    setup_logging(settings)

    mode = "DRY RUN - nothing will be written" if args.dry_run else "APPLYING MIGRATIONS"
    print("=" * 68)
    print(f"  {mode}")
    print("=" * 68)

    try:
        result = run_migrations(settings, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001
        print(f"\nMigration failed: {exc}\n")
        print("Nothing from the failing file was applied - the transaction was")
        print("rolled back, so the database is still in a consistent state.")
        return 1

    print()
    if result.skipped:
        print(f"Already applied ({len(result.skipped)}):")
        for name in result.skipped:
            print(f"  - {name}")
    if result.applied:
        verb = "Would apply" if args.dry_run else "Applied"
        print(f"\n{verb} ({len(result.applied)}):")
        for name in result.applied:
            print(f"  + {name}")
    if result.modified:
        print(f"\nWARNING - these files changed after being applied ({len(result.modified)}):")
        for name in result.modified:
            print(f"  ! {name}")
        print("\n  The database still has the OLD version of these changes.")
        print("  Do not edit an applied migration. Add a new numbered file instead.")

    if not result.applied and not result.modified:
        print("\nDatabase is already up to date.")

    print("\n" + "=" * 68)
    if args.dry_run and result.applied:
        print("  Dry run complete. Re-run without --dry-run to apply.")
    else:
        print("  Done.")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
