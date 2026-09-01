-- 011_seed_product_features_renegade.sql
--
-- What each Renegade programme actually includes, so the "don't promote an
-- unavailable feature" check has something to check against.
--
-- Every row below is grounded in scraped site copy. Nothing is inferred from
-- what a franchise programme usually offers -- that is the failure mode this
-- table exists to prevent.
--
-- TWO BOOLEANS, TWO DIFFERENT QUESTIONS
--   available               does the programme include this today?
--   approved_for_marketing  may copy say so?
--
-- They are separate because the interesting cases are the ones where they
-- disagree. territory-availability below is available = true (the site says a
-- specialist reviews territory during application) and
-- approved_for_marketing = false (the corpus contains no territory map and no
-- exclusivity language, and exclusive-territory promises are an FDD Item 12
-- matter). A single "promote this" boolean could not express that.
--
-- FEATURES ON THE TWO UNAPPROVED PRODUCTS
-- agency-acquisition and back-office-support carry
-- approved_for_marketing = false at the product level, so their copy is
-- already blocked. Their features are set false as well, deliberately: when
-- someone approves the product they should have to approve each feature too,
-- not have thirteen unreviewed features go live in one UPDATE.

begin;

-- ---------------------------------------------------------------------------
-- Renegade Franchise Program
-- ---------------------------------------------------------------------------
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
    ('Commission split',
     'commission-split',
     'Franchise owners earn 80% on new business personal lines commissions '
     'and up to 80% on renewals. Renegade pays agencies monthly based on '
     'carrier commissions received.',
     true, true,
     'The strongest single differentiator in the franchise pitch and the '
     'figure that should replace every "industry-leading commissions" '
     'superlative.',
     'Two qualifiers are load-bearing and must survive into every asset: '
     '"personal lines" scopes the 80% (commercial lines are not stated on '
     'the site), and renewals are "up to 80%", not a flat 80%. /about-us/ '
     'drops both -- do not follow it.'),

    ('Back-office operations',
     'back-office-operations',
     'Renegade''s operations team handles customer service, bookkeeping and '
     'marketing so the owner sells rather than runs operations.',
     true, true,
     'Pairs with the commission split as the two-sided value proposition: '
     'more of each policy, less of the operating load.',
     'Does NOT include hiring. The site is explicit that franchise owners '
     'hire their own staff, with Renegade providing guidance only. Never '
     'write copy implying Renegade staffs the agency or that the owner has '
     'no operational responsibilities.'),

    ('Salesforce-based platform',
     'salesforce-platform',
     'Franchise owners run on Salesforce with AI tools for quoting, client '
     'tracking and reporting.',
     true, true,
     'Concrete and verifiable, unlike generic "technology-driven" language.',
     'Salesforce is a third-party trademark. Describe it as the platform in '
     'use. Never imply a partnership, endorsement, certification or '
     'co-branding relationship, and never use Salesforce logos or marks.'),

    ('Two-week training programme',
     'two-week-training',
     'Mandatory two-week hands-on training programme using real leads, '
     'covering the full sales process, carrier systems and agency '
     'management.',
     true, true,
     'The answer to the strongest objection from career-changers: that they '
     'do not know insurance. "Real leads" is the specific worth keeping.',
     'State it as mandatory and two weeks. Do not describe it as ongoing, '
     'unlimited or certified -- none of those appear in source copy.'),

    ('Candidate financing',
     'candidate-financing',
     'Financing options are available for qualified candidates, structured '
     'case by case.',
     true, true,
     'Directly addresses the fee objection, which is why the qualifier '
     'matters so much.',
     'Never drop "for qualified candidates" -- without it this reads as an '
     'offer of credit to anyone. Never state a rate, term, deposit or '
     'amount: none is published anywhere in the corpus.'),

    ('Direct carrier and MGA access',
     'direct-carrier-access',
     'Renegade holds direct appointments with regional and national carriers '
     'and MGAs across personal lines, commercial lines and specialty '
     'products, giving owners market access from day one.',
     true, true,
     '"From day one" is the useful part: a new independent agency normally '
     'cannot get appointments.',
     'Never state a carrier count. The site claims quotes from "100 '
     'insurance companies" while showing 11 carrier logos -- an open '
     'conflict in kb.conflicts. Name carriers only from those 11.'),

    ('Post-launch support',
     'post-launch-support',
     'Post-launch guidance, regional expert sessions, marketing tools and '
     'webinars.',
     true, true,
     'Weaker and vaguer than the rest; useful as a supporting benefit, not '
     'as a headline.',
     null),

    ('Franchise Disclosure Document',
     'franchise-disclosure-document',
     'The FDD is provided at the demo-and-decision stage, before any '
     'agreement is signed, so all terms can be reviewed first.',
     true, true,
     'Worth marketing as transparency, and separately required: see the '
     'blocker rule in 012 governing franchise offers.',
     'Never characterise marketing material as an offer. The offer can only '
     'be made through the FDD.'),

    ('Territory availability',
     'territory-availability',
     'Territory availability is reviewed with a Franchise Success Specialist '
     'during the application conversation.',
     true, false,
     'Available but NOT approved for marketing, and the gap is the point. '
     'The corpus contains no territory map, no state registration list and '
     'no exclusivity language. Exclusive or protected territory is FDD Item '
     '12; promising it in an ad before the FDD is on file is a franchise-law '
     'exposure, not a copy preference.',
     'Do not claim exclusive, protected or reserved territory. Do not imply '
     'scarcity ("only 3 territories left in your state") -- there is no '
     'source for it.')
) as v(name, slug, description, available, approved, notes, restrictions)
where b.slug = 'renegade' and p.slug = 'franchise-program'
on conflict (product_id, slug) do nothing;


-- ---------------------------------------------------------------------------
-- Renegade Agency Acquisition  (product not yet approved for marketing)
-- ---------------------------------------------------------------------------
insert into public.product_features
    (product_id, name, slug, description, status, available,
     approved_for_marketing, marketing_notes, restrictions,
     effective_from, owner, last_reviewed_at)
select p.id, v.name, v.slug, v.description, 'active', true, false,
       'Recorded from site copy. Not reviewed for marketing use -- '
       'agency-acquisition is Phase 2.',
       v.restrictions, now(), 'automate@renegadeinsurance.com', now()
from public.products p
join public.brands b on b.id = p.brand_id
cross join (values
    ('Market-based valuation',
     'market-based-valuation',
     'The offer is calculated on retention ratio, commission revenue, growth '
     'trajectory and book mix rather than a blanket multiple.',
     'Never publish a multiple, a formula or an example valuation.'),

    ('Cash at close',
     'cash-at-close',
     'Up to 90% of consideration paid as cash upfront, with no performance '
     'thresholds or earnouts.',
     'The "up to" is load-bearing. Never state 90% as the amount a seller '
     'receives. Source copy reads "Up tp 90% cash upfront" -- a typo on the '
     'live page; do not reproduce it.'),

    ('Carrier appointment continuity',
     'carrier-appointment-continuity',
     'Existing carrier appointments stay active through the transfer, with '
     'no mid-term rewrites and no coverage gaps.',
     'A continuity promise touching policyholder coverage. Do not extend it '
     'beyond appointments to guarantee premiums, terms or renewal rates.'),

    ('Staff integration',
     'staff-integration',
     'The seller''s team integrates into Renegade''s platform with defined '
     'roles and growth opportunities.',
     'Never promise employment, retention or specific roles for named '
     'staff -- the site promises a path, not a job.'),

    ('No broker fees',
     'no-broker-fees',
     'Renegade buys directly from agency owners. There are no broker fees at '
     'any stage of the transaction.',
     'Scope it to broker fees. It is not a claim that the transaction has no '
     'costs at all.'),

    ('Agency value calculator',
     'agency-value-calculator',
     'A free, instant, confidential self-serve estimate of agency value at '
     '/agency-value-calculator/.',
     'It produces an estimate, not an offer. Never describe its output as a '
     'valuation, a quote or a price.')
) as v(name, slug, description, restrictions)
where b.slug = 'renegade' and p.slug = 'agency-acquisition'
on conflict (product_id, slug) do nothing;


-- ---------------------------------------------------------------------------
-- Renegade Back Office Support  (product not yet approved for marketing)
-- ---------------------------------------------------------------------------
insert into public.product_features
    (product_id, name, slug, description, status, available,
     approved_for_marketing, marketing_notes, restrictions,
     effective_from, owner, last_reviewed_at)
select p.id, v.name, v.slug, v.description, 'active', true, false,
       'Recorded from site copy. Not reviewed for marketing use -- '
       'back-office-support is Phase 2.',
       v.restrictions, now(), 'automate@renegadeinsurance.com', now()
from public.products p
join public.brands b on b.id = p.brand_id
cross join (values
    ('Policy servicing',
     'policy-servicing',
     'Renewals, cancellations, endorsements and policy downloads handled by '
     'Renegade''s service team.',
     null),

    ('Carrier relationship management',
     'carrier-management',
     'Carrier relationships and appointments managed on the partner''s '
     'behalf.',
     null),

    ('Remarketing support',
     'remarketing',
     'Remarkets and back-end service support.',
     null),

    ('Book ownership retained',
     'book-ownership-retained',
     'Partners keep ownership of their book while Renegade handles servicing, '
     'back office and carrier access.',
     'This is the feature that separates back-office from acquisition. Any '
     'asset that blurs it is selling the wrong programme.')
) as v(name, slug, description, restrictions)
where b.slug = 'renegade' and p.slug = 'back-office-support'
on conflict (product_id, slug) do nothing;

commit;
