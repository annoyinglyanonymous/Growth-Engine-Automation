-- 014_claim_trigger_phrases.sql
--
-- Make prohibited claims actually matchable.
--
-- THE HOLE THIS CLOSES, found by testing rather than by reading
-- validation.checks.check_prohibited_wording matched a prohibited claim two
-- ways: by the money figures inside it, or by its ENTIRE sentence appearing
-- verbatim. That catches "$20,000" reliably and catches nothing else.
--
-- A deliberately non-compliant test asset reading
--
--     headline:    "Industry-leading commissions"
--     description: "Licensed in all 50 states"
--
-- was blocked only because it also contained $20,000 and two prohibited
-- themes. Those two phrases on their own passed clean, despite each being a
-- prohibited claim -- because neither reproduces its claim sentence
-- ("Renegade offers industry-leading commissions.", "Renegade is licensed in
-- all 50 states.") word for word.
--
-- WHY A COLUMN AND NOT A CLEVERER MATCHER
-- The obvious alternative is fuzzy matching -- significant-word overlap
-- between the copy and the claim. It was tried and rejected: the prohibited
-- claim "Franchise owners earn 80% commission on new business, and Renegade
-- runs the back office" shares most of its significant words with the
-- APPROVED claim "Renegade franchise owners earn 80% on new business personal
-- lines commissions and up to 80% on renewals". Any threshold loose enough to
-- catch the paraphrases above also flags the approved wording as prohibited.
--
-- A false-positive blocker is worse than a missed warning: it teaches people
-- to override the checker, and an overridden checker catches nothing. So the
-- trigger phrases are written down and reviewed rather than guessed at.
--
-- HOW TO WRITE A TRIGGER PHRASE
-- Short enough to survive paraphrase, distinctive enough that no compliant
-- sentence contains it. 'all 50 states' qualifies. 'commissions' does not.
-- Matching is case-insensitive and word-boundary anchored, and any whitespace
-- run matches any other, so 'all 50 states' catches 'All  50\nStates'.
--
-- Every phrase below is checked against the approved wordings in the same
-- product by the test suite (test_trigger_phrases_never_match_approved_copy),
-- so a phrase that would block compliant copy fails CI rather than production.

begin;

alter table public.claims
    add column trigger_phrases text[] not null default '{}';

comment on column public.claims.trigger_phrases is
    'Phrases whose presence in copy means this claim was made. Matched '
    'case-insensitively on word boundaries by validation.checks. Only '
    'meaningful for status in (prohibited, restricted); ignored for approved.';

-- Partial index: the checks only ever read phrases for non-approved rows.
create index claims_trigger_phrases_idx
    on public.claims using gin (trigger_phrases)
    where status <> 'approved';


-- ---------------------------------------------------------------------------
-- Seed the eight prohibited and five restricted Renegade claims.
--
-- Keyed on claim_text because public.claims has no natural unique key and
-- these rows were created by 006 and 012. Matching on the sentence is exact
-- and therefore safe here.
-- ---------------------------------------------------------------------------
update public.claims c
set trigger_phrases = v.phrases
from (values
    -- ---- pricing ----------------------------------------------------------
    ('Initial Renegade franchise fee starts at $20,000.',
     array['$20,000', '20,000']),

    -- ---- compensation -----------------------------------------------------
    -- Deliberately NOT '80% commission': the approved claim says
    -- "80% on new business personal lines commissions", and blocking that
    -- would block the campaign's strongest legitimate line. What is
    -- prohibited is the UNSCOPED form, so the phrase targets the missing
    -- scope rather than the figure.
    ('Franchise owners earn 80% commission on new business, and Renegade '
     'runs the back office.',
     array['80% commission on new business']),

    ('Renegade offers industry-leading commissions.',
     array['industry-leading commission', 'industry leading commission',
           'industry-leading commissions', 'industry leading commissions']),

    -- ---- timeline ---------------------------------------------------------
    ('Most independent insurance agency franchises open within 60 to 180 '
     'days of signing.',
     array['most independent insurance agency franchises']),

    -- ---- licensing --------------------------------------------------------
    ('Renegade is licensed in all 50 states.',
     array['all 50 states', 'licensed nationwide', 'licensed in every state',
           'nationwide coverage', 'in all fifty states']),

    -- ---- carrier count ----------------------------------------------------
    ('Renegade provides quotes from 100 insurance companies.',
     array['100 insurance companies', '100 carriers',
           'quotes from 100 insurance']),

    -- ---- testimonial figures ---------------------------------------------
    ('Renegade customers save $2,056 a year.',
     array['$2,056', '$2056', 'save over $2,000', 'save $2,000 a year']),

    ('Renegade lowers premiums by $400.',
     array['$400.00', 'lowers premiums by $400', 'lower premiums by $400']),

    -- ---- restricted: usable only with their condition --------------------
    ('Renegade holds a 4.7 out of 5 Google rating.',
     array['4.7 out of 5', '4.7/5', '4.7 stars', 'rated 4.7']),

    ('Renegade Insurance is a Great Place to Work Certified company.',
     array['great place to work']),

    ('Renegade Insurance is a 2026 Global Recognition Award winner.',
     array['global recognition award']),

    ('Up to 90% cash upfront at close, with no earnouts.',
     array['90% cash', 'cash upfront at close']),

    ('Average completion in six months.',
     array['average completion in six months'])
) as v(claim_text, phrases)
where c.claim_text = v.claim_text;

commit;
