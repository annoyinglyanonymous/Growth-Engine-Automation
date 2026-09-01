"""Pure-function tests for normalize.py.

Every case here is a regression guard for something that was actually wrong in
this corpus, not an invented example. The damage strings are copied from real
files so a future edit cannot quietly reintroduce the problem.
"""

from Ingestion.normalize import (
    THIN_CHARS,
    collapse_ws,
    fix_text,
    is_excluded,
    looks_like_claim,
    normalize_text,
    split_frontmatter,
    strip_chrome,
)


# ---------------------------------------------------------------- fix_text

def test_fixes_mojibake_em_dash():
    # Real: "Car Insurance That Fits Your Life â€"" in product-lines/car-insurance.md
    assert fix_text("Your Life â€” And Your Budget") == \
        "Your Life — And Your Budget"


def test_fixes_mojibake_phone_glyph():
    # Real: "â˜Ž 386-944-5555" in the Edgewater meta description
    assert "☎" in fix_text("Experts â˜Ž 386-944-5555")


def test_decodes_html_entities():
    # Real: "P&amp;C insurance company" throughout faqs.md
    assert fix_text("modern P&amp;C insurance company") == \
        "modern P&C insurance company"


def test_converts_nbsp_to_a_real_space():
    assert fix_text("Get a quote") == "Get a quote"


def test_deletes_zero_width_characters():
    """Deleted, not replaced: they are invisible joiners, not spaces."""
    assert fix_text("Get a​quote") == "Get aquote"
    assert fix_text("﻿Leading BOM") == "Leading BOM"


def test_fix_text_is_idempotent():
    once = fix_text("modern P&amp;C â€” today")
    assert fix_text(once) == once


# ------------------------------------------------------- split_frontmatter

FRONTMATTER_DOC = '''---
title: "About Us - Renegade Insurance"
source_url: https://renegadeinsurance.com/about-us/
category: company
---

# We are Renegade
'''


def test_splits_frontmatter():
    fm, body, warnings = split_frontmatter(FRONTMATTER_DOC)
    assert fm["category"] == "company"
    assert fm["title"] == "About Us - Renegade Insurance"
    assert body.strip().startswith("# We are Renegade")
    assert warnings == ()


def test_missing_frontmatter_warns_but_keeps_body():
    fm, body, warnings = split_frontmatter("# Renegade Insurance - FAQs\n\ntext")
    assert fm == {}
    assert "text" in body
    assert warnings == ("no-frontmatter",)


def test_malformed_yaml_does_not_raise():
    """One bad file must not abort a run over 880 of them."""
    fm, body, warnings = split_frontmatter("---\ntitle: \"unclosed\n  bad: [\n---\nbody\n")
    assert fm == {}
    assert warnings and warnings[0].startswith("bad-yaml")


# ----------------------------------------------------------- strip_chrome

def test_removes_nav_block_and_its_list():
    body = ("Primary Menu\n"
            "- Franchise with Renegade\n"
            "- Sell Your Agency\n"
            "\n"
            "# We are Renegade\n"
            "Real content here.\n")
    out, notes = strip_chrome(body)
    assert "Primary Menu" not in out
    assert "Franchise with Renegade" not in out
    assert "# We are Renegade" in out
    assert "Real content here." in out
    assert "nav-block-removed" in notes


def test_agencyheight_page_without_h1_keeps_its_content():
    """171 of 239 files have no H1 at all.

    An earlier design sliced everything before the first H1, which silently
    kept the chrome on every Agency Height page (their titles are plain text
    and headings start at ##).
    """
    body = ("Agency Management System\n"
            "Everything You Need To Know\n"
            "## What is an AMS?\n"
            "An AMS is a system of record.\n")
    out, _ = strip_chrome(body)
    assert "Agency Management System" in out
    assert "An AMS is a system of record." in out


def test_truncates_at_footer_marker():
    body = ("## Real Section\n"
            "Body text that matters.\n"
            "Quick Links\n"
            "Agency Management System 101\n"
            "Agency Management System FAQ\n")
    out, notes = strip_chrome(body)
    assert "Body text that matters." in out
    assert "Quick Links" not in out
    assert "Agency Management System 101" not in out
    assert "footer-truncated" in notes


def test_removes_inline_widget_but_keeps_content_after_it():
    """The quote CTA sits at line 58 of 149 in one careers page.

    Truncating at it -- rather than dropping just those lines -- would have
    thrown away the second half of several real articles.
    """
    body = ("## Save more by comparing quotes from multiple insurance agents near you.\n"
            "Easy, free and secure.\n"
            "Search Agents\n"
            "Trusted agents working with top national companies.\n"
            "## Technological Support\n"
            "Brightway emphasises the human touch.\n")
    out, notes = strip_chrome(body)
    assert "Search Agents" not in out
    assert "Easy, free and secure." not in out
    assert "## Technological Support" in out
    assert "Brightway emphasises the human touch." in out
    assert "inline-widget-removed" in notes


def test_removes_email_capture_widget():
    body = ("Agency Starter Kit\n"
            "Enter Email Address\n"
            "Join Us\n"
            "Your email has been registered. Redirecting...\n"
            "## What is Inside\n")
    out, _ = strip_chrome(body)
    assert "Enter Email Address" not in out
    assert "Join Us" not in out
    assert "Agency Starter Kit" in out
    assert "## What is Inside" in out


# ------------------------------------------------------------ collapse_ws

def test_collapses_blank_runs_and_trailing_space():
    assert collapse_ws("a   \n\n\n\n\nb  \n") == "a\n\nb"


def test_normalises_crlf():
    assert collapse_ws("a\r\nb") == "a\nb"


# -------------------------------------------------------- looks_like_claim

def test_flags_marketing_figures():
    for text in ("Franchise fees start at $20,000",
                 "Save 40% on premiums",
                 "over 200+ agents nationwide",
                 "rated 4.7/5 by customers",
                 "5k+ agencies served"):
        assert looks_like_claim(text), text


def test_does_not_flag_plain_prose():
    for text in ("Renegade helps agents own and grow agencies.",
                 "Agents are independent rather than captive."):
        assert not looks_like_claim(text), text


# ------------------------------------------------------------ is_excluded

def test_excludes_indexes_and_link_dumps():
    assert is_excluded("agencyheight-kb/kb/company/sitemap.md")
    assert is_excluded("renegade-kb/kb/README.md")
    assert is_excluded("renegade-kb/kb/redirects.md")


def test_excludes_files_superseded_by_json():
    """Both are markdown renderings of data json_ingest owns.

    Ingesting both put all 48 licences and all 11 locations in the corpus
    twice, as competing chunks.
    """
    assert is_excluded("renegade-kb/kb/locations/_directory.md")
    assert is_excluded("renegade-kb/kb/legal/state-licenses.md")


def test_excludes_raw_and_structured_data_dirs():
    assert is_excluded("renegade-kb/raw/html/about-us.html")
    assert is_excluded("renegade-kb/kb/_structured-data/about-us.json")


def test_keeps_real_content():
    assert not is_excluded("renegade-kb/kb/company/about-us.md")
    assert not is_excluded("renegade-kb/kb/faqs.md")
    assert not is_excluded("agencyheight-kb/kb/product-lines/car-insurance.md")


# ---------------------------------------------------------- normalize_text

def test_hash_ignores_chrome_differences():
    """content_hash is of the CLEANED body.

    Two files whose only difference is nav chrome must hash identically, or
    re-ingest would rewrite chunks that did not actually change.
    """
    plain = "---\ntitle: \"T\"\n---\n\n# Heading\n\nBody text.\n"
    chromed = ("---\ntitle: \"T\"\n---\n\nPrimary Menu\n- Blog\n\n"
               "# Heading\n\nBody text.\n")
    a = normalize_text(plain, path="brand-kb/kb/company/a.md")
    b = normalize_text(chromed, path="brand-kb/kb/company/b.md")
    assert a.content_hash == b.content_hash


def test_thin_flagged_not_dropped():
    doc = normalize_text("---\ntitle: \"Stub\"\n---\n\n# Get A Quote\n",
                         path="brand-kb/kb/x/stub.md")
    assert doc.thin is True
    assert doc.char_len < THIN_CHARS
    assert doc.title == "Stub"


def test_path_is_the_natural_key_and_stays_posix():
    doc = normalize_text(FRONTMATTER_DOC, path="renegade-kb/kb/company/about-us.md")
    assert doc.path == "renegade-kb/kb/company/about-us.md"
    assert "\\" not in doc.path
    assert doc.slug == "about-us"


def test_frontmatter_values_are_also_cleaned():
    """Mojibake lives in titles and meta descriptions, not just page bodies."""
    raw = ('---\n'
           'title: "HIG â˜Ž 386-944-5555"\n'
           'meta_description: "P&amp;C coverage"\n'
           '---\n\n# Body\n')
    doc = normalize_text(raw, path="renegade-kb/kb/locations/x.md")
    assert "☎" in doc.title
    assert doc.meta_description == "P&C coverage"
