"""Emit a review worksheet for the claims already STAGED for a brand.

    python scripts/claim_review.py --brand agencyheight
    python scripts/claim_review.py --brand renegade --out docs/renegade.md

READ-ONLY. Writes one markdown file and nothing else; touches no API and
changes no row.

NOT scripts/claim_worksheet.py, which is the step before this one. That mines
the corpus for CANDIDATE assertions -- sentences on the site nobody has ruled
on yet -- and answers "what could we claim?". This reads public.claims and
answers "what have we staged, and what is still undecided?".

WHY A DOCUMENT AND NOT A SCREEN
Approving a claim is an attestation that a statement about the business is
true and defensible. The reviewer needs the exact wording, the source URL and
the reason a claim was restricted side by side, and needs to annotate it and
hand it back. A file in the repo does that and leaves a diff.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import fetch_all, pool  # noqa: E402


def cell(text: str | None) -> str:
    """Markdown table cells cannot contain a raw pipe or a newline."""
    return (text or "").replace("|", r"\|").replace("\n", " ").strip()


async def load(brand: str) -> dict:
    await pool.open()
    try:
        return {
            "product": await fetch_all(
                "select p.slug, p.name, p.approved_for_marketing "
                "from public.products p "
                "join public.brands b on b.id = p.brand_id "
                "where b.slug = %s order by p.slug", (brand,)),
            "claims": await fetch_all(
                "select c.status, c.category, c.claim_text, "
                "       c.approved_wording, c.restriction_notes, c.source, "
                "       c.source_url, c.requires_disclaimer, "
                "       c.disclaimer_text, p.slug as product, "
                "       pf.slug as feature "
                "from public.claims c "
                "join public.brands b on b.id = c.brand_id "
                "left join public.products p on p.id = c.product_id "
                "left join public.product_features pf on pf.id = c.feature_id "
                "where b.slug = %s "
                # pending_review first: those are the open questions.
                "order by (c.status <> 'pending_review'), c.category, "
                "         c.claim_text", (brand,)),
            "rules": await fetch_all(
                "select r.category, r.rule_text, r.severity, r.status, "
                "       r.applies_to_channels "
                "from public.brand_rules r "
                "join public.brands b on b.id = r.brand_id "
                "where b.slug = %s "
                "order by (r.severity <> 'blocker'), r.category", (brand,)),
            "features": await fetch_all(
                "select pf.slug, pf.name, pf.available, "
                "       pf.approved_for_marketing, pf.description "
                "from public.product_features pf "
                "join public.products p on p.id = pf.product_id "
                "join public.brands b on b.id = p.brand_id "
                "where b.slug = %s "
                "order by (not pf.approved_for_marketing), pf.slug",
                (brand,)),
        }
    finally:
        await pool.close()


def render(brand: str, data: dict) -> str:
    claims, rules = data["claims"], data["rules"]
    features, products = data["features"], data["product"]

    by_status: dict[str, list[dict]] = {}
    for c in claims:
        by_status.setdefault(c["status"], []).append(c)

    assertable = len(by_status.get("approved", []))
    gated = [p for p in products if not p["approved_for_marketing"]]

    L: list[str] = []
    w = L.append

    w(f"# {brand} — claim review worksheet")
    w("")
    w("Generated from the live database by "
      "`scripts/claim_review.py`. Re-run it after any change.")
    w("")
    w(f"{len(claims)} claims · {len(rules)} brand rules · "
      f"{len(features)} product features · {len(products)} product(s)")
    w("")

    w("## What you are deciding")
    w("")
    w("Two gates stand between a staged claim and a line of copy, and they "
      "are **independent**:")
    w("")
    for p in products:
        flag = str(p["approved_for_marketing"]).lower()
        w(f"1. `products.{p['slug']}.approved_for_marketing` is **{flag}** — "
          f"the outer gate. While this is false, no product-scoped claim "
          f"reaches a prompt no matter what its own status says.")
    w("2. Each claim's own `status` — the inner gate. Only `approved` is "
      "quotable as fact.")
    w("")
    w("So there are two questions, not one: *is this brand ready to market "
      "at all*, and *is each individual statement true and defensible*. "
      "Answer the second first; the first is one line of SQL once you have.")
    w("")
    if gated and assertable:
        w(f"Worth knowing before you read further: **{assertable} of these "
          f"claims are already `approved`** and still produce nothing, "
          f"because "
          + ", ".join(f"`{p['slug']}`" for p in gated)
          + " is not approved for marketing. That is the outer gate doing "
            "its job, and it means enabling the product is a bigger step "
            "than it looks — those claims become assertable the moment you "
            "flip it.")
        w("")

    pending = by_status.get("pending_review", [])
    if pending:
        w("---")
        w("")
        w("## 1. Undecided")
        w("")
        for c in pending:
            scope = c["product"] or "**brand-level — no product to gate on**"
            w(f"> **{cell(c['claim_text'])}**")
            w(">")
            w(f"> category `{c['category']}` · scope {scope} · "
              f"source {c['source_url'] or c['source'] or 'n/a'}")
            w("")
        if any(not c["product"] for c in pending):
            w("One of these is brand-level, which matters structurally rather "
              "than editorially. A brand-level claim has `product_id IS "
              "NULL`, so there is no product to gate it on and `fetch_claims` "
              "passes it on brand alone. Approving it **bypasses the product "
              "gate entirely** — the outer gate above stops applying to it. "
              "That is why it was staged as `pending_review` rather than "
              "`approved`. Approve it only if you are content for it to be "
              "assertable immediately.")
            w("")

    prohibited = by_status.get("prohibited", [])
    if prohibited:
        w("---")
        w("")
        w("## 2. Prohibited — nothing to approve, confirm they are right")
        w("")
        w("These make the wording unusable wherever it appears, and they are "
          "the load-bearing half: a prohibited claim is what stops a stale or "
          "indefensible figure reaching a customer. Read them as a list of "
          "mistakes you are choosing to prevent.")
        w("")
        w("| # | category | wording that is blocked | why | source |")
        w("|---|---|---|---|---|")
        for i, c in enumerate(prohibited, 1):
            w(f"| {i} | `{c['category']}` | {cell(c['claim_text'])} "
              f"| {cell(c['restriction_notes']) or '—'} "
              f"| {cell(c['source_url']) or '—'} |")
        w("")

    for status, title, note in [
        ("restricted", "3. Restricted — usable, with conditions",
         "Allowed only in the exact approved wording, or with the disclaimer "
         "attached. What to check: is the approved wording one you would put "
         "your name to?"),
        ("approved", "4. Approved",
         "Assertable as fact, subject to the product gate above."),
    ]:
        rows = by_status.get(status, [])
        if not rows:
            continue
        w("---")
        w("")
        w(f"## {title}")
        w("")
        w(note)
        w("")
        w("| # | category | claim | approved wording | disclaimer | source |")
        w("|---|---|---|---|---|---|")
        for i, c in enumerate(rows, 1):
            disc = ("**required** — " + cell(c["disclaimer_text"])
                    if c["requires_disclaimer"] else "—")
            w(f"| {i} | `{c['category']}` | {cell(c['claim_text'])} "
              f"| {cell(c['approved_wording']) or '—'} | {disc} "
              f"| {cell(c['source_url']) or '—'} |")
        w("")

    known = {"pending_review", "prohibited", "restricted", "approved"}
    for status, rows in sorted(by_status.items()):
        if status in known:
            continue
        w(f"## Claims with status `{status}`")
        w("")
        w("| # | category | claim | source |")
        w("|---|---|---|---|")
        for i, c in enumerate(rows, 1):
            w(f"| {i} | `{c['category']}` | {cell(c['claim_text'])} "
              f"| {cell(c['source_url']) or '—'} |")
        w("")

    w("---")
    w("")
    w("## 5. Brand rules")
    w("")
    w("Rules constrain wording regardless of what any claim says. A "
      "`blocker` fails validation outright; a `restriction` warns.")
    w("")
    w("| severity | category | rule | channels |")
    w("|---|---|---|---|")
    for r in rules:
        chans = ", ".join(r["applies_to_channels"] or []) or "all"
        w(f"| `{r['severity']}` | `{r['category']}` "
          f"| {cell(r['rule_text'])} | {chans} |")
    w("")

    w("## 6. Product features")
    w("")
    w("`available` and `approved_for_marketing` are separate on purpose: a "
      "feature can exist and still not be ready to promote. Only the second "
      "lets a generator mention it.")
    w("")
    w("| feature | available | promotable | notes |")
    w("|---|---|---|---|")
    for f in features:
        w(f"| `{f['slug']}` ({cell(f['name'])}) "
          f"| {'yes' if f['available'] else 'no'} "
          f"| {'**yes**' if f['approved_for_marketing'] else 'no'} "
          f"| {cell(f['description'])[:90] or '—'} |")
    w("")

    w("---")
    w("")
    w("## Applying your decisions")
    w("")
    w("Mark this file up however suits you. Hand it back and it becomes a "
      "migration, so the decisions are recorded and reversible rather than "
      "typed into a SQL console.")
    w("")
    if gated:
        w("```sql")
        w("-- The outer gate. Reversible, and an attestation that a human")
        w("-- reviewed the claims above.")
        for p in gated:
            w("update public.products set approved_for_marketing = true")
            w(f" where slug = '{p['slug']}';")
        w("")
        w("-- Per claim, once each is decided:")
        w("update public.claims set status = 'approved',")
        w("       approved_wording = '<the exact wording you would sign>',")
        w("       last_reviewed_at = now(), owner = '<you>'")
        w(" where claim_text = '<claim text from the tables above>';")
        w("```")
        w("")
    return "\n".join(L) + "\n"


async def amain() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--brand", required=True)
    ap.add_argument("--out", default=None,
                    help="default: docs/<brand>-claim-review.md")
    args = ap.parse_args()

    data = await load(args.brand)
    if not data["claims"] and not data["rules"]:
        print(f"no claims or rules staged for brand {args.brand!r}")
        return 1

    out = args.out or os.path.join("docs",
                                   f"{args.brand}-claim-review.md")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render(args.brand, data))

    counts: dict[str, int] = {}
    for c in data["claims"]:
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    print(f"wrote {out}")
    for status, n in sorted(counts.items()):
        print(f"  {status:16} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
