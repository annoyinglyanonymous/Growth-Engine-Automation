-- 017_seed_agencyheight_products.sql
--
-- Agency Height's taxonomy. Requires 015 (brand-scoped claims) only in spirit:
-- this migration touches products and campaign_types, both of which have
-- always been brand-scoped. 018 and 019 are the files that need 015.
--
-- WHAT AGENCY HEIGHT ACTUALLY SELLS
-- The Phase 1 plan called the target "Agency Height Profile", describing a
-- directory listing. The live homepage is a different business:
--
--     "Insurance Market Access & Agent Growth Platform"
--     "Find more markets. Place more risks."
--     "Carrier search and live buyer leads, plus the CRM, website, and intake
--      tools to work them. One login, $0 to start."
--
-- The directory profile still exists, and it is still the free entry point
-- promoted across ~700 editorial pages ("Join our network of successful agents
-- and start getting quality leads"). But it is one tool inside a freemium
-- platform, not the product. Seeding it as the product would have pointed
-- every Agency Height campaign at the wrong offer.
--
-- WHY ONE PRODUCT AND NOT THREE
-- Markets, the Agent Directory, the CRM, Intake Forms, the Lead Bank, the
-- AI-built Website and the mobile app are tiers of one subscription, not
-- separate purchases -- "Markets and the CRM are free on every plan, forever",
-- and everything else is gated by plan. Splitting them into products would
-- invent a boundary the business does not have and immediately raise an
-- unanswerable question: which product owns Markets, when it is free on all of
-- them? So one product, with the tier detail carried by product_features (018)
-- and the free-versus-paid distinction enforced by campaign types below.
--
-- Agency Acquisition is deliberately NOT a product here. The Agency Value
-- Calculator page names it as an external destination ("trusted marketplaces
-- such as Agency Acquisition"), so on this corpus it is a third party. Renegade
-- already has its own agency-acquisition product from 010; conflating them
-- would put one brand's claims behind the other's gate.
--
-- CORRECTING 010'S ESTIMATE
-- 010's header says Agency Height is "1,529 chunks with 983 distinct figures,
-- most of them third-party market statistics". Measured against the live
-- corpus: 712 documents, 1,532 chunks, 426 figure-bearing chunks. And "most of
-- them third-party" is roughly a third true -- of 400 sampled figure-bearing
-- chunks, 32% carry an explicit attribution marker, 33% are self-referential,
-- and 44% carry neither. That last group is the real problem and it drives the
-- rules in 018: the risk here is not the unapproved brag, it is the
-- unattributed fact. 010 is applied and left as written; this supersedes it.
--
-- WHY approved_for_marketing IS FALSE
-- Same reason 010 gave agency-acquisition and back-office-support the same
-- flag: nobody at Agency Height has reviewed a line of this. The product gate
-- in kb_context.fetch_claims means an approved claim on an ungated product is
-- not approved for anything, so this content is staged and inert until a human
-- turns it on. Flipping it is one statement, and it is a person's decision to
-- make, not this migration's:
--
--     update public.products set approved_for_marketing = true,
--            last_reviewed_at = now(), owner = '<the reviewer>'
--      where slug = 'agent-platform'
--        and brand_id = (select id from public.brands
--                         where slug = 'agencyheight');
--
-- Re-runnable via on conflict (brand_id, slug).

begin;

-- ---------------------------------------------------------------------------
-- Product
--
-- pricing_summary is quoted from the homepage plans table rather than
-- paraphrased, because the deterministic price-consistency check compares
-- generated copy against it. A paraphrase would make the check disagree with
-- the site.
--
-- The two annual figures are the discounted per-month rates the page shows
-- under "YEARLY (20% OFF)"; both the monthly and annual numbers are listed
-- because copy legitimately uses either, and a check that only knew one would
-- flag the other as a price inconsistency.
-- ---------------------------------------------------------------------------
insert into public.products
    (brand_id, name, slug, description, status, approved_for_marketing,
     pricing_summary, primary_cta, owner, last_reviewed_at)
select b.id, v.name, v.slug, v.description, 'active', v.approved,
       v.pricing, v.cta, 'automate@renegadeinsurance.com', now()
from public.brands b
cross join (values
    ('Agency Height Agent Growth Platform',
     'agent-platform',
     'Freemium platform for independent insurance agents and agencies. '
     'Markets carrier search across admitted and E&S options filtered by '
     'line, appetite and state; an agent directory profile that surfaces by '
     'license, state and coverage; a CRM that moves leads from Suspect to '
     'Prospect to Customer; coverage-specific bilingual intake forms; a Lead '
     'Bank of unclaimed leads on paid plans; and an AI-built SEO website. '
     'Consumers searching for coverage on Agency Height are routed to '
     'verified agents licensed in their state.',
     false,
     'Three plans. Basic $0/mo, free forever: Markets full carrier access, '
     'agent directory profile, CRM full pipeline tracking, inbound leads via '
     'profile and intake form, up to 2 lead detail views per month. Premium '
     '$39.99/mo billed monthly or $29.99/mo billed annually ($359.88/year), '
     'with a $1 trial: adds unlimited full lead details, Lead Bank 2 claims '
     'per month, and a Recommended badge in the directory. Enterprise $99.99'
     '/mo billed monthly or $79.99/mo billed annually ($959.88/year): adds '
     'Agency Website, Lead Bank 7 claims per month, and priority support.',
     'Get Started Now')
) as v(name, slug, description, approved, pricing, cta)
where b.slug = 'agencyheight'
on conflict (brand_id, slug) do nothing;


-- ---------------------------------------------------------------------------
-- Campaign types
--
-- Three jobs that must not be mixed in one asset:
--   agent-acquisition  get an agent who has never heard of us to sign up free
--   plan-upgrade       get an existing free user onto Premium or Enterprise
--   content-marketing  the editorial/SEO engine that feeds the other two
--
-- The mixing risk here is sharper than Renegade's. Renegade's three programmes
-- address three different people. Agency Height's first two address the SAME
-- person at different moments, and the failure is subtle rather than absurd: an
-- acquisition ad that leads with "$0 to start" and then quotes a Lead Bank
-- limit is selling a paid feature under a free headline. That is not an
-- awkward ad, it is a bait-and-switch.
--
-- content-marketing is a campaign type rather than an afterthought because
-- 426 figure-bearing chunks live there and its prohibitions are the ones that
-- carry legal weight -- premium quotes, state minimums, and named-competitor
-- comparisons.
-- ---------------------------------------------------------------------------
insert into public.campaign_types
    (brand_id, name, slug, description, allowed_themes, prohibited_themes,
     default_objective, default_cta, status, owner, last_reviewed_at)
select b.id, v.name, v.slug, v.description,
       v.allowed, v.prohibited, v.objective, v.cta,
       'active', 'automate@renegadeinsurance.com', now()
from public.brands b
cross join (values
    ('Agent Acquisition',
     'agent-acquisition',
     'Recruiting independent insurance agents and small agencies onto the '
     'free Basic plan. The offer is the free tier: a directory profile, '
     'Markets carrier search and the CRM at no cost.',
     -- NOT 'free forever'. The site says "Markets and the CRM are free on
     -- every plan, forever", and 019 files that as a RESTRICTED claim: usable
     -- scoped to those two tools, never generalised to the platform. Allowing
     -- the bare theme here would green-light the unscoped version, which is
     -- the governance layer contradicting itself -- the same class of bug as
     -- the missing product gate in fetch_claims.
     array[
        'free to start', '$0 to start', 'no credit card',
        'markets and the crm are free',
        'carrier search', 'find more markets', 'place more risks',
        'admitted and e&s', 'carrier appetite', 'agent directory profile',
        'get found by consumers', 'inbound leads', 'intake forms',
        'crm for insurance agents', 'one login', 'independent agent',
        'licensed in your state', 'coverage-specific leads'
     ],
     -- Everything here is a paid-plan feature or an upgrade motion. Naming any
     -- of them under a free-tier headline is the bait-and-switch above.
     array[
        'lead bank', 'claims per month', 'claim leads', 'unlimited lead details',
        'recommended badge', 'agency website', 'priority support',
        'upgrade to premium', 'upgrade to enterprise', 'start $1 trial',
        'billed annually', 'per month', '$39.99', '$29.99', '$99.99', '$79.99'
     ],
     'Free Basic plan signups',
     'Get Started Now'),

    ('Plan Upgrade',
     'plan-upgrade',
     'Moving existing Basic users onto Premium or Enterprise. The audience '
     'already has a profile and has hit the free tier limits.',
     array[
        'upgrade to premium', 'upgrade to enterprise', 'start $1 trial',
        'unlimited full lead details', 'lead bank', 'claim leads',
        'recommended badge', 'agency website', 'priority support',
        'billed annually', 'billed monthly', 'yearly 20% off',
        'up to 2 lead detail views', 'you have hit your limit',
        'scale your agency', 'more clients and more control'
     ],
     -- An upgrade ad must not re-pitch the free tier as if it were new, and
     -- must not imply the paid plan is what makes Markets or the CRM work.
     array[
        'free forever', 'free to start', '$0 to start', 'no credit card',
        'markets is free', 'crm is free', 'getting started with the essentials'
     ],
     'Basic to paid plan conversions',
     'Start $1 Trial'),

    ('Content Marketing',
     'content-marketing',
     'Editorial and SEO content aimed at agents and consumers: guides, tool '
     'comparisons, calculators, state coverage explainers and career '
     'reviews. Feeds the other two campaign types rather than selling '
     'directly.',
     array[
        'guide', 'how to', 'what is', 'step-by-step', 'comparison',
        'checklist', 'calculator', 'estimate your', 'state requirements',
        'minimum coverage', 'licensing requirements', 'agency management system',
        'quoting tools', 'starting an insurance agency'
     ],
     -- These are the four ways editorial content becomes a liability. Each is
     -- also backed by a blocker rule in 018, because a prohibited theme flags
     -- and a blocker rule stops -- and these need stopping.
     array[
        'guaranteed', 'guaranteed leads', 'guaranteed income',
        'you will earn', 'you will save', 'best insurance company',
        'cheapest coverage', 'we recommend you buy',
        'this coverage is right for you', 'you should carry',
        'better than state farm', 'better than allstate', 'beats geico'
     ],
     'Organic sessions and profile signups',
     'Join the Directory')
) as v(name, slug, description, allowed, prohibited, objective, cta)
where b.slug = 'agencyheight'
on conflict (brand_id, slug) do nothing;

commit;
