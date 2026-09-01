"""Markdown -> Document + Chunk records.

Consumes NormalizedDoc from normalize.py and emits JSONL. Deliberately writes
no SQL and opens no connection: chunk boundaries are the thing you iterate on,
and iterating must not cost a network round trip.

    python -m Ingestion.md_ingest                    # whole corpus
    python -m Ingestion.md_ingest --brand renegade   # one brand
    python -m Ingestion.md_ingest --report-only      # measure, write nothing

Output: build/documents.jsonl, build/chunks.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .normalize import NormalizedDoc, looks_like_claim, normalize_file

# --------------------------------------------------------------------------
# Sizing
# --------------------------------------------------------------------------

#: Documents smaller than this become a single chunk.
#:
#: Lowered from 8,000 for embeddings (migration 009). Length is irrelevant to a
#: tsvector -- term positions are term positions -- so 8,000 was the right call
#: while retrieval was lexical-only. It is the wrong call for vectors: one
#: embedding has to represent the whole chunk, and a chunk covering several
#: topics averages into something retrievable for none of them.
#:
#: Measured across the corpus at the old settings, PAGE chunks had a median of
#: 2,842 chars (~710 tokens) and 29.6% of all chunks exceeded 2,500. At these
#: settings the page median is 1,446 (~360 tokens) and only 4.4% exceed 2,500 --
#: those being chunks legitimately at MAX_CHARS + OVERLAP_CHARS.
#:
#: Cost of the change: 1,107 chunks -> 1,759 (+59%), and 441k -> 472k embedding
#: tokens (+7%, all of it added overlap).
SPLIT_THRESHOLD = 2_500

#: Aim per chunk when splitting. ~400 tokens at the usual ~4 chars/token for
#: English prose. Approximate on purpose -- the threshold needs to be sane,
#: not exact, and a tokenizer is a dependency this stage does not need.
#:
#: Must stay below SPLIT_THRESHOLD. If it exceeds it, a document just over the
#: threshold gets "split" into one chunk larger than the target and the sizing
#: stops meaning anything.
TARGET_CHARS = 1_600

#: Hard ceiling. A ## section above this gets a secondary paragraph split.
#: insurance-news.md is one 14,399-char section and needs several.
MAX_CHARS = 2_400

#: Tail of the previous chunk repeated at the head of the next, so a fact
#: spanning a boundary stays findable. Only applied when a document is split.
OVERLAP_CHARS = 200

#: Floor for a PAGE chunk. Below this a chunk is merged into its neighbour
#: rather than emitted.
#:
#: Exists because of a defect the smaller TARGET_CHARS exposed. The preamble
#: before a document's first ## heading -- the title and a line of intro -- is
#: its own section, typically 50-160 chars. At TARGET_CHARS 3,200 it always got
#: packed together with the first real section; at 1,600 the very next section
#: overflows the target and flushes the preamble on its own. That produced 8
#: chunks like "# Home Insurance Calculator" (58 chars): an embedding call spent
#: on a title, and a vector in the index that can never usefully match.
#:
#: Does NOT apply to FAQs, which are atomic and legitimately short -- they use
#: FAQ_THIN_CHARS below.
MIN_CHARS = 200

#: FAQ answers run 100-400 chars, so the page thin threshold (500) would flag
#: nearly all 588 of them. They are atomic by nature; only true blanks are thin.
FAQ_THIN_CHARS = 40

BRAND_BY_DIR = {
    "renegade-kb": "renegade",
    "agencyheight-kb": "agencyheight",
}

# --------------------------------------------------------------------------
# Patterns
# --------------------------------------------------------------------------

#: Level-2 heading only. "### Foo" does not match, because after ## comes a
#: '#' rather than whitespace. Renegade uses # for the title and ## for
#: sections; Agency Height has no # at all and starts at ##. So ## is the
#: section boundary for both brands -- one rule, no per-brand branching.
_SECTION_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.M)

#: A FAQ entry in faqs.md.
_FAQ_SPLIT_RE = re.compile(r"^###[ \t]+", re.M)

#: The attribution line under each FAQ answer, e.g.
#: `source: https://renegadeinsurance.com/about-us/`
_FAQ_SOURCE_RE = re.compile(r"^`source:\s*(.+?)`[ \t]*$", re.M)

_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")
_PARA_BREAK_RE = re.compile(r"\n[ \t]*\n")
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Chunk:
    ordinal: int
    heading_path: str
    text: str
    char_len: int
    claim_like: bool
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Document:
    brand_slug: str
    kind: str                    # page | faq | primer
    path: str                    # posix, relative to KB root; natural key
    slug: str
    title: str
    url: str | None
    category: str | None
    meta_description: str | None
    char_len: int
    content_hash: str
    thin: bool
    meta: dict
    chunks: tuple[Chunk, ...]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def brand_for(path: str) -> str | None:
    """Map the top-level KB directory to a brands.slug value.

    Returns the slug, never a uuid: the lookup against public.brands belongs to
    ingestion_service.py, so this stage stays database-free.
    """
    return BRAND_BY_DIR.get(path.split("/", 1)[0])


def slugify(text: str, maxlen: int = 60) -> str:
    return _SLUG_STRIP_RE.sub("-", text.lower()).strip("-")[:maxlen].strip("-")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def split_sections(body: str) -> list[tuple[str, str]]:
    """Split on ## headings -> [(heading, text_including_heading)].

    Text before the first heading is returned with an empty heading rather than
    discarded -- on Agency Height pages that preamble is the page title and
    intro, which is real content.
    """
    matches = list(_SECTION_RE.finditer(body))
    if not matches:
        return [("", body.strip())]

    sections: list[tuple[str, str]] = []
    preamble = body[: matches[0].start()].strip()
    if preamble:
        sections.append(("", preamble))

    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections.append((match.group(1).strip(), body[match.start():end].strip()))
    return sections


def _split_sentences(text: str, limit: int) -> list[str]:
    """Last resort for a single paragraph above the ceiling. Never mid-sentence."""
    out: list[str] = []
    current = ""
    for sentence in _SENTENCE_END_RE.split(text):
        if current and len(current) + len(sentence) + 1 > limit:
            out.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}" if current else sentence
    if current:
        out.append(current)
    return out


def split_paragraphs(text: str, limit: int) -> list[str]:
    """Break an oversized section on blank-line paragraph boundaries."""
    blocks: list[str] = []
    current = ""
    for para in _PARA_BREAK_RE.split(text):
        para = para.strip()
        if not para:
            continue
        if current and len(current) + len(para) + 2 > limit:
            blocks.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        blocks.append(current)

    out: list[str] = []
    for block in blocks:
        out.extend([block] if len(block) <= limit else _split_sentences(block, limit))
    return out


def _join_headings(headings: list[str], maxlen: int = 120) -> str:
    named = [h for h in headings if h]
    if not named:
        return ""
    joined = " / ".join(dict.fromkeys(named))
    return joined if len(joined) <= maxlen else joined[:maxlen].rsplit(" / ", 1)[0]


def pack_sections(sections: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Accumulate sections toward TARGET_CHARS, splitting any that exceed MAX."""
    packed: list[tuple[str, str]] = []
    texts: list[str] = []
    headings: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal texts, headings, size
        if texts:
            packed.append((_join_headings(headings), "\n\n".join(texts)))
            texts, headings, size = [], [], 0

    for heading, text in sections:
        if len(text) > MAX_CHARS:
            flush()
            for piece in split_paragraphs(text, MAX_CHARS):
                packed.append((heading, piece))
            continue

        over_target = bool(size) and size + len(text) > TARGET_CHARS
        # A runt absorbs the next section even past TARGET, provided the result
        # still fits under MAX. Overshooting the target by a few hundred chars
        # is cheap; emitting a 58-char title as its own chunk is not.
        absorbing_runt = 0 < size < MIN_CHARS and size + len(text) <= MAX_CHARS
        if over_target and not absorbing_runt:
            flush()

        texts.append(text)
        headings.append(heading)
        size += len(text)

    flush()

    # Trailing runt: the same problem at the other end of the document. Fold it
    # back into the previous piece when it fits. Not observed in this corpus,
    # but it is the same defect and costs three lines to close.
    if len(packed) > 1 and len(packed[-1][1]) < MIN_CHARS:
        prev_heading, prev_text = packed[-2]
        last_heading, last_text = packed[-1]
        if len(prev_text) + len(last_text) <= MAX_CHARS:
            packed[-2:] = [(_join_headings([prev_heading, last_heading]),
                            "\n\n".join([prev_text, last_text]))]

    return packed


def add_overlap(pieces: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Prepend the previous chunk's tail, trimmed to a word boundary."""
    if OVERLAP_CHARS <= 0:
        return pieces
    out: list[tuple[str, str]] = []
    for i, (heading, text) in enumerate(pieces):
        if i:
            tail = pieces[i - 1][1][-OVERLAP_CHARS:]
            space = tail.find(" ")
            if space != -1:
                tail = tail[space + 1:]
            text = f"{tail}\n\n{text}"
        out.append((heading, text))
    return out


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

def chunk_page(doc: NormalizedDoc) -> list[Chunk]:
    """One chunk per document, unless it is large enough to need splitting.

    Returns no chunks for an empty body. Two pages normalize to zero chars --
    their entire content was the quote widget -- and an empty chunk would only
    add a row with an empty tsvector. The Document is still emitted so the
    corpus inventory stays complete.
    """
    if not doc.body.strip():
        return []
    if doc.char_len < SPLIT_THRESHOLD:
        return [Chunk(0, "", doc.body, doc.char_len, looks_like_claim(doc.body))]

    pieces = add_overlap(pack_sections(split_sections(doc.body)))
    return [
        Chunk(i, heading, text, len(text), looks_like_claim(text))
        for i, (heading, text) in enumerate(pieces)
    ]


def category_for(doc: NormalizedDoc) -> str | None:
    """Frontmatter category, else the containing directory.

    Four documents have no frontmatter because build_kb.py derived them rather
    than scraping them -- company-profile.md, coverage-catalog.md,
    locations/_directory.md, legal/state-licenses.md. They are among the most
    useful files in the corpus (company-profile.md is what the brand primer
    gets written from), so they should not land in retrieval uncategorised.
    """
    if doc.category:
        return doc.category
    parts = doc.path.split("/")
    return parts[-2] if len(parts) >= 2 else None


def page_document(doc: NormalizedDoc, brand: str, kind: str) -> Document:
    return Document(
        brand_slug=brand,
        kind=kind,
        path=doc.path,
        slug=doc.slug,
        title=doc.title,
        url=doc.source_url,
        category=category_for(doc),
        meta_description=doc.meta_description,
        char_len=doc.char_len,
        content_hash=doc.content_hash,
        thin=doc.thin,
        meta={"warnings": list(doc.warnings)} if doc.warnings else {},
        chunks=tuple(chunk_page(doc)),
    )


def faq_documents(doc: NormalizedDoc, brand: str) -> list[Document]:
    """One document per FAQ, not one document for faqs.md.

    Each ### block carries its own `source:` URL pointing at the page it was
    harvested from, and kb.documents.url is per-document. Bundling all 588
    under one document would make every citation point at faqs.md instead of
    the real source page.

    The chunk text keeps the question above the answer: a bare answer loses the
    context that makes it retrievable, and the question also lands in the
    tsvector at weight B via heading_path.
    """
    out: list[Document] = []
    seen: dict[str, int] = {}

    for block in _FAQ_SPLIT_RE.split(doc.body)[1:]:
        question, _, remainder = block.partition("\n")
        question = question.strip()
        if not question:
            continue

        match = _FAQ_SOURCE_RE.search(remainder)
        url = match.group(1).strip() if match else None
        answer = _FAQ_SOURCE_RE.sub("", remainder).strip()

        slug = slugify(question) or "faq"
        seen[slug] = seen.get(slug, 0) + 1
        if seen[slug] > 1:                      # UNIQUE (brand_id, path) guard
            slug = f"{slug}-{seen[slug]}"

        text = f"{question}\n\n{answer}".strip()
        out.append(Document(
            brand_slug=brand,
            kind="faq",
            path=f"{doc.path}#{slug}",
            slug=slug,
            title=question,
            url=url,
            category="faq",
            meta_description=None,
            char_len=len(answer),
            content_hash=_sha(text),
            thin=len(answer) < FAQ_THIN_CHARS,
            meta={"question": question, "source_url": url},
            chunks=(Chunk(0, question, text, len(text), looks_like_claim(text),
                          {"question": question}),),
        ))
    return out


def ingest_file(path: Path, root: Path) -> list[Document]:
    """Route one file. Returns [] for excluded files and unknown brands."""
    doc = normalize_file(path, root=root)
    if doc.excluded:
        return []

    brand = brand_for(doc.path)
    if brand is None:
        return []

    name = doc.path.rsplit("/", 1)[-1]
    if name == "faqs.md":
        return faq_documents(doc, brand)
    if "/primers/" in f"/{doc.path}":
        return [page_document(doc, brand, "primer")]
    return [page_document(doc, brand, "page")]


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

def collect(root: Path, brand: str | None = None) -> list[Document]:
    """Walk the corpus in sorted order. Sorted so output is byte-reproducible."""
    docs: list[Document] = []
    for path in sorted(root.rglob("*.md")):
        for document in ingest_file(path, root):
            if brand is None or document.brand_slug == brand:
                docs.append(document)
    return docs


def write_jsonl(docs: list[Document], out_dir: Path) -> tuple[int, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    doc_path = out_dir / "documents.jsonl"
    chunk_path = out_dir / "chunks.jsonl"

    n_chunks = 0
    with (doc_path.open("w", encoding="utf-8", newline="\n") as fd,
          chunk_path.open("w", encoding="utf-8", newline="\n") as fc):
        for document in docs:
            record = {k: v for k, v in asdict(document).items() if k != "chunks"}
            fd.write(json.dumps(record, ensure_ascii=False) + "\n")
            for chunk in document.chunks:
                # brand+path repeated so the loader needs no in-memory join
                row = {"brand_slug": document.brand_slug, "path": document.path,
                       **asdict(chunk)}
                fc.write(json.dumps(row, ensure_ascii=False) + "\n")
                n_chunks += 1

    write_manifest(out_dir, len(docs), n_chunks)
    return len(docs), n_chunks


def current_sizing() -> dict:
    """The sizing constants a fresh build would use."""
    return {
        "SPLIT_THRESHOLD": SPLIT_THRESHOLD,
        "TARGET_CHARS": TARGET_CHARS,
        "MAX_CHARS": MAX_CHARS,
        "MIN_CHARS": MIN_CHARS,
        "OVERLAP_CHARS": OVERLAP_CHARS,
        "FAQ_THIN_CHARS": FAQ_THIN_CHARS,
    }


def write_manifest(out_dir: Path, n_docs: int, n_chunks: int) -> None:
    """Record the sizing constants that produced this build.

    Without it, a build/ generated under different chunk settings fails the
    corpus tests as a 122-item list diff that names the symptom and not the
    cause. With it, the tests can say "build/ predates the current sizing --
    re-run md_ingest", and the loader has a record of what it is loading.
    """
    manifest = {
        "sizing": current_sizing(),
        "documents": n_docs,
        "chunks": n_chunks,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")


def report(docs: list[Document]) -> None:
    from collections import Counter

    chunks = [c for d in docs for c in d.chunks]
    by_kind = Counter(d.kind for d in docs)
    by_brand = Counter(d.brand_slug for d in docs)
    split = [d for d in docs if len(d.chunks) > 1]
    claimy = sum(1 for c in chunks if c.claim_like)

    print(f"\ndocuments            {len(docs)}")
    for kind, n in sorted(by_kind.items()):
        print(f"  {kind:<18} {n}")
    for brand, n in sorted(by_brand.items()):
        print(f"  [{brand}] {n}")

    print(f"\nchunks               {len(chunks)}")
    print(f"  from split docs    {sum(len(d.chunks) for d in split)} "
          f"across {len(split)} documents")
    print(f"  thin documents     {sum(1 for d in docs if d.thin)}")
    print(f"  claim_like chunks  {claimy} "
          f"({100 * claimy / max(len(chunks), 1):.0f}%)")

    sizes = sorted(c.char_len for c in chunks)
    if sizes:
        print(f"\nchunk chars          min={sizes[0]}  "
              f"median={sizes[len(sizes) // 2]}  max={sizes[-1]}")

        # The ceiling applies only to chunks produced by splitting. A whole
        # document below SPLIT_THRESHOLD is legitimately up to 7,999 chars in
        # one chunk -- checking those against MAX_CHARS flags 66 correct rows.
        from_split = [c for d in docs if len(d.chunks) > 1 for c in d.chunks]
        over = [c for c in from_split if c.char_len > MAX_CHARS + OVERLAP_CHARS]
        print(f"  whole-doc chunks   {len(chunks) - len(from_split)} "
              f"(uncapped by design, max {SPLIT_THRESHOLD - 1})")
        print(f"  split chunks over ceiling  {len(over)}"
              + ("  <-- investigate" if over else ""))
        for chunk in sorted(over, key=lambda c: -c.char_len)[:5]:
            print(f"    {chunk.char_len:>6}  {chunk.heading_path[:60]!r}")

    print("\nlargest documents by chunk count")
    for d in sorted(docs, key=lambda d: -len(d.chunks))[:8]:
        print(f"  {len(d.chunks):>3} chunks  {d.char_len:>7,} chars  {d.path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb-root", default=None, help="defaults to ./KB")
    parser.add_argument("--out", default=None, help="defaults to ./build")
    parser.add_argument("--brand", choices=sorted(set(BRAND_BY_DIR.values())))
    parser.add_argument("--report-only", action="store_true",
                        help="measure without writing JSONL")
    args = parser.parse_args()

    project = Path(__file__).resolve().parent.parent
    root = Path(args.kb_root) if args.kb_root else project / "KB"
    out_dir = Path(args.out) if args.out else project / "build"

    if not root.is_dir():
        print(f"KB root not found: {root}")
        return 1

    docs = collect(root, args.brand)
    if not docs:
        print("No documents produced.")
        return 1

    report(docs)
    if args.report_only:
        print("\n(report only -- nothing written)")
    else:
        n_docs, n_chunks = write_jsonl(docs, out_dir)
        print(f"\nwrote {n_docs} documents, {n_chunks} chunks -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
