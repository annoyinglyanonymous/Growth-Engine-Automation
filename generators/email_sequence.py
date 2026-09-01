"""Stage 7, email sequence.

    python -m generators.email_sequence "Franchise Recruitment - Captive Agents Q4 2026"
    python -m generators.email_sequence <ref> --concept <uuid> --emails 3
    python -m generators.email_sequence <ref> --approve <asset-uuid> --approved-by <who>

A sequence is N campaign_assets rows sharing a variant and differing by
position -- 013's design, so QA can fail email 2 of 3 and the UI can approve
each step individually. One concept drives the whole sequence: a sequence is
one narrative told across days, not three unrelated ads, which is why this
generator takes exactly one concept (the first approved one, or --concept).

Generated in ONE model call, unlike meta_ads' one-call-per-variant: the emails
reference each other ("as I mentioned Tuesday"), so the model needs to see the
whole arc. Per-email calls would produce three openers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from db import cursor, fetch_all, pool
from generators.pipeline import (
    StageNotReady,
    approve,
    approved_strategy,
    as_jsonb,
    brief_block,
    context_for,
    load_campaign,
    next_version,
)
from generators.prompting import acomplete_json, to_system_prompt
from validation.checks import CHANNEL_LIMITS

#: Imported, not redefined -- see the note in generators/meta_ads.py.
_EMAIL_LIMITS = CHANNEL_LIMITS[("email", "email")]
LIMITS = {"subject": _EMAIL_LIMITS["subject"],
          "preheader": _EMAIL_LIMITS["preheader"]}
BODY_MAX_CHARS = _EMAIL_LIMITS["body"]

INSTRUCTIONS_TMPL = """\
Write a {n}-email nurture sequence from the concept below.

The sequence is ONE argument built across {n} emails, not {n} separate ads:
- Email 1 opens the problem and earns the read; the CTA is soft.
- Middle emails deepen with proof: approved figures, how-it-works, an
  objection handled honestly.
- The final email asks directly for the primary CTA.

Per email:
- subject: <= {subject} characters, no clickbait, no ALL CAPS, no "RE:" fakery.
- preheader: <= {preheader} characters, complements the subject rather than
  repeating it.
- body: plain text with blank-line paragraphs, under {body} characters.
  First person, from a Renegade franchise specialist to a licensed agent.
  No unsubscribe boilerplate (the platform adds it).
- purpose: one line stating this email's job in the sequence, for reviewers.

Every figure in any email must come from the approved claims, with required
disclaimers carried in the email that uses the figure.
"""

SHAPE_HINT = """\
Keys and types:
{
  "emails": [
    {
      "position": 1,
      "purpose": "string",
      "subject": "string",
      "preheader": "string",
      "body": "string"
    },
    ...
  ]
}"""


def validate(data: dict, n: int) -> list[str]:
    problems = []
    emails = data.get("emails")
    if not isinstance(emails, list) or len(emails) != n:
        return [f"emails must be a list of exactly {n}"]
    for i, e in enumerate(emails):
        for key in ("purpose", "subject", "preheader", "body"):
            if not (e or {}).get(key):
                problems.append(f"emails[{i}] missing {key}")
        if (e or {}).get("position") != i + 1:
            problems.append(f"emails[{i}] position must be {i + 1}")
        for key, limit in LIMITS.items():
            if e.get(key) and len(e[key]) > limit:
                problems.append(f"emails[{i}].{key} is {len(e[key])} chars, "
                                f"limit {limit}")
        if e.get("body") and len(e["body"]) > BODY_MAX_CHARS:
            problems.append(f"emails[{i}].body is {len(e['body'])} chars, "
                            f"limit {BODY_MAX_CHARS}")
    return problems


async def pick_concept(campaign_id: str, concept_id: str | None) -> dict:
    rows = await fetch_all(
        "select cc.*, a.name as angle_name, a.hypothesis as angle_hypothesis "
        "from public.creative_concepts cc "
        "join public.campaign_angles a on a.id = cc.angle_id "
        "where cc.campaign_id = %s and cc.status = 'approved' "
        "  and (%s::text is null or cc.id::text = %s) "
        "order by a.position, cc.position limit 1",
        (campaign_id, concept_id, concept_id))
    if not rows:
        raise StageNotReady(
            "no approved concept to build the sequence from. Approve one "
            "(python -m generators.concepts <ref> --approve <id>) or pass "
            "--concept explicitly.")
    return rows[0]


async def generate(campaign_ref: str, *, concept_id: str | None = None,
                   emails: int = 3, provider: str | None = None,
                   show_prompt: bool = False) -> dict:
    if not 2 <= emails <= 7:
        raise SystemExit(f"--emails {emails}: a sequence is 2 to 7 emails")
    campaign = await load_campaign(campaign_ref)
    if "email" not in (campaign["channels"] or []):
        raise StageNotReady(
            f"campaign channels are {campaign['channels']}; email is not "
            f"among them.")
    await approved_strategy(campaign["id"])  # ordering guard
    concept = await pick_concept(campaign["id"], concept_id)

    ctx = await context_for(
        campaign,
        stage_query=f"{campaign['product_name']} {concept['hook']} "
                    f"{concept['angle_name']}",
    )
    system = to_system_prompt(ctx)
    instructions = INSTRUCTIONS_TMPL.format(
        n=emails, subject=LIMITS["subject"], preheader=LIMITS["preheader"],
        body=BODY_MAX_CHARS)
    user = "\n".join([
        instructions, "",
        brief_block(campaign), "",
        "## Concept",
        f"- Angle: {concept['angle_name']} -- {concept['angle_hypothesis']}",
        f"- Idea: {concept['idea']}",
        f"- Hook: {concept['hook']}",
    ])

    if show_prompt:
        print(system, "\n" + "=" * 74 + "\n", user)
        return {}

    data = await acomplete_json(system, user, shape_hint=SHAPE_HINT,
                                provider=provider)
    problems = validate(data, emails)
    if problems:
        data = await acomplete_json(
                                    system,
                                    f"{user}\n\nYour previous sequence had structural problems: "
                                    f"{'; '.join(problems)}. Rewrite within the limits -- tighter "
                                    f"phrasing, not truncation.",
                                    shape_hint=SHAPE_HINT, provider=provider)
        problems = validate(data, emails)
        if problems:
            raise RuntimeError(f"sequence failed validation twice: {problems}")

    # One version number for the whole sequence: the rows form one asset in
    # the review sense, and mixed versions inside a sequence would make
    # "which sequence is live" unanswerable.
    version = await next_version("campaign_assets", campaign["id"])
    written = []
    async with cursor() as cur:
        for e in data["emails"]:
            content = {"subject": e["subject"], "preheader": e["preheader"],
                       "body": e["body"], "purpose": e["purpose"]}
            await cur.execute(
                "insert into public.campaign_assets "
                "  (campaign_id, concept_id, channel, asset_type, variant, "
                "   position, content, version_number, status, "
                "   knowledge_snapshot, generator_version) "
                "values (%s, %s, 'email', 'email', 'A', %s, %s, %s, 'draft', "
                "        %s, 'email_sequence/1') "
                "returning id",
                (campaign["id"], concept["id"], e["position"],
                 as_jsonb(content), version, as_jsonb(ctx["snapshot"])))
            row = await cur.fetchone()
            written.append({
                "asset_id": str(row["id"]),
                "position": e["position"],
                "purpose": e["purpose"],
                "subject": e["subject"],
                "lengths": {"subject": len(e["subject"]),
                            "preheader": len(e["preheader"]),
                            "body": len(e["body"])},
            })

    return {
        "campaign": campaign["name"],
        "concept": concept["hook"][:60],
        "version": version,
        "emails": written,
    }


async def amain() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("campaign", help="campaign UUID or exact name")
    ap.add_argument("--concept", default=None)
    ap.add_argument("--emails", type=int, default=3)
    ap.add_argument("--provider", default=None, choices=["gemini", "openai"])
    ap.add_argument("--show-prompt", action="store_true")
    ap.add_argument("--approve", metavar="ASSET_ID", default=None,
                    help="approve ONE email row (approval is per step)")
    ap.add_argument("--approved-by", default=None)
    args = ap.parse_args()

    await pool.open()
    try:
        if args.approve:
            if not args.approved_by:
                raise SystemExit("--approve requires --approved-by")
            result = await approve("campaign_assets", args.approve,
                                   args.approved_by)
        else:
            result = await generate(args.campaign, concept_id=args.concept,
                                    emails=args.emails,
                                    provider=args.provider,
                                    show_prompt=args.show_prompt)
        if result:
            print(json.dumps(result, indent=2, ensure_ascii=False))
    finally:
        await pool.close()
    return 0


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())
