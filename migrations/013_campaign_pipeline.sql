-- 013_campaign_pipeline.sql
--
-- Stages 4 to 8 of the Campaign Brief to Asset Pack flow. Stages 1 to 3
-- (brief, validation) already had tables; strategy, angles, concepts, assets
-- and asset QA had none.
--
-- THE ORDERING IS IN THE SCHEMA, NOT IN CONVENTION
--   campaign -> strategy -> angle -> concept -> asset
--
-- campaign_assets.concept_id is NOT NULL, so copy cannot exist without a
-- concept behind it. That is the PDF's central process claim -- concepts come
-- before copy "so we don't produce five slightly different versions of the
-- same ad" -- expressed as a constraint rather than as a docstring. A
-- generator that skips straight to writing ads gets a NOT NULL violation.
--
-- campaign_id IS DENORMALISED DOWN THE CHAIN, SAFELY
-- Every level carries campaign_id, reachable by walking parents. 001_kb_init
-- made the same trade for kb.chunks.brand_id and the reasoning holds here: a
-- misparented asset attaches finished copy to the wrong campaign, and that is
-- not something to leave to remembering a three-level join. Each level also
-- carries a unique (own_id, campaign_id) so the child's composite foreign key
-- makes the denormalisation enforced rather than merely hoped for. An asset
-- physically cannot disagree with its concept about which campaign it belongs
-- to.
--
-- VERSIONING
-- Strategies and assets are versioned because the PDF's own example shows
-- "Strategy V2 - Approved". A revision is a new row, not an UPDATE, so the
-- knowledge_snapshot that produced each version survives. Angles and concepts
-- are not versioned: they are cheap to regenerate and nothing downstream cites
-- an angle by version.
--
-- Partial unique indexes enforce "at most one approved version at a time",
-- which is the question every reader of this data actually asks.
--
-- WHY THERE IS A TRIGGER HERE AND NOWHERE ELSE
-- No migration in this project creates a trigger, and every existing
-- public.* table has an updated_at that defaults to now() and is then never
-- touched -- so those columns are duplicates of created_at wearing a
-- misleading name. That is tolerable for tables whose rows do not change.
-- It is not tolerable here: status transitions in place (draft -> review ->
-- approved) are the whole point of these tables, and "when did this get
-- approved" is a question the UI will ask. The existing public tables should
-- get the same trigger; that is a separate change and is not made here.

begin;

-- ---------------------------------------------------------------------------
-- Shared updated_at maintenance.
--
-- In public rather than a new schema because that is where the tables it
-- serves live. Named with a set_ prefix to make its side effect obvious at
-- the call site in each create trigger below.
-- ---------------------------------------------------------------------------
create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at := now();
    return new;
end;
$$;


-- ---------------------------------------------------------------------------
-- campaign_strategies  (stage 4)
--
-- One approved strategy is the input to angle generation. The array columns
-- mirror public.campaigns' own use of text[] for the same kinds of list, so
-- the brief and the strategy stay comparable without casting.
--
-- objections is jsonb rather than text[] because an objection is useless
-- without its response, and two parallel arrays that must stay index-aligned
-- is a data model that breaks the first time someone edits one of them.
-- ---------------------------------------------------------------------------
create table public.campaign_strategies (
    id             uuid primary key default gen_random_uuid(),
    campaign_id    uuid not null references public.campaigns (id)
                        on delete cascade,
    version_number integer not null,

    status         text not null default 'draft'
                        check (status in ('draft', 'review', 'approved',
                                          'rejected', 'superseded')),

    core_message   text not null,
    positioning    text,
    pain_points    text[] not null default '{}',
    benefits       text[] not null default '{}',
    proof_points   text[] not null default '{}',

    --  [{"objection": "...", "response": "..."}, ...]
    objections     jsonb not null default '[]'::jsonb
                        check (jsonb_typeof(objections) = 'array'),

    hypothesis     text,

    -- What the generator saw. Stage 10 (version history) falls out of this
    -- for free: every version records the corpus state that produced it.
    knowledge_snapshot jsonb,
    model_name     text,
    generator_version text,

    approved_by    text,
    approved_at    timestamptz,
    notes          text,

    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),

    constraint campaign_strategies_version_key
        unique (campaign_id, version_number),

    -- Lets campaign_angles carry campaign_id with a composite FK.
    constraint campaign_strategies_id_campaign_key
        unique (id, campaign_id),

    constraint campaign_strategies_version_positive
        check (version_number > 0),

    -- An approval with no approver is not an approval.
    constraint campaign_strategies_approval_complete
        check (status <> 'approved'
               or (approved_by is not null and approved_at is not null))
);

create index campaign_strategies_campaign_idx
    on public.campaign_strategies (campaign_id, version_number desc);

-- At most one approved strategy per campaign. Approving V2 requires moving V1
-- to 'superseded' in the same transaction, which is the intended discipline.
create unique index campaign_strategies_one_approved_idx
    on public.campaign_strategies (campaign_id)
    where status = 'approved';

create trigger campaign_strategies_set_updated_at
    before update on public.campaign_strategies
    for each row execute function public.set_updated_at();


-- ---------------------------------------------------------------------------
-- campaign_angles  (stage 5)
--
-- The distinct arguments a campaign could make: cost, speed, credibility,
-- support. name is free text rather than a CHECK enum because the useful set
-- is campaign-specific and a fixed list would be wrong by the third campaign.
-- ---------------------------------------------------------------------------
create table public.campaign_angles (
    id          uuid primary key default gen_random_uuid(),
    campaign_id uuid not null,
    strategy_id uuid not null,

    name        text not null,
    hypothesis  text not null,
    rationale   text,
    position    integer not null default 0,

    status      text not null default 'draft'
                     check (status in ('draft', 'approved', 'rejected')),

    knowledge_snapshot jsonb,
    model_name  text,

    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),

    -- The composite parent FK: an angle cannot claim a campaign its strategy
    -- does not belong to.
    constraint campaign_angles_strategy_fkey
        foreign key (strategy_id, campaign_id)
        references public.campaign_strategies (id, campaign_id)
        on delete cascade,

    constraint campaign_angles_id_campaign_key unique (id, campaign_id),

    -- Two angles making the same argument is the duplication this stage
    -- exists to prevent.
    constraint campaign_angles_name_key unique (strategy_id, name)
);

create index campaign_angles_strategy_idx
    on public.campaign_angles (strategy_id, position);

create trigger campaign_angles_set_updated_at
    before update on public.campaign_angles
    for each row execute function public.set_updated_at();


-- ---------------------------------------------------------------------------
-- creative_concepts  (stage 6)
--
-- idea, hook and visual_direction are separate columns rather than one blob
-- because the review UI shows them separately and QA checks the hook alone.
-- ---------------------------------------------------------------------------
create table public.creative_concepts (
    id          uuid primary key default gen_random_uuid(),
    campaign_id uuid not null,
    angle_id    uuid not null,

    idea             text not null,
    hook             text not null,
    visual_direction text,
    position         integer not null default 0,

    status      text not null default 'draft'
                     check (status in ('draft', 'approved', 'rejected')),

    knowledge_snapshot jsonb,
    model_name  text,

    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),

    constraint creative_concepts_angle_fkey
        foreign key (angle_id, campaign_id)
        references public.campaign_angles (id, campaign_id)
        on delete cascade,

    constraint creative_concepts_id_campaign_key unique (id, campaign_id),

    constraint creative_concepts_hook_not_blank
        check (length(btrim(hook)) > 0)
);

create index creative_concepts_angle_idx
    on public.creative_concepts (angle_id, position);

create trigger creative_concepts_set_updated_at
    before update on public.creative_concepts
    for each row execute function public.set_updated_at();


-- ---------------------------------------------------------------------------
-- campaign_assets  (stages 7 and 8)
--
-- CONTENT SHAPE
-- content is jsonb with per-channel keys rather than a single body column,
-- because character limits are per field and cannot be checked against a
-- concatenation. A Meta ad stores primary_text / headline / description; an
-- email stores subject / preheader / body. The deterministic character-limit
-- check reads those keys by name.
--
-- An email SEQUENCE is N rows sharing a variant and differing by position,
-- not one row holding an array. Otherwise QA cannot fail email 2 of 3, and
-- the UI cannot approve them individually.
--
-- THE knowledge_snapshot CHECK
-- The failure this whole project is organised against is ungrounded copy that
-- looks fine. So an asset row physically cannot be stored without a snapshot
-- carrying a kb_chunk_ids key. It deliberately does NOT require that key to be
-- non-empty: copy written purely from approved claims and brand rules, with no
-- retrieved passages, is legitimate and grounded. Distinguishing "grounded in
-- governance only" from "grounded in nothing" is a judgement for the QA layer,
-- which can see the claims list too. What the constraint guarantees is that
-- the snapshot was actually taken.
-- ---------------------------------------------------------------------------
create table public.campaign_assets (
    id             uuid primary key default gen_random_uuid(),
    campaign_id    uuid not null,

    -- The ordering guarantee. Not nullable, on purpose.
    concept_id     uuid not null,

    channel        text not null
                        check (channel in ('email', 'meta_ads', 'google_ads',
                                            'landing_page', 'sms')),
    asset_type     text not null
                        check (asset_type in ('meta_ad', 'email', 'google_ad',
                                               'landing_page_section', 'sms')),

    -- A/B label. Two variants of the same asset share everything but this.
    variant        text not null default 'A',

    -- Sequence position, for an email sequence. NULL for a standalone asset.
    position       integer,

    content        jsonb not null
                        check (jsonb_typeof(content) = 'object'
                               and content <> '{}'::jsonb),

    version_number integer not null default 1,

    status         text not null default 'draft'
                        check (status in ('draft', 'review', 'approved',
                                          'rejected', 'superseded')),

    knowledge_snapshot jsonb not null
                        check (jsonb_typeof(knowledge_snapshot) = 'object'
                               and knowledge_snapshot ? 'kb_chunk_ids'),

    model_name     text,
    generator_version text,

    approved_by    text,
    approved_at    timestamptz,
    notes          text,

    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),

    constraint campaign_assets_concept_fkey
        foreign key (concept_id, campaign_id)
        references public.creative_concepts (id, campaign_id)
        -- restrict, not cascade: deleting a concept must not silently destroy
        -- approved copy written from it.
        --
        -- KNOWN CONSEQUENCE, chosen rather than overlooked: every level above
        -- cascades, so this restrict makes `delete from campaigns` FAIL once
        -- any asset exists. That is the intended behaviour -- losing a
        -- finished asset pack to a stray delete is worse than an error -- and
        -- campaigns.status already carries 'archived' as the way to retire a
        -- campaign. To genuinely delete one, delete its assets first,
        -- explicitly.
        on delete restrict,

    constraint campaign_assets_version_positive check (version_number > 0),

    constraint campaign_assets_position_sane
        check (position is null or position > 0),

    -- An email sequence step without a position cannot be ordered, and a
    -- single ad with one is meaningless.
    constraint campaign_assets_position_required_for_email
        check ((asset_type = 'email') = (position is not null)),

    constraint campaign_assets_approval_complete
        check (status <> 'approved'
               or (approved_by is not null and approved_at is not null))
);

-- The natural key. coalesce is why this is an index and not a table
-- constraint: NULL positions would otherwise never collide with each other.
create unique index campaign_assets_natural_key_idx
    on public.campaign_assets
       (campaign_id, channel, asset_type, variant,
        coalesce(position, 0), version_number);

create index campaign_assets_campaign_idx
    on public.campaign_assets (campaign_id, channel, status);

create index campaign_assets_concept_idx
    on public.campaign_assets (concept_id);

-- One live version per slot, same discipline as strategies.
create unique index campaign_assets_one_approved_idx
    on public.campaign_assets
       (campaign_id, channel, asset_type, variant, coalesce(position, 0))
    where status = 'approved';

create trigger campaign_assets_set_updated_at
    before update on public.campaign_assets
    for each row execute function public.set_updated_at();


-- ---------------------------------------------------------------------------
-- asset_qa_results  (stage 8)
--
-- Column-for-column the shape of public.campaign_validations, deliberately.
-- The two tiers answer different questions and must not be merged: the
-- deterministic tier is a fact ("this asset contains $20,000"), the AI tier is
-- a judgement ("this reads as two campaigns"). One combined status would make
-- a model's opinion indistinguishable from a constraint violation.
-- ---------------------------------------------------------------------------
create table public.asset_qa_results (
    id        uuid primary key default gen_random_uuid(),
    asset_id  uuid not null references public.campaign_assets (id)
                   on delete cascade,
    qa_number integer not null,

    status               text not null
                              check (status in ('pass', 'warning', 'blocked',
                                                 'needs_info')),
    deterministic_status text check (deterministic_status in
                                     ('pass', 'warning', 'blocked')),
    ai_status            text check (ai_status in ('pass', 'warning',
                                                   'blocked', 'needs_info')),

    blockers        jsonb not null default '[]'::jsonb
                         check (jsonb_typeof(blockers) = 'array'),
    warnings        jsonb not null default '[]'::jsonb
                         check (jsonb_typeof(warnings) = 'array'),
    recommendations jsonb not null default '[]'::jsonb
                         check (jsonb_typeof(recommendations) = 'array'),

    deterministic_results jsonb not null default '{}'::jsonb,
    ai_results            jsonb not null default '{}'::jsonb,

    knowledge_snapshot jsonb,
    validator_version  text,
    model_name         text,

    validated_at timestamptz not null default now(),
    created_at   timestamptz not null default now(),

    constraint asset_qa_results_number_key unique (asset_id, qa_number),
    constraint asset_qa_results_number_positive check (qa_number > 0),

    -- A blocked result with no blocker listed is unactionable. The typeof
    -- guard is not redundant with the column CHECK above: Postgres does not
    -- order CHECK evaluation, and jsonb_array_length on a non-array RAISES
    -- rather than returning false, which would surface as an internal error
    -- instead of a constraint violation.
    constraint asset_qa_results_blocked_has_reason
        check (status <> 'blocked'
               or (jsonb_typeof(blockers) = 'array'
                   and jsonb_array_length(blockers) > 0))
);

create index asset_qa_results_asset_idx
    on public.asset_qa_results (asset_id, qa_number desc);


-- ---------------------------------------------------------------------------
-- Default deny, matching 001_kb_init's reasoning.
--
-- RLS on with zero policies means PostgREST reaches nothing even if these
-- tables are exposed. The API and generators connect as postgres, which
-- bypasses RLS, so this costs nothing today and prevents an anon key from
-- reading unapproved campaign copy if public is ever exposed.
--
-- This matters more here than in kb: kb holds published site text, while these
-- tables hold unreleased campaign strategy.
-- ---------------------------------------------------------------------------
alter table public.campaign_strategies enable row level security;
alter table public.campaign_angles     enable row level security;
alter table public.creative_concepts   enable row level security;
alter table public.campaign_assets     enable row level security;
alter table public.asset_qa_results    enable row level security;

commit;
