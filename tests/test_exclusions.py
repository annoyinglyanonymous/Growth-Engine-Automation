"""Operator-declared exclusions (023): "do not add this and that".

WHY THIS IS A SEPARATE CHECK FROM check_prohibited_wording
A prohibited CLAIM is a judgement about what is true, reached by review, and
it carries a source, a status and reasoning. An exclusion is an instruction
from a person, needing no justification at all. Merging them would mean
someone typing "free forever" had to be modelled as a claim -- wrong, and
enough friction that nobody would bother.

It is a BLOCKER. "Do not say this" is not advice, and a warning that can be
clicked past is not a prohibition.

The matching is contains_phrase, shared with the claim checks, so an exclusion
gets the same paraphrase-resistant word-boundary matching a reviewed trigger
phrase does -- case, whitespace and punctuation handled, substrings not
matched.
"""

from __future__ import annotations

import pytest

from validation.checks import CheckContext, check_excluded_wording


def ctx(*phrases, scope="brand", note=None) -> CheckContext:
    return CheckContext(exclusions=[
        {"phrase": p, "note": note, "scope": scope} for p in phrases])


def findings(content: dict, context: CheckContext):
    return check_excluded_wording(content, context)


# --------------------------------------------------------------------------
# It catches what it should
# --------------------------------------------------------------------------

def test_an_excluded_phrase_is_a_blocker():
    out = findings({"headline": "Free forever, no catch"},
                   ctx("free forever"))
    assert len(out) == 1
    assert out[0].severity == "blocker"
    assert out[0].check == "excluded_wording"


def test_case_does_not_matter():
    """The operator types it once, however they like. 'FREE FOREVER' in an
    ad is the same instruction broken."""
    assert findings({"h": "FREE FOREVER"}, ctx("free forever"))
    assert findings({"h": "free forever"}, ctx("Free Forever"))


def test_extra_whitespace_does_not_matter():
    assert findings({"h": "free   forever"}, ctx("free forever"))
    assert findings({"h": "free\nforever"}, ctx("free forever"))


def test_a_money_figure_is_caught():
    """contains_phrase anchors on word boundaries only where the edge is a
    word character, so a leading '$' does not defeat it."""
    assert findings({"offer": "Just $20,000 to start"}, ctx("$20,000"))


def test_the_finding_names_the_field():
    out = findings({"subject": "ok", "body": "free forever"},
                   ctx("free forever"))
    assert [f.field_name for f in out] == ["body"]


def test_every_offending_field_is_reported_not_just_the_first():
    """A reviewer fixing one field and re-running to find another is the
    pattern this project keeps designing out."""
    out = findings({"subject": "free forever", "body": "free forever"},
                   ctx("free forever"))
    assert {f.field_name for f in out} == {"subject", "body"}


def test_several_exclusions_each_report():
    out = findings({"h": "free forever and guaranteed leads"},
                   ctx("free forever", "guaranteed leads"))
    assert len(out) == 2


def test_the_evidence_is_the_phrase_the_operator_typed():
    """So the blocker can be traced back to the list entry that caused it."""
    out = findings({"h": "FREE FOREVER"}, ctx("free forever"))
    assert out[0].evidence == "free forever"


# --------------------------------------------------------------------------
# It does not catch what it should not
# --------------------------------------------------------------------------

def test_clean_copy_produces_nothing():
    assert findings({"h": "Markets is included on every plan"},
                    ctx("free forever")) == []


def test_no_exclusions_means_no_findings():
    assert findings({"h": "anything at all"}, CheckContext()) == []


def test_a_substring_inside_a_longer_word_is_not_a_match():
    """The reason contains_phrase is used rather than `in`. An exclusion on
    'ad' must not flag 'adjuster', 'additional' or 'admitted' -- which would
    make the feature unusable within a day of someone trying it."""
    assert findings({"h": "Speak to an adjuster about additional cover"},
                    ctx("ad")) == []


def test_a_single_character_exclusion_is_ignored():
    """023 puts a CHECK on brand_exclusions.phrase, but
    campaigns.do_not_mention is a text[] and Postgres cannot express a
    per-element CHECK without a subquery -- so one character typed into the
    brief reaches this function. contains_phrase is word-boundary anchored,
    so "a" genuinely IS a word in "a nice offer": without the guard, one
    keystroke blocks every asset in the campaign with a finding nobody could
    diagnose."""
    assert findings({"h": "a nice offer"}, ctx("a")) == []


def test_the_minimum_matches_the_schema():
    """If 023's CHECK relaxes, this is the other place to change."""
    from validation.checks import MIN_EXCLUSION_CHARS
    assert MIN_EXCLUSION_CHARS == 2


def test_a_two_character_exclusion_still_works():
    """The boundary itself, so the guard cannot quietly become three."""
    assert findings({"h": "we do AI quoting"}, ctx("AI"))


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_a_blank_phrase_matches_nothing(blank):
    """Otherwise an empty row silently blocks every asset, and the blocker
    would say the copy uses '' -- unactionable and impossible to diagnose."""
    assert findings({"h": "any copy"}, ctx(blank)) == []


def test_a_phrase_stored_with_padding_still_matches():
    assert findings({"h": "free forever"}, ctx("  free forever  "))


# --------------------------------------------------------------------------
# The message tells the reviewer which list to argue with
# --------------------------------------------------------------------------

def test_a_brand_exclusion_says_so():
    out = findings({"h": "free forever"}, ctx("free forever", scope="brand"))
    assert "this brand" in out[0].message


def test_a_brief_exclusion_says_so():
    """The distinction is the whole reason 023 has two lists: a standing rule
    is a compliance question, a one-off is a question about this campaign.
    The reviewer needs to know which one to go and change."""
    out = findings({"h": "free forever"}, ctx("free forever", scope="brief"))
    assert "the brief" in out[0].message


def test_an_unknown_scope_does_not_claim_to_be_the_brief():
    """Defaulting the other way would tell a reviewer to edit a brief that
    does not contain the exclusion."""
    out = check_excluded_wording(
        {"h": "free forever"},
        CheckContext(exclusions=[{"phrase": "free forever"}]))
    assert "this brand" in out[0].message


# --------------------------------------------------------------------------
# Every blocker is actionable
# --------------------------------------------------------------------------

def test_the_operators_own_note_becomes_the_remedy():
    out = findings({"h": "free forever"}, ctx("free forever",
                                              note="Legal asked us to stop."))
    assert out[0].remedy == "Legal asked us to stop."


def test_a_note_is_optional_and_the_remedy_is_still_useful():
    """Forcing a reason produces 'because I said so'. A finding with no
    remedy is a complaint, so the fallback restates the action."""
    out = findings({"h": "free forever"}, ctx("free forever"))
    assert out[0].remedy
    assert "free forever" in out[0].remedy


def test_every_finding_has_a_remedy():
    out = findings({"a": "free forever", "b": "guaranteed leads"},
                   ctx("free forever", "guaranteed leads"))
    assert len(out) == 2
    for f in out:
        assert f.remedy, f.field_name
