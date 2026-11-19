#!/usr/bin/env python3
"""Checks your setup and tells you exactly what is missing.

RUN IT LIKE THIS (from the project root):

    python scripts/check_env.py                # checks database config only
    python scripts/check_env.py --stage all    # checks database + telegram + claude
    python scripts/check_env.py --ping         # also tries to connect to the DB

This is the first thing to run when something is not working. It never prints
a secret: values are shown masked, as `sk-a...(108 chars)`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `import mihq` work when running this file directly, without installing.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mihq.chains import all_chain_keys, enabled_chains  # noqa: E402
from mihq.config import (  # noqa: E402
    DEFAULT_ENV_PATH,
    REQUIRED_BY_STAGE,
    SECRET_FIELDS,
    ConfigError,
    load_settings,
    mask,
    validate_settings,
)

TICK = "[ ok ]"
CROSS = "[fail]"
WARN = "[warn]"


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify MemeCoin Intelligence HQ configuration.")
    parser.add_argument(
        "--stage",
        default="database",
        help="Which credentials to require: database, telegram, claude, or all. "
             "Default: database (what Stage A needs).",
    )
    parser.add_argument(
        "--ping",
        action="store_true",
        help="Also open a real connection to the database to prove it works.",
    )
    args = parser.parse_args()

    if args.stage == "all":
        stages = tuple(REQUIRED_BY_STAGE)
    else:
        stages = tuple(s.strip() for s in args.stage.split(",") if s.strip())

    print("=" * 68)
    print("  MemeCoin Intelligence HQ - configuration check")
    print("=" * 68)

    # --- 1. Does a .env file exist at all? ---
    if DEFAULT_ENV_PATH.exists():
        print(f"{TICK} Found .env at {DEFAULT_ENV_PATH}")
    else:
        print(f"{WARN} No .env file at {DEFAULT_ENV_PATH}")
        print("       Create one by copying the template:")
        print("           Windows:  copy .env.example .env")
        print("           Mac/Linux: cp .env.example .env")
        print("       (Real environment variables are still read, so this is")
        print("        fine on a hosting platform or in GitHub Actions.)")

    # --- 2. Can we parse it? ---
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"\n{CROSS} A value in .env could not be read:\n    {exc}")
        return 1

    # --- 3. Secrets: present or absent, never printed ---
    print("\n--- Credentials ---")
    for name in sorted(SECRET_FIELDS | {"telegram_chat_id", "telegram_admin_chat_id"}):
        value = getattr(settings, name, None)
        label = name.upper()
        required_here = any(name in REQUIRED_BY_STAGE.get(s, ()) for s in stages)
        if value:
            print(f"{TICK} {label:<26} {mask(value)}")
        elif required_here:
            print(f"{CROSS} {label:<26} missing (required for: {args.stage})")
        else:
            print(f"{WARN} {label:<26} not set (needed in a later stage)")

    # --- 4. Everything else ---
    print("\n--- Settings ---")
    try:
        specs = enabled_chains(settings.enabled_chains)
        chain_list = ", ".join(s.display_name for s in specs)
        print(f"{TICK} Chains ({len(specs)}):            {chain_list}")
    except Exception as exc:  # noqa: BLE001
        print(f"{CROSS} Chains:                    {exc}")
        print(f"       Valid keys: {', '.join(all_chain_keys())}")

    print(f"{TICK} Discovery scan every:      {settings.discovery_interval_seconds}s")
    print(f"{TICK} Watchlist scan every:      {settings.watchlist_interval_seconds}s")
    print(f"{TICK} Timezone:                  {settings.timezone}")
    print(f"{TICK} Daily summary at:          "
          f"{settings.daily_summary_hour:02d}:{settings.daily_summary_minute:02d} local")
    print(f"{TICK} Claude daily cap:          {settings.claude_max_analyses_per_day} analyses")
    print(f"{TICK} Log level / format:        {settings.log_level} / {settings.log_format}")

    if settings.dry_run:
        print(f"{WARN} DRY_RUN is ON - no Telegram messages and no Claude calls will be made.")
    else:
        print(f"{TICK} DRY_RUN is off - notifications and Claude calls are live.")

    # --- 5. Validation problems ---
    problems = validate_settings(settings, stages)
    print("\n--- Validation ---")
    if problems:
        for problem in problems:
            print(f"{CROSS} {problem}")
    else:
        print(f"{TICK} No problems found for stage(s): {', '.join(stages)}")

    # --- 6. Optional live database check ---
    if args.ping:
        print("\n--- Database connection ---")
        if not settings.database_url:
            print(f"{CROSS} Cannot ping: DATABASE_URL is not set.")
            problems.append("DATABASE_URL missing")
        else:
            try:
                from mihq.db.connection import ping

                info = ping(settings)
                print(f"{TICK} Connected. Server: {info['server_version']}")
                print(f"{TICK} Server time: {info['server_time']}")
            except Exception as exc:  # noqa: BLE001
                print(f"{CROSS} Connection failed:\n    {exc}")
                problems.append("database unreachable")

    print("\n" + "=" * 68)
    if problems:
        print(f"  RESULT: {len(problems)} problem(s) to fix. See the [fail] lines above.")
        print("=" * 68)
        return 1
    print("  RESULT: configuration looks good.")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
