"""Assemble a CheckContext from the database.

Split from checks.py so the checks stay pure and testable. This is the only
part of validation that touches SQL.

The claims fetched here are NOT the ones kb_context.fetch_claims returns.
That function applies the product gate -- it withholds approved claims from
products not cleared for marketing, because it is building a prompt and the
model must not assert them. Validation needs the opposite: every governed row,
gate or no gate, because a prohibited claim on an ungated product is still
prohibited, and a check that cannot see it cannot enforce it.
"""

from __future__ import annotations

from db import fetch_all, fetch_one
from validation.checks import CheckContext

CAMPAIGN_SQL = """
select c.*,
       b.slug as brand_slug, b.name as brand_name,
       p.id as product_uuid, p.slug as product_slug, p.name as product_name,
       p.approved_for_marketing as product_marketable,
       ct.slug as campaign_type_slug, ct.name as campaign_type_name,
       ct.allowed_themes, ct.prohibited_themes
from public.campaigns c
join public.brands b on b.id = c.brand_id
join public.products p on p.id = c.product_id
left join public.campaign_types ct on ct.id = c.campaign_type_id
where c.id::text = %s or c.name = %s
"""

#: Every governed claim in scope for the campaign, ungated. See the module
#: docstring for why the product gate is deliberately absent here.
#:
#: Scope is brand-level claims (product_id null) PLUS the campaign's own
#: product. Before 015 this read `where c.product_id = %s`, which was the only
#: option -- and it meant the eight Renegade company facts, filed under
#: franchise-program as a workaround, were the only reason brand-level checking
#: worked at all. Any campaign on a different product missed them entirely: an
#: M&A asset claiming "licensed in all 50 states" would not have been blocked,
#: because that prohibited claim lived under the franchise product.
CLAIMS_SQL = """
select c.status, c.claim_text, c.approved_wording, c.category,
       c.requires_disclaimer, c.disclaimer_text, c.restriction_notes,
       coalesce(c.trigger_phrases, '{}') as trigger_phrases
from public.claims c
where c.brand_id = %s
  and (c.product_id is null or c.product_id = %s)
  and (c.effective_from is null or c.effective_from <= now())
  and (c.effective_until is null or c.effective_until > now())
order by c.status, c.category
"""

FEATURES_SQL = """
select name, slug, available, approved_for_marketing, marketing_notes,
       restrictions
from public.product_features
where product_id = %s and status in ('active', 'coming_soon')
order by name
"""

#: Stale values from every conflict for the brand, resolved or not. A resolved
#: conflict is exactly the case where the stale value must still be caught --
#: $20,000 is resolved AND still published.
#: The operator's standing list for the brand (023). No product
#: scoping and no gate: an exclusion is an instruction about wording,
#: so it applies wherever the wording could appear.
EXCLUSIONS_SQL = """
select e.phrase, e.note
from public.brand_exclusions e
where e.brand_id = %s and e.active
order by e.phrase
"""

STALE_SQL = """
select coalesce(k.stale_values, '{}') as stale_values
from kb.conflicts k
join public.brands b on b.id = k.brand_id
where b.slug = %s
"""


class CampaignNotFound(LookupError):
    pass


async def load(campaign_ref: str) -> tuple[dict, CheckContext]:
    """-> (campaign row, CheckContext). One place, so both stages agree."""
    campaign = await fetch_one(CAMPAIGN_SQL, (campaign_ref, campaign_ref))
    if not campaign:
        known = await fetch_all(
            "select name from public.campaigns order by created_at desc "
            "limit 10")
        raise CampaignNotFound(
            f"no campaign {campaign_ref!r}. recent: "
            f"{[r['name'] for r in known]}")

    claims = await fetch_all(
        CLAIMS_SQL, (campaign["brand_id"], campaign["product_uuid"]))
    features = await fetch_all(FEATURES_SQL, (campaign["product_uuid"],))
    stale_rows = await fetch_all(STALE_SQL, (campaign["brand_slug"],))
    stale = [v for r in stale_rows for v in (r["stale_values"] or [])]

    # Both lists, flattened, each tagged with where it came from.
    # The checks treat them identically; only the message differs.
    standing = await fetch_all(EXCLUSIONS_SQL, (campaign["brand_id"],))
    exclusions = [{"phrase": r["phrase"], "note": r["note"],
                   "scope": "brand"} for r in standing]
    exclusions += [{"phrase": p, "note": None, "scope": "brief"}
                   for p in (campaign.get("do_not_mention") or [])
                   if (p or "").strip()]

    ctx = CheckContext(
        claims=claims,
        features=features,
        campaign=campaign,
        prohibited_themes=list(campaign.get("prohibited_themes") or []),
        allowed_themes=list(campaign.get("allowed_themes") or []),
        stale_values=stale,
        exclusions=exclusions,
    )
    return campaign, ctx
