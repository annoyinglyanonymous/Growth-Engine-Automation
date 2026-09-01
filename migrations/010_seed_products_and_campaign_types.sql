-- 010_seed_products_and_campaign_types.sql
--
-- The taxonomy Phase 1 needs before any campaign can exist: what Renegade
-- sells to agents, and which campaign types may say what about it.
--
-- WHY PRODUCTS ARE HERE AND NOT IN 011
-- The plan numbered this migration "campaign_types". Products came with it
-- because both public.product_features and public.claims are product-scoped
-- (product_id NOT NULL on each), so 011 and 012 cannot run until the products
-- exist. campaigns.product_id is NOT NULL too. One taxonomy, one migration.
--
-- WHY ONLY ONE PRODUCT IS APPROVED FOR MARKETING
-- Phase 1 is Renegade Franchise. agency-acquisition and back-office-support
-- exist here because their claims are real and need somewhere governed to
-- live -- "up to 90% cash upfront" must be recorded as restricted whether or
-- not we are running M&A campaigns yet. They carry approved_for_marketing =
-- false so the validation layer blocks copy for them until someone reviews
-- them properly. That is the flag doing its job, not an oversight.
--
-- WHY AGENCY HEIGHT IS ABSENT
-- The plan says Agency Height repeats this after the Renegade pipeline is
-- proven. Its corpus is 1,529 chunks with 983 distinct figures, most of them
-- third-party market statistics rather than self-claims -- a different review
-- question. A half-researched campaign_types row for it would be worse than
-- no row, because prohibited_themes is load-bearing: the validation layer
-- treats it as authoritative.
--
-- THE THEME ARRAYS ARE MACHINE-CHECKABLE ON PURPOSE
-- allowed_themes / prohibited_themes are matched as substrings against
-- generated copy by the deterministic campaign-type-mixing check. So they are
-- lowercase phrases a copywriter would actually write, not abstract category
-- names. "cash at close" is checkable; "exit messaging" is not.
--
-- The mixing risk is real rather than theoretical: the live site cross-links
-- these three programs to each other. /sell-your-insurance-agency/ carries a
-- "Not Ready for a Full Exit? You Still Have Options" block promoting back
-- office, and /franchise/ carries "Have an Existing Book of Business? No
-- Problem". Retrieval will surface those passages for any of the three
-- campaign types, which is exactly when an ad starts selling two things.

begin;

-- ---------------------------------------------------------------------------
-- Products
--
-- franchise-program already exists from 006; the on-conflict guard makes this
-- file safe to re-run or to paste into the SQL editor twice.
-- ---------------------------------------------------------------------------
insert into public.products
    (brand_id, name, slug, description, status, approved_for_marketing,
     pricing_summary, primary_cta, owner, last_reviewed_at)
select b.id, v.name, v.slug, v.description, 'active', v.approved,
       v.pricing, v.cta, 'automate@renegadeinsurance.com', now()
from public.brands b
cross join (values
    ('Renegade Agency Acquisition',
     'agency-acquisition',
     'Direct purchase of an independent P&C agency or book of business. '
     'Market-based valuation, cash at close, no broker involvement. Renegade '
     'takes ownership of the book and integrates staff and carrier '
     'appointments.',
     false,
     'No fee to the seller. Offer is a market-based valuation calculated on '
     'retention ratio, commission revenue, growth trajectory and book mix. '
     'Up to 90% of consideration paid as cash at close.',
     'Book a Free Consultation'),

    ('Renegade Back Office Support',
     'back-office-support',
     'Servicing and operations partnership for independent P&C agency owners '
     'who keep ownership of their book. Renegade handles renewals, '
     'cancellations, endorsements, policy downloads, carrier relationships '
     'and remarketing.',
     false,
     'Not published on the site. Do not state or imply a price for this '
     'programme until a governed pricing claim exists.',
     'See How It Works')
) as v(name, slug, description, approved, pricing, cta)
where b.slug = 'renegade'
on conflict (brand_id, slug) do nothing;


-- ---------------------------------------------------------------------------
-- Campaign types
--
-- Three audiences that must never be addressed in the same asset:
--   franchise       someone who wants to START an agency
--   m-and-a         someone who wants to EXIT one
--   book-servicing  someone who wants to KEEP one and stop servicing it
--
-- A single ad that mixes "own your own agency" with "sell your agency" is not
-- a style problem, it is an unusable asset. Hence prohibited_themes.
-- ---------------------------------------------------------------------------
insert into public.campaign_types
    (brand_id, name, slug, description, allowed_themes, prohibited_themes,
     default_objective, default_cta, status, owner, last_reviewed_at)
select b.id, v.name, v.slug, v.description,
       v.allowed, v.prohibited, v.objective, v.cta,
       'active', 'automate@renegadeinsurance.com', now()
from public.brands b
cross join (values
    ('Franchise Recruitment',
     'franchise',
     'Recruiting licensed P&C agents, captive agents seeking independence, '
     'and professionals from real estate, mortgage and financial services '
     'into agency ownership through the franchise programme.',
     array[
        'agency ownership', 'own an independent agency', 'franchise ownership',
        'commission split', 'new business commission', 'renewal commission',
        'back-office support', 'operations handled for you',
        'two-week training', 'salesforce platform', 'carrier appointments',
        'career change into insurance', 'leaving a captive carrier',
        'recurring revenue', 'financing for qualified candidates',
        'territory availability', 'franchise disclosure document'
     ],
     -- Everything below belongs to m-and-a or book-servicing. A franchise ad
     -- that reaches for "cash at close" is addressing the opposite audience.
     array[
        'sell your agency', 'selling your agency', 'clean exit',
        'exit on your terms', 'cash at close', 'no earnout',
        'agency valuation', 'market-based valuation', 'book transfer',
        'we buy your book', 'no broker fees', 'walk away clean',
        'keep ownership of your book'
     ],
     'Qualified franchise applications',
     'Talk to a Franchise Success Specialist'),

    ('Agency Acquisition',
     'm-and-a',
     'Reaching independent P&C agency owners considering a full exit. '
     'Direct sale to Renegade with no broker.',
     array[
        'sell your agency', 'clean exit', 'exit on your terms',
        'market-based valuation', 'cash at close', 'no earnout',
        'no broker fees', 'carrier appointments stay intact',
        'staff has a path forward', 'clients stay with carriers they know',
        'confidential consultation', 'agency value calculator',
        'due diligence', 'book transfer'
     ],
     -- A seller is not a buyer. Franchise economics are irrelevant and
     -- actively confusing here.
     array[
        'franchise fee', 'franchise ownership', 'own your own agency',
        'commission split', '80% commission', 'start an agency',
        'two-week training', 'initial investment',
        'financing for qualified candidates', 'territory availability'
     ],
     'Booked valuation consultations',
     'Book a Free Consultation'),

    ('Back Office Partnership',
     'book-servicing',
     'Reaching independent P&C agency owners who want to keep their book but '
     'stop carrying servicing and operations.',
     array[
        'back office support', 'servicing offload', 'renewals handled',
        'endorsements handled', 'cancellations handled', 'policy downloads',
        'carrier relationships managed', 'remarketing support',
        'keep ownership of your book', 'write more business',
        'service less', 'grow book value', 'higher exit multiple later'
     ],
     -- 'higher exit multiple later' is allowed above while 'cash at close'
     -- is prohibited here, and the difference is deliberate: the site's own
     -- back-office page pitches future book value ("sell at a stronger
     -- multiple later") without selling the exit itself. Teasing the option
     -- is on-message; closing on it is a different campaign.
     array[
        'franchise fee', 'franchise ownership', 'initial investment',
        'sell your agency', 'cash at close', 'we buy your book',
        'ownership transfer', 'clean exit', 'walk away clean',
        'commission split', 'two-week training'
     ],
     'Back-office partnership enquiries',
     'See How It Works')
) as v(name, slug, description, allowed, prohibited, objective, cta)
where b.slug = 'renegade'
on conflict (brand_id, slug) do nothing;

commit;
