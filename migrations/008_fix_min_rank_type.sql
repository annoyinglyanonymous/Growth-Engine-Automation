-- 008_fix_min_rank_type.sql
--
-- 007 declared p_min_rank as `real`. Every client sends a float, which
-- psycopg (and PostgREST, and anything else) adapts as `double precision` --
-- and Postgres will not implicitly downcast double precision to real during
-- function resolution. So every call from Python failed with:
--
--   function kb.search_kb(unknown, unknown, smallint, unknown, boolean,
--                         double precision) does not exist
--
-- `real` was tidy-looking because ts_rank_cd returns real, but the comparison
-- promotes real -> double precision perfectly well. Declaring the parameter as
-- double precision costs nothing and removes the friction for every caller.
--
-- Casting at the call site would have fixed FastAPI and left the same trap for
-- n8n, so the fix belongs in the signature.

begin;

drop function if exists kb.search_kb(text, text, integer, text[], boolean, real);

create function kb.search_kb(
    p_brand_slug   text,
    p_query        text,
    p_limit        integer  default 10,
    p_kinds        text[]   default array['page', 'faq', 'entity'],
    p_include_thin boolean  default false,
    -- Real lexical matches score 0.20 to 2.60. Paraphrase queries that share
    -- only a common stem score 0.001 to 0.005 -- a ~500x gap, so the signal
    -- separates cleanly. Returning those anyway hands the agent noise it
    -- cannot distinguish from evidence, and would poison any future RRF
    -- fusion, which ranks by position and cannot see that 0.005 is garbage.
    p_min_rank     double precision default 0.01
)
returns table (
    chunk_id       bigint,
    doc_id         bigint,
    kind           text,
    title          text,
    url            text,
    category       text,
    heading_path   text,
    snippet        text,
    chunk_text     text,
    claim_like     boolean,
    disputed       boolean,
    dispute_topics text[],
    blocked        boolean,
    date_modified  timestamptz,
    rank           real
)
language sql
stable
as $$
    with q as (
        select websearch_to_tsquery('english', p_query) as tsq
    ),
    -- coalesce, because `kind = any(null)` is NULL: passing p_kinds => null
    -- silently returned zero rows with no error.
    kinds as (
        select coalesce(p_kinds, array['page', 'faq', 'entity']) as k
    )
    select
        c.chunk_id,
        c.doc_id,
        d.kind,
        d.title,
        d.url,
        d.category,
        c.heading_path,
        ts_headline('english', c.text, q.tsq,
                    'MaxWords=40, MinWords=20, ShortWord=3, MaxFragments=2, '
                    'FragmentDelimiter=" … ", StartSel="**", StopSel="**"'),
        c.text,
        c.claim_like,
        (x.doc_id is not null),
        coalesce(x.topics, '{}'),
        -- A primer is never blocked. The Renegade primer contains "$20,000"
        -- precisely in order to warn against it -- blocking the one document
        -- that explains why the figure is wrong would be exactly backwards.
        (d.kind <> 'primer' and exists (
            select 1
            from kb.conflicts k2
            cross join unnest(k2.stale_values) sv
            where k2.brand_id = c.brand_id
              and c.text like '%' || sv || '%'
        )),
        d.date_modified,
        ts_rank_cd(c.tsv, q.tsq)
    from kb.chunks c
    join kb.documents d on d.doc_id = c.doc_id
    join public.brands b on b.id = c.brand_id
    cross join q
    cross join kinds
    left join kb.document_disputes x on x.doc_id = c.doc_id
    where b.slug = p_brand_slug
      and c.tsv @@ q.tsq
      and d.kind = any(kinds.k)
      and (p_include_thin or not d.thin)
      and ts_rank_cd(c.tsv, q.tsq) >= p_min_rank
    order by ts_rank_cd(c.tsv, q.tsq) desc, c.chunk_id
    limit greatest(p_limit, 0);
$$;

comment on function kb.search_kb is
    'Brand-scoped full-text search over kb.chunks. Site copy is usable by '
    'default; disputed flags a passage whose page is contradicted, blocked '
    'flags one containing a known-wrong figure. Brand slug is required.';

grant execute on function
    kb.search_kb(text, text, integer, text[], boolean, double precision)
    to service_role;

commit;
