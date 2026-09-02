-- ---------------------------------------------------------------------------
-- 023  Prohibition becomes something the operator declares
--
-- THE DECISION THIS IMPLEMENTS
-- Until now every prohibition in this database was written by me, derived from
-- reading the corpus: 12 claims at status='prohibited' and 12 brand_rules at
-- severity='blocker', across both brands. They are defensible readings, but
-- they are readings -- nobody at the company ruled on them, and the rows said
-- otherwise (see the stamp clearing below).
--
-- Prohibition should be an instruction, not an inference. The operator says
-- "do not add this and that" and that is what a prohibition is. So:
--
--   * public.brand_exclusions -- a standing list per brand. Written by a named
--     operator, applies to every campaign for that brand.
--   * campaigns.do_not_mention -- the same thing scoped to one brief, for the
--     one-off that is not a standing rule.
--
-- Both are enforced identically: rendered into the prompt ahead of the
-- evidence, and checked deterministically against every generated field. A
-- match is a blocker, because "do not say this" is not advice.
--
-- WHY TWO PLACES AND NOT ONE
-- They fail differently. A standing exclusion forgotten on a brief is a
-- compliance problem; a one-off promoted to a standing rule quietly narrows
-- every future campaign. Keeping them separate means a blocked line can say
-- WHICH list caught it, and the reviewer knows whether to argue with the brief
-- or with the brand.
--
-- PARKING WHAT I WROTE
-- Reversible on purpose. Prohibited claims become 'pending_review' -- undecided
-- rather than deleted -- so they surface in scripts/claim_review.py as things
-- to rule on, with their reasoning (restriction_notes) intact. Blocker rules
-- become 'inactive' for the same reason.
--
-- KNOWN CONSEQUENCE, stated rather than discovered: after this migration
-- check_prohibited_wording has nothing to match until the operator adds
-- something. '$20,000' in a brief, and 'Markets covers every admitted and E&S
-- option', both pass validation clean. That is the intended trade -- an
-- unreviewed guardrail is a guess with a badge on -- but it IS a reduction in
-- cover, and re-activating any parked row is a one-line UPDATE.
--
-- campaign_types.prohibited_themes is deliberately LEFT ACTIVE. It is not a
-- "do not say this" list: it stops franchise language appearing in an M&A
-- campaign, which is a coherence guard about which programme a campaign is
-- for, not an assertion about what is true. Different question, different
-- decision. Say the word and it goes the same way.
--
-- CLEARING THE FALSE REVIEW STAMPS
-- 012 and 019 both wrote owner = 'automate@renegadeinsurance.com' and
-- last_reviewed_at = now() into every row they seeded -- 54 claims and 21
-- rules. The database therefore asserted a human review that never happened,
-- on the exact date the seed ran, which is the same class of error as an
-- unattributed approval and worse for being confidently dated. Both fields go
-- back to NULL. Reviewing a claim now means something, and the worksheet is
-- asking a real question.
--
-- REVERSIBLE
--   -- un-park (restores the prohibitions, not the stamps):
--   update public.claims set status = 'prohibited'
--    where status = 'pending_review' and restriction_notes is not null;
--   update public.brand_rules set status = 'active' where status = 'inactive';
--   drop table public.brand_exclusions;
--   alter table public.campaigns drop column do_not_mention;
-- ---------------------------------------------------------------------------

-- ------------------------------------------------------------------ standing
create table if not exists public.brand_exclusions (
    id          uuid primary key default gen_random_uuid(),
    brand_id    uuid not null
                     references public.brands (id) on delete cascade,

    -- The words not to use. Two characters minimum: a one-character exclusion
    -- would match inside almost every sentence, and contains_phrase anchors on
    -- word boundaries rather than substrings, so a stray "a" would be both
    -- useless and alarming.
    phrase      text not null check (length(btrim(phrase)) >= 2),

    -- Optional. Forcing a reason produces "because I said so", and the
    -- instruction stands without one -- but where a reason exists it lands in
    -- the blocker's remedy, which is where a reviewer actually reads it.
    note        text,

    -- Not nullable, and no default. The whole point of this table is that
    -- these are the operator's decisions rather than mine; a row that cannot
    -- say whose decision it was defeats the purpose.
    added_by    text not null,
    added_at    timestamptz not null default now(),

    -- Retired rather than deleted, so "we used to forbid this and stopped"
    -- stays answerable. Same attribution discipline as 022's rejection.
    active      boolean not null default true,
    retired_by  text,
    retired_at  timestamptz,

    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),

    constraint brand_exclusions_retirement_attributed
        check (active
               or (retired_by is not null and retired_at is not null))
);

-- Case- and whitespace-insensitive, because "Free Forever" and "free forever"
-- are the same instruction and a duplicate pair would report the same blocker
-- twice with different capitalisation.
create unique index if not exists brand_exclusions_phrase_key
    on public.brand_exclusions (brand_id, lower(btrim(phrase)));

create index if not exists brand_exclusions_active_idx
    on public.brand_exclusions (brand_id)
    where active;

alter table public.brand_exclusions enable row level security;

create trigger brand_exclusions_set_updated_at
    before update on public.brand_exclusions
    for each row execute function public.set_updated_at();

comment on table public.brand_exclusions is
    'Operator-declared wording that must not appear in generated copy for '
    'this brand. Written by a person, not derived from the corpus (023).';

-- ------------------------------------------------------------------ per brief
alter table public.campaigns
    add column if not exists do_not_mention text[] not null default '{}';

comment on column public.campaigns.do_not_mention is
    'Wording this campaign must not use, from the brief. One-off exclusions; '
    'brand_exclusions holds the standing ones (023).';

-- ------------------------------------------------------------------- parking
-- pending_review, not deleted. restriction_notes travels with the row, so the
-- reasoning survives for whoever rules on it.
update public.claims
   set status = 'pending_review'
 where status = 'prohibited';

update public.brand_rules
   set status = 'inactive'
 where severity = 'blocker'
   and status = 'active';

-- ------------------------------------------------- the false review stamps
-- Matched on the exact value the seeds wrote, so a row a human has genuinely
-- reviewed since is left alone.
update public.claims
   set owner = null, last_reviewed_at = null
 where owner = 'automate@renegadeinsurance.com';

update public.brand_rules
   set owner = null, last_reviewed_at = null
 where owner = 'automate@renegadeinsurance.com';
