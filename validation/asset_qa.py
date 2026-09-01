"""Stage 8: asset QA.

    python -m validation.asset_qa "Franchise Recruitment - Captive Agents Q4 2026"
    python -m validation.asset_qa <ref> --ai
    python -m validation.asset_qa <ref> --dry-run
    python -m validation.asset_qa <ref> --status draft

Writes one asset_qa_results row per asset. Per-asset checks run per asset;
price consistency runs across the whole set, because inconsistency is only
visible in the comparison -- two ads each stating a defensible price, differing
from each other, is exactly what no single-asset check can see.

A cross-set finding is attached to EVERY asset in the set rather than to one
arbitrarily. Whichever asset a reviewer opens, they see the problem.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import auth
from db import cursor, fetch_all, pool
from validation import context as vctx
from validation.ai import ai_review_assets
from validation.checks import (
    check_price_consistency,
    run_asset_checks,
    split,
    status_for,
)

VALIDATOR_VERSION = "asset_qa/1"

ASSETS_SQL = """
select a.id, a.channel, a.asset_type, a.variant, a.position, a.status,
       a.content, a.version_number, a.knowledge_snapshot,
       cc.hook as concept_hook
from public.campaign_assets a
join public.creative_concepts cc on cc.id = a.concept_id
where a.campaign_id = %s
  and (%s::text is null or a.status = %s)
order by a.channel, a.asset_type, a.variant, a.position nulls first
"""


def combined_status(det: str, ai: str | None) -> str:
    """Deterministic blockers always win. See validation.ai for why."""
    if det == "blocked":
        return "blocked"
    if ai in ("blocked", "needs_info"):
        return "needs_info"
    if "warning" in (det, ai):
        return "warning"
    return "pass"


def check_grounding(asset: dict) -> list:
    """An asset whose snapshot cites no chunks and no claims is ungrounded.

    013 requires the kb_chunk_ids key to exist, which proves a snapshot was
    taken. It deliberately does not require it to be non-empty, because copy
    written purely from approved claims is legitimately grounded. That
    distinction needs the claims list too, so it is checked here rather than
    in the schema.
    """
    from validation.checks import Finding
    snap = asset.get("knowledge_snapshot") or {}
    chunks = snap.get("kb_chunk_ids") or []
    claims = snap.get("approved_claims") or []
    if chunks or claims:
        return []
    return [Finding(
        check="ungrounded", severity="blocker",
        message="This asset's knowledge snapshot cites no retrieved passages "
                "and no approved claims, so nothing in it is traceable to "
                "the knowledge base.",
        remedy="Regenerate it; the retrieval step returned nothing usable.",
    )]


async def qa(campaign_ref: str, *, validated_by: str, use_ai: bool = False,
             dry_run: bool = False, status_filter: str | None = None,
             provider: str | None = None) -> dict:
    """Run stage 8 across a campaign's assets. See brief.validate for why
    validated_by is keyword-only with no default."""
    campaign, ctx = await vctx.load(campaign_ref)
    assets = await fetch_all(ASSETS_SQL,
                             (campaign["id"], status_filter, status_filter))
    if not assets:
        return {"campaign": campaign["name"], "assets": 0,
                "note": "no assets matched"}

    # Cross-set checks, computed once and attached to every asset.
    cross = check_price_consistency([a["content"] for a in assets], ctx)

    ai_status = None
    ai_findings: list = []
    ai_results: dict = {}
    if use_ai:
        ai_status, ai_findings, ai_results = await ai_review_assets(
            assets, campaign, ctx, provider=provider)

    per_asset = []
    for asset in assets:
        findings = [
            *run_asset_checks(asset["content"], asset["channel"],
                              asset["asset_type"], ctx),
            *check_grounding(asset),
            *cross,
        ]
        det_status = status_for(findings)
        combined = combined_status(det_status, ai_status)
        buckets = split(findings + ai_findings)
        per_asset.append({
            "asset_id": str(asset["id"]),
            "label": f"{asset['channel']}/{asset['variant']}"
                     + (f"#{asset['position']}" if asset["position"] else ""),
            "status": combined,
            "deterministic_status": det_status,
            "ai_status": ai_status,
            "buckets": buckets,
            "counts": {k: len(v) for k, v in buckets.items()},
        })

    summary = {
        "campaign": campaign["name"],
        "assets": len(assets),
        "blocked": sum(1 for a in per_asset if a["status"] == "blocked"),
        "warning": sum(1 for a in per_asset if a["status"] == "warning"),
        "needs_info": sum(1 for a in per_asset if a["status"] == "needs_info"),
        "pass": sum(1 for a in per_asset if a["status"] == "pass"),
        "results": [{k: v for k, v in a.items() if k != "buckets"}
                    | {"blockers": a["buckets"]["blockers"],
                       "warnings": a["buckets"]["warnings"]}
                    for a in per_asset],
    }

    if dry_run:
        summary["written"] = False
        return summary

    async with cursor() as cur:
        for a in per_asset:
            await cur.execute(
                "select coalesce(max(qa_number), 0) + 1 as n "
                "from public.asset_qa_results where asset_id = %s",
                (a["asset_id"],))
            number = (await cur.fetchone())["n"]
            await cur.execute(
                "insert into public.asset_qa_results "
                "  (asset_id, qa_number, status, deterministic_status, "
                "   ai_status, blockers, warnings, recommendations, "
                "   deterministic_results, ai_results, validator_version, "
                "   validated_by) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (a["asset_id"], number, a["status"],
                 a["deterministic_status"], a["ai_status"],
                 json.dumps(a["buckets"]["blockers"]),
                 json.dumps(a["buckets"]["warnings"]),
                 json.dumps(a["buckets"]["recommendations"]),
                 json.dumps({"counts": a["counts"]}),
                 json.dumps(ai_results),
                 VALIDATOR_VERSION, validated_by))
            # A blocked asset is moved out of draft so it cannot be approved
            # by someone reading only the status column.
            if a["status"] == "blocked":
                await cur.execute(
                    "update public.campaign_assets set status = 'rejected' "
                    "where id = %s and status = 'draft'", (a["asset_id"],))
            elif a["status"] in ("pass", "warning"):
                await cur.execute(
                    "update public.campaign_assets set status = 'review' "
                    "where id = %s and status = 'draft'", (a["asset_id"],))

    summary["written"] = True
    return summary


async def amain() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("campaign")
    ap.add_argument("--ai", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", default=None,
                    help="only QA assets with this status")
    ap.add_argument("--provider", default=None, choices=["gemini", "openai"])
    ap.add_argument("--as", dest="operator", default=None,
                    help="who to record the run as; defaults to cli:<os user>")
    args = ap.parse_args()

    await pool.open()
    try:
        result = await qa(args.campaign,
                          validated_by=auth.cli_operator(args.operator),
                          use_ai=args.ai, dry_run=args.dry_run,
                          status_filter=args.status, provider=args.provider)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 1 if result.get("blocked") else 0
    finally:
        await pool.close()


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())
