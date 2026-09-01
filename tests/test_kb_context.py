"""Tests for the packing logic extracted out of main.py's /context handler.

None of this was testable before the extraction: it lived inside a FastAPI
route, so exercising it meant starting a server and issuing HTTP requests. That
is the direct reason both governance defects found so far -- rules the endpoint
never returned, restricted claims fetch_claims filtered out -- were found by
hand rather than by a test.

Everything here is a pure function or a small stateful helper. No database, no
API key, no event loop.
"""

from __future__ import annotations

import pytest

from config import settings
from kb_context import (
    DISPUTED_NOTE,
    _Deduper,
    _jaccard,
    _shingle,
    estimate_tokens,
    governance_tokens,
)

# --------------------------------------------------------------------------
# Word-set similarity
# --------------------------------------------------------------------------

def test_shingle_is_case_and_punctuation_insensitive():
    assert _shingle("Franchise Fee!") == _shingle("franchise, fee")


def test_jaccard_bounds():
    a = _shingle("one two three")
    assert _jaccard(a, a) == 1.0
    assert _jaccard(a, _shingle("four five six")) == 0.0


def test_jaccard_of_empty_is_zero_not_an_error():
    """An empty chunk cannot reach here -- chunks_text_not_blank forbids it --
    but a zero guard is cheaper than a ZeroDivisionError in a request."""
    assert _jaccard(frozenset(), _shingle("x y")) == 0.0


# --------------------------------------------------------------------------
# The deduper
# --------------------------------------------------------------------------

#: The real shape of the problem: Renegade templates this FAQ per location, and
#: each copy is its own document, so max_chunks_per_document cannot see them as
#: related. Measured overlap between members of this family: 0.944 - 0.963.
def _location_faq(city: str) -> str:
    return (
        f"How will Renegade help me with {city}-specific insurance problems? "
        f"Renegade agents in {city} know the local market, the carriers that "
        f"write in {city}, and the coverage questions that come up most often "
        f"for {city} homeowners and business owners. Your agent lives and "
        f"works in the area and can meet you in person."
    )


def test_first_passage_is_never_a_duplicate():
    d = _Deduper(threshold=0.72, min_chars=240)
    assert d.is_duplicate(_location_faq("Orlando")) is False


def test_templated_family_is_suppressed_after_the_first():
    d = _Deduper(threshold=0.72, min_chars=240)
    cities = ["Orlando", "Porter", "Savannah", "Palm Bay", "Panama City"]
    verdicts = [d.is_duplicate(_location_faq(c)) for c in cities]
    assert verdicts[0] is False
    assert all(verdicts[1:]), "every later city should be dropped"


def test_distinct_passages_all_survive():
    d = _Deduper(threshold=0.72, min_chars=240)
    texts = [
        "Franchise owners earn 80% on new business personal lines commissions "
        "and up to 80% on renewals. Renegade pays agencies monthly based on "
        "carrier commissions received, so more of every policy stays with the "
        "owner rather than the carrier.",
        "Renegade's operations team handles customer service, bookkeeping and "
        "marketing so the franchise owner can focus entirely on selling. The "
        "back office is included rather than something the owner staffs and "
        "manages themselves from day one.",
        "Onboarding includes a mandatory two-week hands-on training programme "
        "using real leads. Training covers the full sales process, carrier "
        "systems and agency management for both experienced agents and career "
        "changers entering insurance.",
    ]
    assert [d.is_duplicate(t) for t in texts] == [False, False, False]


def test_short_passages_are_exempt():
    """Two brief passages on one topic can legitimately share most of their
    words, and suppressing them saves almost no budget."""
    d = _Deduper(threshold=0.72, min_chars=240)
    short = "Franchise fee starting at $25,000"
    assert d.is_duplicate(short) is False
    assert d.is_duplicate(short) is False, "identical short text still kept"


def test_a_dropped_passage_does_not_become_a_comparison_target():
    """Only kept passages seed the comparison set.

    Otherwise a chain of items each 0.72 similar to the previous one would
    drift arbitrarily far from anything actually selected.
    """
    d = _Deduper(threshold=0.72, min_chars=240)
    d.is_duplicate(_location_faq("Orlando"))
    d.is_duplicate(_location_faq("Porter"))     # dropped
    assert len(d.kept) == 1


def test_configured_threshold_sits_inside_the_measured_gap():
    """Guards the calibration recorded in config.py.

    Measured by scripts/calibrate_dedupe.py over 2,231 pooled pairs from six
    probe queries: the distinct population tops out at 0.5449 and the
    duplicate population starts at 0.6815.

    A threshold below the gap eats distinct passages; above it, duplicates
    survive. The first value chosen here was 0.72, which was above the gap and
    silently missed five duplicate pairs -- this test is what would have caught
    that.
    """
    assert 0.5449 < settings.near_duplicate_threshold < 0.6815


# --------------------------------------------------------------------------
# Token accounting
# --------------------------------------------------------------------------

def test_estimate_tokens_never_returns_zero():
    """A zero-cost passage would be packed regardless of budget."""
    assert estimate_tokens("") == 1
    assert estimate_tokens("a") == 1


def claim(status: str, **kw) -> dict:
    row = {"status": status, "claim_text": "x" * 40, "approved_wording": None,
           "requires_disclaimer": False, "disclaimer_text": None,
           "restriction_notes": None}
    row.update(kw)
    return row


def test_governance_charges_restriction_notes():
    """The bug this covers: the budget charged only claim_text while the prompt
    also rendered restriction_notes, under-counting governance by ~800 tokens
    against a 3,000-token budget and then overspending on passages."""
    notes = "y" * 400
    without = governance_tokens([], [claim("prohibited")])
    with_notes = governance_tokens([], [claim("prohibited",
                                              restriction_notes=notes)])
    assert with_notes > without
    assert with_notes - without == estimate_tokens(notes)


def test_governance_charges_disclaimer_only_when_required():
    text = "z" * 200
    off = governance_tokens([], [claim("approved", disclaimer_text=text)])
    on = governance_tokens([], [claim("approved", requires_disclaimer=True,
                                      disclaimer_text=text)])
    assert on - off == estimate_tokens(text)


def test_approved_claim_is_costed_at_its_wording_not_its_text():
    """approved_wording is what the prompt emits, and it is usually longer --
    it carries the qualifiers."""
    long_wording = "w" * 400
    c = claim("approved", approved_wording=long_wording)
    assert governance_tokens([], [c]) == estimate_tokens(long_wording)


def test_restriction_notes_on_an_approved_claim_are_not_charged():
    """to_system_prompt does not render them for approved rows, so charging
    them would reserve budget nothing spends."""
    c = claim("approved", restriction_notes="q" * 400)
    assert governance_tokens([], [c]) == estimate_tokens(c["claim_text"])


def test_rules_are_charged():
    rule = {"rule": "r" * 300, "severity": "blocker"}
    assert governance_tokens([rule], []) == estimate_tokens(rule["rule"])


# --------------------------------------------------------------------------
# Labelling
# --------------------------------------------------------------------------

def test_disputed_note_names_the_topics():
    """A note that does not say WHICH conflict applies is not actionable."""
    note = DISPUTED_NOTE.format(topics="franchise-fee, carrier-count")
    assert "franchise-fee" in note
    assert "carrier-count" in note
    assert "public.claims" in note


@pytest.mark.parametrize("threshold", [0.0, 1.0])
def test_deduper_extremes_are_coherent(threshold):
    """threshold 0 drops everything after the first; 1.0 drops only identical
    text. Neither is a configuration to use, but neither should crash."""
    d = _Deduper(threshold=threshold, min_chars=1)
    a, b = _location_faq("Orlando"), "completely unrelated words here entirely"
    assert d.is_duplicate(a) is False
    assert d.is_duplicate(b) is (threshold == 0.0)
