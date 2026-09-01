#!/usr/bin/env python
"""Re-parse agency locations from the built markdown with tolerant patterns."""
import json, re, pathlib

KB = pathlib.Path(__file__).parent / "kb"
LOC = KB / "locations"

# a street number through STATE ZIP, on one line, non-greedy
RE_ADDR = re.compile(r"(\d+\s+[^\n]{5,80}?\b[A-Z]{2}\s+\d{5})")
RE_PHONE = re.compile(r"\b(\d{3}-\d{3}-\d{4})\b")
RE_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-]+@renegadeinsurance\.com)\b")
RE_DAY = re.compile(r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday):\s*(.+)$", re.M)
RE_RANGE = re.compile(r"(\d{1,2}(?::\d{2})?\s?[AP]M\s?-\s?\d{1,2}(?::\d{2})?\s?[AP]M)", re.I)
RE_STATUS = re.compile(r"^(Agency Update:.*)$", re.M)
RE_MGR = re.compile(r"Agency Manager,\s*([A-Z][a-zA-Z.'-]+(?:\s+[A-Z][a-zA-Z.'-]+){1,3})")
RE_TITLE = re.compile(r'^title:\s*"(.*)"', re.M)
RE_URL = re.compile(r"^source_url:\s*(\S+)", re.M)

HQ = "Beaverton"


def city_words(slug):
    return [w for w in slug.replace("agency__", "").split("-") if w]


def pick_address(text, slug):
    cands = [re.sub(r"\s+", " ", m).strip(" ,.") for m in RE_ADDR.findall(text)]
    if not cands:
        # some pages wrap the address across lines - retry on collapsed text
        flat = re.sub(r"\s+", " ", text)
        cands = [re.sub(r"\s+", " ", m).strip(" ,.") for m in RE_ADDR.findall(flat)]
    cands = [c for c in cands if HQ not in c]
    if not cands:
        return None
    words = city_words(slug)
    for c in cands:
        low = c.lower()
        if all(w in low for w in words):
            return c
    return cands[0]


def parse(f):
    txt = f.read_text(encoding="utf-8")
    slug = f.stem
    tm = RE_TITLE.search(txt)
    um = RE_URL.search(txt)
    name = (tm.group(1) if tm else slug).replace(" - Renegade Insurance", "").strip()
    d = {"slug": slug, "name": name, "url": um.group(1) if um else None}

    sm = RE_STATUS.search(txt)
    d["status_note"] = sm.group(1).strip() if sm else None
    d["closed"] = bool(d["status_note"] and re.search(r"closed", d["status_note"], re.I))

    phones = list(dict.fromkeys(RE_PHONE.findall(txt)))
    d["phone"] = phones[0] if phones else None
    d["other_phones"] = phones[1:] or None

    emails = list(dict.fromkeys(RE_EMAIL.findall(txt)))
    emails = [e for e in emails if not e.startswith("info@")]
    d["email"] = emails[0] if emails else None
    d["other_emails"] = emails[1:] or None

    d["address"] = pick_address(txt, slug)

    hb = {}
    for day, val in RE_DAY.findall(txt):
        hb.setdefault(day, val.strip())
    # also check the sibling get-a-quote page
    sib = LOC / (slug + "__get-a-quote.md")
    if sib.exists():
        for day, val in RE_DAY.findall(sib.read_text(encoding="utf-8")):
            hb.setdefault(day, val.strip())
    d["hours_by_day"] = hb or None
    rr = RE_RANGE.findall(txt)
    d["hours_summary"] = re.sub(r"\s+", " ", rr[0]) if rr else None

    mm = RE_MGR.search(txt)
    d["agency_manager"] = mm.group(1) if mm else None
    return d


def main():
    files = sorted(f for f in LOC.glob("agency__*.md")
                   if re.fullmatch(r"agency__[a-z-]+", f.stem))
    locs = [parse(f) for f in files]
    locs.sort(key=lambda d: d["name"])

    (KB / "locations.json").write_text(
        json.dumps(locs, indent=1, ensure_ascii=False), encoding="utf-8")

    L = ["# Agency Location Directory\n",
         "_{} agency locations with a dedicated page. Parsed from page copy - these pages carry "
         "no LocalBusiness structured data, so this table is the structured version._\n".format(len(locs)),
         "| Agency | Phone | Email | Address | Manager | Status |",
         "|---|---|---|---|---|---|"]
    for d in locs:
        L.append("| [{}]({}) | {} | {} | {} | {} | {} |".format(
            d["name"], d["url"], d["phone"] or "-", d["email"] or "-",
            d["address"] or "-", d["agency_manager"] or "-",
            "**CLOSED**" if d["closed"] else "open"))

    notices = [d for d in locs if d["status_note"]]
    if notices:
        L.append("\n## Closure / status notices\n")
        for d in notices:
            L.append("- **{}** - {}".format(d["name"], d["status_note"]))

    L.append("\n## Hours\n")
    for d in locs:
        hb = d.get("hours_by_day")
        if hb:
            days = ", ".join("{} {}".format(k[:3], v) for k, v in hb.items())
            L.append("- **{}**: {}".format(d["name"], days))
        elif d.get("hours_summary"):
            L.append("- **{}**: {} (weekdays)".format(d["name"], d["hours_summary"]))

    extra = [d for d in locs if d.get("other_emails") or d.get("other_phones")]
    if extra:
        L.append("\n## Additional contacts found on the page\n")
        for d in extra:
            bits = []
            if d.get("other_phones"):
                bits.append("phones: " + ", ".join(d["other_phones"]))
            if d.get("other_emails"):
                bits.append("emails: " + ", ".join(d["other_emails"]))
            L.append("- **{}** - {}".format(d["name"], "; ".join(bits)))

    (LOC / "_directory.md").write_text("\n".join(L) + "\n", encoding="utf-8")

    miss = [d["name"] for d in locs if not d["address"]]
    print("locations: {}".format(len(locs)))
    print("with address: {}".format(sum(1 for d in locs if d["address"])))
    print("with per-day hours: {}".format(sum(1 for d in locs if d["hours_by_day"])))
    print("with manager: {}".format(sum(1 for d in locs if d["agency_manager"])))
    if miss:
        print("MISSING address:", miss)


if __name__ == "__main__":
    main()
