-- 006_seed_franchise_fee.sql
--
-- The first governed fact in this database, and the first thing to cross the
-- kb -> public boundary.
--
-- Nothing in the ingestion pipeline can write here. A figure only becomes an
-- approved claim when a human confirms it, which is what happened: the
-- franchise-fee conflict ($20,000 on /about-us/ vs $25,000 on /franchise/)
-- was put to the account owner and answered.
--
-- PROVENANCE, stated exactly as strong as it is: this is confirmation from
-- automate@renegadeinsurance.com on 2026-08-26. It is NOT a citation of the
-- Franchise Disclosure Document. The FDD's initial-fee item is the legally
-- controlling figure in a franchise offering, so if the FDD reference is ever
-- obtained it should be appended to claims.source and last_reviewed_at bumped.
--
-- Three rows, doing three different jobs:
--   products     the thing being priced (claims.product_id is NOT NULL)
--   claims       the approved figure, plus a PROHIBITED row for the stale one
--   brand_rules  the enforceable instruction the agent reads

begin;

-- ---------------------------------------------------------------------------
-- The franchise program is a product in the marketing sense: something sold,
-- with a price and a CTA. It is not an insurance product, which is why
-- approved_for_marketing matters -- franchise recruitment copy is a different
-- compliance regime from policy copy.
-- ---------------------------------------------------------------------------
insert into public.products
    (brand_id, name, slug, description, status, approved_for_marketing,
     pricing_summary, primary_cta, owner, last_reviewed_at)
select b.id,
       'Renegade Franchise Program',
       'franchise-program',
       'Agency-ownership franchise for licensed P&C agents and career-changers. '
       'Franchisee earns 80% commission on new business; Renegade runs the back office.',
       'active',
       true,
       'Initial franchise fee starts at $25,000, varying by business type. '
       'Monthly support and technology fee applies after launch. Financing '
       'available for qualified candidates.',
       'Talk to a Franchise Success Specialist',
       'automate@renegadeinsurance.com',
       now()
from public.brands b
where b.slug = 'renegade'
on conflict (brand_id, slug) do nothing;


-- ---------------------------------------------------------------------------
-- The approved figure.
--
-- requires_disclaimer is true and that is not boilerplate caution: every
-- source sentence on the live site carries "starting at" and "depending on
-- business type", and franchise fee advertising is governed by the FDD. A bare
-- "$25,000" with those qualifiers dropped is a materially different claim.
-- ---------------------------------------------------------------------------
insert into public.claims
    (product_id, claim_text, status, category, approved_wording,
     source, source_url, requires_disclaimer, disclaimer_text,
     effective_from, owner, last_reviewed_at)
select p.id,
       'Initial Renegade franchise fee starts at $25,000.',
       'approved',
       'pricing',
       'Initial franchise fees start at $25,000 depending on your business type. '
       'A monthly support and technology fee applies after launch. Financing is '
       'available for qualified candidates.',
       'Confirmed by automate@renegadeinsurance.com, 2026-08-26, resolving the '
       'franchise-fee conflict between /about-us/ and /franchise/. Not yet '
       'cross-checked against the Franchise Disclosure Document.',
       'https://renegadeinsurance.com/franchise/',
       true,
       'Additional startup costs vary by state and are detailed in the '
       'Franchise Disclosure Document.',
       now(),
       'automate@renegadeinsurance.com',
       now()
from public.products p
join public.brands b on b.id = p.brand_id
where b.slug = 'renegade' and p.slug = 'franchise-program';


-- ---------------------------------------------------------------------------
-- The stale figure, recorded as a claim with status='prohibited' rather than
-- simply deleted from memory.
--
-- $20,000 is still live on /about-us/ and still sits in 16 KB documents. An
-- agent that retrieves one of those passages needs a governed row saying the
-- figure is wrong -- absence of an approved claim is not the same signal.
-- ---------------------------------------------------------------------------
insert into public.claims
    (product_id, claim_text, status, category, restriction_notes,
     source, source_url, effective_from, owner, last_reviewed_at)
select p.id,
       'Initial Renegade franchise fee starts at $20,000.',
       'prohibited',
       'pricing',
       'Superseded. $20,000 is stale copy still published on /about-us/ and '
       'mirrored into 16 KB documents. Never use in any channel. The page '
       'itself needs correcting to $25,000.',
       'Superseded by owner confirmation 2026-08-26.',
       'https://renegadeinsurance.com/about-us/',
       now(),
       'automate@renegadeinsurance.com',
       now()
from public.products p
join public.brands b on b.id = p.brand_id
where b.slug = 'renegade' and p.slug = 'franchise-program';


-- ---------------------------------------------------------------------------
-- The instruction the agent actually reads, via kb.brand_context.
--
-- This is the enforcement half. Two migrations ago I proposed adding a
-- forbidden-figures column to kb.conflicts for exactly this -- unnecessary,
-- because brand_rules already has severity='blocker' plus good/bad examples.
-- The governed side owns directives; kb only describes what the sites say.
-- ---------------------------------------------------------------------------
insert into public.brand_rules
    (brand_id, product_id, category, rule_text, severity, status,
     good_example, bad_example, owner, last_reviewed_at)
select b.id, p.id,
       'pricing',
       'The initial Renegade franchise fee is $25,000. Never state $20,000 -- '
       'it is stale copy still live on /about-us/. Always keep the "starting '
       'at" and "depending on business type" qualifiers; never present the fee '
       'as a flat or final price.',
       'blocker',
       'active',
       'Initial franchise fees start at $25,000 depending on your business type.',
       'Own your own agency for just $20,000.',
       'automate@renegadeinsurance.com',
       now()
from public.brands b
join public.products p on p.brand_id = b.id and p.slug = 'franchise-program'
where b.slug = 'renegade';

commit;
