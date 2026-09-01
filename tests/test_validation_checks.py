"""Tests for the deterministic checks.

These are the checks that must never produce a false positive, because a
blocker that fires wrongly teaches people to override the checker -- and an
overridden checker catches nothing. So the negative cases here matter at least
as much as the positive ones, and several exist specifically because an earlier
draft of checks.py got them wrong:

  - substring matching flagged "no earnout" inside unrelated text
  - "$20,000" matched inside "$20,000,000"
  - curly quotes let a prohibited phrase through

No database, no model, no network.
"""

from __future__ import annotations

import pytest

from validation.checks import (
    CheckContext,
    check_campaign_type_mixing,
    check_character_limits,
    check_cta_consistency,
    check_missing_brief_fields,
    check_price_consistency,
    check_prohibited_wording,
    check_unavailable_features,
    contains_phrase,
    money_amounts,
    normalise,
    split,
    status_for,
)


def claim(status: str, text: str, **kw) -> dict:
    row = {"status": status, "claim_text": text, "approved_wording": None,
           "category": "pricing", "requires_disclaimer": False,
           "disclaimer_text": None, "restriction_notes": None}
    row.update(kw)
    return row


APPROVED_FEE = claim(
    "approved", "Initial Renegade franchise fee starts at $25,000.",
    approved_wording="Initial franchise fees start at $25,000 depending on "
                     "your business type.")
PROHIBITED_FEE = claim(
    "prohibited", "Initial Renegade franchise fee starts at $20,000.",
    restriction_notes="Superseded. Never use in any channel.")


def ctx(**kw) -> CheckContext:
    base = {"claims": [APPROVED_FEE, PROHIBITED_FEE], "features": [],
            "campaign": {}, "prohibited_themes": [], "allowed_themes": [],
            "stale_values": []}
    base.update(kw)
    return CheckContext(**base)


# --------------------------------------------------------------------------
# Text primitives
# --------------------------------------------------------------------------

def test_normalise_folds_curly_quotes_and_odd_spaces():
    assert normalise("don’t stop") == "don't stop"
    assert normalise("a—b") == "a-b"


@pytest.mark.parametrize("text,needle,expected", [
    ("we offer cash at close", "cash at close", True),
    ("cash  at\nclose today", "cash at close", True),   # any whitespace run
    ("the existing book", "exit", False),               # substring, not word
    ("learnout period", "no earnout", False),           # the original bug
    ("no earnout, ever", "no earnout", True),
    ("Sell Your Agency now", "sell your agency", True),  # case-insensitive
    ("don’t sell your agency", "sell your agency", True),
])
def test_contains_phrase(text, needle, expected):
    assert contains_phrase(text, needle) is expected


def test_contains_phrase_handles_punctuation_edges():
    """\\b never matches before '$', so anchoring must be conditional."""
    assert contains_phrase("fees are $25,000 today", "$25,000") is True


@pytest.mark.parametrize("text,expected", [
    ("$25,000", {25000.0}),
    ("$ 25,000", {25000.0}),
    ("$25000", {25000.0}),
    ("$20,000,000 raised", {20000000.0}),   # NOT 20000
    ("from $25,000 to $40,000", {25000.0, 40000.0}),
    ("no money here", set()),
])
def test_money_amounts(text, expected):
    assert money_amounts(text) == expected


def test_twenty_million_is_not_twenty_thousand():
    """The bug this exists for: '$20,000,000' must not read as the prohibited
    '$20,000'."""
    assert 20000.0 not in money_amounts("a $20,000,000 book of business")


# --------------------------------------------------------------------------
# 1. Prohibited wording
# --------------------------------------------------------------------------

def test_prohibited_figure_is_blocked():
    f = check_prohibited_wording(
        {"headline": "Own an agency for $20,000"}, ctx())
    assert [x.severity for x in f] == ["blocker"]
    assert f[0].field_name == "headline"
    assert "20,000" in f[0].evidence
    assert "Never use" in (f[0].remedy or "")


def test_approved_figure_is_not_blocked():
    assert check_prohibited_wording(
        {"headline": "Fees start at $25,000"}, ctx()) == []


def test_prohibited_check_reports_the_field():
    f = check_prohibited_wording(
        {"headline": "clean", "primary_text": "just $20,000"}, ctx())
    assert len(f) == 1
    assert f[0].field_name == "primary_text"


def test_same_problem_twice_in_one_field_is_reported_once():
    f = check_prohibited_wording(
        {"body": "$20,000 today, $20,000 tomorrow"}, ctx())
    assert len(f) == 1


def test_stale_value_from_conflicts_is_blocked():
    f = check_prohibited_wording(
        {"body": "the fee is $20,000"},
        ctx(claims=[APPROVED_FEE], stale_values=["$20,000"]))
    assert any(x.check == "stale_figure" for x in f)


def test_stale_non_money_value_is_matched_as_a_phrase():
    f = check_prohibited_wording(
        {"body": "quotes from 100 insurance companies"},
        ctx(claims=[], stale_values=["100 insurance companies"]))
    assert [x.check for x in f] == ["stale_figure"]


def test_no_findings_on_empty_content():
    assert check_prohibited_wording({}, ctx()) == []


# --------------------------------------------------------------------------
# 2. Price consistency
# --------------------------------------------------------------------------

def test_two_assets_with_different_prices_is_blocked():
    f = check_price_consistency(
        [{"headline": "from $25,000"}, {"headline": "from $30,000"}], ctx())
    assert [x.severity for x in f] == ["blocker"]
    assert "30,000" in f[0].evidence


def test_consistent_approved_price_passes():
    assert check_price_consistency(
        [{"headline": "from $25,000"}, {"body": "$25,000 to start"}],
        ctx()) == []


def test_price_with_no_approved_pricing_claim_is_blocked():
    f = check_price_consistency([{"headline": "only $9,999"}],
                                ctx(claims=[]))
    assert [x.severity for x in f] == ["blocker"]
    assert "no approved pricing claim" in f[0].message


def test_no_price_anywhere_passes():
    assert check_price_consistency([{"headline": "own an agency"}],
                                    ctx(claims=[])) == []


# --------------------------------------------------------------------------
# 3. CTA consistency
# --------------------------------------------------------------------------

CAMPAIGN = {"primary_cta": "Talk to a Franchise Success Specialist",
            "secondary_cta": "Download the franchise overview"}


def test_cta_matching_on_intent_not_wording():
    """A 30-char description cannot hold the full CTA; sharing its action is
    the requirement."""
    assert check_cta_consistency(
        {"body": "Talk to a specialist today"},
        ctx(campaign=CAMPAIGN)) == []


def test_secondary_cta_also_satisfies():
    assert check_cta_consistency(
        {"body": "Download the overview"}, ctx(campaign=CAMPAIGN)) == []


def test_absent_cta_is_a_warning_not_a_blocker():
    f = check_cta_consistency({"body": "Insurance is important."},
                              ctx(campaign=CAMPAIGN))
    assert [x.severity for x in f] == ["warning"]


def test_no_cta_in_brief_means_no_check():
    assert check_cta_consistency({"body": "anything"},
                                  ctx(campaign={})) == []


def test_stopwords_alone_do_not_satisfy_the_cta():
    """'to a the' shares only stopwords with the CTA and must not pass."""
    f = check_cta_consistency({"body": "to a the with your"},
                              ctx(campaign=CAMPAIGN))
    assert [x.severity for x in f] == ["warning"]


# --------------------------------------------------------------------------
# 4. Features
# --------------------------------------------------------------------------

UNAVAILABLE = {"name": "Territory protection", "available": False,
               "approved_for_marketing": False,
               "restrictions": "Do not claim exclusive territory."}
UNAPPROVED = {"name": "Agency value calculator", "available": True,
              "approved_for_marketing": False,
              "marketing_notes": "Phase 2."}
FINE = {"name": "Commission split", "available": True,
        "approved_for_marketing": True}


def test_unavailable_feature_is_a_blocker():
    f = check_unavailable_features(
        {"body": "Includes Territory protection"},
        ctx(features=[UNAVAILABLE]))
    assert [x.severity for x in f] == ["blocker"]
    assert "exclusive territory" in (f[0].remedy or "")


def test_available_but_unapproved_feature_is_a_warning():
    """Different severity on purpose: the fix may be to approve the feature."""
    f = check_unavailable_features(
        {"body": "Try our Agency value calculator"},
        ctx(features=[UNAPPROVED]))
    assert [x.severity for x in f] == ["warning"]


def test_approved_feature_passes():
    assert check_unavailable_features({"body": "Commission split of 80%"},
                                       ctx(features=[FINE])) == []


def test_feature_not_mentioned_is_not_flagged():
    assert check_unavailable_features({"body": "unrelated copy"},
                                       ctx(features=[UNAVAILABLE])) == []


# --------------------------------------------------------------------------
# 5. Campaign-type mixing
# --------------------------------------------------------------------------

MIXING = ["sell your agency", "cash at close", "no earnout"]


def test_prohibited_theme_is_blocked():
    f = check_campaign_type_mixing(
        {"body": "Own an agency, or sell your agency to us"},
        ctx(prohibited_themes=MIXING))
    assert [x.severity for x in f] == ["blocker"]
    assert f[0].evidence == "sell your agency"


def test_clean_franchise_copy_passes_the_mixing_check():
    assert check_campaign_type_mixing(
        {"body": "Own an independent agency and earn 80% on new business."},
        ctx(prohibited_themes=MIXING)) == []


def test_mixing_check_is_not_fooled_by_substrings():
    """'existing' contains 'exit'; 'learnout' contains 'earnout'."""
    assert check_campaign_type_mixing(
        {"body": "bring your existing book; the learnout is quick"},
        ctx(prohibited_themes=["exit", "earnout"])) == []


def test_no_themes_configured_means_no_check():
    assert check_campaign_type_mixing({"body": "sell your agency"},
                                       ctx(prohibited_themes=[])) == []


# --------------------------------------------------------------------------
# 6. Brief fields
# --------------------------------------------------------------------------

FULL_BRIEF = {
    "name": "X", "objective": "X", "target_audience": "X",
    "customer_problem": "X", "primary_benefit": "X", "primary_cta": "X",
    "primary_kpi": "X", "channels": ["email"], "offer": "X",
    "proof_points": ["X"],
}


def test_complete_brief_with_approved_claims_passes():
    assert check_missing_brief_fields(
        ctx(campaign=FULL_BRIEF, claims=[APPROVED_FEE])) == []


def test_missing_required_field_is_blocked():
    brief = dict(FULL_BRIEF, objective="")
    f = check_missing_brief_fields(ctx(campaign=brief, claims=[APPROVED_FEE]))
    assert [x.field_name for x in f] == ["objective"]
    assert f[0].severity == "blocker"


def test_whitespace_only_field_counts_as_empty():
    brief = dict(FULL_BRIEF, primary_benefit="   ")
    f = check_missing_brief_fields(ctx(campaign=brief, claims=[APPROVED_FEE]))
    assert any(x.field_name == "primary_benefit" for x in f)


def test_channel_specific_requirement():
    """email requires an offer -- a nurture sequence needs something to
    nurture toward."""
    brief = dict(FULL_BRIEF, offer=None)
    f = check_missing_brief_fields(ctx(campaign=brief, claims=[APPROVED_FEE]))
    assert any(x.field_name == "offer" for x in f)


def test_no_channels_is_blocked():
    brief = dict(FULL_BRIEF, channels=[])
    f = check_missing_brief_fields(ctx(campaign=brief, claims=[APPROVED_FEE]))
    assert any(x.field_name == "channels" for x in f)


def test_zero_approved_claims_is_itself_a_blocker():
    """A brief that validates cleanly against no governance has not been
    checked. Silence must not read as a pass."""
    f = check_missing_brief_fields(
        ctx(campaign=FULL_BRIEF, claims=[PROHIBITED_FEE]))
    assert any(x.check == "no_approved_claims" and x.severity == "blocker"
               for x in f)


def test_missing_proof_points_is_only_a_warning():
    brief = dict(FULL_BRIEF, proof_points=[])
    f = check_missing_brief_fields(ctx(campaign=brief, claims=[APPROVED_FEE]))
    assert [x.severity for x in f] == ["warning"]


# --------------------------------------------------------------------------
# 7. Character limits
# --------------------------------------------------------------------------

def test_over_limit_field_is_a_warning_with_the_overflow_quoted():
    f = check_character_limits({"headline": "x" * 50}, "meta_ads", "meta_ad")
    assert [x.severity for x in f] == ["warning"]
    assert f[0].field_name == "headline"
    assert "50 characters" in f[0].message


def test_within_limits_passes():
    assert check_character_limits(
        {"headline": "short", "primary_text": "also short",
         "description": "fine"}, "meta_ads", "meta_ad") == []


def test_unknown_channel_reports_info_rather_than_silently_passing():
    f = check_character_limits({"headline": "x" * 500}, "tiktok", "video")
    assert [x.severity for x in f] == ["info"]
    assert "No character limits defined" in f[0].message


def test_non_string_field_is_skipped_not_crashed():
    assert check_character_limits({"headline": None, "position": 3},
                                  "meta_ads", "meta_ad") == []


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

def test_status_precedence():
    from validation.checks import Finding
    blocker = Finding("c", "blocker", "m")
    warning = Finding("c", "warning", "m")
    info = Finding("c", "info", "m")
    assert status_for([]) == "pass"
    assert status_for([info]) == "pass"
    assert status_for([warning, info]) == "warning"
    assert status_for([warning, blocker]) == "blocked"


def test_split_buckets_and_orders_by_severity():
    from validation.checks import Finding
    out = split([Finding("z", "info", "i"), Finding("a", "blocker", "b"),
                 Finding("m", "warning", "w")])
    assert [f["check"] for f in out["blockers"]] == ["a"]
    assert [f["check"] for f in out["warnings"]] == ["m"]
    assert [f["check"] for f in out["recommendations"]] == ["z"]


def test_as_dict_omits_empty_optional_fields():
    from validation.checks import Finding
    d = Finding("c", "warning", "m").as_dict()
    assert set(d) == {"check", "severity", "message"}
