
from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from pathlib import Path

import ftfy
import yaml

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

#: Below this many characters a document is a stub (nav/CTA shell, empty page).
#: Flagged, never dropped -- the caller decides.
THIN_CHARS = 500

#: Files that are indexes or link dumps rather than content.
EXCLUDED_BASENAMES = frozenset({
    "README.md",
    "redirects.md",
    "sitemap.md",  # agencyheight: ~93k chars of pure link text
})

#: Any path containing one of these components is not markdown content.
#: ``raw/`` is regeneration source; ``_structured-data/`` is json_ingest's job.
EXCLUDED_PARTS = frozenset({"raw", "_structured-data"})

#: Markdown renderings of data that json_ingest owns. The Renegade README says
#: it plainly -- locations.json is "Same location data as JSON" as _directory.md,
#: and state-licenses.json likewise for legal/state-licenses.md. Ingesting both
#: would put all 48 licences and all 11 locations in the corpus twice, as
#: competing chunks.
#:
#: JSON wins because it yields one document per location, so "what are the
#: Orlando hours" hits that location alone rather than one big directory table.
SUPERSEDED_BY_JSON = frozenset({
    "_directory.md",        # renegade-kb/kb/locations/   <- locations.json
    "state-licenses.md",    # renegade-kb/kb/legal/       <- state-licenses.json
})

#: Bare lines that open the end-of-article widget region (related links,
#: "recent posts" lists). Everything from here to EOF is link text.
#:
#: Verified empirically: of the 111 files containing one of these markers,
#: *zero* have a prose-length line (>=200 chars) after it. Truncating is safe.
#: Known residue: ~3 files end with a recent-posts list under a topic-specific
#: heading ("## Home Insurance Blogs", "## Commercial Auto Insurance"). Those
#: strings are too generic to match on without risking real sections, so the
#: widget is left in place. insurance-blog.md keeps its post list on purpose --
#: there, the list *is* the page.
FOOTER_MARKERS = frozenset({
    "Highlights",
    "Quick Links",
    "## Related Articles",
})

#: Nav headings whose following ``- item`` list is a menu, not content.
#: Removed as a block so we don't have to enumerate every link label.
NAV_HEADS = frozenset({
    "Primary Menu",
    "Customer Login",
    "Log in to Your Customer Portal",
})

#: How many lines a nav block may consume. A guard against a nav head that is
#: legitimately followed by a long content list.
NAV_BLOCK_MAX = 15

#: Widget lines that appear *mid-document*, so they cannot be truncated at --
#: e.g. the quote CTA sits at line 58 of 149 with real content after it.
#: Matched exactly, after stripping, to avoid catching similar prose.
INLINE_NOISE = frozenset({
    # agencyheight -- email capture widget
    "Enter Email Address",
    "Join Us",
    "Your email has been registered. Redirecting...",
    "Sign up now and connect with ready-to-buy leads near you.",
    # agencyheight -- quote comparison CTA
    "##### Compare Quotes from Top Local Agents",
    "## Save more by comparing quotes from multiple insurance agents near you.",
    "Explore, select, and save with agents who understand your unique needs.",
    "Easy, free and secure.",
    "Search Agents",
    "Trusted agents working with top national companies.",
    # renegade -- footer nav leftovers
    "- Share a Review",
    "- Blog",
    "- Blogs",
})

#: Characters that survive HTML extraction but carry no meaning.
#:
#: Every key here is a literal, and all six are invisible on screen -- which is
#: how U+202F went unnoticed until generate.py --show-prompt died on it with a
#: cp1252 UnicodeEncodeError. It was present in 18 chunks. Check the codepoints
#: rather than trusting the glyphs when editing this. Its siblings U+2009
#: (thin space) and U+2060
#: (word joiner) come from the same web-typography family and are absent from
#: the corpus today; add them here if they ever appear rather than widening
#: this pre-emptively, since every addition changes content_hash and forces a
#: re-ingest of the affected documents.
_JUNK_CHARS = {
    " ": " ",  # narrow no-break space
    " ": " ",   # non-breaking space
    "​": "",    # zero-width space
    "‌": "",    # zero-width non-joiner
    "‍": "",    # zero-width joiner
    "﻿": "",    # BOM appearing mid-document
}

# --------------------------------------------------------------------------
# Patterns
# --------------------------------------------------------------------------

#: YAML frontmatter delimited by --- at the very start of the file.
#: Non-greedy so it stops at the *first* closing delimiter.
_FRONTMATTER_RE = re.compile(r"^---[ \t]*\n(.*?)\n---[ \t]*\n?", re.S)

#: Text of an H1, used only as a title fallback. Agency Height pages have no
#: H1 at all, so this misses there and the slug fallback takes over.
_H1_TEXT_RE = re.compile(r"^#[ \t]+(.+?)[ \t]*$", re.M)

#: Claim-like content: money, percentages, "200+", star ratings.
#: Used to flag chunks whose numbers must be labelled unverified downstream.
_CLAIM_RES = (
    re.compile(r"\$\s?[\d,]+(?:\.\d+)?"),          # $20,000
    re.compile(r"\b\d+(?:\.\d+)?\s?%"),            # 40%, 4.5 %
    re.compile(r"\b\d[\d,]*\+"),                   # 200+, 2800+
    re.compile(r"\b\d(?:\.\d+)?\s?/\s?5\b"),       # 4.7/5
    re.compile(r"\b\d[\d,]*\s?(?:k|K)\+?\b"),      # 5k+
)


# --------------------------------------------------------------------------
# Result type
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class NormalizedDoc:
    path: str                 # posix, relative to the KB root -- the natural key
    slug: str
    title: str
    body: str
    char_len: int
    content_hash: str         # sha256 of the *cleaned* body
    thin: bool
    excluded: bool
    source_url: str | None = None
    category: str | None = None
    meta_description: str | None = None
    frontmatter: dict = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


# --------------------------------------------------------------------------
# Text helpers -- pure str -> str, individually testable
# --------------------------------------------------------------------------

def fix_text(s: str) -> str:
    s = ftfy.fix_text(s)
    s = html.unescape(s)
    for bad, good in _JUNK_CHARS.items():
        s = s.replace(bad, good)
    return s


def _fix_deep(value):
    """Apply fix_text to every string inside a nested frontmatter value."""
    if isinstance(value, str):
        return fix_text(value)
    if isinstance(value, list):
        return [_fix_deep(v) for v in value]
    if isinstance(value, dict):
        return {k: _fix_deep(v) for k, v in value.items()}
    return value


def split_frontmatter(raw: str) -> tuple[dict, str, tuple[str, ...]]:
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        return {}, raw, ("no-frontmatter",)

    body = raw[match.end():]
    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        return {}, body, (f"bad-yaml:{type(exc).__name__}",)

    if parsed is None:
        return {}, body, ("empty-frontmatter",)
    if not isinstance(parsed, dict):
        return {}, body, ("frontmatter-not-mapping",)
    return parsed, body, ()


def strip_chrome(body: str) -> tuple[str, tuple[str, ...]]:
    lines = body.split("\n")
    kept: list[str] = []
    notes: list[str] = []
    i = 0

    while i < len(lines):
        stripped = lines[i].strip()

        if stripped in FOOTER_MARKERS:
            notes.append("footer-truncated")
            break

        if stripped in NAV_HEADS:
            i += 1
            consumed = 0
            while i < len(lines) and consumed < NAV_BLOCK_MAX:
                nxt = lines[i].strip()
                if nxt and not nxt.startswith("- "):
                    break
                i += 1
                consumed += 1
            notes.append("nav-block-removed")
            continue

        if stripped in INLINE_NOISE:
            notes.append("inline-widget-removed")
            i += 1
            continue

        kept.append(lines[i])
        i += 1

    # dict.fromkeys keeps first-seen order while de-duplicating
    return "\n".join(kept), tuple(dict.fromkeys(notes))


def collapse_ws(s: str) -> str:
    """Normalise line endings and blank runs.

    Char counts drive the thin flag and the split threshold downstream, so they
    need to reflect content rather than the extractor's block spacing.
    """
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t]+\n", "\n", s)      # trailing whitespace per line
    s = re.sub(r"\n{3,}", "\n\n", s)      # at most one blank line
    return s.strip()


def looks_like_claim(text: str) -> bool:
    """True if the text contains a marketing number needing an unverified label.

    The scraped copy asserts "48 states", "200+ agents", "4.7/5", "$20,000"
    franchise fees -- figures the corpus READMEs call unverified, and which the
    site contradicts in places. Chunks matching this get returned to the agent
    labelled, so it does not restate them as fact.
    """
    return any(pattern.search(text) for pattern in _CLAIM_RES)


def is_excluded(rel_posix: str) -> bool:
    """True for indexes, link dumps, JSON-superseded files, and raw dirs.

    Note for json_ingest: _structured-data is in EXCLUDED_PARTS, which is
    correct for markdown and wrong for JSON-LD. Do not reuse this there.
    """
    parts = rel_posix.split("/")
    if parts[-1] in EXCLUDED_BASENAMES or parts[-1] in SUPERSEDED_BY_JSON:
        return True
    return bool(EXCLUDED_PARTS.intersection(parts))


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------

def normalize_text(raw: str, *, path: str) -> NormalizedDoc:
    """Clean one document. Pure -- no filesystem access.

    ``path`` must already be posix and relative to the KB root: it becomes the
    natural key in ``kb.documents``, so backslashes would make the same file
    hash to a different key on another platform.
    """
    warnings: list[str] = []

    frontmatter, body, fm_warnings = split_frontmatter(raw)
    warnings.extend(fm_warnings)

    frontmatter = _fix_deep(frontmatter)
    body = fix_text(body)

    body, chrome_warnings = strip_chrome(body)
    warnings.extend(chrome_warnings)

    body = collapse_ws(body)

    slug = path.rsplit("/", 1)[-1].removesuffix(".md")
    title = _resolve_title(frontmatter, body, slug, warnings)

    char_len = len(body)
    return NormalizedDoc(
        path=path,
        slug=slug,
        title=title,
        body=body,
        char_len=char_len,
        content_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        thin=char_len < THIN_CHARS,
        excluded=is_excluded(path),
        source_url=_as_str(frontmatter.get("source_url")),
        category=_as_str(frontmatter.get("category")),
        meta_description=_as_str(frontmatter.get("meta_description")),
        frontmatter=frontmatter,
        warnings=tuple(warnings),
    )


def normalize_file(p: Path, *, root: Path) -> NormalizedDoc:
    """Read and clean one file.

    ``encoding='utf-8'`` is not optional: on Windows ``open()`` defaults to
    cp1252, which would decode these files wrongly and *create* fresh mojibake
    on top of what ftfy is here to remove.
    """
    raw = p.read_text(encoding="utf-8")
    rel = p.resolve().relative_to(root.resolve()).as_posix()
    return normalize_text(raw, path=rel)


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------

def _as_str(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _resolve_title(
    frontmatter: dict, body: str, slug: str, warnings: list[str]
) -> str:
    """Frontmatter title, else the H1, else the slug."""
    title = _as_str(frontmatter.get("title"))
    if title:
        return title

    match = _H1_TEXT_RE.search(body)
    if match:
        warnings.append("title-from-h1")
        return match.group(1).strip()

    warnings.append("title-from-slug")
    return slug.replace("__", " / ").replace("-", " ").strip()


# --------------------------------------------------------------------------
# Self-check: python -m Ingestion.normalize
# --------------------------------------------------------------------------

def _report(root: Path) -> int:
    """Run over the corpus and print what changed. No writes, no network.

    The largest before/after drops are the thing to read: if a document lost
    most of its content, chrome-stripping is eating real text -- much cheaper
    to catch here than after 1,800 rows are in Supabase.
    """
    from collections import Counter

    files = sorted(p for p in root.rglob("*.md"))
    if not files:
        print(f"No .md files under {root}")
        return 1

    rows, warn_counts, mojibake = [], Counter(), []

    for p in files:
        before = len(p.read_text(encoding="utf-8"))
        doc = normalize_file(p, root=root)
        for w in doc.warnings:
            warn_counts[w] += 1
        if "â" in doc.body or "" in doc.body:
            mojibake.append(doc.path)
        rows.append((before, doc))

    kept = [d for _, d in rows if not d.excluded]
    print(f"\nfiles                {len(rows)}")
    print(f"  excluded           {sum(1 for _, d in rows if d.excluded)}")
    print(f"  thin (<{THIN_CHARS})        {sum(1 for d in kept if d.thin)}")
    print(f"  claim-like body    {sum(1 for d in kept if looks_like_claim(d.body))}")
    print(f"  total chars kept   {sum(d.char_len for d in kept):,}")

    print("\nwarnings")
    if warn_counts:
        for name, count in warn_counts.most_common():
            print(f"  {count:>5}  {name}")
    else:
        print("  none")

    print(f"\nresidual mojibake    {len(mojibake)} file(s)")
    for path in mojibake[:10]:
        print(f"  {path}")

    print("\nlargest content drops (check these for over-stripping)")
    drops = sorted(
        ((before, d) for before, d in rows if not d.excluded and before),
        key=lambda r: (r[1].char_len - r[0]) / r[0],
    )
    for before, d in drops[:10]:
        pct = 100 * (before - d.char_len) / before
        print(f"  -{pct:5.1f}%  {before:>7,} -> {d.char_len:>7,}  {d.path}")

    return 0


if __name__ == "__main__":
    import sys

    default_root = Path(__file__).resolve().parent.parent / "KB"
    raise SystemExit(_report(Path(sys.argv[1]) if len(sys.argv) > 1 else default_root))
