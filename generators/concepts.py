"""Stage 6: creative concepts.

    python -m generators.concepts "Franchise Recruitment - Captive Agents Q4 2026"
    python -m generators.concepts <ref> --approve <concept-uuid>

2 to 3 concepts per APPROVED angle. A concept is the creative expression of an
angle -- the idea, the hook, the visual direction -- and it exists as its own
stage because the PDF is explicit that concepts precede copy "so we don't
produce five slightly different versions of the same ad". 013 backs that with
campaign_assets.concept_id NOT NULL: no concept, no copy.

Same draft-replacement semantics as angles: regeneration deletes draft
concepts for the angles being regenerated, keeps decided ones.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import auth
from db import cursor, fetch_all, pool
from generators.pipeline import (
    StageNotReady,
    approved_strategy,
    as_jsonb,
    brief_block,
    context_for,
    load_campaign,
)
from generators.prompting import acomplete_json, to_system_prompt

INSTRUCTIONS = """\
For EACH angle below, propose 2 to 3 creative concepts.

A concept is one way to express that angle -- not copy yet. Each needs:
- idea: the creative device, one or two sentences. What the ad/email actually
  does: a comparison, a day-in-the-life, a myth-vs-fact, a cost breakdown, a
  before/after. Concepts within one angle must use DIFFERENT devices.
- hook: the opening line or headline thought, in the audience's language.
  This is the one line that must survive into the copy stage.
- visual_direction: what the ad looks like or the email's visual structure.
  Concrete enough for a designer to start, no stock-photo cliches.

Stay inside the approved claims for any figure a hook implies. A hook that
needs an unapproved number is an unusable concept.
"""

SHAPE_HINT = """\
Keys and types:
{
  "concepts": [
    {
      "angle_name": "exactly the angle name given",
      "idea": "string",
      "hook": "string",
      "visual_direction": "string"
    },
    ...
  ]
}"""


def angles_block(angles: list[dict]) -> str:
    lines = ["## Approved angles"]
    for a in angles:
        lines += [f"### {a['name']}",
                  f"- Hypothesis: {a['hypothesis']}",
                  f"- Rationale: {a['rationale']}", ""]
    return "\n".join(lines)


def validate(data: dict, angle_names: set[str]) -> list[str]:
    problems = []
    concepts = data.get("concepts")
    if not isinstance(concepts, list) or not concepts:
        return ["concepts must be a non-empty list"]
    per_angle: dict[str, int] = {}
    for i, c in enumerate(concepts):
        for key in ("angle_name", "idea", "hook", "visual_direction"):
            if not (c or {}).get(key):
                problems.append(f"concepts[{i}] missing {key}")
        name = (c or {}).get("angle_name", "")
        if name and name not in angle_names:
            problems.append(f"concepts[{i}] names unknown angle {name!r}")
        per_angle[name] = per_angle.get(name, 0) + 1
    for name in angle_names:
        if not 2 <= per_angle.get(name, 0) <= 3:
            problems.append(f"angle {name!r} needs 2 to 3 concepts, "
                            f"got {per_angle.get(name, 0)}")
    return problems


async def generate(campaign_ref: str, *, provider: str | None = None,
                   show_prompt: bool = False) -> dict:
    campaign = await load_campaign(campaign_ref)
    strategy = await approved_strategy(campaign["id"])
    angles = await fetch_all(
        "select * from public.campaign_angles "
        "where strategy_id = %s and status = 'approved' order by position",
        (strategy["id"],))
    if not angles:
        raise StageNotReady(
            "no approved angles for the approved strategy. Approve at least "
            "one (python -m generators.angles <ref> --approve <id>) first.")

    ctx = await context_for(
        campaign,
        stage_query=f"{campaign['product_name']} "
                    f"{' '.join(a['name'] for a in angles)} "
                    f"{campaign['primary_benefit'] or ''}",
    )
    system = to_system_prompt(ctx)
    user = (f"{INSTRUCTIONS}\n\n{brief_block(campaign)}\n\n"
            f"{angles_block(angles)}")

    if show_prompt:
        print(system, "\n" + "=" * 74 + "\n", user)
        return {}

    angle_names = {a["name"] for a in angles}
    data = await acomplete_json(system, user, shape_hint=SHAPE_HINT,
                                provider=provider)
    problems = validate(data, angle_names)
    if problems:
        data = await acomplete_json(
                                    system,
                                    f"{user}\n\nYour previous answer had structural problems: "
                                    f"{'; '.join(problems)}. Produce the complete object.",
                                    shape_hint=SHAPE_HINT, provider=provider)
        problems = validate(data, angle_names)
        if problems:
            raise RuntimeError(f"concepts failed validation twice: {problems}")

    by_name = {a["name"]: a for a in angles}
    async with cursor() as cur:
        await cur.execute(
            "delete from public.creative_concepts "
            "where angle_id = any(%s) and status = 'draft'",
            ([a["id"] for a in angles],))
        replaced = cur.rowcount

        written = []
        position: dict[str, int] = {}
        for c in data["concepts"]:
            angle = by_name[c["angle_name"]]
            pos = position.get(c["angle_name"], 0)
            position[c["angle_name"]] = pos + 1
            await cur.execute(
                "insert into public.creative_concepts "
                "  (campaign_id, angle_id, idea, hook, visual_direction, "
                "   position, status, knowledge_snapshot) "
                "values (%s, %s, %s, %s, %s, %s, 'draft', %s) "
                "returning id",
                (campaign["id"], angle["id"], c["idea"], c["hook"],
                 c["visual_direction"], pos, as_jsonb(ctx["snapshot"])))
            row = await cur.fetchone()
            written.append({"id": str(row["id"]),
                            "angle": c["angle_name"],
                            "hook": c["hook"]})

    return {
        "campaign": campaign["name"],
        "angles": sorted(angle_names),
        "drafts_replaced": replaced,
        "concepts": written,
    }


async def decide(concept_id: str, status: str, decided_by: str) -> dict:
    """Approve or reject one concept, recording who decided. See angles.decide
    for why decided_by has no default."""
    assert status in ("approved", "rejected")
    async with cursor() as cur:
        await cur.execute(
            "update public.creative_concepts "
            "set status = %s, decided_by = %s, decided_at = now() "
            "where id = %s returning id, hook, status, decided_by",
            (status, decided_by, concept_id))
        row = await cur.fetchone()
    if not row:
        raise LookupError(f"no concept {concept_id}")
    return {"id": str(row["id"]), "hook": row["hook"], "status": row["status"],
            "decided_by": row["decided_by"]}


async def amain() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("campaign", help="campaign UUID or exact name")
    ap.add_argument("--provider", default=None, choices=["gemini", "openai"])
    ap.add_argument("--show-prompt", action="store_true")
    ap.add_argument("--approve", metavar="CONCEPT_ID", default=None)
    ap.add_argument("--reject", metavar="CONCEPT_ID", default=None)
    ap.add_argument("--as", dest="operator", default=None,
                    help="who to record the decision as; defaults to cli:<os "
                         "user>")
    args = ap.parse_args()

    await pool.open()
    try:
        if args.approve or args.reject:
            result = await decide(args.approve or args.reject,
                                  "approved" if args.approve else "rejected",
                                  auth.cli_operator(args.operator))
        else:
            result = await generate(args.campaign, provider=args.provider,
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
