"""Trigger-phrase matching, and the safety property migration 014 promises.

Split from test_validation_checks.py because the collision test is the one
check in this suite that genuinely needs the live claims table: it asserts that
no seeded trigger phrase matches any approved wording for the same product, and
the whole point is to catch a phrase somebody adds later. A fixture cannot do
that -- it would only prove the fixture agrees with itself.

The DB tests skip cleanly when Supabase is unreachable, so the suite still runs
offline. The pure tests above them cover the matching logic itself.
"""

from __future__ import annotations

import asyncio

import pytest

from validation.checks import (
    CheckContext,
    check_prohibited_wording,
    contains_phrase,
)

CAMPAIGN = "Franchise Recruitment - Captive Agents Q4 2026"


def claim(status: str, text: str, phrases: list[str], **kw) -> dict:
    row = {"status": status, "claim_text": text, "approved_wording": None,
           "category": "company", "requires_disclaimer": False,
           "disclaimer_text": None, "restriction_notes": "do not",
           "trigger_phrases": phrases}
    row.update(kw)
    return row


# --------------------------------------------------------------------------
# Pure: the matching logic
# --------------------------------------------------------------------------

PROHIBITED = claim("prohibited", "Renegade is licensed in all 50 states.",
                   ["all 50 states", "licensed nationwide"])
RESTRICTED = claim("restricted", "Renegade holds a 4.7 out of 5 Google rating.",
                   ["4.7 stars", "4.7 out of 5"])


def ctx(claims) -> CheckContext:
    return CheckContext(claims=claims)


def test_trigger_phrase_catches_a_paraphrase():
    """The hole 014 closes: the copy reproduces none of the claim sentence."""
    f = check_prohibited_wording({"headline": "Licensed in all 50 states"},
                                 ctx([PROHIBITED]))
    assert [x.severity for x in f] == ["blocker"]
    assert f[0].evidence == "all 50 states"


def test_trigger_phrase_is_case_insensitive():
    f = check_prohibited_wording({"headline": "ALL 50 STATES today"},
                                 ctx([PROHIBITED]))
    assert len(f) == 1


def test_trigger_phrase_tolerates_odd_whitespace():
    f = check_prohibited_wording({"body": "all  50\nstates"},
                                 ctx([PROHIBITED]))
    assert len(f) == 1


def test_trigger_phrase_respects_word_boundaries():
    """'all 50 statesman' is not the claim."""
    assert check_prohibited_wording({"body": "all 50 statesmanship"},
                                    ctx([PROHIBITED])) == []


def test_restricted_claim_use_is_a_warning_not_a_blocker():
    """A restricted claim is usable when its condition is met, and only a
    human can confirm that -- so it surfaces the condition rather than
    blocking."""
    f = check_prohibited_wording({"headline": "Rated 4.7 stars"},
                                 ctx([RESTRICTED]))
    assert [x.severity for x in f] == ["warning"]
    assert [x.check for x in f] == ["restricted_claim_used"]
    assert f[0].remedy == "do not"


def test_claim_with_no_trigger_phrases_still_matches_its_figures():
    """014 added phrases; it did not remove the money matcher."""
    c = claim("prohibited", "The fee is $20,000.", [])
    f = check_prohibited_wording({"body": "just $20,000"}, ctx([c]))
    assert [x.severity for x in f] == ["blocker"]


def test_approved_claims_are_never_matched_on_their_phrases():
    """trigger_phrases is meaningless on an approved row and must be ignored,
    or an approved claim would flag its own wording."""
    c = claim("approved", "Fees start at $25,000.", ["$25,000"],
              approved_wording="Fees start at $25,000.")
    assert check_prohibited_wording({"body": "Fees start at $25,000."},
                                    ctx([c])) == []


def test_the_unscoped_commission_phrase_does_not_hit_the_approved_wording():
    """The specific collision 014's seed had to avoid.

    Prohibiting '80% commission' outright would block the campaign's strongest
    legitimate line, so the phrase targets the UNSCOPED form only.
    """
    prohibited = claim(
        "prohibited",
        "Franchise owners earn 80% commission on new business, and Renegade "
        "runs the back office.",
        ["80% commission on new business"])
    approved_wording = ("Franchise owners earn 80% on new business personal "
                        "lines commissions and up to 80% on renewals.")
    assert not contains_phrase(approved_wording,
                               "80% commission on new business")
    assert check_prohibited_wording({"body": approved_wording},
                                    ctx([prohibited])) == []


# --------------------------------------------------------------------------
# Live: the safety property 014's comment promises
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def live_claims(run_db):
    """Claims for the test campaign's product, or skip.

    Skips rather than fails when the database is unreachable so the offline
    suite stays green; a CI run with credentials gets the real check.
    """
    from validation import context as vctx

    async def load():
        _, ctx_obj = await vctx.load(CAMPAIGN)
        return ctx_obj.claims

    return run_db(load())


@pytest.mark.dbtest
def test_trigger_phrases_never_match_approved_copy(live_claims):
    """THE safety property. A trigger phrase that matches an approved wording
    would block compliant copy, and a false-positive blocker teaches people to
    override the checker.

    This is why 014's phrases are hand-written rather than derived: any
    fuzzy-matching scheme loose enough to catch 'Industry-leading commissions'
    also flags the approved 80% wording.
    """
    approved = [c for c in live_claims if c["status"] == "approved"]
    assert approved, "no approved claims to check against"

    collisions = []
    for c in live_claims:
        if c["status"] == "approved":
            continue
        for phrase in c.get("trigger_phrases") or []:
            for a in approved:
                wording = a["approved_wording"] or a["claim_text"]
                if contains_phrase(wording, phrase):
                    collisions.append((phrase, wording))
    assert not collisions, (
        "trigger phrases collide with approved wordings:\n" +
        "\n".join(f"  {p!r} matches {w!r}" for p, w in collisions))


@pytest.mark.dbtest
def test_every_approved_wording_passes_its_own_product_checks(live_claims):
    """Belt and braces on the same property, through the real check rather
    than through contains_phrase directly."""
    ctx_obj = CheckContext(claims=live_claims)
    for c in live_claims:
        if c["status"] != "approved":
            continue
        wording = c["approved_wording"] or c["claim_text"]
        findings = check_prohibited_wording({"body": wording}, ctx_obj)
        blockers = [f for f in findings if f.severity == "blocker"]
        assert not blockers, (
            f"approved wording flagged as prohibited: {wording!r} "
            f"(trigger {blockers[0].evidence!r})")


@pytest.mark.dbtest
def test_prohibited_claims_have_trigger_phrases(live_claims):
    """A prohibited claim with no phrases and no figure is unenforceable --
    it can only be caught by verbatim reproduction of its whole sentence."""
    from validation.checks import money_amounts

    unenforceable = [
        c["claim_text"] for c in live_claims
        if c["status"] == "prohibited"
        and not (c.get("trigger_phrases") or [])
        and not money_amounts(c["claim_text"])
    ]
    assert not unenforceable, (
        "prohibited claims that cannot be detected:\n" +
        "\n".join(f"  {t}" for t in unenforceable))
