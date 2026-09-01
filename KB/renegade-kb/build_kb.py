#!/usr/bin/env python
"""Build the Renegade Insurance knowledgebase from browser-captured HTML."""
import json, re, pathlib, collections, sys, hashlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from extract import run

BASE = pathlib.Path(__file__).parent
HTML = BASE / "raw" / "html"
KB = BASE / "kb"

PORTAL_NOTE = "Customer portal login (email OTP). Portal support line: 770-723-3933"
FUNNEL_NOTE = "Quote funnel; assigned agent Ajaya Sharma <ajaya.sharma@renegadeinsurance.com>"
REDIRECTS = {
    "support/personal-lines/": ("customer.renegadeinsurance.com/login/email", PORTAL_NOTE),
    "support/commercial-lines/": ("customer.renegadeinsurance.com/login/email", PORTAL_NOTE),
    "get-a-quote-business-insurance/": (
        "customer.renegadeinsurance.com/campaign/649a8b62852f8954ba2275f3",
        "Business insurance. " + FUNNEL_NOTE),
    "get-a-quote-trucking-insurance/": (
        "customer.renegadeinsurance.com/campaign/649a8a3d852f8954ba2275c2",
        "Trucking insurance. " + FUNNEL_NOTE),
}

FRANCHISE_KEYS = ["franchise", "partner", "become-an-agency-owner",
                  "sell-your-insurance-agency", "back-office",
                  "agency-value-calculator", "insurance-agent-application"]
LEGAL_KEYS = ["privacy-policy", "terms-of-use", "license-disclosures"]


def cat(slug):
    if slug.startswith("agency__") or slug == "agency":
        return "locations"
    if any(k in slug for k in FRANCHISE_KEYS):
        return "franchise-and-partners"
    if any(k in slug for k in LEGAL_KEYS):
        return "legal"
    if slug.startswith("get-a-quote"):
        return "products-and-quotes"
    return "company"


def valid(p):
    try:
        b = p.read_bytes()
    except Exception:
        return False
    if len(b) < 4000:
        return False
    low = b[:4000].lower()
    return not any(k in low for k in (b"sgcaptcha", b"robot challenge screen", b"403 - forbidden"))


def norm(l):
    return re.sub(r"\s+", " ", re.sub(r"^[#\-\s]+", "", l)).strip().lower()


def url_for(slug):
    tail = "" if slug == "home" else slug.replace("__", "/") + "/"
    return "https://renegadeinsurance.com/" + tail


RE_PHONE = re.compile(r"\b(\d{3}-\d{3}-\d{4})\b")
RE_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-]+@renegadeinsurance\.com)\b")
RE_ADDR = re.compile(r"\b(\d+[^\n,]{3,60},\s*[A-Za-z .]+\s+[A-Z]{2}\s+\d{5})")
RE_HOURS = re.compile(r"(\d{1,2}(?::\d{2})?\s?[AP]M\s?-\s?\d{1,2}(?::\d{2})?\s?[AP]M)", re.I)
RE_DAYS = re.compile(r"(Monday\s+to\s+\w+)", re.I)
RE_STATUS = re.compile(r"^(Agency Update:.*)$", re.M)


def parse_location(slug, title, text):
    d = {"slug": slug,
         "name": title.replace(" - Renegade Insurance", "").strip(),
         "url": url_for(slug)}
    m = RE_STATUS.search(text)
    d["status_note"] = m.group(1).strip() if m else None
    ph = RE_PHONE.findall(text)
    d["phone"] = ph[0] if ph else None
    em = RE_EMAIL.findall(text)
    d["email"] = em[0] if em else None
    ad = RE_ADDR.findall(text)
    d["address"] = re.sub(r"\s+", " ", ad[0]).strip() if ad else None
    hr = RE_HOURS.findall(text)
    d["hours"] = hr[0] if hr else None
    dy = RE_DAYS.findall(text)
    d["days"] = dy[0] if dy else None
    d["closed"] = bool(d["status_note"] and re.search(r"closed", d["status_note"], re.I))
    return d


def main():
    files = sorted(p for p in HTML.glob("*.html") if valid(p))
    total = len(list(HTML.glob("*.html")))
    print("valid pages: {} / {}".format(len(files), total))

    pages = {}
    for p in files:
        meta, text, ld = run(p)
        pages[p.stem] = {"meta": meta, "text": text, "ld": ld}

    df = collections.Counter()
    for d in pages.values():
        for l in set(norm(x) for x in d["text"].split("\n") if x.strip()):
            df[l] += 1
    n = len(pages)
    thresh = max(3, int(n * 0.70))
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

    for slug in pages:
        pages[slug]["clean"] = clean(pages[slug]["text"])

    byhash = {}
    aliases = collections.defaultdict(list)
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
    manifest, faqs, locations = [], [], []
    orgs = {}

    for slug, d in sorted(pages.items()):
        if slug in dupes:
            continue
        c = cat(slug)
        (KB / c).mkdir(exist_ok=True)
        m = d["meta"]
        body = d["clean"]
        title = re.sub(r"\s+", " ", m.get("title", "")).strip()
        al = aliases.get(slug, [])
        src = m.get("canonical") or url_for(slug)
        desc = m.get("description", "").replace('"', "'")
        fm = ["---", 'title: "{}"'.format(title), "source_url: {}".format(src),
              "category: {}".format(c), 'meta_description: "{}"'.format(desc)]
        if al:
            fm.append("aliases:  # these URLs serve byte-identical content")
            fm.extend("  - {}".format(url_for(a)) for a in al)
        fm.append("---")
        (KB / c / (slug + ".md")).write_text("\n".join(fm) + "\n\n" + body + "\n", encoding="utf-8")
        manifest.append({"slug": slug, "category": c, "title": title,
                         "url": src, "chars": len(body), "aliases": al})

        if d["ld"]:
            (KB / "_structured-data" / (slug + ".json")).write_text(
                json.dumps(d["ld"], indent=1, ensure_ascii=False), encoding="utf-8")
        for blk in d["ld"]:
            if isinstance(blk, dict) and blk.get("@type") == "FAQPage":
                for q in blk.get("mainEntity", []):
                    ans = (q.get("acceptedAnswer") or {}).get("text", "").strip()
                    faqs.append((slug, q.get("name", "").strip(), ans))
            nodes = blk.get("@graph", [blk]) if isinstance(blk, dict) else []
            for nd in nodes:
                if not isinstance(nd, dict):
                    continue
                ty = nd.get("@type")
                tys = ty if isinstance(ty, list) else [ty]
                if set(["Organization", "InsuranceAgency", "LocalBusiness"]) & set(tys):
                    orgs.setdefault(nd.get("@id") or nd.get("name"), nd)
        if re.fullmatch(r"agency__[a-z-]+", slug):
            locations.append(parse_location(slug, title, d["text"]))

    (KB / "_manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")

    seen = set()
    out = ["# Renegade Insurance - FAQs\n",
           "_Aggregated from FAQPage structured data across the site._\n"]
    for slug, q, a in faqs:
        k = q.lower().strip()
        if not q or k in seen:
            continue
        seen.add(k)
        out.append("### {}\n\n{}\n\n`source: {}`\n".format(q, a, url_for(slug)))
    (KB / "faqs.md").write_text("\n".join(out), encoding="utf-8")

    locations.sort(key=lambda d: d["name"])
    L = ["# Agency Location Directory\n",
         "_{} agency location pages. Parsed from page text - these pages carry no "
         "LocalBusiness structured data._\n".format(len(locations)),
         "| Agency | Phone | Email | Address | Hours | Status |",
         "|---|---|---|---|---|---|"]
    for d in locations:
        hours = "{} {}".format(d["hours"], d["days"] or "").strip() if d["hours"] else "-"
        L.append("| [{}]({}) | {} | {} | {} | {} | {} |".format(
            d["name"], d["url"], d["phone"] or "-", d["email"] or "-",
            d["address"] or "-", hours, "**CLOSED**" if d["closed"] else "open"))
    notices = [d for d in locations if d["status_note"]]
    if notices:
        L.append("\n## Notices\n")
        for d in notices:
            L.append("- **{}** - {}".format(d["name"], d["status_note"]))
    (KB / "locations" / "_directory.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    (KB / "locations.json").write_text(
        json.dumps(locations, indent=1, ensure_ascii=False), encoding="utf-8")

    R = ["# Off-site redirects (no public content)\n",
         "These paths 3xx to `customer.renegadeinsurance.com`. They are app / funnel entry",
         "points rather than content pages. The portal paths are login-gated and were",
         "**not** authenticated into.\n",
         "| Path | Redirects to | Notes |", "|---|---|---|"]
    for p, (t, note) in REDIRECTS.items():
        R.append("| `/{}` | `{}` | {} |".format(p, t, note))
    (KB / "redirects.md").write_text("\n".join(R) + "\n", encoding="utf-8")

    (KB / "_structured-data" / "organizations.json").write_text(
        json.dumps(orgs, indent=1, ensure_ascii=False), encoding="utf-8")

    cats = sorted(set(m["category"] for m in manifest))
    idx = ["# Renegade Insurance Knowledgebase\n",
           "Scraped from <https://renegadeinsurance.com/> - **blog excluded** by design.\n",
           "- **{}** content pages across {} categories".format(len(manifest), len(cats)),
           "- **{}** unique FAQs -> [faqs.md](faqs.md)".format(len(seen)),
           "- **{}** agency locations -> [locations/_directory.md](locations/_directory.md)"
           " | [locations.json](locations.json)".format(len(locations)),
           "- **{}** off-site redirects -> [redirects.md](redirects.md)".format(len(REDIRECTS)),
           "- **{}** duplicate template stubs folded into canonical pages\n".format(len(dupes))]
    for c in cats:
        rows = sorted([m for m in manifest if m["category"] == c], key=lambda r: -r["chars"])
        idx.append("\n## {}  ({} pages)\n".format(c, len(rows)))
        for m in rows:
            extra = "  _(+{} alias)_".format(len(m["aliases"])) if m["aliases"] else ""
            idx.append("- [{}]({}/{}.md) - {:,} chars{}".format(
                m["title"] or m["slug"], c, m["slug"], m["chars"], extra))
    (KB / "README.md").write_text("\n".join(idx) + "\n", encoding="utf-8")
    print("FAQs {} | locations {} | pages {} | orgs {}".format(
        len(seen), len(locations), len(manifest), len(orgs)))


if __name__ == "__main__":
    main()
