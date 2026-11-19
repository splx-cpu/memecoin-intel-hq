-- =============================================================================
-- 009_daily_summaries.sql
-- One research summary per local calendar day.
-- =============================================================================

create table if not exists daily_summaries (
    id                   bigserial    primary key,

    -- The LOCAL calendar date the summary covers, in the configured timezone
    -- (TIMEZONE in .env). Stored as a plain date because "which day" is a
    -- local-calendar question, not an instant in time.
    summary_date         date         not null,

    -- Recorded alongside the date so a later timezone change cannot make old
    -- summaries ambiguous.
    timezone             text         not null,

    generated_at         timestamptz  not null default now(),

    -- Counts for the day: tokens discovered, alerts by category, scanner
    -- health, provider errors. Example:
    --   {"new_tokens": 142, "alerts": {"momentum": 6, "risk": 3},
    --    "scanner_runs": {"discovery": 288, "watchlist": 1440},
    --    "provider_errors": 4}
    stats                jsonb        not null default '{}'::jsonb,

    -- The written overview sent to Telegram.
    narrative            text,

    -- Ids of the research_reports linked from the summary.
    report_ids           jsonb        not null default '[]'::jsonb,

    -- Watchlist additions and removals during the day.
    watchlist_changes    jsonb        not null default '{}'::jsonb,

    status               text         not null default 'pending'
                                      check (status in ('pending', 'sent', 'failed')),
    telegram_message_id  bigint,

    created_at           timestamptz  not null default now(),
    updated_at           timestamptz  not null default now(),

    -- Exactly one summary per day. If the job runs twice, the second run
    -- updates this row instead of sending a second message.
    constraint daily_summaries_date_unique unique (summary_date)
);

create index if not exists daily_summaries_date_idx on daily_summaries (summary_date desc);

drop trigger if exists daily_summaries_set_updated_at on daily_summaries;
create trigger daily_summaries_set_updated_at
    before update on daily_summaries
    for each row execute function set_updated_at();

-- Now that daily_summaries exists, connect the column reserved in 008.
do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'notification_logs_daily_summary_fk'
    ) then
        alter table notification_logs
            add constraint notification_logs_daily_summary_fk
            foreign key (daily_summary_id)
            references daily_summaries (id) on delete cascade;
    end if;
end
$$;

comment on table  daily_summaries is 'One educational research summary per local calendar day.';
comment on column daily_summaries.summary_date is 'Local calendar date in `timezone`, not a UTC instant.';
