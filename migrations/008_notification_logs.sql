-- =============================================================================
-- 008_notification_logs.sql
-- One row per delivery ATTEMPT, so retries are visible rather than hidden.
-- =============================================================================
--
-- Why log attempts instead of just "sent / not sent": when a Telegram message
-- fails you need to know whether it failed once and succeeded on retry, or
-- failed four times and was given up on. A single status column cannot tell
-- you that. The health monitor counts failures from this table.
-- =============================================================================

create table if not exists notification_logs (
    id                   bigserial    primary key,

    alert_event_id       bigint       references alert_events (id) on delete cascade,

    -- NULL for alert notifications; set for the once-a-day summary message.
    daily_summary_id     bigint,      -- FK added in 009, after that table exists

    channel              text         not null default 'telegram',

    -- Which of the four notification types, or 'daily_summary' / 'system' for
    -- health and error reports to the admin chat.
    message_type         text         not null
                                      check (message_type in (
                                          'momentum',
                                          'new_coin',
                                          'risk',
                                          'long_term',
                                          'daily_summary',
                                          'system'
                                      )),

    -- Stored as text: Telegram chat ids can be negative (groups) and large.
    chat_id              text,

    status               text         not null default 'pending'
                                      check (status in ('pending', 'sent', 'failed', 'skipped')),

    -- 1 for the first try, 2 for the first retry, and so on.
    attempt              integer      not null default 1
                                      check (attempt >= 1),

    -- Returned by Telegram on success. Lets a later message reply to this one.
    telegram_message_id  bigint,

    -- Telegram's HTTP status and description on failure, e.g. 429 with
    -- "Too Many Requests: retry after 31".
    error_code           integer,
    error_message        text,

    -- What we sent, with secrets excluded. Useful when a message renders
    -- wrongly and you need the exact text that produced it.
    request_payload      jsonb        not null default '{}'::jsonb,

    sent_at              timestamptz,
    created_at           timestamptz  not null default now(),

    -- A given event may be attempted several times, but never twice with the
    -- same attempt number - which is what would happen if a retry loop
    -- double-fired.
    constraint notification_logs_attempt_unique
        unique (alert_event_id, attempt, message_type)
);

create index if not exists notification_logs_event_idx   on notification_logs (alert_event_id);
create index if not exists notification_logs_status_idx  on notification_logs (status, created_at desc);
create index if not exists notification_logs_time_idx    on notification_logs (created_at desc);

-- Powers "are deliveries failing right now?" in the health monitor.
create index if not exists notification_logs_failed_idx
    on notification_logs (created_at desc)
    where status = 'failed';

comment on table  notification_logs is 'Every Telegram delivery attempt, including retries and failures.';
comment on column notification_logs.chat_id is 'Text, not bigint: group chat ids are negative and can exceed int range.';
