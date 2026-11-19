-- =============================================================================
-- 011_security_rls.sql
-- Row Level Security: deny-by-default for anyone using Supabase's public keys.
-- =============================================================================
--
-- WHAT THIS PROTECTS AGAINST
--
-- Every Supabase project exposes an automatic REST API over your tables,
-- reachable with the "anon" public key. If RLS is off, that API will happily
-- serve your tables to anyone who has that key - and the anon key is designed
-- to be embedded in front-end code, so treat it as public.
--
-- Enabling RLS with NO policies means: the anon and authenticated roles can
-- read and write nothing. The table is closed.
--
-- WHY THE APP STILL WORKS
--
-- This backend connects over a direct PostgreSQL connection as the table
-- owner, and a table's owner is exempt from its RLS policies. So the scanner
-- keeps full access while the public REST API returns nothing.
--
-- WHEN YOU BUILD THE DASHBOARD
--
-- Do NOT disable this. Either keep reading through the Python backend, or add
-- explicit read-only policies for the specific tables and columns the
-- dashboard needs. Opening everything up is the mistake this file exists to
-- prevent.
-- =============================================================================

alter table tokens             enable row level security;
alter table market_snapshots   enable row level security;
alter table watchlists         enable row level security;
alter table risk_flags         enable row level security;
alter table alert_events       enable row level security;
alter table research_reports   enable row level security;
alter table notification_logs  enable row level security;
alter table daily_summaries    enable row level security;
alter table scanner_runs       enable row level security;

-- schema_migrations is created by the Python migration runner, so it may not
-- exist yet the very first time this file runs. Guarded accordingly.
do $$
begin
    if exists (select 1 from pg_tables where tablename = 'schema_migrations') then
        execute 'alter table schema_migrations enable row level security';
    end if;
end
$$;

-- Intentionally no CREATE POLICY statements. No policies = no access for the
-- anon and authenticated roles.
