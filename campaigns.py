"""Stage 1: the campaign brief. Read and write.

Nothing owned public.campaigns until now -- the generators and validators all
read it, and the only row in the table was created by a throwaway script. The
UI needs a real writer, so it lives here rather than inside a route handler.

Deliberately thin. There is no business logic in filing a brief: validation is
stage 3 and has its own module, which is why create() does NOT validate. A
brief is allowed to be wrong; that is what 'draft' means, and finding out how
it is wrong is validation's job rather than a reason to refuse the insert.
"""

from __future__ import annotations

from db import cursor, fetch_all, fetch_one
import lifecycle
from generators.pipeline import (bulk_skip_reason,
                                 newer_pending_versions)

#: Text fields taken verbatim from the form or caller.
TEXT_FIELDS = (
    "name", "objective", "target_audience", "customer_problem", "offer",
    "primary_benefit", "primary_cta", "secondary_cta", "primary_kpi",
    "additional_context",
)

#: text[] columns. Submitted as newline- or comma-separated text by the form.
ARRAY_FIELDS = (
    "supporting_benefits", "proof_points", "channels", "geographic_target",
    # 023. One-off exclusions for this campaign; brand_exclusions
    # holds the standing ones.
    "do_not_mention",
)

LIST_SQL = """
select c.id, c.name, c.status, c.channels, c.created_at, c.updated_at,
       b.slug as brand_slug, b.name as brand_name,
       p.slug as product_slug, p.name as product_name,
       ct.name as campaign_type_name,
       (select count(*) from public.campaign_strategies s
         where s.campaign_id = c.id) as strategies,
       (select count(*) from public.campaign_assets a
         where a.campaign_id = c.id) as assets,
       (select count(*) from public.campaign_assets a
         where a.campaign_id = c.id and a.status = 'approved')
           as approved_assets
from public.campaigns c
join public.brands b on b.id = c.brand_id
join public.products p on p.id = c.product_id
left join public.campaign_types ct on ct.id = c.campaign_type_id
order by c.created_at desc
"""


def split_list(raw: str | None) -> list[str]:
    """Newlines or commas, whichever the person used.

    Asking someone to remember which separator a field wants is a small
    cruelty in an internal tool, and the failure is silent -- one long string
    where a list was meant.
    """
    if not raw:
        return []
    parts = [p.strip() for chunk in raw.replace("\r", "").split("\n")
             for p in chunk.split(",")]
    return [p for p in parts if p]


async def list_campaigns() -> list[dict]:
    return await fetch_all(LIST_SQL)


async def get(campaign_ref: str) -> dict | None:
    return await fetch_one(
        "select c.*, b.slug as brand_slug, b.name as brand_name, "
        "       p.slug as product_slug, p.name as product_name, "
        "       p.approved_for_marketing as product_marketable, "
        "       ct.slug as campaign_type_slug, ct.name as campaign_type_name, "
        "       ct.prohibited_themes, ct.default_cta "
        "from public.campaigns c "
        "join public.brands b on b.id = c.brand_id "
        "join public.products p on p.id = c.product_id "
        "left join public.campaign_types ct on ct.id = c.campaign_type_id "
        "where c.id::text = %s or c.name = %s",
        (campaign_ref, campaign_ref))


async def form_options() -> dict:
    """Brands, products and campaign types for the brief form's selects.

    Products carry their brand slug and marketability so the form can show
    "(not approved for marketing)" rather than letting someone file a brief
    against a product whose copy validation will then block.
    """
    products = await fetch_all(
        "select p.id, p.slug, p.name, p.approved_for_marketing, "
        "       p.primary_cta, b.slug as brand_slug, b.name as brand_name "
        "from public.products p join public.brands b on b.id = p.brand_id "
        "where p.status = 'active' order by b.slug, p.name")
    types = await fetch_all(
        "select ct.id, ct.slug, ct.name, ct.default_objective, "
        "       ct.default_cta, b.slug as brand_slug "
        "from public.campaign_types ct "
        "join public.brands b on b.id = ct.brand_id "
        "where ct.status = 'active' order by b.slug, ct.name")
    return {"products": products, "campaign_types": types}


async def create(data: dict, *, created_by: str) -> dict:
    """Insert one campaign in 'draft'. Returns the new row.

    status is always 'draft' regardless of what the caller passes: a brief
    becomes 'validated' by passing validation, not by asserting it.

    created_by is keyword-only with no default. campaigns.created_by has
    existed since the table was built and nothing ever wrote to it, so the one
    pre-existing row carries 016's unattributed sentinel. Every brief filed
    from here on names whoever filed it.
    """
    values = {k: (data.get(k) or None) for k in TEXT_FIELDS}
    for k in ARRAY_FIELDS:
        raw = data.get(k)
        values[k] = raw if isinstance(raw, list) else split_list(raw)
    values["product_id"] = data["product_id"]
    values["campaign_type_id"] = data.get("campaign_type_id") or None
    values["start_date"] = data.get("start_date") or None
    values["end_date"] = data.get("end_date") or None
    values["created_by"] = created_by

    async with cursor() as cur:
        await cur.execute(
            "insert into public.campaigns "
            "  (brand_id, product_id, campaign_type_id, name, status, "
            "   objective, target_audience, customer_problem, offer, "
            "   primary_benefit, supporting_benefits, proof_points, "
            "   primary_cta, secondary_cta, channels, primary_kpi, "
            "   geographic_target, start_date, end_date, additional_context, "
            "   created_by) "
            "select p.brand_id, p.id, %(campaign_type_id)s, %(name)s, "
            "       'draft', %(objective)s, %(target_audience)s, "
            "       %(customer_problem)s, %(offer)s, %(primary_benefit)s, "
            "       %(supporting_benefits)s, %(proof_points)s, "
            "       %(primary_cta)s, %(secondary_cta)s, %(channels)s, "
            "       %(primary_kpi)s, %(geographic_target)s, "
            "       %(start_date)s, %(end_date)s, %(additional_context)s, "
            "       %(created_by)s "
            "from public.products p where p.id = %(product_id)s "
            "returning id, name, status",
            values)
        row = await cur.fetchone()
    if not row:
        raise LookupError(f"no product {data['product_id']!r}")
    return row


async def pipeline_state(campaign_id: str) -> dict:
    """Everything the detail page renders, in one place.

    Assembled here rather than in the route so the shape is testable and so
    the template stays a template. The counts drive which buttons are enabled:
    the UI must not offer "Generate Assets" before a concept is approved,
    because the generator would only raise StageNotReady.
    """
    validations = await fetch_all(
        "select validation_number, status, deterministic_status, ai_status, "
        "       blockers, warnings, recommendations, validated_at, "
        "       validated_by "
        "from public.campaign_validations where campaign_id = %s "
        "order by validation_number desc", (campaign_id,))

    strategies = await fetch_all(
        "select id, version_number, status, core_message, positioning, "
        "       pain_points, benefits, proof_points, objections, hypothesis, "
        "       approved_by, approved_at, knowledge_snapshot, created_at "
        "from public.campaign_strategies where campaign_id = %s "
        "order by version_number desc", (campaign_id,))
    approved_strategy = next(
        (s for s in strategies if s["status"] == "approved"), None)

    angles = []
    concepts = []
    if approved_strategy:
        angles = await fetch_all(
            "select id, name, hypothesis, rationale, status, position, "
            "       decided_by, decided_at "
            "from public.campaign_angles where strategy_id = %s "
            "order by position", (approved_strategy["id"],))
        if angles:
            concepts = await fetch_all(
                "select cc.id, cc.idea, cc.hook, cc.visual_direction, "
                "       cc.status, cc.position, cc.angle_id, a.name as angle, "
                "       cc.decided_by, cc.decided_at "
                "from public.creative_concepts cc "
                "join public.campaign_angles a on a.id = cc.angle_id "
                "where cc.angle_id = any(%s) order by a.position, cc.position",
                ([a["id"] for a in angles],))

    assets = await fetch_all(
        "select a.id, a.channel, a.asset_type, a.variant, a.position, "
        "       a.content, a.status, a.version_number, a.approved_by, "
        # rejected_by/notes need 022. The boot preflight names it.
        "       a.rejected_by, a.notes, "
        "       a.knowledge_snapshot, cc.hook as concept_hook, "
        "       q.status as qa_status, q.blockers, q.warnings, "
        "       q.recommendations, q.deterministic_status, q.ai_status, "
        "       q.validated_by as qa_by "
        "from public.campaign_assets a "
        "join public.creative_concepts cc on cc.id = a.concept_id "
        "left join (  "
        "  select distinct on (asset_id) asset_id, status, blockers, "
        "         warnings, recommendations, deterministic_status, ai_status, "
        "         validated_by "
        "  from public.asset_qa_results order by asset_id, qa_number desc"
        ") q on q.asset_id = a.id "
        "where a.campaign_id = %s "
        "order by a.channel, a.variant, a.position nulls first, "
        "         a.version_number", (campaign_id,))

    # Revision requests, fetched once and indexed by target so each item can
    # show its own history without a query per row.
    revisions = await fetch_all(
        "select id, strategy_id, angle_id, concept_id, asset_id, feedback, "
        "       requested_by, requested_at, status, agent_note, "
        "       addressed_at, addressed_by, resulting_version "
        "from public.revision_requests where campaign_id = %s "
        "order by requested_at desc", (campaign_id,))

    by_target: dict[str, list[dict]] = {}
    for r in revisions:
        for col in ("strategy_id", "angle_id", "concept_id", "asset_id"):
            if r[col]:
                by_target.setdefault(str(r[col]), []).append(r)

    def attach(rows: list[dict], table: str,
               qa_key: str | None = None,
               overtaken: dict[str, int] | None = None) -> None:
        """Give each row its revisions and its bulk-approve verdict.

        The verdict comes from generators.pipeline.bulk_skip_reason -- the same
        function approve_many uses -- so the button's count and the action's
        behaviour cannot drift apart.
        """
        for row in rows:
            mine = by_target.get(str(row["id"]), [])
            row["revisions"] = mine
            row["open_revision"] = next(
                (r for r in mine if r["status"] == "open"), None)
            row["bulk_skip_reason"] = bulk_skip_reason(
                table, row["status"],
                row.get(qa_key) if qa_key else None,
                sum(1 for r in mine if r["status"] == "open"),
                (overtaken or {}).get(str(row["id"])))

    attach(strategies, "campaign_strategies")
    attach(angles, "campaign_angles")
    attach(concepts, "creative_concepts")
    # Same helper approve_many uses, so the "Approve all (n)" count
    # and the action agree about which version of a slot is next.
    attach(assets, "campaign_assets", qa_key="qa_status",
           overtaken=newer_pending_versions(
               assets, "review",
               ("channel", "asset_type", "variant", "position")))

    approved_angles = [a for a in angles if a["status"] == "approved"]
    approved_concepts = [c for c in concepts if c["status"] == "approved"]

    def bulk_ready(rows: list[dict]) -> int:
        return sum(1 for r in rows if r["bulk_skip_reason"] is None)

    # Stage 9. The readiness blockers come from lifecycle.approval_readiness --
    # the same function lifecycle.promote enforces -- so the checklist the page
    # shows and the guard the button hits cannot drift apart.
    status = (await fetch_one(
        "select status from public.campaigns where id = %s",
        (campaign_id,)) or {}).get("status", "draft")
    facts = await lifecycle.gather(campaign_id)
    blockers = lifecycle.approval_readiness(facts)

    return {
        "lifecycle": {
            "blockers": [x.as_dict() for x in blockers],
            "ready_to_approve": not blockers,
            "can_go_to": list(lifecycle.MANUAL_MOVES.get(status, ())),
            "earned": await lifecycle.earned_status(campaign_id),
            "history": await lifecycle.history(campaign_id),
            "slots": sorted(
                (lifecycle.slot_label(s), ok) for s, ok in facts.slots.items()),
        },
        "revisions": revisions,
        "open_revisions": [r for r in revisions if r["status"] == "open"],
        "bulk": {
            "angles": bulk_ready(angles),
            "concepts": bulk_ready(concepts),
            "assets": bulk_ready(assets),
        },
        "validations": validations,
        "latest_validation": validations[0] if validations else None,
        "strategies": strategies,
        "approved_strategy": approved_strategy,
        "angles": angles,
        "concepts": concepts,
        "assets": assets,
        "can": {
            # Which buttons are live. Mirrors the generators' own guards so
            # the UI never offers an action that would raise StageNotReady.
            "generate_strategy": True,
            "generate_angles": approved_strategy is not None,
            "generate_concepts": bool(approved_angles),
            "generate_assets": bool(approved_concepts),
            "run_qa": bool(assets),
        },
        "counts": {
            "angles_approved": len(approved_angles),
            "concepts_approved": len(approved_concepts),
            "assets": len(assets),
            "assets_blocked": sum(1 for a in assets
                                  if a["qa_status"] == "blocked"),
            "assets_approved": sum(1 for a in assets
                                   if a["status"] == "approved"),
        },
    }
