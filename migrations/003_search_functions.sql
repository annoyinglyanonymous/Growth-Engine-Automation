-- 003_search_functions.sql
--
-- The single ranking implementation. FastAPI calls these, and so can n8n via
-- PostgREST once 004 grants access -- so ranking never forks into two
-- implementations that drift apart.
--
-- Deliberately NOT in here: token budgeting, primer injection, and unverified-
-- claim labelling. Those live in the API layer, because they are packing
-- decisions rather than ranking ones. SQL returns ranked rows; Python decides
-- what fits in a context window and how it is labelled.
--
-- security invoker (the default) is correct. kb tables have RLS enabled with
-- no policies, so: the API connecting as postgres reads normally, n8n using a
-- service-role key reads normally (service_role has BYPASSRLS), and an anon
-- key gets nothing even if the schema is later exposed. No security definer,
-- no policies to audit.

begin;

-- ---------------------------------------------------------------------------
-- kb.search_kb -- brand-scoped full-text search over chunks.
--
-- p_brand_slug is required and has no default. A Renegade campaign grounding
-- itself in Agency Height copy is a silent failure that reads as plausible, so
-- the signature makes "search everything" unexpressible.
-- ---------------------------------------------------------------------------
create or replace function kb.search_kb(
    p_brand_slug   text,
    p_query        text,
    p_limit        integer  default 10,
    p_kinds        text[]   default array['page', 'faq', 'entity'],
    p_include_thin boolean  default false
)
returns table (
    chunk_id      bigint,
    doc_id        bigint,
    kind          text,
    title         text,
    url           text,
    category      text,
    heading_path  text,
    snippet       text,
    chunk_text    text,
    claim_like    boolean,
    date_modified timestamptz,
    rank          real
)
language sql
stable
as $$
    with q as (
        -- websearch_to_tsquery, not plainto_tsquery: it understands quoted
        -- phrases and -exclusions, which matters for jargon like
        -- "errors and omissions" and identifiers like E&O.
        select websearch_to_tsquery('english', p_query) as tsq
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
        d.date_modified,
        ts_rank_cd(c.tsv, q.tsq)
    from kb.chunks c
    join kb.documents d on d.doc_id = c.doc_id
    join public.brands b on b.id = c.brand_id
    cross join q
    where b.slug = p_brand_slug
      and c.tsv @@ q.tsq
      and d.kind = any(p_kinds)
      and (p_include_thin or not d.thin)
    -- chunk_id as tiebreaker so equal ranks return in a stable order; without
    -- it, paging and knowledge_snapshot reproducibility both wobble.
    order by ts_rank_cd(c.tsv, q.tsq) desc, c.chunk_id
    limit greatest(p_limit, 0);
$$;

comment on function kb.search_kb is
    'Brand-scoped BM25 search over kb.chunks. Returns ranked chunks with '
    'ts_headline snippets. Brand slug is required.';


-- ---------------------------------------------------------------------------
-- kb.brand_context -- the always-on part of an agent prompt.
--
-- The primer answers "who is this company" deterministically. Full-text search
-- is the wrong tool for that: it would be hoping about-us outranks 1,100 other
-- chunks on a vague query. Conflicts ride along so an answer can flag a
-- contradiction rather than silently pick a side.
-- ---------------------------------------------------------------------------
create or replace function kb.brand_context(p_brand_slug text)
returns jsonb
language sql
stable
as $$
    select jsonb_build_object(
        'brand_slug', b.slug,
        'brand_name', b.name,
        'primer', (
            select c.text
            from kb.documents d
            join kb.chunks c on c.doc_id = d.doc_id
            where d.brand_id = b.id and d.kind = 'primer'
            order by c.ordinal
            limit 1
        ),
        'conflicts', coalesce((
            select jsonb_agg(jsonb_build_object(
                       'topic', k.topic,
                       'detail', k.detail,
                       'severity', k.severity,
                       'sources', k.sources)
                   order by
                       case k.severity
                           when 'blocker' then 0
                           when 'warning' then 1
                           else 2
                       end,
                       k.topic)
            from kb.conflicts k
            where k.brand_id = b.id
        ), '[]'::jsonb),
        'corpus', jsonb_build_object(
            'documents', (select count(*) from kb.documents where brand_id = b.id),
            'chunks',    (select count(*) from kb.chunks    where brand_id = b.id),
            'newest_page', (select max(date_modified) from kb.documents
                            where brand_id = b.id)
        )
    )
    from public.brands b
    where b.slug = p_brand_slug;
$$;

comment on function kb.brand_context is
    'Always-inject context for a brand: hand-written primer, documented site '
    'contradictions, and corpus stats.';

commit;
