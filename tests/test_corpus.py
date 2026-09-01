"""Invariants over the built corpus in build/.

These are the checks that were run by hand, once, in throwaway scripts during
development. Pinning them here means a future change to normalize.py or
md_ingest.py cannot silently regress the corpus.

Skipped if build/ has not been generated:
    python -m Ingestion.md_ingest && python -m Ingestion.json_ingest
"""

import json
from collections import Counter
from pathlib import Path

import pytest

from Ingestion.md_ingest import (
    MAX_CHARS,
    MIN_CHARS,
    OVERLAP_CHARS,
    SPLIT_THRESHOLD,
    current_sizing,
)

BUILD = Path(__file__).resolve().parent.parent / "build"

#: 532 Agency Height + 56 Renegade, per the corpus READMEs. JSON-LD holds 661
#: Question nodes; the extra 73 are the same questions repeated across pages,
#: which build_kb.py already deduplicated. Ingesting both sources would give
#: 1,249 FAQ chunks with 661 partial duplicates.
EXPECTED_FAQS = 588


def _load(name):
    path = BUILD / f"{name}.jsonl"
    if not path.is_file():
        pytest.skip(f"{path} not built")
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


@pytest.fixture(scope="module")
def documents():
    return _load("documents") + _load("entity_documents")


@pytest.fixture(scope="module")
def chunks():
    return _load("chunks") + _load("entity_chunks")


@pytest.fixture(scope="module")
def fresh_build():
    """Skip the size invariants when build/ predates the current chunk sizing.

    Those tests compare build/ against the CURRENT constants in md_ingest, so a
    stale build fails them as a 122-item list diff that names the symptom and
    not the cause. build/manifest.json records the sizing that produced the
    build, which turns that into one legible line.
    """
    path = BUILD / "manifest.json"
    if not path.is_file():
        pytest.skip("build/manifest.json missing -- regenerate build/: "
                    "python -m Ingestion.md_ingest")
    built = json.loads(path.read_text(encoding="utf-8")).get("sizing", {})
    drift = {k: (built.get(k), v) for k, v in current_sizing().items()
             if built.get(k) != v}
    if drift:
        detail = ", ".join(f"{k} built={b} now={n}" for k, (b, n) in drift.items())
        pytest.skip("build/ predates the current chunk sizing (" + detail
                    + ") -- regenerate: python -m Ingestion.md_ingest && "
                      "python -m Ingestion.json_ingest")


# ------------------------------------------------------------------ shape

def test_faq_count_is_exact(documents):
    assert sum(1 for d in documents if d["kind"] == "faq") == EXPECTED_FAQS


def test_exactly_two_primers_one_per_brand(documents):
    primers = [d for d in documents if d["kind"] == "primer"]
    assert len(primers) == 2
    assert {p["brand_slug"] for p in primers} == {"renegade", "agencyheight"}


def test_every_document_has_a_known_brand(documents):
    assert {d["brand_slug"] for d in documents} == {"renegade", "agencyheight"}


def test_every_document_has_a_category(documents):
    """The four files build_kb.py derived rather than scraped carry no
    frontmatter; they fall back to their containing directory. Landing them in
    retrieval with category NULL would be a quiet loss -- company-profile.md is
    what the brand primer is written from."""
    assert [d["path"] for d in documents if not d.get("category")] == []


# -------------------------------------------------------------- integrity

def test_brand_and_path_are_unique(documents):
    """kb.documents has UNIQUE (brand_id, path); a duplicate would abort the
    load rather than merge."""
    keys = Counter((d["brand_slug"], d["path"]) for d in documents)
    assert [k for k, n in keys.items() if n > 1] == []


def test_every_chunk_has_a_parent_document(chunks, documents):
    known = {(d["brand_slug"], d["path"]) for d in documents}
    orphans = [(c["brand_slug"], c["path"]) for c in chunks
               if (c["brand_slug"], c["path"]) not in known]
    assert orphans == []


def test_chunk_ordinals_are_contiguous_from_zero(chunks):
    by_doc: dict[tuple, list[int]] = {}
    for c in chunks:
        by_doc.setdefault((c["brand_slug"], c["path"]), []).append(c["ordinal"])
    bad = {k: sorted(v) for k, v in by_doc.items()
           if sorted(v) != list(range(len(v)))}
    assert bad == {}


# ----------------------------------------------------------------- content

def test_no_empty_chunks(chunks):
    """kb.chunks enforces this with a CHECK constraint too. An empty chunk is a
    row with an empty tsvector: never matches, always costs."""
    assert [c["path"] for c in chunks if not c["text"].strip()] == []


def test_no_residual_mojibake(chunks):
    """The extractors wrote UTF-8 decoded as cp1252. A stray 'â' means ftfy
    missed a pattern -- or something re-read a file with the wrong encoding."""
    suspects = [c["path"] for c in chunks
                if "â" in c["text"] or "�" in c["text"]]
    assert suspects == []


def test_no_undecoded_html_entities(chunks):
    for entity in ("&amp;", "&lt;", "&gt;", "&#8217;", "&nbsp;"):
        hits = [c["path"] for c in chunks if entity in c["text"]]
        assert hits == [], f"{entity} in {hits[:3]}"


def test_no_template_chrome_survives(chunks):
    """Each of these was found interspersed through the corpus, not as a
    leading block -- which is why chrome removal is line-wise."""
    for phrase in ("Enter Email Address", "Primary Menu", "Customer Login",
                   "Search Agents", "Quick Links",
                   "Compare Quotes from Top Local Agents"):
        hits = [c["path"] for c in chunks if phrase in c["text"]]
        assert hits == [], f"{phrase!r} survives in {hits[:3]}"


# -------------------------------------------------------------- chunk size

def test_split_chunks_respect_the_ceiling(chunks, fresh_build):
    """Applies only to chunks produced by splitting. A whole document under
    SPLIT_THRESHOLD is legitimately one chunk of up to 7,999 chars -- checking
    those against MAX_CHARS flags 66 correct rows."""
    counts = Counter((c["brand_slug"], c["path"]) for c in chunks)
    from_split = [c for c in chunks if counts[(c["brand_slug"], c["path"])] > 1]
    over = [(c["path"], c["char_len"]) for c in from_split
            if c["char_len"] > MAX_CHARS + OVERLAP_CHARS + 200]
    assert over == []


def test_whole_document_chunks_stay_under_the_split_threshold(chunks, fresh_build):
    counts = Counter((c["brand_slug"], c["path"]) for c in chunks)
    singles = [c for c in chunks if counts[(c["brand_slug"], c["path"])] == 1]
    over = [(c["path"], c["char_len"]) for c in singles
            if c["char_len"] >= SPLIT_THRESHOLD]
    # entity and faq chunks are single by construction and always small;
    # a page above the threshold should have been split.
    assert [p for p, _ in over if p.endswith(".md")] == []


def test_claim_like_is_a_minority_but_not_absent(chunks):
    flagged = sum(1 for c in chunks if c["claim_like"])
    assert 0 < flagged < len(chunks) / 2


# ------------------------------------------------------------- enrichment

def test_enrichment_has_full_freshness_coverage():
    rows = _load("enrichment")
    assert rows
    assert all(r.get("dateModified") for r in rows)
    assert all(r.get("breadcrumbs") for r in rows)


def test_faq_nodes_are_never_taken_from_json_ld():
    """md_ingest owns FAQs. json_ingest must skip FAQPage/Question/Answer, or
    the corpus gains 661 partial duplicates of the 588 deduplicated pairs."""
    skipped: Counter = Counter()
    for row in _load("enrichment"):
        skipped.update(row.get("skipped_node_counts", {}))
    assert skipped["Question"] > 0
    assert skipped["Answer"] > 0
    assert skipped["FAQPage"] > 0


def test_entity_rows_cover_locations_and_licences():
    rows = _load("entities")
    kinds = Counter(r["entity_type"] for r in rows)
    assert kinds["location"] == 11
    # 49 jurisdictions, not 48 states: the list includes the District of
    # Columbia. This pin was 48 and was wrong -- the live licence-disclosures
    # page misspells "Massachuetts", so that row was silently dropped during
    # extraction and the pin locked the loss in. Alaska and Hawaii are the two
    # states genuinely absent.
    assert kinds["state_license"] == 49
