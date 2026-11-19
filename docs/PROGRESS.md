# Progress log

Update this at the end of every working session so the next one can continue
without re-deriving context.

---

## Current state: Stage A complete

**Last updated:** 2026-11-15

| Stage | Status |
|---|---|
| A - Project foundation, config, migrations | **Complete and tested** |
| B - Market data providers | Not started - next |
| C - Scanner loops | Not started |
| D - Historical storage + event detection | Not started |
| E - Claude analysis | Not started |
| F - Telegram notifications | Not started |
| G - Daily summary | Not started |
| H - Testing and failure handling | Not started |
| I - Deployment | Not started |

---

## What Stage A built

**Configuration** (`src/mihq/config.py`)
Single loader for every setting, reading `.env` plus real environment
variables. Typed readers give actionable errors (`DRY_RUN must be true or
false, got 'maybe'`). Secrets are masked in `__repr__`, so a stray print or a
traceback cannot leak a credential. Validation is staged, so Stage A requires
only `DATABASE_URL` rather than demanding every credential on day one.

**Logging** (`src/mihq/logging_setup.py`)
Redacts credentials automatically, by exact value *and* by pattern. The
pattern layer matters because the Telegram API puts the bot token in the URL
path, which is the most likely accidental leak.

**Chain registry** (`src/mihq/chains.py`)
All five chains (Solana, Ethereum, Base, BNB Chain, Avalanche) with their
per-provider identifiers, explorer URL templates, and address rules. Adding a
chain is one entry here plus one word in `.env`.

**Database layer** (`src/mihq/db/`)
`connection.py` centralises SSL, timeouts and transaction handling.
`migrate.py` applies numbered SQL files once each, recording a checksum so an
edited-after-apply migration is reported instead of silently ignored.

**Schema** (`migrations/001` - `011`)
All nine required tables plus `schema_migrations`, with indexes, unique
constraints, cascade rules, `updated_at` triggers, and Row Level Security.

**Scripts**
`check_env.py` diagnoses configuration and optionally pings the database.
`run_migrations.py` applies migrations with a `--dry-run` mode.

---

## What has actually been verified

Running it, not assuming it.

**Schema, against a real PostgreSQL 16 server**

- All 11 migrations applied cleanly in order
- All 11 re-applied with no error - safe to run repeatedly
- 10 tables created; RLS enabled on all 10; 0 policies (closed to the public API)
- 18/18 behavioural checks passed:

| # | Check |
|---|---|
| 1 | Duplicate `(chain, address)` token rejected |
| 2 | Same address on a different chain allowed |
| 3 | Invalid category rejected by CHECK |
| 4 | Only one *active* watchlist row per token |
| 5 | Re-add after deactivation works; history retained |
| 6 | `updated_at` trigger advances on UPDATE |
| 7 | Duplicate market observation rejected |
| 8 | Exact numeric price retained (`0.000001234567890123`, no float drift) |
| 9 | All-NULL metric snapshot accepted (never fabricate) |
| 10 | Negative liquidity rejected by CHECK |
| 11 | **Duplicate `event_key` rejected** - the duplicate-alert guard |
| 12 | Pruning a snapshot keeps alert history (`snapshot_id` nulled) |
| 13 | Bidirectional `alert_events` <-> `research_reports` link works |
| 14 | Retry attempts logged; same attempt number twice rejected |
| 15 | Large negative Telegram group chat id stored intact |
| 16 | Only one daily summary per date |
| 17 | `scanner_runs.chains_scanned` array is queryable |
| 18 | Deleting a token cascades all children, no orphans |

- The exact SQL the migration runner issues was also run directly: the
  bootstrap table is idempotent, and recording the same migration twice is
  blocked by the primary key.

**Python: 83 unit tests, all passing** (`python -m pytest -q`)
Chain registry and address normalization (33), configuration and masking (24),
log redaction (11), migration discovery and checksums (15).

**Scripts**
`check_env.py` exercised with no `.env` (exits 1, lists the problem) and with
a full `.env` (exits 0). Confirmed zero secret values appear in its output.

---

## Two bugs found and fixed during verification

Recorded because both were the kind that only surface when you run things.

1. **`create index on research_reports (date(created_at))` was rejected** by
   PostgreSQL: `functions in index expression must be marked IMMUTABLE`.
   `date(timestamptz)` depends on the session timezone, so it cannot be
   indexed. Removed; the daily spend cap now uses a range query on
   `created_at`, which is the correct approach anyway when "today" means a
   local calendar day.

2. **The Telegram token redaction regex never matched the case that matters.**
   The pattern began with `\b`, but in a logged URL like
   `.../bot7584112233:AAH...` there is no word boundary between the `t` of
   `bot` and the first digit - so a logged Telegram URL would have leaked the
   full token. Anchors removed; the character classes already stop at `/`.

---

## Not yet verified

Honest list of what is written but unproven:

- **`src/mihq/db/connection.py` and the database half of `migrate.py`.** The
  SQL they issue has been run and verified; the psycopg plumbing around it has
  not, because `psycopg` could not be installed in the environment where the
  schema was tested. First thing to confirm on your machine:
  `python scripts/check_env.py --ping` then `python scripts/run_migrations.py`.
- **Provider chain identifiers** in `chains.py` - `dexscreener_chain_id` and
  `geckoterminal_network`. These are marked with a verification note in the
  code and will be confirmed against live API responses as the first task of
  Stage B. Internal consistency (uniqueness, round-tripping) is tested.
- **Explorer URL templates** - structurally tested, not fetched.

---

## Decisions made

1. **Scheduling: one long-running Python worker**, not Supabase Edge
   Functions. Edge Functions are TypeScript/Deno, which would mean a second
   language. GitHub Actions is the zero-cost fallback but cannot do a
   1-minute interval. See ARCHITECTURE.md section 2. Nothing in Stage A
   depends on this, so it can still change before Stage C.
2. **Plain SQL with psycopg 3**, not an ORM. One mental model instead of two.
3. **Deduplication via a UNIQUE constraint on a deterministic `event_key`**,
   not a read-then-write check, which has a race condition.
4. **`chain` stored as plain text with no foreign key**, so adding a
   blockchain never requires a migration.
5. **RLS enabled with zero policies** on every table, closing Supabase's
   automatic public REST API while leaving the backend full access.

---

## Open items for Stage D

**Snapshot storage growth.** At 100 watchlist tokens on a 60-second interval,
writing every pass produces ~144,000 rows/day and would exhaust Supabase's
500 MB free tier in under four days. Mitigation plan (write-on-material-change,
selective `raw` storage, scheduled downsampling) is in ARCHITECTURE.md
section 5. The schema already supports all three.

---

## Next session should start with

**Stage B - market data providers.**

1. Call the DEX Screener and GeckoTerminal APIs and confirm the real `chainId`
   and network identifiers against `chains.py`.
2. Build a shared HTTP client with per-provider rate limiting, response
   caching (`PROVIDER_CACHE_TTL_SECONDS`), retry with exponential backoff, and
   explicit handling of 429 responses.
3. Define the normalized `MarketData` shape every adapter returns, with
   missing fields as `None` rather than defaults.
4. Write provider adapters plus tests using recorded API responses, so the
   test suite stays offline and deterministic.

Before that, run on your own machine:

```bash
python scripts/check_env.py --ping
python scripts/run_migrations.py
python -m pytest -q
```
