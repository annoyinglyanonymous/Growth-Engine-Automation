"""Stage 3: brief validation.

    python -m validation.brief "Franchise Recruitment - Captive Agents Q4 2026"
    python -m validation.brief <ref> --ai        # add the judgement tier
    python -m validation.brief <ref> --dry-run   # report, write nothing

Writes a campaign_validations row and moves campaigns.status. The brief itself
is checked as if it were copy -- because it is: its offer, primary_benefit and
proof_points end up quoted in assets, so a prohibited figure in the brief
becomes a prohibited figure in every asset generated from it.

STATUS TRANSITIONS
  blocked     -> campaigns.status = 'blocked'
  needs_info  -> 'needs_info'   (AI tier only; missing context, not wrong)
  warning     -> 'validated'    (proceed, with the warnings recorded)
  pass        -> 'validated'

A pass never moves a campaign BACKWARDS -- one already at 'review'
stays there. Every move is recorded in campaign_status_events; a
re-run that lands on the status it already had is not a move.

A warning does not stop the pipeline. Blocking on style would train people to
skip validation, which costs more than the warnings catch.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import auth
import lifecycle
from db import cursor, fetch_one, pool
from validation import context as vctx
from validation.ai import ai_review_brief
from validation.checks import (
    check_missing_brief_fields,
    check_prohibited_wording,
    run_asset_checks,
    split,
    status_for,
)

VALIDATOR_VERSION = "brief/1"

#: The brief fields that become copy. Checked with the same machinery as an
#: asset, because that is what they turn into.
_COPY_FIELDS = ("objective", "target_audience", "customer_problem", "offer",
                "primary_benefit", "primary_cta", "secondary_cta",
                "additional_context")


def brief_as_content(campaign: dict) -> dict:
    content = {k: campaign[k] for k in _COPY_FIELDS
               if isinstance(campaign.get(k), str) and campaign[k]}
    # Array fields flattened into one pseudo-field each: a prohibited figure
    # in proof_points[2] is still a prohibited figure, and naming the array is
    # specific enough for a reviewer to find it.
    for key in ("supporting_benefits", "proof_points"):
        vals = campaign.get(key) or []
        if vals:
            content[key] = " | ".join(vals)
    return content


def combined_status(det: str, ai: str | None) -> str:
    """The overall status. Deterministic blockers always win.

    The AI tier can add needs_info and can raise pass to warning, but it can
    never clear a deterministic blocker. A model's opinion must not be able to
    unblock a constraint violation -- that is the whole reason the two tiers
    are separate columns in the schema.
    """
    if det == "blocked":
        return "blocked"
    if ai == "blocked":
        # An AI blocker is real but softer: it means "a human must look".
        return "needs_info"
    if ai == "needs_info":
        return "needs_info"
    if "warning" in (det, ai):
        return "warning"
    return "pass"


async def validate(campaign_ref: str, *, validated_by: str,
                   use_ai: bool = False, dry_run: bool = False,
                   provider: str | None = None) -> dict:
    """Run stage 3 for one campaign.

    validated_by is keyword-only and has no default. 016 made the column NOT
    NULL precisely so a caller that forgets cannot quietly write an
    unattributed run -- a default here would reintroduce that.
    """
    campaign, ctx = await vctx.load(campaign_ref)
    content = brief_as_content(campaign)

    findings = [
        *check_missing_brief_fields(ctx),
        *check_prohibited_wording(content, ctx),
        # Campaign-type mixing, CTA and features apply to the brief too; the
        # character-limit check does not (a brief has no channel), so
        # run_asset_checks is called with a channel that has no limits and
        # returns an info finding saying so, which is then dropped.
        *[f for f in run_asset_checks(content, "brief", "brief", ctx)
          if f.check != "character_limits"],
    ]
    det_status = status_for(findings)

    ai_status = None
    ai_results: dict = {}
    if use_ai:
        ai_status, ai_findings, ai_results = await ai_review_brief(
            campaign, ctx, provider=provider)
        findings.extend(ai_findings)

    overall = combined_status(det_status, ai_status)
    buckets = split(findings)

    result = {
        "campaign": campaign["name"],
        "status": overall,
        "deterministic_status": det_status,
        "ai_status": ai_status,
        **buckets,
        "counts": {k: len(v) for k, v in buckets.items()},
    }
    if dry_run:
        result["written"] = False
        return result

    next_status = {"blocked": "blocked", "needs_info": "needs_info",
                   "warning": "validated", "pass": "validated"}[overall]

    # The status BEFORE this run. Needed twice -- to decide whether a
    # passing re-validation would move the campaign backwards, and to record
    # the transition -- so it is read once here rather than inside the branch.
    current = (await fetch_one(
        "select status from public.campaigns where id = %s",
        (campaign["id"],)) or {}).get("status")

    # A PASSING re-validation must not drag a campaign backwards. Once 021 made
    # the later statuses reachable, re-validating a campaign sitting at
    # 'review' would have set it to 'validated' and lifecycle.advance would
    # then have moved it forward again -- correct final state, two spurious
    # rows in the status history, and a window where the campaign read as less
    # progressed than it was.
    #
    # A FAILING validation still moves it: blocked means blocked, wherever the
    # campaign had got to. That asymmetry is the point -- bad news travels and
    # good news does not undo progress.
    if next_status == "validated" and current not in (
            None, "draft", "validating", "blocked", "needs_info",
            "validated"):
        next_status = current

    async with cursor() as cur:
        await cur.execute(
            "select coalesce(max(validation_number), 0) + 1 as n "
            "from public.campaign_validations where campaign_id = %s",
            (campaign["id"],))
        number = (await cur.fetchone())["n"]
        await cur.execute(
            "insert into public.campaign_validations "
            "  (campaign_id, validation_number, status, "
            "   deterministic_status, ai_status, blockers, warnings, "
            "   recommendations, deterministic_results, ai_results, "
            "   validator_version, validated_by) "
            "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "returning id",
            (campaign["id"], number, overall, det_status, ai_status,
             json.dumps(buckets["blockers"]),
             json.dumps(buckets["warnings"]),
             json.dumps(buckets["recommendations"]),
             json.dumps({"checks_run": sorted({f.check for f in findings})
                         or ["all_clear"]}),
             json.dumps(ai_results),
             VALIDATOR_VERSION, validated_by))
        row = await cur.fetchone()

        # Stage 3 moves the campaign, so stage 3 records the move --
        # in this same transaction, so a status and the event
        # explaining it cannot come apart. Without this the history
        # had a hole exactly where a reviewer looks hardest:
        # 'blocked' arrived with no row saying who ran the
        # validation that blocked it, or when.
        #
        # automatic=True: a person clicked Validate, but nobody
        # CHOSE 'blocked' -- the checks derived it, which is the
        # same character as lifecycle.advance. changed_by still
        # records who ran it.
        if next_status != current:
            await cur.execute(
                "update public.campaigns set status = %s "
                "where id = %s",
                (next_status, campaign["id"]))
            await lifecycle.record_transition(
                cur, campaign["id"], current, next_status,
                validated_by, True,
                f"validation #{number}: {overall}")

    result["validation_id"] = str(row["id"])
    result["validation_number"] = number
    result["campaign_status"] = next_status
    result["written"] = True
    return result


async def amain() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("campaign")
    ap.add_argument("--ai", action="store_true",
                    help="add the judgement tier (costs one model call)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report findings, write nothing")
    ap.add_argument("--provider", default=None, choices=["gemini", "openai"])
    ap.add_argument("--as", dest="operator", default=None,
                    help="who to record the run as; defaults to cli:<os user>")
    args = ap.parse_args()

    await pool.open()
    try:
        result = await validate(args.campaign,
                                validated_by=auth.cli_operator(args.operator),
                                use_ai=args.ai, dry_run=args.dry_run,
                                provider=args.provider)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 1 if result["status"] == "blocked" else 0
    finally:
        await pool.close()


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())
