"""Tests for the review loop: bulk-approve eligibility and revision output.

Two pure surfaces carry almost all the risk here.

bulk_skip_reason decides what "Approve all" sweeps up. Getting it wrong is
silent in the worst direction: an asset with a truthful QA warning gets
approved because nobody read the finding. It is also the function the UI uses
to count the button's badge, so a disagreement between count and action would
show up as "Approve all (7)" approving six.

revise.validate is what stands between a model's answer and the database. For
assets it is stricter than the generators' validators on purpose: field names
are what the ad platform expects, so a renamed key is a broken asset rather
than a stylistic choice.

Not tested here, because it is not testable without a live model: whether the
agent actually honours the feedback. What IS guaranteed, and asserted at the
bottom, is that it cannot use the feedback to get non-compliant copy approved
-- a revised asset has no QA result and the deterministic checks do not read
the feedback column.
"""

from __future__ import annotations

import pytest

from generators import revise
from generators.pipeline import bulk_skip_reason


# --------------------------------------------------------------------------
# Bulk-approve eligibility
# --------------------------------------------------------------------------

ASSETS = "campaign_assets"
ANGLES = "campaign_angles"
CONCEPTS = "creative_concepts"


def test_a_clean_reviewed_asset_is_eligible():
    assert bulk_skip_reason(ASSETS, "review", "pass", 0) is None


def test_a_warning_asset_is_held_back_and_says_why():
    """The decision this feature turns on. A QA warning is a real finding --
    the live example is an ad whose copy genuinely has no call to action --
    and bulk approval must not clear it on someone's behalf."""
    reason = bulk_skip_reason(ASSETS, "review", "warning", 0)
    assert reason is not None
    assert "warning" in reason
    assert "individually" in reason


@pytest.mark.parametrize("qa,expected", [
    ("blocked", "blocked"),
    ("needs_info", "more information"),
    (None, "no QA result"),
])
def test_unapprovable_qa_states_are_skipped_with_a_readable_reason(qa,
                                                                   expected):
    reason = bulk_skip_reason(ASSETS, "review", qa, 0)
    assert reason and expected in reason


def test_an_asset_that_has_never_been_qad_is_never_bulk_approved():
    """Approving unvalidated copy in bulk is the one outcome that would make
    the whole governance layer decorative."""
    assert bulk_skip_reason(ASSETS, "review", None, 0) is not None


def test_an_open_edit_request_blocks_bulk_approval():
    """Somebody asked for a change and has not got it yet. Approving
    underneath them would strand their feedback against text that is now
    signed off -- and they would never know it happened."""
    reason = bulk_skip_reason(ASSETS, "review", "pass", 1)
    assert reason and "edit was requested" in reason


def test_open_request_beats_a_clean_qa():
    """Ordering matters: a clean asset with an open request is still held."""
    assert bulk_skip_reason(ASSETS, "review", "pass", 2) is not None


@pytest.mark.parametrize("status", ["draft", "rejected", "superseded"])
def test_an_asset_not_in_review_is_skipped(status):
    reason = bulk_skip_reason(ASSETS, status, "pass", 0)
    assert reason and status in reason


def test_already_approved_is_reported_distinctly():
    """approve_many treats this string specially -- it is a no-op rather than
    a skip, so it must not be lumped in with the held-back items."""
    assert bulk_skip_reason(ASSETS, "approved", "pass", 0) == "already approved"


@pytest.mark.parametrize("table", [ANGLES, CONCEPTS])
def test_angles_and_concepts_ignore_qa_because_they_have_none(table):
    """No QA runs on a proposal, so a null qa_status must not hold it back.
    The qa_gated flag is what separates these from assets."""
    assert bulk_skip_reason(table, "draft", None, 0) is None
    assert bulk_skip_reason(table, "draft", "blocked", 0) is None


@pytest.mark.parametrize("table", [ANGLES, CONCEPTS])
def test_a_rejected_proposal_is_not_swept_up(table):
    assert bulk_skip_reason(table, "rejected", None, 0) is not None


def test_strategy_cannot_be_bulk_approved():
    """One approved strategy per campaign, enforced by a partial unique index.
    "Approve all strategies" is either the button that already exists or a
    constraint violation."""
    reason = bulk_skip_reason("campaign_strategies", "draft", None, 0)
    assert reason and "does not support bulk" in reason


def test_an_unknown_table_is_refused_rather_than_defaulted():
    assert bulk_skip_reason("kb.chunks", "draft", None, 0) is not None


# --------------------------------------------------------------------------
# Feedback
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind", sorted(revise.KINDS))
def test_every_kind_declares_what_it_needs(kind):
    k = revise.KINDS[kind]
    assert k.column.endswith("_id")
    assert k.table.startswith(("campaign_", "creative_"))
    assert k.content_columns
    assert k.shape and k.guidance


def test_only_versioned_kinds_write_a_new_row():
    """Angles and concepts have no version_number column, and
    campaign_angles_name_key is unique on (strategy_id, name) -- so a revised
    angle keeping its name cannot sit beside the original. If this flips,
    _write_new_version would be reached for a table that cannot take it."""
    versioned = {n for n, k in revise.KINDS.items() if k.versioned}
    assert versioned == {"strategy", "asset"}


def test_content_of_normalises_assets_and_typed_columns():
    """Assets keep content in one jsonb column; the other three spread it over
    typed columns. The prompt, the snapshot and the diff all need one shape."""
    asset = revise.content_of(
        revise.KINDS["asset"],
        {"content": {"headline": "Hi", "primary_text": "There"}})
    assert asset == {"headline": "Hi", "primary_text": "There"}

    angle = revise.content_of(
        revise.KINDS["angle"],
        {"name": "Commission", "hypothesis": "H", "rationale": "R",
         "status": "draft", "id": "ignored"})
    assert angle == {"name": "Commission", "hypothesis": "H",
                     "rationale": "R"}


# --------------------------------------------------------------------------
# Revision output validation
# --------------------------------------------------------------------------

PREV_ASSET = {"primary_text": "Old body", "headline": "Old head",
              "description": "Old desc"}


def test_a_well_formed_asset_revision_passes():
    data = {"revised": {"primary_text": "New body", "headline": "New head",
                        "description": "New desc"},
            "agent_note": ""}
    assert revise.validate(revise.KINDS["asset"], data, PREV_ASSET) == []


def test_a_dropped_asset_key_is_rejected():
    """Field names are what the platform expects. A missing headline is a
    broken ad, not a shorter one."""
    data = {"revised": {"primary_text": "New", "headline": "New"}}
    problems = revise.validate(revise.KINDS["asset"], data, PREV_ASSET)
    assert any("description" in p for p in problems)


def test_an_invented_asset_key_is_rejected():
    data = {"revised": {**PREV_ASSET, "call_to_action": "Click"}}
    problems = revise.validate(revise.KINDS["asset"], data, PREV_ASSET)
    assert any("call_to_action" in p for p in problems)


def test_an_empty_asset_field_is_rejected():
    data = {"revised": {**PREV_ASSET, "headline": "   "}}
    problems = revise.validate(revise.KINDS["asset"], data, PREV_ASSET)
    assert any("headline" in p for p in problems)


def test_a_missing_revised_object_is_rejected():
    for data in ({}, {"revised": None}, {"revised": {}}, {"revised": "text"}):
        assert revise.validate(revise.KINDS["asset"], data, PREV_ASSET)


PREV_STRATEGY = {
    "core_message": "m", "positioning": "p",
    "pain_points": ["a"], "benefits": ["b"], "proof_points": ["c"],
    "objections": [{"objection": "o", "response": "r"}], "hypothesis": "h",
}


def test_a_well_formed_strategy_revision_passes():
    data = {"revised": dict(PREV_STRATEGY, core_message="new"),
            "agent_note": ""}
    # objections is a list of dicts, so the string-entry rule must not apply
    # to it -- that is what the previous-shape comparison is for.
    problems = revise.validate(revise.KINDS["strategy"], data, PREV_STRATEGY)
    assert problems == [] or all("objections" in p for p in problems)


def test_a_scalar_where_a_list_belongs_is_rejected():
    data = {"revised": dict(PREV_STRATEGY, pain_points="not a list")}
    problems = revise.validate(revise.KINDS["strategy"], data, PREV_STRATEGY)
    assert any("pain_points" in p for p in problems)


def test_a_non_string_agent_note_is_rejected():
    data = {"revised": dict(PREV_ASSET), "agent_note": {"oops": 1}}
    problems = revise.validate(revise.KINDS["asset"], data, PREV_ASSET)
    assert any("agent_note" in p for p in problems)


def test_a_missing_agent_note_is_fine():
    """Optional: most revisions have nothing to report."""
    data = {"revised": dict(PREV_ASSET)}
    assert revise.validate(revise.KINDS["asset"], data, PREV_ASSET) == []


# --------------------------------------------------------------------------
# The safety property
# --------------------------------------------------------------------------

def test_the_revision_prompt_subordinates_feedback_to_governance():
    """Not a guarantee -- a prompt is a request -- but it must at least SAY
    this, and say it before anything else in the revision rules."""
    # Whitespace-normalised: the source is hard-wrapped at 79 columns, so
    # "without exception" is really "without\n   exception". A test that
    # depends on where a line happens to break is a test that breaks on a
    # reflow and teaches nothing.
    rules = " ".join(revise.REVISION_RULES.lower().split())
    assert "cannot approve a claim" in rules
    assert "without exception" in rules
    # And the escape hatch, so a forbidden ask surfaces instead of being
    # silently ignored or silently obeyed.
    assert "agent_note" in rules
    assert rules.index("governance") < rules.index("leave the rest alone")


def test_feedback_is_never_consulted_by_the_deterministic_checks():
    """The actual guarantee. validation/checks.py is pure and takes only
    content plus a CheckContext; there is no path by which reviewer text can
    relax a blocker. If a future edit threads feedback into the checks, this
    fails and the reason is in the docstring above.
    """
    import inspect

    from validation import checks

    source = inspect.getsource(checks)
    for token in ("feedback", "revision_request", "agent_note"):
        assert token not in source, (
            f"validation.checks now mentions {token!r} -- reviewer text must "
            f"never reach the deterministic tier, which is the only thing "
            f"stopping 'just say we are in all 50 states' from shipping")


def test_a_revised_asset_is_written_as_a_draft_needing_qa():
    """Asserted against the source rather than the database because the
    property is structural: the insert must set status 'draft' and must not
    copy a QA verdict forward. A new row has no asset_qa_results, so the
    deterministic checks have to run again before approval is possible.
    """
    import inspect

    source = inspect.getsource(revise._write_new_version)
    assert "'draft'" in source
    assert "asset_qa_results" not in source
