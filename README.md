# Marketing Growth Engine

Campaign brief in, reviewed asset pack out. Eleven stages, of which 1–9 are
built: file a brief, validate it against approved claims, generate strategy →
angles → concepts → email and Meta ads, QA every asset, and approve the
campaign. A person signs off at every stage; nothing publishes itself.

Two brands: **Renegade Insurance** (live) and **Agency Height** (governance
staged, marketing not yet enabled — see [Enabling a
brand](#enabling-a-brand)).

---

## Running it

```
python -m main                    # API + UI on http://127.0.0.1:8000
python -m main --port 9000        # or somewhere else
python -m main --reload           # auto-restart on edit, for development
```

Bound to `127.0.0.1` deliberately. This is not hardened for a network — see
[What this is not](#what-this-is-not).

**Read the boot output.** Two lines matter:

```
identity: 1 operator(s), name only. Approvals are attributed.
```

If it instead says identity is not configured, every approval is recorded as
`local-ui` and the UI carries a standing warning on every page. Set
`OPERATORS` to fix it.

```
SCHEMA: 023_operator_exclusions.sql is not applied
        breaks: brief validation, asset QA and every generator -- ...
```

That means code and database have drifted. The app still starts — `/health`,
`/search` and `/context` do not touch the pipeline tables and there is no
reason to take the knowledge API down over a pending UI migration — but the
named screens will fail. Run the migration.

### Configuration

A `.env` file in the project root. Only the first is required.

| key | what it does |
|---|---|
| `DATABASE_URL` | Supabase Postgres connection string. **Required.** |
| `OPERATORS` | Comma-separated names allowed to sign in. Empty = identity off, everything recorded as `local-ui`. |
| `SESSION_SECRET` | HMAC key for the signed session cookie. Generate once; changing it signs everyone out. |
| `OPERATOR_PASSCODE` | Optional shared passcode on top of the name. |
| `GEMINI_API` | Generation and embeddings. Without it, generators cannot run; everything else works. |
| `OPEN_AI_API` | Alternative provider. `--provider openai` on any generator. |

Removing a name from `OPERATORS` ends that person's session on their next
request. The allowlist is re-checked on every cookie read, not just at
sign-in.

---

## The flow

Each stage has a button in the UI and a CLI command that does the same thing.
The UI is one caller; the guards live in the modules, not the routes.

| stage | UI | CLI |
|---|---|---|
| 1. Brief | New campaign | — |
| 3. Validate | Validate | `python -m validation.brief "<name>"` |
| 4. Strategy | Generate strategy → Approve | `python -m generators.strategy "<name>"` |
| 5. Angles | Generate → Approve/Reject each | `python -m generators.angles "<name>"` |
| 6. Concepts | Generate → Approve/Reject each | `python -m generators.concepts "<name>"` |
| 7. Assets | Generate Meta ads / email sequence / UGC video scripts | `python -m generators.meta_ads "<name>"` |
| | | `python -m generators.video_script "<name>" --seconds 30` |
| 8. QA | Run QA | `python -m validation.asset_qa "<name>"` |
| 9. Approval | Approve campaign | `python -m lifecycle "<name>" --promote approved` |

Add `--ai` to validation or QA for the judgement tier (one model call). Add
`--dry-run` to see findings without writing.

**Do not use** in the nav sits outside the stages: it is where you declare
wording that must never appear, and it constrains every stage below. See
[Saying what not to say](#saying-what-not-to-say).

Stage 2 (knowledge retrieval) runs inside every generator rather than as a
step. Stage 10 (version history) is a by-product: every asset carries the
chunks, claims and rules that produced it. Stage 11 is not built.

### Asking for a change

Every generated item has an **Edit** button. Write what is wrong; the agent
receives it, revises, and returns a new version. Assets and strategies get a
new version row; angles and concepts are revised in place.

Three things about this are deliberate:

- A revised asset arrives with **no QA result**. QA has to run again. The
  guarantee is the deterministic checks, not the prompt — reviewer feedback is
  untrusted text and could ask for something prohibited.
- Governance goes into the prompt **before** the feedback, so a request that
  conflicts with a blocker rule loses. The agent has an `agent_note` field to
  say so rather than silently ignoring the request.
- An item with an open edit request **cannot** be approved, in bulk or
  individually. Approving over someone's feedback strands it against copy
  that is now signed off.

If the revision is worse than what it replaced, **Reject** it. The older
version stays live and the slot is settled. Requires migration 022.

### Tracked links

Give the brief a **Destination URL** and every approved asset gets that URL
with its own utm tags — source/medium per channel, the campaign as
`utm_campaign`, and the asset type + slot + version as `utm_content`
(`ad-a-v3`, `vid-a-v3`, `email-a-2-v5`), so v5 and v7 of the same ad stay
distinguishable in a report — and so a UGC video and a static ad in the
same slot do not report as one thing, which they did until 026. The link is **stamped at
approval** (renaming the campaign later cannot drift a URL that already
shipped) and shown on the approved asset — whoever builds the email or the
ad pastes *that*, not the bare destination, or the campaign ships
unmeasurable. A brief with channels and no destination gets a validation
warning, not a blocker. Conventions live in one map in `tracking.py`.

### UGC video scripts

The team makes UGC video ads on an AI video platform, and the platform needs a
script. Pick a length (15/30/45/60s), press **Generate UGC video scripts**,
and each approved concept yields two variants — one asset row holding a
**shot list**:

| Per shot | What it is for |
| --- | --- |
| `spoken` | what the person on camera says |
| `on_screen` | the burned-in caption |
| `visual_prompt` | paste this into the video tool to make the clip |
| `seconds` | how long the shot runs |

Plus a `cta` and a feed `caption` for the ad as a whole. The channel is
`meta_ads`, not a channel of its own: the video runs as a Meta ad, so it
inherits Meta's utm convention rather than needing a second one invented for
it. TikTok would be a genuinely new channel, decided when it is wanted.

**A script is approved or rejected whole.** Unlike an email sequence (N rows,
so QA can fail email 2 of 3), the shots are one deliverable — shot 3 without
shot 2 is not a shorter ad, it is a broken one.

QA checks two things a character count cannot:

- **Can the line be said?** Words ÷ 2.5 per second against the shot's own
  length. A 3-second hook carrying 20 words is undeliverable, and that is a
  fact, not a preference — so it is a warning with a word budget attached,
  not advice.
- **Does it run to length?** Shot lengths against the target, and the opening
  shot against 3 seconds, because on Reels the first three seconds decide
  whether the rest is watched.

Structural breakage (no shots, a shot missing its spoken line, shots numbered
with gaps) is a **blocker** — there is no false-positive case for it.

**One thing the checker cannot catch.** The generator is instructed never to
depict an identifiable real person, and never to frame the speaker as a named
customer giving a testimonial. A generated face presented as a real customer
is a fabricated endorsement however well-sourced the words are, and no
deterministic check can see a picture. That one is on the reviewer.

### Approve all

Approves everything eligible and **reports what it skipped, with reasons**. A
bulk action that quietly approves six of seven and says "done" hides the one
item that needed a human. Skips you will see:

- `has a QA warning -- read it and approve individually`. Warnings are
  truthful findings; bulk-approving them means someone cleared them without
  reading. The single Approve button still accepts a warning.
- `v4 is a newer version awaiting review`. A slot holds several versions and
  only one can be live, so only the **newest** is a candidate. Approving an
  older version over a newer one is never what a reviewer means.
- `an edit was requested and is still open`.

---

## When a campaign will not approve

`Approve campaign` requires **every asset slot to have an approved version**.
A slot is one channel + type + variant + sequence position — an email sequence
is N slots, not one — and a slot may hold several versions with at most one
approved. The page lists every blocker at once, so you see the whole list
rather than discovering the next after fixing the last.

| blocker | what to do |
|---|---|
| `no_assets` | Generate assets for the channels in the brief. |
| `assets_not_approved` | Approve one version of each named slot. |
| `newer_version_pending` | A slot's live version is older than one awaiting review. Approve the newer one, or reject it to keep what is live. |
| `channel_not_covered` | The brief promises a channel with no approved asset. Generate and approve them, or remove the channel from the brief. |
| `open_revision` | Let the agent answer the edit request, or withdraw it. |
| `qa_blocked` | Fix the copy with an edit request and re-run QA. |
| `brief_blocked` | Re-run validation and clear the blockers. |

The checklist on the page and the guard behind the button are the same
function (`lifecycle.approval_readiness`), so they cannot disagree.

### Statuses

`draft → validated → strategy → production → review → approved → live →
completed`, plus `blocked` / `needs_info` from validation and `archived` from
anywhere.

The first five are **inferred** — a strategy was approved, so the campaign is
at `strategy`. Nobody decided that; it follows. The rest are **decided** by a
person. `campaign_status_events` records every move with an `automatic` flag,
because an audit that cannot tell an inference from a sign-off is misleading
in the direction that matters.

`approved` has exactly one legal predecessor: `review`. A campaign cannot be
promoted backwards, and re-validating one that has progressed does not demote
it — though a *failing* validation still moves it, wherever it had got to.

---

## Governance: why the copy is trustworthy

The system separates **evidence** from **authority**, and this is the point of
the whole design.

- `kb.*` — 833 documents, 1,755 chunks of scraped site copy. **Unverified.**
  Useful for tone and context; never quotable as fact.
- `public.claims` — human-approved statements with `approved_wording`. A claim
  is `approved`, `restricted` (usable only under its stated condition),
  `prohibited`, or `pending_review`. Only `approved` claims reach a prompt
  as assertable.
- `public.brand_rules` — blockers and restrictions that constrain wording
  regardless of what any claim says.
- `public.product_features` — `available` and `approved_for_marketing` are
  **separate** booleans, so a feature that exists but is not ready to promote
  can be recorded honestly.

Two nested gates let a claim through: the product must be
`approved_for_marketing`, then the claim itself must be `approved`.

### Saying what not to say

Everything above is *derived* — read out of the corpus, then reviewed. There is
a second kind of prohibition that is simply an instruction, and it does not
need a source or a status:

```
Do not use  →  free forever
               guaranteed leads
               $20,000
```

- **`public.brand_exclusions`** — the standing list, per brand, at
  **/exclusions**. Applies to every campaign for that brand.
- **`campaigns.do_not_mention`** — the same thing on one brief, for a one-off
  that is not a standing rule.

Both are **blockers**: any asset containing the wording fails QA and cannot be
approved. The phrases are also stated to the model *before* it sees any
evidence, as a flat instruction with no rationale attached — a reason invites
the model to decide the reason does not apply here.

Two lists rather than one because they fail differently. A standing exclusion
forgotten on a brief is a compliance problem; a one-off promoted to a standing
rule quietly narrows every future campaign. A blocker says which list caught
it, so you know whether to argue with the brief or with the brand.

Matching ignores case and extra whitespace and anchors on whole words, so an
exclusion on `ad` will not flag "adjuster". Exclusions shorter than two
characters are ignored: `contains_phrase` is word-boundary anchored, so "a"
genuinely is a word in "a nice offer", and one keystroke would otherwise block
every asset in a campaign.

Retiring an exclusion keeps the row and records who ended it. Re-adding a
retired phrase revives it.

**Nothing is prohibited by default.** Migration 023 parked the twelve
prohibited claims and twelve blocker rules that were originally derived from
the corpus rather than decided by anyone — claims to `pending_review`, rules to
`inactive`, both reversible with one `UPDATE`. Until you add something, no
wording is blocked on an operator's instruction. That is deliberate, and it is
a real reduction in cover: `$20,000` in a brief passes validation clean today.

Deterministic checks — prohibited wording, price consistency, CTA
consistency, unavailable features, campaign-type mixing, missing brief fields,
character limits — run with no model involved and can produce no false
positives. The AI tier handles only judgement: conflicting messaging, message
match across channels, weak concepts. **An AI opinion can never clear a
deterministic blocker.** That is why the two live in separate columns.

Every asset stores a `knowledge_snapshot` naming the chunks, claims and rules
that produced it. An empty one would mean the copy was ungrounded; the schema
forbids it.

### Enabling a brand

Agency Height's governance is staged and provably inert: `fetch_claims`
returns 4 prohibited and 6 restricted rules, and **zero assertable claims**.
Its one product is `approved_for_marketing = false`.

Before enabling it, generate the review worksheet:

```
python scripts/claim_review.py --brand agencyheight
```

That writes `docs/agencyheight-claim-review.md` from the live database — every
claim with its exact wording, source URL and the reason it was restricted,
`pending_review` first. Annotate it and hand it back; it becomes a migration,
so the decisions leave a diff instead of being typed into a SQL console.

Note what the worksheet points out: **12 Agency Height claims are already
`approved` and still produce nothing**, because the product gate is shut. They
all become assertable the moment it opens, which makes enabling the product a
larger step than one boolean suggests.

Turning it on is a factual attestation that a human reviewed those claims, so
it is one deliberate statement, quoted in `migrations/017`:

```sql
update public.products set approved_for_marketing = true
 where slug = 'agent-platform';
```

The brand-level positioning claim is `pending_review` rather than `approved`
for a structural reason worth knowing: a brand-level claim has no product to
gate on, so it passes on brand alone. An `approved` row there would bypass the
product gate entirely.

---

## Migrations

```
python migrate.py --status        # what is applied, what is pending
python migrate.py --dry-run       # parse and check, write nothing
python migrate.py                 # apply everything pending, in order
python scripts/validate_migrations.py   # static checks, no database
```

Each file runs in one transaction and is recorded, so a failure rolls back
cleanly and re-running is safe. Every file carries a header saying what it
does, why, and how to reverse it.

`scripts/validate_migrations.py` catches balance errors, `VALUES` arity
mismatches, and status/channel vocabulary drift before anything touches the
database. It is self-tested against deliberate mutations.

---

## Tests

```
python -m pytest                  # 589 tests, about 10 seconds
python -m pytest -m dbtest        # only the ones needing a live database
```

Everything except `dbtest` is pure and runs offline. **A skip means the
database is unreachable and nothing else** — that narrowness was bought the
hard way, after a broad `except Exception` reported a perfectly reachable
database as down and nine tests silently stopped running.

```
python scripts/scan_secrets.py            # ten credential shapes, never prints a match
python scripts/scan_secrets.py --install-hook   # block them at commit time
```

---

## What this is not

Phase 1 stops here on purpose. Not built, and each needs a decision rather
than just code:

- **Hosting and real auth.** The signed cookie plus an allowlist is honest
  for `127.0.0.1`. The moment the team reaches this over a network it needs a
  login system and a deploy target.
- **Google Ads, landing pages, SMS.** Schema slots and asset types exist;
  generators and character limits do not. Google Ads is deferred by decision.
- **Reading performance back.** Tagged links exist (025/026), so every
  approved asset is now identifiable in an analytics report. Nothing yet
  imports those numbers and lays them against the brief's `primary_kpi` —
  that needs a decision about where the numbers come from (GA4 export,
  platform CSVs, manual entry) before it needs code.
- **The rest of stage 11** — Teams approval, launch integrations, pushing an
  asset to the ad platform itself.
- **Freshness and citation for Agency Height's editorial figures.** 426 of
  them are third-party market statistics, not self-claims. Quoting a
  third-party figure needs a citation and an age, which is a different review
  question from approving a claim about your own business.

One structural note for whoever picks this up: the app connects as a Postgres
superuser with `BYPASSRLS`. RLS is enabled with zero policies on every table,
which stops PostgREST and anon keys but does **not** constrain this process.
The operator cookie changes who a row credits, not what a local process can
do. Treat it as attribution, not access control.
