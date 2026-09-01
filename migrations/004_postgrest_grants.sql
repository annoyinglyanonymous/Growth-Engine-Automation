-- 004_postgrest_grants.sql
--
-- OPT-IN. Apply this only if you want n8n (or anything else holding a
-- service-role key) to call the KB directly over PostgREST, instead of going
-- through the FastAPI service.
--
-- This file is only the database half. PostgREST also will not see the schema
-- until you add `kb` to Exposed Schemas in the dashboard:
--     Project Settings -> API -> Exposed schemas -> add "kb"
-- That is a dashboard change and deliberately left to you.
--
-- Once both halves are done, n8n can POST to
--     https://<project>.supabase.co/rest/v1/rpc/search_kb
--     {"p_brand_slug": "renegade", "p_query": "franchise fee", "p_limit": 5}
-- with the service-role key in apikey + Authorization headers.
--
-- Why this is safe:
--   * anon and authenticated are granted NOTHING here. A browser-side anon key
--     cannot read the corpus or execute the functions even with kb exposed.
--   * kb tables keep RLS enabled with zero policies, so the only readers are
--     roles with BYPASSRLS -- postgres (the API and loader) and service_role.
--   * The functions are security invoker, so there is no privilege escalation
--     path and no definer function to audit.
--
-- A service-role key must never reach a browser or an untrusted n8n instance:
-- it bypasses RLS everywhere, not just here.

begin;

grant usage on schema kb to service_role;

grant select on kb.documents  to service_role;
grant select on kb.chunks     to service_role;
grant select on kb.entities   to service_role;
grant select on kb.conflicts  to service_role;

grant execute on function
    kb.search_kb(text, text, integer, text[], boolean) to service_role;
grant execute on function
    kb.brand_context(text) to service_role;

-- Anything created in kb later is readable by service_role without another
-- migration. Writes are still postgres-only.
alter default privileges in schema kb
    grant select on tables to service_role;

commit;
