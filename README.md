# MemeCoin Intelligence HQ

An educational, multi-chain crypto market **research** monitor. It reads public
market data from DEX aggregators, stores a history of observations, asks Claude
to describe what changed, and sends research notifications to Telegram.

## What this project does NOT do

These boundaries are enforced in the code, not just documented:

- No trading, orders, or transactions of any kind
- No wallet connections, private keys, or seed phrases
- No personalized investment advice and no buy/sell signals
- No fabricated data: a value we could not fetch is stored as `NULL` and
  labelled "not available", never estimated

Claude's output separates **verified facts** (backed by fetched data, with a
source link) from **assumptions** (explicitly labelled inferences). Those are
different database columns, so the distinction survives into every alert.

---

## Quick start

You need Python 3.11 or newer. Check with `python --version`.

### 1. Get the code and create a virtual environment

A virtual environment keeps this project's packages separate from everything
else on your computer, so one project can never break another.

```bash
cd memecoin-intel-hq

# Windows (PowerShell)
python -m venv .venv
.venv\Scripts\Activate.ps1

# Mac / Linux
python3 -m venv .venv
source .venv/bin/activate
```

Your prompt should now start with `(.venv)`. Re-activate it every new terminal.

### 2. Install dependencies

```bash
pip install -r requirements-dev.txt
```

### 3. Create your Supabase database

1. Go to <https://supabase.com> and create a free account.
2. Create a new project. Choose a region near you and **save the database
   password it generates** - you cannot see it again later (you can reset it).
3. In the project, click **Connect** at the top, choose the **Session pooler**
   connection string, and copy it. It looks like:
   `postgresql://postgres.abcdefghijkl:YOURPASSWORD@aws-0-us-east-1.pooler.supabase.com:5432/postgres`
4. Replace `YOURPASSWORD` in that string with the real password.

Use the **session pooler** string, not the direct connection. The free tier
limits direct connections, and the pooler survives a scanner that restarts.

### 4. Configure

```bash
# Windows
copy .env.example .env
# Mac / Linux
cp .env.example .env
```

Open `.env` and paste your connection string into `DATABASE_URL`. Everything
else can keep its default for now.

`.env` holds your secrets and is already in `.gitignore`. Never commit it.

### 5. Verify your setup

```bash
python scripts/check_env.py --ping
```

This tells you exactly what is missing or misconfigured, and opens a real
connection to prove the database is reachable. Secrets are shown masked.

### 6. Create the database tables

```bash
python scripts/run_migrations.py --dry-run   # see what would happen
python scripts/run_migrations.py             # apply
```

Safe to run repeatedly - each migration is applied at most once.

### 7. Run the tests

```bash
python -m pytest -q
```

---

## Project structure

```
memecoin-intel-hq/
├── .env                  Your secrets. Never committed.
├── .env.example          Template with every setting explained.
├── requirements.txt      Runtime dependencies.
├── requirements-dev.txt  Adds pytest.
│
├── migrations/           Numbered SQL files. Applied in filename order.
│   ├── 001_foundation.sql       Shared trigger function + conventions.
│   ├── 002_tokens.sql           One row per (chain, token address).
│   ├── 003_market_snapshots.sql Append-only observation history.
│   ├── 004_watchlists.sql       What the 1-minute scanner checks.
│   ├── 005_risk_flags.sql       Observed risk indicators.
│   ├── 006_alert_events.sql     Detected events + the dedupe key.
│   ├── 007_research_reports.sql Claude output + cost tracking.
│   ├── 008_notification_logs.sql Every delivery attempt, incl. retries.
│   ├── 009_daily_summaries.sql  One summary per local day.
│   ├── 010_scanner_runs.sql     Health and observability.
│   └── 011_security_rls.sql     Locks the tables against public API access.
│
├── scripts/
│   ├── check_env.py       Diagnose configuration. Run this first when stuck.
│   └── run_migrations.py  Create or update the database tables.
│
├── src/mihq/
│   ├── config.py          Reads .env. The only place secrets are loaded.
│   ├── logging_setup.py   Logging that redacts credentials automatically.
│   ├── chains.py          Chain registry. Add a blockchain here, nowhere else.
│   └── db/
│       ├── connection.py  How we connect to Postgres.
│       └── migrate.py     Applies migrations, tracks which ones ran.
│
├── tests/                 83 tests, no database required.
└── docs/
    ├── ARCHITECTURE.md    Design decisions and why. Read this first.
    └── PROGRESS.md        Stage status. Update at the end of each session.
```

## Adding a blockchain

Add one entry to `CHAINS` in `src/mihq/chains.py`, then add its key to
`ENABLED_CHAINS` in `.env`. No scanner code changes, and no migration.

## Common commands

| What you want | Command |
|---|---|
| Check configuration | `python scripts/check_env.py --ping` |
| List migrations | `python scripts/run_migrations.py --list` |
| Apply migrations | `python scripts/run_migrations.py` |
| Run tests | `python -m pytest -q` |
| Check Telegram + Claude config too | `python scripts/check_env.py --stage all` |

## Status

Stage A (foundation and database) is complete and tested. See
`docs/PROGRESS.md` for exactly what is verified and what comes next.
