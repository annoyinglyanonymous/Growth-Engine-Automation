-- 009_hybrid_retrieval.sql
--
-- Adds a vector arm to kb.search_kb and fuses it with the existing lexical arm
-- using reciprocal rank fusion.
--
-- WHY: measured on the live corpus, lexical search answers keyword queries well
-- (0.20-2.60 on real matches) and fails completely on the queries an LLM
-- actually emits. websearch_to_tsquery ANDs every term, so a five-term topic
-- string needs all five stems inside one chunk:
--
--     "franchise ownership back office support"  -> 0 passages
--     "franchise ownership"                      -> 2 passages
--     "how do agents get paid"                   -> 0 passages
--     "why use a broker instead of buying direct"-> 0 passages
--
-- A real generation run against the first of those produced five plausible
-- headlines grounded in nothing but the primer, and the failure was invisible
-- in the output.
--
-- `errors and omissions` is the sharpest example, and not in the direction it
-- first appears. It returns ZERO rows for Renegade lexically -- but Renegade
-- does sell it: 3 chunks say "E&O" and 14 say "professional liability",
-- including the brand primer's commercial-lines list. The lexical miss is pure
-- vocabulary mismatch, and it is the single best argument for the vector arm
-- rather than a case against it.
--
-- WHAT IS STILL DELIBERATELY PRESERVED: correct emptiness. Vector search always
-- returns its top k, so a query whose subject genuinely is not in the corpus --
-- "how do I handle a lapsed policy renewal", procedural content that a scraped
-- MARKETING corpus simply does not contain -- would come back with the five
-- least-unrelated passages and the agent would write from them. p_max_distance
-- is the guard: the vector arm contributes nothing when nothing is close.
--
-- TWO FLOORS, ONE PER ARM, and they are not interchangeable:
--   p_min_rank     lexical noise floor  (ts_rank_cd, default 0.01)
--   p_max_distance vector relevance cap (cosine distance, default 0.45)
--
-- p_max_distance IS A STARTING POINT AND MUST BE CALIBRATED once the corpus is
-- embedded. See scripts/calibrate_retrieval.py. Too loose and E&O starts
-- returning insurance-adjacent noise; too tight and paraphrase recall dies.
--
-- DIMENSION: 1536, and the choice is load-bearing.
--   * pgvector's hnsw index rejects `vector` columns above 2,000 dimensions,
--     so gemini-embedding-2's native 3072 cannot be indexed this way.
--   * gemini-embedding-2 returns UNIT-NORMALISED vectors when truncated to
--     1536 (measured norm 1.0000). gemini-embedding-001 does NOT -- truncating
--     it to 1536 yields norm 0.6935, raw Matryoshka output. Cosine distance is
--     scale-invariant so it would still rank, but any later switch to inner
--     product would silently break. Use gemini-embedding-2.
--   * Halves storage and index size versus 3072 for negligible quality loss.
-- Changing this means a new migration AND a full re-embed.

begin;

-- Supabase keeps extensions out of public; pg_trgm already lives here, so the
-- operator classes below must be schema-qualified to match.
create extension if not exists vector with schema extensions;

alter table kb.chunks
    add column embedding extensions.vector(1536),
    -- Which model produced it. Without this, a model swap is undetectable and
    -- you end up silently comparing vectors from two different spaces.
    add column embedding_model text,
    add column embedded_at timestamptz;

comment on column kb.chunks.embedding is
    'gemini-embedding-2 truncated to 1536 dims (unit-normalised at that size). '
    'NULL until Ingestion/embed.py has run; search_kb degrades to lexical-only '
    'for un-embedded rows rather than failing.';

-- Built on an all-NULL column, which is fine and cheap: hnsw indexes only
-- non-null rows, so this costs nothing until embed.py populates it.
create index chunks_embedding_idx on kb.chunks
    using hnsw (embedding extensions.vector_cosine_ops);

-- Finding un-embedded or stale-model rows is the embedder's inner loop.
create index chunks_embedding_pending_idx on kb.chunks (chunk_id)
    where embedding is null;


-- ---------------------------------------------------------------------------
-- kb.search_kb -- lexical + vector, fused.
--
-- The query embedding is an INPUT, not something this function computes: SQL
-- cannot call an embedding API. The caller embeds the query and passes the
-- vector. Passing NULL is the supported degraded path -- if the embedding
-- provider is down or the caller skips it, search falls back to lexical and
-- keeps working rather than erroring.
--
-- Return type changes again, so drop rather than replace.
-- ---------------------------------------------------------------------------
drop function if exists kb.search_kb(text, text, integer, text[], boolean,
                                     double precision);

create function kb.search_kb(
    p_brand_slug      text,
    p_query           text,
    p_limit           integer default 10,
    p_kinds           text[]  default array['page', 'faq', 'entity'],
    p_include_thin    boolean default false,
    p_min_rank        double precision default 0.01,
    p_query_embedding extensions.vector(1536) default null,
    p_mode            text    default 'hybrid',
    p_max_distance    double precision default 0.45
)
returns table (
    chunk_id        bigint,
    doc_id          bigint,
    kind            text,
    title           text,
    url             text,
    category        text,
    heading_path    text,
    snippet         text,
    chunk_text      text,
    claim_like      boolean,
    disputed        boolean,
    dispute_topics  text[],
    blocked         boolean,
    date_modified   timestamptz,
    -- The fused RRF score. Always populated, which is why it is `rank`: a
    -- vector-only hit has no lexical rank, and callers order on this.
    rank            double precision,
    -- Diagnostics. lexical_rank is NULL for vector-only hits and vice versa;
    -- matched_by makes a thin result set explainable instead of mysterious.
    lexical_rank    real,
    vector_distance double precision,
    matched_by      text
)
language sql
stable
as $$
    with q as (
        -- websearch_to_tsquery, not plainto_tsquery: it understands quoted
        -- phrases and -exclusions, which matters for jargon like
        -- "errors and omissions" and identifiers like E&O.
        select websearch_to_tsquery('english', p_query) as tsq
    ),
    kinds as (
        -- coalesce, because `kind = any(null)` is NULL: passing p_kinds => null
        -- silently returned zero rows with no error.
        select coalesce(p_kinds, array['page', 'faq', 'entity']) as k
    ),
    -- Brand, kind and thin filtering happen once and both arms read from here.
    -- Brand scoping is not optional: three Agency Height competitor articles
    -- quote "80% commission" about Brightway and Goosehead, and a Renegade
    -- campaign must never be able to reach them.
    base as (
        select c.chunk_id, c.doc_id, c.text, c.tsv, c.embedding, c.brand_id,
               c.heading_path, c.claim_like,
               d.kind, d.title, d.url, d.category, d.date_modified
        from kb.chunks c
        join kb.documents d on d.doc_id = c.doc_id
        join public.brands b on b.id = c.brand_id
        cross join kinds
        where b.slug = p_brand_slug
          and d.kind = any (kinds.k)
          and (p_include_thin or not d.thin)
    ),
    -- Over-fetch both arms so fusion has something to fuse; a document ranked
    -- 12th lexically and 3rd by vector should still surface.
    lex as (
        select base.chunk_id,
               ts_rank_cd(base.tsv, q.tsq) as rank,
               row_number() over (order by ts_rank_cd(base.tsv, q.tsq) desc,
                                           base.chunk_id) as pos
        from base cross join q
        where p_mode in ('lexical', 'hybrid')
          and base.tsv @@ q.tsq
          and ts_rank_cd(base.tsv, q.tsq) >= p_min_rank
        order by rank desc, base.chunk_id
        limit greatest(p_limit * 4, 40)
    ),
    vec as (
        select base.chunk_id,
               (base.embedding operator(extensions.<=>) p_query_embedding)
                   as distance,
               row_number() over (
                   order by base.embedding operator(extensions.<=>)
                            p_query_embedding) as pos
        from base
        where p_mode in ('vector', 'hybrid')
          and p_query_embedding is not null
          and base.embedding is not null
          and (base.embedding operator(extensions.<=>) p_query_embedding)
              <= p_max_distance
        order by distance
        limit greatest(p_limit * 4, 40)
    ),
    -- Reciprocal rank fusion. k=60 is the standard constant from the original
    -- Cormack et al. paper; it flattens the head so a confident #1 in one arm
    -- cannot bulldoze a broad consensus in the other.
    --
    -- RRF scores POSITION, not the underlying rank value. That is precisely why
    -- p_min_rank has to filter the lexical arm BEFORE this point: a 0.005
    -- stem-collision hit sitting at lexical position 3 would otherwise earn the
    -- same fusion weight as a genuine 2.60 match at position 3.
    fused as (
        select coalesce(l.chunk_id, v.chunk_id) as chunk_id,
               coalesce(1.0 / (60 + l.pos), 0.0)
                 + coalesce(1.0 / (60 + v.pos), 0.0) as score,
               l.rank     as lexical_rank,
               v.distance as vector_distance,
               case when l.chunk_id is not null and v.chunk_id is not null
                        then 'both'
                    when l.chunk_id is not null then 'lexical'
                    else 'vector'
               end as matched_by
        from lex l
        full outer join vec v on v.chunk_id = l.chunk_id
    )
    select
        b.chunk_id,
        b.doc_id,
        b.kind,
        b.title,
        b.url,
        b.category,
        b.heading_path,
        -- For a vector-only hit the tsquery matches nothing and ts_headline
        -- returns the head of the passage. That is the right fallback: still a
        -- readable preview, just without highlighting.
        ts_headline('english', b.text, q.tsq,
                    'MaxWords=40, MinWords=20, ShortWord=3, MaxFragments=2, '
                    'FragmentDelimiter=" … ", StartSel="**", StopSel="**"'),
        b.text,
        b.claim_like,
        (x.doc_id is not null),
        coalesce(x.topics, '{}'),
        -- A primer is never blocked. The Renegade primer contains "$20,000"
        -- precisely in order to warn against it -- blocking the one document
        -- that explains why the figure is wrong would be exactly backwards.
        (b.kind <> 'primer' and exists (
            select 1
            from kb.conflicts k2
            cross join unnest(k2.stale_values) sv
            where k2.brand_id = b.brand_id
              and b.text like '%' || sv || '%'
        )),
        b.date_modified,
        f.score,
        f.lexical_rank,
        f.vector_distance,
        f.matched_by
    from fused f
    join base b on b.chunk_id = f.chunk_id
    cross join q
    left join kb.document_disputes x on x.doc_id = b.doc_id
    -- chunk_id as tiebreaker so equal scores return in a stable order; without
    -- it, paging and knowledge_snapshot reproducibility both wobble.
    order by f.score desc, b.chunk_id
    limit greatest(p_limit, 0);
$$;

comment on function kb.search_kb is
    'Brand-scoped hybrid search over kb.chunks: lexical (tsvector) fused with '
    'vector (cosine) by reciprocal rank fusion. Caller supplies the query '
    'embedding; NULL degrades to lexical-only. p_min_rank floors the lexical '
    'arm, p_max_distance caps the vector arm so semantically-absent topics '
    'still return nothing. Brand slug is required.';

grant execute on function
    kb.search_kb(text, text, integer, text[], boolean, double precision,
                 extensions.vector, text, double precision)
    to service_role;

commit;
