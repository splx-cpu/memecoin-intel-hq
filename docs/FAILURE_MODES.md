# What breaks, and whether you would find out

Written to be honest rather than reassuring. The point of a monitoring system
is not that it never fails — it is that you know when it has.

The uncomfortable fact underneath all of this: **a heartbeat that runs on the
same machinery it is monitoring cannot report that machinery being down.**
The daily summary tells you the scanner is alive. It cannot tell you the
scanner is dead, because if the scanner is dead the summary does not run
either. Everything below is an attempt to work around that, and the last
section admits where it is not fully worked around.

---

## Summary

| What fails | You find out | How long |
|---|---|---|
| A scan pass fails | Telegram system alert | Next run (~1h) |
| A pass partially fails | Telegram system alert | Next run (~1h) |
| A scanner stops succeeding | Telegram system alert | Within 6h |
| A run is killed mid-pass | Telegram system alert | Next run (~1h) |
| Database fills up | Telegram alert at 70%, again at 90% | Next run (~1h) |
| Database unreachable | Telegram alert (no database needed) | Next run (~1h) |
| Provider API down | Partial-fail alert; recovers on its own | Next run (~1h) |
| Bot token revoked | **Nothing** — see below | Only the daily digest stops |
| GitHub suspends the schedule | Keepalive should prevent it | See below |
| Supabase project paused/deleted | Alert, if the workflow still runs | Next run (~1h) |
| GitHub Actions outage | **Nothing** | — |

---

## Failures the system reports itself

These all work by the same route: the scan runs, something is wrong, and a
`system` alert goes to Telegram. Implemented in `src/mihq/health.py`, called
from `worker.run_once`.

**A failed or partial pass.** Reported immediately, from the run's own
status. A `partial` is one chain's provider having a bad minute — normal, and
it corrects itself — so it is reported as `high`, not `critical`. If a
partial were treated as critical you would learn to ignore critical.

**A scanner that has stopped succeeding.** The important one. A scanner
erroring on every pass still *runs*, so without this check the only symptom
is that alerts gradually stop arriving — indistinguishable from a quiet
market. Threshold is 6 hours by default, deliberately generous because
GitHub's scheduled runs arrive roughly hourly rather than on the cron in the
workflow file.

**A run killed mid-pass.** Left marked `running` forever. It neither
succeeded nor failed, so no other check notices it.

**The database filling up.** Supabase's free tier is 500 MB. You get a
warning at 70% and a critical at 90%. This matters because the failure mode
when it fills is *writes starting to error*, which reads like a scanner bug —
you would debug it in entirely the wrong place.

**The database being unreachable.** The process dies inside `connect()`,
before any health check can run. `worker.notify_fatal` catches that and sends
a Telegram alert built from a bare client with no repository, because
Telegram does not need the database. The connection string is scrubbed first
— psycopg puts the DSN, password and all, in its error text.

### Why alerts do not repeat every hour

Each problem has a stable `key`, and the same key is not re-sent within 12
hours. The key deliberately contains no elapsed times or sizes: if it did,
every occurrence would look new and nothing would ever be throttled — which
is how alerting systems end up muted.

One deliberate exception: the throttle is stored *in the database*. When the
database is the problem it cannot be read, so those alerts fall back to
sending every run. That is correct rather than a limitation. An hourly
reminder that your database is down is a reasonable thing to be told.

---

## Failures that are partly covered

### GitHub suspends the scheduled workflows

**The most dangerous failure here**, because it is completely silent and it
takes everything down at once — including every alert that would have told
you.

GitHub suspends scheduled triggers on a **public** repository after **60 days
with no repository activity**. The workflows do not fail. They simply stop
being triggered. Scheduled runs of the scanner do *not* reset the timer — it
takes a commit.

`.github/workflows/keepalive.yml` commits a timestamp weekly, which resets
it. Eight chances to succeed before the deadline matters.

**This is not airtight.** If the keepalive itself is suspended or broken, the
whole arrangement fails quietly. The honest mitigation is a calendar
reminder: once a month, open the Actions tab and check the workflows are not
showing *"This scheduled workflow is disabled…"*. Thirty seconds, and it is
the only check that does not depend on the system being alive to perform it.

### The bot token being revoked

If the token is revoked or the chat blocked, Telegram alerts fail — including
the alert saying Telegram is failing. Telegram cannot report that Telegram is
down.

The only visible symptom is that the **daily summary stops arriving**. That
is the main practical reason the daily summary exists: it is the one message
you expect at a known time, so its absence is information. If it has not
arrived by mid-morning, something is wrong.

Delivery failures are recorded in `notification_logs` with their HTTP status,
so once you are looking you can see exactly what happened.

---

## Failures nothing here can detect

Stated plainly, because a monitoring document that implies full coverage is
worse than one that admits gaps.

- **A GitHub Actions outage.** Nothing runs, nothing reports.
- **The repository being deleted or made private.** Private repos get 2,000
  Actions minutes a month rather than unlimited, so the workflows would stop
  partway through a month.
- **Supabase pausing the project.** Free-tier projects pause after a period
  of inactivity. Constant scanner writes should prevent it, but if the
  scanner is already down, the database pausing is a second failure stacked
  on the first.
- **Silently wrong data.** Every check here asks *did it run*, not *was it
  right*. A provider returning plausible but incorrect liquidity figures
  would pass every check in this file. The `largest $2325.2M` figure in the
  tuning report is a live example: almost certainly bad provider data, and
  nothing flagged it.

That last one is the real limit of this stage. The system can now tell you it
has stopped. It cannot tell you it is lying.

---

## What to do when an alert arrives

1. **Open the Actions tab** — `github.com/splx-cpu/memecoin-intel-hq/actions`.
   A red run shows the error; the log is the first place to look.
2. **Check the database is reachable** — `python scripts/check_env.py --ping`.
3. **Check how full it is** — `python scripts/tune_floors.py --days 3` also
   prints the watchlist size, which is usually what grew.
4. **Re-run by hand** — any workflow's *Run workflow* button. If it passes,
   it was transient; if it fails the same way, it is not.

### If the storage alert fires

In order of how much you lose:

1. `SNAPSHOT_RAW_MODE=never` — stops storing raw provider payloads, which are
   the bulk of each row. Costs you the ability to re-parse history.
2. Raise `WATCHLIST_MIN_LIQUIDITY_USD` — fewer tokens tracked, fewer rows.
3. Lower `WATCHLIST_RETIRE_UNEVENTFUL_HOURS` — retire quiet tokens sooner.
4. Delete old `market_snapshots` rows. Alert history survives: `snapshot_id`
   is nulled rather than cascading, which was designed in at Stage A.
