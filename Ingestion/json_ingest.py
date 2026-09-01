"""JSON -> entity documents, plus enrichment for documents md_ingest created.

Two unrelated jobs that happen to share a filename:

  Job A  entity JSON (locations, state licenses) -> new documents + typed rows
  Job B  JSON-LD in _structured-data/            -> enrichment only, no new docs

Job B creates nothing. Those 232 files are metadata *about* pages md_ingest has
already emitted, so they are keyed by brand+slug and merged onto documents at
load time in ingestion_service.py.

    python -m Ingestion.json_ingest
    python -m Ingestion.json_ingest --report-only

Output: build/entity_documents.jsonl, build/entity_chunks.jsonl,
        build/enrichment.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from .md_ingest import BRAND_BY_DIR, Chunk, Document, slugify
from .normalize import fix_text, looks_like_claim

# --------------------------------------------------------------------------
# What to harvest, and what to ignore
# --------------------------------------------------------------------------

#: JSON-LD nodes that are byte-identical on every page: one WebSite, one
#: SearchAction, one ReadAction per file, carrying no page-specific fact.
#: Indexing them would put 180 copies of the same text in the corpus.
BOILERPLATE_TYPES = frozenset({
    "WebSite", "SearchAction", "EntryPoint", "ReadAction",
    "PropertyValueSpecification", "ImageObject",
})

#: Q&A lives in faqs.md, which build_kb.py already deduplicated.
#: Measured: JSON-LD holds 661 Question nodes (101 renegade + 560 agencyheight)
#: against 588 unique in faqs.md. The extra 73 are the same questions repeated
#: across pages. Ingesting both would create 661 partial duplicates, so FAQ
#: nodes are skipped here and md_ingest owns FAQs outright.
FAQ_TYPES = frozenset({"FAQPage", "Question", "Answer"})

#: Rare but genuinely informative nodes. Agency Height carries single
#: InsuranceAgency / PostalAddress / AggregateRating nodes plus a handful of
#: Offer and Service entries -- a real address and a real rating.
BUSINESS_TYPES = frozenset({
    "InsuranceAgency", "PostalAddress", "AggregateRating",
    "Offer", "OfferCatalog", "Service", "Product", "WebApplication",
})

#: Files in _structured-data/ that are aggregates, not per-page documents.
#: organizations.json has no matching page and must not reach the matcher.
NON_PAGE_JSON = frozenset({"organizations.json"})

DAY_ORDER = ("Monday", "Tuesday", "Wednesday", "Thursday",
             "Friday", "Saturday", "Sunday")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def walk_nodes(node, out: list[tuple[str, dict]]) -> None:
    """Collect every dict carrying an @type, at any depth.

    Recursive on purpose. A top-level @graph walk misses BreadcrumbList's
    nested ListItems and every Question node, because FAQPage entries hang off
    mainEntity rather than sitting in @graph. That mistake reported 0 Question
    nodes where there are in fact 661.
    """
    if isinstance(node, dict):
        types = node.get("@type")
        if types:
            for t in (types if isinstance(types, list) else [types]):
                out.append((t, node))
        for value in node.values():
            walk_nodes(value, out)
    elif isinstance(node, list):
        for value in node:
            walk_nodes(value, out)


# --------------------------------------------------------------------------
# Job A -- entity JSON
# --------------------------------------------------------------------------

def _render_location(rec: dict) -> str:
    """Readable text for retrieval. Structured fields also go to meta."""
    lines = [rec.get("name") or rec.get("slug", "")]
    if rec.get("closed"):
        lines.append("STATUS: This location is closed.")
    if rec.get("status_note"):
        lines.append(rec["status_note"])
    if rec.get("address"):
        lines.append(f"Address: {rec['address']}")
    for label, key in (("Phone", "phone"), ("Email", "email")):
        if rec.get(key):
            lines.append(f"{label}: {rec[key]}")
    for label, key in (("Other phones", "other_phones"),
                       ("Other emails", "other_emails")):
        value = rec.get(key)
        if value:
            joined = ", ".join(value) if isinstance(value, list) else str(value)
            lines.append(f"{label}: {joined}")
    if rec.get("agency_manager"):
        lines.append(f"Agency manager: {rec['agency_manager']}")

    hours = rec.get("hours_by_day") or {}
    if hours:
        lines.append("Hours:")
        for day in DAY_ORDER:
            if day in hours:
                lines.append(f"  {day}: {hours[day]}")
    return fix_text("\n".join(line for line in lines if line))


def locations_documents(
    path: Path, brand: str, rel: str
) -> tuple[list[Document], list[dict]]:
    """One document per location. 11 records, each a distinct real place."""
    records = json.loads(path.read_text(encoding="utf-8"))
    docs: list[Document] = []
    entities: list[dict] = []

    for rec in records:
        slug = rec.get("slug") or slugify(rec.get("name", "location"))
        text = _render_location(rec)
        title = fix_text(rec.get("name") or slug)
        docs.append(Document(
            brand_slug=brand,
            kind="entity",
            path=f"{rel}#{slug}",
            slug=slug,
            title=title,
            url=rec.get("url"),
            category="locations",
            meta_description=None,
            char_len=len(text),
            content_hash=_sha(text),
            thin=False,
            meta={"entity_type": "location", **rec},
            chunks=(Chunk(0, title, text, len(text), looks_like_claim(text),
                          {"entity_type": "location"}),),
        ))
        entities.append({"brand_slug": brand, "entity_type": "location",
                         "key": slug, "data": rec})
    return docs, entities


#: Jurisdictions that hold insurance licences but are not states. Only DC
#: appears in the corpus today; the rest are here so a later addition does not
#: silently inflate a state count.
NON_STATE_JURISDICTIONS = frozenset({
    "District of Columbia", "Puerto Rico", "Guam", "American Samoa",
    "U.S. Virgin Islands", "Northern Mariana Islands",
})


def licence_title(records: list[dict]) -> str:
    """Title the licence document by what the list actually contains.

    len(records) is NOT the number of states: the list includes the District of
    Columbia. Titling it "(49 states)" would put a wrong figure in the one
    place retrieval is most likely to quote verbatim -- a document title is
    weighted 'A' in the tsvector -- and it would contradict the approved claim,
    which reads "48 states and the District of Columbia".
    """
    states = sum(1 for r in records
                 if r.get("state") not in NON_STATE_JURISDICTIONS)
    extra = sorted(r["state"] for r in records
                   if r.get("state") in NON_STATE_JURISDICTIONS)
    if not extra:
        return f"State insurance licenses ({states} states)"
    tail = ", ".join(f"the {e}" for e in extra)
    return f"State insurance licenses ({states} states and {tail})"


def licenses_documents(
    path: Path, brand: str, rel: str
) -> tuple[list[Document], list[dict]]:
    """ONE document for all 49 jurisdictions, not 49 documents.

    A license number is a single line. Forty-nine near-identical documents
    would compete for every query mentioning a state and crowd out real
    content. So retrieval gets one document that answers "which states are you
    licensed in", and the typed kb.entities rows answer "are you licensed in
    Ohio" exactly, via SQL, with no ranking involved.
    """
    records = json.loads(path.read_text(encoding="utf-8"))
    title = licence_title(records)
    lines = [title] + [
        f"{r['state']}: {r['license_number']}" for r in records if r.get("state")
    ]
    text = fix_text("\n".join(lines))

    doc = Document(
        brand_slug=brand,
        kind="entity",
        path=f"{rel}#all-states",
        slug="state-licenses",
        title=title,
        url=None,
        category="legal",
        meta_description=None,
        char_len=len(text),
        content_hash=_sha(text),
        thin=False,
        # state_count was len(records), which counted DC as a state. Both are
        # recorded now so a consumer cannot pick the wrong one by accident.
        meta={"entity_type": "state_license_list",
              "jurisdiction_count": len(records),
              "state_count": sum(
                  1 for r in records
                  if r.get("state") not in NON_STATE_JURISDICTIONS)},
        chunks=(Chunk(0, "State insurance licenses", text, len(text),
                      looks_like_claim(text),
                      {"entity_type": "state_license_list"}),),
    )
    entities = [
        {"brand_slug": brand, "entity_type": "state_license",
         "key": r["state"], "data": r}
        for r in records if r.get("state")
    ]
    return [doc], entities


ENTITY_HANDLERS = {
    "locations.json": locations_documents,
    "state-licenses.json": licenses_documents,
}


# --------------------------------------------------------------------------
# Job B -- JSON-LD enrichment
# --------------------------------------------------------------------------

def _breadcrumbs(nodes: list[tuple[str, dict]]) -> list[str]:
    items = [n for t, n in nodes if t == "ListItem" and n.get("name")]
    items.sort(key=lambda n: n.get("position", 0))
    return [fix_text(n["name"]) for n in items]


def enrichment_for(path: Path, brand: str, slug: str) -> dict:
    """Harvest the few fields worth keeping from one page's JSON-LD."""
    nodes: list[tuple[str, dict]] = []
    walk_nodes(json.loads(path.read_text(encoding="utf-8")), nodes)

    record: dict = {"brand_slug": brand, "slug": slug}
    skipped: Counter = Counter()
    business: list[dict] = []

    for node_type, node in nodes:
        if node_type in FAQ_TYPES or node_type in BOILERPLATE_TYPES:
            skipped[node_type] += 1
            continue

        if node_type == "WebPage":
            for field in ("dateModified", "datePublished", "inLanguage", "url"):
                if node.get(field):
                    record[field] = node[field]
            if node.get("description"):
                record["description"] = fix_text(node["description"])

        elif node_type == "Organization" and node.get("name"):
            record["organization"] = fix_text(node["name"])

        elif node_type in BUSINESS_TYPES:
            business.append({"type": node_type, **{
                k: v for k, v in node.items()
                if k not in {"@type", "@id"} and isinstance(v, (str, int, float))
            }})

    crumbs = _breadcrumbs(nodes)
    if crumbs:
        record["breadcrumbs"] = crumbs
    if business:
        record["business_nodes"] = business
    record["skipped_node_counts"] = dict(skipped)
    return record


def load_manifest(kb_dir: Path) -> dict[str, dict]:
    """slug -> manifest entry. The join table between .json and .md.

    _structured-data/about-us.json is named by slug, but the markdown lives at
    <brand>/kb/company/about-us.md -- the category directory is not in the JSON
    filename. The manifest already carries slug, category, title and url, so it
    is the correct join rather than guessing the directory.
    """
    path = kb_dir / "_manifest.json"
    if not path.is_file():
        return {}
    return {e["slug"]: e for e in json.loads(path.read_text(encoding="utf-8"))}


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

def collect(root: Path) -> tuple[list[Document], list[dict], list[dict], list[str]]:
    docs: list[Document] = []
    entities: list[dict] = []
    enrichment: list[dict] = []
    notes: list[str] = []

    for brand_dir in sorted(BRAND_BY_DIR):
        brand = BRAND_BY_DIR[brand_dir]
        kb_dir = root / brand_dir / "kb"
        if not kb_dir.is_dir():
            notes.append(f"{brand}: no kb/ directory")
            continue

        # -- Job A
        for name, handler in ENTITY_HANDLERS.items():
            path = kb_dir / name
            if not path.is_file():
                notes.append(f"{brand}: {name} absent")
                continue
            rel = path.relative_to(root).as_posix()
            new_docs, new_entities = handler(path, brand, rel)
            docs.extend(new_docs)
            entities.extend(new_entities)
            notes.append(f"{brand}: {name} -> {len(new_docs)} docs, "
                         f"{len(new_entities)} entity rows")

        # -- Job B. NOTE: normalize.is_excluded() rejects _structured-data,
        # which is right for markdown and wrong here. Do not use it.
        sd_dir = kb_dir / "_structured-data"
        if not sd_dir.is_dir():
            notes.append(f"{brand}: no _structured-data/")
            continue

        manifest = load_manifest(kb_dir)
        matched = 0
        unmatched: list[str] = []
        for path in sorted(sd_dir.glob("*.json")):
            if path.name in NON_PAGE_JSON:
                continue
            slug = path.stem
            if slug not in manifest:
                unmatched.append(slug)
                continue
            matched += 1
            enrichment.append(enrichment_for(path, brand, slug))

        notes.append(f"{brand}: JSON-LD matched {matched}, "
                     f"unmatched {len(unmatched)}, manifest slugs {len(manifest)}")
        for slug in unmatched[:5]:
            notes.append(f"{brand}:   no manifest entry for {slug!r}")

    return docs, entities, enrichment, notes


def write_outputs(docs, entities, enrichment, out_dir: Path) -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}

    doc_file = out_dir / "entity_documents.jsonl"
    with doc_file.open("w", encoding="utf-8", newline="\n") as fh:
        for doc in docs:
            record = {k: v for k, v in asdict(doc).items() if k != "chunks"}
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    counts["entity_documents"] = len(docs)

    n_chunks = 0
    chunk_file = out_dir / "entity_chunks.jsonl"
    with chunk_file.open("w", encoding="utf-8", newline="\n") as fh:
        for doc in docs:
            for chunk in doc.chunks:
                row = {"brand_slug": doc.brand_slug, "path": doc.path,
                       **asdict(chunk)}
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                n_chunks += 1
    counts["entity_chunks"] = n_chunks

    for name, rows in (("entities", entities), ("enrichment", enrichment)):
        with (out_dir / f"{name}.jsonl").open("w", encoding="utf-8",
                                              newline="\n") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        counts[name] = len(rows)
    return counts


def report(docs, entities, enrichment, notes) -> None:
    print("\n-- collection log")
    for note in notes:
        print(f"   {note}")

    print(f"\nentity documents     {len(docs)}")
    for (brand, category), n in sorted(
        Counter((d.brand_slug, d.category) for d in docs).items()
    ):
        print(f"  {brand:<14} {str(category):<18} {n}")

    print(f"entity rows          {len(entities)}")
    for entity_type, n in Counter(e["entity_type"] for e in entities).most_common():
        print(f"  {entity_type:<31} {n}")

    print(f"\nenrichment records   {len(enrichment)}")
    have: Counter = Counter()
    for rec in enrichment:
        for field in ("dateModified", "datePublished", "description",
                      "breadcrumbs", "organization", "business_nodes"):
            if rec.get(field):
                have[field] += 1
    for field, n in have.most_common():
        print(f"  {field:<20} {n:>4} / {len(enrichment)}")

    skipped: Counter = Counter()
    for rec in enrichment:
        skipped.update(rec.get("skipped_node_counts", {}))
    print("\nnodes skipped (boilerplate + FAQ owned by md_ingest)")
    for node_type, n in skipped.most_common():
        print(f"  {node_type:<28} {n:>5}")

    if enrichment:
        print("\nsample enrichment record")
        sample = next((r for r in enrichment if r.get("breadcrumbs")), enrichment[0])
        print(json.dumps(sample, ensure_ascii=False, indent=1)[:700])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb-root", default=None, help="defaults to ./KB")
    parser.add_argument("--out", default=None, help="defaults to ./build")
    parser.add_argument("--report-only", action="store_true",
                        help="measure without writing JSONL")
    args = parser.parse_args()

    project = Path(__file__).resolve().parent.parent
    root = Path(args.kb_root) if args.kb_root else project / "KB"
    out_dir = Path(args.out) if args.out else project / "build"

    if not root.is_dir():
        print(f"KB root not found: {root}")
        return 1

    docs, entities, enrichment, notes = collect(root)
    report(docs, entities, enrichment, notes)

    if args.report_only:
        print("\n(report only -- nothing written)")
    else:
        counts = write_outputs(docs, entities, enrichment, out_dir)
        print(f"\nwrote {counts} -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
