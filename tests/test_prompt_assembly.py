"""Tests for generate.to_system_prompt.

Pure function over a dict, so it needs no database and no API key -- which is
why it is worth testing at all. Every other governance defect in this project
so far has been of the same shape: the SQL was right, the data was right, and
the assembled prompt silently dropped it. 005 added rules that /context never
returned; 012 added restricted claims that fetch_claims filtered out. Neither
was caught by a corpus test, because neither was a corpus problem.

What matters here is not formatting but three invariants:
  1. governance precedes evidence
  2. a claim's condition travels with the claim
  3. a prohibited claim is never rendered as usable
"""

from __future__ import annotations

import generate


def claim(status: str, text: str, **kw) -> dict:
    """A claims row with the columns fetch_claims actually selects."""
    row = {
        "status": status,
        "claim_text": text,
        "approved_wording": None,
        "category": "pricing",
        "requires_disclaimer": False,
        "disclaimer_text": None,
        "restriction_notes": None,
        "product": "Renegade Franchise Program",
    }
    row.update(kw)
    return row


def ctx(**kw) -> dict:
    base = {
        "brand": {"name": "Renegade Insurance"},
        "primer": None,
        "rules": [],
        "claims": [],
        "passages": [],
        "conflicts": [],
    }
    base.update(kw)
    return base


def section_order(prompt: str) -> list[str]:
    return [ln for ln in prompt.splitlines() if ln.startswith("## ")]


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

def test_governance_precedes_site_copy():
    """The one ordering guarantee to_system_prompt's docstring makes.

    A blocker rule after 3,000 tokens of scraped copy is a rule weighed
    against everything above it.
    """
    p = generate.to_system_prompt(ctx(
        rules=[{"severity": "blocker", "rule": "Never state $20,000."}],
        claims=[claim("approved", "Fee starts at $25,000.")],
        passages=[{"title": "Franchise", "text": "Own an agency.",
                   "disputed": False, "dispute_topics": []}],
    ))
    order = section_order(p)
    rules_at = next(i for i, s in enumerate(order) if "Rules" in s)
    copy_at = next(i for i, s in enumerate(order) if "Site copy" in s)
    claims_at = next(i for i, s in enumerate(order) if "Approved claims" in s)
    assert rules_at < claims_at < copy_at


def test_restricted_sits_between_approved_and_prohibited():
    p = generate.to_system_prompt(ctx(claims=[
        claim("approved", "A."),
        claim("prohibited", "B.", restriction_notes="never"),
        claim("restricted", "C.", restriction_notes="only with a date"),
    ]))
    order = " | ".join(section_order(p))
    assert (order.index("Approved")
            < order.index("Restricted")
            < order.index("Prohibited"))


# ---------------------------------------------------------------------------
# Restricted claims -- the regression this file was added for
# ---------------------------------------------------------------------------

def test_restricted_claim_is_rendered_at_all():
    """It was not, before 012.

    fetch_claims excluded restricted on the grounds that it is not safe to
    assert. The effect was that the agent never learned the claim existed,
    while the underlying site sentence stayed retrievable as ordinary copy.
    """
    p = generate.to_system_prompt(ctx(claims=[
        claim("restricted", "Renegade holds a 4.7 out of 5 Google rating.",
              restriction_notes="Only with the source named and an as-of date."),
    ]))
    assert "4.7 out of 5" in p


def test_restricted_claim_carries_its_condition():
    """A restricted claim without its condition is worse than absent: it reads
    as permission."""
    p = generate.to_system_prompt(ctx(claims=[
        claim("restricted", "Up to 90% cash upfront.",
              restriction_notes="Never omit 'up to'."),
    ]))
    assert "CONDITION:" in p
    assert "Never omit 'up to'." in p


def test_restricted_is_not_listed_as_assertable():
    """The approved section calls itself "the only figures you may assert", so
    a restricted claim appearing under it would make that sentence false."""
    p = generate.to_system_prompt(ctx(claims=[
        claim("approved", "Fee starts at $25,000."),
        claim("restricted", "4.7 out of 5 Google rating.",
              restriction_notes="Needs an as-of date."),
    ]))
    approved_block = p.split("## Approved claims")[1].split("##")[0]
    assert "$25,000" in approved_block
    assert "4.7" not in approved_block


def test_no_restricted_section_when_there_are_none():
    p = generate.to_system_prompt(ctx(claims=[
        claim("approved", "Fee starts at $25,000."),
    ]))
    assert "Restricted" not in p


# ---------------------------------------------------------------------------
# Approved and prohibited
# ---------------------------------------------------------------------------

def test_approved_wording_wins_over_claim_text():
    """claim_text is the assertion; approved_wording is the sentence to use.
    Where they differ, the qualifiers live in approved_wording."""
    p = generate.to_system_prompt(ctx(claims=[
        claim("approved",
              "Initial Renegade franchise fee starts at $25,000.",
              approved_wording="Initial franchise fees start at $25,000 "
                               "depending on your business type."),
    ]))
    assert "depending on your business type" in p


def test_disclaimer_is_flagged_not_merely_included():
    p = generate.to_system_prompt(ctx(claims=[
        claim("approved", "Fee starts at $25,000.",
              requires_disclaimer=True,
              disclaimer_text="Additional startup costs vary by state."),
    ]))
    assert "DISCLAIMER REQUIRED" in p
    assert "vary by state" in p


def test_prohibited_claim_never_appears_as_approved():
    p = generate.to_system_prompt(ctx(claims=[
        claim("prohibited", "Fee starts at $20,000.",
              restriction_notes="Stale copy on /about-us/."),
    ]))
    assert "Approved claims" not in p
    assert "$20,000" in p
    assert "Stale copy" in p


# ---------------------------------------------------------------------------
# Evidence labelling
# ---------------------------------------------------------------------------

def test_disputed_passage_is_labelled_inline():
    """The label has to sit next to the passage. A disputed-topics list at the
    end of the prompt does not attach to the text that carries the figure."""
    p = generate.to_system_prompt(ctx(passages=[
        {"title": "About Us", "text": "Fees start at $20,000.",
         "disputed": True, "dispute_topics": ["franchise-fee"]},
    ]))
    lines = p.splitlines()
    header = next(i for i, ln in enumerate(lines) if "About Us" in ln)
    assert "DISPUTED" in lines[header]
    assert "franchise-fee" in lines[header]
