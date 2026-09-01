"""The judgement tier.

Only what deterministic checks cannot answer: whether the messaging conflicts
with itself, whether assets across channels tell the same story, whether two
concepts are really the same idea, whether a claim is technically approved but
misleading in context.

WHAT THIS TIER MAY NOT DO
It cannot clear a deterministic blocker. brief.combined_status and
asset_qa.combined_status both give the deterministic verdict precedence, and
an AI 'blocked' becomes 'needs_info' -- meaning "a human should look" rather
than "this is definitely wrong". A model that could unblock a constraint
violation would make the constraint advisory.

It is also opt-in (--ai) because it costs a model call per object and adds a
failure mode -- a rate limit during QA should not block a release.
"""

from __future__ import annotations

import asyncio

from generators.prompting import complete_json
from validation.checks import CheckContext, Finding

SHAPE_HINT = """\
Keys and types:
{
  "verdict": "pass" | "warning" | "blocked" | "needs_info",
  "findings": [
    {
      "severity": "blocker" | "warning" | "info",
      "issue": "what is wrong, one sentence",
      "field": "which field, or null",
      "remedy": "what to do instead"
    },
    ...
  ]
}"""

_BRIEF_TASK = """\
Review this campaign brief for problems a rule cannot catch. Judge only:

1. Internal contradiction -- does the offer contradict the primary benefit, or
   the audience contradict the problem?
2. Audience/message mismatch -- would this benefit actually move this audience?
3. Unfalsifiable or missing success condition -- can the KPI be affected by
   this campaign at all?
4. Misleading-in-context -- a claim that is individually approved but which
   this framing makes misleading.

Do NOT report: missing fields, prohibited figures, character limits, campaign
type mixing, or CTA mismatches. Those are checked deterministically and
reporting them here creates duplicate noise.

Return verdict 'pass' if you find nothing of substance. Do not invent problems
to appear thorough: a false warning costs a reviewer's trust in this whole
step.
"""

_ASSET_TASK = """\
Review this asset set for problems a rule cannot catch. Judge only:

1. Message match across the set -- do these assets tell one coherent story, or
   several competing ones?
2. Duplicate ideas wearing different words -- two variants that are the same
   ad, which defeats A/B testing.
3. Misleading-in-context -- an approved claim framed so it implies more than
   it says.
4. Tone or register failures for the stated audience.

Do NOT report: prohibited figures, character limits, price inconsistency,
campaign type mixing, unavailable features, or CTA mismatches. Those are
checked deterministically.

Return verdict 'pass' if you find nothing of substance.
"""

_SEVERITY = {"blocker", "warning", "info"}
_VERDICT = {"pass", "warning", "blocked", "needs_info"}


def _to_findings(data: dict, check: str) -> list[Finding]:
    out = []
    for item in data.get("findings") or []:
        if not isinstance(item, dict) or not item.get("issue"):
            continue
        severity = item.get("severity")
        if severity not in _SEVERITY:
            severity = "warning"
        out.append(Finding(
            check=check,
            # An AI blocker is recorded as a warning in the finding list: the
            # verdict column carries its weight, and mixing model opinions
            # into the blockers bucket would make the deterministic blockers
            # harder to trust at a glance.
            severity="warning" if severity == "blocker" else severity,
            message=item["issue"],
            field_name=item.get("field") or None,
            remedy=item.get("remedy") or None,
        ))
    return out


def _brief_block(campaign: dict) -> str:
    keys = ("name", "objective", "target_audience", "customer_problem",
            "offer", "primary_benefit", "primary_cta", "primary_kpi")
    lines = [f"- {k}: {campaign.get(k)}" for k in keys if campaign.get(k)]
    for key in ("supporting_benefits", "proof_points", "channels"):
        if campaign.get(key):
            lines.append(f"- {key}: {', '.join(campaign[key])}")
    return "## Brief\n" + "\n".join(lines)


def _governance_block(ctx: CheckContext) -> str:
    approved = [c for c in ctx.claims if c["status"] == "approved"]
    lines = ["## Approved claims (the only assertable figures)"]
    lines += [f"- {c['approved_wording'] or c['claim_text']}" for c in approved]
    return "\n".join(lines)


async def ai_review_brief(campaign: dict, ctx: CheckContext, *,
                          provider: str | None = None
                          ) -> tuple[str, list[Finding], dict]:
    system = ("You are a marketing reviewer. You are precise, you do not "
              "invent problems, and you never repeat a check that a "
              "deterministic rule already performs.")
    user = f"{_BRIEF_TASK}\n\n{_brief_block(campaign)}\n\n" \
           f"{_governance_block(ctx)}"
    return await _run(system, user, "ai_brief_review", provider)


async def ai_review_assets(assets: list[dict], campaign: dict,
                           ctx: CheckContext, *,
                           provider: str | None = None
                           ) -> tuple[str, list[Finding], dict]:
    lines = ["## Assets"]
    for a in assets:
        pos = f" #{a['position']}" if a.get("position") else ""
        lines.append(f"### {a['channel']}/{a['asset_type']} "
                     f"variant {a['variant']}{pos}")
        for k, v in (a["content"] or {}).items():
            lines.append(f"- {k}: {v}")
        lines.append("")
    user = (f"{_ASSET_TASK}\n\n{_brief_block(campaign)}\n\n"
            f"{_governance_block(ctx)}\n\n" + "\n".join(lines))
    system = ("You are a marketing reviewer. You are precise, you do not "
              "invent problems, and you never repeat a check that a "
              "deterministic rule already performs.")
    return await _run(system, user, "ai_asset_review", provider)


async def _run(system: str, user: str, check: str,
               provider: str | None) -> tuple[str, list[Finding], dict]:
    """The model call, off the event loop, with failure degrading to skipped.

    A rate limit or a bad key must not block a release: the deterministic tier
    already ran and its verdict stands. The returned ai_status is None so the
    stored row says "not assessed" rather than "passed".
    """
    try:
        data = await asyncio.to_thread(
            complete_json, system, user, shape_hint=SHAPE_HINT,
            provider=provider)
    except Exception as exc:  # noqa: BLE001 -- degrade, never block
        return None, [], {"skipped": f"{type(exc).__name__}: {exc}"[:200]}

    verdict = data.get("verdict")
    if verdict not in _VERDICT:
        verdict = "warning" if data.get("findings") else "pass"
    findings = _to_findings(data, check)
    return verdict, findings, {"verdict": verdict,
                               "raw_findings": data.get("findings") or []}
