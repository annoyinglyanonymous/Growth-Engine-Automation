"""Stage 5: campaign angles.

    python -m generators.angles "Franchise Recruitment - Captive Agents Q4 2026"
    python -m generators.angles <ref> --approve <angle-uuid> [--reject <uuid>]

Derives 3 to 5 distinct arguments from the APPROVED strategy. Refuses to run
against a draft one (StageNotReady): angles from a strategy that then changes
are a wasted generation pass, and the schema cannot express that ordering --
only existence.

Angles are not versioned (regeneration replaces drafts; approved ones stay).
Approval here is per-angle and additive -- several angles can be approved at
once, because stage 6 fans concepts out across all of them. That is why this
module does NOT use pipeline.approve(), which enforces sole-survivor semantics.

WHY REGENERATION DELETES DRAFTS
Leftover draft angles from a previous run would sit beside the new set,
indistinguishable, and the angles_name_key unique constraint would reject any
regenerated angle that kept its name. Approved and rejected rows are decisions;
drafts are proposals, and a fresh proposal set replaces the standing one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import auth
from db import cursor, pool
from generators.pipeline import (
    approved_strategy,
    as_jsonb,
    brief_block,
    context_for,
    load_campaign,
)
from generators.prompting import acomplete_json, to_system_prompt

INSTRUCTIONS = """\
Derive 3 to 5 DISTINCT campaign angles from the approved strategy below.

An angle is one argument the campaign could lead with -- cost, independence,
support, speed-to-open, credibility. Each must:
- attack a different pain point or motivation; if two angles would produce
  similar ads, merge them.
- be honest to the approved claims: an angle whose proof would need an
  unapproved figure is not usable.
- carry a falsifiable hypothesis tied to the campaign KPI.

Order them by how strongly the strategy's evidence supports them, strongest
first.
"""

SHAPE_HINT = """\
Keys and types:
{
  "angles": [
    {
      "name": "short handle, 2-4 words, e.g. 'Commission economics'",
      "hypothesis": "If we lead with X for audience Y, we expect Z",
      "rationale": "why this angle, tied to a pain point and the evidence"
    },
    ...
  ]
}"""


def strategy_block(strategy: dict) -> str:
    lines = [
        "## Approved strategy",
        f"- Core message: {strategy['core_message']}",
        f"- Positioning: {strategy['positioning']}",
        f"- Hypothesis: {strategy['hypothesis']}",
        "",
        "### Pain points",
        *[f"- {p}" for p in strategy["pain_points"]],
        "",
        "### Benefits",
        *[f"- {b}" for b in strategy["benefits"]],
        "",
        "### Proof points (all pre-approved)",
        *[f"- {p}" for p in strategy["proof_points"]],
    ]
    return "\n".join(lines)


def validate(data: dict) -> list[str]:
    problems = []
    angles = data.get("angles")
    if not isinstance(angles, list) or not 3 <= len(angles or []) <= 5:
        problems.append("angles must be a list of 3 to 5")
        return problems
    names = []
    for i, a in enumerate(angles):
        for key in ("name", "hypothesis", "rationale"):
            if not (a or {}).get(key):
                problems.append(f"angles[{i}] missing {key}")
        if a.get("name"):
            names.append(a["name"].strip().lower())
    if len(set(names)) != len(names):
        problems.append("angle names must be distinct")
    return problems


async def generate(campaign_ref: str, *, provider: str | None = None,
                   show_prompt: bool = False) -> dict:
    campaign = await load_campaign(campaign_ref)
    strategy = await approved_strategy(campaign["id"])

    ctx = await context_for(
        campaign,
        stage_query=f"{campaign['product_name']} motivations objections "
                    f"{' '.join(strategy['pain_points'][:3])}",
    )
    system = to_system_prompt(ctx)
    user = (f"{INSTRUCTIONS}\n\n{brief_block(campaign)}\n\n"
            f"{strategy_block(strategy)}")

    if show_prompt:
        print(system, "\n" + "=" * 74 + "\n", user)
        return {}

    data = await acomplete_json(system, user, shape_hint=SHAPE_HINT,
                                provider=provider)
    problems = validate(data)
    if problems:
        data = await acomplete_json(
                                    system,
                                    f"{user}\n\nYour previous answer had structural problems: "
                                    f"{'; '.join(problems)}. Produce the complete object.",
                                    shape_hint=SHAPE_HINT, provider=provider)
        problems = validate(data)
        if problems:
            raise RuntimeError(f"angles failed validation twice: {problems}")

    async with cursor() as cur:
        # Replace the standing proposal set; keep decided rows.
        await cur.execute(
            "delete from public.campaign_angles "
            "where strategy_id = %s and status = 'draft'",
            (strategy["id"],))
        replaced = cur.rowcount

        written = []
        for pos, a in enumerate(data["angles"]):
            await cur.execute(
                "insert into public.campaign_angles "
                "  (campaign_id, strategy_id, name, hypothesis, rationale, "
                "   position, status, knowledge_snapshot) "
                "values (%s, %s, %s, %s, %s, %s, 'draft', %s) "
                "on conflict (strategy_id, name) do nothing "
                "returning id, name",
                (campaign["id"], strategy["id"], a["name"].strip(),
                 a["hypothesis"], a["rationale"], pos,
                 as_jsonb(ctx["snapshot"])))
            row = await cur.fetchone()
            if row:
                written.append({"id": str(row["id"]), "name": row["name"]})
            # A None row means the model re-proposed an angle that is already
            # approved or rejected under the same name -- which is fine: the
            # decision stands and the duplicate proposal is discarded.

    return {
        "campaign": campaign["name"],
        "strategy_version": strategy["version_number"],
        "drafts_replaced": replaced,
        "angles": written,
        "hypotheses": {a["name"]: next(
            x["hypothesis"] for x in data["angles"]
            if x["name"].strip() == a["name"]) for a in written},
    }


async def decide(angle_id: str, status: str, decided_by: str) -> dict:
    """Approve or reject one angle, recording who decided.

    decided_by is required, not optional with a default: 016's CHECK rejects
    any non-draft row without it, and a default would let a caller that forgot
    to pass an identity keep working while writing an unattributed decision.

    Rejection is attributed as well as approval. An angle rejection removes a
    whole line of argument from the campaign, which is as much an editorial
    decision as clearing one.
    """
    assert status in ("approved", "rejected")
    async with cursor() as cur:
        await cur.execute(
            "update public.campaign_angles "
            "set status = %s, decided_by = %s, decided_at = now() "
            "where id = %s returning id, name, status, decided_by",
            (status, decided_by, angle_id))
        row = await cur.fetchone()
    if not row:
        raise LookupError(f"no angle {angle_id}")
    return {"id": str(row["id"]), "name": row["name"], "status": row["status"],
            "decided_by": row["decided_by"]}


async def amain() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("campaign", help="campaign UUID or exact name")
    ap.add_argument("--provider", default=None, choices=["gemini", "openai"])
    ap.add_argument("--show-prompt", action="store_true")
    ap.add_argument("--approve", metavar="ANGLE_ID", default=None)
    ap.add_argument("--reject", metavar="ANGLE_ID", default=None)
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
