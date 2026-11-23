"""Every database read and write the scanners perform.

WHY THE SQL LIVES IN NAMED CONSTANTS
------------------------------------
Each statement below is a module-level `SQL_*` constant rather than an inline
string. That makes them extractable, so the test suite can run every one
against a real PostgreSQL server and prove the syntax and column names are
correct - something a mocked database can never tell you.

PARAMETERS, ALWAYS
------------------
Every value is passed via `%s` placeholders, never formatted into the SQL
string. Token symbols come from strangers on the internet; a token named
`'); drop table tokens; --` must be stored as text, not executed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Sequence

from mihq.logging_setup import get_logger
from mihq.models import MarketObservation

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# tokens
# ---------------------------------------------------------------------------

# `xmax = 0` is a PostgreSQL trick: on a freshly inserted row the system
# column xmax is 0, while an updated row carries the transaction id that
# touched it. It is how one statement can tell us whether this token is new,
# which the discovery scanner reports as `new_tokens`.
#
# COALESCE on every updatable column is deliberate: a later observation that
# lacks a symbol must not erase a symbol we already know. And
# `coalesce(tokens.pair_created_at, excluded.pair_created_at)` keeps the FIRST
# creation time we ever learned, because a pool's birth cannot change.
SQL_UPSERT_TOKEN = """
insert into tokens (
    chain, token_address, symbol, name, primary_pool_address,
    pair_created_at, last_seen_at, metadata
)
values (%s, %s, %s, %s, %s, %s, now(), %s)
on conflict (chain, token_address) do update set
    symbol               = coalesce(excluded.symbol, tokens.symbol),
    name                 = coalesce(excluded.name, tokens.name),
    primary_pool_address = coalesce(excluded.primary_pool_address,
                                    tokens.primary_pool_address),
    pair_created_at      = coalesce(tokens.pair_created_at, excluded.pair_created_at),
    last_seen_at         = now()
returning id, (xmax = 0) as was_inserted
"""

SQL_GET_TOKEN_ID = """
select id from tokens where chain = %s and token_address = %s
"""


# ---------------------------------------------------------------------------
# market_snapshots
# ---------------------------------------------------------------------------

SQL_INSERT_SNAPSHOT = """
insert into market_snapshots (
    token_id, chain, pool_address, source, captured_at, provider_updated_at,
    price_usd, price_native, liquidity_usd, fdv_usd, market_cap_usd,
    volume_5m_usd, volume_1h_usd, volume_6h_usd, volume_24h_usd,
    price_change_5m, price_change_1h, price_change_6h, price_change_24h,
    buys_1h, sells_1h, buys_24h, sells_24h, holder_count, raw
)
values (
    %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s
)
on conflict (token_id, source, captured_at) do nothing
returning id
"""

# Used by the write policy to compare against. Only the columns the policy
# actually reads are selected - fetching the raw jsonb here would pull
# megabytes across the wire for no reason.
SQL_LATEST_SNAPSHOT = """
select captured_at, price_usd, liquidity_usd, volume_24h_usd
  from market_snapshots
 where token_id = %s
 order by captured_at desc
 limit 1
"""

SQL_SNAPSHOT_HISTORY = """
select captured_at, price_usd, liquidity_usd, volume_24h_usd, price_change_24h
  from market_snapshots
 where token_id = %s and captured_at >= %s
 order by captured_at desc
 limit %s
"""


# ---------------------------------------------------------------------------
# watchlists
# ---------------------------------------------------------------------------

# The watchlist scanner's main query. `make_interval(secs => ...)` lets each
# row carry its own cadence via scan_interval_seconds, falling back to the
# global setting. Ordering by priority then oldest-scanned means that when
# the batch limit bites, nothing starves.
SQL_DUE_WATCHLIST = """
select w.id,
       w.token_id,
       t.chain,
       t.token_address,
       t.primary_pool_address,
       t.symbol,
       w.category,
       w.priority,
       w.last_scanned_at,
       w.consecutive_failures
  from watchlists w
  join tokens t on t.id = w.token_id
 where w.is_active
   and (
        w.last_scanned_at is null
        or w.last_scanned_at <= now() - make_interval(
               secs => coalesce(w.scan_interval_seconds, %s)
           )
       )
 order by w.priority asc, w.last_scanned_at asc nulls first
 limit %s
"""

# The partial unique index is `on watchlists (token_id) where is_active`, so
# the conflict target must repeat that predicate for Postgres to match it.
# The `where not exists` is a COOLDOWN, and it is what stops a loop.
#
# Discovery now adds any token that meets the liquidity floor, not only ones
# it has never seen - otherwise a pool that launches small and grows could
# never be picked up, because `was_inserted` is true exactly once. But that
# also means a token retired for being uneventful, while still well funded,
# would be re-added on the very next pass and bounce between states forever.
#
# `on conflict do nothing` cannot prevent that: the retired row is inactive,
# so the partial unique index does not see a conflict at all.
SQL_ADD_TO_WATCHLIST = """
insert into watchlists (token_id, category, added_reason, priority)
select %s, %s, %s, %s
 where not exists (
     select 1
       from watchlists w
      where w.token_id = %s
        and w.removed_at is not null
        and w.removed_at > now() - make_interval(hours => %s)
 )
on conflict (token_id) where is_active do nothing
returning id
"""

SQL_MARK_SCANNED = """
update watchlists
   set last_scanned_at = now(),
       consecutive_failures = 0
 where id = any(%s)
"""

SQL_MARK_SCAN_FAILED = """
update watchlists
   set last_scanned_at = now(),
       consecutive_failures = consecutive_failures + 1
 where id = any(%s)
returning id, token_id, consecutive_failures
"""

# A pool that has vanished from both providers for many passes is not coming
# back. Deactivating rather than deleting keeps the research history.
SQL_DEACTIVATE_FAILED_WATCHLIST = """
update watchlists
   set is_active = false,
       removed_at = now(),
       notes = coalesce(notes || ' | ', '') || %s
 where is_active and consecutive_failures >= %s
returning id, token_id
"""

SQL_COUNT_ACTIVE_WATCHLIST = """
select count(*) from watchlists where is_active
"""


# ---------------------------------------------------------------------------
# scanner_runs
# ---------------------------------------------------------------------------

SQL_START_RUN = """
insert into scanner_runs (scanner, status, chains_scanned)
values (%s, 'running', %s)
returning id
"""

SQL_FINISH_RUN = """
update scanner_runs
   set status = %s,
       finished_at = now(),
       duration_ms = extract(epoch from (now() - started_at)) * 1000,
       tokens_seen = %s,
       new_tokens = %s,
       snapshots_written = %s,
       events_created = %s,
       events_suppressed = %s,
       provider_errors = %s,
       database_errors = %s,
       error_message = %s,
       details = %s
 where id = %s
"""

SQL_LAST_SUCCESSFUL_RUN = """
select scanner, max(finished_at) as last_success
  from scanner_runs
 where status = 'success'
 group by scanner
"""

# A row still marked 'running' long after it started means the process was
# killed mid-pass. The health monitor surfaces these.
SQL_STALE_RUNNING = """
select id, scanner, started_at
  from scanner_runs
 where status = 'running'
   and started_at <= now() - make_interval(secs => %s)
 order by started_at
"""


# ---------------------------------------------------------------------------
# alert_events
# ---------------------------------------------------------------------------

# THE dedupe statement. `on conflict (event_key) do nothing` means a repeat of
# the same event within its time bucket returns no row, and the caller treats
# that as "already alerted" - no race, because the database serialises it.
SQL_INSERT_ALERT_EVENT = """
insert into alert_events (
    event_key, token_id, chain, category, event_type, severity,
    payload, snapshot_id
)
values (%s, %s, %s, %s, %s, %s, %s, %s)
on conflict (event_key) do nothing
returning id
"""

SQL_PENDING_ANALYSIS = """
select id, token_id, chain, category, event_type, severity, triggered_at, payload
  from alert_events
 where not is_analyzed and not is_suppressed
 order by
     case severity
         when 'critical' then 0 when 'high' then 1 when 'medium' then 2
         when 'low' then 3 else 4
     end,
     triggered_at asc
 limit %s
"""

# Everything the notifier needs for one event, in a single query: the event,
# its token, and the research report if one exists. Three round trips per
# alert would be wasteful when the alert is already late by definition.
SQL_EVENT_WITH_CONTEXT = """
select ae.id, ae.event_key, ae.token_id, ae.chain, ae.category,
       ae.event_type, ae.severity, ae.triggered_at, ae.payload,
       ae.research_report_id,
       t.symbol, t.name, t.token_address,
       rr.headline, rr.summary, rr.what_changed,
       rr.verified_facts, rr.assumptions, rr.missing_data, rr.risks,
       rr.sources, rr.model
  from alert_events ae
  join tokens t on t.id = ae.token_id
  left join research_reports rr on rr.id = ae.research_report_id
 where ae.id = %s
"""

SQL_INSERT_NOTIFICATION_LOG = """
insert into notification_logs (
    alert_event_id, daily_summary_id, channel, message_type, chat_id,
    status, attempt, telegram_message_id, error_code, error_message,
    request_payload, sent_at
)
values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
        case when %s = 'sent' then now() else null end)
on conflict (alert_event_id, attempt, message_type) do nothing
returning id
"""

SQL_NOTIFICATION_FAILURES_SINCE = """
select count(*) from notification_logs
 where status = 'failed' and created_at >= %s
"""

# The category exclusion is NOT an optimisation - it is what keeps the queue
# usable. Ordering is oldest-first, so without it ~500 new-token events a day
# would sit in front of every risk alert, and a liquidity drain would wait
# days behind a backlog of "a new pool exists". Excluded categories never
# enter the queue at all; SQL_RESOLVE_DIGEST_ONLY clears them separately.
#
# An empty exclusion list is handled by `<> all(...)`, which is true for
# every row against an empty array - so clearing the setting restores the
# old behaviour with no special case.
SQL_PENDING_NOTIFICATION = """
select id, token_id, chain, category, event_type, severity, triggered_at,
       payload, research_report_id
  from alert_events
 where not is_notified and not is_suppressed
   and category <> all(%s)
 order by triggered_at asc
 limit %s
"""

# Marks digest-only events as handled in one statement rather than one round
# trip each. Two reasons this matters beyond speed:
#
# 1. Left pending forever, the queue grows without limit.
# 2. If the user later removes a category from the exclusion list, every
#    back-dated event would suddenly become sendable and arrive at once.
#    Resolving them as they occur means re-enabling affects only new events.
#
# `is_suppressed` is deliberately NOT set: the daily summary excludes
# suppressed events, and these must still be counted there. Being quiet
# about something is not the same as pretending it did not happen.
SQL_RESOLVE_DIGEST_ONLY = """
update alert_events
   set is_notified = true
 where not is_notified
   and not is_suppressed
   and category = any(%s)
"""

# Events are inserted BEFORE the snapshot (so deduplication decides whether
# the observation counts as evidence), then linked back to it.
SQL_INSERT_RESEARCH_REPORT = """
insert into research_reports (
    token_id, alert_event_id, category, model, prompt_version,
    headline, summary, what_changed,
    verified_facts, assumptions, missing_data, risks, sources,
    confidence, raw_response, input_tokens, output_tokens, cost_usd, latency_ms
)
values (
    %s, %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s
)
returning id
"""

# Powers the daily spend cap. A RANGE query on created_at, deliberately not
# an index on date(created_at) - that function is not IMMUTABLE and Postgres
# rejects indexing it (we hit exactly that error in Stage A).
SQL_COUNT_REPORTS_SINCE = """
select count(*), coalesce(sum(cost_usd), 0)
  from research_reports
 where created_at >= %s and model <> 'template-v1'
"""

SQL_LINK_EVENT_SNAPSHOT = """
update alert_events set snapshot_id = %s where id = %s and snapshot_id is null
"""

SQL_MARK_EVENT_ANALYZED = """
update alert_events
   set is_analyzed = true, research_report_id = %s
 where id = %s
"""

SQL_MARK_EVENT_NOTIFIED = """
update alert_events set is_notified = true where id = %s
"""

SQL_SUPPRESS_EVENT = """
update alert_events
   set is_suppressed = true, suppressed_reason = %s
 where id = %s
"""

SQL_COUNT_EVENTS_SINCE = """
select category, count(*) from alert_events
 where triggered_at >= %s
 group by category
"""


# ---------------------------------------------------------------------------
# risk_flags
# ---------------------------------------------------------------------------

# A flag describes a STATE, so re-raising it must refresh the detail rather
# than create a duplicate. `detected_at` is deliberately preserved on update:
# "this has been true since 04:00" is more useful than a timestamp that
# resets every minute.
SQL_UPSERT_RISK_FLAG = """
insert into risk_flags (token_id, flag_type, severity, detail, evidence, source)
values (%s, %s, %s, %s, %s, %s)
on conflict (token_id, flag_type) where is_active do update set
    severity = excluded.severity,
    detail   = excluded.detail,
    evidence = excluded.evidence
returning id, (xmax = 0) as was_new
"""

# Conditions that are no longer true are resolved, not deleted, so the
# research history survives.
SQL_RESOLVE_ABSENT_RISK_FLAGS = """
update risk_flags
   set is_active = false, resolved_at = now()
 where token_id = %s
   and is_active
   and not (flag_type = any(%s))
returning id, flag_type
"""

SQL_ACTIVE_RISK_FLAGS = """
select flag_type, severity, detail, evidence, detected_at
  from risk_flags
 where token_id = %s and is_active
 order by case severity
     when 'critical' then 0 when 'high' then 1 when 'medium' then 2
     when 'low' then 3 else 4 end
"""


# Why is each active watchlist entry still here? One row, so the answer is a
# single round trip rather than a scan of the whole table.
#
# This exists because the first set of retirement thresholds did not converge
# and it was not obvious which of the two rules was failing. Guessing at
# thresholds without this is how the dead zone below got created in the first
# place.
SQL_WATCHLIST_DIAGNOSIS = """
with active as (
    select w.id, w.token_id, w.added_at,
           (select s.liquidity_usd
              from market_snapshots s
             where s.token_id = w.token_id
             order by s.captured_at desc
             limit 1) as liquidity_usd
      from watchlists w
     where w.is_active
)
select
    count(*)                                                       as total,
    count(*) filter (where added_at > now() - make_interval(hours => %s))
                                                                   as in_grace,
    count(*) filter (where liquidity_usd is null)                  as unknown_liq,
    count(*) filter (where liquidity_usd < %s)                     as below_retire,
    count(*) filter (where liquidity_usd >= %s and liquidity_usd < %s)
                                                                   as dead_zone,
    count(*) filter (where liquidity_usd >= %s)                    as above_entry,
    count(*) filter (where added_at < now() - make_interval(hours => %s))
                                                                   as old_enough,
    count(*) filter (where exists (
        select 1 from alert_events e
         where e.token_id = active.token_id and e.event_type <> %s))
                                                                   as has_events
  from active
"""

# Which event types are keeping tokens alive under the "uneventful" rule.
# If one noisy detector fires for most of the watchlist, that rule can never
# retire anything and the whole mechanism is inert.
SQL_WATCHLIST_EVENT_TYPES = """
select e.event_type, count(distinct e.token_id)
  from alert_events e
  join watchlists w on w.token_id = e.token_id and w.is_active
 where e.event_type <> %s
 group by e.event_type
 order by 2 desc
"""


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------

# Supabase's free tier is 500 MB. Nothing in the system could see how close it
# was to that, and the failure mode when it fills is writes starting to fail -
# which looks like a scanner bug rather than a quota.
SQL_DATABASE_SIZE = """
select pg_database_size(current_database())
"""

# Throttles repeated system alerts. The key identifies the KIND of problem, so
# a database that has been unreachable for six hours reports once rather than
# once per run.
#
# Deliberately keyed on request_payload->>'key' rather than on the message
# text: the text contains elapsed times and sizes that change every run, so
# matching on it would never throttle anything.
SQL_LAST_SYSTEM_ALERT = """
select max(created_at)
  from notification_logs
 where message_type = 'system'
   and status in ('sent', 'skipped')
   and request_payload->>'key' = %s
"""


# ---------------------------------------------------------------------------
# watchlist retirement
# ---------------------------------------------------------------------------
#
# WHY THIS EXISTS
#
# Discovery adds; nothing removed. The list reached 283 entries in two days,
# and every entry costs an API call and a row on every pass, against a 500 MB
# free tier. Growth without a counterweight ends in one of two ways: the
# quota runs out, or the scanner slows until it is useless.
#
# WHY RETIREMENT IS SAFE
#
# `is_active = false` with `removed_at` set keeps the whole history, and the
# partial unique index (active rows only) means discovery can re-add the same
# token later if it comes back to life. Nothing is deleted.

# Rule 1: the money left.
#
# The NULL handling here is the important part, and it is deliberate. The
# subquery returns NULL when a token has no snapshots at all, and also when
# the latest snapshot has no liquidity figure. `NULL < 2000` is NULL, not
# true, so neither case retires the row.
#
# That is the behaviour we want: liquidity DISAPPEARING from the provider
# feed is the single most important signal this system has - it may be the
# pool being pulled - and it already raises `liquidity_unreported`. Retiring
# a token at exactly the moment it becomes interesting would be the worst
# possible bug, and "unknown is not low" is what prevents it.
SQL_RETIRE_LOW_LIQUIDITY_WATCHLIST = """
update watchlists w
   set is_active = false,
       removed_at = now(),
       notes = coalesce(w.notes || ' | ', '') || %s
 where w.is_active
   and w.added_at < now() - make_interval(hours => %s)
   and (
       select s.liquidity_usd
         from market_snapshots s
        where s.token_id = w.token_id
        order by s.captured_at desc
        limit 1
   ) < %s
returning w.id, w.token_id
"""

# Rule 3: liquidity has been unknown for a long time.
#
# THIS RULE EXISTS BECAUSE RULE 1'S PROTECTION WAS TOO BROAD.
#
# Rule 1 deliberately never retires a token whose liquidity is unknown,
# because liquidity vanishing from the feed may be a pool being pulled - the
# moment you most want to still be watching. That reasoning is right about
# the FIRST FEW HOURS and wrong after that.
#
# Measured on real data: 990 of 1,859 active entries (53%) had unknown
# liquidity. It is not a rare emergency, it is the ordinary end state of a
# dead token - it drops out of the provider feed and never returns. Rule 1
# was therefore pinning half the watchlist in place permanently, and no
# amount of adjusting the other thresholds would have fixed it.
#
# `liquidity_unreported` has already alerted by then. Watching a token whose
# liquidity has been unreadable for a day buys nothing.
#
# The COALESCE matters: a token that has never had a known figure falls back
# to when it was added, so "we have had no liquidity for this token since we
# started watching it" is handled by the same rule rather than slipping
# through a NULL comparison - which is exactly how Rule 1 ended up too broad.
SQL_RETIRE_UNKNOWN_LIQUIDITY_WATCHLIST = """
update watchlists w
   set is_active = false,
       removed_at = now(),
       notes = coalesce(w.notes || ' | ', '') || %s
 where w.is_active
   and w.added_at < now() - make_interval(hours => %s)
   and coalesce(
         (select max(s.captured_at)
            from market_snapshots s
           where s.token_id = w.token_id
             and s.liquidity_usd is not null),
         w.added_at
       ) < now() - make_interval(hours => %s)
   -- WE MUST HAVE ACTUALLY LOOKED.
   --
   -- Without this the rule retires tokens for not being SCANNED rather than
   -- for being dead, and the two are indistinguishable from the snapshot
   -- table alone: both leave no recent reading.
   --
   -- It is not hypothetical. With a batch size of 40, an hourly schedule and
   -- 1,859 active entries, a full cycle took 46 hours - so most of the list
   -- was always outside a 24-hour window through no fault of its own. The
   -- watchlist duly collapsed, which looked like the fix working.
   --
   -- A token we have not managed to scan is one we have no opinion about.
   and w.last_scanned_at is not null
   and w.last_scanned_at > now() - make_interval(hours => %s)
returning w.id, w.token_id
"""


# Rule 2: it has been watched for days and has never done anything.
#
# The arrival event is excluded from the "did anything happen" test because
# every token on the list fired it once on arrival - counting it would mean no
# row ever qualifies, and the rule would silently do nothing. It is passed in
# rather than written here: the first version hardcoded 'new_token', which is
# the detector's CLASS name, while the event it writes is 'first_seen'. The
# rule matched nothing and failed silently, which is exactly what a rule like
# this does when it is wrong.
SQL_RETIRE_UNEVENTFUL_WATCHLIST = """
update watchlists w
   set is_active = false,
       removed_at = now(),
       notes = coalesce(w.notes || ' | ', '') || %s
 where w.is_active
   and w.added_at < now() - make_interval(hours => %s)
   and not exists (
       select 1
         from alert_events e
        where e.token_id = w.token_id
          and e.event_type <> %s
   )
returning w.id, w.token_id
"""

# Reported by the tuning script so a threshold can be chosen from real data
# rather than guessed. Reads liquidity from the event's own payload rather
# than joining to market_snapshots: `snapshot_id` is null whenever the write
# policy skipped the snapshot, and a join through it silently drops those
# rows - which is how the first version of this query returned nothing at all
# against a database holding 66 of these events.
SQL_NEW_TOKEN_LIQUIDITY_SAMPLE = """
select (e.payload->>'liquidity_usd')::numeric
  from alert_events e
 where e.event_type = %s
   and e.triggered_at >= %s
   and e.payload->>'liquidity_usd' is not null
"""

# ---------------------------------------------------------------------------
# daily summaries
# ---------------------------------------------------------------------------
#
# EVERY QUERY HERE TAKES A HALF-OPEN WINDOW [start, end).
#
# Not `now() - interval '24 hours'`. The summary covers a LOCAL CALENDAR DAY
# in the configured timezone, and on the two DST changeover days that day is
# 23 or 25 hours long. The window is computed in Python from the local
# midnight boundaries (see `mihq.summary.day_window`) and passed in as two
# UTC timestamps, so the SQL never has to know about any of that.
#
# Half-open matters: `>= start and < end` counts a row at exactly midnight
# once, in the day it begins. `between` would double-count it.

# scanner_runs already accumulates per-pass counters, so the day's activity
# is a SUM over rows we already wrote rather than a recount of the whole
# snapshot table. Cheaper, and it reports what the scanner believed it did -
# which is what a health summary should say.
SQL_SUMMARY_RUN_TOTALS = """
select scanner,
       count(*)                                      as runs,
       count(*) filter (where status = 'success')    as successful,
       count(*) filter (where status = 'partial')    as partial,
       count(*) filter (where status = 'failed')     as failed,
       count(*) filter (where status = 'running')    as still_running,
       coalesce(sum(tokens_seen), 0)                 as tokens_seen,
       coalesce(sum(new_tokens), 0)                  as new_tokens,
       coalesce(sum(snapshots_written), 0)           as snapshots_written,
       coalesce(sum(events_created), 0)              as events_created,
       coalesce(sum(events_suppressed), 0)           as events_suppressed,
       coalesce(sum(alerts_sent), 0)                 as alerts_sent,
       coalesce(sum(provider_errors), 0)             as provider_errors,
       coalesce(sum(telegram_errors), 0)             as telegram_errors,
       coalesce(sum(database_errors), 0)             as database_errors,
       max(finished_at)                              as last_finished
  from scanner_runs
 where started_at >= %s and started_at < %s
 group by scanner
 order by scanner
"""

# Category and severity in one pass; the caller pivots it two ways.
# Suppressed events are excluded because they were never shown to anyone,
# and a summary that counts them would overstate the day.
SQL_SUMMARY_ALERTS = """
select category, severity, count(*)
  from alert_events
 where triggered_at >= %s and triggered_at < %s
   and not is_suppressed
 group by category, severity
"""

# The few events worth naming. Ordered by severity first so a quiet day with
# one critical event leads with it rather than burying it under the newest.
SQL_SUMMARY_HIGHLIGHTS = """
select e.chain, t.symbol, t.token_address, e.event_type, e.severity,
       e.triggered_at, e.payload
  from alert_events e
  left join tokens t on t.id = e.token_id
 where e.triggered_at >= %s and e.triggered_at < %s
   and not e.is_suppressed
 order by case e.severity
     when 'critical' then 0 when 'high' then 1 when 'medium' then 2
     when 'low' then 3 else 4 end,
     e.triggered_at desc
 limit %s
"""

# Both halves of the delta in one statement. `removed_at` is null for an
# active row, and a null fails the filter, so active rows simply do not count.
SQL_SUMMARY_WATCHLIST_DELTA = """
select count(*) filter (where added_at   >= %s and added_at   < %s) as added,
       count(*) filter (where removed_at >= %s and removed_at < %s) as removed
  from watchlists
"""

# Flags RAISED during the day. `detected_at` is deliberately preserved when a
# flag is re-raised (see SQL_UPSERT_RISK_FLAG), so this counts first
# detections, not repeats - which is what "new risks found today" means.
SQL_SUMMARY_RISK_FLAGS_RAISED = """
select flag_type, severity, count(*)
  from risk_flags
 where detected_at >= %s and detected_at < %s
 group by flag_type, severity
 order by count(*) desc
"""

# NOTE the deliberate omission: `status` is NOT in the update list.
#
# Re-running the job for a day that was already sent must refresh the numbers
# WITHOUT resetting status to 'pending', because a 'pending' row would be
# picked up and sent a second time. The returned status is therefore the
# status already on file, which is exactly what the caller needs to decide
# whether to send.
SQL_UPSERT_DAILY_SUMMARY = """
insert into daily_summaries (
    summary_date, timezone, stats, narrative, report_ids, watchlist_changes, status
) values (%s, %s, %s, %s, %s, %s, 'pending')
on conflict (summary_date) do update set
    timezone          = excluded.timezone,
    stats             = excluded.stats,
    narrative         = excluded.narrative,
    report_ids        = excluded.report_ids,
    watchlist_changes = excluded.watchlist_changes,
    generated_at      = now()
returning id, status, (xmax = 0) as was_new
"""

SQL_DAILY_SUMMARY_FOR_DATE = """
select id, status, generated_at, telegram_message_id, narrative
  from daily_summaries
 where summary_date = %s
"""

SQL_MARK_DAILY_SUMMARY_SENT = """
update daily_summaries
   set status = %s, telegram_message_id = %s
 where id = %s
"""


# ---------------------------------------------------------------------------
# Helper types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WatchlistEntry:
    """One row from `SQL_DUE_WATCHLIST`."""

    watchlist_id: int
    token_id: int
    chain: str
    token_address: str
    primary_pool_address: str | None
    symbol: str | None
    category: str
    priority: int
    last_scanned_at: datetime | None
    consecutive_failures: int

    @classmethod
    def from_row(cls, row: Sequence[Any]) -> "WatchlistEntry":
        return cls(
            watchlist_id=row[0],
            token_id=row[1],
            chain=row[2],
            token_address=row[3],
            primary_pool_address=row[4],
            symbol=row[5],
            category=row[6],
            priority=row[7],
            last_scanned_at=row[8],
            consecutive_failures=row[9],
        )


@dataclass(frozen=True)
class StoredSnapshot:
    """The few columns the write policy compares against."""

    captured_at: datetime
    price_usd: float | None
    liquidity_usd: float | None
    volume_24h_usd: float | None


@dataclass(frozen=True)
class UpsertResult:
    token_id: int
    was_inserted: bool


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------

class Repository:
    """Database operations for the scanners.

    Takes any DB-API connection, so production passes a psycopg connection
    and the tests pass a recorder that captures the SQL without a server.
    """

    def __init__(self, connection: Any) -> None:
        self.conn = connection

    # -- tokens ----------------------------------------------------------

    def upsert_token(
        self,
        *,
        chain: str,
        token_address: str,
        symbol: str | None = None,
        name: str | None = None,
        primary_pool_address: str | None = None,
        pair_created_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> UpsertResult:
        """Insert a token, or refresh what we know about an existing one."""
        with self.conn.cursor() as cur:
            cur.execute(
                SQL_UPSERT_TOKEN,
                (
                    chain,
                    token_address,
                    symbol,
                    name,
                    primary_pool_address,
                    pair_created_at,
                    json.dumps(metadata or {}),
                ),
            )
            row = cur.fetchone()
        return UpsertResult(token_id=row[0], was_inserted=bool(row[1]))

    def upsert_token_from(self, observation: MarketObservation) -> UpsertResult:
        """Convenience wrapper for the common case."""
        return self.upsert_token(
            chain=observation.token.chain,
            token_address=observation.token.address,
            symbol=observation.token.symbol,
            name=observation.token.name,
            primary_pool_address=observation.pool_address,
            pair_created_at=observation.pair_created_at,
            metadata={"dex_id": observation.dex_id} if observation.dex_id else {},
        )

    # -- snapshots -------------------------------------------------------

    def latest_snapshot(self, token_id: int) -> StoredSnapshot | None:
        with self.conn.cursor() as cur:
            cur.execute(SQL_LATEST_SNAPSHOT, (token_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return StoredSnapshot(
            captured_at=row[0],
            price_usd=float(row[1]) if row[1] is not None else None,
            liquidity_usd=float(row[2]) if row[2] is not None else None,
            volume_24h_usd=float(row[3]) if row[3] is not None else None,
        )

    def insert_snapshot(
        self, token_id: int, observation: MarketObservation, *, include_raw: bool = False
    ) -> int | None:
        """Write one observation. Returns the new id, or None if it was a duplicate.

        `include_raw=False` stores an empty JSON object instead of the full
        provider payload. The payload is the bulk of a row's size, so this is
        the single biggest lever on storage growth.
        """
        row = observation.to_snapshot_row(token_id)
        raw_json = json.dumps(row["raw"]) if include_raw else "{}"
        with self.conn.cursor() as cur:
            cur.execute(
                SQL_INSERT_SNAPSHOT,
                (
                    row["token_id"], row["chain"], row["pool_address"], row["source"],
                    row["captured_at"], row["provider_updated_at"],
                    row["price_usd"], row["price_native"], row["liquidity_usd"],
                    row["fdv_usd"], row["market_cap_usd"],
                    row["volume_5m_usd"], row["volume_1h_usd"], row["volume_6h_usd"],
                    row["volume_24h_usd"],
                    row["price_change_5m"], row["price_change_1h"],
                    row["price_change_6h"], row["price_change_24h"],
                    row["buys_1h"], row["sells_1h"], row["buys_24h"], row["sells_24h"],
                    row["holder_count"], raw_json,
                ),
            )
            result = cur.fetchone()
        return result[0] if result else None

    # -- watchlist -------------------------------------------------------

    def due_watchlist(self, *, default_interval_seconds: int, limit: int) -> list[WatchlistEntry]:
        with self.conn.cursor() as cur:
            cur.execute(SQL_DUE_WATCHLIST, (default_interval_seconds, limit))
            rows = cur.fetchall()
        return [WatchlistEntry.from_row(r) for r in rows]

    def add_to_watchlist(
        self, token_id: int, *, category: str, reason: str | None = None,
        priority: int = 100, readd_cooldown_hours: int = 0,
    ) -> int | None:
        """Returns the new row id, or None.

        None means one of two things, deliberately not distinguished here:
        the token is already watched, or it was retired recently enough to
        still be in its cooldown. Both mean "do not add", and the caller has
        nothing different to do about either.
        """
        with self.conn.cursor() as cur:
            cur.execute(SQL_ADD_TO_WATCHLIST,
                        (token_id, category, reason, priority,
                         token_id, readd_cooldown_hours))
            row = cur.fetchone()
        return row[0] if row else None

    def mark_scanned(self, watchlist_ids: Sequence[int]) -> None:
        if not watchlist_ids:
            return
        with self.conn.cursor() as cur:
            cur.execute(SQL_MARK_SCANNED, (list(watchlist_ids),))

    def mark_scan_failed(self, watchlist_ids: Sequence[int]) -> list[tuple[int, int, int]]:
        if not watchlist_ids:
            return []
        with self.conn.cursor() as cur:
            cur.execute(SQL_MARK_SCAN_FAILED, (list(watchlist_ids),))
            return list(cur.fetchall())

    def deactivate_persistently_failing(
        self, *, threshold: int, note: str = "auto-removed: pool not found"
    ) -> list[tuple[int, int]]:
        with self.conn.cursor() as cur:
            cur.execute(SQL_DEACTIVATE_FAILED_WATCHLIST, (note, threshold))
            return list(cur.fetchall())

    def active_watchlist_count(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute(SQL_COUNT_ACTIVE_WATCHLIST)
            row = cur.fetchone()
        return int(row[0]) if row else 0

    # -- alert events ----------------------------------------------------

    def insert_alert_event(
        self,
        *,
        event_key: str,
        token_id: int,
        chain: str,
        category: str,
        event_type: str,
        severity: str,
        payload: dict[str, Any],
        snapshot_id: int | None = None,
    ) -> int | None:
        """Record a detected event.

        Returns the new id, or **None if this event_key already exists** -
        which is the duplicate-alert guard doing its job, not an error.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                SQL_INSERT_ALERT_EVENT,
                (event_key, token_id, chain, category, event_type, severity,
                 json.dumps(payload), snapshot_id),
            )
            row = cur.fetchone()
        return row[0] if row else None

    def insert_research_report(self, row: dict[str, Any]) -> int:
        """Store one analysis. `row` comes from `ResearchReport.to_row`."""
        with self.conn.cursor() as cur:
            cur.execute(
                SQL_INSERT_RESEARCH_REPORT,
                (
                    row["token_id"], row["alert_event_id"], row["category"],
                    row["model"], row["prompt_version"],
                    row["headline"], row["summary"], row["what_changed"],
                    json.dumps(row["verified_facts"]),
                    json.dumps(row["assumptions"]),
                    json.dumps(row["missing_data"]),
                    json.dumps(row["risks"]),
                    json.dumps(row["sources"]),
                    row["confidence"], json.dumps(row["raw_response"]),
                    row["input_tokens"], row["output_tokens"],
                    row["cost_usd"], row["latency_ms"],
                ),
            )
            return int(cur.fetchone()[0])

    def paid_analyses_since(self, since: datetime) -> tuple[int, float]:
        """(count, total cost) of PAID analyses since `since`.

        Template reports are excluded because they cost nothing, so they must
        not consume the daily budget.
        """
        with self.conn.cursor() as cur:
            cur.execute(SQL_COUNT_REPORTS_SINCE, (since,))
            row = cur.fetchone()
        return (int(row[0]), float(row[1])) if row else (0, 0.0)

    def link_event_snapshot(self, event_id: int, snapshot_id: int) -> None:
        """Attach the snapshot that provides this event's evidence."""
        with self.conn.cursor() as cur:
            cur.execute(SQL_LINK_EVENT_SNAPSHOT, (snapshot_id, event_id))

    def mark_event_analyzed(self, event_id: int, research_report_id: int | None) -> None:
        with self.conn.cursor() as cur:
            cur.execute(SQL_MARK_EVENT_ANALYZED, (research_report_id, event_id))

    def event_with_context(self, event_id: int) -> dict[str, Any] | None:
        """The event, its token and its report, as one dict ready to format."""
        with self.conn.cursor() as cur:
            cur.execute(SQL_EVENT_WITH_CONTEXT, (event_id,))
            row = cur.fetchone()
        if row is None:
            return None
        keys = (
            "id", "event_key", "token_id", "chain", "category", "event_type",
            "severity", "triggered_at", "payload", "research_report_id",
            "symbol", "name", "token_address",
            "headline", "summary", "what_changed", "verified_facts",
            "assumptions", "missing_data", "risks", "sources", "model",
        )
        return dict(zip(keys, row))

    def log_notification(
        self,
        *,
        alert_event_id: int | None,
        message_type: str,
        status: str,
        attempt: int = 1,
        chat_id: str | None = None,
        telegram_message_id: int | None = None,
        error_code: int | None = None,
        error_message: str | None = None,
        request_payload: dict[str, Any] | None = None,
        daily_summary_id: int | None = None,
        channel: str = "telegram",
    ) -> int | None:
        """Record one delivery ATTEMPT.

        Attempts are logged rather than outcomes, so a message that failed
        twice and succeeded on the third try leaves three rows telling that
        story. A single status column could not.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                SQL_INSERT_NOTIFICATION_LOG,
                (
                    alert_event_id, daily_summary_id, channel, message_type,
                    chat_id, status, attempt, telegram_message_id,
                    error_code, error_message,
                    json.dumps(request_payload or {}), status,
                ),
            )
            row = cur.fetchone()
        return row[0] if row else None

    def notification_failures_since(self, since: datetime) -> int:
        with self.conn.cursor() as cur:
            cur.execute(SQL_NOTIFICATION_FAILURES_SINCE, (since,))
            row = cur.fetchone()
        return int(row[0]) if row else 0

    def mark_event_notified(self, event_id: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute(SQL_MARK_EVENT_NOTIFIED, (event_id,))

    def suppress_event(self, event_id: int, reason: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(SQL_SUPPRESS_EVENT, (reason, event_id))

    def pending_analysis(self, limit: int = 20) -> list[tuple[Any, ...]]:
        with self.conn.cursor() as cur:
            cur.execute(SQL_PENDING_ANALYSIS, (limit,))
            return list(cur.fetchall())

    def pending_notification(
        self, limit: int = 20, *, exclude_categories: tuple[str, ...] = ()
    ) -> list[tuple[Any, ...]]:
        with self.conn.cursor() as cur:
            cur.execute(SQL_PENDING_NOTIFICATION, (list(exclude_categories), limit))
            return list(cur.fetchall())

    def resolve_digest_only(self, categories: tuple[str, ...]) -> int:
        """Mark digest-only events handled. Returns how many."""
        if not categories:
            return 0
        with self.conn.cursor() as cur:
            cur.execute(SQL_RESOLVE_DIGEST_ONLY, (list(categories),))
            return cur.rowcount or 0

    # -- risk flags ------------------------------------------------------

    def upsert_risk_flag(
        self,
        token_id: int,
        *,
        flag_type: str,
        severity: str,
        detail: str,
        evidence: dict[str, Any] | None = None,
        source: str = "heuristic",
    ) -> tuple[int, bool]:
        """Raise or refresh a risk flag. Returns (id, was_newly_raised)."""
        with self.conn.cursor() as cur:
            cur.execute(
                SQL_UPSERT_RISK_FLAG,
                (token_id, flag_type, severity, detail,
                 json.dumps(evidence or {}), source),
            )
            row = cur.fetchone()
        return (row[0], bool(row[1]))

    def resolve_absent_risk_flags(
        self, token_id: int, current_flag_types: Sequence[str]
    ) -> list[tuple[int, str]]:
        """Close any active flag whose condition is no longer true."""
        with self.conn.cursor() as cur:
            cur.execute(SQL_RESOLVE_ABSENT_RISK_FLAGS, (token_id, list(current_flag_types)))
            return list(cur.fetchall())

    def active_risk_flags(self, token_id: int) -> list[tuple[Any, ...]]:
        with self.conn.cursor() as cur:
            cur.execute(SQL_ACTIVE_RISK_FLAGS, (token_id,))
            return list(cur.fetchall())

    # -- scanner runs ----------------------------------------------------

    def start_run(self, scanner: str, chains: Iterable[str]) -> int:
        with self.conn.cursor() as cur:
            cur.execute(SQL_START_RUN, (scanner, list(chains)))
            row = cur.fetchone()
        return int(row[0])

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        tokens_seen: int = 0,
        new_tokens: int = 0,
        snapshots_written: int = 0,
        events_created: int = 0,
        events_suppressed: int = 0,
        provider_errors: int = 0,
        database_errors: int = 0,
        error_message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                SQL_FINISH_RUN,
                (
                    status, tokens_seen, new_tokens, snapshots_written,
                    events_created, events_suppressed, provider_errors,
                    database_errors, error_message,
                    json.dumps(details or {}), run_id,
                ),
            )

    def last_successful_runs(self) -> dict[str, datetime]:
        with self.conn.cursor() as cur:
            cur.execute(SQL_LAST_SUCCESSFUL_RUN)
            return {row[0]: row[1] for row in cur.fetchall()}

    def stale_running_runs(self, older_than_seconds: int) -> list[tuple[int, str, datetime]]:
        with self.conn.cursor() as cur:
            cur.execute(SQL_STALE_RUNNING, (older_than_seconds,))
            return list(cur.fetchall())

    def retire_low_liquidity(
        self, *, below_usd: float, grace_hours: int, note: str
    ) -> list[tuple[int, int]]:
        """Retire entries whose latest snapshot shows the money has gone.

        Tokens with unknown liquidity are deliberately left alone - see the
        note on SQL_RETIRE_LOW_LIQUIDITY_WATCHLIST.
        """
        with self.conn.cursor() as cur:
            cur.execute(SQL_RETIRE_LOW_LIQUIDITY_WATCHLIST,
                        (note, grace_hours, below_usd))
            return list(cur.fetchall())

    def retire_unknown_liquidity(
        self, *, grace_hours: int, unknown_for_hours: int,
        scanned_within_hours: int, note: str
    ) -> list[tuple[int, int]]:
        """Retire entries we HAVE scanned recently that still show no liquidity.

        `scanned_within_hours` is the guard against retiring a token merely
        because the scanner has not reached it yet.
        """
        with self.conn.cursor() as cur:
            cur.execute(SQL_RETIRE_UNKNOWN_LIQUIDITY_WATCHLIST,
                        (note, grace_hours, unknown_for_hours,
                         scanned_within_hours))
            return list(cur.fetchall())

    def retire_uneventful(
        self, *, older_than_hours: int, note: str, arrival_event_type: str
    ) -> list[tuple[int, int]]:
        """Retire long-lived entries that never produced a real event.

        `arrival_event_type` is the one that does NOT count as activity. It
        is passed in from the detector that writes it so the two cannot
        drift apart.
        """
        with self.conn.cursor() as cur:
            cur.execute(SQL_RETIRE_UNEVENTFUL_WATCHLIST,
                        (note, older_than_hours, arrival_event_type))
            return list(cur.fetchall())

    def new_token_liquidity_sample(
        self, since: datetime, *, arrival_event_type: str
    ) -> list[float]:
        """Liquidity of recently discovered tokens, for choosing a floor."""
        with self.conn.cursor() as cur:
            cur.execute(SQL_NEW_TOKEN_LIQUIDITY_SAMPLE, (arrival_event_type, since))
            return [float(r[0]) for r in cur.fetchall() if r[0] is not None]

    def watchlist_diagnosis(
        self, *, grace_hours: int, retire_floor: float, entry_floor: float,
        uneventful_hours: int, arrival_event_type: str,
    ) -> dict[str, int]:
        cols = ("total", "in_grace", "unknown_liq", "below_retire", "dead_zone",
                "above_entry", "old_enough", "has_events")
        with self.conn.cursor() as cur:
            cur.execute(SQL_WATCHLIST_DIAGNOSIS,
                        (grace_hours, retire_floor, retire_floor, entry_floor,
                         entry_floor, uneventful_hours, arrival_event_type))
            row = cur.fetchone()
        return dict(zip(cols, (int(v or 0) for v in row))) if row else {}

    def watchlist_event_types(self, arrival_event_type: str) -> list[tuple[str, int]]:
        with self.conn.cursor() as cur:
            cur.execute(SQL_WATCHLIST_EVENT_TYPES, (arrival_event_type,))
            return [(r[0], int(r[1])) for r in cur.fetchall()]

    # -- health ----------------------------------------------------------

    def database_size_bytes(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute(SQL_DATABASE_SIZE)
            row = cur.fetchone()
        return int(row[0]) if row else 0

    def last_system_alert(self, key: str) -> datetime | None:
        with self.conn.cursor() as cur:
            cur.execute(SQL_LAST_SYSTEM_ALERT, (key,))
            row = cur.fetchone()
        return row[0] if row else None

    # -- daily summaries -------------------------------------------------

    def summary_run_totals(
        self, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        """Per-scanner totals for the window, one dict per scanner."""
        columns = (
            "scanner", "runs", "successful", "partial", "failed", "still_running",
            "tokens_seen", "new_tokens", "snapshots_written", "events_created",
            "events_suppressed", "alerts_sent", "provider_errors",
            "telegram_errors", "database_errors", "last_finished",
        )
        with self.conn.cursor() as cur:
            cur.execute(SQL_SUMMARY_RUN_TOTALS, (start, end))
            return [dict(zip(columns, row)) for row in cur.fetchall()]

    def summary_alerts(
        self, start: datetime, end: datetime
    ) -> list[tuple[str, str, int]]:
        """(category, severity, count) for non-suppressed alerts."""
        with self.conn.cursor() as cur:
            cur.execute(SQL_SUMMARY_ALERTS, (start, end))
            return [(r[0], r[1], int(r[2])) for r in cur.fetchall()]

    def summary_highlights(
        self, start: datetime, end: datetime, limit: int = 5
    ) -> list[tuple[Any, ...]]:
        with self.conn.cursor() as cur:
            cur.execute(SQL_SUMMARY_HIGHLIGHTS, (start, end, limit))
            return list(cur.fetchall())

    def summary_watchlist_delta(
        self, start: datetime, end: datetime
    ) -> tuple[int, int]:
        """(added, removed) during the window."""
        with self.conn.cursor() as cur:
            # The window is needed twice: once for each filter.
            cur.execute(SQL_SUMMARY_WATCHLIST_DELTA, (start, end, start, end))
            row = cur.fetchone()
        return (int(row[0]), int(row[1])) if row else (0, 0)

    def summary_risk_flags_raised(
        self, start: datetime, end: datetime
    ) -> list[tuple[str, str, int]]:
        with self.conn.cursor() as cur:
            cur.execute(SQL_SUMMARY_RISK_FLAGS_RAISED, (start, end))
            return [(r[0], r[1], int(r[2])) for r in cur.fetchall()]

    def upsert_daily_summary(
        self,
        *,
        summary_date: Any,
        timezone_name: str,
        stats: dict[str, Any],
        narrative: str | None,
        report_ids: list[int] | None = None,
        watchlist_changes: dict[str, Any] | None = None,
    ) -> tuple[int, str, bool]:
        """Store the day's summary. Returns (id, status_on_file, was_new).

        `status_on_file` is the status ALREADY recorded, not one this call
        set - re-running for a day that was sent returns 'sent', which is how
        the caller knows not to send it twice.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                SQL_UPSERT_DAILY_SUMMARY,
                (summary_date, timezone_name, json.dumps(stats), narrative,
                 json.dumps(report_ids or []),
                 json.dumps(watchlist_changes or {})),
            )
            row = cur.fetchone()
        return (int(row[0]), str(row[1]), bool(row[2]))

    def daily_summary_for_date(self, summary_date: Any) -> tuple[Any, ...] | None:
        with self.conn.cursor() as cur:
            cur.execute(SQL_DAILY_SUMMARY_FOR_DATE, (summary_date,))
            return cur.fetchone()

    def mark_daily_summary_sent(
        self, summary_id: int, *, status: str, telegram_message_id: int | None
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(SQL_MARK_DAILY_SUMMARY_SENT,
                        (status, telegram_message_id, summary_id))


# Every statement in this module, for the test that runs them all against a
# real PostgreSQL server. Keeping this list beside the constants means a new
# statement that nobody added here fails the completeness test below.
ALL_STATEMENTS: dict[str, str] = {
    "SQL_UPSERT_TOKEN": SQL_UPSERT_TOKEN,
    "SQL_GET_TOKEN_ID": SQL_GET_TOKEN_ID,
    "SQL_INSERT_SNAPSHOT": SQL_INSERT_SNAPSHOT,
    "SQL_LATEST_SNAPSHOT": SQL_LATEST_SNAPSHOT,
    "SQL_SNAPSHOT_HISTORY": SQL_SNAPSHOT_HISTORY,
    "SQL_DUE_WATCHLIST": SQL_DUE_WATCHLIST,
    "SQL_ADD_TO_WATCHLIST": SQL_ADD_TO_WATCHLIST,
    "SQL_MARK_SCANNED": SQL_MARK_SCANNED,
    "SQL_MARK_SCAN_FAILED": SQL_MARK_SCAN_FAILED,
    "SQL_DEACTIVATE_FAILED_WATCHLIST": SQL_DEACTIVATE_FAILED_WATCHLIST,
    "SQL_COUNT_ACTIVE_WATCHLIST": SQL_COUNT_ACTIVE_WATCHLIST,
    "SQL_START_RUN": SQL_START_RUN,
    "SQL_FINISH_RUN": SQL_FINISH_RUN,
    "SQL_LAST_SUCCESSFUL_RUN": SQL_LAST_SUCCESSFUL_RUN,
    "SQL_STALE_RUNNING": SQL_STALE_RUNNING,
    "SQL_INSERT_ALERT_EVENT": SQL_INSERT_ALERT_EVENT,
    "SQL_LINK_EVENT_SNAPSHOT": SQL_LINK_EVENT_SNAPSHOT,
    "SQL_INSERT_RESEARCH_REPORT": SQL_INSERT_RESEARCH_REPORT,
    "SQL_COUNT_REPORTS_SINCE": SQL_COUNT_REPORTS_SINCE,
    "SQL_PENDING_ANALYSIS": SQL_PENDING_ANALYSIS,
    "SQL_PENDING_NOTIFICATION": SQL_PENDING_NOTIFICATION,
    "SQL_RESOLVE_DIGEST_ONLY": SQL_RESOLVE_DIGEST_ONLY,
    "SQL_EVENT_WITH_CONTEXT": SQL_EVENT_WITH_CONTEXT,
    "SQL_INSERT_NOTIFICATION_LOG": SQL_INSERT_NOTIFICATION_LOG,
    "SQL_NOTIFICATION_FAILURES_SINCE": SQL_NOTIFICATION_FAILURES_SINCE,
    "SQL_MARK_EVENT_ANALYZED": SQL_MARK_EVENT_ANALYZED,
    "SQL_MARK_EVENT_NOTIFIED": SQL_MARK_EVENT_NOTIFIED,
    "SQL_SUPPRESS_EVENT": SQL_SUPPRESS_EVENT,
    "SQL_COUNT_EVENTS_SINCE": SQL_COUNT_EVENTS_SINCE,
    "SQL_UPSERT_RISK_FLAG": SQL_UPSERT_RISK_FLAG,
    "SQL_RESOLVE_ABSENT_RISK_FLAGS": SQL_RESOLVE_ABSENT_RISK_FLAGS,
    "SQL_ACTIVE_RISK_FLAGS": SQL_ACTIVE_RISK_FLAGS,
    "SQL_WATCHLIST_DIAGNOSIS": SQL_WATCHLIST_DIAGNOSIS,
    "SQL_WATCHLIST_EVENT_TYPES": SQL_WATCHLIST_EVENT_TYPES,
    "SQL_DATABASE_SIZE": SQL_DATABASE_SIZE,
    "SQL_LAST_SYSTEM_ALERT": SQL_LAST_SYSTEM_ALERT,
    "SQL_RETIRE_LOW_LIQUIDITY_WATCHLIST": SQL_RETIRE_LOW_LIQUIDITY_WATCHLIST,
    "SQL_RETIRE_UNKNOWN_LIQUIDITY_WATCHLIST": SQL_RETIRE_UNKNOWN_LIQUIDITY_WATCHLIST,
    "SQL_RETIRE_UNEVENTFUL_WATCHLIST": SQL_RETIRE_UNEVENTFUL_WATCHLIST,
    "SQL_NEW_TOKEN_LIQUIDITY_SAMPLE": SQL_NEW_TOKEN_LIQUIDITY_SAMPLE,
    "SQL_SUMMARY_RUN_TOTALS": SQL_SUMMARY_RUN_TOTALS,
    "SQL_SUMMARY_ALERTS": SQL_SUMMARY_ALERTS,
    "SQL_SUMMARY_HIGHLIGHTS": SQL_SUMMARY_HIGHLIGHTS,
    "SQL_SUMMARY_WATCHLIST_DELTA": SQL_SUMMARY_WATCHLIST_DELTA,
    "SQL_SUMMARY_RISK_FLAGS_RAISED": SQL_SUMMARY_RISK_FLAGS_RAISED,
    "SQL_UPSERT_DAILY_SUMMARY": SQL_UPSERT_DAILY_SUMMARY,
    "SQL_DAILY_SUMMARY_FOR_DATE": SQL_DAILY_SUMMARY_FOR_DATE,
    "SQL_MARK_DAILY_SUMMARY_SENT": SQL_MARK_DAILY_SUMMARY_SENT,
}
