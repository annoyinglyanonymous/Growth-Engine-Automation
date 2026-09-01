"""Shared plumbing for the stage generators.

What lives here and why:

  load_campaign      one JOIN, reused by every stage, and the place where
                     "which product, which campaign type" is resolved once.
  next_version       version numbers are allocated from the database, not
                     counted in Python, so two concurrent generations cannot
                     both write V2.
  approve            the two-step dance (supersede the old, approve the new)
                     in one transaction, matching the partial unique index in
                     013 that allows at most one approved row at a time.
  context_for        the one place a campaign is turned into a retrieval
                     query and a build_context call.

Generators are async because build_context is async; they run on db.py's pool
and therefore under the same Windows selector-loop requirement as main.py --
import db before creating any event loop.
"""

from __future__ import annotations

import json

from db import cursor, fetch_all, fetch_one
from kb_context import build_context


class CampaignNotFound(LookupError):
    pass


class StageNotReady(RuntimeError):
    """The upstream stage this generator depends on has no approved row.

    Named loudly because the pipeline's ordering is a process guarantee: angles
    from an unapproved strategy are angles someone will have to regenerate, and
    assets from an unapproved concept violate the PDF's core rule. The schema
    enforces existence (NOT NULL foreign keys); this enforces approval.
    """


async def load_campaign(campaign_ref: str) -> dict:
    """Campaign plus its brand, product and campaign-type context.

    campaign_ref is a UUID or an exact name. Name lookup exists for the CLI --
    nobody types UUIDs -- but is exact rather than fuzzy: generating against
    the wrong campaign because of a LIKE match would be expensive.
    """
    row = await fetch_one(
        "select c.*, "
        "       b.slug as brand_slug, b.name as brand_name, "
        "       p.slug as product_slug, p.name as product_name, "
        "       p.approved_for_marketing as product_marketable, "
        "       ct.slug as campaign_type_slug, ct.name as campaign_type_name, "
        "       ct.allowed_themes, ct.prohibited_themes "
        "from public.campaigns c "
        "join public.brands b on b.id = c.brand_id "
        "join public.products p on p.id = c.product_id "
        "left join public.campaign_types ct on ct.id = c.campaign_type_id "
        "where c.id::text = %s or c.name = %s",
        (campaign_ref, campaign_ref),
    )
    if not row:
        known = await fetch_all(
            "select name, status from public.campaigns order by created_at desc "
            "limit 10")
        raise CampaignNotFound(
            f"no campaign {campaign_ref!r}. recent: "
            f"{[(r['name'], r['status']) for r in known]}")
    return row


async def next_version(table: str, campaign_id: str) -> int:
    """max(version_number) + 1, from the database at write time.

    table is interpolated from a fixed allowlist -- it arrives from our own
    code, but string-formatting SQL is a habit that should hurt a little.
    """
    assert table in ("campaign_strategies", "campaign_assets"), table
    row = await fetch_one(
        f"select coalesce(max(version_number), 0) + 1 as v "
        f"from public.{table} where campaign_id = %s",
        (campaign_id,),
    )
    return row["v"]


async def context_for(campaign: dict, *, stage_query: str,
                      token_budget: int | None = None) -> dict:
    """build_context, scoped the way a campaign should be.

    The query is the stage's own question plus the brief's audience and
    problem, because those two fields carry the campaign's actual vocabulary.
    product_slug scopes claims to the campaign's product -- a franchise brief
    must not see M&A claims (the product gate in kb_context.fetch_claims
    handles approval; this handles relevance).
    """
    query = " ".join(filter(None, [
        stage_query,
        campaign.get("target_audience") or "",
        campaign.get("customer_problem") or "",
    ]))[:500]
    return await build_context(
        campaign["brand_slug"], query,
        token_budget=token_budget,
        product_slug=campaign["product_slug"],
    )


def brief_block(campaign: dict) -> str:
    """The stage-1 brief, rendered for a user message.

    Every generator sends this: the strategy exists to serve THIS brief, not a
    generic one for the product. Only fields with content are rendered --
    "Offer: None" teaches the model to write 'None' into copy.
    """
    fields = [
        ("Campaign", campaign.get("name")),
        ("Campaign type", campaign.get("campaign_type_name")),
        ("Objective", campaign.get("objective")),
        ("Target audience", campaign.get("target_audience")),
        ("Customer problem", campaign.get("customer_problem")),
        ("Offer", campaign.get("offer")),
        ("Primary benefit", campaign.get("primary_benefit")),
        ("Supporting benefits", ", ".join(campaign.get("supporting_benefits")
                                          or [])),
        ("Proof points", ", ".join(campaign.get("proof_points") or [])),
        ("Primary CTA", campaign.get("primary_cta")),
        ("Secondary CTA", campaign.get("secondary_cta")),
        ("Channels", ", ".join(campaign.get("channels") or [])),
        ("Primary KPI", campaign.get("primary_kpi")),
        ("Geographic target", ", ".join(campaign.get("geographic_target")
                                        or [])),
        ("Additional context", campaign.get("additional_context")),
    ]
    lines = ["## Campaign brief"]
    lines += [f"- {label}: {value}" for label, value in fields if value]

    if campaign.get("prohibited_themes"):
        lines += ["", "## Themes prohibited for this campaign type",
                  "Any of these appearing in output is a blocking error:"]
        lines += [f"- {t}" for t in campaign["prohibited_themes"]]
    return "\n".join(lines)


def as_jsonb(value) -> str:
    """psycopg adapts dicts natively only with the jsonb adapter registered;
    explicit dumps keeps the write sites obvious and identical."""
    return json.dumps(value, ensure_ascii=False)


#: What "the previously approved version" means, per table. It is NOT the same
#: scope: a campaign has one approved strategy, but one approved asset PER SLOT
#: (channel, type, variant, sequence position) -- that is exactly what 013's
#: partial unique indexes say. A campaign-wide supersede on assets would retire
#: the whole approved asset pack the moment one ad variant is re-approved.
_SUPERSEDE_SCOPE = {
    "campaign_strategies": ("campaign_id",),
    "campaign_assets": ("campaign_id", "channel", "asset_type", "variant",
                        "position"),
}


async def approve(table: str, row_id: str, approved_by: str) -> dict:
    """Approve one version; supersede whatever was approved in its scope.

    One transaction, matching 013's partial unique indexes (at most one
    approved row per scope). Doing the supersede first is not optional: with
    the index in place, approving the new row while the old one is still
    approved is a constraint violation.

    approved_by is recorded because 013's CHECK requires it: an approval with
    no approver is not an approval.
    """
    scope = _SUPERSEDE_SCOPE[table]  # KeyError for an unknown table is correct
    async with cursor() as cur:
        await cur.execute(
            f"select status, {', '.join(scope)} from public.{table} "
            f"where id = %s for update", (row_id,))
        row = await cur.fetchone()
        if not row:
            raise LookupError(f"no {table} row {row_id}")
        if row["status"] == "approved":
            return {"id": row_id, "already": True}

        # "col is not distinct from %s" rather than "=", because position is
        # NULL for non-sequence assets and NULL = NULL is not true.
        where = " and ".join(f"{c} is not distinct from %s" for c in scope)
        await cur.execute(
            f"update public.{table} set status = 'superseded' "
            f"where status = 'approved' and {where}",
            tuple(row[c] for c in scope))
        superseded = cur.rowcount

        await cur.execute(
            f"update public.{table} "
            f"set status = 'approved', approved_by = %s, approved_at = now() "
            f"where id = %s", (approved_by, row_id))
    return {"id": row_id, "superseded": superseded}


async def approved_strategy(campaign_id: str) -> dict:
    row = await fetch_one(
        "select * from public.campaign_strategies "
        "where campaign_id = %s and status = 'approved'",
        (campaign_id,),
    )
    if not row:
        raise StageNotReady(
            "no approved strategy for this campaign. Generate one and approve "
            "it first: angles derived from a draft strategy get regenerated "
            "when the strategy changes, which is the wasted pass this check "
            "prevents.")
    return row
