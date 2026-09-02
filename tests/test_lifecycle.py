"""Tests for stage 9: campaign status transitions and the approval gate.

approval_readiness is pure, which is the point -- the UI renders its findings
as a checklist and lifecycle.promote enforces them, so a single function means
the button and the guard cannot disagree. Everything below exercises it
directly.

The transition tables are data, and the properties worth pinning are the ones
that would be silently wrong: that 'approved' has exactly one legal
predecessor, that nothing automatic can move a campaign backwards, and that
nothing automatic can move it out of a state a person put it in.
"""

from __future__ import annotations

import pytest

import lifecycle
from lifecycle import AUTO_BAND, FROZEN, MANUAL_MOVES, ApprovalFacts
from lifecycle import approval_readiness as ready


def facts(**kw) -> ApprovalFacts:
    """A campaign that IS ready, unless a kwarg spoils it."""
    base = {
        "slots": {("email", "email", "A", 1): True,
                  ("meta_ads", "meta_ad", "A", None): True},
        "promised_channels": ("email", "meta_ads"),
        "approved_channels": frozenset({"email", "meta_ads"}),
        "open_revisions": (),
        "validation_status": "pass",
        "qa_blocked": (),
        "newer_pending": (),
    }
    base.update(kw)
    return ApprovalFacts(**base)


# --------------------------------------------------------------------------
# The gate: every asset approved
# --------------------------------------------------------------------------

def test_a_fully_approved_campaign_is_ready():
    assert ready(facts()) == []


def test_one_unapproved_slot_blocks_and_names_it():
    """The decision this stage turns on. 'Every asset approved' is per SLOT,
    not per row -- a slot holds several versions and 013's partial unique
    index allows exactly one approved among them."""
    blockers = ready(facts(
        slots={("email", "email", "A", 1): True,
               ("email", "email", "A", 2): False,
               ("meta_ads", "meta_ad", "A", None): True}))
    assert [b.check for b in blockers] == ["assets_not_approved"]
    assert "email/A/2" in blockers[0].message
    assert "1 of 3" in blockers[0].message


def test_the_slot_labels_are_sorted_so_the_message_is_stable():
    blockers = ready(facts(
        slots={("meta_ads", "meta_ad", "B", None): False,
               ("email", "email", "A", 2): False,
               ("email", "email", "A", 1): False},
        approved_channels=frozenset()))
    msg = next(b.message for b in blockers
               if b.check == "assets_not_approved")
    assert "email/A/1, email/A/2, meta_ads/B" in msg


def test_no_assets_reports_once_and_stops():
    """Five findings that all mean "there are no assets" is noise. The early
    return is deliberate."""
    blockers = ready(facts(slots={}, approved_channels=frozenset()))
    assert [b.check for b in blockers] == ["no_assets"]


def test_a_promised_channel_with_no_approved_asset_blocks():
    """The failure this catches is easy to miss because what exists looks
    finished: approve the Meta ads, forget the emails, ship half a campaign."""
    blockers = ready(facts(
        slots={("meta_ads", "meta_ad", "A", None): True},
        approved_channels=frozenset({"meta_ads"})))
    checks = [b.check for b in blockers]
    assert "channel_not_covered" in checks
    msg = next(b.message for b in blockers
               if b.check == "channel_not_covered")
    assert "email" in msg


def test_the_channel_message_agrees_with_itself_grammatically():
    one = ready(facts(slots={("email", "email", "A", 1): True},
                      approved_channels=frozenset({"email"})))
    msg = next(b.message for b in one if b.check == "channel_not_covered")
    assert "exists for it." in msg

    two = ready(facts(promised_channels=("email", "meta_ads", "sms"),
                      approved_channels=frozenset({"email"}),
                      slots={("email", "email", "A", 1): True}))
    msg = next(b.message for b in two if b.check == "channel_not_covered")
    assert "exists for them." in msg


def test_an_open_edit_request_blocks_approval():
    """Someone asked for a change and has not got it. Approving the campaign
    over the top strands their feedback against published copy."""
    blockers = ready(facts(open_revisions=("meta_ads/A",)))
    assert [b.check for b in blockers] == ["open_revision"]


def test_many_open_requests_are_truncated_not_dumped():
    blockers = ready(facts(open_revisions=tuple(f"a{i}" for i in range(9))))
    msg = blockers[0].message
    assert "9 edit request(s)" in msg
    assert "and more" in msg
    assert "a8" not in msg


def test_a_qa_blocked_asset_blocks_approval():
    blockers = ready(facts(qa_blocked=("meta_ads/A",)))
    assert [b.check for b in blockers] == ["qa_blocked"]


def test_a_blocked_brief_blocks_approval():
    """Should be unreachable -- assets cannot be approved under a blocked
    brief -- but a campaign approval is the wrong place to discover that
    assumption was wrong."""
    blockers = ready(facts(validation_status="blocked"))
    assert [b.check for b in blockers] == ["brief_blocked"]


def test_a_warning_validation_does_not_block():
    """Warnings are truthful findings, not blockers, and the asset-level
    approval already required someone to read them."""
    assert ready(facts(validation_status="warning")) == []


def test_blockers_come_back_in_a_fixable_order():
    """All of them at once, so a reviewer sees the whole list rather than
    discovering the next one after fixing the last."""
    blockers = ready(facts(
        slots={("email", "email", "A", 1): False},
        approved_channels=frozenset(),
        newer_pending=("email/A/1 (v2 live, v4 pending)",),
        open_revisions=("x",), qa_blocked=("y",),
        validation_status="blocked"))
    assert [b.check for b in blockers] == [
        "assets_not_approved", "newer_version_pending",
        "channel_not_covered", "open_revision", "qa_blocked",
        "brief_blocked"]


def test_every_blocker_carries_a_remedy():
    """A finding that says what is wrong but not what to do is a complaint."""
    all_of_them = ready(facts(
        slots={("email", "email", "A", 1): False},
        approved_channels=frozenset(),
        newer_pending=("email/A/1 (v2 live, v4 pending)",),
        open_revisions=("x",), qa_blocked=("y",),
        validation_status="blocked")) + ready(facts(slots={}))
    for b in all_of_them:
        assert b.remedy, b.check


def test_blocker_as_dict_omits_an_empty_remedy():
    assert lifecycle.Blocker("c", "m").as_dict() == {"check": "c",
                                                     "message": "m"}
    assert "remedy" in lifecycle.Blocker("c", "m", "r").as_dict()


# --------------------------------------------------------------------------
# Slot labels
# --------------------------------------------------------------------------

@pytest.mark.parametrize("slot,expected", [
    (("email", "email", "A", 1), "email/A/1"),
    (("email", "email", "A", 3), "email/A/3"),
    (("meta_ads", "meta_ad", "B", None), "meta_ads/B"),
])
def test_slot_label(slot, expected):
    """A position of None must not render as 'meta_ads/B/None', and a position
    of 0 must not vanish -- `if position is not None`, not `if position`."""
    assert lifecycle.slot_label(slot) == expected


def test_position_zero_is_not_treated_as_absent():
    assert lifecycle.slot_label(("email", "email", "A", 0)) == "email/A/0"


# --------------------------------------------------------------------------
# The transition tables
# --------------------------------------------------------------------------

def test_approved_has_exactly_one_legal_predecessor():
    """'review' and nothing else. If a second route to 'approved' appears, the
    gate above stops being the only way in."""
    sources = [s for s, targets in MANUAL_MOVES.items()
               if "approved" in targets]
    assert sources == ["review"]


def test_live_requires_approved_first():
    sources = [s for s, targets in MANUAL_MOVES.items() if "live" in targets]
    assert sources == ["approved"]


def test_everything_can_be_archived_except_archived_itself():
    for status, targets in MANUAL_MOVES.items():
        assert "archived" in targets, status
    assert "archived" not in MANUAL_MOVES


def test_the_automatic_band_and_the_manual_states_do_not_overlap():
    """A status is either inferred or decided. One that was both would let
    advance() undo a person's decision."""
    manual_targets = {t for v in MANUAL_MOVES.values() for t in v}
    assert not (set(AUTO_BAND) & manual_targets)


def test_nothing_automatic_escapes_a_frozen_status():
    assert FROZEN == {"approved", "live", "completed", "archived"}
    assert not (FROZEN & set(AUTO_BAND))


def test_validating_is_unreachable_on_purpose():
    """It only means something if validation runs out of band. Today it runs
    inside the request, so setting it would be theatre -- and 021's header
    says so rather than quietly omitting it."""
    assert "validating" not in AUTO_BAND
    assert "validating" not in {t for v in MANUAL_MOVES.values() for t in v}
    assert "validating" in lifecycle.VALIDATION_STATES


def test_every_manual_source_is_a_real_campaign_status():
    """The vocabulary is duplicated in 021's CHECK, so a typo here would only
    surface as a constraint violation at transition time."""
    valid = {"draft", "validating", "blocked", "needs_info", "validated",
             "strategy", "production", "review", "approved", "live",
             "completed", "archived"}
    for source, targets in MANUAL_MOVES.items():
        assert source in valid, source
        for target in targets:
            assert target in valid, target


def test_the_auto_band_is_ordered_and_starts_at_draft():
    """advance() compares list positions, so the order IS the semantics."""
    assert AUTO_BAND[0] == "draft"
    assert AUTO_BAND[-1] == "review"
    assert len(set(AUTO_BAND)) == len(AUTO_BAND)


def test_review_is_where_the_automatic_band_hands_over():
    """The last inferred status must be the one a person promotes FROM,
    otherwise there is a gap no transition covers."""
    assert AUTO_BAND[-1] in MANUAL_MOVES
    assert "approved" in MANUAL_MOVES[AUTO_BAND[-1]]


# --------------------------------------------------------------------------
# An approved slot is not necessarily a settled one
# --------------------------------------------------------------------------

def test_a_newer_pending_version_blocks_campaign_approval():
    """Somebody asked for an edit, got v4, and approved v2 -- or never came
    back to v4 at all. Signing off now publishes v2 and silently abandons a
    revision that was requested on purpose."""
    blockers = ready(facts(
        newer_pending=("meta_ads/B (v2 live, v4 pending)",)))
    assert [b.check for b in blockers] == ["newer_version_pending"]


def test_the_newer_version_blocker_names_the_slot_and_both_versions():
    """A reviewer has to be able to find it. 'a slot has a newer version' is
    not a location."""
    blockers = ready(facts(
        newer_pending=("meta_ads/B (v2 live, v4 pending)",)))
    assert "meta_ads/B" in blockers[0].message
    assert "v2 live, v4 pending" in blockers[0].message


def test_many_newer_versions_are_truncated_not_dumped():
    blockers = ready(facts(
        newer_pending=tuple(f"slot{i} (v1 live, v2 pending)"
                            for i in range(9))))
    assert "9 slot(s)" in blockers[0].message
    assert "and more" in blockers[0].message
    assert "slot8" not in blockers[0].message


def test_the_remedy_offers_both_ways_out():
    """Approving the newer version and rejecting it are both legitimate --
    the reviewer may well prefer the copy that is already live. What is not
    legitimate is leaving it undecided."""
    blocker = ready(facts(
        newer_pending=("meta_ads/B (v2 live, v4 pending)",)))[0]
    assert "Approve the newer version" in blocker.remedy
    assert "reject" in blocker.remedy


def test_a_slot_with_no_newer_version_does_not_block():
    assert ready(facts(newer_pending=())) == []


def test_an_unapproved_slot_and_a_newer_version_are_separate_findings():
    """Different actions: one slot needs any approval, the other needs a
    decision about a specific version. Collapsing them would hide one."""
    blockers = ready(facts(
        slots={("email", "email", "A", 1): False,
               ("meta_ads", "meta_ad", "B", None): True},
        approved_channels=frozenset({"meta_ads"}),
        promised_channels=("meta_ads",),
        newer_pending=("meta_ads/B (v2 live, v4 pending)",)))
    assert [b.check for b in blockers] == ["assets_not_approved",
                                           "newer_version_pending"]
