-- =============================================================================
-- 002_tokens.sql
-- One row per token, per chain. The anchor every other table points at.
-- =============================================================================

create table if not exists tokens (
    id                   bigserial    primary key,

    -- Our internal chain key from src/mihq/chains.py ('solana', 'base', ...).
    -- Deliberately plain text with no foreign key to a chains table: adding a
    -- new chain must not require a migration, only a registry entry.
    chain                text         not null,

    -- The token's contract/mint address, already normalized by
    -- chains.normalize_address(): lowercase for EVM, unchanged for Solana.
    token_address        text         not null,

    symbol               text,
    name                 text,
    decimals             integer,

    -- The pool/pair we most often read prices from. A token can trade in many
    -- pools; this is just the one we default to.
    primary_pool_address text,

    -- When the token's first pool was created, AS REPORTED by the provider.
    -- NULL when the provider does not tell us - this is the field that decides
    -- whether something counts as a "new" token, so it must never be guessed.
    pair_created_at      timestamptz,

    -- When WE first saw it, and when we last successfully read data for it.
    first_seen_at        timestamptz  not null default now(),
    last_seen_at         timestamptz,

    -- Provider payload for anything not broken out into columns above.
    metadata             jsonb        not null default '{}'::jsonb,

    created_at           timestamptz  not null default now(),
    updated_at           timestamptz  not null default now(),

    -- THE most important constraint in the schema. Without it, the discovery
    -- scanner inserts the same token on every pass and you get duplicate
    -- alerts forever.
    constraint tokens_chain_address_key unique (chain, token_address),

    constraint tokens_decimals_sane check (decimals is null or decimals between 0 and 36)
);

create index if not exists tokens_chain_idx            on tokens (chain);
create index if not exists tokens_first_seen_idx       on tokens (first_seen_at desc);
create index if not exists tokens_pair_created_idx     on tokens (pair_created_at desc nulls last);
-- Case-insensitive symbol search for a future dashboard search box.
create index if not exists tokens_symbol_lower_idx     on tokens (lower(symbol));

drop trigger if exists tokens_set_updated_at on tokens;
create trigger tokens_set_updated_at
    before update on tokens
    for each row execute function set_updated_at();

comment on table  tokens is 'Every token we have ever observed, one row per (chain, address).';
comment on column tokens.token_address is 'Normalized: lowercase for EVM chains, case-preserved base58 for Solana.';
comment on column tokens.pair_created_at is 'Provider-reported pool creation time. NULL means unknown - never inferred.';
