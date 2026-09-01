"""Stage 7, Meta ads.

    python -m generators.meta_ads "Franchise Recruitment - Captive Agents Q4 2026"
    python -m generators.meta_ads <ref> --concept <concept-uuid>
    python -m generators.meta_ads <ref> --approve <asset-uuid> --approved-by <who>

Writes 2 variants (A/B) per approved concept, each a campaign_assets row with
content = {primary_text, headline, description} as distinct keys -- because
Meta's limits are per FIELD and QA cannot check a concatenation.

THE LIMITS ARE RECOMMENDED DISPLAY LIMITS, NOT API MAXIMA
Meta truncates around these lengths on most placements; the API accepts more.
QA enforces them as warnings, and this generator writes them into the request
so the model aims at them rather than being trimmed afterwards. Trimming ad
copy after generation is editing without judgement.
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

#: Imported, not redefined. validation.checks.CHANNEL_LIMITS is the single
#: source of truth: a generator aiming at one number while QA enforces another
#: produces copy that is rewritten on every QA pass.
LIMITS = CHANNEL_LIMITS[("meta_ads", "meta_ad")]

VARIANTS = ("A", "B")

INSTRUCTIONS = f"""\
Write ONE Meta (Facebook/Instagram) ad from the concept below.

Fields and display limits:
- primary_text: <= {LIMITS['primary_text']} characters. The post text above
  the creative. Lead with the hook's thought, not the company name.
- headline: <= {LIMITS['headline']} characters. Below the creative; carries
  one concrete value point.
- description: <= {LIMITS['description']} characters. Secondary line; may be
  omitted by some placements, so it must not carry anything essential.

Rules:
- Every figure must come from the approved claims. If the concept's hook
  implies a figure that is not approved, express the idea without the number.
- The CTA in the copy must match the campaign's primary CTA in intent -- a
  qualifying conversation, not a purchase or commitment.
- No exclamation-mark pileups, no ALL CAPS words, no emoji unless the concept's
  idea calls for them.
- The variant instruction below says how this variant must differ.
"""

SHAPE_HINT = """\
Keys and types:
{
  "primary_text": "string",
  "headline": "string",
  "description": "string"
}"""

VARIANT_TWIST = {
    "A": "Variant A: lead with the pain point; the benefit resolves it.",
    "B": "Variant B: lead with the strongest approved figure the concept "
         "supports; the pain point is implicit.",
}


def concept_block(concept: dict, angle: dict) -> str:
    return "\n".join([
        "## Concept",
        f"- Angle: {angle['name']} -- {angle['hypothesis']}",
        f"- Idea: {concept['idea']}",
        f"- Hook: {concept['hook']}",
        f"- Visual direction: {concept['visual_direction']}",
    ])


def validate(data: dict) -> list[str]:
    problems = []
    for key, limit in LIMITS.items():
        val = data.get(key)
        if not val or not isinstance(val, str):
            problems.append(f"missing {key}")
        elif len(val) > limit:
            problems.append(f"{key} is {len(val)} chars, limit {limit}")
    return problems


async def approved_concepts(campaign_id: str,
                            concept_id: str | None) -> list[dict]:
    rows = await fetch_all(
        "select cc.*, a.name as angle_name, a.hypothesis as angle_hypothesis "
        "from public.creative_concepts cc "
        "join public.campaign_angles a on a.id = cc.angle_id "
        "where cc.campaign_id = %s and cc.status = 'approved' "
        "  and (%s::text is null or cc.id::text = %s) "
        "order by a.position, cc.position",
        (campaign_id, concept_id, concept_id))
    if not rows:
        raise StageNotReady(
            "no approved concepts. Approve at least one "
            "(python -m generators.concepts <ref> --approve <id>) first -- "
            "013 makes concept_id NOT NULL on assets, so there is no copy "
            "without a concept by construction.")
    return rows


async def generate(campaign_ref: str, *, concept_id: str | None = None,
                   provider: str | None = None,
                   show_prompt: bool = False) -> dict:
    campaign = await load_campaign(campaign_ref)
    if "meta_ads" not in (campaign["channels"] or []):
        raise StageNotReady(
            f"campaign channels are {campaign['channels']}; meta_ads is not "
            f"among them. Generating assets for a channel the brief did not "
            f"ask for is scope creep with a QA cost.")
    await approved_strategy(campaign["id"])  # ordering guard only
    concepts = await approved_concepts(campaign["id"], concept_id)

    ctx = await context_for(
        campaign,
        stage_query=f"{campaign['product_name']} "
                    f"{' '.join(c['hook'] for c in concepts[:3])}",
    )
    system = to_system_prompt(ctx)

    if show_prompt:
        angle = {"name": concepts[0]["angle_name"],
                 "hypothesis": concepts[0]["angle_hypothesis"]}
        print(system, "\n" + "=" * 74 + "\n",
              f"{INSTRUCTIONS}\n{VARIANT_TWIST['A']}\n\n"
              f"{brief_block(campaign)}\n\n"
              f"{concept_block(concepts[0], angle)}")
        return {}

    written = []
    for concept in concepts:
        angle = {"name": concept["angle_name"],
                 "hypothesis": concept["angle_hypothesis"]}
        for variant in VARIANTS:
            user = (f"{INSTRUCTIONS}\n{VARIANT_TWIST[variant]}\n\n"
                    f"{brief_block(campaign)}\n\n"
                    f"{concept_block(concept, angle)}")
            data = await acomplete_json(system, user, shape_hint=SHAPE_HINT,
                                        provider=provider)
            problems = validate(data)
            if problems:
                data = await acomplete_json(
                                            system,
                                            f"{user}\n\nYour previous ad broke these limits: "
                                            f"{'; '.join(problems)}. Rewrite within the limits -- "
                                            f"tighter phrasing, not truncation.",
                                            shape_hint=SHAPE_HINT, provider=provider)
                problems = validate(data)
                if problems:
                    raise RuntimeError(
                        f"variant {variant} of concept {concept['id']} broke "
                        f"limits twice: {problems}")

            version = await next_version("campaign_assets", campaign["id"])
            async with cursor() as cur:
                await cur.execute(
                    "insert into public.campaign_assets "
                    "  (campaign_id, concept_id, channel, asset_type, "
                    "   variant, position, content, version_number, status, "
                    "   knowledge_snapshot, generator_version) "
                    "values (%s, %s, 'meta_ads', 'meta_ad', %s, null, %s, "
                    "        %s, 'draft', %s, 'meta_ads/1') "
                    "returning id",
                    (campaign["id"], concept["id"], variant, as_jsonb(data),
                     version, as_jsonb(ctx["snapshot"])))
                row = await cur.fetchone()
            written.append({
                "asset_id": str(row["id"]),
                "concept": concept["hook"][:48],
                "variant": variant,
                "version": version,
                "headline": data["headline"],
                "primary_text": data["primary_text"],
                "lengths": {k: len(data[k]) for k in LIMITS},
            })

    return {"campaign": campaign["name"], "assets": written}


async def amain() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("campaign", help="campaign UUID or exact name")
    ap.add_argument("--concept", default=None,
                    help="generate for this one approved concept only")
    ap.add_argument("--provider", default=None, choices=["gemini", "openai"])
    ap.add_argument("--show-prompt", action="store_true")
    ap.add_argument("--approve", metavar="ASSET_ID", default=None)
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
