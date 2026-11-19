# Architecture

Design decisions for MemeCoin Intelligence HQ, and the reasoning behind each
one. Read this before changing the stack.

---

## 1. Data flow

```
          ┌──────────────────────────────────────────────┐
          │  DEX Screener API        GeckoTerminal API   │   public, free
          └────────────────┬─────────────────────────────┘
                           │  rate limited, cached, retried
                           ▼
          ┌──────────────────────────────────────────────┐
          │  Chain adapters  →  normalized MarketData    │   src/mihq/chains.py
          └────────────────┬─────────────────────────────┘
                           ▼
   ┌───────────────────────────────┐   ┌──────────────────────────────┐
   │ DISCOVERY SCANNER  (~5 min)   │   │ WATCHLIST SCANNER  (~1 min)  │
   │ finds newly listed tokens     │   │ re-checks tracked tokens     │
   └──────────────┬────────────────┘   └───────────────┬──────────────┘
                  └────────────────┬───────────────────┘
                                   ▼
          ┌──────────────────────────────────────────────┐
          │  Postgres (Supabase)                         │
          │  tokens · market_snapshots · watchlists      │
          │  risk_flags · scanner_runs                   │
          └────────────────┬─────────────────────────────┘
                           ▼
          ┌──────────────────────────────────────────────┐
          │  EVENT DETECTION  →  alert_events            │
          │  deterministic event_key = dedupe guard      │
          └────────────────┬─────────────────────────────┘
                           │  only qualifying events continue
                           ▼
          ┌──────────────────────────────────────────────┐
          │  CLAUDE ANALYSIS  →  research_reports        │
          │  verified facts vs assumptions, separately   │
          └────────────────┬─────────────────────────────┘
                           ▼
          ┌──────────────────────────────────────────────┐
          │  TELEGRAM  →  notification_logs              │
          │  4 alert types + daily summary + health      │
          └──────────────────────────────────────────────┘
```

Each arrow writes to the database before the next step runs. That is what lets
a crashed process resume instead of re-sending alerts it already sent.

---

## 2. Decision: how the scanners get scheduled

The spec mentions Supabase Edge Functions and Cron. Here are the three real
options and why the recommendation differs.

### Option A - one long-running Python worker  ← **RECOMMENDED**

A single process holds both loops: discovery on a 5-minute timer, watchlist on
a 1-minute timer. Deployed as one container on Railway, Render, or Fly.io.

- **Why:** it is the only option that actually delivers the ~1-minute
  watchlist interval the spec asks for. It is also one language (Python), one
  codebase, one log stream, and an in-memory provider cache that works.
- **Cost:** roughly $5/month, and some providers have a free tier that fits.
- **Trade-off:** you are running a server, so you need the health monitoring
  from section 10 of the spec. That is already designed in (`scanner_runs`).

### Option B - GitHub Actions on a cron schedule

- **Why consider it:** free, no server, and you have already set this pattern
  up before, so there is nothing new to learn.
- **Why it is the fallback, not the pick:** the shortest GitHub Actions cron
  interval is 5 minutes, and scheduled runs are frequently delayed under load
  - sometimes 10+ minutes. A one-minute watchlist is not achievable. Each run
  also pays cold-start and dependency-install time, and an in-memory cache is
  useless across separate runs.
- **If you choose this:** set `WATCHLIST_INTERVAL_SECONDS=300` and accept
  5-minute (realistically 5-15 minute) resolution.

### Option C - Supabase Edge Functions + pg_cron

- **Why it loses:** Edge Functions are TypeScript running on Deno. Taking this
  path means writing the scanner twice, or writing the whole thing in a second
  language you have not asked to learn. It also contradicts the project's own
  rule that the backend is Python.

**Decision: Option A, with Option B as a zero-cost fallback.** Supabase is
used for PostgreSQL only. Nothing in Stage A depends on this choice, so it can
still change before Stage C without rework.

---

## 3. Decision: plain SQL with psycopg, not an ORM

An ORM such as SQLAlchemy adds a second model of the database on top of SQL.
Since the spec requires real migrations, you are writing SQL regardless - so
one mental model is better than two. What you type in the Supabase SQL editor
is what you type in the code.

`psycopg[binary]` ships a prebuilt wheel, so there is no compiler to install
on Windows.

---

## 4. Decision: how duplicate alerts are prevented

This is the most important design idea in the project, because duplicate
Telegram spam is what makes a scanner unusable.

Every detected event gets a deterministic `event_key` built from the facts:

```
momentum:base:0xabc123...:volume_spike:2026-11-15T04:35
└──────┘ └──┘ └─────────┘ └──────────┘ └──────────────┘
category chain  address     detector     rounded time bucket
```

`alert_events.event_key` has a UNIQUE constraint. The same spike detected on
three consecutive one-minute passes generates the *same* key, so the second
and third inserts fail harmlessly and only one message is ever sent.

The alternative - querying "have I already alerted on this?" before inserting
- has a race condition: two passes can both read "no" before either writes.
Letting the database's unique constraint decide removes the race entirely.

The time bucket is what makes a *genuinely new* spike an hour later alert
again, instead of being suppressed forever.

---

## 5. Open problem: snapshot storage growth

**Flagging this now because it changes Stage D's design.**

Supabase's free tier gives 500 MB of database storage. A naive implementation
that writes one snapshot per token per pass would consume it in days:

| Watchlist size | Interval | Rows/day | With `raw` JSON (~1 KB) |
|---|---|---|---|
| 40 tokens | 60s | 57,600 | ~58 MB/day |
| 100 tokens | 60s | 144,000 | ~144 MB/day |

At 100 tokens you exhaust the free tier in under four days.

Three mitigations, to be implemented in Stage D:

1. **Write on material change only.** Persist a snapshot when a metric moves
   past a threshold (say 2% on price, 5% on liquidity), plus a guaranteed
   heartbeat snapshot every N minutes per token so the history has no gaps.
   This alone cuts volume by roughly 80-90% in quiet markets.
2. **Store `raw` selectively.** The provider payload is the bulk of each row.
   Keep it only for snapshots attached to an `alert_event`, where it is the
   audit trail that matters. Elsewhere store the parsed columns only.
3. **Downsample on a schedule.** Keep 1-minute resolution for 48 hours, then
   roll older rows up to hourly averages and delete the originals.

The schema already supports all three: `market_snapshots` is append-only with
an index on `(token_id, captured_at desc)`, and `alert_events.snapshot_id` is
`ON DELETE SET NULL` - verified by test, so deleting old snapshots never
destroys alert or research history.

---

## 6. Cost model

| Component | Cost |
|---|---|
| Supabase Postgres | Free tier (500 MB). Paid tier if you outgrow it. |
| DEX Screener API | Free, no key required |
| GeckoTerminal API | Free public tier, no key required |
| Telegram Bot API | Free |
| **Claude API** | **Usage-based - the only metered cost** |
| Hosting (Option A) | $0-5/month depending on provider |

Claude is the only thing that costs money per use, so spending is controlled in
three places:

1. **Gating.** Only events that pass detection thresholds are analyzed at all.
   Most market noise never reaches the API.
2. **A hard daily cap.** `CLAUDE_MAX_ANALYSES_PER_DAY` (default 40) is checked
   before every call. Exceeding it skips analysis and logs the skip rather
   than quietly spending more.
3. **Per-call accounting.** `research_reports` records `input_tokens`,
   `output_tokens` and `cost_usd` for every call, so actual spend is a single
   SQL query rather than a surprise on the invoice.

Check current per-token rates at <https://www.anthropic.com/pricing> - they
change, so the code records token counts and leaves the rate configurable
rather than hardcoding a price.

---

## 7. Security model

| Requirement | How it is met |
|---|---|
| Credentials in environment secrets | `src/mihq/config.py` is the only loader; `.env` is gitignored |
| No hardcoded bot tokens | A test asserts no credential-shaped strings exist in the migrations |
| Credentials never in logs | `SecretRedactingFilter` scrubs both known secret values and credential *patterns*; 11 tests cover it |
| Restrict administrative operations | Row Level Security enabled on every table with zero policies - see below |
| No wallet access | No wallet, signing, or key-handling code exists anywhere in the project |

**On Row Level Security:** every Supabase project automatically exposes a REST
API over your tables, reachable with the "anon" key - which is designed to be
embedded in front-end code and should be treated as public. Migration
`011_security_rls.sql` enables RLS on all ten tables and creates *no policies*,
which closes them completely to that API. The backend still has full access
because it connects over direct PostgreSQL as the table owner, and owners are
exempt from their own tables' RLS.

When you build the dashboard, do not disable this. Either read through the
Python backend, or add narrow read-only policies for exactly what the
dashboard needs.

---

## 8. Schema conventions

| Convention | Reason |
|---|---|
| `timestamptz` everywhere, never `timestamp` | Plain `timestamp` silently discards the offset. A test enforces this. |
| `numeric`, never `float`/`double precision` | Floats cannot represent decimal prices exactly; a price of `0.000001234567890123` must round-trip unchanged. Tested. |
| `NULL` means "not available" | The spec forbids fabricating data, so zero is reserved for a genuine zero. Nearly every market metric is nullable. |
| `raw jsonb` on provider-sourced tables | Lets a parsing change be re-applied later without re-querying, and proves what the source actually said. |
| Append-only history | `market_snapshots` and `research_reports` are never updated, so research history cannot be overwritten. |
| Partial unique indexes | e.g. one *active* watchlist row per token, while historical inactive rows are preserved. A plain UNIQUE would forbid that. |
| `chain` as plain text, no FK | Adding a blockchain must need only a registry entry, never a migration. |
