-- 020_revision_requests.sql
--
-- The review loop: a person says what they did not like, and the agent revises.
--
-- Until now a reviewer had two options at every stage -- approve or reject.
-- Rejection throws the work away and says nothing about why, so the only way
-- to get different output was to regenerate from the same prompt and hope. The
-- reviewer's judgement, which is the most valuable input in the whole system,
-- had nowhere to go.
--
-- This table is where it goes. One row per "here is what is wrong with this",
-- attributed and timestamped, alongside a snapshot of exactly what the
-- reviewer was looking at when they said it.
--
-- WHY previous_content IS STORED HERE AND NOT INFERRED
-- Two of the four targets cannot be versioned:
--
--   campaign_strategies  has version_number      -> revision writes a new row
--   campaign_assets      has version_number      -> revision writes a new row
--   campaign_angles      has NO version column, and campaign_angles_name_key
--                        is unique on (strategy_id, name), so a revised angle
--                        keeping its name cannot be inserted beside the old one
--   creative_concepts    has NO version column
--
-- So angles and concepts are revised IN PLACE, and without a snapshot the
-- critiqued text would be gone -- the feedback would read as a complaint about
-- something that no longer exists. previous_content makes every request
-- self-contained: who, when, what they saw, what they said. It also means the
-- versioned and unversioned targets behave the same way from the audit side,
-- rather than one being traceable and the other not.
--
-- WHY EXACTLY-ONE-TARGET COLUMNS AND NOT (target_table, target_id)
-- A polymorphic pair is shorter and unenforceable: nothing stops a row naming
-- 'campaign_assets' with a strategy's uuid. Four nullable foreign keys plus a
-- CHECK that exactly one is set is enforceable, and this schema has
-- consistently paid that cost -- 013's composite FKs exist for the same reason.
--
-- FEEDBACK IS UNTRUSTED INPUT AND THE PROMPT IS NOT THE GUARDRAIL
-- This text is written by a person and handed to a model. "Ignore the
-- disclaimer" and "just say we are in all 50 states" are things a reviewer can
-- type, in good faith or otherwise. The revision prompt says the feedback
-- cannot relax governance, but a prompt is a request, not a guarantee.
--
-- The guarantee is that a revised asset is a NEW row with NO QA result, so it
-- must pass the deterministic checks again before it can be approved. Those
-- checks do not read this column and cannot be talked out of anything. A
-- reviewer can ask for non-compliant copy; they cannot approve it.

begin;

-- Assets are the one target without an (id, campaign_id) unique key, so they
-- are the one that cannot take the composite FK the other three use. Adding it
-- here rather than working around it, so all four targets are guarded the same
-- way.
do $$
begin
    if not exists (select 1 from pg_constraint
                    where conname = 'campaign_assets_id_campaign_key') then
        alter table public.campaign_assets
            add constraint campaign_assets_id_campaign_key
            unique (id, campaign_id);
    end if;
end $$;


create table if not exists public.revision_requests (
    id            uuid primary key default gen_random_uuid(),

    -- Denormalised down the chain exactly as 013 does it, and enforced the
    -- same way: the composite FKs below make a misparented request impossible
    -- rather than merely unlikely.
    campaign_id   uuid not null
                       references public.campaigns (id) on delete cascade,

    -- Exactly one of these. See the header.
    strategy_id   uuid,
    angle_id      uuid,
    concept_id    uuid,
    asset_id      uuid,

    --: What the reviewer wants changed, in their words.
    feedback      text not null,

    --: The target's content at the moment it was critiqued.
    previous_content jsonb not null
                       check (jsonb_typeof(previous_content) = 'object'),

    requested_by  text not null,
    requested_at  timestamptz not null default now(),

    status        text not null default 'open'
                       check (status in ('open', 'addressed', 'withdrawn')),
    addressed_at  timestamptz,
    addressed_by  text,

    --: What the agent says about the revision, and specifically about
    --: anything in the feedback it could not do.
    --:
    --: This exists because of the one failure mode a revision loop has that a
    --: generation does not: the reviewer asked for something governance
    --: forbids. "Say we are licensed in all 50 states" has a compliant
    --: neighbour ("48 states and the District of Columbia") and no compliant
    --: form of what was literally asked. Without somewhere to say so, the
    --: agent either silently ignores the reviewer -- who then thinks the
    --: system is broken and asks again -- or silently complies, which is the
    --: failure this whole project exists to prevent.
    agent_note    text,

    --: For versioned targets, the version this request produced. Null for
    --: angles and concepts, which are revised in place.
    resulting_version integer,

    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),

    constraint revision_requests_one_target check (
        (strategy_id is not null)::int
      + (angle_id    is not null)::int
      + (concept_id  is not null)::int
      + (asset_id    is not null)::int = 1
    ),

    -- Empty or one-word feedback is worse than no feedback: it triggers a
    -- regeneration that cannot be steered, and it looks like a decision was
    -- recorded. 12 characters is not a quality bar, it is a typo bar.
    constraint revision_requests_feedback_substantive check (
        length(btrim(feedback)) >= 12
    ),

    -- Same shape as 013's approver CHECK: a state that implies an actor must
    -- carry one.
    constraint revision_requests_addressed_attributed check (
        status <> 'addressed'
        or (addressed_at is not null and addressed_by is not null)
    ),

    constraint revision_requests_strategy_fkey
        foreign key (strategy_id, campaign_id)
        references public.campaign_strategies (id, campaign_id)
        on delete cascade,
    constraint revision_requests_angle_fkey
        foreign key (angle_id, campaign_id)
        references public.campaign_angles (id, campaign_id)
        on delete cascade,
    constraint revision_requests_concept_fkey
        foreign key (concept_id, campaign_id)
        references public.creative_concepts (id, campaign_id)
        on delete cascade,
    constraint revision_requests_asset_fkey
        foreign key (asset_id, campaign_id)
        references public.campaign_assets (id, campaign_id)
        on delete cascade
);

-- The detail page's main read: open requests for this campaign.
create index if not exists revision_requests_campaign_idx
    on public.revision_requests (campaign_id, status, requested_at desc);

-- Per-target lookups, so an item can show its own history. Partial, because
-- three of the four columns are null on any given row.
create index if not exists revision_requests_strategy_idx
    on public.revision_requests (strategy_id) where strategy_id is not null;
create index if not exists revision_requests_angle_idx
    on public.revision_requests (angle_id)    where angle_id is not null;
create index if not exists revision_requests_concept_idx
    on public.revision_requests (concept_id)  where concept_id is not null;
create index if not exists revision_requests_asset_idx
    on public.revision_requests (asset_id)    where asset_id is not null;

-- At most one open request per target. A second one is not extra signal: two
-- open critiques of the same draft cannot both be "the thing to fix next", and
-- the reviewer who wrote the first would never see that it had been
-- superseded. Withdraw or address the standing one first.
create unique index if not exists revision_requests_one_open_strategy_idx
    on public.revision_requests (strategy_id)
    where status = 'open' and strategy_id is not null;
create unique index if not exists revision_requests_one_open_angle_idx
    on public.revision_requests (angle_id)
    where status = 'open' and angle_id is not null;
create unique index if not exists revision_requests_one_open_concept_idx
    on public.revision_requests (concept_id)
    where status = 'open' and concept_id is not null;
create unique index if not exists revision_requests_one_open_asset_idx
    on public.revision_requests (asset_id)
    where status = 'open' and asset_id is not null;

drop trigger if exists revision_requests_set_updated_at
    on public.revision_requests;
create trigger revision_requests_set_updated_at
    before update on public.revision_requests
    for each row execute function public.set_updated_at();

-- Default deny, matching every other table in this schema: RLS on, zero
-- policies, so only BYPASSRLS roles read it.
alter table public.revision_requests enable row level security;

comment on table public.revision_requests is
    'Reviewer feedback that drives a regeneration. previous_content snapshots '
    'what was critiqued, because angles and concepts are revised in place.';
comment on column public.revision_requests.feedback is
    'Untrusted human text, passed to a model. The deterministic checks -- not '
    'the prompt -- are what stop it authorising non-compliant copy.';

commit;
