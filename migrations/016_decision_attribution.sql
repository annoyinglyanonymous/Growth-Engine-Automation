-- 016_decision_attribution.sql
--
-- Puts a name on every decision the pipeline records.
--
-- WHAT WAS MISSING
-- 013 gave campaign_strategies and campaign_assets an approved_by / approved_at
-- pair and a CHECK requiring both once status = 'approved'. It gave the other
-- four tables nothing:
--
--   campaign_angles       status goes draft -> approved | rejected, unattributed
--   creative_concepts     same
--   campaign_validations  a run record with no runner
--   asset_qa_results      a run record with no runner
--
-- So the audit trail has holes exactly where a human exercised judgement.
-- "Angle 2 was approved and angle 3 rejected" is recorded; who decided that is
-- not. An angle rejection is a real editorial decision -- it removes a line of
-- argument from the campaign -- and it currently leaves no trace of an author.
--
-- This matters most for the strictest rule in the system: the FDD blocker
-- seeded in 012 says franchise marketing is not an offer to sell a franchise
-- and no offer may be made to residents of unregistered states. If franchise
-- copy ever has to be defended, "who approved this" has to be answerable from
-- the row, years later, without relying on anyone's memory.
--
-- WHY A SENTINEL RATHER THAN A NULL OR A NAME
-- The decisions already in the table were made from the CLI before any of this
-- existed, so their author genuinely is not recorded anywhere. Three options:
--
--   * leave them NULL and make the CHECK NOT VALID -- then an unattributed row
--     is indistinguishable from a bug in the writer, and the constraint is
--     never trustworthy
--   * backfill a plausible name -- that is fiction in an audit column, which
--     is worse than an absence because it reads as evidence
--   * backfill an explicit sentinel that says what is true
--
-- Third one. 'unattributed:pre-016' is honest, greppable, and lets the CHECK be
-- fully valid so every row from here on is attributed for real.
--
-- Re-runnable.

begin;

-- --------------------------------------------------------------------------
-- 1. campaign_angles / creative_concepts -- who decided, and when
-- --------------------------------------------------------------------------
-- Both approval AND rejection are attributed. 013's strategy/asset CHECK only
-- guards 'approved' because those tables have no reject state; these two do,
-- and a rejection is just as much a decision.

alter table public.campaign_angles
    add column if not exists decided_by text,
    add column if not exists decided_at timestamptz;

alter table public.creative_concepts
    add column if not exists decided_by text,
    add column if not exists decided_at timestamptz;

update public.campaign_angles
   set decided_by = coalesce(decided_by, 'unattributed:pre-016'),
       decided_at = coalesce(decided_at, updated_at)
 where status <> 'draft'
   and (decided_by is null or decided_at is null);

update public.creative_concepts
   set decided_by = coalesce(decided_by, 'unattributed:pre-016'),
       decided_at = coalesce(decided_at, updated_at)
 where status <> 'draft'
   and (decided_by is null or decided_at is null);

do $$
begin
    if not exists (select 1 from pg_constraint
                    where conname = 'campaign_angles_decision_attributed') then
        alter table public.campaign_angles
            add constraint campaign_angles_decision_attributed
            check (status = 'draft'
                   or (decided_by is not null and decided_at is not null));
    end if;
    if not exists (select 1 from pg_constraint
                    where conname = 'creative_concepts_decision_attributed')
    then
        alter table public.creative_concepts
            add constraint creative_concepts_decision_attributed
            check (status = 'draft'
                   or (decided_by is not null and decided_at is not null));
    end if;
end $$;

-- --------------------------------------------------------------------------
-- 2. campaign_validations / asset_qa_results -- who ran it
-- --------------------------------------------------------------------------
-- NOT NULL with no default, deliberately. A default would let a caller that
-- forgets to pass an identity keep working while writing an unattributed row,
-- which is the failure this migration exists to remove. The two writers
-- (validation/brief.py, validation/asset_qa.py) pass it explicitly.

alter table public.campaign_validations
    add column if not exists validated_by text;
alter table public.asset_qa_results
    add column if not exists validated_by text;

update public.campaign_validations
   set validated_by = 'unattributed:pre-016' where validated_by is null;
update public.asset_qa_results
   set validated_by = 'unattributed:pre-016' where validated_by is null;

alter table public.campaign_validations
    alter column validated_by set not null;
alter table public.asset_qa_results
    alter column validated_by set not null;

-- --------------------------------------------------------------------------
-- 3. campaigns.created_by -- uuid to text
-- --------------------------------------------------------------------------
-- The first version of this migration went straight to the backfill and failed
-- on apply:
--
--     invalid input syntax for type uuid: "unattributed:pre-016"
--
-- created_by is a uuid. Every other attribution column in the schema is text
-- -- campaign_strategies.approved_by, campaign_assets.approved_by, the five
-- `owner` columns, and the four this migration adds. campaigns.created_by is
-- the only uuid *_by column anywhere in public.
--
-- It is an orphan. No foreign key, no comment, one row and that row is NULL.
-- The shape says it was meant for a Supabase auth.users id, and auth.users
-- exists with zero rows: the integration was provisioned and never used. Since
-- the identity this project actually has is a name from an allowlist, not a
-- Supabase user id, that column cannot hold what the app now knows.
--
-- WHY CONVERT RATHER THAN ADD A SECOND COLUMN
-- The alternative is to leave created_by for a future auth integration and add
-- created_by_name text beside it. That trades a real inconsistency today --
-- two columns for one concept, and every reader having to know which is
-- populated -- against a hypothetical integration nobody has scheduled. When
-- real auth does land it needs a deploy target, a non-superuser role and RLS
-- policies; adding created_by_user_id uuid WITH an actual foreign key at that
-- point is a smaller change than untangling two half-used columns.
--
-- Safe: nothing reads it (only campaigns.create writes it, added alongside
-- this migration), there is no FK to drop, and the single existing value is
-- NULL so the cast cannot fail.
do $$
begin
    if exists (
        select 1 from information_schema.columns
         where table_schema = 'public' and table_name = 'campaigns'
           and column_name = 'created_by' and data_type = 'uuid')
    then
        alter table public.campaigns
            alter column created_by type text using created_by::text;
    end if;
end $$;

update public.campaigns
   set created_by = 'unattributed:pre-016' where created_by is null;

comment on column public.campaigns.created_by is
    'Operator who filed the brief. Text, matching every other attribution '
    'column. Was an unused uuid shaped for auth.users; see 016.';

comment on column public.campaign_angles.decided_by is
    'Operator who approved or rejected this angle. See 016 for the '
    'unattributed:pre-016 sentinel.';
comment on column public.creative_concepts.decided_by is
    'Operator who approved or rejected this concept.';
comment on column public.campaign_validations.validated_by is
    'Operator who ran this validation. NOT NULL on purpose -- an unattributed '
    'run is the defect, not an edge case.';
comment on column public.asset_qa_results.validated_by is
    'Operator who ran this QA pass.';

commit;
