-- 007_dispute_scoped_labelling.sql
--
-- Inverts the labelling default.
--
-- BEFORE: any chunk containing a figure was labelled "unverified, do not
-- restate as fact" -- 314 of 1,119 chunks, 28% of the corpus. The premise was
-- that scraped copy is untrusted until a human approves it claim by claim.
--
-- That premise is wrong for this project. The corpus is the company's OWN
-- published website. Copy that is already public is not new risk when it is
-- repeated in a campaign; it was cleared once, at publication. Blocking 314
-- chunks because 5 topics are contradicted is the wrong trade, and it made the
-- single strongest line in the franchise pitch -- "80% commission", published
-- in 7 places -- unusable.
--
-- AFTER: site copy is usable by default. A passage is labelled only when it is
-- ACTUALLY disputed:
--   * its source page is cited by a row in kb.conflicts, or
--   * it contains a figure listed in that brand's stale_values.
-- That is 37 chunks, 3.3%.
--
-- claim_like is kept and still returned. It remains a useful signal -- it is
-- how the 5 conflicts were found in the first place, and it is what a future
-- claims-review pass would work through. It just no longer gates usability.
--
-- Also fixes two bugs in search_kb that are unrelated to labelling but live in
-- the same function: no rank floor, and p_kinds => null returning nothing.

begin;

-- ---------------------------------------------------------------------------
-- Which documents are disputed, and how.
--
-- The join is on url rather than doc_id because conflicts are written by hand
-- against public URLs, and one URL legitimately maps to many documents: a page
-- plus every FAQ pair scraped from it. franchise-fee cites 2 URLs and reaches
-- 16 documents that way, 15 of them FAQ answers that read as standalone facts
-- with nothing to indicate the page behind them is contradicted.
-- ---------------------------------------------------------------------------
create or replace view kb.document_disputes as
select
    d.doc_id,
    d.brand_id,
    array_agg(distinct k.topic order by k.topic)          as topics,
    bool_or(k.status = 'active')                          as has_open_dispute,
    -- Worst severity across every conflict touching this document.
    min(case k.severity when 'blocker' then 0
                        when 'warning' then 1
                        else 2 end)                       as severity_rank,
    array_remove(array_agg(distinct k.correct_value), null) as correct_values,
    coalesce(array_agg(distinct v) filter (where v is not null), '{}') as stale_values
from kb.documents d
join kb.conflicts k
  on k.brand_id = d.brand_id
 and rtrim(d.url, '/') = any (select rtrim(s, '/') from unnest(k.sources) s)
left join unnest(k.stale_values) v on true
group by d.doc_id, d.brand_id;

comment on view kb.document_disputes is
    'Documents whose source page is cited by a conflict. Joined on url because '
    'conflicts are recorded against public URLs and one URL maps to a page plus '
    'every FAQ pair scraped from it.';


-- ---------------------------------------------------------------------------
-- search_kb, rebuilt.
--
-- Return type changes, so this is a drop rather than a replace.
-- ---------------------------------------------------------------------------
drop function if exists kb.search_kb(text, text, integer, text[], boolean);

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
    p_min_rank     real     default 0.01
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
    -- The passage's page is contradicted somewhere. Usable with attribution,
    -- but the figure in it must not be restated as settled fact.
    disputed       boolean,
    dispute_topics text[],
    -- Contains a figure known to be wrong. Never publishable.
    blocked        boolean,
    date_modified  timestamptz,
    rank           real
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
    -- chunk_id as tiebreaker so equal ranks return in a stable order; without
    -- it, paging and knowledge_snapshot reproducibility both wobble.
    order by ts_rank_cd(c.tsv, q.tsq) desc, c.chunk_id
    limit greatest(p_limit, 0);
$$;

comment on function kb.search_kb is
    'Brand-scoped full-text search over kb.chunks. Site copy is usable by '
    'default; disputed flags a passage whose page is contradicted, blocked '
    'flags one containing a known-wrong figure. Brand slug is required.';

grant execute on function
    kb.search_kb(text, text, integer, text[], boolean, real) to service_role;

commit;
