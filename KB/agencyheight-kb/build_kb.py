#!/usr/bin/env python
"""Build the Agency Height knowledgebase from browser-captured HTML.

Scope: page-sitemap.xml only. Blog posts (post-sitemap.xml, 738 URLs) and the
/insurance-reviews/ cluster (204 URLs) are excluded by explicit instruction.
"""
import json, re, pathlib, collections, sys, hashlib

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from extract import run

BASE = pathlib.Path(__file__).parent
HTML = BASE / "raw" / "html"
KB = BASE / "kb"
SITE = "https://agencyheight.com/"

# first path segment -> category
SEGMAP = {
    "get-insurance": "consumer-insurance-guides",
    "local-insurance": "local-insurance",
    "insurance-agents-near-me": "local-insurance",
    "best-insurance-near-me-atlanta": "local-insurance",
    "insurance-careers": "careers",
    "insurance-license": "licensing",
    "agency-management-system": "agent-tools",
    "customer-relationship-management": "agent-tools",
    "insurance-marketing": "agent-tools",
    "agency-starter-kit": "agent-tools",
    "insurance-back-office-outsourcing": "agent-tools",
    "best-insurance-quoting-tools": "agent-tools",
    "best-voip-phone-services": "agent-tools",
    "insta-quote": "agent-tools",
    "calculator": "agent-tools",
    "legal-services": "legal-services",
    "insurance-news": "resources",
    "insurance-forums": "resources",
    "insurance-expert-opinion": "resources",
    "insurance-referrals-guide": "resources",
}
PRODUCT_HINTS = ("insurance",)  # any other *-insurance* segment -> product-lines

# exact top-level slugs that would otherwise fall through to the catch-all
SLUGMAP = {
    # the platform's own paid products / core offering
    "markets": "core-products",
    "lead-bank": "core-products",
    "lead-generator-enricher": "core-products",
    "agent-directory": "core-products",
    "insta-quote": "core-products",
    "insurance-agency-websites": "core-products",
    "insurance-intake-form": "core-products",
    # legal
    "terms-and-conditions": "site-policies",
    "privacy-policy": "site-policies",
    # editorial comparisons / resources
    "top-peo-services": "resources",
    "siaa-vs-smart-choice": "resources",
    # calculators are agent tooling
    "truck-finance-calculator": "agent-tools",
    "trucking-insurance-calculator": "agent-tools",
    "home-insurance-calculator": "agent-tools",
}


def cat(slug):
    seg = slug.split("__")[0]
    if seg in SLUGMAP:
        return SLUGMAP[seg]
    if slug == "home":
        return "company"
    if seg in SEGMAP:
        return SEGMAP[seg]
    if any(h in seg for h in PRODUCT_HINTS):
        return "product-lines"
    return "company"


def valid(p):
    try:
        b = p.read_bytes()
    except Exception:
        return False
    if len(b) < 3000:
        return False
    low = b[:4000].lower()
    return not any(k in low for k in (b"sgcaptcha", b"robot challenge screen", b"403 - forbidden"))


def norm(l):
    return re.sub(r"\s+", " ", re.sub(r"^[#\-\s]+", "", l)).strip().lower()


def url_for(slug):
    return SITE + ("" if slug == "home" else slug.replace("__", "/") + "/")


def main():
    files = sorted(p for p in HTML.glob("*.html") if valid(p))
    print("valid pages: {} / {}".format(len(files), len(list(HTML.glob("*.html")))))

    pages = {}
    for p in files:
        meta, text, ld = run(p)
        pages[p.stem] = {"meta": meta, "text": text, "ld": ld}

    df = collections.Counter()
    for d in pages.values():
        for l in set(norm(x) for x in d["text"].split("\n") if x.strip()):
            df[l] += 1
    n = len(pages)
    thresh = max(3, int(n * 0.60))
    chrome = set(l for l, c in df.items() if c >= thresh and 0 < len(l) < 130)

    def clean(text):
        out = []
        for l in text.split("\n"):
            nl = norm(l)
            if nl and nl in chrome:
                continue
            if re.match(r"^\[(image|field): ", l.strip()):
                continue
            out.append(l.rstrip())
        return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()

    for s in pages:
        pages[s]["clean"] = clean(pages[s]["text"])

    byhash, aliases = {}, collections.defaultdict(list)
    for slug in sorted(pages, key=lambda s: (len(s), s)):
        h = hashlib.md5(pages[slug]["clean"].encode("utf-8")).hexdigest()
        if h in byhash:
            aliases[byhash[h]].append(slug)
        else:
            byhash[h] = slug
    dupes = set(s for lst in aliases.values() for s in lst)
    print("boilerplate lines removed: {}; duplicate stubs folded: {}".format(len(chrome), len(dupes)))

    KB.mkdir(exist_ok=True)
    (KB / "_structured-data").mkdir(exist_ok=True)
    manifest, faqs, orgs = [], [], {}

    for slug, d in sorted(pages.items()):
        if slug in dupes:
            continue
        c = cat(slug)
        (KB / c).mkdir(exist_ok=True)
        m = d["meta"]
        title = re.sub(r"\s+", " ", m.get("title", "")).strip()
        al = aliases.get(slug, [])
        src = m.get("canonical") or url_for(slug)
        fm = ["---", 'title: "{}"'.format(title.replace('"', "'")),
              "source_url: {}".format(src), "category: {}".format(c),
              'meta_description: "{}"'.format(m.get("description", "").replace('"', "'"))]
        if al:
            fm.append("aliases:  # byte-identical content")
            fm.extend("  - {}".format(url_for(a)) for a in al)
        fm.append("---")
        (KB / c / (slug + ".md")).write_text(
            "\n".join(fm) + "\n\n" + d["clean"] + "\n", encoding="utf-8")
        manifest.append({"slug": slug, "category": c, "title": title,
                         "url": src, "chars": len(d["clean"]), "aliases": al})

        if d["ld"]:
            (KB / "_structured-data" / (slug + ".json")).write_text(
                json.dumps(d["ld"], indent=1, ensure_ascii=False), encoding="utf-8")
        for blk in d["ld"]:
            stack = [blk]
            while stack:
                nd = stack.pop()
                if isinstance(nd, list):
                    stack.extend(nd)
                    continue
                if not isinstance(nd, dict):
                    continue
                if "@graph" in nd:
                    stack.extend(nd["@graph"])
                ty = nd.get("@type")
                tys = ty if isinstance(ty, list) else [ty]
                if "FAQPage" in tys:
                    for q in nd.get("mainEntity", []) or []:
                        if isinstance(q, dict):
                            ans = (q.get("acceptedAnswer") or {}).get("text", "")
                            faqs.append((slug, q.get("name", "").strip(), ans.strip()))
                if set(["Organization", "Corporation", "LocalBusiness"]) & set(tys):
                    orgs.setdefault(nd.get("@id") or nd.get("name"), nd)

    (KB / "_manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")

    seen, out = set(), ["# Agency Height - FAQs\n",
                        "_Aggregated from FAQPage structured data across the site._\n"]
    for slug, q, a in faqs:
        k = re.sub(r"\s+", " ", q.lower()).strip()
        if not q or k in seen:
            continue
        seen.add(k)
        out.append("### {}\n\n{}\n\n`source: {}`\n".format(q, a, url_for(slug)))
    (KB / "faqs.md").write_text("\n".join(out), encoding="utf-8")

    (KB / "_structured-data" / "organizations.json").write_text(
        json.dumps(orgs, indent=1, ensure_ascii=False), encoding="utf-8")

    cats = sorted(set(m["category"] for m in manifest))
    R = ["# Agency Height Knowledgebase\n",
         "Scraped from <https://agencyheight.com/>.\n",
         "**Scope:** `page-sitemap.xml` only. Excluded by instruction: the **738 blog posts** "
         "(`post-sitemap.xml`) and the **204-page `/insurance-reviews/` cluster**.\n",
         "- **{}** pages across {} categories".format(len(manifest), len(cats)),
         "- **{}** unique FAQs -> [faqs.md](faqs.md)".format(len(seen)),
         "- **{}** duplicate stubs folded into canonical pages".format(len(dupes)),
         "- Raw JSON-LD per page in [_structured-data/](_structured-data/)\n"]
    for c in cats:
        rows = sorted([m for m in manifest if m["category"] == c], key=lambda r: -r["chars"])
        sub = [m for m in rows if m["chars"] >= 900]
        thin = [m for m in rows if m["chars"] < 900]
        R.append("\n## {}  ({} pages)\n".format(c, len(rows)))
        for m in sub:
            extra = "  _(+{} alias)_".format(len(m["aliases"])) if m["aliases"] else ""
            R.append("- [{}]({}/{}.md) - {:,} chars{}".format(
                m["title"] or m["slug"], c, m["slug"], m["chars"], extra))
        if thin:
            R.append("\n<details><summary>{} thin pages</summary>\n".format(len(thin)))
            for m in thin:
                R.append("- [{}]({}/{}.md) - {:,} chars".format(
                    m["title"] or m["slug"], c, m["slug"], m["chars"]))
            R.append("\n</details>")

    empties = sorted([m for m in manifest if m["chars"] < 200], key=lambda r: r["chars"])
    R += ["\n## Caveats & findings\n",
          "- **Scope was trimmed deliberately.** 738 blog posts and the 204-page "
          "`/insurance-reviews/` cluster were excluded on instruction. The site's "
          "`page-sitemap.xml` lists 397 URLs; 193 of those are in scope here.",
          "- **9 `/local-insurance/` URLs redirect off-site** to `agents.agencyheight.com` "
          "state directory pages, collapsing to just **5 distinct state pages** (TX, GA, NY, FL, MI). "
          "City- and line-level URLs (e.g. `michigan/redford`, `georgia/car-insurance`) all land on "
          "the plain state page. That app is **client-side rendered**, so agent listings are not in "
          "the static HTML and were not captured. Mapping: "
          "[raw/agent-directory-redirects.json](../raw/agent-directory-redirects.json).",
          "- **`/sitemap/` is an HTML link index** (~93k chars of link text, the largest 'page' here). "
          "Useful as a URL inventory, not as prose.",
          "- **`/home-old/` is still live and indexed** - a stale duplicate of the homepage "
          "(\"Find an Insurance Agent Near Me\"). Worth redirecting or removing.",
          "- Marketing claims are reproduced, not verified.",
          ]
    if empties:
        R.append("- **{} effectively empty pages** are live and in the sitemap: {}.".format(
            len(empties),
            ", ".join("`/{}/` ({} chars)".format(m["slug"].replace("__", "/"), m["chars"])
                      for m in empties)))
    R += ["\n## Provenance\n",
          "`agencyheight.com` sits behind the same SiteGround JS challenge as `renegadeinsurance.com`, "
          "which blocks `curl` and datacenter fetchers. Pages were captured by same-origin `fetch()` "
          "from inside a real browser session. Raw HTML is kept in `raw/html/` so the markdown can be "
          "regenerated with `python build_kb.py` without re-scraping.",
          ]
    (KB / "README.md").write_text("\n".join(R) + "\n", encoding="utf-8")
    print("FAQs {} | pages {} | orgs {}".format(len(seen), len(manifest), len(orgs)))


if __name__ == "__main__":
    main()
