"""Retrieval and context packing, with no HTTP in it.

Extracted from main.py's /context handler, which had grown to hold the token
budget, the per-document cap, dispute labelling, near-duplicate suppression and
snapshot assembly inside a FastAPI route. Two consequences, both bad:

  * generators would have had to make an HTTP request to the process they run
    in to reach it
  * none of it was testable without starting a server, which is why the two
    governance defects found so far (rules that /context never returned,
    restricted claims that fetch_claims filtered out) were both found by hand

main.py now owns HTTP shapes and nothing else. This module raises UnknownBrand
rather than HTTPException for the same reason: a generator calling
build_context should not receive a web framework's exception type.

    ctx = await build_context("renegade", "franchise commissions")
    ctx["passages"], ctx["claims"], ctx["rules"], ctx["snapshot"]
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel

from config import settings
from db import fetch_all, fetch_one

DocumentKind = Literal["page", "faq", "entity", "primer"]

#: Attached only to passages whose source page is actually contradicted
#: somewhere in the corpus -- 37 chunks of 1,755, not the 314 that merely
#: contain a digit.
#:
#: The earlier default labelled every figure as unverified. That treated the
#: company's own published website as untrusted, which is the wrong premise:
#: copy that is already public was cleared once, at publication, and repeating
#: it in a campaign is not new risk. It also made "80% commission" -- published
#: in 7 places and the strongest line in the franchise pitch -- unusable.
DISPUTED_NOTE = (
    "This page is contradicted elsewhere on the site (see conflicts: {topics}). "
    "Usable for positioning and voice, but do not restate its figures as "
    "settled fact without checking public.claims."
)

#: A chunk containing a figure that is known to be wrong -- currently only
#: "$20,000" for the Renegade franchise fee. Withheld from /context entirely
#: rather than labelled: a passage the agent must never publish from is a
#: passage it does not need to read. /search still returns it, flagged, because
#: that endpoint exists for inspection.
BLOCKED_NOTE = "Contains a figure known to be wrong. Never publish from this passage."


class UnknownBrand(LookupError):
    """Requested brand slug is not in public.brands.

    Carries the known slugs because the caller cannot usefully guess, and
    because main.py turns this into a 404 whose body should say what IS valid.
    """

    def __init__(self, slug: str, known: list[str]) -> None:
        self.slug = slug
        self.known = known
        super().__init__(f"unknown brand {slug!r}. known: {known}")


class Passage(BaseModel):
    chunk_id: int
    doc_id: int
    kind: str
    title: str
    url: str | None
    category: str | None
    heading_path: str
    text: str
    rank: float
    #: Contains a marketing figure. Informational only -- it does NOT restrict
    #: use. Kept because it is how the conflicts were found, and how a future
    #: claims-review pass would work through the corpus.
    has_figures: bool
    #: Source page is contradicted somewhere. This is the one that restricts.
    disputed: bool
    dispute_topics: list[str] = []
    note: str | None = None


@dataclass(frozen=True)
class SearchOptions:
    """Plain arguments for kb.search_kb, independent of any request model."""

    brand: str
    query: str
    limit: int = 10
    kinds: list[str] | None = None
    include_thin: bool = False
    #: Floor on ts_rank_cd. Real matches score 0.20-2.60; paraphrase queries
    #: sharing only a common stem score 0.001-0.005.
    min_rank: float = 0.01
    #: None -> settings.search_mode.
    mode: str | None = None
    #: None -> settings.vector_max_distance (calibrated; see config.py).
    max_distance: float | None = None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    return max(1, len(text) // settings.chars_per_token)


async def require_brand(slug: str) -> dict:
    row = await fetch_one(
        "select id, slug, name from public.brands where slug = %s", (slug,))
    if not row:
        known = await fetch_all("select slug from public.brands order by slug")
        raise UnknownBrand(slug, [r["slug"] for r in known])
    return row


async def fetch_exclusions(brand_slug: str) -> list[dict]:
    """The operator's standing do-not-use list for a brand (023).

    No product scoping and no gate. A claim is a statement about the business
    and is scoped to the product it describes; an exclusion is an instruction
    about wording, so it applies wherever those words could appear. Narrowing
    it by product would mean "never say free forever" quietly stopped applying
    to the next product added.

    Only active rows. A retired exclusion is kept for the record -- "we used
    to forbid this and stopped" -- and must not still be enforced.
    """
    return await fetch_all(
        "select e.phrase, e.note "
        "from public.brand_exclusions e "
        "join public.brands b on b.id = e.brand_id "
        "where b.slug = %s and e.active "
        "order by e.phrase",
        (brand_slug,))


async def fetch_claims(brand_slug: str,
                       product_slug: str | None = None) -> list[dict]:
    """Governed claims for a brand, reached through products.

    Prohibited rows travel alongside approved ones deliberately. "Never write
    $20,000" is a different instruction from the mere absence of an approved
    $20,000 claim, and the agent has no way to infer the first from the second.

    restricted travels with them for the same reason. Excluding it -- which
    this function used to do, on the grounds that it "is not safe to assert" --
    meant the agent never learned the claim existed. The 4.7 Google rating is
    the case that shows it: the raw sentence "4.7 / 5 Google Reviews" is
    retrievable site copy and lands in the passages regardless, so withholding
    the restriction left the model seeing the figure with no governed signal
    about the as-of date it needs. Silence is a weaker instruction than a
    condition.

    pending_review stays excluded: an undecided claim carries no instruction.
    The effective_from/until window is honoured so a time-boxed promotional
    claim stops being offered on its own.

    THE PRODUCT GATE
    An APPROVED claim on a product whose approved_for_marketing is false is not
    approved for anything -- the product flag is the outer gate, the claim
    status the inner one. Without this, seeding 012 immediately put "no broker
    fees at any stage", "up to 90% cash upfront" and "partners keep ownership
    of their book" into the prompt for a franchise query: precisely the
    cross-programme mixing the blocker rule in 012 forbids. The governance
    layer was contradicting itself through the evidence it supplied.

    Prohibited and restricted rows survive the gate. They are warnings, not
    permissions, and their value is highest when the product is off-limits.

    BRAND-LEVEL CLAIMS (015)
    A claim with product_id NULL is a fact about the company, so there is no
    product to gate it on and it passes on brand alone. That is not a hole in
    the gate: the approval on the row IS the decision, and a brand fact has no
    product whose marketability could sensibly switch it off. Before 015 these
    were parented to a flagship product, which meant a product-narrowed query
    silently dropped them -- a franchise brief could not see "licensed in 48
    states", one of the strongest proof points the brand owns.

    The corollary is that seeding a brand-level claim as `approved` bypasses the
    product gate entirely, so an unreviewed brand may not have one. 019 files
    Agency Height's positioning claim as pending_review for exactly this
    reason.
    """
    return await fetch_all(
        "select c.status, c.claim_text, c.approved_wording, c.category, "
        "       c.requires_disclaimer, c.disclaimer_text, c.restriction_notes, "
        "       p.name as product, p.slug as product_slug "
        "from public.claims c "
        "join public.brands b on b.id = c.brand_id "
        "left join public.products p on p.id = c.product_id "
        "where b.slug = %s "
        "  and c.status in ('approved', 'prohibited', 'restricted') "
        "  and (c.effective_from is null or c.effective_from <= now()) "
        "  and (c.effective_until is null or c.effective_until > now()) "
        # A brand-level claim survives the product narrowing. "Renegade is
        # licensed in 48 states" is exactly as relevant to a franchise brief
        # as to an M&A one, and dropping it was the visible cost of the
        # pre-015 workaround.
        "  and (%s::text is null or p.slug = %s or c.product_id is null) "
        "  and (c.status <> 'approved' or c.product_id is null or ("
        "        p.approved_for_marketing "
        "    and p.status = 'active' "
        "    and (p.effective_from is null or p.effective_from <= now()) "
        "    and (p.effective_until is null or p.effective_until > now())"
        "  )) "
        "order by c.status, c.category, c.claim_text",
        (brand_slug, product_slug, product_slug),
    )


def governance_tokens(rules: list[dict], claims: list[dict]) -> int:
    """What the assembled prompt will actually spend on governance.

    Charged up front and never trimmed. When something has to give it is site
    copy: an agent missing a passage writes weaker copy, an agent missing a
    blocker rule writes non-compliant copy.

    Costs what generate.to_system_prompt renders, not just claim_text -- an
    approved claim emits its wording plus any disclaimer, a restricted or
    prohibited one emits its text plus restriction_notes, and those notes carry
    the whole instruction at several hundred characters each. Costing only
    claim_text under-counted by ~800 tokens against a 3,000-token budget, which
    then overspent on passages.
    """
    used = sum(estimate_tokens(r["rule"]) for r in rules)
    for c in claims:
        used += estimate_tokens(c["approved_wording"] or c["claim_text"])
        if c["requires_disclaimer"] and c["disclaimer_text"]:
            used += estimate_tokens(c["disclaimer_text"])
        if c["status"] != "approved" and c["restriction_notes"]:
            used += estimate_tokens(c["restriction_notes"])
    return used


# --------------------------------------------------------------------------
# Query embedding
# --------------------------------------------------------------------------

@lru_cache(maxsize=settings.query_embedding_cache)
def _embed_query_cached(query: str) -> str | None:
    """Embed one query. Returns a pgvector literal, or None on any failure.

    Cached because every search otherwise costs one request against a 1,000/day
    free-tier cap, and the same queries repeat constantly during development.

    Returning None rather than raising is the whole fallback design: if the
    embedding provider is down, out of quota, or misconfigured, kb.search_kb
    receives a NULL embedding and answers from the lexical arm alone. Retrieval
    degrades; it does not break.
    """
    if not settings.gemini_api:
        return None
    try:
        from openai import OpenAI

        from Ingestion.embed import DIMENSIONS, MODEL, to_vector_literal
        client = OpenAI(api_key=settings.gemini_api,
                        base_url=settings.gemini_base_url)
        resp = client.embeddings.create(model=MODEL, input=query,
                                        dimensions=DIMENSIONS)
        return to_vector_literal(resp.data[0].embedding)
    except Exception:
        return None


async def embed_query(query: str) -> str | None:
    """The OpenAI client is synchronous, so it runs off the event loop."""
    return await asyncio.to_thread(_embed_query_cached, query)


async def search(opts: SearchOptions) -> list[dict]:
    kinds = opts.kinds or ["page", "faq", "entity"]
    mode = opts.mode or settings.search_mode
    embedding = None if mode == "lexical" else await embed_query(opts.query)
    return await fetch_all(
        "select * from kb.search_kb(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (opts.brand, opts.query, opts.limit, list(kinds), opts.include_thin,
         opts.min_rank, embedding,
         # If embedding failed, ask for lexical rather than a hybrid query with
         # a NULL vector -- same result, but the mode reported back is honest.
         mode if embedding or mode == "lexical" else "lexical",
         opts.max_distance if opts.max_distance is not None
         else settings.vector_max_distance),
    )


# --------------------------------------------------------------------------
# Near-duplicate suppression
# --------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9']+")


def _shingle(text: str) -> frozenset[str]:
    return frozenset(_WORD_RE.findall(text.lower()))


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass
class _Deduper:
    """Drop passages that restate one already selected.

    THE PROBLEM THIS SOLVES, measured
    A query for "what makes us different from captive agencies" returned 21
    passages, of which 13 were the same templated FAQ repeated per location:
    "How will Renegade help me with Porter-specific insurance", the same for
    Panama City, Orlando, Palm Bay and nine others. They cluster at nearly
    identical vector distances, so they occupy the whole top-k and burn the
    budget -- 19 further passages were dropped over budget behind them.

    max_chunks_per_document cannot help: each of those FAQs is its own
    document by design, so the per-document cap sees one chunk each.

    WHY JACCARD AND NOT MMR
    MMR is the textbook answer and needs a vector per candidate. kb.search_kb
    does not return embeddings, and fetching 40 of them per request to solve a
    lexical-template problem is a poor trade. These passages differ by one or
    two location tokens out of forty, so word-set overlap separates them
    cleanly and cheaply, is deterministic, and is explainable in a review --
    which matters more here than optimality.

    Short passages are exempt: they are cheap, and two brief passages on one
    topic can legitimately share most of their words.
    """

    threshold: float
    min_chars: int
    kept: list[frozenset[str]] = field(default_factory=list)

    def is_duplicate(self, text: str) -> bool:
        if len(text) < self.min_chars:
            return False
        s = _shingle(text)
        if any(_jaccard(s, prev) >= self.threshold for prev in self.kept):
            return True
        self.kept.append(s)
        return False


# --------------------------------------------------------------------------
# The context builder
# --------------------------------------------------------------------------

async def build_context(
    brand: str,
    query: str,
    *,
    token_budget: int | None = None,
    include_primer: bool = True,
    product_slug: str | None = None,
    overfetch: int = 40,
    dedupe: bool = True,
) -> dict:
    """Everything the agent is allowed to see for one query.

    Primer first (always, and deterministically -- full-text search is the
    wrong tool for "who is this company"), then governance, then ranked
    passages packed into whatever budget remains, then the brand's documented
    contradictions.

    product_slug narrows claims to one programme. A franchise brief should not
    see M&A claims even once both products are approved for marketing.
    """
    brand_row = await require_brand(brand)
    budget = token_budget or settings.default_token_budget

    ctx_row = await fetch_one("select kb.brand_context(%s) as ctx", (brand,))
    brand_ctx = ctx_row["ctx"] if ctx_row else {}

    rules = brand_ctx.get("rules", [])
    claims = await fetch_claims(brand, product_slug)
    # Brand-scoped only. A brief's own exclusions are added by
    # generators.pipeline.context_for, which is the layer that knows
    # which campaign this is.
    exclusions = [{"phrase": e["phrase"], "note": e["note"],
                   "scope": "brand"}
                  for e in await fetch_exclusions(brand)]

    used = 0
    primer = None
    if include_primer and brand_ctx.get("primer"):
        primer = brand_ctx["primer"]
        used += estimate_tokens(primer)
    used += governance_tokens(rules, claims)

    # Over-fetch, then pack: cheaper than paging, and the per-document cap and
    # deduper both mean the raw top-k is not the final set.
    rows = await search(SearchOptions(brand=brand, query=query,
                                      limit=overfetch))

    passages: list[Passage] = []
    per_doc: dict[int, int] = {}
    skipped_budget = skipped_doc_cap = skipped_blocked = skipped_dupe = 0
    deduper = _Deduper(threshold=settings.near_duplicate_threshold,
                       min_chars=settings.near_duplicate_min_chars)

    for row in rows:
        # Withheld, not labelled. A passage the agent must never publish from
        # is a passage it does not need to read, and the conflict record in
        # brand_context already tells it the page exists and why it is wrong.
        if row["blocked"]:
            skipped_blocked += 1
            continue
        doc_id = row["doc_id"]
        if per_doc.get(doc_id, 0) >= settings.max_chunks_per_document:
            skipped_doc_cap += 1
            continue
        # Before the budget check, not after: a duplicate that consumed budget
        # would still have displaced a distinct passage.
        if dedupe and deduper.is_duplicate(row["chunk_text"]):
            skipped_dupe += 1
            continue
        cost = estimate_tokens(row["chunk_text"])
        if used + cost > budget:
            skipped_budget += 1
            continue

        topics = list(row["dispute_topics"] or [])
        passages.append(Passage(
            chunk_id=row["chunk_id"],
            doc_id=doc_id,
            kind=row["kind"],
            title=row["title"],
            url=row["url"],
            category=row["category"],
            heading_path=row["heading_path"],
            text=row["chunk_text"],
            rank=float(row["rank"]),
            has_figures=row["claim_like"],
            disputed=row["disputed"],
            dispute_topics=topics,
            note=(DISPUTED_NOTE.format(topics=", ".join(topics))
                  if row["disputed"] else None),
        ))
        per_doc[doc_id] = per_doc.get(doc_id, 0) + 1
        used += cost

    snapshot = await build_snapshot(
        brand=brand, query=query, passages=passages, rules=rules,
        claims=claims, brand_ctx=brand_ctx, blocked_withheld=skipped_blocked,
        primer_included=primer is not None, product_slug=product_slug,
    )

    return {
        "brand": {"slug": brand, "name": brand_ctx.get("brand_name")
                  or brand_row["name"]},
        "primer": primer,
        "passages": passages,
        "conflicts": brand_ctx.get("conflicts", []),
        "corpus": brand_ctx.get("corpus"),
        "rules": rules,
        "claims": claims,
        "exclusions": exclusions,
        "tokens": {"budget": budget, "used": used, "estimated": True},
        "dropped": {"over_budget": skipped_budget,
                    "per_document_cap": skipped_doc_cap,
                    "near_duplicate": skipped_dupe,
                    "blocked": skipped_blocked},
        "snapshot": snapshot,
    }


async def build_snapshot(
    *,
    brand: str,
    query: str,
    passages: list[Passage],
    rules: list[dict],
    claims: list[dict],
    brand_ctx: dict,
    blocked_withheld: int,
    primer_included: bool,
    product_slug: str | None = None,
) -> dict:
    """Shaped for campaign_validations.knowledge_snapshot and
    campaign_assets.knowledge_snapshot.

    Pinned to content hashes so "why did this pass in March" stays answerable
    after the sites are re-scraped. 013 makes the kb_chunk_ids key mandatory on
    every asset row, which is why it is always present here even when empty.
    """
    snapshot: dict = {
        "brand_slug": brand,
        "product_slug": product_slug,
        "query": query,
        "kb_chunk_ids": [p.chunk_id for p in passages],
        "content_hashes": [],
        "conflicts_surfaced": [c["topic"]
                               for c in brand_ctx.get("conflicts", [])],
        "disputed_passages": [p.chunk_id for p in passages if p.disputed],
        "blocked_withheld": blocked_withheld,
        "primer_included": primer_included,
        "rules_applied": [r["rule"] for r in rules],
        "approved_claims": [c["claim_text"] for c in claims
                            if c["status"] == "approved"],
        "prohibited_claims": [c["claim_text"] for c in claims
                              if c["status"] == "prohibited"],
        # Recorded separately from approved: an asset built on a restricted
        # claim needs its condition auditable after the fact, not merged into
        # the approved list where the condition disappears.
        "restricted_claims": [c["claim_text"] for c in claims
                              if c["status"] == "restricted"],
    }
    if passages:
        hashes = await fetch_all(
            "select c.chunk_id, d.content_hash, d.ingest_run_id "
            "from kb.chunks c join kb.documents d on d.doc_id = c.doc_id "
            "where c.chunk_id = any(%s) order by c.chunk_id",
            ([p.chunk_id for p in passages],),
        )
        snapshot["content_hashes"] = [h["content_hash"] for h in hashes]
        snapshot["ingest_run_ids"] = sorted(
            {h["ingest_run_id"] for h in hashes
             if h["ingest_run_id"] is not None}
        )
    return snapshot
