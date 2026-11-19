-- =============================================================================
-- 006_alert_events.sql
-- A detected, notification-worthy change. Created BEFORE any Claude call or
-- Telegram send, which is what makes duplicate suppression possible.
-- =============================================================================
--
-- THE DEDUPLICATION DESIGN - the single most important idea in this file.
--
-- `event_key` is a deterministic string the detector builds from the facts of
-- the event, for example:
--
--     momentum:base:0xabc...:vol_spike:2026-11-15T04:35
--
-- Because the key includes a rounded time bucket, the same spike detected on
-- three consecutive one-minute passes produces the SAME key. The unique
-- constraint then makes the second and third inserts fail harmlessly, so only
-- one Telegram message is ever sent. No "have I already alerted this?" lookup
-- that can race with itself - the database decides.
-- =============================================================================

create table if not exists alert_events (
    id                  bigserial    primary key,

    -- See the explanation above. This is the duplicate-alert guard.
    event_key           text         not null,

    token_id            bigint       not null
                                     references tokens (id) on delete cascade,
    chain               text         not null,

    -- Which of the four notification types this will become.
    category            text         not null
                                     check (category in (
                                         'momentum',
                                         'new_coin',
                                         'risk',
                                         'long_term'
                                     )),

    -- Finer-grained detector name, e.g. 'volume_spike', 'liquidity_drain',
    -- 'first_seen', 'holder_concentration'.
    event_type          text         not null,

    severity            text         not null default 'info'
                                     check (severity in ('info', 'low', 'medium', 'high', 'critical')),

    triggered_at        timestamptz  not null default now(),

    -- The before/after numbers that tripped the detector. This is what gets
    -- handed to Claude, and what the alert quotes as its evidence.
    payload             jsonb        not null default '{}'::jsonb,

    -- The observation that triggered it. ON DELETE SET NULL so pruning old
    -- snapshots under the retention policy never deletes alert history.
    snapshot_id         bigint       references market_snapshots (id) on delete set null,

    -- --- Pipeline state. Each step flips one flag, so a crash mid-pipeline
    -- --- resumes instead of re-sending. ---
    is_analyzed         boolean      not null default false,
    is_notified         boolean      not null default false,
    is_suppressed       boolean      not null default false,
    suppressed_reason   text,

    created_at          timestamptz  not null default now(),
    updated_at          timestamptz  not null default now(),

    constraint alert_events_event_key_unique unique (event_key)
);

create index if not exists alert_events_token_time_idx on alert_events (token_id, triggered_at desc);
create index if not exists alert_events_time_idx       on alert_events (triggered_at desc);
create index if not exists alert_events_category_idx   on alert_events (category, triggered_at desc);

-- Partial indexes for the two queue queries the pipeline runs constantly:
-- "what still needs analysis" and "what still needs sending".
create index if not exists alert_events_pending_analysis_idx
    on alert_events (triggered_at)
    where not is_analyzed and not is_suppressed;

create index if not exists alert_events_pending_notify_idx
    on alert_events (triggered_at)
    where not is_notified and not is_suppressed;

drop trigger if exists alert_events_set_updated_at on alert_events;
create trigger alert_events_set_updated_at
    before update on alert_events
    for each row execute function set_updated_at();

comment on table  alert_events is 'Detected events awaiting analysis and notification.';
comment on column alert_events.event_key is 'Deterministic dedupe key; the unique constraint prevents duplicate alerts.';
