"""Set settings.near_duplicate_threshold from measurement, not guesswork.

Read-only. Makes one embedding API call per probe query (via the hybrid search
path) and writes nothing.

    python scripts/calibrate_dedupe.py
    python scripts/calibrate_dedupe.py --brand renegade -v

THE PROBLEM THIS SOLVES
-----------------------
Renegade's site templates one FAQ per location: "How will Renegade help me with
Orlando-specific insurance...", the same for Panama City, Porter, Palm Bay and
nine others. Every one is its own document, so max_chunks_per_document -- which
caps chunks per document -- sees a single chunk each and lets all thirteen
through. They also cluster at nearly identical vector distances, so they occupy
the entire top-k and spend the token budget on one restated answer.

Measured before the fix: a query for "what makes us different from captive
agencies" returned 21 passages, 13 of them that one family, with 19 further
passages dropped over budget behind them.

WHAT THIS MEASURES
------------------
Pairwise Jaccard overlap of word sets among the top-k candidates, split into
the two populations that matter:

  NEAR-DUPLICATE   pairs that restate each other. Should score high.
  DISTINCT         pairs that genuinely differ. Should score low.

A usable threshold sits in the gap. Unlike the vector distance threshold, these
populations separate cleanly here, so the answer is not a compromise.

Note the DUPLICATE_QUERIES / CONTROL_QUERIES split: a threshold that suppresses
duplicates is only half the result. The control queries must lose nothing, or
the deduper is silently costing recall on ordinary queries.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402  -- importing installs the Windows selector loop policy
from config import settings  # noqa: E402
from kb_context import SearchOptions, _jaccard, _shingle, search  # noqa: E402

#: Probe queries. Deliberately NOT split into "has duplicates" and "clean":
#: the first version of this script did that and reported a false warning,
#: because "back office support for my agency" surfaces two genuinely
#: near-identical application pages at 0.9135. Labelling a query as a control
#: and then treating its highest overlap as the distinct-population ceiling
#: measures the wrong thing -- a real duplicate can appear in any query.
#:
#: So all pairs from all queries are pooled and the separation is found
#: empirically, by looking for the widest gap in the sorted scores. That needs
#: no labels and cannot be contaminated by one mislabelled query.
PROBE_QUERIES = [
    # Hits the templated per-location FAQ family.
    "what makes us different from captive agencies",
    "help me with local insurance in my area",
    # Ordinary queries. Any duplicates here are real ones.
    "how do franchise owners get paid and what does it cost to start",
    "back office support for my agency",
    "franchise fee",
    "selling my insurance agency",
]

#: Pairs below this are uninteresting -- ordinary topical overlap. The gap
#: search runs above it so it is not distracted by the dense low-score mass.
GAP_SEARCH_FLOOR = 0.30

THRESHOLDS = (0.60, 0.65, 0.70, 0.72, 0.75, 0.80, 0.85, 0.90)


def pairs_for(rows: list[dict], min_chars: int) -> list[tuple[float, str, str]]:
    sh = [(r["title"], _shingle(r["chunk_text"]), len(r["chunk_text"]))
          for r in rows]
    out = []
    for i in range(len(sh)):
        for j in range(i + 1, len(sh)):
            if sh[i][2] < min_chars or sh[j][2] < min_chars:
                continue
            out.append((_jaccard(sh[i][1], sh[j][1]), sh[i][0], sh[j][0]))
    out.sort(reverse=True)
    return out


def greedy_drop(rows: list[dict], threshold: float,
                min_chars: int) -> list[str]:
    """Replays _Deduper's logic: rank order, keep-first, drop restatements."""
    kept: list[frozenset[str]] = []
    dropped: list[str] = []
    for r in rows:
        text = r["chunk_text"]
        s = _shingle(text)
        if len(text) >= min_chars and any(
                _jaccard(s, k) >= threshold for k in kept):
            dropped.append(r["title"])
            continue
        kept.append(s)
    return dropped


async def run(brand: str, limit: int, verbose: bool) -> int:
    min_chars = settings.near_duplicate_min_chars
    await db.pool.open()
    try:
        pooled: list[tuple[float, str, str]] = []

        for q in PROBE_QUERIES:
            rows = await search(SearchOptions(brand=brand, query=q,
                                              limit=limit))
            print(f"\n{q!r} -> {len(rows)} candidates")
            if len(rows) < 2:
                print("  too few candidates to compare")
                continue
            pr = pairs_for(rows, min_chars)
            if not pr:
                print(f"  no pairs above the {min_chars}-char floor")
                continue
            pooled.extend(pr)
            vals = [p[0] for p in pr]
            print(f"  {len(pr)} comparable pairs   max={max(vals):.4f} "
                  f"median={statistics.median(vals):.4f} min={min(vals):.4f}")
            if verbose:
                for s, t1, t2 in pr[:5]:
                    print(f"      {s:.4f}  {t1[:34]:<34} || {t2[:34]}")
            dropped = greedy_drop(rows, settings.near_duplicate_threshold,
                                  min_chars)
            print(f"  at configured {settings.near_duplicate_threshold}: "
                  f"{len(rows) - len(dropped)} kept, {len(dropped)} dropped")
            for d in dropped[:6]:
                print(f"      dropped: {d[:62]}")

        print(f"\n{'=' * 74}\nSEPARATION  (all queries pooled)\n{'=' * 74}")
        if not pooled:
            print("  no comparable pairs at all")
            return 1

        vals = sorted((p[0] for p in pooled), reverse=True)
        print(f"  {len(vals)} pairs   max={vals[0]:.4f} "
              f"median={statistics.median(vals):.4f} min={vals[-1]:.4f}")
        for thr in THRESHOLDS:
            print(f"    >= {thr:.2f}: {sum(1 for v in vals if v >= thr):>5} pair(s)")

        # Widest gap between consecutive scores above the floor. With two
        # populations present this lands between them; with one it lands
        # somewhere arbitrary, which the width check below catches.
        upper = [v for v in vals if v >= GAP_SEARCH_FLOOR]
        if len(upper) < 2:
            print(f"\n  fewer than two pairs above {GAP_SEARCH_FLOOR} -- no "
                  f"duplicate population in this sample. The threshold cannot "
                  f"be calibrated from these queries; add probes that hit "
                  f"templated content.")
            return 1

        gaps = [(upper[i] - upper[i + 1], upper[i + 1], upper[i])
                for i in range(len(upper) - 1)]
        width, low, high = max(gaps)
        rec = round((low + high) / 2, 2)

        print(f"\n  widest gap above {GAP_SEARCH_FLOOR}: "
              f"{low:.4f} -> {high:.4f}  (width {width:.4f})")
        print(f"    duplicate population:  >= {high:.4f}  "
              f"({sum(1 for v in vals if v >= high)} pairs)")
        print(f"    distinct population:   <= {low:.4f}  "
              f"({sum(1 for v in vals if v <= low)} pairs)")
        print(f"  recommended near_duplicate_threshold = {rec}")
        print(f"  configured: {settings.near_duplicate_threshold}")

        if width < 0.10:
            print("\n  WARNING: the widest gap is under 0.10. The two "
                  "populations are not clearly separated -- inspect with -v "
                  "before trusting any threshold here.")
            return 1
        if not (low < settings.near_duplicate_threshold < high):
            print("\n  WARNING: the configured value is OUTSIDE the gap "
                  f"({low:.4f}, {high:.4f}). It is either failing to suppress "
                  "duplicates or eating distinct passages.")
            return 1
        print("\n  OK: the configured threshold sits inside the gap.")
    finally:
        await db.pool.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--brand", default="renegade")
    ap.add_argument("--limit", type=int, default=40,
                    help="candidates per probe; match build_context's overfetch")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    return asyncio.run(run(args.brand, args.limit, args.verbose))


if __name__ == "__main__":
    raise SystemExit(main())
