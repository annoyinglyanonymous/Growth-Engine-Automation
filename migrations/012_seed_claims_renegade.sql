-- 012_seed_claims_renegade.sql
--
-- Every Renegade assertion the corpus contains, with a decision attached.
--
-- Derived from scripts/claim_worksheet.py, which found 46 distinct candidate
-- assertions across 182 chunks. Of those: 28 carried figures, 3 were bare
-- superlatives, 6 were third-party validations, 4 were customer testimonials
-- and 5 were staff biographies. The bios are excluded (true of a named person,
-- not of the company); everything else is decided below.
--
-- THE DEFAULT APPLIED HERE
-- Published site copy is treated as usable, per the standing decision that
-- what is on the website is the truth. So a plainly-stated site claim is
-- approved unless something specific argues otherwise. The exceptions are the
-- interesting rows, and each one names its reason:
--   - a governed figure contradicts it            ($20,000, from 006)
--   - the corpus contradicts itself               (carrier count)
--   - the claim is unscoped                       ("average completion")
--   - it is an individual result, not a company's ($2,056 saving)
--   - it is a superlative with nothing behind it  ("industry-leading")
--   - it decays with time                         (Google rating, awards)
--
-- WHY prohibited ROWS FOR THINGS NOBODY WROTE
-- Most prohibited rows below quote live site copy, as 006's $20,000 row does.
-- Two do not: "licensed in all 50 states" and the two generalised testimonial
-- figures. Those are pre-emptive -- they are the specific wrong sentence a
-- generator produces when it rounds up. A prohibited claim is the only
-- mechanism the deterministic checker can match on, so a guardrail against an
-- invented claim has to be written as a claim.
--
-- A SCHEMA GAP, RECORDED NOT WORKED AROUND
-- claims.product_id is NOT NULL, and there is no brand-scoped claims table. So
-- brand-level facts -- 48 states, 200+ agents, 9 locations, the Google rating
-- -- have nowhere to live except a product. They are attached to
-- franchise-program below, marked category 'company', because Phase 1 is
-- franchise-only and it costs nothing today. It will cost something the moment
-- an M&A campaign wants to say "licensed in 48 states": that claim will be
-- invisible to it. The fix is a brand_id column on claims with product_id made
-- nullable, and it belongs in 013 alongside the pipeline tables rather than
-- buried in a seed file.
--
-- Guarded on (product_id, claim_text) rather than on conflict, because
-- public.claims has no unique constraint -- and per migrate.py's own docstring
-- the SQL editor is not a privileged path, so this file has to survive being
-- pasted twice.

begin;

-- ---------------------------------------------------------------------------
-- FRANCHISE PROGRAMME
--
-- feature_id is set where a claim is about a specific feature, so the
-- "don't promote an unavailable feature" check can reach the claim through
-- the feature rather than by matching text.
-- ---------------------------------------------------------------------------
insert into public.claims
    (product_id, feature_id, claim_text, status, category, approved_wording,
     restriction_notes, source, source_url, requires_disclaimer,
     disclaimer_text, effective_from, owner, last_reviewed_at)
select p.id, f.id, v.claim_text, v.status, v.category, v.approved_wording,
       v.restriction_notes, v.source, v.source_url, v.requires_disclaimer,
       v.disclaimer_text, now(), 'automate@renegadeinsurance.com', now()
from public.products p
join public.brands b on b.id = p.brand_id
cross join (values
    -- ---- compensation -----------------------------------------------------
    ('commission-split',
     'Renegade franchise owners earn 80% on new business personal lines '
     'commissions and up to 80% on renewals.',
     'approved', 'compensation',
     'Franchise owners earn 80% on new business personal lines commissions '
     'and up to 80% on renewals. Renegade pays agencies monthly based on '
     'carrier commissions received.',
     null,
     'Stated four times on /franchise/, consistently, including in the FAQ '
     '"How are Renegade franchise owners compensated?".',
     'https://renegadeinsurance.com/franchise/', false, null),

    (null,
     'Franchise owners earn 80% commission on new business, and Renegade '
     'runs the back office.',
     'prohibited', 'compensation', null,
     'Live on /about-us/ and prohibited for what it omits, not for the '
     'figure. It drops "personal lines", which scopes the 80% -- the site '
     'says nothing about commercial lines splits -- and it drops renewals '
     'entirely, where the rate is "up to 80%", not 80%. Use the approved '
     'wording.',
     'Superseded by the scoped /franchise/ wording.',
     'https://renegadeinsurance.com/about-us/', false, null),

    (null,
     'Renegade offers industry-leading commissions.',
     'prohibited', 'compensation', null,
     'Unsubstantiated superlative, appearing three times on the live site '
     '(/careers/ twice, /become-an-agency-owner/ once). The corpus contains '
     'no competitor benchmark of any kind, so there is nothing to lead. '
     'Replace it with the figure -- 80% on new business personal lines '
     'commissions -- which is stronger anyway.',
     'Flagged by scripts/claim_worksheet.py as a superlative with no '
     'supporting figure.',
     'https://renegadeinsurance.com/become-an-agency-owner/', false, null),

    -- ---- timeline ---------------------------------------------------------
    (null,
     'Most Renegade franchise locations open within 60 to 180 days of '
     'signing.',
     'approved', 'timeline',
     'Most Renegade franchise locations open within 60 to 180 days of '
     'signing. Timeline depends on licensing, location setup and local '
     'permit requirements in your state.',
     null,
     'FAQ on /franchise/: "How long does it take to open a Renegade '
     'Insurance agency?".',
     'https://renegadeinsurance.com/franchise/', false, null),

    (null,
     'Most independent insurance agency franchises open within 60 to 180 '
     'days of signing.',
     'prohibited', 'timeline', null,
     'Also live on /franchise/, and the problem is the subject: this asserts '
     'a timeline for insurance franchises Renegade does not operate. '
     'Nothing in the corpus evidences an industry-wide figure, and the '
     'Renegade-scoped version conveys the same thing about the only thing '
     'we can evidence.',
     'Unsupported industry-wide generalisation of a Renegade-specific '
     'figure.',
     'https://renegadeinsurance.com/franchise/', false, null),

    -- ---- programme content ------------------------------------------------
    ('two-week-training',
     'Renegade provides a mandatory two-week hands-on training programme '
     'using real leads.',
     'approved', 'training',
     'Renegade provides a mandatory two-week hands-on training programme '
     'using real leads, covering the full sales process, carrier systems '
     'and agency management.',
     null,
     'Stated on /franchise/ in both the onboarding steps and the FAQ '
     '"Does Renegade Insurance provide training?".',
     'https://renegadeinsurance.com/franchise/', false, null),

    ('candidate-financing',
     'Financing is available for qualified candidates.',
     'approved', 'pricing',
     'Financing is available for qualified candidates.',
     null,
     'FAQ on /franchise/: "Does Renegade Insurance offer financing?".',
     'https://renegadeinsurance.com/franchise/', false, null),

    ('salesforce-platform',
     'Franchise owners run on Salesforce with AI tools built for quoting, '
     'client tracking and reporting.',
     'approved', 'technology',
     'Franchise owners run on Salesforce with AI tools built for quoting, '
     'client tracking and reporting.',
     null,
     '/franchise/, "Technology Advantage".',
     'https://renegadeinsurance.com/franchise/', false, null),

    ('direct-carrier-access',
     'Renegade holds direct appointments with regional and national carriers '
     'and MGAs across personal lines, commercial lines and specialty '
     'products.',
     'approved', 'positioning',
     'Renegade holds direct appointments with regional and national carriers '
     'and MGAs across personal lines, commercial lines and specialty '
     'products, giving franchise owners broad market access from day one.',
     null,
     '/franchise/, "Direct Carrier Access".',
     'https://renegadeinsurance.com/franchise/', false, null),

    ('back-office-operations',
     'Renegade''s operations team handles customer service, bookkeeping and '
     'marketing for franchise owners.',
     'approved', 'positioning',
     'Renegade''s operations team handles customer service, bookkeeping and '
     'marketing, so franchise owners focus on sales rather than operations.',
     null,
     'Stated three times on /franchise/ and once on /about-us/.',
     'https://renegadeinsurance.com/franchise/', false, null),

    -- ---- company-level facts (see the schema-gap note above) --------------
    (null,
     'Renegade is licensed for Property and Casualty in 48 states and the '
     'District of Columbia.',
     'approved', 'company',
     'Renegade is licensed for Property and Casualty insurance in 48 states '
     'and the District of Columbia.',
     null,
     'Verified against /license-disclosures/, which lists 49 named '
     'jurisdictions with individual licence numbers: 48 states plus the '
     'District of Columbia. The site''s own shorthand "48 states" is '
     'accurate but omits DC. NOTE: the curated '
     'KB/renegade-kb/kb/state-licenses.json holds only 48 rows because the '
     'live page misspells Massachusetts as "Massachuetts" and that row was '
     'dropped during extraction -- the data is short a state, the claim is '
     'not wrong.',
     'https://renegadeinsurance.com/license-disclosures/', false, null),

    (null,
     'Renegade is licensed in all 50 states.',
     'prohibited', 'company', null,
     'False. /license-disclosures/ lists 48 states plus DC; Alaska and '
     'Hawaii are absent. Never claim all 50 states, nationwide coverage, '
     'or "every state", and never geo-target advertising to AK or HI -- see '
     'the geographic-targeting rule. Recorded pre-emptively: no page says '
     'this, but it is the sentence a generator writes when it rounds 48 up.',
     'Derived from the 49 licence records in kb.entities.',
     'https://renegadeinsurance.com/license-disclosures/', false, null),

    (null,
     'Renegade has 200+ agents.',
     'approved', 'company',
     'Choose from 200+ agents working with multiple leading carriers.',
     null,
     'Homepage, stated twice. Site-sourced and approved under the standing '
     'rule that published copy is usable. Stated honestly: this is NOT '
     'independently verifiable from this knowledge base -- no agent roster '
     'exists here -- so it is a company claim, not a verified fact.',
     'https://renegadeinsurance.com/', false, null),

    (null,
     'Renegade operates 9 open retail agency locations across Florida, '
     'South Carolina, Georgia and Texas.',
     'approved', 'company',
     'Renegade operates 9 retail agency locations across Florida, South '
     'Carolina, Georgia and Texas.',
     null,
     'The only claim in this file verifiable from structured data rather '
     'than prose: kb.entities holds 11 location records, of which 2 are '
     'flagged closed (Edgewater FL, New Smyrna Beach FL), leaving 9. This '
     'reconciles the site''s own "9 retail locations". Re-derive the count '
     'before each campaign -- it changes whenever a location opens or '
     'closes.',
     null, false, null),

    (null,
     'Renegade provides quotes from 100 insurance companies.',
     'prohibited', 'company', null,
     'Open conflict in kb.conflicts (topic carrier-count): site meta claims '
     '100 insurance companies while the homepage shows 11 carrier logos, '
     'and 11 is the verifiable set. Never state a carrier count in any '
     'channel. Say "access to leading national and regional carriers", or '
     'name carriers from the 11.',
     'kb.conflicts topic=carrier-count, severity=warning, status=active.',
     'https://renegadeinsurance.com/', false, null),

    (null,
     'Renegade holds a 4.7 out of 5 Google rating.',
     'restricted', 'company', null,
     'A rating moves, and stale review claims are a live advertising-'
     'compliance issue rather than a tidiness one. Usable only with the '
     'source named and an as-of date -- "4.7 out of 5 on Google as of '
     '<month year>" -- and only after re-checking the current figure. Never '
     '"rated 4.7 stars" with no source or date.',
     'Homepage: "4.7 / 5 Google Reviews".',
     'https://renegadeinsurance.com/', false, null),

    (null,
     'Renegade Insurance is a Great Place to Work Certified company.',
     'restricted', 'company', null,
     'Great Place to Work certification runs for a 12-month period. Usable '
     'only with the certification year stated, only while current, and with '
     'the registered mark reproduced. Verify before each campaign.',
     '/franchise/ and /careers/.',
     'https://renegadeinsurance.com/careers/', false, null),

    (null,
     'Renegade Insurance is a 2026 Global Recognition Award winner.',
     'restricted', 'company', null,
     'The awarding body must be verified before any paid use. Awards with '
     'similar names operate on paid entry, which changes what may be '
     'claimed and makes this a comparative-advertising exposure if used as '
     'a superiority signal. Never present it as evidence of being better '
     'than competitors.',
     '/franchise/, "2026 Global Recognition Award Winner".',
     'https://renegadeinsurance.com/franchise/', false, null),

    -- ---- testimonial figures, blocked as company claims -------------------
    (null,
     'Renegade customers save $2,056 a year.',
     'prohibited', 'testimonial', null,
     'This figure is one customer''s result, quoted on /agency/porter/: '
     '"With Carly''s help, we are switching over both our car and home '
     'insurance and are saving $2056.00 a year!". An individual result is '
     'not a company claim and may never be generalised, averaged or '
     'restated without attribution. Quoting the testimonial verbatim and '
     'attributed, with a results-vary qualifier, is fine -- see the '
     'testimonial-use rule.',
     'Customer testimonial, /agency/porter/.',
     'https://renegadeinsurance.com/agency/porter/', false, null),

    (null,
     'Renegade lowers premiums by $400.',
     'prohibited', 'testimonial', null,
     'One customer''s condo premium reduction after wind-mitigation '
     'documentation, quoted on three HIG location pages. Same reasoning as '
     'the $2,056 row: an individual result, tied to a specific coverage '
     'action, never a company claim.',
     'Customer testimonial, /agency/edgewater/ and two others.',
     'https://renegadeinsurance.com/agency/port-orange/', false, null)
) as v(feature_slug, claim_text, status, category, approved_wording,
       restriction_notes, source, source_url, requires_disclaimer,
       disclaimer_text)
left join public.product_features f
       on f.product_id = p.id and f.slug = v.feature_slug
where b.slug = 'renegade' and p.slug = 'franchise-program'
  and not exists (select 1 from public.claims c
                  where c.product_id = p.id and c.claim_text = v.claim_text);


-- ---------------------------------------------------------------------------
-- AGENCY ACQUISITION
--
-- Inert until someone sets products.approved_for_marketing = true on
-- agency-acquisition. Recorded now because the figures are real and the "up
-- to 90%" qualifier needs governing before, not after, the first M&A brief.
-- ---------------------------------------------------------------------------
insert into public.claims
    (product_id, feature_id, claim_text, status, category, approved_wording,
     restriction_notes, source, source_url, requires_disclaimer,
     effective_from, owner, last_reviewed_at)
select p.id, f.id, v.claim_text, v.status, v.category, v.approved_wording,
       v.restriction_notes, v.source, v.source_url, false, now(),
       'automate@renegadeinsurance.com', now()
from public.products p
join public.brands b on b.id = p.brand_id
cross join (values
    ('cash-at-close',
     'Up to 90% cash upfront at close, with no earnouts.',
     'restricted', 'pricing', null,
     'A financial claim about what a seller receives, so "up to" is not '
     'softening language -- dropping it converts a ceiling into a promise. '
     'Never state 90% as the amount received, never omit "up to", never '
     'imply the balance is guaranteed. Source copy reads "Up tp 90% cash '
     'upfront" -- a typo on the live page; do not reproduce it.',
     '/sell-your-insurance-agency/, "Cash at Close, Not an Earnout".',
     'https://renegadeinsurance.com/sell-your-insurance-agency/'),

    ('no-broker-fees',
     'Renegade buys directly from agency owners with no broker fees at any '
     'stage of the transaction.',
     'approved', 'pricing',
     'Renegade buys directly from agency owners. There are no broker fees at '
     'any stage of the transaction.',
     null,
     'Stated in the body and again in the FAQ "Do I have to use a broker to '
     'sell my insurance agency?".',
     'https://renegadeinsurance.com/sell-your-insurance-agency/'),

    (null,
     'Most book transfers complete in 30 to 120 days from contract signing.',
     'approved', 'timeline',
     'Most book transfers complete in 30 to 120 days from contract signing, '
     'depending on carrier requirements and administrative procedures.',
     null,
     'FAQ on /sell-your-insurance-agency/. Scoped to post-contract transfer, '
     'which is what makes it usable.',
     'https://renegadeinsurance.com/sell-your-insurance-agency/'),

    (null,
     'Average completion in six months.',
     'restricted', 'timeline', null,
     'The live page states this without saying what completes, directly '
     'above a three-step process, while its own FAQ says book transfers '
     'complete in 30 to 120 days from contract signing. The two reconcile '
     '-- roughly six months end to end, of which 30 to 120 days is the '
     'post-contract transfer -- but only when the scope is stated. Never '
     'use the bare sentence: it reads as a contradiction of the FAQ.',
     '/sell-your-insurance-agency/, process section.',
     'https://renegadeinsurance.com/sell-your-insurance-agency/'),

    ('carrier-appointment-continuity',
     'Existing carrier appointments stay active through the transfer, with '
     'no mid-term rewrites and no coverage gaps.',
     'approved', 'process',
     'Existing carrier appointments stay active through the transfer. No '
     'mid-term rewrites, no coverage gaps, no carrier disruption.',
     null,
     '/sell-your-insurance-agency/, "Carrier Appointments Stay Intact".',
     'https://renegadeinsurance.com/sell-your-insurance-agency/'),

    ('market-based-valuation',
     'The offer is calculated on retention ratio, commission revenue, '
     'growth trajectory and book mix rather than a blanket multiple.',
     'approved', 'pricing',
     'Your offer is calculated on retention ratio, commission revenue, '
     'growth trajectory and book mix. Not a blanket multiple.',
     null,
     '/sell-your-insurance-agency/, "Market-Based Valuation".',
     'https://renegadeinsurance.com/sell-your-insurance-agency/')
) as v(feature_slug, claim_text, status, category, approved_wording,
       restriction_notes, source, source_url)
left join public.product_features f
       on f.product_id = p.id and f.slug = v.feature_slug
where b.slug = 'renegade' and p.slug = 'agency-acquisition'
  and not exists (select 1 from public.claims c
                  where c.product_id = p.id and c.claim_text = v.claim_text);


-- ---------------------------------------------------------------------------
-- BACK OFFICE SUPPORT
-- ---------------------------------------------------------------------------
insert into public.claims
    (product_id, feature_id, claim_text, status, category, approved_wording,
     source, source_url, requires_disclaimer, effective_from, owner,
     last_reviewed_at)
select p.id, f.id, v.claim_text, 'approved', v.category, v.approved_wording,
       v.source, v.source_url, false, now(),
       'automate@renegadeinsurance.com', now()
from public.products p
join public.brands b on b.id = p.brand_id
cross join (values
    ('policy-servicing',
     'Renegade handles renewals, cancellations, endorsements and policy '
     'downloads for Back Office Support partners.',
     'positioning',
     'Renewals, cancellations, endorsements and policy downloads handled. '
     'Carrier relationships and appointments managed. Remarkets and '
     'back-end service support.',
     'Stated identically on /insurance-agency-back-office-support/ and in '
     'the cross-link block on /sell-your-insurance-agency/.',
     'https://renegadeinsurance.com/insurance-agency-back-office-support/'),

    ('book-ownership-retained',
     'Back Office Support partners keep ownership of their book while '
     'Renegade handles servicing, back office and carrier access.',
     'positioning',
     'Partners retain their book while Renegade handles servicing, back '
     'office and carrier access.',
     'FAQ on /sell-your-insurance-agency/: "What if I''m not ready to sell '
     'my agency yet?". This is the claim that separates back-office from '
     'acquisition.',
     'https://renegadeinsurance.com/sell-your-insurance-agency/'),

    (null,
     'The average transition to Renegade back office services completes in '
     'six months.',
     'timeline',
     'The average transition to Renegade back office services completes in '
     'six months.',
     'FAQ on /insurance-agency-back-office-support/: "How long does the '
     'transition take?". Approved where the identical figure on the '
     'acquisition page is restricted, and the difference is the only thing '
     'that matters -- this sentence says what completes.',
     'https://renegadeinsurance.com/insurance-agency-back-office-support/')
) as v(feature_slug, claim_text, category, approved_wording, source,
       source_url)
left join public.product_features f
       on f.product_id = p.id and f.slug = v.feature_slug
where b.slug = 'renegade' and p.slug = 'back-office-support'
  and not exists (select 1 from public.claims c
                  where c.product_id = p.id and c.claim_text = v.claim_text);


-- ---------------------------------------------------------------------------
-- BRAND RULES: the enforcement half.
--
-- 005 and 006 established why these ship together with claims. A claim table
-- describes what is true; brand_rules is what the agent is actually
-- instructed to do, and kb.brand_context sorts blockers first. A claim
-- without its rule is documentation.
-- ---------------------------------------------------------------------------
insert into public.brand_rules
    (brand_id, product_id, category, rule_text, severity, status,
     applies_to_channels, good_example, bad_example, owner, last_reviewed_at)
select b.id,
       case when v.product_slug is null then null else p.id end,
       v.category, v.rule_text, v.severity, 'active', v.channels,
       v.good_example, v.bad_example,
       'automate@renegadeinsurance.com', now()
from public.brands b
-- The VALUES list must be joined BEFORE the left join that references it:
-- v.product_slug in an ON clause that precedes v is an invalid FROM-clause
-- reference, not a lateral one.
cross join (values
    -- The most consequential rule in this file, and it comes from the site's
    -- own footer disclaimer on /franchise/ rather than from anyone's caution.
    ('franchise-program', 'legal',
     'Franchise marketing is not an offer to sell a franchise. The offer can '
     'only be made through a Franchise Disclosure Document provided before '
     'any agreement is signed, and no offer may be made to residents of '
     'states where the franchise is not registered or approved. Every '
     'franchise asset must therefore avoid offer and commitment language, '
     'must not imply that responding creates or leads directly to an '
     'agreement, and must not promise territory. Route to a qualifying '
     'conversation, never to a purchase.',
     'blocker',
     array['email', 'meta_ads', 'google_ads', 'landing_page', 'sms'],
     'Talk to a Franchise Success Specialist to see whether you qualify. '
     'Full terms are provided in the Franchise Disclosure Document.',
     'Sign up today and own your Renegade agency -- territories are being '
     'reserved now.'),

    (null, 'messaging',
     'One asset addresses one audience. Never combine franchise recruitment '
     '(start an agency) with agency acquisition (sell your agency) or back '
     'office support (keep your agency, stop servicing it). Check the '
     'campaign type''s prohibited_themes before writing. This is not '
     'hypothetical: the live site cross-links all three programmes to each '
     'other, so retrieval surfaces the wrong programme''s copy for any of '
     'them.',
     'blocker',
     array['email', 'meta_ads', 'google_ads', 'landing_page'],
     'Own an independent insurance agency. 80% on new business personal '
     'lines commissions, and Renegade runs the back office.',
     'Whether you want to own an agency or sell the one you have, Renegade '
     'has an option for you.'),

    (null, 'testimonials',
     'Customer and seller testimonials may be used only verbatim, attributed '
     'as they are on the site, and with a results-vary qualifier. Never '
     'convert a testimonial figure into a company claim, never average or '
     'aggregate testimonials, and never edit a quote to strengthen it. The '
     '$2,056 and $400 figures in the corpus are individual results and are '
     'recorded as prohibited claims.',
     'blocker',
     array['email', 'meta_ads', 'google_ads', 'landing_page'],
     '"With Carly''s help, we are switching over both our car and home '
     'insurance and are saving $2056.00 a year!" -- Porter, TX customer. '
     'Individual results vary.',
     'Renegade customers save over $2,000 a year.'),

    (null, 'geography',
     'Never target advertising to Alaska or Hawaii: Renegade holds no '
     'insurance licence in either state. Never drive traffic to the '
     'Edgewater FL or New Smyrna Beach FL agency pages -- both locations '
     'are flagged closed in the knowledge base while their quote pages '
     'remain live on the site. Do not treat Miami as a Renegade location: '
     'no Miami location record exists, and the Pembroke Pines page '
     'misidentifies itself as Renegade Insurance Miami with a Miami phone '
     'number.',
     'blocker',
     array['meta_ads', 'google_ads', 'email'],
     'Targeting: Florida, South Carolina, Georgia, Texas -- excluding '
     'Edgewater and New Smyrna Beach.',
     'Targeting: nationwide, all 50 states.'),

    (null, 'licensing',
     'Any licensing claim must carry the Property and Casualty scope and '
     'must count the District of Columbia separately from states: "48 states '
     'and the District of Columbia". Never "all 50 states", "nationwide", or '
     '"every state" -- Alaska and Hawaii are absent from '
     '/license-disclosures/.',
     'blocker',
     array['email', 'meta_ads', 'google_ads', 'landing_page'],
     'Licensed for Property and Casualty in 48 states and the District of '
     'Columbia.',
     'Licensed nationwide.'),

    (null, 'superlatives',
     'Superlatives and absolutes -- industry-leading, best-in-class, #1, '
     'fastest-growing, unlimited, guaranteed -- require a substantiating '
     'approved claim. Where a figure exists, use the figure instead: it is '
     'both defensible and more persuasive. Specifically, replace '
     '"industry-leading commissions" with "80% on new business personal '
     'lines commissions".',
     'warning',
     array['email', 'meta_ads', 'google_ads', 'landing_page'],
     'Earn 80% on new business personal lines commissions and up to 80% on '
     'renewals.',
     'Earn industry-leading commission splits.'),

    (null, 'claims-freshness',
     'Three approved company claims decay and must be re-checked before '
     'each campaign launch rather than trusted: the Google rating (4.7 out '
     'of 5, needs an as-of date), Great Place to Work certification (annual '
     'period), and the retail location count (9 open of 11 on record). The '
     'location count is the one that can be re-derived from data -- count '
     'kb.entities location records where closed is not true.',
     'info',
     array['email', 'meta_ads', 'google_ads', 'landing_page'],
     '4.7 out of 5 on Google as of September 2026.',
     'Rated 4.7 stars.')
) as v(product_slug, category, rule_text, severity, channels, good_example,
       bad_example)
left join public.products p
       on p.brand_id = b.id and p.slug = v.product_slug
where b.slug = 'renegade'
  and not exists (select 1 from public.brand_rules r
                  where r.brand_id = b.id
                    and r.category = v.category
                    and r.rule_text = v.rule_text);

commit;
