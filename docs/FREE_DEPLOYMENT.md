# Running this for $0/month

Every component is on a free tier. This document is the whole cost story.

| Component | Cost | What makes it free |
|---|---|---|
| Supabase PostgreSQL | $0 | Free tier, 500 MB. The write policy keeps us ~173 days per fill. |
| DEX Screener API | $0 | Public, no key, 300 req/min |
| GeckoTerminal API | $0 | Public, no key, ~30 calls/min |
| Telegram Bot API | $0 | Always free |
| GitHub Actions | $0 | **Unlimited minutes on a public repository** |
| Analysis | $0 | `ANALYSIS_MODE=template` needs no API key |

**Total: $0/month, with no trial that expires.**

---

## The one setting that decides whether this costs money

`ANALYSIS_MODE` in `.env`:

- `template` (default) - builds the research report from market data already
  collected. No API key, no network call, no cost. It states every number
  with its source, lists the risk flags with their evidence, and says plainly
  what could not be checked. It makes **no inferences**, so `assumptions` is
  always empty - that is accurate rather than a limitation being hidden.
- `claude` - adds natural-language explanation. Costs money per event.

Switching to `claude` later is one line in `.env` plus a key. Nothing else
changes, and `CLAUDE_MAX_ANALYSES_PER_DAY` caps the spend.

---

## Why a public repository

GitHub gives **unlimited** Actions minutes to public repos. Private repos get
2,000 minutes/month free, which at ~1 minute per run is about one scan every
22 minutes - too slow to be useful.

**Public does not mean your credentials are public.** They live in
Settings -> Secrets and variables -> Actions, which is encrypted and never
visible in the repository, in logs, or to forks. `.gitignore` already
excludes `.env`, and the Tests workflow fails the build if anything
credential-shaped is ever committed.

What IS public: the code, the migrations, and the fact that you built this.
None of that is sensitive.

---

## The trade-off you are accepting

GitHub Actions cron has a **5-minute minimum**, and scheduled runs are
frequently delayed when GitHub is busy - 5 to 15 minutes late is normal and
not a bug.

So:

| | Designed | On free GitHub Actions |
|---|---|---|
| Watchlist | every 1 min | every 5-15 min |
| Discovery | every 5 min | every 15-20 min |

For educational research this is fine. What it is NOT suitable for is
anything time-critical - and since this project explicitly does not trade,
nothing here is time-critical.

**If you later want true 1-minute scanning**, run `python -m mihq.worker` as
a long-lived process on any always-on machine. The code is identical; only
the entrypoint differs. Options: an Oracle Cloud always-free ARM VM ($0
forever, ~30-60 min setup), or Railway/Render at roughly $5/month.

---

## Setup, in order

1. **Create the GitHub repository** (public).
2. **Push this project to it.**
3. **Add secrets** - Settings -> Secrets and variables -> Actions -> New
   repository secret:
   - `DATABASE_URL` - your Supabase session pooler string
   - `TELEGRAM_BOT_TOKEN` - from @BotFather (needed from Stage F)
   - `TELEGRAM_CHAT_ID` - your chat id (needed from Stage F)
   - `ANTHROPIC_API_KEY` - **leave this out entirely** unless you switch to
     `claude` mode
4. **Add variables** (same page, Variables tab) - these are not secret:
   - `ENABLED_CHAINS` = `solana,base` (fewer chains = fewer API calls; add
     more once it is running well)
   - `ANALYSIS_MODE` = `template`
5. **Run one by hand** - Actions tab -> Discovery scan -> Run workflow. Check
   the log, then check your Supabase `tokens` table has rows.
6. **Let the schedule take over.**

> GitHub disables scheduled workflows on a repository with no activity for 60
> days. A single commit re-enables them.

---

## Efficiency measures already built in

These are why the free API tiers are comfortable rather than tight:

- **Batching** - 30 pool addresses per DEX Screener request, so 120 watched
  tokens cost 4 calls, not 120.
- **Rate limiting** - a token bucket per provider, well under each published
  limit.
- **Response caching** - identical requests within `PROVIDER_CACHE_TTL_SECONDS`
  reuse the first response.
- **Write-on-material-change** - about 87% of observations are never written,
  which is what keeps Supabase's 500 MB lasting months.
- **Event deduplication** - a sustained condition alerts once per time
  bucket, not once per scan.

## Keeping an eye on the free tiers

| Watch | Where | Act when |
|---|---|---|
| Database size | Supabase -> Reports | Over 400 MB: add the retention job |
| Workflow failures | GitHub Actions tab | Any red run |
| Scanner health | `scanner_runs` table | No `success` row in several hours |
| Paid analyses | `research_reports` where model <> 'template-v1' | Should be 0 in template mode |
