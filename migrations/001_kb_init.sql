-- 001_kb_init.sql
--
-- The kb schema: scraped/curated knowledge the AI agent grounds on.
--
-- Deliberately separate from public. The public tables (claims, brand_rules)
-- are AUTHORITY: curated, human-approved, governed by owner/last_reviewed_at/
-- effective_from. This schema is EVIDENCE: site copy, unverified, internally
-- contradictory. Keeping them in different schemas makes an accidental join
-- between scraped text and a compliance decision visible in review.
--
-- Sized for what the pipeline currently produces:
--   833 documents   231 page, 588 faq, 12 entity, 2 primer
--   1,119 chunks    largest 7,997 chars
--   59 entity rows  11 locations, 48 state licences
--
-- Applies cleanly more than once is NOT a goal here -- run it on a fresh kb
-- schema. Re-runs are handled by later numbered migrations.

begin;

create schema if not exists kb;

-- On Supabase these live in the extensions schema, which is on the default
-- search_path. Index operators below are schema-qualified anyway, so this
-- works even if search_path differs.
create extension if not exists pg_trgm with schema extensions;


-- ---------------------------------------------------------------------------
-- ingest_runs: one row per pipeline run.
-- Exists because step 9 (campaign_validations.knowledge_snapshot) has to be
-- able to answer "what did the agent see when this campaign passed".
-- ---------------------------------------------------------------------------
create table kb.ingest_runs (
    run_id      bigserial primary key,
    started_at  timestamptz not null default now(),
    finished_at timestamptz,
    status      text        not null default 'running'
                            check (status in ('running', 'ok', 'failed')),
    counts      jsonb       not null default '{}'::jsonb,
    errors      jsonb       not null default '[]'::jsonb,
    notes       text
);


-- ---------------------------------------------------------------------------
-- documents
-- ---------------------------------------------------------------------------
create table kb.documents (
    doc_id           bigserial primary key,
    brand_id         uuid not null references public.brands (id) on delete cascade,

    -- page   scraped markdown page
    -- faq    one Q&A pair; carries its own source url, which is why FAQs are
    --        documents rather than chunks of one faqs.md document
    -- entity rendered from locations.json / state-licenses.json
    -- primer hand-written, always injected, never retrieved by search alone
    kind             text not null
                     check (kind in ('page', 'faq', 'entity', 'primer')),

    -- posix, relative to the KB root. FAQ and entity paths carry a #fragment
    -- (faqs.md#what-is-renegade-insurance) so each is distinct.
    path             text not null,
    slug             text not null,
    title            text not null,
    url              text,
    category         text,
    meta_description text,

    -- from JSON-LD enrichment; present for all 230 scraped pages
    date_modified    timestamptz,
    date_published   timestamptz,

    -- under 500 chars of body: nav/CTA shells and empty pages. Flagged, not
    -- dropped, so retrieval can exclude by default without losing inventory.
    thin             boolean not null default false,
    char_len         integer not null default 0,

    -- sha256 of the CLEANED body, so improving normalize.py invalidates every
    -- document and re-ingest picks the fix up
    content_hash     text not null,

    meta             jsonb not null default '{}'::jsonb,
    ingest_run_id    bigint references kb.ingest_runs (run_id),
    ingested_at      timestamptz not null default now(),

    constraint documents_brand_path_key unique (brand_id, path),

    -- Trivially true given the primary key, but a composite FK needs a unique
    -- constraint to point at. See kb.chunks.chunks_parent_fkey.
    constraint documents_id_brand_key unique (doc_id, brand_id)
);

create index documents_brand_kind_idx
    on kb.documents (brand_id, kind, category)
    where not thin;

create index documents_title_trgm_idx
    on kb.documents using gin (title extensions.gin_trgm_ops);

create index documents_date_modified_idx
    on kb.documents (brand_id, date_modified desc nulls last);


-- ---------------------------------------------------------------------------
-- chunks
-- ---------------------------------------------------------------------------
create table kb.chunks (
    chunk_id     bigserial primary key,
    doc_id       bigint not null,

    -- Denormalised from documents on purpose. Every retrieval is brand-scoped,
    -- and a safety property should not depend on remembering a join. The
    -- composite FK below makes the denormalisation safe rather than risky:
    -- a chunk physically cannot disagree with its parent about its brand.
    brand_id     uuid not null,

    ordinal      integer not null,
    heading_path text not null default '',

    -- Denormalised too: a generated column cannot reach across a join, and
    -- title weighting is where most ranking quality comes from. The corpus is
    -- a frozen scrape, so the title cannot drift independently.
    doc_title    text not null,

    text         text not null,
    char_len     integer not null,

    -- text contains money, a percentage, "200+", or a "4.7/5" rating. Such
    -- chunks are returned to the agent LABELLED unverified, so it does not
    -- restate site marketing figures as fact in new campaign copy.
    claim_like   boolean not null default false,

    meta         jsonb not null default '{}'::jsonb,

    tsv tsvector generated always as (
             setweight(to_tsvector('english', coalesce(doc_title, '')),    'A')
          || setweight(to_tsvector('english', coalesce(heading_path, '')), 'B')
          || setweight(to_tsvector('english', text),                       'C')
         ) stored,

    constraint chunks_doc_ordinal_key unique (doc_id, ordinal),

    constraint chunks_parent_fkey
        foreign key (doc_id, brand_id)
        references kb.documents (doc_id, brand_id) on delete cascade,

    -- An empty chunk is a row with an empty tsvector: never matches, always
    -- costs. Two pages normalise to zero chars and correctly produce no chunk.
    constraint chunks_text_not_blank check (length(btrim(text)) > 0)
);

create index chunks_tsv_idx    on kb.chunks using gin (tsv);
create index chunks_brand_idx  on kb.chunks (brand_id);
create index chunks_meta_idx   on kb.chunks using gin (meta jsonb_path_ops);

-- Fuzzy match on chunk bodies, not just titles: "has this claim already been
-- made somewhere on the site" is trigram similarity against the text.
create index chunks_text_trgm_idx
    on kb.chunks using gin (text extensions.gin_trgm_ops);


-- ---------------------------------------------------------------------------
-- entities: structured rows answerable by SQL rather than by ranking.
-- "Are you licensed in Ohio" should be an exact lookup, not a search result.
-- ---------------------------------------------------------------------------
create table kb.entities (
    entity_id   bigserial primary key,
    brand_id    uuid not null references public.brands (id) on delete cascade,
    entity_type text not null check (entity_type in ('location', 'state_license')),
    key         text not null,          -- location slug, or state name
    data        jsonb not null,
    ingest_run_id bigint references kb.ingest_runs (run_id),
    ingested_at timestamptz not null default now(),

    constraint entities_natural_key unique (brand_id, entity_type, key)
);

create index entities_data_idx on kb.entities using gin (data jsonb_path_ops);


-- ---------------------------------------------------------------------------
-- conflicts: contradictions the live sites actually contain.
-- Retrieval joins against this so an answer FLAGS the disagreement instead of
-- silently returning whichever chunk ranked higher. severity uses the same
-- vocabulary as public.brand_rules.
-- ---------------------------------------------------------------------------
create table kb.conflicts (
    conflict_id bigserial primary key,
    brand_id    uuid not null references public.brands (id) on delete cascade,
    topic       text not null,
    detail      text not null,
    sources     text[] not null default '{}',
    severity    text not null default 'warning'
                check (severity in ('info', 'warning', 'blocker')),
    created_at  timestamptz not null default now(),

    constraint conflicts_brand_topic_key unique (brand_id, topic)
);


-- ---------------------------------------------------------------------------
-- Default deny.
--
-- This schema is not in Supabase's Exposed Schemas today, so PostgREST cannot
-- reach it. If it is ever added -- which options B and C of the retrieval API
-- would require -- anon-key holders could otherwise read the whole corpus.
-- RLS with zero policies means nobody reads it. The loader and API connect as
-- postgres, which bypasses RLS, so this costs nothing now.
-- ---------------------------------------------------------------------------
alter table kb.documents   enable row level security;
alter table kb.chunks      enable row level security;
alter table kb.entities    enable row level security;
alter table kb.conflicts   enable row level security;
alter table kb.ingest_runs enable row level security;

commit;
