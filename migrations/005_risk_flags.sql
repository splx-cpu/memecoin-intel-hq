-- =============================================================================
-- 005_risk_flags.sql
-- Observed risk indicators. Educational findings, not investment advice.
-- =============================================================================

create table if not exists risk_flags (
    id            bigserial    primary key,

    token_id      bigint       not null
                               references tokens (id) on delete cascade,

    -- Machine-readable kind of risk, e.g. 'holder_concentration',
    -- 'low_liquidity', 'mint_authority_present', 'liquidity_unlocked',
    -- 'missing_verification', 'wash_trading_suspected'.
    flag_type     text         not null,

    severity      text         not null
                               check (severity in ('info', 'low', 'medium', 'high', 'critical')),

    -- One-sentence human explanation shown in the Telegram risk warning.
    detail        text         not null,

    -- The numbers behind the flag, so the alert can show its reasoning and a
    -- reader can judge it. Example:
    --   {"top10_holder_pct": 78.4, "measured_at": "2026-11-15T04:00:00Z"}
    evidence      jsonb        not null default '{}'::jsonb,

    -- Where the finding came from: a provider name, or 'heuristic' when our
    -- own rules derived it. Keeps verified facts separable from inferences.
    source        text         not null default 'heuristic',

    detected_at   timestamptz  not null default now(),
    -- Set when the condition no longer holds. Rows are resolved, not deleted,
    -- so the risk history survives.
    resolved_at   timestamptz,
    is_active     boolean      not null default true,

    created_at    timestamptz  not null default now(),
    updated_at    timestamptz  not null default now()
);

-- One ACTIVE flag of a given type per token. Stops the risk scanner from
-- re-raising "low liquidity" every single minute, while keeping resolved
-- historical rows.
create unique index if not exists risk_flags_one_active_per_type
    on risk_flags (token_id, flag_type)
    where is_active;

create index if not exists risk_flags_token_idx     on risk_flags (token_id, detected_at desc);
create index if not exists risk_flags_severity_idx  on risk_flags (severity, detected_at desc) where is_active;

drop trigger if exists risk_flags_set_updated_at on risk_flags;
create trigger risk_flags_set_updated_at
    before update on risk_flags
    for each row execute function set_updated_at();

comment on table  risk_flags is 'Observed risk indicators per token. Educational, not advice.';
comment on column risk_flags.source is 'Provider name for verified data, or "heuristic" for our own derivation.';
