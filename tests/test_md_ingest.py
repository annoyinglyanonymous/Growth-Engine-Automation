"""Chunking tests for md_ingest.py."""

import pytest

from Ingestion.md_ingest import (
    MAX_CHARS,
    OVERLAP_CHARS,
    SPLIT_THRESHOLD,
    add_overlap,
    brand_for,
    chunk_page,
    faq_documents,
    pack_sections,
    slugify,
    split_paragraphs,
    split_sections,
)
from Ingestion.normalize import normalize_text


def make_doc(body: str, path="renegade-kb/kb/company/x.md", title="T"):
    return normalize_text(f'---\ntitle: "{title}"\n---\n\n{body}', path=path)


# ---------------------------------------------------------- split_sections

def test_splits_on_h2_only():
    """Renegade uses # for the title and ## for sections; Agency Height has no
    # at all. ## is therefore the section boundary for both brands."""
    body = ("# Page Title\nIntro text.\n"
            "## First Section\nAlpha.\n"
            "### Not A Boundary\nBeta.\n"
            "## Second Section\nGamma.\n")
    sections = split_sections(body)
    headings = [h for h, _ in sections]
    assert headings == ["", "First Section", "Second Section"]
    # the h3 stays inside its parent section rather than splitting it
    assert "### Not A Boundary" in dict(zip(headings, [t for _, t in sections]))["First Section"]


def test_preamble_before_first_heading_is_kept():
    """On Agency Height pages that preamble is the title and intro -- real
    content, not chrome."""
    sections = split_sections("Agency Management System\nEverything you need.\n"
                              "## What is it?\nA system of record.\n")
    assert sections[0][0] == ""
    assert "Everything you need." in sections[0][1]


def test_document_with_no_headings_is_one_section():
    sections = split_sections("Just a wall of prose with no headings at all.")
    assert len(sections) == 1
    assert sections[0][0] == ""


# ------------------------------------------------------------- chunk_page

def test_small_document_is_a_single_unsplit_chunk():
    doc = make_doc("# Title\n\n" + "Short body. " * 20)
    chunks = chunk_page(doc)
    assert len(chunks) == 1
    assert chunks[0].ordinal == 0
    assert chunks[0].heading_path == ""


def test_empty_body_yields_no_chunks():
    """Two pages normalise to zero chars -- their whole content was the quote
    widget. An empty chunk is a row with an empty tsvector: never matches,
    always costs. kb.chunks has a CHECK constraint forbidding it."""
    doc = make_doc("")
    assert chunk_page(doc) == []


def test_large_document_is_split_on_headings():
    body = "# Title\n\n" + "".join(
        f"## Section {i}\n" + ("Sentence about insurance coverage. " * 60) + "\n\n"
        for i in range(6)
    )
    doc = make_doc(body)
    assert doc.char_len > SPLIT_THRESHOLD
    chunks = chunk_page(doc)
    assert len(chunks) > 1
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    assert all(c.heading_path for c in chunks[1:])


def test_no_split_chunk_exceeds_the_ceiling():
    """A single ## section can itself blow the budget -- insurance-news.md is
    one 14,399-char section -- so oversized sections get a paragraph split."""
    body = "# Title\n\n## One Huge Section\n\n" + \
           "\n\n".join("Paragraph about premiums and deductibles. " * 12
                       for _ in range(40))
    chunks = chunk_page(make_doc(body))
    assert len(chunks) > 1
    assert max(c.char_len for c in chunks) <= MAX_CHARS + OVERLAP_CHARS + 200


#: ~2,000 chars each, so a handful of sections clears SPLIT_THRESHOLD and the
#: document is actually split. A body under the threshold becomes ONE chunk,
#: which makes any per-chunk assertion vacuous.
PLAIN_SECTION = "Agents own their agencies outright. " * 57
MONEY_SECTION = "Fees start at $20,000 here. " * 72


def big_body(*sections: str) -> str:
    return "# Title\n\n" + "".join(
        f"## Section {i}\n{text}\n\n" for i, text in enumerate(sections)
    )


def test_claim_like_is_evaluated_per_chunk_not_per_document():
    """140 documents contain a marketing figure, usually in one section.
    Flagging every chunk of those documents would make the label meaningless.

    The money section goes last: overlap prepends the previous chunk's tail, so
    putting it first would carry the figure forward. See the next test.
    """
    doc = make_doc(big_body(PLAIN_SECTION, PLAIN_SECTION, PLAIN_SECTION,
                            PLAIN_SECTION, MONEY_SECTION))
    assert doc.char_len > SPLIT_THRESHOLD, "body must be large enough to split"

    chunks = chunk_page(doc)
    assert len(chunks) > 1
    assert any(c.claim_like for c in chunks)
    assert not chunks[0].claim_like


def test_overlap_can_carry_a_figure_into_the_next_chunk():
    """Intended, not a leak. claim_like describes the text the agent actually
    receives, and the overlap is part of that text -- so a chunk inheriting
    "$20,000" through overlap is correctly labelled unverified. Over-flagging
    is the safe direction for a compliance signal.
    """
    doc = make_doc(big_body(MONEY_SECTION, PLAIN_SECTION, PLAIN_SECTION,
                            PLAIN_SECTION, PLAIN_SECTION))
    chunks = chunk_page(doc)

    assert chunks[0].claim_like
    # only one section carries a figure, yet more than one chunk is flagged
    assert sum(1 for c in chunks if c.claim_like) >= 2
    assert not chunks[-1].claim_like


def test_document_with_no_figures_flags_nothing():
    body = "# Title\n\n" + "".join(
        f"## Section {i}\n" + ("Agents own their agencies outright. " * 60) + "\n\n"
        for i in range(6)
    )
    chunks = chunk_page(make_doc(body))
    assert len(chunks) > 1
    assert not any(c.claim_like for c in chunks)


# ------------------------------------------------------------ add_overlap

def test_overlap_carries_the_previous_tail():
    pieces = [("A", "First chunk ends with this exact sentence."),
              ("B", "Second chunk begins here.")]
    out = add_overlap(pieces)
    assert out[0][1] == pieces[0][1]          # first chunk untouched
    assert "exact sentence." in out[1][1]     # tail carried forward
    assert out[1][1].endswith("Second chunk begins here.")


def test_overlap_starts_on_a_word_boundary():
    pieces = [("A", "x" * 500 + " alpha beta gamma delta"), ("B", "next")]
    carried = add_overlap(pieces)[1][1].split("\n\n")[0]
    assert not carried.startswith("x")


# ---------------------------------------------------------- split_paragraphs

def test_paragraph_split_respects_the_limit():
    text = "\n\n".join("Sentence one. Sentence two. Sentence three." * 8
                       for _ in range(20))
    for block in split_paragraphs(text, 1000):
        assert len(block) <= 1000


def test_single_oversized_paragraph_splits_on_sentences():
    text = "This is a sentence that goes on. " * 200
    blocks = split_paragraphs(text, 600)
    assert len(blocks) > 1
    assert all(len(b) <= 600 for b in blocks)


# ------------------------------------------------------------- pack_sections

def test_packing_merges_small_sections():
    sections = [(f"S{i}", f"## S{i}\nshort body {i}") for i in range(10)]
    packed = pack_sections(sections)
    assert len(packed) < len(sections)
    assert "/" in packed[0][0]      # merged headings joined


# ------------------------------------------------------------------- FAQs

FAQ_BODY = """# Renegade Insurance - FAQs

### What is Renegade Insurance?

Renegade is a modern P&C insurance company.

`source: https://renegadeinsurance.com/about-us/`

### How much does a franchise cost?

Fees start at $20,000 depending on business type.

`source: https://renegadeinsurance.com/franchise/`
"""


def test_faq_becomes_one_document_per_pair():
    """Each pair carries its own source url, and kb.documents.url is
    per-document -- bundling all 588 under faqs.md would make every citation
    point at faqs.md instead of the real source page."""
    doc = make_doc(FAQ_BODY, path="renegade-kb/kb/faqs.md")
    faqs = faq_documents(doc, "renegade")
    assert len(faqs) == 2
    assert all(f.kind == "faq" for f in faqs)
    assert faqs[0].url == "https://renegadeinsurance.com/about-us/"
    assert faqs[1].url == "https://renegadeinsurance.com/franchise/"
    assert faqs[0].path == "renegade-kb/kb/faqs.md#what-is-renegade-insurance"


def test_faq_source_line_is_not_retrievable_content():
    doc = make_doc(FAQ_BODY, path="renegade-kb/kb/faqs.md")
    for faq in faq_documents(doc, "renegade"):
        assert "source:" not in faq.chunks[0].text


def test_faq_chunk_keeps_the_question_above_the_answer():
    """A bare answer loses the context that makes it retrievable, and the
    question also lands in the tsvector at weight B via heading_path."""
    faq = faq_documents(make_doc(FAQ_BODY, path="renegade-kb/kb/faqs.md"),
                        "renegade")[0]
    assert faq.chunks[0].text.startswith("What is Renegade Insurance?")
    assert faq.chunks[0].heading_path == "What is Renegade Insurance?"


def test_faq_entities_are_decoded():
    faq = faq_documents(make_doc(FAQ_BODY, path="renegade-kb/kb/faqs.md"),
                        "renegade")[0]
    assert "P&C" in faq.chunks[0].text
    assert "&amp;" not in faq.chunks[0].text


def test_duplicate_questions_get_distinct_paths():
    """UNIQUE (brand_id, path) would reject the second one."""
    body = ("### Same question?\n\nAnswer one.\n\n"
            "### Same question?\n\nAnswer two.\n")
    faqs = faq_documents(make_doc(body, path="renegade-kb/kb/faqs.md"), "renegade")
    assert len({f.path for f in faqs}) == len(faqs) == 2


def test_faq_thin_threshold_is_not_the_page_threshold():
    """FAQ answers run 100-400 chars; the page threshold of 500 would flag
    nearly all 588 of them as thin."""
    faqs = faq_documents(make_doc(FAQ_BODY, path="renegade-kb/kb/faqs.md"),
                         "renegade")
    assert all(not f.thin for f in faqs)


# ------------------------------------------------------------------ misc

@pytest.mark.parametrize("path,expected", [
    ("renegade-kb/kb/company/about-us.md", "renegade"),
    ("agencyheight-kb/kb/product-lines/car-insurance.md", "agencyheight"),
    ("primers/shared.md", None),
])
def test_brand_for(path, expected):
    """A top-level primers/ directory resolves to no brand -- which is why
    primers live inside each brand directory instead."""
    assert brand_for(path) == expected


def test_slugify_handles_punctuation_and_length():
    assert slugify("What is Renegade Insurance?") == "what-is-renegade-insurance"
    assert len(slugify("word " * 50)) <= 60
    assert not slugify("What is Renegade Insurance?").endswith("-")
