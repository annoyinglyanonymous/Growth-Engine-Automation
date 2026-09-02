# agencyheight — claim review worksheet

Generated from the live database by `scripts/claim_review.py`. Re-run it after any change.

23 claims · 9 brand rules · 9 product features · 1 product(s)

## What you are deciding

Two gates stand between a staged claim and a line of copy, and they are **independent**:

1. `products.agent-platform.approved_for_marketing` is **false** — the outer gate. While this is false, no product-scoped claim reaches a prompt no matter what its own status says.
2. Each claim's own `status` — the inner gate. Only `approved` is quotable as fact.

So there are two questions, not one: *is this brand ready to market at all*, and *is each individual statement true and defensible*. Answer the second first; the first is one line of SQL once you have.

Worth knowing before you read further: **12 of these claims are already `approved`** and still produce nothing, because `agent-platform` is not approved for marketing. That is the outer gate doing its job, and it means enabling the product is a bigger step than it looks — those claims become assertable the moment you flip it.

---

## 1. Undecided

> **Agency Height is an insurance market access and agent growth platform for independent insurance agents and agencies.**
>
> category `positioning` · scope **brand-level — no product to gate on** · source https://agencyheight.com/

One of these is brand-level, which matters structurally rather than editorially. A brand-level claim has `product_id IS NULL`, so there is no product to gate it on and `fetch_claims` passes it on brand alone. Approving it **bypasses the product gate entirely** — the outer gate above stops applying to it. That is why it was staged as `pending_review` rather than `approved`. Approve it only if you are content for it to be assertable immediately.

---

## 2. Prohibited — nothing to approve, confirm they are right

These make the wording unusable wherever it appears, and they are the load-bearing half: a prohibited claim is what stops a stale or indefensible figure reaching a customer. Read them as a list of mistakes you are choosing to prevent.

| # | category | wording that is blocked | why | source |
|---|---|---|---|---|
| 1 | `positioning` | Agency Height guarantees leads. | No guaranteed-outcome claim may be made about the Lead Bank, the directory, or the platform. The Lead Bank has no stated pool size or refresh rate, and lead supply depends on consumer demand nobody controls. | https://agencyheight.com/ |
| 2 | `positioning` | The Recommended badge means Agency Height recommends the agent. | The badge is included with the paid plans. Describing a purchased directory placement as a recommendation, endorsement or quality signal describes it as something it is not, and undisclosed pay-for-placement is the specific exposure. | https://agencyheight.com/ |
| 3 | `technology` | Markets covers every admitted and E&S option. | An absolute completeness claim about a carrier universe that no one can substantiate, and it appears verbatim on the homepage. One missing carrier in one state makes it false. Use the approved wording, which says what Markets does without claiming exhaustiveness. | https://agencyheight.com/ |
| 4 | `testimonial` | Agents get 2 to 3 new leads a day. | This is one named agent's testimonial figure ("I have seen, on average, 2-3 new leads on a daily basis") converted into a platform claim. Converting a testimonial into a company claim is a blocker rule in its own right. There is no substantiated lead frequency anywhere in the corpus. | https://agencyheight.com/ |

---

## 3. Restricted — usable, with conditions

Allowed only in the exact approved wording, or with the disclaimer attached. What to check: is the approved wording one you would put your name to?

| # | category | claim | approved wording | disclaimer | source |
|---|---|---|---|---|---|
| 1 | `positioning` | Independent agents and growing agencies use Agency Height to write more business. | — | — | https://agencyheight.com/ |
| 2 | `pricing` | Markets and the CRM are free on every plan, forever. | Markets and the CRM are free on every plan. | — | https://agencyheight.com/ |
| 3 | `pricing` | The Agency Website is included with the Premium plan. | — | — | https://agencyheight.com/ |
| 4 | `process` | Agency Height routes leads to verified agents. | Matched to agents licensed in their state, by coverage type. | — | https://agencyheight.com/ |
| 5 | `testimonial` | Matthew Roman, Russel L Armine and Ceranus Lejulus have given five-star testimonials about Agency Height. | — | **required** — Individual results vary. | https://agencyheight.com/ |
| 6 | `timeline` | The Agency Website goes live within a week. | Your site is AI-built and SEO-optimised, typically live within a week once your details are in. | **required** — Timing depends on how quickly your details and NPN are provided. | https://agencyheight.com/ |

---

## 4. Approved

Assertable as fact, subject to the product gate above.

| # | category | claim | approved wording | disclaimer | source |
|---|---|---|---|---|---|
| 1 | `pricing` | Lead Bank claims are capped at 2 per month on Premium and 7 per month on Enterprise. The Lead Bank is not available on the Basic plan. | Premium includes 2 Lead Bank claims a month and Enterprise includes 7. | — | https://agencyheight.com/ |
| 2 | `pricing` | The Basic plan includes up to 2 lead detail views per month. | — | — | https://agencyheight.com/ |
| 3 | `pricing` | The Basic plan is $0 per month and includes full Markets carrier access, an agent directory profile, and full CRM pipeline tracking. | — | — | https://agencyheight.com/ |
| 4 | `pricing` | The Enterprise plan includes priority support. | — | — | https://agencyheight.com/ |
| 5 | `pricing` | The Enterprise plan is $99.99 per month billed monthly, or $79.99 per month billed annually at $959.88 per year. | — | — | https://agencyheight.com/ |
| 6 | `pricing` | The Premium plan can be started with a $1 trial. | — | — | https://agencyheight.com/ |
| 7 | `pricing` | The Premium plan is $39.99 per month billed monthly, or $29.99 per month billed annually at $359.88 per year. | — | — | https://agencyheight.com/ |
| 8 | `process` | Consumers searching for coverage on Agency Height are matched to agents licensed in their state and filtered by coverage type. | Matched to agents licensed in your state, by coverage type. | — | https://agencyheight.com/ |
| 9 | `process` | The agent directory profile surfaces by license, state and coverage, and leads from it arrive in the CRM. | — | — | https://agencyheight.com/ |
| 10 | `technology` | Intake forms are coverage-specific and bilingual, and every submission syncs to the CRM. | — | — | https://agencyheight.com/ |
| 11 | `technology` | Markets covers admitted and E&S markets, filtered by line of business, carrier appetite and state. | Markets covers admitted and E&S markets, filtered by line, appetite and state -- so you can check appetite before you call. | — | https://agencyheight.com/ |
| 12 | `technology` | The CRM moves each lead from Suspect to Prospect to Customer. | — | — | https://agencyheight.com/ |

---

## 5. Brand rules

Rules constrain wording regardless of what any claim says. A `blocker` fails validation outright; a `restriction` warns.

| severity | category | rule | channels |
|---|---|---|---|
| `blocker` | `claims` | No guaranteed outcomes. Never promise or imply a number of leads, a lead frequency, an income, a conversion rate, or growth. The Lead Bank has no stated pool size or refresh rate anywhere in the corpus, and lead supply depends on consumer demand nobody controls. The monthly claim CAP (2 on Premium, 7 on Enterprise) is a governed figure and may be stated; the supply behind it may not. | email, meta_ads, google_ads, landing_page, sms |
| `blocker` | `legal` | Never state an insurance premium, cost range, or savings figure without naming its source and an as-of date in the same asset. This is the single largest risk in the Agency Height corpus: of 400 sampled figure-bearing chunks, 44% carry no attribution marker at all. A figure with a bad citation can be corrected; a figure with no citation cannot even be checked. Applies to every dollar amount about the insurance market, including ranges and averages. | email, meta_ads, google_ads, landing_page, sms |
| `blocker` | `legal` | Never give insurance advice. Do not recommend a specific coverage, limit, or deductible, do not say a coverage is right or sufficient for the reader, and do not tell anyone what they should carry. Agency Height is a directory and platform, not the agent of record, and coverage recommendations are the licensed agent's job. This is the rule the calculators come closest to breaking -- an estimate is not a recommendation and must not be worded as one. | email, meta_ads, google_ads, landing_page |
| `blocker` | `legal` | Never make a comparative claim about a named carrier, competitor, or their compensation, products, or employees. The corpus contains full salary and benefits reviews of named insurers, including side-by-side earnings tables. Republishing any of that as marketing turns editorial research into a disparagement and unfair-competition exposure. Naming a carrier factually as a market Agency Height covers is fine; ranking, rating, or comparing one against another is not. | email, meta_ads, google_ads, landing_page, sms |
| `blocker` | `legal` | State-specific insurance requirements must name the state and the as-of date, and must never be presented as national or as advice. A minimum-coverage figure is correct for one state and wrong for the other 49, and the corpus is full of them -- the commercial auto and trucking by-state pages carry a per-state limit table each. An asset that lifts one row without its state is simply false everywhere else. | email, meta_ads, google_ads, landing_page |
| `blocker` | `messaging` | One asset, one offer tier. Never mix free-tier acquisition with paid-plan features. An ad that leads with "$0 to start" and then names the Lead Bank, unlimited lead details, the Recommended badge or the Agency Website is selling a paid feature under a free headline. Check the campaign type's prohibited_themes: agent-acquisition prohibits every paid feature, and plan-upgrade prohibits the free-tier framing. This is sharper than it looks -- both campaign types address the same person at different moments, so the mistake reads as plausible rather than absurd. | email, meta_ads, google_ads, landing_page |
| `info` | `positioning` | Editorial content is not an offer, and the two must stay distinguishable. Guides, comparisons, calculators and career reviews exist to be useful; they must not read as an inducement to sign up, and a platform CTA inside one must be visibly separate from the editorial claim it sits next to. The corpus already blurs this -- the Farmers careers review has "Grow Your Agency Faster with Agency Height Insurance Directory" spliced mid-article, three times. | landing_page, email |
| `warning` | `sourcing` | Third-party review scores must carry the platform, the score, the review count where the source gives one, and the date the score was captured. Scores drift continuously -- the corpus holds "Capterra: 4.6/5 (14168 reviews)" and "TrustRadius: 8.2/10 (5810 reviews)", both true only on the day they were scraped. A score without a capture date is a figure that quietly becomes wrong. | email, meta_ads, google_ads, landing_page |
| `warning` | `testimonials` | Testimonials verbatim, attributed exactly as the source attributes them, with a results-vary qualifier. Never average or aggregate them, never edit a quote to strengthen it, and never convert a figure inside a testimonial into a platform claim. The homepage carries three named five-star testimonials and one of them contains a lead-frequency figure, which is recorded as a prohibited claim precisely because it reads like a statistic. | email, meta_ads, google_ads, landing_page |

## 6. Product features

`available` and `approved_for_marketing` are separate on purpose: a feature can exist and still not be ready to promote. Only the second lets a generator mention it.

| feature | available | promotable | notes |
|---|---|---|---|
| `agent-directory-profile` (Agent directory profile) | yes | **yes** | A free profile that surfaces to consumers searching by license, state and coverage type. L |
| `crm` (CRM) | yes | **yes** | Lead and book management. Every lead becomes an account and moves from Suspect to Prospect |
| `intake-forms` (Intake forms) | yes | **yes** | Coverage-specific bilingual lead capture forms that can be placed on any channel, with sub |
| `markets` (Markets carrier search) | yes | **yes** | Carrier search across admitted and E&S options, filtered by line of business, carrier appe |
| `agency-website` (Agency Website) | yes | no | An AI-built, SEO-optimised agency website that pulls the agent's NPN and specialties autom |
| `lead-bank` (Lead Bank) | yes | no | A pool of unclaimed inbound leads, filterable by state and coverage and claimable in one c |
| `mobile-app` (Mobile app) | yes | no | Listed on the homepage plans table as an included item. No further detail appears anywhere |
| `recommended-badge` (Recommended badge) | yes | no | A badge shown against the agent's directory listing. Included with Premium and Enterprise. |
| `verified-agent-routing` (Verified agent routing) | yes | no | Consumer-side matching: shoppers on Agency Height are routed to agents licensed in their s |

---

## Applying your decisions

Mark this file up however suits you. Hand it back and it becomes a migration, so the decisions are recorded and reversible rather than typed into a SQL console.

```sql
-- The outer gate. Reversible, and an attestation that a human
-- reviewed the claims above.
update public.products set approved_for_marketing = true
 where slug = 'agent-platform';

-- Per claim, once each is decided:
update public.claims set status = 'approved',
       approved_wording = '<the exact wording you would sign>',
       last_reviewed_at = now(), owner = '<you>'
 where claim_text = '<claim text from the tables above>';
```

