"""Load build/*.jsonl into Supabase.

The first stage that touches the network. Everything before this is offline by
design, so by the time anything reaches here the chunk boundaries have already
been reviewed.

    python -m Ingestion.ingestion_service --dry-run   # plan only, no writes
    python -m Ingestion.ingestion_service             # load
    python -m Ingestion.ingestion_service --brand renegade

Reads, all produced by md_ingest and json_ingest:

    build/documents.jsonl         md pages, FAQs, primers
    build/chunks.jsonl
    build/entity_documents.jsonl  locations, state licence list
    build/entity_chunks.jsonl
    build/enrichment.jsonl        JSON-LD fields, keyed by brand+slug
    build/entities.jsonl          typed rows for SQL-answerable questions
    KB/conflicts.json             documented site contradictions

Idempotent: a document whose content_hash is unchanged keeps its existing
chunks and costs one comparison. Only new and changed documents have their
chunks deleted and rewritten.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import psycopg
from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: Columns COPY'd into kb.chunks. tsv is generated, so it is not listed --
#: naming it would be an error and the database computes it server-side.
CHUNK_COLUMNS = (
    "doc_id", "brand_id", "ordinal", "heading_path", "doc_title",
    "text", "char_len", "claim_like", "meta",
)

DOCUMENT_UPSERT = """
insert into kb.documents (
    brand_id, kind, path, slug, title, url, category, meta_description,
    date_modified, date_published, thin, char_len, content_hash, meta,
    ingest_run_id
) values (
    %(brand_id)s, %(kind)s, %(path)s, %(slug)s, %(title)s, %(url)s,
    %(category)s, %(meta_description)s, %(date_modified)s, %(date_published)s,
    %(thin)s, %(char_len)s, %(content_hash)s, %(meta)s, %(run_id)s
)
on conflict (brand_id, path) do update set
    kind             = excluded.kind,
    slug             = excluded.slug,
    title            = excluded.title,
    url              = excluded.url,
    category         = excluded.category,
    meta_description = excluded.meta_description,
    date_modified    = excluded.date_modified,
    date_published   = excluded.date_published,
    thin             = excluded.thin,
    char_len         = excluded.char_len,
    content_hash     = excluded.content_hash,
    meta             = excluded.meta,
    ingest_run_id    = excluded.ingest_run_id,
    ingested_at      = now()
"""

ENTITY_UPSERT = """
insert into kb.entities (brand_id, entity_type, key, data, ingest_run_id)
values (%(brand_id)s, %(entity_type)s, %(key)s, %(data)s, %(run_id)s)
on conflict (brand_id, entity_type, key) do update set
    data          = excluded.data,
    ingest_run_id = excluded.ingest_run_id,
    ingested_at   = now()
"""

# KB/conflicts.json is the source of truth, resolutions included -- so every
# resolution column is written here too. Omitting them would silently reset a
# resolved conflict back to 'active' on the next ingest, which is exactly the
# kind of quiet regression this table exists to prevent.
CONFLICT_UPSERT = """
insert into kb.conflicts
    (brand_id, topic, detail, sources, severity, status,
     correct_value, stale_values, resolution, resolved_at, resolved_by)
values
    (%(brand_id)s, %(topic)s, %(detail)s, %(sources)s, %(severity)s, %(status)s,
     %(correct_value)s, %(stale_values)s, %(resolution)s, %(resolved_at)s,
     %(resolved_by)s)
on conflict (brand_id, topic) do update set
    detail        = excluded.detail,
    sources       = excluded.sources,
    severity      = excluded.severity,
    status        = excluded.status,
    correct_value = excluded.correct_value,
    stale_values  = excluded.stale_values,
    resolution    = excluded.resolution,
    resolved_at   = excluded.resolved_at,
    resolved_by   = excluded.resolved_by
"""


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def database_url() -> str:
    values = dotenv_values(PROJECT_ROOT / ".env") or {}
    url = values.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL not found in .env")
    return url


# --------------------------------------------------------------------------
# Loading steps
# --------------------------------------------------------------------------

def resolve_brands(cur) -> dict[str, str]:
    """slug -> brands.id. This is where brand_slug becomes a real foreign key."""
    cur.execute("select slug, id from public.brands")
    return {slug: str(bid) for slug, bid in cur.fetchall()}


def existing_documents(cur, brand_ids: list[str]) -> dict[tuple[str, str], tuple[int, str]]:
    """(brand_id, path) -> (doc_id, content_hash) for skip-unchanged."""
    cur.execute(
        "select brand_id, path, doc_id, content_hash from kb.documents "
        "where brand_id = any(%s)",
        (brand_ids,),
    )
    return {(str(b), p): (d, h) for b, p, d, h in cur.fetchall()}


def merge_enrichment(doc: dict, enrichment: dict) -> dict:
    """Fold JSON-LD fields onto a document record.

    Only kind='page' is enriched. JSON-LD describes scraped web pages, and the
    key is (brand, slug) -- but 10 of the 11 location entity documents share a
    slug with their location page (agency__orlando), so an unrestricted lookup
    would silently graft page metadata onto entity records.

    Enrichment with no matching document is expected: sitemap.md is excluded
    from markdown while its JSON-LD still exists, so that key is never used.
    """
    extra = ({} if doc["kind"] != "page"
             else enrichment.get((doc["brand_slug"], doc["slug"])) or {})
    meta = dict(doc.get("meta") or {})
    for field in ("breadcrumbs", "business_nodes", "organization"):
        if extra.get(field):
            meta[field] = extra[field]

    return {
        "kind": doc["kind"],
        "path": doc["path"],
        "slug": doc["slug"],
        "title": doc["title"],
        "url": doc.get("url") or extra.get("url"),
        "category": doc.get("category"),
        "meta_description": doc.get("meta_description") or extra.get("description"),
        "date_modified": extra.get("dateModified"),
        "date_published": extra.get("datePublished"),
        "thin": doc["thin"],
        "char_len": doc["char_len"],
        "content_hash": doc["content_hash"],
        "meta": json.dumps(meta, ensure_ascii=False),
    }


def build_sizing(build: Path) -> dict:
    """The chunk sizing that produced build/, per its manifest."""
    path = build / "manifest.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("sizing", {})


def loaded_sizing(cur) -> dict:
    """The sizing the database was last successfully loaded with.

    Row-factory agnostic on purpose: this module connects with psycopg's default
    tuple factory, but embed.py calls it with dict_row. Indexing by position
    worked here and raised KeyError there.
    """
    cur.execute("select counts from kb.ingest_runs "
                "where status = 'ok' and counts -> 'sizing' is not null "
                "order by run_id desc limit 1")
    row = cur.fetchone()
    if not row:
        return {}
    counts = row["counts"] if isinstance(row, dict) else row[0]
    return (counts or {}).get("sizing") or {}


def load(conn, build: Path, run_id: int, brand_filter: str | None,
         dry_run: bool, force_chunks: bool = False) -> dict:
    stats: dict[str, object] = {}
    started = time.monotonic()

    documents = read_jsonl(build / "documents.jsonl") + \
        read_jsonl(build / "entity_documents.jsonl")
    chunks = read_jsonl(build / "chunks.jsonl") + \
        read_jsonl(build / "entity_chunks.jsonl")
    entities = read_jsonl(build / "entities.jsonl")
    enrichment_rows = read_jsonl(build / "enrichment.jsonl")
    conflicts = json.loads((PROJECT_ROOT / "KB" / "conflicts.json")
                           .read_text(encoding="utf-8"))

    if brand_filter:
        documents = [d for d in documents if d["brand_slug"] == brand_filter]
        chunks = [c for c in chunks if c["brand_slug"] == brand_filter]
        entities = [e for e in entities if e["brand_slug"] == brand_filter]
        conflicts = [c for c in conflicts if c["brand_slug"] == brand_filter]

    if not documents:
        sys.exit("no documents in build/ -- run md_ingest and json_ingest first")

    enrichment = {(r["brand_slug"], r["slug"]): r for r in enrichment_rows}

    with conn.cursor() as cur:
        brands = resolve_brands(cur)
        missing = {d["brand_slug"] for d in documents} - set(brands)
        if missing:
            sys.exit(f"no public.brands row for slug(s): {sorted(missing)}. "
                     f"Apply migrations/002_seed_brands.sql or fix BRAND_BY_DIR.")

        brand_ids = sorted({brands[d["brand_slug"]] for d in documents})
        before = existing_documents(cur, brand_ids)

        new, changed, unchanged = [], [], []
        for doc in documents:
            record = merge_enrichment(doc, enrichment)
            record["brand_id"] = brands[doc["brand_slug"]]
            key = (record["brand_id"], record["path"])
            if key not in before:
                new.append(record)
            elif before[key][1] != record["content_hash"]:
                changed.append(record)
            else:
                unchanged.append(record)

        # Distinct enrichment keys consumed -- not documents matched. Several
        # documents can share a slug, so counting documents overcounts and can
        # make "unused" negative.
        page_keys = {(d["brand_slug"], d["slug"])
                     for d in documents if d["kind"] == "page"}
        stats.update(documents_new=len(new), documents_changed=len(changed),
                     documents_unchanged=len(unchanged),
                     enrichment_unused=len(set(enrichment) - page_keys))

        if dry_run:
            stats.update(chunks_would_write=sum(
                1 for c in chunks
                if (brands[c["brand_slug"]], c["path"]) in
                {(r["brand_id"], r["path"]) for r in new + changed}))
            return stats

        # -- documents
        cur.executemany(DOCUMENT_UPSERT,
                        [{**r, "run_id": run_id} for r in new + changed])

        # doc_id map, re-read so it covers rows inserted just now
        cur.execute("select brand_id, path, doc_id from kb.documents "
                    "where brand_id = any(%s)", (brand_ids,))
        doc_ids = {(str(b), p): d for b, p, d in cur.fetchall()}

        # -- chunks: only for documents that are new or whose content changed.
        # Unchanged documents keep their rows and cost nothing.
        #
        # EXCEPT when the chunk sizing itself changed. content_hash is of the
        # cleaned DOCUMENT BODY, so re-chunking leaves every hash identical --
        # this block would report "833 unchanged, 0 chunks written" and quietly
        # keep the old chunk boundaries. The re-chunk would never apply, and
        # nothing would say so.
        #
        # build/manifest.json records the sizing that produced the build;
        # kb.ingest_runs.counts records what the database was last loaded with.
        # A mismatch forces a full chunk rebuild.
        manifest_sizing = build_sizing(build)
        db_sizing = loaded_sizing(cur)
        sizing_drift = bool(manifest_sizing) and manifest_sizing != db_sizing
        rebuild_all = force_chunks or sizing_drift

        if sizing_drift:
            print("  chunk sizing changed since the last load:")
            for key in sorted(set(manifest_sizing) | set(db_sizing)):
                was, now = db_sizing.get(key), manifest_sizing.get(key)
                if was != now:
                    print(f"    {key}: {was} -> {now}")
            print("  forcing a full chunk rebuild "
                  "(embeddings on replaced chunks are discarded)")
        elif force_chunks:
            print("  --force-chunks: rebuilding every chunk")

        if rebuild_all:
            touched = {(r["brand_id"], r["path"])
                       for r in new + changed + unchanged}
        else:
            touched = {(r["brand_id"], r["path"]) for r in new + changed}
        stats["chunks_rebuilt_all"] = int(rebuild_all)
        if manifest_sizing:
            stats["sizing"] = manifest_sizing
        touched_ids = [doc_ids[k] for k in touched if k in doc_ids]
        if touched_ids:
            cur.execute("delete from kb.chunks where doc_id = any(%s)", (touched_ids,))
            stats["chunks_deleted"] = cur.rowcount

        # doc_title is denormalised onto chunks so the generated tsvector can
        # weight it at 'A'. It is resolved here rather than carried in the
        # JSONL: it is a database-shaped concern, and repeating the parent
        # title on 1,119 chunk rows would just be redundant on disk.
        titles = {(brands[d["brand_slug"]], d["path"]): d["title"]
                  for d in documents}

        rows = []
        for chunk in chunks:
            key = (brands[chunk["brand_slug"]], chunk["path"])
            if key not in touched or key not in doc_ids:
                continue
            rows.append((
                doc_ids[key], key[0], chunk["ordinal"], chunk["heading_path"],
                titles[key], chunk["text"], chunk["char_len"],
                chunk["claim_like"],
                json.dumps(chunk.get("meta") or {}, ensure_ascii=False),
            ))

        # COPY, not executemany: round trips to Supabase run 50-200ms, so
        # per-row inserts would turn ~1,100 chunks into minutes per run.
        if rows:
            columns = ", ".join(CHUNK_COLUMNS)
            with cur.copy(f"copy kb.chunks ({columns}) from stdin") as copy:
                for row in rows:
                    copy.write_row(row)
        stats["chunks_written"] = len(rows)

        # -- entities
        if entities:
            cur.executemany(ENTITY_UPSERT, [{
                "brand_id": brands[e["brand_slug"]],
                "entity_type": e["entity_type"],
                "key": e["key"],
                "data": json.dumps(e["data"], ensure_ascii=False),
                "run_id": run_id,
            } for e in entities])
        stats["entities"] = len(entities)

        # -- conflicts
        if conflicts:
            cur.executemany(CONFLICT_UPSERT, [{
                "brand_id": brands[c["brand_slug"]],
                "topic": c["topic"],
                "detail": c["detail"],
                "sources": c.get("sources", []),
                "severity": c.get("severity", "warning"),
                "status": c.get("status", "active"),
                "correct_value": c.get("correct_value"),
                "stale_values": c.get("stale_values", []),
                "resolution": c.get("resolution"),
                "resolved_at": c.get("resolved_at"),
                "resolved_by": c.get("resolved_by"),
            } for c in conflicts])
        stats["conflicts"] = len(conflicts)
        stats["conflicts_open"] = sum(
            1 for c in conflicts if c.get("status", "active") == "active")

    stats["seconds"] = round(time.monotonic() - started, 2)
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", default=None, help="defaults to ./build")
    parser.add_argument("--brand", default=None)
    parser.add_argument("--force-chunks", action="store_true",
                        help="delete and rewrite every chunk regardless of "
                             "content_hash. Implied automatically when "
                             "build/manifest.json shows the chunk sizing "
                             "changed.")
    parser.add_argument("--dry-run", action="store_true",
                        help="report the plan, write nothing")
    args = parser.parse_args()

    build = Path(args.build) if args.build else PROJECT_ROOT / "build"
    if not build.is_dir():
        sys.exit(f"build directory not found: {build}")

    with psycopg.connect(database_url(), connect_timeout=30) as conn:
        # One transaction. A failure leaves the corpus exactly as it was
        # rather than half-loaded.
        with conn.cursor() as cur:
            cur.execute(
                "insert into kb.ingest_runs (notes) values (%s) returning run_id",
                (f"brand={args.brand or 'all'} dry_run={args.dry_run}",),
            )
            run_id = cur.fetchone()[0]

        try:
            stats = {"run_id": run_id}
            stats.update(load(conn, build, run_id, args.brand, args.dry_run,
                              args.force_chunks))
        except Exception as exc:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute(
                    "update kb.ingest_runs set status='failed', finished_at=now(), "
                    "errors=%s where run_id=%s",
                    (json.dumps([{"error": type(exc).__name__, "detail": str(exc)[:2000]}]),
                     run_id),
                )
            conn.commit()
            print(f"FAILED (run {run_id}): {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

        if args.dry_run:
            conn.rollback()
        else:
            with conn.cursor() as cur:
                cur.execute(
                    "update kb.ingest_runs set status='ok', finished_at=now(), "
                    "counts=%s where run_id=%s",
                    (json.dumps(stats), run_id),
                )
            conn.commit()

    print(f"\nrun {run_id}" + ("  (dry run, rolled back)" if args.dry_run else ""))
    for key, value in stats.items():
        if key != "run_id":
            print(f"  {key:<22} {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
