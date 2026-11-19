-- =============================================================================
-- 004_watchlists.sql
-- Which tokens the one-minute watchlist scanner should keep checking.
-- =============================================================================

create table if not exists watchlists (
    id                     bigserial    primary key,

    token_id               bigint       not null
                                        references tokens (id) on delete cascade,

    -- Which research bucket this token sits in. Fixed vocabulary, enforced by
    -- the database so a typo in code fails loudly instead of creating a
    -- phantom category.
    category               text         not null
                                        check (category in (
                                            'momentum',
                                            'new_coin',
                                            'risk',
                                            'long_term'
                                        )),

    -- Why it was added. Free text, written by the scanner, shown in alerts.
    added_reason           text,

    -- Lower number = checked first when the batch size limit bites.
    priority               smallint     not null default 100,

    -- Per-token override of WATCHLIST_INTERVAL_SECONDS. NULL = use the global
    -- setting from .env.
    scan_interval_seconds  integer      check (scan_interval_seconds is null
                                               or scan_interval_seconds >= 15),

    is_active              boolean      not null default true,

    last_scanned_at        timestamptz,
    -- Count of consecutive failed scans. Used to stop retrying a token whose
    -- pool has been removed from the provider.
    consecutive_failures   integer      not null default 0,

    added_at               timestamptz  not null default now(),
    removed_at             timestamptz,
    notes                  text,

    created_at             timestamptz  not null default now(),
    updated_at             timestamptz  not null default now()
);

-- A token may be on the watchlist only once at a time. A PARTIAL unique index
-- (the `where is_active` part) enforces that while still allowing historical
-- rows where is_active = false - so you keep the record of it having been
-- watched before, which a plain UNIQUE would forbid.
create unique index if not exists watchlists_one_active_per_token
    on watchlists (token_id)
    where is_active;

-- The exact query the watchlist scanner runs each minute.
create index if not exists watchlists_due_idx
    on watchlists (priority, last_scanned_at nulls first)
    where is_active;

create index if not exists watchlists_category_idx on watchlists (category) where is_active;

drop trigger if exists watchlists_set_updated_at on watchlists;
create trigger watchlists_set_updated_at
    before update on watchlists
    for each row execute function set_updated_at();

comment on table watchlists is 'Tokens under active monitoring, plus the history of ones removed.';
