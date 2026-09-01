#!/usr/bin/env python
"""Second pass: carrier list, coverage catalog, per-day hours, company profile."""
import json, re, pathlib, collections

BASE = pathlib.Path(__file__).parent
HTML = BASE / "raw" / "html"
KB = BASE / "kb"

# ---------------------------------------------------------------- carriers
CARRIER_FIX = {
    "the-hartford": "The Hartford", "us_assure-": "US Assure", "us_assure": "US Assure",
    "travellers": "Travelers", "national wide": "Nationwide", "nationalwide": "Nationwide",
    "universal property": "Universal Property", "bhhc": "Berkshire Hathaway Homestate (BHHC)",
    "asi": "ASI (Progressive)", "amtrust": "AmTrust", "chubb": "Chubb",
    "dairyland": "Dairyland", "stillwater": "Stillwater",
}
SKIP_ALT = re.compile(
    r"renegade|logo|icon|close|call|vector|arrow|star|group|computer|circle|"
    r"insurance$|blue|^$|wp-content|women|posing|map|quote|banner|bg|shape",
    re.I)


def carriers():
    html = (HTML / "home.html").read_text(encoding="utf-8", errors="replace")
    alts = re.findall(r'<img[^>]*\balt="([^"]{2,60})"', html, re.I)
    found, seen = [], set()
    for a in alts:
        a = a.strip()
        key = a.lower().strip("- ")
        if key in CARRIER_FIX:
            name = CARRIER_FIX[key]
        elif SKIP_ALT.search(a):
            continue
        else:
            continue
        if name not in seen:
            seen.add(name)
            found.append(name)
    return found


# ------------------------------------------------------------ coverages
def coverages():
    md = (KB / "company" / "home.md").read_text(encoding="utf-8")
    cats = {}
    # cards: "### X Insurance" followed by a description line
    block = md.split("## What We Cover", 1)
    if len(block) > 1:
        section = block[1].split("## Our Agents", 1)[0]
        for m in re.finditer(r"^### (.+?)\n\n(.+?)\n", section, re.M):
            cats[m.group(1).strip()] = m.group(2).strip()
    # the long slash-separated "Coverages We Provide" list
    full = []
    m = re.search(r"^(.*?/.*?)\nCoverages We Provide", md, re.M)
    if m:
        full = [c.strip() for c in m.group(1).split("/") if c.strip()]
    return cats, full


# ----------------------------------------------------------- per-day hours
DAY = re.compile(r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday):\s*(.+)$", re.M)


def per_day_hours():
    out = {}
    for f in (KB / "locations").glob("agency__*__get-a-quote.md"):
        city = f.stem.replace("agency__", "").replace("__get-a-quote", "")
        txt = f.read_text(encoding="utf-8")
        hrs = {d: v.strip() for d, v in DAY.findall(txt)}
        if hrs:
            out[city] = hrs
    return out


def main():
    car = carriers()
    cats, full = coverages()
    hours = per_day_hours()

    locs = json.loads((KB / "locations.json").read_text(encoding="utf-8"))
    added = 0
    for d in locs:
        city = d["slug"].replace("agency__", "")
        if city in hours:
            d["hours_by_day"] = hours[city]
            added += 1
    (KB / "locations.json").write_text(
        json.dumps(locs, indent=1, ensure_ascii=False), encoding="utf-8")

    # ---- coverages doc ----
    C = ["# Coverage & Product Catalog\n",
         "_Products Renegade Insurance sells, per the homepage and agency pages._\n"]
    if cats:
        C.append("## Featured coverages (with site descriptions)\n")
        for k, v in cats.items():
            C.append("### {}\n\n{}\n".format(k, v))
    if full:
        C.append("## Full coverage list (site footer band)\n")
        for c in full:
            C.append("- {}".format(c))
        C.append("")
    if car:
        C.append("\n## Carrier panel (logos shown on the homepage)\n")
        for c in car:
            C.append("- {}".format(c))
        C.append("\n_Note: the site elsewhere claims quotes from \"100 insurance companies\";"
                 " the {} above are the carriers whose logos appear on the homepage._"
                 .format(len(car)))
    (KB / "products-and-quotes").mkdir(exist_ok=True)
    (KB / "products-and-quotes" / "coverage-catalog.md").write_text(
        "\n".join(C) + "\n", encoding="utf-8")

    # ---- company profile ----
    home = (KB / "company" / "home.md").read_text(encoding="utf-8")
    P = ["# Company Profile - Key Facts\n",
         "_Extracted from site copy. Figures are the company's own marketing claims._\n",
         "| Fact | Value | Source |", "|---|---|---|",
         "| Corporate address | 9450 SW Gemini Dr, PMB 47941, Beaverton, OR 97008 | homepage footer |",
         "| Main phone | 770-723-3933 | homepage footer / customer portal |",
         "| General email | info@renegadeinsurance.com | homepage footer |",
         "| Customer portal | customer.renegadeinsurance.com | site nav |",
         "| Twitter/X | @renegadeins | site meta tags |"]
    for pat, label in [
        (r"licensed in (\d+) states", "States licensed"),
        (r"(\d+\+?) ?agents", "Agent network"),
        (r"([\d.]+) ?/ ?5 Google Reviews", "Google rating"),
        (r"(\d+) insurance companies", "Carriers claimed"),
    ]:
        m = re.search(pat, home, re.I)
        if m:
            P.append("| {} | {} | homepage |".format(label, m.group(1)))
    P.append("\n## Positioning\n")
    P.append("Renegade combines an insurance **marketplace** (multiple carrier quotes) with a "
             "**dedicated local agent**. Agents are independent rather than captive, so they can "
             "place business with multiple carriers.\n")
    P.append("## Stated differentiators vs. competitors\n")
    P.append("- vs. **other insurance sites**: no spam calls or redirects, does not sell your "
             "information, real service support")
    P.append("- vs. **local non-Renegade agents**: not limited to phone/in-person, does not sell "
             "your information, broader carrier access\n")
    (KB / "company" / "company-profile.md").write_text("\n".join(P) + "\n", encoding="utf-8")

    # ---- rebuild locations directory with per-day hours ----
    locs.sort(key=lambda d: d["name"])
    L = ["# Agency Location Directory\n",
         "_{} agency locations. Parsed from page text - these pages carry no LocalBusiness "
         "structured data._\n".format(len(locs)),
         "| Agency | Phone | Email | Address | Weekday hours | Status |",
         "|---|---|---|---|---|---|"]
    for d in locs:
        hb = d.get("hours_by_day") or {}
        if hb:
            wk = hb.get("Monday", "-")
            sat = hb.get("Saturday", "")
            hrs = wk + (" (Sat {})".format(sat) if sat and sat.lower() != "closed" else "")
        elif d.get("hours"):
            hrs = "{} {}".format(d["hours"], d.get("days") or "").strip()
        else:
            hrs = "-"
        L.append("| [{}]({}) | {} | {} | {} | {} | {} |".format(
            d["name"], d["url"], d["phone"] or "-", d["email"] or "-",
            d["address"] or "-", hrs, "**CLOSED**" if d["closed"] else "open"))
    notices = [d for d in locs if d.get("status_note")]
    if notices:
        L.append("\n## Notices\n")
        for d in notices:
            L.append("- **{}** - {}".format(d["name"], d["status_note"]))
    L.append("\n## Full per-day hours\n")
    for d in locs:
        hb = d.get("hours_by_day")
        if hb:
            days = ", ".join("{} {}".format(k[:3], v) for k, v in hb.items())
            L.append("- **{}**: {}".format(d["name"], days))
    (KB / "locations" / "_directory.md").write_text("\n".join(L) + "\n", encoding="utf-8")

    print("carriers: {}".format(len(car)))
    print("coverage cards: {} | full list: {}".format(len(cats), len(full)))
    print("locations with per-day hours: {}/{}".format(added, len(locs)))


if __name__ == "__main__":
    main()
