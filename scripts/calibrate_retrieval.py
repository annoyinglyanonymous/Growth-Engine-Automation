"""Set kb.search_kb's p_max_distance from measurement, not guesswork.

Run AFTER `python -m Ingestion.embed`. Read-only against the database; makes one
embedding API call per probe.

    python scripts/calibrate_retrieval.py
    python scripts/calibrate_retrieval.py --brand agencyheight

THE PROBLEM THIS SOLVES
-----------------------
A vector arm always returns its top k. Without a distance cap, a query whose
subject is genuinely absent from the corpus still comes back with the five
least-unrelated passages, and the agent writes copy from them. With the cap too
tight, paraphrase recall dies and we are back to lexical-only.

So the threshold has to sit between two measured populations:

  MUST MATCH        paraphrase queries whose answers demonstrably ARE in the
                    corpus. These are the queries lexical search fails on --
                    the whole reason for the vector arm.

  ABSENT / ADJACENT the hard negatives: insurance-domain, plausible-sounding,
                    and genuinely NOT in a scraped marketing corpus. Procedural
                    and operational questions. These are what a loose threshold
                    answers wrongly.

  ABSENT / OFF      sanity check. If these score close, something is broken.

Note on E&O: "errors and omissions" is NOT a valid absent probe for Renegade.
It returns nothing lexically, but Renegade does sell it -- 3 chunks say "E&O",
14 say "professional liability". That is a vocabulary miss the vector arm should
FIX, so it lives under MUST_MATCH.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

# Run as `python scripts/calibrate_retrieval.py` from the project root: sys.path
# starts at scripts/, so config and Ingestion are not importable without this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402
from openai import OpenAI  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from config import settings  # noqa: E402
from Ingestion.embed import DIMENSIONS, MODEL, to_vector_literal  # noqa: E402

PROBES: dict[str, dict[str, list[str]]] = {
    "renegade": {
        "must_match": [
            "what makes us different from captive agencies",
            "how do agents get paid",
            "switching careers into insurance",
            "is it affordable to get started",
            "who handles the paperwork and servicing for me",
            # Vocabulary mismatch: corpus says "E&O" / "professional liability".
            "errors and omissions coverage",
            # Lexical controls -- these already work; they must not regress.
            "back office support",
            "franchise ownership",
        ],
        "absent_adjacent": [
            "how do I handle a lapsed policy renewal",
            "what is the workers compensation claim filing procedure",
            "how do I endorse a mid-term policy change",
            "what are the underwriting guidelines for high-value homes",
        ],
        "absent_offdomain": [
            "how to bake sourdough bread",
            "python asyncio tutorial",
        ],
    },
    "agencyheight": {
        "must_match": [
            "how do I find a good agent near me",
            "why use a broker instead of buying direct",
            "what does business insurance cost",
            "comparing quotes from multiple carriers",
            "agency management software",
        ],
        "absent_adjacent": [
            "how do I handle a lapsed policy renewal",
            "what is the claims adjudication workflow",
            "how do I file a certificate of insurance request",
        ],
        "absent_offdomain": [
            "how to bake sourdough bread",
            "python asyncio tutorial",
        ],
    },
}

NEIGHBOURS_SQL = """
select c.chunk_id,
       d.title,
       d.kind,
       c.heading_path,
       (c.embedding operator(extensions.<=>) %(emb)s::extensions.vector)
           as distance,
       left(regexp_replace(c.text, '\\s+', ' ', 'g'), 80) as preview
from kb.chunks c
join kb.documents d on d.doc_id = c.doc_id
join public.brands b on b.id = c.brand_id
where b.slug = %(brand)s
  and c.embedding is not null
  -- Primers are injected unconditionally by /context, so a primer hit tells us
  -- nothing about retrieval quality. search_kb excludes them by default too.
  and d.kind <> 'primer'
order by distance
limit %(k)s
"""


def embed(client: OpenAI, text: str, model: str) -> str:
    r = client.embeddings.create(model=model, input=text, dimensions=DIMENSIONS)
    return to_vector_literal(r.data[0].embedding)


def probe(conn, client, brand: str, query: str, model: str, k: int) -> list[dict]:
    emb = embed(client, query, model)
    with conn.cursor() as cur:
        cur.execute(NEIGHBOURS_SQL, {"emb": emb, "brand": brand, "k": k})
        return cur.fetchall()


def run(brand: str, model: str, show: int, verbose: bool) -> int:
    if brand not in PROBES:
        raise SystemExit(f"no probe set for {brand!r}; have {list(PROBES)}")
    if not settings.gemini_api:
        raise SystemExit("GEMINI_API is not set in .env")

    client = OpenAI(api_key=settings.gemini_api,
                    base_url=settings.gemini_base_url)

    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select count(*) filter (where embedding is not null) as done, "
                "       count(*) as total from kb.chunks")
            c = cur.fetchone()
        if not c["done"]:
            raise SystemExit(
                "no chunks are embedded yet -- run `python -m Ingestion.embed` "
                "first")
        print(f"corpus: {c['done']}/{c['total']} chunks embedded")
        print(f"brand:  {brand}   model: {model}\n")

        best: dict[str, list[float]] = {}
        for tier, queries in PROBES[brand].items():
            print(f"=== {tier} ===")
            best[tier] = []
            for q in queries:
                rows = probe(conn, client, brand, q, model, show)
                if not rows:
                    print(f"  {q!r}: no embedded chunks for this brand")
                    continue
                d1 = float(rows[0]["distance"])
                best[tier].append(d1)
                print(f"  {d1:.4f}  {q}")
                if verbose:
                    for r in rows:
                        print(f"          {float(r['distance']):.4f} "
                              f"[{r['kind']}] {r['title'][:44]}")
                        print(f"                 {r['preview']}")
            print("")

        # ---- the actual decision ----------------------------------------
        must = best.get("must_match", [])
        adjacent = best.get("absent_adjacent", [])
        offdomain = best.get("absent_offdomain", [])

        print("=== separation ===")
        for label, vals in (("must_match", must),
                            ("absent_adjacent", adjacent),
                            ("absent_offdomain", offdomain)):
            if vals:
                print(f"  {label:<18} best-distance min={min(vals):.4f} "
                      f"median={statistics.median(vals):.4f} "
                      f"max={max(vals):.4f}")

        if not must or not adjacent:
            print("\n  not enough probes to recommend a threshold")
            return 1

        worst_match = max(must)
        closest_absent = min(adjacent)

        print("")
        if worst_match < closest_absent:
            recommended = round((worst_match + closest_absent) / 2, 3)
            print(f"  CLEAN SEPARATION: hardest must-match {worst_match:.4f} "
                  f"< closest absent {closest_absent:.4f}")
            print(f"  recommended p_max_distance = {recommended}")
            print(f"  headroom: {closest_absent - worst_match:.4f}")
            print("\n  Set this as the default in kb.search_kb "
                  "(migration 009) and in main.py's SearchRequest.")
        else:
            print(f"  NO CLEAN SEPARATION: hardest must-match "
                  f"{worst_match:.4f} >= closest absent {closest_absent:.4f}")
            print("  The two populations overlap. Options, in order of "
                  "preference:")
            print("    1. Look at which must_match probe is the outlier "
                  "(re-run with -v) -- it may be a genuinely thin topic and "
                  "not something the threshold should stretch for.")
            print("    2. Set the threshold to exclude the absent set and "
                  "accept losing that one probe; the lexical arm still covers "
                  "queries that share vocabulary.")
            print("    3. Do NOT widen past the absent set. Answering an "
                  "absent query with the least-unrelated passage is the "
                  "failure this parameter exists to prevent.")
            worst_ok = max((d for d in must if d < closest_absent),
                           default=None)
            if worst_ok is not None:
                print(f"\n  conservative choice = "
                      f"{round((worst_ok + closest_absent) / 2, 3)} "
                      f"(keeps {sum(1 for d in must if d < closest_absent)}"
                      f"/{len(must)} must-match probes)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--brand", default="renegade", choices=sorted(PROBES))
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--show", type=int, default=3,
                    help="neighbours to fetch per probe")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print each neighbour, not just the best distance")
    args = ap.parse_args()
    return run(args.brand, args.model, args.show, args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
