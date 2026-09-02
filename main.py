"""KB retrieval API.

What the AI agent calls to ground itself, over HTTP.

Three layers, deliberately:

  SQL          ranking -- kb.search_kb, kb.brand_context. Shared with any n8n
               workflow hitting PostgREST, so there is one implementation.
  kb_context   packing -- token budget, primer injection, dispute labelling,
               near-duplicate suppression, snapshot assembly. Importable, so
               the generators call it directly instead of making an HTTP
               request to the process they are running in.
  this module  HTTP shapes and nothing else.

The packing logic used to live in the /context handler here. That made it
unreachable except over the network and untestable except by starting a server,
which is why both governance defects found so far -- rules that /context never
returned, restricted claims that fetch_claims filtered out -- had to be found
by hand.

    python main.py                      # start it (see the note below)
    python main.py --reload --port 8000

On Windows, start it THIS way rather than with the bare `uvicorn main:app` CLI.
psycopg cannot run async on Windows' default ProactorEventLoop, and the CLI
creates its event loop *before* importing this module -- so setting the policy
at import time is too late and every query fails as a 30s PoolTimeout. The
__main__ block below sets the policy first, then hands off to uvicorn.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import auth
from db import fetch_all, fetch_one, pool
from kb_context import (
    DocumentKind,
    Passage,
    SearchOptions,
    UnknownBrand,
    build_context,
    require_brand,
    search,
)

__all__ = ["app", "Passage", "DocumentKind"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Printed at boot because an unconfigured install still works and still
    # records approvals as 'local-ui'. Silence there would be the bug.
    warning = auth.startup_warning()
    if warning:
        # flush=True: stdout is block-buffered whenever it is redirected to a
        # file or a pipe, so without it this line only appears when the process
        # exits -- which for a warning about unattributed approvals is the same
        # as not printing it at all.
        print(warning, flush=True)
    await pool.open()
    for line in await schema_preflight():
        print(line, flush=True)
    try:
        yield
    finally:
        await pool.close()


#: What this code needs the schema to already have, keyed by the migration
#: that provides it, with what breaks without it.
#:
#: `columns` entries are (table, column, expected data_type). The TYPE is
#: checked and not just existence, because the first bug this preflight missed
#: was a column that existed with the wrong one: campaigns.created_by was an
#: unused uuid shaped for auth.users, and writing an operator name to it failed
#: on apply with `invalid input syntax for type uuid`.
#:
#: `tables` exists because the SECOND bug this preflight missed was a whole
#: missing table. 020 adds revision_requests, campaigns.pipeline_state queries
#: it, and the preflight reported nothing at all while the campaign detail page
#: returned a 500 -- a check that only knows about columns is silent on exactly
#: the migration that adds no columns to anything.
REQUIRED_SCHEMA: dict[str, dict] = {
    "015_brand_scoped_claims.sql": {
        "columns": (("claims", "brand_id", "uuid"),),
        "breaks": "claim retrieval, /context, and every generator",
    },
    "016_decision_attribution.sql": {
        "columns": (("campaign_angles", "decided_by", "text"),
                    ("creative_concepts", "decided_by", "text"),
                    ("campaign_validations", "validated_by", "text"),
                    ("asset_qa_results", "validated_by", "text"),
                    ("campaigns", "created_by", "text")),
        "breaks": "the campaign detail screen, validation, QA, and filing a "
                  "brief",
    },
    "020_revision_requests.sql": {
        "tables": ("revision_requests",),
        "breaks": "the campaign detail screen, Approve all, and every Edit "
                  "button",
    },
    "021_campaign_lifecycle.sql": {
        "tables": ("campaign_status_events",),
        "columns": (("campaigns", "approved_by", "text"),),
        "breaks": "the campaign detail screen, stage 9 (campaign "
                  "approval), and brief validation -- which now "
                  "records its own status move",
    },
    "022_asset_rejection.sql": {
        "columns": (("campaign_assets", "rejected_by", "text"),
                    ("campaign_strategies", "rejected_by", "text")),
        "breaks": "the Reject button on an asset -- the only way to turn "
                  "down a revision without approving it",
    },
}


async def schema_preflight() -> list[str]:
    """Name any migration this code needs that has not been applied.

    Without this, code/schema skew surfaces as a 500 with a psycopg traceback
    at the moment someone opens a screen -- `column "validated_by" does not
    exist` is accurate but arrives in a log, on a request, to whoever happened
    to click. The information is available at boot, so it belongs at boot.

    Deliberately a warning and not a refusal to start. /health, /search and
    /context do not touch these columns and there is no reason to take the KB
    API down over a pending UI migration. The affected screens still fail --
    that is the honest consequence of pending work -- but nobody has to
    reverse-engineer why.
    """
    have = {
        (r["table_name"], r["column_name"]): r["data_type"]
        for r in await fetch_all(
            "select table_name, column_name, data_type "
            "from information_schema.columns where table_schema = 'public'")
    }
    tables = {table for table, _ in have}

    lines: list[str] = []
    for migration, spec in REQUIRED_SCHEMA.items():
        wrong: list[str] = []
        for table in spec.get("tables", ()):
            if table not in tables:
                wrong.append(f"table {table} (missing)")
        for table, column, want in spec.get("columns", ()):
            actual = have.get((table, column))
            if actual is None:
                wrong.append(f"{table}.{column} (missing)")
            elif actual != want:
                wrong.append(f"{table}.{column} (is {actual}, needs {want})")
        if wrong:
            lines.append(
                f"SCHEMA: {migration} is not applied -- "
                f"{', '.join(wrong)}. This breaks {spec['breaks']}. "
                f"Run: python migrate.py")
    return lines


app = FastAPI(
    title="Marketing Growth Engine - KB",
    description="Brand-scoped retrieval over the knowledge base.",
    version="0.1.0",
    lifespan=lifespan,
)

# The UI ships with the API it talks to: one process, one port, no build step.
# Imported here rather than the other way round so ui.py can stay a router and
# this module keeps ownership of the app and its lifespan.
#
# Mounted LAST in this file's import order but registered before the catch-all
# routes below matter, because the UI owns "/" and the API owns everything
# under a named path. If a future API route ever wants "/", that collision
# should be a deliberate decision rather than an import-order accident.
import ui  # noqa: E402  -- must come after `app` exists

app.include_router(ui.public_router)
app.include_router(ui.router)


@app.exception_handler(auth.NotSignedIn)
async def not_signed_in_handler(request, exc: auth.NotSignedIn):
    """Send an unidentified caller to the sign-in screen.

    303 rather than 307 so a POST that hits the gate becomes a GET of the
    sign-in form. The form body is lost, which is the correct trade for the one
    case it happens in -- a session expiring mid-edit -- against 307's
    alternative of the browser re-POSTing to /signin.
    """
    from urllib.parse import quote

    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"/signin?next={quote(exc.next_url, safe='')}",
                            status_code=303)


# --------------------------------------------------------------------------
# Error translation
#
# kb_context raises domain errors so that generators importing it do not have
# to catch a web framework's exception type. The HTTP mapping belongs here.
# --------------------------------------------------------------------------

@app.exception_handler(UnknownBrand)
async def unknown_brand_handler(request, exc: UnknownBrand):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=404, content={"detail": str(exc)})


async def _require_brand_http(slug: str) -> dict:
    """require_brand, with UnknownBrand surfaced as a 404 immediately.

    The exception handler above covers responses; this exists because several
    endpoints call require_brand purely as a guard and read no result, and a
    raised UnknownBrand escaping mid-handler is harder to follow than one
    translated at the call site.
    """
    try:
        return await require_brand(slug)
    except UnknownBrand as exc:
        raise HTTPException(404, str(exc)) from None


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class SearchRequest(BaseModel):
    # No default and no "all brands" option. Grounding a Renegade campaign in
    # Agency Height copy is a silent failure whose output reads as plausible,
    # so the request cannot express it.
    brand: str = Field(..., description="brands.slug, e.g. 'renegade'")
    query: str = Field(..., min_length=1)
    limit: int = Field(10, ge=1, le=100)
    kinds: list[DocumentKind] | None = Field(
        None, description="defaults to page, faq, entity -- primers excluded"
    )
    include_thin: bool = False
    #: Floor on ts_rank_cd. Real matches score 0.20-2.60; paraphrase queries
    #: sharing only a common stem score 0.001-0.005. Without a floor the API
    #: hands the agent noise it cannot distinguish from evidence.
    min_rank: float = Field(0.01, ge=0.0)
    #: None -> settings.search_mode. 'lexical' skips the embedding call, which
    #: is the one to use when the daily embedding quota matters more than recall.
    mode: Literal["lexical", "vector", "hybrid"] | None = None
    #: None -> settings.vector_max_distance (calibrated; see config.py).
    max_distance: float | None = Field(None, ge=0.0, le=2.0)

    def to_options(self) -> SearchOptions:
        return SearchOptions(
            brand=self.brand, query=self.query, limit=self.limit,
            kinds=list(self.kinds) if self.kinds else None,
            include_thin=self.include_thin, min_rank=self.min_rank,
            mode=self.mode, max_distance=self.max_distance,
        )


class ContextRequest(BaseModel):
    brand: str
    query: str = Field(..., min_length=1)
    token_budget: int | None = Field(None, ge=500, le=100_000)
    include_primer: bool = True
    #: Narrows claims to one programme. A franchise brief should not see M&A
    #: claims even once both products are approved for marketing.
    product: str | None = None
    #: Off only for diagnosing what the deduper removed. Leaving it off in
    #: normal use spends the budget on restatements of one answer.
    dedupe: bool = True


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    row = await fetch_one("select count(*) as chunks from kb.chunks")
    return {"status": "ok", "chunks": row["chunks"]}


@app.post("/search")
async def search_endpoint(req: SearchRequest) -> dict:
    """Ranked chunks. Snippets are ts_headline output with ** around matches."""
    await _require_brand_http(req.brand)
    rows = await search(req.to_options())
    return {
        "brand": req.brand,
        "query": req.query,
        "count": len(rows),
        "results": [
            {
                "chunk_id": r["chunk_id"],
                "doc_id": r["doc_id"],
                "kind": r["kind"],
                "title": r["title"],
                "url": r["url"],
                "category": r["category"],
                "heading_path": r["heading_path"],
                "snippet": r["snippet"],
                "rank": float(r["rank"]),
                "has_figures": r["claim_like"],
                "disputed": r["disputed"],
                "dispute_topics": r["dispute_topics"],
                "blocked": r["blocked"],
                # Which arm found it, and how well. Without these a thin result
                # set is a mystery: you cannot tell a lexical miss from a
                # vector cap from an empty corpus.
                "matched_by": r["matched_by"],
                "lexical_rank": (float(r["lexical_rank"])
                                 if r["lexical_rank"] is not None else None),
                "vector_distance": (float(r["vector_distance"])
                                    if r["vector_distance"] is not None else None),
                "date_modified": r["date_modified"],
            }
            for r in rows
        ],
    }


@app.post("/context")
async def context(req: ContextRequest) -> dict:
    """The endpoint the agent actually uses.

    A thin wrapper over kb_context.build_context. Everything interesting is
    there; this exists so an out-of-process caller (n8n, curl, a future UI on
    another host) reaches the same assembly the in-process generators use.
    """
    try:
        return await build_context(
            req.brand, req.query,
            token_budget=req.token_budget,
            include_primer=req.include_primer,
            product_slug=req.product,
            dedupe=req.dedupe,
        )
    except UnknownBrand as exc:
        raise HTTPException(404, str(exc)) from None


@app.get("/documents/{doc_id}")
async def document(doc_id: int) -> dict:
    doc = await fetch_one(
        "select d.*, b.slug as brand_slug from kb.documents d "
        "join public.brands b on b.id = d.brand_id where d.doc_id = %s",
        (doc_id,),
    )
    if not doc:
        raise HTTPException(404, f"no document {doc_id}")
    chunks = await fetch_all(
        "select chunk_id, ordinal, heading_path, text, char_len, claim_like "
        "from kb.chunks where doc_id = %s order by ordinal",
        (doc_id,),
    )
    doc.pop("brand_id", None)
    return {"document": doc, "chunks": chunks}


@app.get("/brands/{slug}/locations")
async def locations(slug: str, include_closed: bool = False) -> dict:
    """Typed SQL, not retrieval. 'Which Florida agencies are open' is a filter,
    not a ranking problem."""
    await _require_brand_http(slug)
    rows = await fetch_all(
        "select e.key, e.data from kb.entities e "
        "join public.brands b on b.id = e.brand_id "
        "where b.slug = %s and e.entity_type = 'location' "
        "  and (%s or (e.data->>'closed')::boolean is false) "
        "order by e.key",
        (slug, include_closed),
    )
    return {"brand": slug, "count": len(rows),
            "locations": [{"key": r["key"], **r["data"]} for r in rows]}


@app.get("/brands/{slug}/licenses")
async def licenses(slug: str, state: str | None = None) -> dict:
    await _require_brand_http(slug)
    rows = await fetch_all(
        "select e.key as state, e.data->>'license_number' as license_number "
        "from kb.entities e join public.brands b on b.id = e.brand_id "
        "where b.slug = %s and e.entity_type = 'state_license' "
        "  and (%s::text is null or e.key = %s) order by e.key",
        (slug, state, state),
    )
    return {"brand": slug, "count": len(rows), "licenses": rows}


if __name__ == "__main__":
    import argparse
    import asyncio
    import sys

    import uvicorn

    parser = argparse.ArgumentParser(description="Run the KB retrieval API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    opts = parser.parse_args()

    windows = sys.platform == "win32"

    if opts.reload or not windows:
        # Reload runs the app in a subprocess, and uvicorn's own factory
        # already picks SelectorEventLoop in that mode -- so this path needs
        # no special handling on any platform.
        uvicorn.run("main:app", host=opts.host, port=opts.port,
                    reload=opts.reload)
    else:
        # Single-process on Windows. uvicorn >= 0.36 does not consult the event
        # loop policy: uvicorn.loops.asyncio.asyncio_loop_factory returns
        # ProactorEventLoop outright, and psycopg refuses to run async on it
        # (surfacing as a 30s PoolTimeout rather than a clear error).
        #
        # So construct the loop ourselves instead of asking uvicorn for one.
        # loop="none" makes get_loop_factory() return None so uvicorn does not
        # override the loop this Runner supplies.
        config = uvicorn.Config(app, host=opts.host, port=opts.port,
                                loop="none")
        server = uvicorn.Server(config)
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(server.serve())
