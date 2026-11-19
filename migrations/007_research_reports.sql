-- =============================================================================
-- 007_research_reports.sql
-- Claude's structured research output, stored append-only.
-- =============================================================================
--
-- The spec requires verified facts to stay separable from assumptions. That is
-- why they are three different columns rather than one blob of prose: the
-- Telegram formatter can then label each section honestly, and a reader can
-- see exactly which claims rest on fetched data.
-- =============================================================================

create table if not exists research_reports (
    id                bigserial    primary key,

    token_id          bigint       not null
                                   references tokens (id) on delete cascade,

    -- The event that prompted this analysis. NULL for reports generated
    -- outside the alert pipeline, e.g. an on-demand lookup.
    alert_event_id    bigint       references alert_events (id) on delete set null,

    category          text         not null
                                   check (category in (
                                       'momentum',
                                       'new_coin',
                                       'risk',
                                       'long_term'
                                   )),

    -- Which model and prompt produced this. Essential later: when you change
    -- the prompt, you need to know which reports came from the old one.
    model             text         not null,
    prompt_version    text         not null default 'v1',

    -- --- The report itself ---
    headline          text,
    summary           text         not null,
    what_changed      text,

    -- Arrays of strings, kept as jsonb. Example:
    --   verified_facts: ["24h volume 412,000 USD (DEX Screener, 04:31 UTC)"]
    --   assumptions:    ["volume pattern resembles organic interest"]
    --   missing_data:   ["holder distribution not available from either provider"]
    --   risks:          ["liquidity 18,000 USD is thin relative to volume"]
    --   sources:        [{"label": "DEX Screener", "url": "https://..."}]
    verified_facts    jsonb        not null default '[]'::jsonb,
    assumptions       jsonb        not null default '[]'::jsonb,
    missing_data      jsonb        not null default '[]'::jsonb,
    risks             jsonb        not null default '[]'::jsonb,
    sources           jsonb        not null default '[]'::jsonb,

    -- Claude's own stated confidence in its reading of the data.
    confidence        text         check (confidence is null
                                          or confidence in ('low', 'medium', 'high')),

    -- Unmodified API response, so a parsing change can be re-run later
    -- without paying for the call again.
    raw_response      jsonb        not null default '{}'::jsonb,

    -- --- Cost tracking. Claude is the only metered dependency, so every
    -- --- call records its usage and the daily cap is enforced from here. ---
    input_tokens      integer,
    output_tokens     integer,
    cost_usd          numeric(12, 6),
    latency_ms        integer,

    created_at        timestamptz  not null default now()
);

create index if not exists research_reports_token_time_idx
    on research_reports (token_id, created_at desc);

create index if not exists research_reports_event_idx
    on research_reports (alert_event_id);

create index if not exists research_reports_time_idx
    on research_reports (created_at desc);

-- NOTE on the daily spend cap: count analyses with a RANGE query against
-- created_at, for example
--     where created_at >= $start_of_local_day and created_at < $end_of_local_day
-- and let research_reports_time_idx above serve it. Do not add an index on
-- date(created_at): that function depends on the session timezone, so
-- Postgres rejects it in an index ("must be marked IMMUTABLE"). A range query
-- is also the correct approach when "today" means a local calendar day.

comment on table  research_reports is 'Claude-generated research notes. Educational only, never investment advice.';
comment on column research_reports.verified_facts is 'Claims backed by fetched provider data, each with its source.';
comment on column research_reports.assumptions is 'Explicitly labelled inferences. Never presented to the user as fact.';


-- -----------------------------------------------------------------------------
-- Link the other direction: alert_events -> its report.
--
-- Added by ALTER rather than in 006 because the two tables reference each
-- other, and the second table has to exist before its foreign key can.
-- -----------------------------------------------------------------------------
alter table alert_events
    add column if not exists research_report_id bigint;

do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'alert_events_research_report_fk'
    ) then
        alter table alert_events
            add constraint alert_events_research_report_fk
            foreign key (research_report_id)
            references research_reports (id) on delete set null;
    end if;
end
$$;
