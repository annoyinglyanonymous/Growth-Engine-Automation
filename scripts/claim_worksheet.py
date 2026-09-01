"""Emit a candidate-claims review checklist from the corpus.

    python scripts/claim_worksheet.py --brand renegade
    python scripts/claim_worksheet.py --brand renegade --out KB/worksheet.md

READ-ONLY. Touches no API, writes nothing to the database. Re-run it whenever
the corpus is reloaded to see what new assertions appeared on the site.

WHY THIS EXISTS
---------------
public.claims is the only thing a generator may assert as fact. Populating it
from a blank page means someone reading every chunk by hand. This groups every
figure-bearing and superlative sentence by the assertion it makes, dedups across
the per-location template pages, and marks which are already decided -- so
review is a yes/no pass over ~20 rows instead.

It deliberately does NOT recommend wording. Choosing what the company is willing
to assert, and with which qualifiers, is the human half of the job.

WHY NOT normalize.looks_like_claim
----------------------------------
That predicate is for chunk LABELLING and matches money, percentages, "N+",
"N/5" and "Nk". It cannot match a bare count, so "48 states", "9 locations",
"60 to 180 days" and "two-week training" -- several of the highest-value
franchise claims -- are invisible to it. It also cannot see an unsubstantiated
superlative, which is a claim carrying no figure at all ("industry-leading
commissions").

Widening looks_like_claim itself would change the disputed labelling of live
chunks and force a full reload, for no gain to retrieval. So the patterns below
are a superset local to this script and to review.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from config import settings  # noqa: E402

#: Superset of normalize._CLAIM_RES. Order matters only for display.
PATTERNS: dict[str, re.Pattern] = {
    "money": re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?"),
    "percent": re.compile(r"\b\d+(?:\.\d+)?\s?%"),
    "plus_count": re.compile(r"\b\d[\d,]*\s?\+"),
    "rating": re.compile(r"\b\d(?:\.\d+)?\s?(?:/|out of)\s?5\b"),
    # The gap looks_like_claim leaves: a counted noun with no $ or %.
    "counted": re.compile(
        r"\b(?:\d[\d,]*|two|three|four|five|six|seven|eight|nine|ten)"
        r"[- ](?:to[- ]\d+[- ])?"
        r"(?:states?|agents?|agencies|locations?|offices?|carriers?|years?|"
        r"partners?|clients?|customers?|policies|markets?|employees?|"
        r"team[- ]members?|professionals?|days?|weeks?|months?|hours?|"
        r"lines?[- ]of[- ]business)\b",
        re.I),
    "always_on": re.compile(r"\b24\s?/\s?7\b|\bround[- ]the[- ]clock\b", re.I),
    # A claim need not carry a figure. An unsubstantiated superlative is the
    # most common single source of unapprovable ad copy.
    "superlative": re.compile(
        r"\b(?:#1|number one|the largest|fastest[- ]growing|industry[- ]?leading|"
        r"best[- ]in[- ]class|only \w+ that|unlimited|guaranteed?|no cost|"
        r"free of charge|leading provider)\b",
        re.I),
    # Third-party validation: a claim about someone else's judgement, which
    # needs the certifying body named and the period stated or it goes stale.
    "third_party": re.compile(
        r"\b(?:certified|award[- ]winner|award[- ]winning|accredited|"
        r"recognition award|great place to work)\b", re.I),
}

#: First-person markers. A figure inside a customer quote is a testimonial, not
#: a company claim -- it needs individual-results treatment, not approval.
#: A heuristic, not a parser: review confirms the call.
_FIRST_PERSON = re.compile(
    r"(?:^|\W)(?:I|I'm|I've|my|we are|we're|our (?:car|home|condo))(?:\W|$)")

#: Staff-bio figures ("23 years of experience"). Real, but not marketing claims
#: -- they belong on the location page and nowhere else.
_BIO = re.compile(r"\b(?:meet|our exceptional|originally from|fluent in)\b", re.I)

_SENT = re.compile(r"(?<=[.!?])\s+|\n+")

CHUNKS_SQL = """
select c.chunk_id, c.doc_title, c.text, d.url, d.kind, d.path
from kb.chunks c
join kb.documents d on d.doc_id = c.doc_id
join public.brands b on b.id = c.brand_id
where b.slug = %(brand)s
  and d.kind <> 'primer'
  -- Legal boilerplate is full of figures and asserts nothing marketable.
  and d.path not ilike '%%privacy%%'
  and d.path not ilike '%%terms-of-use%%'
order by d.kind, c.doc_title, c.ordinal
"""

# Reached through claims.brand_id since 015, not through the product. The old
# inner join to products dropped every brand-level claim, which after 015 is
# where the company facts live -- so a worksheet run would have re-listed
# "licensed in 48 states" as an undecided candidate months after it was
# approved. '(brand)' marks a claim with no product parent.
DECIDED_SQL = """
select c.status, c.claim_text, c.approved_wording, c.category,
       coalesce(p.slug, '(brand)') as product
from public.claims c
join public.brands b on b.id = c.brand_id
left join public.products p on p.id = c.product_id
where b.slug = %(brand)s
"""

CONFLICTS_SQL = """
select k.topic, k.status, k.correct_value, k.stale_values, k.detail, k.severity
from kb.conflicts k
join public.brands b on b.id = k.brand_id
where b.slug = %(brand)s
order by k.topic
"""


def figures(text: str) -> set[str]:
    """Every distinct figure token in the text, for grouping and cross-ref."""
    out: set[str] = set()
    for name in ("money", "percent", "plus_count", "rating", "counted"):
        out.update(m.group(0).strip() for m in PATTERNS[name].finditer(text))
    return out


def classify(sentence: str) -> str:
    """Which review question this sentence poses.

    Ordered by how much the answer differs: a testimonial is never approved as a
    company claim however true it is, so that test comes before the rest.
    """
    if _FIRST_PERSON.search(sentence):
        return "testimonial"
    if _BIO.search(sentence):
        return "staff_bio"
    if PATTERNS["third_party"].search(sentence):
        return "third_party"
    if figures(sentence):
        return "figure"
    return "superlative"


TIER_ORDER = ("figure", "superlative", "third_party", "testimonial", "staff_bio")

TIER_NOTE = {
    "figure": "Company assertions carrying a number. These are what "
              "public.claims exists to govern.",
    "superlative": "Assertions with no figure behind them. Each needs either a "
                   "substantiating figure or a decision not to say it.",
    "third_party": "Someone else's judgement. Needs the certifying body named "
                   "and the period stated, or it goes stale silently.",
    "testimonial": "Individual customer results. Never assertable as a company "
                   "claim; usable only attributed and with a results-vary "
                   "qualifier.",
    "staff_bio": "True of a named person, not of the company. Excluded from "
                 "claims -- listed so review can confirm the call.",
}


def collect(cur, brand: str) -> tuple[dict, list[dict]]:
    cur.execute(CHUNKS_SQL, {"brand": brand})
    rows = cur.fetchall()

    found: dict[str, dict] = {}
    for r in rows:
        for raw in _SENT.split(r["text"]):
            s = re.sub(r"\s+", " ", raw).strip(" -*#|•")
            # Under 15 chars is a fragment; over 400 is an unsplit block whose
            # figure cannot be attributed to a single assertion.
            if not (15 <= len(s) <= 400):
                continue
            kinds = tuple(k for k, p in PATTERNS.items() if p.search(s))
            if not kinds:
                continue
            e = found.setdefault(s, {
                "tier": classify(s), "kinds": kinds,
                "figures": figures(s), "sources": [],
            })
            src = (r["doc_title"], r["url"])
            if src not in e["sources"]:
                e["sources"].append(src)

    return found, rows


def render(brand: str, found: dict, chunks: list[dict],
           decided: list[dict], conflicts: list[dict]) -> str:
    L: list[str] = []
    w = L.append

    w(f"# Candidate claims worksheet -- {brand}")
    w("")
    w("Generated by `scripts/claim_worksheet.py` from the loaded corpus. "
      "Read-only; re-run after any reload.")
    w("")
    w(f"- chunks scanned: **{len(chunks)}** "
      "(excludes primer, privacy policy, terms of use)")
    w(f"- distinct candidate assertions: **{len(found)}**")
    w(f"- already decided in `public.claims`: **{len(decided)}**")
    w("")
    w("Each entry is one assertion. Decide a `status`:")
    w("")
    w("| status | means |")
    w("|---|---|")
    w("| `approved` | may be asserted, using `approved_wording` verbatim |")
    w("| `restricted` | usable only under the stated condition |")
    w("| `prohibited` | never usable, in any channel |")
    w("| `pending_review` | left undecided on purpose |")
    w("| *(omit)* | not a claim; no row |")
    w("")

    if decided:
        w("## Already decided")
        w("")
        w("Listed so the same figure is not re-litigated below.")
        w("")
        for d in sorted(decided,
                        key=lambda r: (r["status"], r["category"] or "")):
            w(f"- **{d['status']}** ({d['category']}/{d['product']}): "
              f"{d['claim_text']}")
        w("")

    decided_figs: set[str] = set()
    for d in decided:
        decided_figs |= figures(d["claim_text"] or "")
        decided_figs |= figures(d["approved_wording"] or "")

    by_tier: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for s, e in found.items():
        by_tier[e["tier"]].append((s, e))

    for tier in TIER_ORDER:
        items = by_tier.get(tier)
        if not items:
            continue
        w(f"## {tier.replace('_', ' ').title()}  ({len(items)})")
        w("")
        w(TIER_NOTE[tier])
        w("")
        # Most-repeated first: a sentence on 8 pages is a load-bearing claim.
        items.sort(key=lambda kv: (-len(kv[1]["sources"]), kv[0]))
        for s, e in items:
            mark = ("  _[figure already governed]_"
                    if e["figures"] & decided_figs else "")
            w(f"### {s}{mark}")
            w("")
            w(f"- appears on **{len(e['sources'])}** page(s); "
              f"figures: {', '.join(sorted(e['figures'])) or '(none)'}")
            for title, url in e["sources"][:4]:
                w(f"- `{title[:64]}` {url or '(curated KB file, no url)'}")
            if len(e["sources"]) > 4:
                w(f"- ... and {len(e['sources']) - 4} more page(s)")
            w("")
            w("  - [ ] status: ______  wording: ______")
            w("")

    w("## Open conflicts in `kb.conflicts`")
    w("")
    w("A claim cannot be approved while its figure is disputed -- resolve the "
      "conflict first, then approve the surviving value and prohibit the "
      "stale one.")
    w("")
    for c in conflicts:
        w(f"- **{c['topic']}** ({c['status']}/{c['severity']}) "
          f"correct={c['correct_value'] or '--'} "
          f"stale={list(c['stale_values'] or [])}")
        w(f"  - {c['detail']}")
    w("")
    return "\n".join(L)


def run(brand: str, out: Path | None) -> int:
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("select 1 from public.brands where slug = %s", (brand,))
            if not cur.fetchone():
                raise SystemExit(f"no brand with slug {brand!r}")
            found, chunks = collect(cur, brand)
            cur.execute(DECIDED_SQL, {"brand": brand})
            decided = cur.fetchall()
            cur.execute(CONFLICTS_SQL, {"brand": brand})
            conflicts = cur.fetchall()

    text = render(brand, found, chunks, decided, conflicts)
    if out:
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}  ({len(found)} candidates from {len(chunks)} chunks)")
    else:
        print(text)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--brand", default="renegade")
    ap.add_argument("--out", type=Path, default=None,
                    help="write markdown here instead of stdout")
    args = ap.parse_args()
    return run(args.brand, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
