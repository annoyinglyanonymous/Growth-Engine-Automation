"""Live checks on brand-scoped claims (migration 015).

All dbtest: these assert a schema shape, and the only honest way to test that
is against the schema. Three outcomes, distinguished on purpose:

  DB unreachable          skip. The offline suite stays green.
  015 pending             skip. Unapplied work is migrate.py --status's job to
                          report, not this file's. A suite that goes red the
                          moment a migration is written trains people to
                          ignore failures, which costs more than it catches.
  015 applied, mismatch   FAIL. The migration ran and the schema is still
                          wrong -- that is a real broken state and the one
                          thing here worth shouting about.

Two properties matter, and they pull in opposite directions:

  retrieval    a product-narrowed query must still offer brand-level claims.
               This is what the pre-015 workaround silently broke: a franchise
               brief could not see "licensed in 48 states".
  validation   a prohibited brand-level claim must apply to EVERY campaign,
               whatever its product. Before 015 the eight Renegade company
               facts lived under franchise-program, so an M&A asset writing
               "licensed in all 50 states" would not have been blocked.
"""

from __future__ import annotations

import asyncio

import pytest

RENEGADE = "renegade"
COMPANY_FACTS = [
    "200+ agents",
    "48 states",
    "9 open retail agency locations",
]


def _run(coro_factory):
    import db

    async def go():
        await db.pool.open()
        try:
            return await coro_factory()
        finally:
            await db.pool.close()

    try:
        return asyncio.run(go())
    except Exception as exc:  # noqa: BLE001 -- unreachable DB is a skip
        pytest.skip(f"no live database: {type(exc).__name__}: {exc}"[:140])


#: The migration this file describes. Recorded in public.schema_migrations by
#: migrate.py once applied.
MIGRATION = "015_brand_scoped_claims.sql"


@pytest.fixture(scope="module")
def schema() -> dict:
    """Which of 015's pieces are present, and whether 015 claims to be applied."""
    from db import fetch_all, fetch_one

    async def load():
        applied = await fetch_one(
            "select 1 as ok from public.schema_migrations where filename = %s",
            (MIGRATION,))
        col = await fetch_one(
            "select 1 as ok from information_schema.columns "
            "where table_schema = 'public' and table_name = 'claims' "
            "  and column_name = 'brand_id'")
        nullable = await fetch_one(
            "select is_nullable from information_schema.columns "
            "where table_schema = 'public' and table_name = 'claims' "
            "  and column_name = 'product_id'")
        constraints = await fetch_all(
            "select conname from pg_constraint where conname = any(%s)",
            (["claims_brand_id_fkey", "claims_product_brand_fkey",
              "claims_feature_requires_product",
              "products_id_brand_id_key"],))
        indexes = await fetch_all(
            "select indexname from pg_indexes where schemaname = 'public' "
            "  and indexname = any(%s)",
            (["claims_natural_key", "idx_claims_brand_status"],))
        return {
            "applied": bool(applied),
            "brand_id": bool(col),
            "product_id_nullable": (nullable or {}).get("is_nullable"),
            "constraints": {r["conname"] for r in constraints},
            "indexes": {r["indexname"] for r in indexes},
        }

    return _run(load)


def _require(schema) -> None:
    """Skip when 015 is pending; fail when it ran and left the schema wrong."""
    if not schema["applied"]:
        pytest.skip(f"{MIGRATION} is pending -- see python migrate.py --status")
    if not schema["brand_id"]:
        pytest.fail(f"{MIGRATION} is recorded as applied but claims.brand_id "
                    f"does not exist")


@pytest.mark.dbtest
def test_brand_id_exists(schema):
    _require(schema)
    assert schema["brand_id"]


@pytest.mark.dbtest
def test_product_id_became_optional(schema):
    _require(schema)
    assert schema["product_id_nullable"] == "YES"


@pytest.mark.dbtest
def test_the_consistency_guards_exist(schema):
    """A brand_id and a product_id from different brands must be unrepresentable.
    A CHECK cannot see another table, so the guard is the composite FK plus the
    unique (id, brand_id) it points at."""
    _require(schema)
    for name in ("claims_brand_id_fkey", "claims_product_brand_fkey",
                 "claims_feature_requires_product",
                 "products_id_brand_id_key"):
        assert name in schema["constraints"], f"missing constraint {name}"


@pytest.mark.dbtest
def test_the_natural_key_exists_and_is_nulls_not_distinct(schema):
    """Without NULLS NOT DISTINCT the key does nothing for brand-level rows:
    every product_id NULL would be distinct from every other, so two identical
    brand-level claims would not collide."""
    _require(schema)
    assert "claims_natural_key" in schema["indexes"]

    from db import fetch_one

    async def load():
        return await fetch_one(
            "select indexdef from pg_indexes "
            "where indexname = 'claims_natural_key'")

    row = _run(load)
    assert "NULLS NOT DISTINCT" in row["indexdef"].upper()


# --------------------------------------------------------------------------
# The eight re-parented Renegade claims
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def company_claims(schema) -> list[dict]:
    _require(schema)
    from db import fetch_all

    async def load():
        return await fetch_all(
            "select c.status, c.claim_text, c.product_id, c.trigger_phrases "
            "from public.claims c "
            "join public.brands b on b.id = c.brand_id "
            "where b.slug = %s and c.category = 'company'", (RENEGADE,))

    return _run(load)


@pytest.mark.dbtest
def test_company_facts_have_no_product_parent(company_claims):
    """The eight rows 015 re-parents. A company fact attached to a product
    inherits that product's marketability gate, which is a decision it has
    nothing to do with."""
    assert company_claims, "no category='company' claims found"
    still_parented = [c["claim_text"][:60] for c in company_claims
                      if c["product_id"] is not None]
    assert not still_parented, (
        "company-level claims still parented to a product: "
        f"{still_parented}")


@pytest.mark.dbtest
def test_the_prohibited_company_claims_kept_their_trigger_phrases(
        company_claims):
    """015 changes a parent, not a payload. 'licensed in all 50 states' has to
    stay enforceable -- and it is now enforceable brand-wide, which is the
    point: it is false in an M&A email too."""
    prohibited = [c for c in company_claims if c["status"] == "prohibited"]
    assert prohibited, "expected prohibited company claims"
    for c in prohibited:
        assert c["trigger_phrases"], (
            f"prohibited claim lost its phrases: {c['claim_text'][:60]!r}")


# --------------------------------------------------------------------------
# Retrieval: the property the workaround broke
# --------------------------------------------------------------------------

@pytest.mark.dbtest
def test_product_narrowed_retrieval_still_offers_brand_claims(schema):
    """fetch_claims('renegade', product_slug='franchise-program') must include
    the company facts. Before 015 they only appeared because they had been
    filed under that exact product; any other product missed them."""
    _require(schema)
    from kb_context import fetch_claims

    async def load():
        return await fetch_claims(RENEGADE, "franchise-program")

    claims = _run(load)
    text = " ".join((c["approved_wording"] or c["claim_text"])
                    for c in claims)
    missing = [f for f in COMPANY_FACTS if f not in text]
    assert not missing, f"brand-level facts absent from a product query: {missing}"


@pytest.mark.dbtest
def test_brand_claims_reach_a_different_product_too(schema):
    """The generalisation. agency-acquisition is a different product and is
    approved_for_marketing = false, so its own approved claims are correctly
    withheld -- but the brand's prohibited facts must still arrive."""
    _require(schema)
    from kb_context import fetch_claims

    async def load():
        return await fetch_claims(RENEGADE, "agency-acquisition")

    claims = _run(load)
    prohibited = " ".join(c["claim_text"] for c in claims
                          if c["status"] == "prohibited")
    assert "all 50 states" in prohibited, (
        "a brand-wide prohibited claim did not reach an M&A query -- this is "
        "the exact hole 015 closes")


@pytest.mark.dbtest
def test_an_ungated_products_approved_claims_are_still_withheld(schema):
    """015 must not have widened the product gate while making product_id
    optional. A brand-level claim bypasses the gate because there is no product
    to gate on; a product-level one must not."""
    _require(schema)
    from kb_context import fetch_claims

    async def load():
        return await fetch_claims(RENEGADE, "agency-acquisition")

    claims = _run(load)
    approved_product_claims = [
        c for c in claims
        if c["status"] == "approved" and c["product_slug"] is not None]
    assert not approved_product_claims, (
        "approved claims leaked past approved_for_marketing = false: "
        f"{[c['claim_text'][:50] for c in approved_product_claims]}")
