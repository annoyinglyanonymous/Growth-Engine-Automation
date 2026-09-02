-- 021_campaign_lifecycle.sql
--
-- Makes the other eight campaign statuses reachable, and records who moved
-- them.
--
-- WHAT WAS DEAD
-- campaigns.status has carried the whole flow since 001:
--
--   draft -> validating -> blocked | needs_info | validated
--         -> strategy -> production -> review -> approved -> live
--         -> completed -> archived
--
-- and exactly one line of code ever moved it: validation/brief.py, which sets
-- blocked, needs_info or validated. So four of twelve values were reachable
-- and eight were decoration. A campaign could have seven approved assets and
-- still read 'validated' -- the status it got before a single word was
-- written.
--
-- The revision loop in 020 sharpened this rather than causing it. Once a
-- reviewer can iterate an asset pack until it is right, the absence of any way
-- to say "this campaign is done" is the obvious next gap.
--
-- WHY A STATUS EVENTS TABLE AND NOT JUST A COLUMN
-- campaigns.status is a single value: it says where the campaign is, never how
-- it got there. For most of this flow that is fine. It is not fine for one
-- transition -- review -> approved -- because that is the moment a person
-- takes responsibility for everything the campaign will publish.
--
-- The FDD blocker seeded in 012 is why. Franchise marketing is not an offer to
-- sell a franchise, and no offer may be made to residents of unregistered
-- states. If franchise copy ever has to be defended, "who approved this
-- campaign, when, and what did they say about it" has to be answerable from a
-- row years later. A status column that has since moved to 'completed' cannot
-- answer it.
--
-- So: every transition gets an event, automatic ones included. The automatic
-- flag separates "the system observed that a strategy was approved" from "a
-- person decided this campaign is ready", which are different kinds of claim
-- and should not look alike in an audit.
--
-- VALIDATING IS DELIBERATELY LEFT UNREACHABLE
-- It only means anything if validation runs out of band. Today it runs inside
-- the request, so a status of 'validating' would exist for the 200ms nobody
-- can observe it. Setting it would be theatre. It becomes real when there is a
-- job table, which is Phase 2.

begin;

-- --------------------------------------------------------------------------
-- 1. The campaign carries its own approval, like strategies and assets do
-- --------------------------------------------------------------------------

alter table public.campaigns
    add column if not exists approved_by text,
    add column if not exists approved_at timestamptz;

-- Same shape as 013's approver CHECK. 'live' and 'completed' are included
-- because approval persists through them -- a campaign does not stop having
-- been approved when it launches. 'archived' is excluded: a draft can be
-- archived without ever having been approved, and requiring an approver there
-- would make abandoning a bad brief impossible.
do $$
begin
    if not exists (select 1 from pg_constraint
                    where conname = 'campaigns_approval_attributed') then
        alter table public.campaigns
            add constraint campaigns_approval_attributed
            check (status not in ('approved', 'live', 'completed')
                   or (approved_by is not null and approved_at is not null));
    end if;
end $$;


-- --------------------------------------------------------------------------
-- 2. Every transition, with an author
-- --------------------------------------------------------------------------

create table if not exists public.campaign_status_events (
    id           uuid primary key default gen_random_uuid(),
    campaign_id  uuid not null
                      references public.campaigns (id) on delete cascade,

    --: Null only for a backfilled origin event, where the previous status is
    --: genuinely unknown rather than absent.
    from_status  text,
    to_status    text not null,

    changed_by   text not null,
    changed_at   timestamptz not null default now(),

    --: True when the system inferred the move from pipeline state, false when
    --: a person pressed a button. Both are real transitions; only one is a
    --: decision, and an audit that cannot tell them apart is misleading in the
    --: direction that matters -- it would let an automatic advance look like a
    --: sign-off.
    automatic    boolean not null default false,

    --: Free text from the person making the call. The one place a campaign
    --: approval can carry a reason.
    note         text,

    created_at   timestamptz not null default now(),

    -- The vocabulary is duplicated from campaigns.status rather than shared,
    -- because a status this table has recorded must stay recordable even if
    -- the campaigns CHECK is later narrowed. History does not become invalid
    -- when a workflow changes.
    constraint campaign_status_events_to_status_check check (
        to_status in ('draft', 'validating', 'blocked', 'needs_info',
                      'validated', 'strategy', 'production', 'review',
                      'approved', 'live', 'completed', 'archived')),
    constraint campaign_status_events_from_status_check check (
        from_status is null or from_status in (
            'draft', 'validating', 'blocked', 'needs_info', 'validated',
            'strategy', 'production', 'review', 'approved', 'live',
            'completed', 'archived')),

    -- A transition to the status it already had is not an event.
    constraint campaign_status_events_actually_moved check (
        from_status is null or from_status <> to_status)
);

create index if not exists campaign_status_events_campaign_idx
    on public.campaign_status_events (campaign_id, changed_at desc);

-- The approval events specifically, for the audit question this table exists
-- to answer. Partial because they are a small fraction of the rows.
create index if not exists campaign_status_events_approvals_idx
    on public.campaign_status_events (campaign_id, changed_at desc)
    where to_status = 'approved' and not automatic;

alter table public.campaign_status_events enable row level security;


-- --------------------------------------------------------------------------
-- 3. Origin events for campaigns that predate this table
-- --------------------------------------------------------------------------
-- from_status null and the same unattributed sentinel 016 uses. The row says
-- "this campaign was at this status before anyone was recording transitions",
-- which is true, rather than inventing a path it took to get there.

insert into public.campaign_status_events
    (campaign_id, from_status, to_status, changed_by, changed_at, automatic,
     note)
select c.id, null, c.status, 'unattributed:pre-021', c.updated_at, true,
       'origin event -- status predates campaign_status_events'
from public.campaigns c
where not exists (
    select 1 from public.campaign_status_events e
     where e.campaign_id = c.id);

comment on table public.campaign_status_events is
    'Every campaigns.status transition. `automatic` separates a system '
    'inference from a person''s decision; see 021.';
comment on column public.campaigns.approved_by is
    'Who approved the campaign for launch. Set only on the review -> '
    'approved transition, which requires every asset slot approved.';

commit;
