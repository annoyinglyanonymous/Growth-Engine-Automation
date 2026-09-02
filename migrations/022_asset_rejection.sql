-- ---------------------------------------------------------------------------
-- 022  Rejection, attributed, for versioned artefacts
--
-- WHAT WAS MISSING
-- campaign_assets.status and campaign_strategies.status have both allowed
-- 'rejected' since 013, and no code path has ever set it. Angles and concepts
-- have had approve/reject since they were built (generators/angles.py,
-- concepts.py, both writing decided_by/decided_at from 016). Assets and
-- strategies -- the two VERSIONED artefacts -- could only be approved.
--
-- That gap sits exactly where the revision loop lives. A reviewer clicks Edit,
-- says what is wrong, and the agent returns v4. If v4 is worse than v2, the
-- only available actions were "approve it anyway" or "ask for another edit".
-- There was no way to say "no, keep what we had" -- so the unwanted version
-- sat in 'review' forever, and lifecycle's newer_version_pending blocker (021
-- + this session) would hold the whole campaign on it with no way to clear it.
--
-- WHY NOT REUSE approved_by
-- 013 gave both tables approved_by/approved_at with a CHECK that an approved
-- row names its approver. Overloading those columns for rejection would make
-- `approved_by` mean "whoever last touched this", and every existing query
-- that reads it as "who signed this off" would quietly become wrong. Two more
-- columns is the cheaper mistake.
--
-- WHY NOT decided_by, like angles and concepts
-- Because approved_by already exists on these two tables and is already read
-- by campaigns.pipeline_state and the UI. Renaming it would be a wider change
-- than this warrants, and leaving BOTH would be the ambiguity above. So these
-- two tables stay symmetric with themselves: approved_by/at, rejected_by/at.
-- The inconsistency with angles/concepts is real and deliberately accepted;
-- it is recorded here rather than left for someone to rediscover.
--
-- NO BACKFILL
-- Measured before writing: campaign_assets holds 1 approved + 6 review,
-- campaign_strategies 1 approved + 1 superseded. Zero rejected rows exist in
-- either table, so the CHECK below is satisfiable on the current data without
-- an UPDATE. If that ever stops being true the constraint will refuse to be
-- added, which is the correct failure -- an unattributed rejection is exactly
-- what this migration exists to prevent.
--
-- REVERSIBLE
--   alter table public.campaign_assets
--       drop constraint campaign_assets_rejection_attributed,
--       drop column rejected_by, drop column rejected_at;
--   alter table public.campaign_strategies
--       drop constraint campaign_strategies_rejection_attributed,
--       drop column rejected_by, drop column rejected_at;
-- ---------------------------------------------------------------------------

alter table public.campaign_assets
    add column if not exists rejected_by text,
    add column if not exists rejected_at timestamptz;

alter table public.campaign_strategies
    add column if not exists rejected_by text,
    add column if not exists rejected_at timestamptz;

-- The same rule 013 applied to approval and 016 applied to every other
-- decision in the pipeline: a decision with no decider is not a decision.
-- A rejection is the decision most likely to be questioned later -- somebody
-- asked for a change and did not get it -- so it is the last one that should
-- be anonymous.
do $$
begin
    if not exists (select 1 from pg_constraint
                    where conname = 'campaign_assets_rejection_attributed')
    then
        alter table public.campaign_assets
            add constraint campaign_assets_rejection_attributed
            check (status <> 'rejected'
                   or (rejected_by is not null and rejected_at is not null));
    end if;

    if not exists (select 1 from pg_constraint
                    where conname
                          = 'campaign_strategies_rejection_attributed')
    then
        alter table public.campaign_strategies
            add constraint campaign_strategies_rejection_attributed
            check (status <> 'rejected'
                   or (rejected_by is not null and rejected_at is not null));
    end if;
end $$;

-- Rejections are read in one pattern only: "what did we turn down in this
-- slot, and why". Partial, because rejected rows are the rare case and an
-- index over every asset row to find them would be mostly dead weight.
create index if not exists campaign_assets_rejected_idx
    on public.campaign_assets (campaign_id, channel, variant)
    where status = 'rejected';

comment on column public.campaign_assets.rejected_by is
    'Operator who rejected this version. Required when status = ''rejected'' '
    '(022). Distinct from approved_by, which means "who signed this off".';

comment on column public.campaign_assets.rejected_at is
    'When this version was rejected (022).';

comment on column public.campaign_strategies.rejected_by is
    'Operator who rejected this version. Required when status = ''rejected'' '
    '(022).';

comment on column public.campaign_strategies.rejected_at is
    'When this version was rejected (022).';
