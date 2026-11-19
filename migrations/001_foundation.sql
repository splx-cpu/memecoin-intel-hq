-- =============================================================================
-- 001_foundation.sql
-- Shared helpers every later migration relies on.
-- =============================================================================
--
-- CONVENTIONS USED THROUGHOUT THIS SCHEMA
--
--   timestamptz  Every point in time is stored WITH a timezone, always in UTC.
--                Display conversion to America/New_York happens in Python.
--                Never use plain `timestamp` - it silently loses the offset.
--
--   numeric      All prices, volumes and dollar amounts use `numeric`, never
--                `float`/`double precision`. Floats cannot represent decimal
--                fractions exactly, so a float price of 0.00000123 can come
--                back as 0.0000012299999. For money that is unacceptable.
--
--   NULL         Means "we do not have this data". The spec says never
--                fabricate missing data, so almost every market metric below
--                is nullable, and zero is reserved for a genuine zero.
--
--   jsonb        Each table that reads from an external API keeps the raw
--                response. That lets us re-derive a field later without
--                re-querying the provider, and proves what the source said.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- Automatic `updated_at` maintenance.
--
-- Rather than remembering to set updated_at in every UPDATE statement, we
-- attach this trigger to tables that are edited in place. Tables that are
-- append-only (market_snapshots, research_reports) do not need it.
-- -----------------------------------------------------------------------------
create or replace function set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

comment on function set_updated_at() is
    'Trigger function: stamps updated_at = now() on every UPDATE.';
