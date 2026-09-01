-- 019_seed_claims_agencyheight.sql
--
-- Agency Height's governed claims and brand rules.
--
-- REQUIRES 015. Every claim here is inserted with a brand_id, and two of them
-- have product_id NULL because they are facts about the company rather than
-- about the platform. Neither is expressible on the pre-015 schema -- Agency
-- Height has one product now, but "Agency Height is an agent growth platform"
-- is not a claim about that product, and filing it there would repeat exactly
-- the mistake 015 just unwound for Renegade's "48 states".
--
-- WHY THIS FILE IS 22 CLAIMS AND NOT 400
-- The corpus has 426 figure-bearing chunks. The worksheet
-- (KB/candidate-claims-agencyheight.md) surfaced 1,221 candidate figures from
-- 1,512 chunks -- roughly 26x Renegade's 46. Almost none of them belong in this
-- table, and understanding why is the whole point of this file.
--
-- public.claims governs SELF-CLAIMS: things the brand asserts about itself,
-- which a human approves, prohibits or restricts once. That model fits
-- Renegade, whose figures are about Renegade -- commission splits, licence
-- counts, location counts.
--
-- Agency Height's figures are overwhelmingly statements about the WORLD:
--
--     "commercial truck insurance might range from $1,000-$3,000"
--     "Georgia $25,000 bodily injury liability per person / $50,000 per
--      accident / $25,000 property damage"
--     "Capterra: 4.6/5 (14168 reviews) - TrustRadius: 8.2/10 (5810 reviews)"
--     "American National ... ranked among the top financial institutions for
--      trustworthiness in 2017 by Forbes Magazine"
--     a full salary and benefits review of Farmers Insurance
--
-- Those are not claims anyone can approve. Nobody at Agency Height gets to
-- decide whether Georgia requires $25,000 of bodily injury liability. They
-- fail in four ways this table has no column for:
--
--   staleness      a premium range and a 2017 Forbes ranking both rot, and
--                  nothing here expires them against a source's date_modified
--   jurisdiction   a state minimum is right for one state and wrong for 49
--   comparison     a named-competitor salary review is a legal exposure of a
--                  different kind, and trigger phrases cannot catch it because
--                  the problem is the act of comparing, not a wording
--   attribution    of 400 sampled figure-bearing chunks, 44% carry no source
--                  marker at all. An unattributed figure cannot even be checked
--
-- So the review question inverts. Renegade's was "may we say this?". Agency
-- Height's is "is this still true, and where did it come from?" -- which is a
-- citation-and-freshness problem, structurally closer to kb.conflicts than to
-- claims. That mechanism does not exist yet and is not invented here.
--
-- What IS here: the 22 genuine self-claims, and nine brand_rules that carry
-- the weight for everything else. The rules are the load-bearing half of this
-- file. A rule saying "no premium figure without a named source and an as-of
-- date" governs all 426 chunks; 426 claim rows would govern none of them,
-- because nobody would ever finish reviewing them.
--
-- Re-runnable: `on conflict (brand_id, product_id, claim_text) do nothing`,
-- using the natural key 015 added. This is the first seed file in the project
-- that can use a real upsert instead of `where not exists`.

begin;

-- ---------------------------------------------------------------------------
-- Claims
--
-- Prices are quoted exactly as the homepage plans table renders them, monthly
-- and annual, because the deterministic price-consistency check compares
-- generated copy against these strings.
-- ---------------------------------------------------------------------------
insert into public.claims
    (brand_id, product_id, claim_text, status, category, restriction_notes,
     approved_wording, source, source_url, requires_disclaimer,
     disclaimer_text, trigger_phrases, owner, last_reviewed_at)
select b.id,
       case when v.product_slug is null then null else p.id end,
       v.claim_text, v.status, v.category, v.restriction_notes,
       v.approved_wording, 'agencyheight.com', v.source_url,
       v.requires_disclaimer, v.disclaimer_text, v.triggers,
       'automate@renegadeinsurance.com', now()
from public.brands b
-- The VALUES list is joined before the left join that references it: a
-- v.product_slug in an ON clause that precedes v is an invalid FROM-clause
-- reference. Same ordering bug 012 hit.
cross join (values
    -- ==================== APPROVED: pricing ============================
    ('agent-platform'::text,
     'The Basic plan is $0 per month and includes full Markets carrier '
     'access, an agent directory profile, and full CRM pipeline tracking.',
     'approved'::text, 'pricing'::text,
     null::text, null::text,
     'https://agencyheight.com/'::text,
     false, null::text, '{}'::text[]),

    ('agent-platform',
     'The Basic plan includes up to 2 lead detail views per month.',
     'approved', 'pricing', null, null,
     'https://agencyheight.com/', false, null, '{}'),

    ('agent-platform',
     'The Premium plan is $39.99 per month billed monthly, or $29.99 per '
     'month billed annually at $359.88 per year.',
     'approved', 'pricing', null, null,
     'https://agencyheight.com/', false, null, '{}'),

    ('agent-platform',
     'The Enterprise plan is $99.99 per month billed monthly, or $79.99 per '
     'month billed annually at $959.88 per year.',
     'approved', 'pricing', null, null,
     'https://agencyheight.com/', false, null, '{}'),

    ('agent-platform',
     'The Premium plan can be started with a $1 trial.',
     'approved', 'pricing', null, null,
     'https://agencyheight.com/', false, null, '{}'),

    ('agent-platform',
     'Lead Bank claims are capped at 2 per month on Premium and 7 per month '
     'on Enterprise. The Lead Bank is not available on the Basic plan.',
     'approved', 'pricing',
     null,
     -- The cap is governed; the supply behind it is not. See 018's
     -- restrictions on the lead-bank feature.
     'Premium includes 2 Lead Bank claims a month and Enterprise includes 7.',
     'https://agencyheight.com/', false, null, '{}'),

    ('agent-platform',
     'The Enterprise plan includes priority support.',
     'approved', 'pricing', null, null,
     'https://agencyheight.com/', false, null, '{}'),

    -- ==================== APPROVED: how it works =======================
    ('agent-platform',
     'Markets covers admitted and E&S markets, filtered by line of business, '
     'carrier appetite and state.',
     'approved', 'technology',
     null,
     -- Deliberately the substantiable version of the site's "every admitted
     -- and E&S option", which is filed as prohibited below.
     'Markets covers admitted and E&S markets, filtered by line, appetite '
     'and state -- so you can check appetite before you call.',
     'https://agencyheight.com/', false, null, '{}'),

    ('agent-platform',
     'The CRM moves each lead from Suspect to Prospect to Customer.',
     'approved', 'technology', null, null,
     'https://agencyheight.com/', false, null, '{}'),

    ('agent-platform',
     'Intake forms are coverage-specific and bilingual, and every submission '
     'syncs to the CRM.',
     'approved', 'technology',
     null, null,
     'https://agencyheight.com/', false, null, '{}'),

    ('agent-platform',
     'Consumers searching for coverage on Agency Height are matched to agents '
     'licensed in their state and filtered by coverage type.',
     'approved', 'process',
     null,
     -- The licence match is the real differentiator and it is substantiated.
     -- "verified agents", the site's own phrasing, is restricted below.
     'Matched to agents licensed in your state, by coverage type.',
     'https://agencyheight.com/', false, null, '{}'),

    ('agent-platform',
     'The agent directory profile surfaces by license, state and coverage, '
     'and leads from it arrive in the CRM.',
     'approved', 'process', null, null,
     'https://agencyheight.com/', false, null, '{}'),

    -- ==================== BRAND-LEVEL ==================================
    -- product_id NULL. A fact about the company, and 015 is what makes it
    -- expressible at all.
    --
    -- pending_review, NOT approved, and the reason is structural. A
    -- brand-level claim has no product to gate it on, so fetch_claims passes
    -- it on brand alone -- an approved row here would bypass
    -- agent-platform's approved_for_marketing = false and become the one
    -- assertable Agency Height claim in the system. Nobody at Agency Height
    -- has reviewed a line of this file, so nothing in it should be
    -- assertable. pending_review is excluded from retrieval entirely, which
    -- is the correct state for an undecided claim: it carries no instruction
    -- either way.
    --
    -- This is the whole file's gate in one row. Approve it, and the brand's
    -- positioning becomes usable; leave it, and Agency Height copy has no
    -- approved foundation to build on. Deliberately a person's call.
    (null,
     'Agency Height is an insurance market access and agent growth platform '
     'for independent insurance agents and agencies.',
     'pending_review', 'positioning', null, null,
     'https://agencyheight.com/', false, null, '{}'),

    -- ==================== PROHIBITED ===================================
    ('agent-platform',
     'Markets covers every admitted and E&S option.',
     'prohibited', 'technology',
     'An absolute completeness claim about a carrier universe that no one can '
     'substantiate, and it appears verbatim on the homepage. One missing '
     'carrier in one state makes it false. Use the approved wording, which '
     'says what Markets does without claiming exhaustiveness.',
     'Markets covers admitted and E&S markets, filtered by line, appetite '
     'and state.',
     'https://agencyheight.com/', false, null,
     array['every admitted and e&s', 'all admitted and e&s',
           'every carrier', 'all carriers', 'every market',
           'complete carrier list', 'exhaustive carrier']),

    ('agent-platform',
     'Agents get 2 to 3 new leads a day.',
     'prohibited', 'testimonial',
     'This is one named agent''s testimonial figure ("I have seen, on '
     'average, 2-3 new leads on a daily basis") converted into a platform '
     'claim. Converting a testimonial into a company claim is a blocker rule '
     'in its own right. There is no substantiated lead frequency anywhere in '
     'the corpus.',
     null,
     'https://agencyheight.com/', false, null,
     array['2-3 new leads', '2 to 3 new leads', '2-3 leads',
           'new leads on a daily basis', 'leads every day',
           'daily leads', 'leads per day']),

    ('agent-platform',
     'Agency Height guarantees leads.',
     'prohibited', 'positioning',
     'No guaranteed-outcome claim may be made about the Lead Bank, the '
     'directory, or the platform. The Lead Bank has no stated pool size or '
     'refresh rate, and lead supply depends on consumer demand nobody '
     'controls.',
     null,
     'https://agencyheight.com/', false, null,
     array['guaranteed leads', 'guarantee leads', 'guaranteed income',
           'guaranteed results', 'guaranteed clients', 'we guarantee',
           'guaranteed to grow']),

    ('agent-platform',
     'The Recommended badge means Agency Height recommends the agent.',
     'prohibited', 'positioning',
     'The badge is included with the paid plans. Describing a purchased '
     'directory placement as a recommendation, endorsement or quality signal '
     'describes it as something it is not, and undisclosed pay-for-placement '
     'is the specific exposure.',
     null,
     'https://agencyheight.com/', false, null,
     array['recommended by agency height', 'agency height recommends',
           'we recommend this agent', 'endorsed by agency height',
           'recommended agent badge']),

    -- ==================== RESTRICTED ===================================
    ('agent-platform',
     'Markets and the CRM are free on every plan, forever.',
     'restricted', 'pricing',
     'Usable only scoped to those two tools and only as the site states it. '
     '"Forever" is a permanent pricing commitment, so it must never be '
     'generalised: "Agency Height is free forever" and "the platform is free '
     'forever" are both false, because leads, the Lead Bank, the website and '
     'the badge are all paid. If in doubt use "$0 to start", which is also on '
     'the homepage and commits to nothing.',
     'Markets and the CRM are free on every plan.',
     'https://agencyheight.com/', false, null,
     array['free forever', 'forever free', 'free for life',
           'always be free', 'always free', 'platform is free',
           'agency height is free']),

    ('agent-platform',
     'The Agency Website goes live within a week.',
     'restricted', 'timeline',
     'A delivery commitment with no stated conditions or exclusions. Usable '
     'only with a qualifier and only if the dependency on the agent supplying '
     'their NPN and details is stated. Never as a flat promise, and never in '
     'a paid ad where the qualifier will be trimmed for length.',
     'Your site is AI-built and SEO-optimised, typically live within a week '
     'once your details are in.',
     'https://agencyheight.com/', true,
     'Timing depends on how quickly your details and NPN are provided.',
     array['live within a week', 'live in a week', 'website in a week',
           'ready in a week', 'live in 7 days', 'up in a week']),

    ('agent-platform',
     'The Agency Website is included with the Premium plan.',
     'restricted', 'pricing',
     'THE SOURCE COPY CONTRADICTS ITSELF. The homepage plans table lists '
     '"Agency Website" inside Premium''s feature list, and then lists '
     'Enterprise as "Everything in Premium" followed by "Agency Website" -- '
     'which implies Premium does not include it. Both renderings are on the '
     'same page, most likely the monthly and yearly tabs flattened into one '
     'scrape. Not resolvable from the corpus. Do not state which plan '
     'includes the website until Agency Height confirms it.',
     null,
     'https://agencyheight.com/', false, null,
     array['agency website is included with premium',
           'premium includes agency website',
           'premium includes a website', 'premium plan website',
           'get a website with premium']),

    ('agent-platform',
     'Agency Height routes leads to verified agents.',
     'restricted', 'process',
     'The site uses "verified agents" without stating anywhere what '
     'verification consists of. Usable only if the criteria are stated in the '
     'same asset. The state-licence match is substantiated and says the true '
     'part without borrowing credibility from an unexplained process, so '
     'prefer the approved wording.',
     'Matched to agents licensed in their state, by coverage type.',
     'https://agencyheight.com/', false, null,
     array['verified agents', 'verified agent', 'vetted agents',
           'vetted agent', 'agent verification']),

    ('agent-platform',
     'Matthew Roman, Russel L Armine and Ceranus Lejulus have given '
     'five-star testimonials about Agency Height.',
     'restricted', 'testimonial',
     'Testimonials may be used only verbatim, attributed exactly as the '
     'homepage attributes them, and with a results-vary qualifier. Never '
     'average them, never aggregate them, never edit a quote to strengthen '
     'it, and never convert a figure inside one into a platform claim -- the '
     '"2-3 new leads" figure is separately prohibited above.',
     null,
     'https://agencyheight.com/', true,
     'Individual results vary.',
     array['matthew roman', 'russel l armine', 'russel armine',
           'ceranus lejulus']),

    (null,
     'Independent agents and growing agencies use Agency Height to write '
     'more business.',
     'restricted', 'positioning',
     'The homepage''s own framing above the testimonials, and an outcome '
     'claim: "write more business" asserts a result the corpus does not '
     'substantiate with any figure. Usable as positioning about what the '
     'platform is for, never as a statement of what it achieves. "Independent '
     'agents use Agency Height to find markets and work leads" describes the '
     'product; "to write more business" describes a result.',
     null,
     'https://agencyheight.com/', false, null,
     array['write more business', 'writes more business',
           'more business than', 'grow your revenue by'])
) as v(product_slug, claim_text, status, category, restriction_notes,
       approved_wording, source_url, requires_disclaimer, disclaimer_text,
       triggers)
left join public.products p
       on p.brand_id = b.id and p.slug = v.product_slug
where b.slug = 'agencyheight'
  -- A named product that does not exist must not silently become a
  -- brand-level claim. 017 has to be applied first.
  and (v.product_slug is null or p.id is not null)
on conflict (brand_id, product_id, claim_text) do nothing;


-- ---------------------------------------------------------------------------
-- Brand rules
--
-- The load-bearing half. Nine rules, and six of them are blockers, because the
-- risks in this corpus are not brand-voice risks -- they are unlicensed
-- advice, unattributed pricing, and comparative claims about named companies.
--
-- Rules 1 to 4 exist because of the 44% of figure-bearing chunks that carry no
-- attribution marker. A claim row cannot govern those; a rule can.
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
cross join (values
    (null::text, 'legal'::text,
     'Never state an insurance premium, cost range, or savings figure without '
     'naming its source and an as-of date in the same asset. This is the '
     'single largest risk in the Agency Height corpus: of 400 sampled '
     'figure-bearing chunks, 44% carry no attribution marker at all. A figure '
     'with a bad citation can be corrected; a figure with no citation cannot '
     'even be checked. Applies to every dollar amount about the insurance '
     'market, including ranges and averages.',
     'blocker'::text,
     array['email', 'meta_ads', 'google_ads', 'landing_page', 'sms']::text[],
     'Short-term commercial truck policies ran $1,000-$3,000 for standard '
     'operations with clean records (Agency Height analysis, 2026). Your '
     'quote will differ.',
     'Short-term truck insurance costs $1,000-$3,000.'),

    (null, 'legal',
     'State-specific insurance requirements must name the state and the '
     'as-of date, and must never be presented as national or as advice. A '
     'minimum-coverage figure is correct for one state and wrong for the '
     'other 49, and the corpus is full of them -- the commercial auto and '
     'trucking by-state pages carry a per-state limit table each. An asset '
     'that lifts one row without its state is simply false everywhere else.',
     'blocker',
     array['email', 'meta_ads', 'google_ads', 'landing_page'],
     'Georgia required $25,000 bodily injury liability per person as of '
     'January 2026. Requirements differ by state and change.',
     'You need at least $25,000 in bodily injury liability.'),

    (null, 'legal',
     'Never make a comparative claim about a named carrier, competitor, or '
     'their compensation, products, or employees. The corpus contains full '
     'salary and benefits reviews of named insurers, including side-by-side '
     'earnings tables. Republishing any of that as marketing turns editorial '
     'research into a disparagement and unfair-competition exposure. Naming a '
     'carrier factually as a market Agency Height covers is fine; ranking, '
     'rating, or comparing one against another is not.',
     'blocker',
     array['email', 'meta_ads', 'google_ads', 'landing_page', 'sms'],
     'Markets covers admitted and E&S carriers across all major lines.',
     'Farmers agents average $65K. Independent agents on Agency Height do '
     'better.'),

    (null, 'legal',
     'Never give insurance advice. Do not recommend a specific coverage, '
     'limit, or deductible, do not say a coverage is right or sufficient for '
     'the reader, and do not tell anyone what they should carry. Agency '
     'Height is a directory and platform, not the agent of record, and '
     'coverage recommendations are the licensed agent''s job. This is the '
     'rule the calculators come closest to breaking -- an estimate is not a '
     'recommendation and must not be worded as one.',
     'blocker',
     array['email', 'meta_ads', 'google_ads', 'landing_page'],
     'Estimate your likely premium range, then talk to a licensed agent about '
     'the coverage that fits.',
     'You should carry $100,000 in liability -- it is the right level for '
     'most homeowners.'),

    ('agent-platform', 'messaging',
     'One asset, one offer tier. Never mix free-tier acquisition with '
     'paid-plan features. An ad that leads with "$0 to start" and then names '
     'the Lead Bank, unlimited lead details, the Recommended badge or the '
     'Agency Website is selling a paid feature under a free headline. Check '
     'the campaign type''s prohibited_themes: agent-acquisition prohibits '
     'every paid feature, and plan-upgrade prohibits the free-tier framing. '
     'This is sharper than it looks -- both campaign types address the same '
     'person at different moments, so the mistake reads as plausible rather '
     'than absurd.',
     'blocker',
     array['email', 'meta_ads', 'google_ads', 'landing_page'],
     'Get found by consumers searching your state and coverage. Free '
     'directory profile, full Markets access, no card required.',
     'Start free and claim 7 leads a month from the Lead Bank.'),

    ('agent-platform', 'claims',
     'No guaranteed outcomes. Never promise or imply a number of leads, a '
     'lead frequency, an income, a conversion rate, or growth. The Lead Bank '
     'has no stated pool size or refresh rate anywhere in the corpus, and '
     'lead supply depends on consumer demand nobody controls. The monthly '
     'claim CAP (2 on Premium, 7 on Enterprise) is a governed figure and may '
     'be stated; the supply behind it may not.',
     'blocker',
     array['email', 'meta_ads', 'google_ads', 'landing_page', 'sms'],
     'Premium includes 2 Lead Bank claims a month, filterable by state and '
     'coverage.',
     'Premium agents see 2-3 new leads a day.'),

    (null, 'sourcing',
     'Third-party review scores must carry the platform, the score, the '
     'review count where the source gives one, and the date the score was '
     'captured. Scores drift continuously -- the corpus holds "Capterra: '
     '4.6/5 (14168 reviews)" and "TrustRadius: 8.2/10 (5810 reviews)", both '
     'true only on the day they were scraped. A score without a capture date '
     'is a figure that quietly becomes wrong.',
     'warning',
     array['email', 'meta_ads', 'google_ads', 'landing_page'],
     'Rated 4.6/5 on Capterra from 14,168 reviews (as of February 2026).',
     'Rated 4.6/5 by users.'),

    (null, 'testimonials',
     'Testimonials verbatim, attributed exactly as the source attributes '
     'them, with a results-vary qualifier. Never average or aggregate them, '
     'never edit a quote to strengthen it, and never convert a figure inside '
     'a testimonial into a platform claim. The homepage carries three named '
     'five-star testimonials and one of them contains a lead-frequency '
     'figure, which is recorded as a prohibited claim precisely because it '
     'reads like a statistic.',
     'warning',
     array['email', 'meta_ads', 'google_ads', 'landing_page'],
     '"Agency Height Markets helps me narrow down which carriers may be '
     'worth contacting." -- Russel L Armine, Insurance Agent. Individual '
     'results vary.',
     'Agents report 2-3 new leads a day.'),

    (null, 'positioning',
     'Editorial content is not an offer, and the two must stay '
     'distinguishable. Guides, comparisons, calculators and career reviews '
     'exist to be useful; they must not read as an inducement to sign up, and '
     'a platform CTA inside one must be visibly separate from the editorial '
     'claim it sits next to. The corpus already blurs this -- the Farmers '
     'careers review has "Grow Your Agency Faster with Agency Height '
     'Insurance Directory" spliced mid-article, three times.',
     'info',
     array['landing_page', 'email'],
     'A clearly delimited "Join the Agency Height directory" block at the end '
     'of the guide.',
     'A sentence in the middle of a salary table that pivots into a signup '
     'pitch.')
) as v(product_slug, category, rule_text, severity, channels,
       good_example, bad_example)
left join public.products p
       on p.brand_id = b.id and p.slug = v.product_slug
where b.slug = 'agencyheight'
  and (v.product_slug is null or p.id is not null)
  and not exists (
      select 1 from public.brand_rules r
       where r.brand_id = b.id and r.rule_text = v.rule_text);

commit;
