"""Stage 4: campaign strategy.

    python -m generators.strategy "Renegade Franchise Q4"
    python -m generators.strategy <campaign-uuid> --show-prompt
    python -m generators.strategy <ref> --approve <strategy-uuid>

Reads the stage-1 brief, retrieves governed context, asks the model for one
strategy, and writes it as the next campaign_strategies version in 'draft'.
Approval is a separate, human action -- the generator never approves its own
output.

WHY THE OUTPUT IS FIELDS AND NOT AN ESSAY
campaign_strategies stores pain_points, benefits, proof_points and objections
as typed columns because the downstream stages consume them individually:
angles are generated per pain point, QA checks proof points against approved
claims. One prose blob would push a parsing problem into every consumer.

PROOF POINTS ARE THE GOVERNED EDGE
The model is instructed to take proof_points ONLY from approved claims, and
the instruction is enforced later: deterministic QA (step 5) cross-checks
every stored proof point against public.claims. Here we also record in the
snapshot which claims were offered, so a strategy whose proof points cite
nothing is visibly ungrounded rather than quietly plausible.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from db import cursor, pool
from generators.pipeline import (
    approve,
    as_jsonb,
    brief_block,
    context_for,
    load_campaign,
    next_version,
)
from generators.prompting import acomplete_json, to_system_prompt

GENERATOR_VERSION = "strategy/1"

#: What the model is asked to produce. Kept next to the SHAPE_HINT it must
#: match, because the two drift apart when separated.
INSTRUCTIONS = """\
Write ONE campaign strategy for the brief below.

Ground rules:
- pain_points: the audience's actual pains, in their vocabulary, sharpest
  first. 3 to 5.
- benefits: what this campaign's product genuinely offers against those pains.
  3 to 5. No benefit that the site copy or approved claims cannot support.
- proof_points: ONLY facts from the "Approved claims" section, quoted or
  tightly paraphrased. If a claim requires a disclaimer, carry the disclaimer.
  Never invent a figure, never promote a number from site copy that is not in
  the approved list.
- objections: the 2 to 4 objections this audience will actually raise, each
  with a response that uses only approved material.
- core_message: one sentence. The single thing the audience should believe
  after seeing this campaign.
- positioning: 2 to 3 sentences. How this offer is framed against the
  audience's alternatives (staying captive, staying independent alone, etc.)
  -- without naming or disparaging specific competitors.
- hypothesis: one sentence, falsifiable: "If we lead with X for audience Y,
  we expect Z" where Z relates to the brief's KPI.
"""

SHAPE_HINT = """\
Keys and types:
{
  "core_message": "string",
  "positioning": "string",
  "pain_points": ["string", ...],
  "benefits": ["string", ...],
  "proof_points": ["string", ...],
  "objections": [{"objection": "string", "response": "string"}, ...],
  "hypothesis": "string"
}"""

#: Fields the model must return non-empty. A strategy without pain points is
#: not a strategy, and storing it as one hands a hollow row to stage 5.
REQUIRED = ("core_message", "positioning", "pain_points", "benefits",
            "proof_points", "objections", "hypothesis")


def validate_shape(data: dict) -> list[str]:
    """Cheap structural validation, returning problems rather than raising.

    This is NOT the QA stage. It rejects only what would make the row
    unusable to stage 5: missing keys, empty lists, objections without both
    halves. Judgement calls (weak hypothesis, duplicate benefits) belong to
    validation/, where they are recorded rather than silently retried.
    """
    problems = []
    for key in REQUIRED:
        if not data.get(key):
            problems.append(f"missing or empty: {key}")
    for i, obj in enumerate(data.get("objections") or []):
        if not isinstance(obj, dict) or not obj.get("objection") \
                or not obj.get("response"):
            problems.append(f"objections[{i}] needs 'objection' and 'response'")
    for key in ("pain_points", "benefits", "proof_points"):
        val = data.get(key)
        if val is not None and not isinstance(val, list):
            problems.append(f"{key} must be a list")
    return problems


async def generate(campaign_ref: str, *, provider: str | None = None,
                   show_prompt: bool = False) -> dict:
    campaign = await load_campaign(campaign_ref)

    ctx = await context_for(
        campaign,
        stage_query=f"{campaign['product_name']} positioning benefits "
                    f"objections {campaign['primary_benefit'] or ''}",
    )
    system = to_system_prompt(ctx)
    user = f"{INSTRUCTIONS}\n\n{brief_block(campaign)}"

    if show_prompt:
        print(system)
        print("\n" + "=" * 74 + "\n")
        print(user)
        return {}

    data = await acomplete_json(system, user, shape_hint=SHAPE_HINT,
                                provider=provider)
    problems = validate_shape(data)
    if problems:
        # One retry with the problems named. A model that misses required
        # fields twice is not having a formatting hiccup.
        data = await acomplete_json(
                                    system,
                                    f"{user}\n\nYour previous strategy had structural problems: "
                                    f"{'; '.join(problems)}. Produce the complete object.",
                                    shape_hint=SHAPE_HINT, provider=provider)
        problems = validate_shape(data)
        if problems:
            raise RuntimeError(
                f"strategy failed structural validation twice: {problems}")

    version = await next_version("campaign_strategies", campaign["id"])
    async with cursor() as cur:
        await cur.execute(
            "insert into public.campaign_strategies "
            "  (campaign_id, version_number, status, core_message, "
            "   positioning, pain_points, benefits, proof_points, objections, "
            "   hypothesis, knowledge_snapshot, model_name, generator_version) "
            "values (%s, %s, 'draft', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "returning id, version_number",
            (campaign["id"], version,
             data["core_message"], data["positioning"],
             data["pain_points"], data["benefits"], data["proof_points"],
             as_jsonb(data["objections"]), data["hypothesis"],
             as_jsonb(ctx["snapshot"]), None, GENERATOR_VERSION))
        row = await cur.fetchone()

    return {
        "strategy_id": str(row["id"]),
        "version": row["version_number"],
        "campaign": campaign["name"],
        "core_message": data["core_message"],
        "hypothesis": data["hypothesis"],
        "counts": {k: len(data[k]) for k in
                   ("pain_points", "benefits", "proof_points", "objections")},
        "grounding": {
            "kb_chunks": len(ctx["snapshot"]["kb_chunk_ids"]),
            "approved_claims_offered":
                len(ctx["snapshot"]["approved_claims"]),
        },
    }


async def run_approve(strategy_id: str, approved_by: str) -> dict:
    return await approve("campaign_strategies", strategy_id, approved_by)


async def amain() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("campaign", help="campaign UUID or exact name")
    ap.add_argument("--provider", default=None, choices=["gemini", "openai"])
    ap.add_argument("--show-prompt", action="store_true",
                    help="print the assembled prompt and stop; no API key needed")
    ap.add_argument("--approve", metavar="STRATEGY_ID", default=None,
                    help="approve this strategy version instead of generating")
    ap.add_argument("--approved-by", default=None,
                    help="who is approving (required with --approve)")
    args = ap.parse_args()

    await pool.open()
    try:
        if args.approve:
            if not args.approved_by:
                raise SystemExit("--approve requires --approved-by "
                                 "(013 rejects an approval with no approver)")
            result = await run_approve(args.approve, args.approved_by)
        else:
            result = await generate(args.campaign, provider=args.provider,
                                    show_prompt=args.show_prompt)
        if result:
            print(json.dumps(result, indent=2, ensure_ascii=False))
    finally:
        await pool.close()
    return 0


def main() -> int:
    # db.py's import installed the selector-loop policy; asyncio.run then
    # builds the right loop on Windows.
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())
