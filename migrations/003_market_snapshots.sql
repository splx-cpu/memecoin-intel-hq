-- =============================================================================
-- 003_market_snapshots.sql
-- Append-only history of market observations. The spec requires keeping
-- history rather than overwriting, so nothing here is ever UPDATEd.
-- =============================================================================
--
-- This is the table that grows fastest. See docs/ARCHITECTURE.md for the
-- retention plan; Supabase's free tier gives you 500 MB of database storage.
-- =============================================================================

create table if not exists market_snapshots (
    id                   bigserial    primary key,

    token_id             bigint       not null
                                      references tokens (id) on delete cascade,

    -- Denormalized copy of tokens.chain. Costs a little storage, saves a join
    -- on nearly every analytics query.
    chain                text         not null,
    pool_address         text,

    -- Which API this observation came from ('dexscreener' | 'geckoterminal').
    -- Two providers can disagree; keeping both lets us notice that instead of
    -- silently trusting one.
    source               text         not null,

    -- When WE fetched it. Always set.
    captured_at          timestamptz  not null default now(),
    -- When the PROVIDER says its data was last updated. NULL if not reported.
    provider_updated_at  timestamptz,

    -- --- Price and size. All nullable: NULL = provider did not report it. ---
    price_usd            numeric(38, 18),
    price_native         numeric(38, 18),
    liquidity_usd        numeric(28, 6),
    fdv_usd              numeric(28, 6),
    market_cap_usd       numeric(28, 6),

    -- --- Volume, by window ---
    volume_5m_usd        numeric(28, 6),
    volume_1h_usd        numeric(28, 6),
    volume_6h_usd        numeric(28, 6),
    volume_24h_usd       numeric(28, 6),

    -- --- Percent price change, by window. 12.34 means +12.34%. ---
    price_change_5m      numeric(14, 4),
    price_change_1h      numeric(14, 4),
    price_change_6h      numeric(14, 4),
    price_change_24h     numeric(14, 4),

    -- --- Transaction counts, by window ---
    buys_1h              integer,
    sells_1h             integer,
    buys_24h             integer,
    sells_24h            integer,

    -- Rarely available from DEX aggregators; stays NULL until we add a
    -- provider that reports it.
    holder_count         integer,

    -- Full provider response for audit and for fields we have not broken out.
    raw                  jsonb        not null default '{}'::jsonb,

    created_at           timestamptz  not null default now(),

    -- Guards against writing the identical observation twice when two scanner
    -- passes overlap.
    constraint market_snapshots_unique_obs
        unique (token_id, source, captured_at),

    constraint market_snapshots_non_negative check (
        (liquidity_usd  is null or liquidity_usd  >= 0) and
        (volume_24h_usd is null or volume_24h_usd >= 0) and
        (price_usd      is null or price_usd      >= 0)
    )
);

-- The workhorse index: "give me this token's recent history, newest first".
create index if not exists market_snapshots_token_time_idx
    on market_snapshots (token_id, captured_at desc);

create index if not exists market_snapshots_chain_time_idx
    on market_snapshots (chain, captured_at desc);

-- Supports the daily summary's "everything observed today" scan.
create index if not exists market_snapshots_captured_idx
    on market_snapshots (captured_at desc);

comment on table  market_snapshots is 'Append-only market observations. Never updated or overwritten.';
comment on column market_snapshots.source is 'Provider that supplied this row: dexscreener | geckoterminal.';
comment on column market_snapshots.captured_at is 'Time WE fetched the data, not the provider timestamp.';
