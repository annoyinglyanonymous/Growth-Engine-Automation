"""Read and write the operator's standing do-not-use lists (023).

Thin, like campaigns.py, and for the same reason: the UI needs a real writer
and a route handler is the wrong place for one.

WHAT MAKES THIS DIFFERENT FROM public.claims
A claim is a statement about the business, reviewed, sourced, and scoped to
the product it describes. An exclusion is an instruction about wording, given
by a person, needing no justification. That is why it is two columns and a
name rather than the twenty a claim carries -- if declaring one cost as much
as filing a claim, nobody would do it, and the guardrail would exist only in
the schema.

Retire rather than delete. "We used to forbid this and stopped, and here is
who stopped it" is a question worth being able to answer, and it costs one
boolean.
"""

from __future__ import annotations

from db import cursor, fetch_all, fetch_one

#: Mirrors 023's CHECK and validation.checks.MIN_EXCLUSION_CHARS. Rejected
#: here too so the UI can say why rather than surfacing a constraint violation.
MIN_PHRASE_CHARS = 2


class ExclusionProblem(ValueError):
    """Bad input from a person, phrased for a person."""


async def brands() -> list[dict]:
    return await fetch_all(
        "select id, slug, name from public.brands order by name")


async def for_brand(brand_slug: str | None = None) -> list[dict]:
    """Every exclusion, newest first, with the brand it belongs to.

    Retired rows are included and flagged rather than filtered: the point of
    keeping them is that someone can see them.
    """
    return await fetch_all(
        "select e.id, e.phrase, e.note, e.added_by, e.added_at, e.active, "
        "       e.retired_by, e.retired_at, b.slug as brand_slug, "
        "       b.name as brand_name "
        "from public.brand_exclusions e "
        "join public.brands b on b.id = e.brand_id "
        "where %s::text is null or b.slug = %s "
        "order by e.active desc, b.name, e.phrase",
        (brand_slug, brand_slug))


async def add(brand_slug: str, phrase: str, added_by: str,
              note: str | None = None) -> dict:
    """Declare one exclusion. Returns the row.

    Re-adding a phrase that is retired REVIVES it rather than failing on the
    unique index -- which is what someone repeating themselves means, and the
    alternative is an error that reads like a bug.
    """
    phrase = (phrase or "").strip()
    if len(phrase) < MIN_PHRASE_CHARS:
        raise ExclusionProblem(
            f"an exclusion needs at least {MIN_PHRASE_CHARS} characters -- "
            f"a single character matches a word in almost every sentence")

    brand = await fetch_one(
        "select id from public.brands where slug = %s", (brand_slug,))
    if not brand:
        raise ExclusionProblem(f"no brand {brand_slug!r}")

    async with cursor() as cur:
        # The unique index is on lower(btrim(phrase)), so the conflict target
        # has to be the same expression.
        await cur.execute(
            "insert into public.brand_exclusions "
            "  (brand_id, phrase, note, added_by) "
            "values (%s, %s, %s, %s) "
            "on conflict (brand_id, lower(btrim(phrase))) do update "
            "   set active = true, retired_by = null, retired_at = null, "
            "       note = coalesce(excluded.note, "
            "                       public.brand_exclusions.note), "
            "       added_by = excluded.added_by, added_at = now() "
            "returning id, phrase, active",
            (brand["id"], phrase, (note or "").strip() or None, added_by))
        return await cur.fetchone()


async def retire(exclusion_id: str, retired_by: str) -> dict:
    """Stop enforcing one exclusion, keeping the record of it."""
    async with cursor() as cur:
        await cur.execute(
            "select phrase, active from public.brand_exclusions "
            "where id = %s for update", (exclusion_id,))
        row = await cur.fetchone()
        if not row:
            raise ExclusionProblem(f"no exclusion {exclusion_id}")
        if not row["active"]:
            return {"phrase": row["phrase"], "already": True}
        await cur.execute(
            "update public.brand_exclusions "
            "set active = false, retired_by = %s, retired_at = now() "
            "where id = %s", (retired_by, exclusion_id))
    return {"phrase": row["phrase"], "retired": True}
