-- =============================================================================
-- 010_scanner_runs.sql
-- Health and observability: one row per scanner execution.
-- =============================================================================
--
-- This is how the system knows it is alive. The health monitor asks a single
-- question - "when did a scanner last finish successfully?" - and alerts the
-- admin chat when the answer is too long ago. Without this table, a scanner
-- that silently dies looks exactly like a quiet market.
-- =============================================================================

create table if not exists scanner_runs (
    id                 bigserial    primary key,

    -- Which process this was.
    scanner            text         not null
                                    check (scanner in (
                                        'discovery',
                                        'watchlist',
                                        'daily_summary',
                                        'health_check'
                                    )),

    started_at         timestamptz  not null default now(),
    finished_at        timestamptz,
    duration_ms        integer,

    -- 'running' is written at the start and replaced at the end. A row still
    -- marked 'running' long after started_at means the process was killed
    -- mid-pass, which is itself a useful signal.
    status             text         not null default 'running'
                                    check (status in ('running', 'success', 'partial', 'failed')),

    chains_scanned     text[]       not null default '{}',

    -- --- Work done ---
    tokens_seen        integer      not null default 0,
    new_tokens         integer      not null default 0,
    snapshots_written  integer      not null default 0,
    events_created     integer      not null default 0,
    events_suppressed  integer      not null default 0,
    analyses_run       integer      not null default 0,
    alerts_sent        integer      not null default 0,

    -- --- Failures, counted by subsystem so the monitor can say WHAT broke ---
    provider_errors    integer      not null default 0,
    claude_errors      integer      not null default 0,
    telegram_errors    integer      not null default 0,
    database_errors    integer      not null default 0,

    -- First fatal error message, if the run failed.
    error_message      text,

    -- Per-provider timings, rate-limit waits, cache hit counts.
    details            jsonb        not null default '{}'::jsonb,

    created_at         timestamptz  not null default now()
);

create index if not exists scanner_runs_scanner_time_idx
    on scanner_runs (scanner, started_at desc);

-- THE health query: most recent successful run of each scanner.
create index if not exists scanner_runs_last_success_idx
    on scanner_runs (scanner, finished_at desc)
    where status = 'success';

create index if not exists scanner_runs_failures_idx
    on scanner_runs (started_at desc)
    where status in ('failed', 'partial');

comment on table  scanner_runs is 'One row per scanner execution. Source of truth for system health.';
comment on column scanner_runs.status is 'running until the pass completes; a stale "running" row means the process died.';
