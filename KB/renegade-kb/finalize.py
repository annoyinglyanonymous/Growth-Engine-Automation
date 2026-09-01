#!/usr/bin/env python
"""Final pass: state license table, README with all assets, caveats."""
import json, re, pathlib

BASE = pathlib.Path(__file__).parent
KB = BASE / "kb"

STATES = {
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut",
    "Delaware", "District of Columbia", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois",
    "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts",
    "Michigan", "Minnesota", "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York", "North Carolina", "North Dakota",
    "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina",
    "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
    "West Virginia", "Wisconsin", "Wyoming",
}


def licenses():
    f = KB / "legal" / "license-disclosures.md"
    lines = [l.strip() for l in f.read_text(encoding="utf-8").split("\n")]
    pairs, i = [], 0
    while i < len(lines):
        if lines[i] in STATES:
            state = lines[i]
            for j in range(i + 1, min(i + 5, len(lines))):
                v = lines[j]
                if not v:
                    continue
                if v in STATES:
                    break
                if re.fullmatch(r"[A-Z0-9][A-Z0-9\- ]{3,25}", v):
                    pairs.append((state, v))
                    break
                break
        i += 1
    seen, out = set(), []
    for s, n in pairs:
        if s not in seen:
            seen.add(s)
            out.append({"state": s, "license_number": n})
    return out


def main():
    lic = licenses()
    if lic:
        L = ["# State Insurance Licenses\n",
             "_{} states listed on the license disclosures page._\n".format(len(lic)),
             "| State | License number |", "|---|---|"]
        for d in lic:
            L.append("| {} | {} |".format(d["state"], d["license_number"]))
        L.append("\n`source: https://renegadeinsurance.com/license-disclosures/`")
        (KB / "legal" / "state-licenses.md").write_text("\n".join(L) + "\n", encoding="utf-8")
        (KB / "state-licenses.json").write_text(
            json.dumps(lic, indent=1, ensure_ascii=False), encoding="utf-8")

    man = json.loads((KB / "_manifest.json").read_text(encoding="utf-8"))
    locs = json.loads((KB / "locations.json").read_text(encoding="utf-8"))
    faq_n = len(re.findall(r"^### ", (KB / "faqs.md").read_text(encoding="utf-8"), re.M))

    cats = sorted(set(m["category"] for m in man))
    R = ["# Renegade Insurance Knowledgebase\n",
         "Structured knowledgebase scraped from <https://renegadeinsurance.com/>.",
         "**The blog was excluded by design** (all `/blog/` content and blog posts skipped).\n",
         "## What's here\n",
         "| Asset | Contents |", "|---|---|",
         "| [company/company-profile.md](company/company-profile.md) | HQ address, phone, email, "
         "licensing/agent/rating claims, positioning, differentiators |",
         "| [products-and-quotes/coverage-catalog.md](products-and-quotes/coverage-catalog.md) | "
         "11 featured coverages with copy, 18-item full coverage list, 11-carrier panel |",
         "| [locations/_directory.md](locations/_directory.md) | {} agency locations: phone, email, "
         "address, manager, per-day hours, closures |".format(len(locs)),
         "| [locations.json](locations.json) | Same location data as JSON |",
         "| [faqs.md](faqs.md) | {} unique Q&A pairs harvested from FAQPage structured data |".format(faq_n),
         "| [legal/state-licenses.md](legal/state-licenses.md) | Per-state license numbers ({} states) |".format(len(lic)),
         "| [state-licenses.json](state-licenses.json) | Same license data as JSON |",
         "| [redirects.md](redirects.md) | 4 paths that leave the site for the customer portal |",
         "| [_structured-data/](_structured-data/) | Raw JSON-LD per page + organizations.json |",
         "| [_manifest.json](_manifest.json) | Every page: slug, category, title, URL, size, aliases |",
         "\n## Page corpus ({} pages)\n".format(len(man))]

    for c in cats:
        rows = sorted([m for m in man if m["category"] == c], key=lambda r: -r["chars"])
        substantial = [m for m in rows if m["chars"] >= 900]
        stubs = [m for m in rows if m["chars"] < 900]
        R.append("\n### {}  ({} pages)\n".format(c, len(rows)))
        for m in substantial:
            extra = "  _(+{} alias URL)_".format(len(m["aliases"])) if m["aliases"] else ""
            R.append("- [{}]({}/{}.md) - {:,} chars{}".format(
                m["title"] or m["slug"], c, m["slug"], m["chars"], extra))
        if stubs:
            R.append("\n<details><summary>{} thin form/CTA stub pages</summary>\n".format(len(stubs)))
            for m in stubs:
                R.append("- [{}]({}/{}.md) - {:,} chars".format(
                    m["title"] or m["slug"], c, m["slug"], m["chars"]))
            R.append("\n</details>")

    R += ["\n## Caveats & known gaps\n",
          "- **Blog excluded on purpose.** `post-sitemap.xml` and `/blog/` were never fetched.",
          "- **Miami has no parent page.** `/agency/miami/` returns nothing, but its child pages "
          "(`become-a-partner`, `get-a-quote`) exist and are captured. Charleston likewise has only "
          "a stray thank-you page, which was filtered out.",
          "- **4 paths redirect off-site** to `customer.renegadeinsurance.com` (see "
          "[redirects.md](redirects.md)). Two are login-gated; these were **not** authenticated into, "
          "so no portal content is included.",
          "- **4 URLs are duplicate template stubs** serving byte-identical homepage content "
          "(`/agency/`, `/support/`, `/get-a-quote/`). They are recorded as `aliases` in "
          "`company/home.md` rather than stored four times.",
          "- **Agency pages carry no LocalBusiness structured data**, so addresses, hours and "
          "managers were parsed from page copy. Spot-check before relying on them commercially.",
          "- **Hours conflict slightly by page.** Some agency pages state `8:30AM-5PM` in the header "
          "while their quote sub-page lists `9AM-5PM` per day. The per-day values are used here.",
          "- **Marketing claims are reproduced, not verified** - \"48 states\", \"200+ agents\", "
          "\"100 insurance companies\", \"4.7/5\" are the site's own numbers. Note the site claims "
          "100 carriers but shows 11 logos.",
          "- Image `alt` text and form field labels were stripped from page bodies; the carrier list "
          "was recovered from `alt` attributes before stripping.",
          "\n## Contradictions found in the source content\n",
          "These are real inconsistencies on the live site, preserved rather than silently "
          "reconciled. Worth resolving before the knowledgebase is used to answer customers.\n",
          "| Topic | Conflicting values | Where |",
          "|---|---|---|",
          "| Franchise fee | **$20,000** vs **$25,000** starting fee | `/about-us/` vs `/franchise/` |",
          "| Carrier count | \"100 insurance companies\" vs 11 carrier logos | site meta vs homepage panel |",
          "| Agency hours | `8:30AM-5PM` vs `9AM-5PM` | agency page header vs its quote sub-page |",
          "| Pembroke Pines meta | its meta description markets \"Renegade Insurance Miami\" and "
          "shares Miami's 305-944-3030 number | `/agency/pembroke-pines/` |",
          "\n## Provenance\n",
          "Captured via the site's own origin in a real browser session (SiteGround bot protection "
          "blocks `curl` and datacenter fetchers with a JS challenge). Raw HTML retained under "
          "`raw/html/` for re-extraction; `extract.py`, `build_kb.py`, `enrich.py`, "
          "`fix_locations.py`, `finalize.py` regenerate everything.",
          ]
    (KB / "README.md").write_text("\n".join(R) + "\n", encoding="utf-8")
    print("state licenses: {}".format(len(lic)))
    print("README rebuilt; pages {}, faqs {}, locations {}".format(len(man), faq_n, len(locs)))


if __name__ == "__main__":
    main()
