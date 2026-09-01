-- 018_seed_product_features_agencyheight.sql
--
-- The nine tools inside agent-platform, with `available` and
-- `approved_for_marketing` set independently.
--
-- Requires 017 (the product must exist).
--
-- WHY THE FEATURE FLAGS ARE NOT ALL FALSE
-- agent-platform carries approved_for_marketing = false, so the product gate
-- in kb_context.fetch_claims already withholds every approved claim for this
-- brand. It would be consistent to mark every feature false too and let the
-- whole thing be uniformly inert.
--
-- That would throw away the only information this table exists to carry. The
-- product flag answers "has anyone signed off on marketing this at all"; the
-- feature flags answer "which parts of it are safe to promote once someone
-- has". Setting all nine to false collapses those into one bit and guarantees
-- that whoever flips the product gate inherits nine features that all look
-- equally cleared -- including the four below that specifically are not.
--
-- So the feature flags record the real judgement now, while the product gate
-- holds everything back until a human at Agency Height reviews it. Five true,
-- four false, and the four are the interesting ones.
--
-- THE FOUR THAT ARE AVAILABLE BUT NOT PROMOTABLE
-- This is the same shape as 011's territory-availability: the feature exists,
-- customers get it, and an asset still must not lead with it.
--
--   lead-bank         the lead volume is the thing agents actually buy, and
--                     the site substantiates it nowhere. Promoting it invites
--                     a guaranteed-leads reading, which is a blocker in 019.
--   agency-website    the source copy contradicts itself about which tier
--                     includes it (see below), and "live within a week" is a
--                     delivery promise with no stated conditions.
--   recommended-badge "Recommended" reads as an editorial endorsement. It is
--                     bought. Promoting it as a recommendation is close to
--                     undisclosed pay-for-placement.
--   mobile-app        the homepage lists it as a bare label with no detail.
--                     There is nothing truthful to say about it yet.
--
-- A CONTRADICTION IN THE SOURCE COPY, RECORDED HERE
-- The homepage plans table lists Agency Website twice with different meanings.
-- Premium's feature list includes "Agency Website"; Enterprise's list reads
-- "Everything in Premium" followed by "Agency Website", which implies Premium
-- does NOT include it. Both renderings are on the same page. This is most
-- likely the monthly and yearly tabs flattened into one scrape, but it cannot
-- be resolved from the corpus, so the feature is not promotable and 019 files
-- the tier membership as a restricted claim rather than guessing.
--
-- Re-runnable: guarded with `where not exists`, since product_features has no
-- unique key on (product_id, slug).

begin;

insert into public.product_features
    (product_id, name, slug, description, status, available,
     approved_for_marketing, marketing_notes, restrictions,
     effective_from, owner, last_reviewed_at)
select p.id, v.name, v.slug, v.description, 'active', v.available,
       v.approved, v.notes, v.restrictions, now(),
       'automate@renegadeinsurance.com', now()
from public.products p
join public.brands b on b.id = p.brand_id
cross join (values
    ('Markets carrier search',
     'markets',
     'Carrier search across admitted and E&S options, filtered by line of '
     'business, carrier appetite and state. Positioned as the way to check '
     'appetite before calling a carrier.',
     true, true,
     'The homepage headline feature and the one agents are told is free on '
     'every plan. Lead with the job it does -- "check appetite before you '
     'call" -- rather than with coverage counts.',
     'The site says "every admitted and E&S option". Do not repeat that '
     'wording: an absolute completeness claim about a carrier universe cannot '
     'be substantiated and is filed as prohibited in 019. Say that Markets '
     'covers admitted and E&S markets filtered by line, appetite and state.'),

    ('Agent directory profile',
     'agent-directory-profile',
     'A free profile that surfaces to consumers searching by license, state '
     'and coverage type. Leads from the profile route into the CRM.',
     true, true,
     'The acquisition offer. This is what the CTA across roughly 700 '
     'editorial pages points at ("Join our network of successful agents and '
     'start getting quality leads"), and it is genuinely free on Basic.',
     'Free tier includes the profile and inbound leads but caps lead detail '
     'views at 2 per month. An acquisition asset may say the profile is free; '
     'it must not imply that full lead details are.'),

    ('CRM',
     'crm',
     'Lead and book management. Every lead becomes an account and moves from '
     'Suspect to Prospect to Customer. Full pipeline tracking is on the free '
     'plan.',
     true, true,
     'Second half of the "free forever" pair with Markets. The concrete '
     'detail worth using is the named pipeline -- Suspect, Prospect, '
     'Customer -- because it is specific and checkable.',
     'Free on every plan per the homepage. Do not present the CRM as a reason '
     'to upgrade; that inverts the actual offer and is a prohibited theme on '
     'the plan-upgrade campaign type.'),

    ('Intake forms',
     'intake-forms',
     'Coverage-specific bilingual lead capture forms that can be placed on '
     'any channel, with submissions syncing to the CRM.',
     true, true,
     'Bilingual and coverage-specific are the two substantive details. '
     'Available on the free plan alongside profile leads.',
     'The site says "bilingual" without naming the languages. Do not name '
     'Spanish or any other specific language until the corpus states it.'),

    ('Lead Bank',
     'lead-bank',
     'A pool of unclaimed inbound leads, filterable by state and coverage '
     'and claimable in one click. Claims are capped by plan: 2 per month on '
     'Premium, 7 per month on Enterprise. Not available on Basic.',
     true, false,
     'AVAILABLE, NOT PROMOTABLE. This is the feature agents are really '
     'buying, and it is the one the site substantiates least -- there is no '
     'stated pool size, refresh rate, or lead quality standard anywhere in '
     'the corpus. Any asset that leads with it is implying a lead volume '
     'nobody has verified.',
     'Never state or imply a number of available leads, a lead frequency, or '
     'a conversion rate. The monthly CLAIM CAP (2 on Premium, 7 on '
     'Enterprise) is a governed figure and may be stated; the SUPPLY behind '
     'it may not. Never use the testimonial figure "2-3 new leads on a daily '
     'basis" as a platform claim -- it is one agent''s result and is filed as '
     'prohibited in 019.'),

    ('Agency Website',
     'agency-website',
     'An AI-built, SEO-optimised agency website that pulls the agent''s NPN '
     'and specialties automatically. The homepage says it goes live within a '
     'week.',
     true, false,
     'AVAILABLE, NOT PROMOTABLE, for two independent reasons. First, the '
     'plans table contradicts itself about whether Premium includes it or '
     'only Enterprise does. Second, "live within a week" is a delivery '
     'commitment with no stated conditions or exclusions.',
     'Do not state which plan includes this until the contradiction on the '
     'homepage is resolved by Agency Height -- the claim is filed as '
     'restricted in 019. Do not repeat "live within a week" without a '
     'qualifier and a stated dependency on the agent supplying their '
     'details.'),

    ('Recommended badge',
     'recommended-badge',
     'A badge shown against the agent''s directory listing. Included with '
     'Premium and Enterprise.',
     true, false,
     'AVAILABLE, NOT PROMOTABLE. The badge says "Recommended" and is '
     'obtained by paying. Marketing it as a recommendation, an endorsement, '
     'or a quality signal describes it as something it is not.',
     'Never describe the badge as an endorsement, a rating, a verification, '
     'or evidence of quality. If it must be mentioned at all, name it as a '
     'directory placement feature of the paid plans and nothing more.'),

    ('Verified agent routing',
     'verified-agent-routing',
     'Consumer-side matching: shoppers on Agency Height are routed to '
     'agents licensed in their state and filtered by coverage type. '
     'Unclaimed leads flow to the Lead Bank.',
     true, false,
     'AVAILABLE, NOT PROMOTABLE. The routing is real and the licensing '
     'filter is a genuine differentiator, but the site calls the endpoint '
     '"verified agents" without stating anywhere what verification consists '
     'of.',
     'Do not use the word "verified" about agents without stating the '
     'criteria. State-licence matching is substantiated and may be described '
     'directly -- "matched to agents licensed in your state" says the true '
     'part without borrowing credibility from an unexplained process.'),

    ('Mobile app',
     'mobile-app',
     'Listed on the homepage plans table as an included item. No further '
     'detail appears anywhere in the corpus.',
     true, false,
     'AVAILABLE, NOT PROMOTABLE. The corpus contains the label and nothing '
     'else: no platforms, no features, no plan scoping. There is no truthful '
     'sentence to write about it.',
     'Do not mention the mobile app in any asset until the corpus says what '
     'it does and which plans include it.')
) as v(name, slug, description, available, approved, notes, restrictions)
where b.slug = 'agencyheight'
  and p.slug = 'agent-platform'
  and not exists (
      select 1 from public.product_features f
       where f.product_id = p.id and f.slug = v.slug);

commit;
