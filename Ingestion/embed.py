"""Populate kb.chunks.embedding.

Runs AFTER the loader, directly against the database:

    python -m Ingestion.md_ingest
    python -m Ingestion.json_ingest
    python -m Ingestion.ingestion_service
    python -m Ingestion.embed          <- here

Not before, and not via build/*.jsonl. The loader already deletes and
re-inserts chunks only for documents whose content_hash changed, so a changed
page's new chunk rows arrive with embedding NULL while every unchanged chunk
keeps the vector it already had. Resumability therefore comes free from the
existing idempotency -- "what needs embedding" is just `embedding is null`,
which the chunks_embedding_pending_idx partial index answers directly.

Routing the vectors through JSONL instead would mean serialising 1536 floats per
chunk to disk (tens of MB of JSON text) and threading a new column through the
COPY path, for no gain.

Safe to interrupt and re-run: each batch commits on its own.
"""

from __future__ import annotations

import argparse
import sys
import time

import psycopg
from openai import OpenAI
from psycopg.rows import dict_row

from config import settings

from .ingestion_service import loaded_sizing
from .md_ingest import current_sizing

#: gemini-embedding-2, not -001. Both are 3072 natively, but only -2 returns
#: unit-normalised vectors when truncated (measured: -2 -> 1.0000,
#: -001 -> 0.6935 at 1536 dims). Cosine distance is scale-invariant so -001
#: would still rank correctly today, but it leaves a trap for any later switch
#: to inner product.
MODEL = "gemini-embedding-2"

#: Must match `extensions.vector(1536)` in migration 009. pgvector's hnsw index
#: refuses `vector` columns above 2,000 dimensions, which rules out the native
#: 3072. Changing this needs a migration AND a full re-embed.
DIMENSIONS = 1536

#: The API accepts a list, but batching buys NO quota relief: Gemini's
#: OpenAI-compatible endpoint expands a batch into one embedContent request per
#: item. Measured the hard way -- 9 batches of 100 consumed ~900 of the 1,000
#: daily free-tier requests.
#:
#: So batch size is only about how much progress a failed call throws away, and
#: how finely the daily cap can be approached. 25 loses less at the boundary.
BATCH_SIZE = 25

#: Free-tier embedding also has a per-MINUTE cap, and since each item is its own
#: request, a 100-item batch blows it instantly -- which is what turned the
#: first run into a retry storm (17.8/s for the first batch, then ~2/s).
#: Pacing to stay under it is far cheaper than retrying through it.
REQUESTS_PER_MINUTE = 90

#: gemini-embedding accepts ~2,048 tokens. Post-re-chunk the largest chunk is
#: ~2,500 chars (~625 tokens), so this only ever fires on legacy oversized rows
#: -- but silently sending an over-long input would fail the whole batch.
MAX_INPUT_CHARS = 7_000

RETRY_DELAYS = (2, 5, 15, 45)


class DailyQuotaExhausted(RuntimeError):
    """The per-day free-tier cap. Retrying is pointless -- it resets tomorrow.

    Distinct from a per-minute rate limit, which retrying does fix. Treating
    them the same is what produced 30 useless retries and then a traceback.
    """


PENDING_SQL = """
select c.chunk_id, c.doc_title, c.heading_path, c.text
from kb.chunks c
where c.embedding is null
   or c.embedding_model is distinct from %(model)s
order by c.chunk_id
limit %(limit)s
"""

COUNT_SQL = """
select count(*) filter (where embedding is not null
                          and embedding_model = %(model)s) as done,
       count(*) filter (where embedding is null
                          or embedding_model is distinct from %(model)s) as pending,
       count(*) as total
from kb.chunks
"""

UPDATE_SQL = """
update kb.chunks
set embedding       = %(emb)s::extensions.vector,
    embedding_model = %(model)s,
    embedded_at     = now()
where chunk_id = %(chunk_id)s
"""


def embed_input(row: dict) -> str:
    """Title and heading prepended to the chunk body.

    The generated tsvector already weights doc_title at 'A' and heading_path at
    'B'; the embedding needs the same context by a different route. A chunk from
    the middle of a long page is far more useful as a vector when it carries
    which page and which section it came from -- otherwise "Initial fees start
    at $25,000 depending on your business type" embeds as generic pricing prose
    with no franchise signal at all.
    """
    parts = [row.get("doc_title") or "", row.get("heading_path") or "",
             row["text"]]
    text = "\n".join(p for p in parts if p.strip())
    return text[:MAX_INPUT_CHARS]


def to_vector_literal(vec: list[float]) -> str:
    """pgvector input format. 7 significant digits because the `vector` type
    stores float4 -- more precision would be discarded on the way in."""
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"


def embed_batch(client: OpenAI, texts: list[str], model: str) -> list[list[float]]:
    """One API call, retried on transient failure.

    Free-tier rate limits are the expected failure here, not a bug -- so back
    off and retry rather than dying halfway through a corpus. A wrong model name
    or bad key is not retried: it would fail identically four more times.
    """
    last: Exception | None = None
    for attempt, delay in enumerate((0, *RETRY_DELAYS)):
        if delay:
            time.sleep(delay)
        try:
            resp = client.embeddings.create(
                model=model, input=texts, dimensions=DIMENSIONS,
            )
            vectors = [d.embedding for d in resp.data]
            if len(vectors) != len(texts):
                raise RuntimeError(
                    f"asked for {len(texts)} embeddings, got {len(vectors)}"
                )
            bad = [len(v) for v in vectors if len(v) != DIMENSIONS]
            if bad:
                raise RuntimeError(
                    f"expected {DIMENSIONS} dims, got {sorted(set(bad))} -- "
                    f"model {model!r} may not honour the dimensions parameter"
                )
            return vectors
        except Exception as exc:
            msg = str(exc).lower()
            # Per-DAY cap vs per-minute cap. Both arrive as 429, but only one is
            # worth retrying. Conflating them produced 30 useless retries and a
            # traceback on the first real run.
            flat = msg.replace("_", "").replace("-", "").replace(" ", "")
            if "perday" in flat:
                raise DailyQuotaExhausted(str(exc)) from None
            fatal = ("not found" in msg or "api key" in msg
                     or "unauthorized" in msg or "permission" in msg)
            if fatal:
                raise
            last = exc
            print(f"    rate limited, retry {attempt + 1}/{len(RETRY_DELAYS)}",
                  file=sys.stderr)
    raise RuntimeError(f"batch failed after {len(RETRY_DELAYS)} retries: {last}")


def check_sizing(cur) -> None:
    """Refuse to embed chunks that are about to be deleted.

    The expensive mistake this prevents, learned by making it: chunk sizing was
    changed for embeddings, but embed.py ran before the reload. 900 of the 1,000
    daily free-tier requests were spent on chunks the next loader run replaces.

    kb.ingest_runs.counts records the sizing the database was loaded with. If it
    is absent or differs from md_ingest's current constants, the chunk rows in
    the database are stale and embedding them is throwing away quota.
    """
    db = loaded_sizing(cur)
    current = current_sizing()

    if not db:
        raise SystemExit(
            """The database has no recorded chunk sizing, so its chunks predate
the current md_ingest constants. Embedding them would spend quota on rows the
next load replaces.

Reload first:
    python -m Ingestion.md_ingest
    python -m Ingestion.json_ingest
    python -m Ingestion.ingestion_service

The loader detects the sizing change and rebuilds every chunk."""
        )

    drift = {k: (db.get(k), v) for k, v in current.items() if db.get(k) != v}
    if drift:
        detail = "\n".join(f"    {k}: database={b}  md_ingest={n}"
                           for k, (b, n) in sorted(drift.items()))
        raise SystemExit(
            "The database was loaded with different chunk sizing:\n"
            + detail
            + """

Its chunks are stale. Reload before embedding:
    python -m Ingestion.md_ingest && python -m Ingestion.json_ingest
    python -m Ingestion.ingestion_service"""
        )


def run(model: str, batch_size: int, max_chunks: int | None,
        dry_run: bool) -> int:
    if not settings.gemini_api:
        raise SystemExit(
            "GEMINI_API is not set in .env -- embedding needs it.\n"
            "Get a key at https://aistudio.google.com/apikey"
        )

    client = OpenAI(api_key=settings.gemini_api,
                    base_url=settings.gemini_base_url)

    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            check_sizing(cur)
            cur.execute(COUNT_SQL, {"model": model})
            counts = cur.fetchone()
        print(f"model   {model} @ {DIMENSIONS} dims")
        print(f"chunks  {counts['total']} total, {counts['done']} embedded, "
              f"{counts['pending']} pending")

        if not counts["pending"]:
            print("nothing to do")
            return 0
        if dry_run:
            print("dry run -- nothing written")
            return 0

        done = 0
        started = time.monotonic()
        batch_started: float | None = None
        last_batch_items = 0
        while True:
            remaining = (max_chunks - done) if max_chunks else batch_size
            if remaining <= 0:
                break
            with conn.cursor() as cur:
                cur.execute(PENDING_SQL, {"model": model,
                                          "limit": min(batch_size, remaining)})
                rows = cur.fetchall()
            if not rows:
                break

            # Pace to stay under the per-minute cap. Each ITEM is a request
            # through the compat endpoint, so a batch of N costs N requests and
            # needs N/(RPM/60) seconds of headroom. Sleeping here is strictly
            # cheaper than being rate limited and retrying.
            if batch_started is not None:
                min_interval = last_batch_items / (REQUESTS_PER_MINUTE / 60)
                slack = min_interval - (time.monotonic() - batch_started)
                if slack > 0:
                    time.sleep(slack)
            batch_started = time.monotonic()
            last_batch_items = len(rows)

            try:
                vectors = embed_batch(client,
                                      [embed_input(r) for r in rows], model)
            except DailyQuotaExhausted as exc:
                # Not a failure of this script. Everything already committed is
                # persisted; the cap resets and a re-run picks up from there.
                print("")
                print(f"daily free-tier quota reached after {done} chunks "
                      f"this run.")
                print(f"{counts['pending'] - done} still pending. The cap "
                      f"resets every 24h -- re-run then; finished chunks are "
                      f"already committed.")
                print(f"  ({str(exc)[:120]})")
                return 2

            # Commit per batch. An interrupted run leaves the finished batches
            # persisted and the rest still NULL, so re-running resumes exactly
            # where it stopped.
            with conn.cursor() as cur:
                cur.executemany(UPDATE_SQL, [
                    {"chunk_id": r["chunk_id"],
                     "emb": to_vector_literal(v),
                     "model": model}
                    for r, v in zip(rows, vectors)
                ])
            conn.commit()

            done += len(rows)
            rate = done / max(time.monotonic() - started, 0.001)
            print(f"  {done}/{counts['pending']} embedded "
                  f"({rate:.1f}/s)", flush=True)

        elapsed = time.monotonic() - started
        print(f"\ndone: {done} chunks in {elapsed:.1f}s")

        with conn.cursor() as cur:
            cur.execute(COUNT_SQL, {"model": model})
            after = cur.fetchone()
        print(f"now     {after['done']} embedded, {after['pending']} pending")
        if after["pending"]:
            print("re-run to finish the remainder")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after N chunks -- use for a costed trial run")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what is pending, write nothing")
    args = ap.parse_args()
    return run(args.model, args.batch_size, args.limit, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
