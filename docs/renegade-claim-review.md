# renegade — claim review worksheet

Generated from the live database by `scripts/claim_review.py`. Re-run it after any change.

31 claims · 8 brand rules · 19 product features · 3 product(s)

## What you are deciding

Two gates stand between a staged claim and a line of copy, and they are **independent**:

1. `products.agency-acquisition.approved_for_marketing` is **false** — the outer gate. While this is false, no product-scoped claim reaches a prompt no matter what its own status says.
1. `products.back-office-support.approved_for_marketing` is **false** — the outer gate. While this is false, no product-scoped claim reaches a prompt no matter what its own status says.
1. `products.franchise-program.approved_for_marketing` is **true** — the outer gate. While this is false, no product-scoped claim reaches a prompt no matter what its own status says.
2. Each claim's own `status` — the inner gate. Only `approved` is quotable as fact.

So there are two questions, not one: *is this brand ready to market at all*, and *is each individual statement true and defensible*. Answer the second first; the first is one line of SQL once you have.

Worth knowing before you read further: **18 of these claims are already `approved`** and still produce nothing, because `agency-acquisition`, `back-office-support` is not approved for marketing. That is the outer gate doing its job, and it means enabling the product is a bigger step than it looks — those claims become assertable the moment you flip it.

---

## 2. Prohibited — nothing to approve, confirm they are right

These make the wording unusable wherever it appears, and they are the load-bearing half: a prohibited claim is what stops a stale or indefensible figure reaching a customer. Read them as a list of mistakes you are choosing to prevent.

| # | category | wording that is blocked | why | source |
|---|---|---|---|---|
| 1 | `company` | Renegade is licensed in all 50 states. | False. /license-disclosures/ lists 48 states plus DC; Alaska and Hawaii are absent. Never claim all 50 states, nationwide coverage, or "every state", and never geo-target advertising to AK or HI -- see the geographic-targeting rule. Recorded pre-emptively: no page says this, but it is the sentence a generator writes when it rounds 48 up. | https://renegadeinsurance.com/license-disclosures/ |
| 2 | `company` | Renegade provides quotes from 100 insurance companies. | Open conflict in kb.conflicts (topic carrier-count): site meta claims 100 insurance companies while the homepage shows 11 carrier logos, and 11 is the verifiable set. Never state a carrier count in any channel. Say "access to leading national and regional carriers", or name carriers from the 11. | https://renegadeinsurance.com/ |
| 3 | `compensation` | Franchise owners earn 80% commission on new business, and Renegade runs the back office. | Live on /about-us/ and prohibited for what it omits, not for the figure. It drops "personal lines", which scopes the 80% -- the site says nothing about commercial lines splits -- and it drops renewals entirely, where the rate is "up to 80%", not 80%. Use the approved wording. | https://renegadeinsurance.com/about-us/ |
| 4 | `compensation` | Renegade offers industry-leading commissions. | Unsubstantiated superlative, appearing three times on the live site (/careers/ twice, /become-an-agency-owner/ once). The corpus contains no competitor benchmark of any kind, so there is nothing to lead. Replace it with the figure -- 80% on new business personal lines commissions -- which is stronger anyway. | https://renegadeinsurance.com/become-an-agency-owner/ |
| 5 | `pricing` | Initial Renegade franchise fee starts at $20,000. | Superseded. $20,000 is stale copy still published on /about-us/ and mirrored into 16 KB documents. Never use in any channel. The page itself needs correcting to $25,000. | https://renegadeinsurance.com/about-us/ |
| 6 | `testimonial` | Renegade customers save $2,056 a year. | This figure is one customer's result, quoted on /agency/porter/: "With Carly's help, we are switching over both our car and home insurance and are saving $2056.00 a year!". An individual result is not a company claim and may never be generalised, averaged or restated without attribution. Quoting the testimonial verbatim and attributed, with a results-vary qualifier, is fine -- see the testimonial-use rule. | https://renegadeinsurance.com/agency/porter/ |
| 7 | `testimonial` | Renegade lowers premiums by $400. | One customer's condo premium reduction after wind-mitigation documentation, quoted on three HIG location pages. Same reasoning as the $2,056 row: an individual result, tied to a specific coverage action, never a company claim. | https://renegadeinsurance.com/agency/port-orange/ |
| 8 | `timeline` | Most independent insurance agency franchises open within 60 to 180 days of signing. | Also live on /franchise/, and the problem is the subject: this asserts a timeline for insurance franchises Renegade does not operate. Nothing in the corpus evidences an industry-wide figure, and the Renegade-scoped version conveys the same thing about the only thing we can evidence. | https://renegadeinsurance.com/franchise/ |

---

## 3. Restricted — usable, with conditions

Allowed only in the exact approved wording, or with the disclaimer attached. What to check: is the approved wording one you would put your name to?

| # | category | claim | approved wording | disclaimer | source |
|---|---|---|---|---|---|
| 1 | `company` | Renegade holds a 4.7 out of 5 Google rating. | — | — | https://renegadeinsurance.com/ |
| 2 | `company` | Renegade Insurance is a 2026 Global Recognition Award winner. | — | — | https://renegadeinsurance.com/franchise/ |
| 3 | `company` | Renegade Insurance is a Great Place to Work Certified company. | — | — | https://renegadeinsurance.com/careers/ |
| 4 | `pricing` | Up to 90% cash upfront at close, with no earnouts. | — | — | https://renegadeinsurance.com/sell-your-insurance-agency/ |
| 5 | `timeline` | Average completion in six months. | — | — | https://renegadeinsurance.com/sell-your-insurance-agency/ |

---

## 4. Approved

Assertable as fact, subject to the product gate above.

| # | category | claim | approved wording | disclaimer | source |
|---|---|---|---|---|---|
| 1 | `company` | Renegade has 200+ agents. | Choose from 200+ agents working with multiple leading carriers. | — | https://renegadeinsurance.com/ |
| 2 | `company` | Renegade is licensed for Property and Casualty in 48 states and the District of Columbia. | Renegade is licensed for Property and Casualty insurance in 48 states and the District of Columbia. | — | https://renegadeinsurance.com/license-disclosures/ |
| 3 | `company` | Renegade operates 9 open retail agency locations across Florida, South Carolina, Georgia and Texas. | Renegade operates 9 retail agency locations across Florida, South Carolina, Georgia and Texas. | — | — |
| 4 | `compensation` | Renegade franchise owners earn 80% on new business personal lines commissions and up to 80% on renewals. | Franchise owners earn 80% on new business personal lines commissions and up to 80% on renewals. Renegade pays agencies monthly based on carrier commissions received. | — | https://renegadeinsurance.com/franchise/ |
| 5 | `positioning` | Back Office Support partners keep ownership of their book while Renegade handles servicing, back office and carrier access. | Partners retain their book while Renegade handles servicing, back office and carrier access. | — | https://renegadeinsurance.com/sell-your-insurance-agency/ |
| 6 | `positioning` | Renegade handles renewals, cancellations, endorsements and policy downloads for Back Office Support partners. | Renewals, cancellations, endorsements and policy downloads handled. Carrier relationships and appointments managed. Remarkets and back-end service support. | — | https://renegadeinsurance.com/insurance-agency-back-office-support/ |
| 7 | `positioning` | Renegade holds direct appointments with regional and national carriers and MGAs across personal lines, commercial lines and specialty products. | Renegade holds direct appointments with regional and national carriers and MGAs across personal lines, commercial lines and specialty products, giving franchise owners broad market access from day one. | — | https://renegadeinsurance.com/franchise/ |
| 8 | `positioning` | Renegade's operations team handles customer service, bookkeeping and marketing for franchise owners. | Renegade's operations team handles customer service, bookkeeping and marketing, so franchise owners focus on sales rather than operations. | — | https://renegadeinsurance.com/franchise/ |
| 9 | `pricing` | Financing is available for qualified candidates. | Financing is available for qualified candidates. | — | https://renegadeinsurance.com/franchise/ |
| 10 | `pricing` | Initial Renegade franchise fee starts at $25,000. | Initial franchise fees start at $25,000 depending on your business type. A monthly support and technology fee applies after launch. Financing is available for qualified candidates. | **required** — Additional startup costs vary by state and are detailed in the Franchise Disclosure Document. | https://renegadeinsurance.com/franchise/ |
| 11 | `pricing` | Renegade buys directly from agency owners with no broker fees at any stage of the transaction. | Renegade buys directly from agency owners. There are no broker fees at any stage of the transaction. | — | https://renegadeinsurance.com/sell-your-insurance-agency/ |
| 12 | `pricing` | The offer is calculated on retention ratio, commission revenue, growth trajectory and book mix rather than a blanket multiple. | Your offer is calculated on retention ratio, commission revenue, growth trajectory and book mix. Not a blanket multiple. | — | https://renegadeinsurance.com/sell-your-insurance-agency/ |
| 13 | `process` | Existing carrier appointments stay active through the transfer, with no mid-term rewrites and no coverage gaps. | Existing carrier appointments stay active through the transfer. No mid-term rewrites, no coverage gaps, no carrier disruption. | — | https://renegadeinsurance.com/sell-your-insurance-agency/ |
| 14 | `technology` | Franchise owners run on Salesforce with AI tools built for quoting, client tracking and reporting. | Franchise owners run on Salesforce with AI tools built for quoting, client tracking and reporting. | — | https://renegadeinsurance.com/franchise/ |
| 15 | `timeline` | Most book transfers complete in 30 to 120 days from contract signing. | Most book transfers complete in 30 to 120 days from contract signing, depending on carrier requirements and administrative procedures. | — | https://renegadeinsurance.com/sell-your-insurance-agency/ |
| 16 | `timeline` | Most Renegade franchise locations open within 60 to 180 days of signing. | Most Renegade franchise locations open within 60 to 180 days of signing. Timeline depends on licensing, location setup and local permit requirements in your state. | — | https://renegadeinsurance.com/franchise/ |
| 17 | `timeline` | The average transition to Renegade back office services completes in six months. | The average transition to Renegade back office services completes in six months. | — | https://renegadeinsurance.com/insurance-agency-back-office-support/ |
| 18 | `training` | Renegade provides a mandatory two-week hands-on training programme using real leads. | Renegade provides a mandatory two-week hands-on training programme using real leads, covering the full sales process, carrier systems and agency management. | — | https://renegadeinsurance.com/franchise/ |

---

## 5. Brand rules

Rules constrain wording regardless of what any claim says. A `blocker` fails validation outright; a `restriction` warns.

| severity | category | rule | channels |
|---|---|---|---|
| `blocker` | `geography` | Never target advertising to Alaska or Hawaii: Renegade holds no insurance licence in either state. Never drive traffic to the Edgewater FL or New Smyrna Beach FL agency pages -- both locations are flagged closed in the knowledge base while their quote pages remain live on the site. Do not treat Miami as a Renegade location: no Miami location record exists, and the Pembroke Pines page misidentifies itself as Renegade Insurance Miami with a Miami phone number. | meta_ads, google_ads, email |
| `blocker` | `legal` | Franchise marketing is not an offer to sell a franchise. The offer can only be made through a Franchise Disclosure Document provided before any agreement is signed, and no offer may be made to residents of states where the franchise is not registered or approved. Every franchise asset must therefore avoid offer and commitment language, must not imply that responding creates or leads directly to an agreement, and must not promise territory. Route to a qualifying conversation, never to a purchase. | email, meta_ads, google_ads, landing_page, sms |
| `blocker` | `licensing` | Any licensing claim must carry the Property and Casualty scope and must count the District of Columbia separately from states: "48 states and the District of Columbia". Never "all 50 states", "nationwide", or "every state" -- Alaska and Hawaii are absent from /license-disclosures/. | email, meta_ads, google_ads, landing_page |
| `blocker` | `messaging` | One asset addresses one audience. Never combine franchise recruitment (start an agency) with agency acquisition (sell your agency) or back office support (keep your agency, stop servicing it). Check the campaign type's prohibited_themes before writing. This is not hypothetical: the live site cross-links all three programmes to each other, so retrieval surfaces the wrong programme's copy for any of them. | email, meta_ads, google_ads, landing_page |
| `blocker` | `pricing` | The initial Renegade franchise fee is $25,000. Never state $20,000 -- it is stale copy still live on /about-us/. Always keep the "starting at" and "depending on business type" qualifiers; never present the fee as a flat or final price. | all |
| `blocker` | `testimonials` | Customer and seller testimonials may be used only verbatim, attributed as they are on the site, and with a results-vary qualifier. Never convert a testimonial figure into a company claim, never average or aggregate testimonials, and never edit a quote to strengthen it. The $2,056 and $400 figures in the corpus are individual results and are recorded as prohibited claims. | email, meta_ads, google_ads, landing_page |
| `info` | `claims-freshness` | Three approved company claims decay and must be re-checked before each campaign launch rather than trusted: the Google rating (4.7 out of 5, needs an as-of date), Great Place to Work certification (annual period), and the retail location count (9 open of 11 on record). The location count is the one that can be re-derived from data -- count kb.entities location records where closed is not true. | email, meta_ads, google_ads, landing_page |
| `warning` | `superlatives` | Superlatives and absolutes -- industry-leading, best-in-class, #1, fastest-growing, unlimited, guaranteed -- require a substantiating approved claim. Where a figure exists, use the figure instead: it is both defensible and more persuasive. Specifically, replace "industry-leading commissions" with "80% on new business personal lines commissions". | email, meta_ads, google_ads, landing_page |

## 6. Product features

`available` and `approved_for_marketing` are separate on purpose: a feature can exist and still not be ready to promote. Only the second lets a generator mention it.

| feature | available | promotable | notes |
|---|---|---|---|
| `back-office-operations` (Back-office operations) | yes | **yes** | Renegade's operations team handles customer service, bookkeeping and marketing so the owne |
| `candidate-financing` (Candidate financing) | yes | **yes** | Financing options are available for qualified candidates, structured case by case. |
| `commission-split` (Commission split) | yes | **yes** | Franchise owners earn 80% on new business personal lines commissions and up to 80% on rene |
| `direct-carrier-access` (Direct carrier and MGA access) | yes | **yes** | Renegade holds direct appointments with regional and national carriers and MGAs across per |
| `franchise-disclosure-document` (Franchise Disclosure Document) | yes | **yes** | The FDD is provided at the demo-and-decision stage, before any agreement is signed, so all |
| `post-launch-support` (Post-launch support) | yes | **yes** | Post-launch guidance, regional expert sessions, marketing tools and webinars. |
| `salesforce-platform` (Salesforce-based platform) | yes | **yes** | Franchise owners run on Salesforce with AI tools for quoting, client tracking and reportin |
| `two-week-training` (Two-week training programme) | yes | **yes** | Mandatory two-week hands-on training programme using real leads, covering the full sales p |
| `agency-value-calculator` (Agency value calculator) | yes | no | A free, instant, confidential self-serve estimate of agency value at /agency-value-calcula |
| `book-ownership-retained` (Book ownership retained) | yes | no | Partners keep ownership of their book while Renegade handles servicing, back office and ca |
| `carrier-appointment-continuity` (Carrier appointment continuity) | yes | no | Existing carrier appointments stay active through the transfer, with no mid-term rewrites  |
| `carrier-management` (Carrier relationship management) | yes | no | Carrier relationships and appointments managed on the partner's behalf. |
| `cash-at-close` (Cash at close) | yes | no | Up to 90% of consideration paid as cash upfront, with no performance thresholds or earnout |
| `market-based-valuation` (Market-based valuation) | yes | no | The offer is calculated on retention ratio, commission revenue, growth trajectory and book |
| `no-broker-fees` (No broker fees) | yes | no | Renegade buys directly from agency owners. There are no broker fees at any stage of the tr |
| `policy-servicing` (Policy servicing) | yes | no | Renewals, cancellations, endorsements and policy downloads handled by Renegade's service t |
| `remarketing` (Remarketing support) | yes | no | Remarkets and back-end service support. |
| `staff-integration` (Staff integration) | yes | no | The seller's team integrates into Renegade's platform with defined roles and growth opport |
| `territory-availability` (Territory availability) | yes | no | Territory availability is reviewed with a Franchise Success Specialist during the applicat |

---

## Applying your decisions

Mark this file up however suits you. Hand it back and it becomes a migration, so the decisions are recorded and reversible rather than typed into a SQL console.

```sql
-- The outer gate. Reversible, and an attestation that a human
-- reviewed the claims above.
update public.products set approved_for_marketing = true
 where slug = 'agency-acquisition';
update public.products set approved_for_marketing = true
 where slug = 'back-office-support';

-- Per claim, once each is decided:
update public.claims set status = 'approved',
       approved_wording = '<the exact wording you would sign>',
       last_reviewed_at = now(), owner = '<you>'
 where claim_text = '<claim text from the tables above>';
```

