"""Bulk approval when a slot holds more than one pending version.

THE CASE
An edit request creates v4 while v2 is still sitting in review, so a slot
legitimately holds two candidates. Before newer_pending_versions existed,
Approve all treated both as eligible and approved them in turn -- no crash,
because approve() supersedes as it goes, but the surviving version was
whichever the loop reached last and the report claimed two approvals for one
live asset. The live data had this shape twice over.

The rule: within a slot, only the NEWEST pending version is a candidate. If it
is ineligible, the whole slot waits. Approving an older version over a newer
one is never what a reviewer means.
"""

from __future__ import annotations

import asyncio

import pytest

from generators import pipeline
from generators.pipeline import (bulk_skip_reason,
                                 newer_pending_versions)

SLOT = ("channel", "asset_type", "variant", "position")


def row(rid, version, *, status="review", channel="email", variant="A",
        position=1, asset_type="email"):
    return {"id": rid, "version_number": version, "status": status,
            "channel": channel, "asset_type": asset_type, "variant": variant,
            "position": position}


# --------------------------------------------------------------------------
# newer_pending_versions
# --------------------------------------------------------------------------

def test_the_only_pending_version_is_never_overtaken():
    assert newer_pending_versions([row("a", 1)], "review", SLOT) == {}


def test_the_older_of_two_is_overtaken_and_names_the_winner():
    overtaken = newer_pending_versions([row("a", 2), row("b", 4)],
                                       "review", SLOT)
    assert overtaken == {"a": 4}


def test_the_result_is_the_same_whatever_order_the_rows_arrive_in():
    """The bug was order-dependence, so this is the property that matters."""
    forward = newer_pending_versions([row("a", 2), row("b", 4)],
                                     "review", SLOT)
    backward = newer_pending_versions([row("b", 4), row("a", 2)],
                                      "review", SLOT)
    assert forward == backward == {"a": 4}


def test_three_versions_leave_only_the_newest_standing():
    overtaken = newer_pending_versions(
        [row("a", 1), row("b", 2), row("c", 5)], "review", SLOT)
    assert overtaken == {"a": 5, "b": 5}


def test_different_slots_do_not_compete():
    """meta_ads/A v3 must not overtake email/A/1 v1."""
    rows = [row("a", 1, channel="email", position=1),
            row("b", 3, channel="meta_ads", asset_type="meta_ad",
                position=None)]
    assert newer_pending_versions(rows, "review", SLOT) == {}


def test_positions_in_the_same_channel_are_different_slots():
    """An email sequence is N slots, not one. Step 3 v4 must not overtake
    step 2 v2."""
    rows = [row("a", 2, position=2), row("b", 4, position=3)]
    assert newer_pending_versions(rows, "review", SLOT) == {}


def test_a_null_position_is_a_slot_key_like_any_other():
    rows = [row("a", 1, channel="meta_ads", asset_type="meta_ad",
                position=None),
            row("b", 3, channel="meta_ads", asset_type="meta_ad",
                position=None)]
    assert newer_pending_versions(rows, "review", SLOT) == {"a": 3}


def test_variants_are_separate_slots():
    rows = [row("a", 2, variant="A"), row("b", 4, variant="B")]
    assert newer_pending_versions(rows, "review", SLOT) == {}


def test_rows_not_in_the_candidate_status_are_ignored_entirely():
    """An approved v5 does not overtake a pending v2 here. That is a
    different question -- lifecycle's newer_version_pending blocker -- and
    answering it in this function would skip the pending row for the wrong
    reason."""
    rows = [row("a", 2), row("b", 5, status="approved")]
    assert newer_pending_versions(rows, "review", SLOT) == {}


def test_a_superseded_older_version_does_not_count_as_a_candidate():
    rows = [row("a", 1, status="superseded"), row("b", 3)]
    assert newer_pending_versions(rows, "review", SLOT) == {}


def test_no_slot_columns_means_no_ranking():
    """Angles and concepts have no version_number -- they are revised in
    place -- and they approve additively, so several may stand at once."""
    assert newer_pending_versions([{"id": "a", "status": "draft"}],
                                  "draft", ()) == {}


# --------------------------------------------------------------------------
# bulk_skip_reason
# --------------------------------------------------------------------------

def test_the_newest_pending_version_is_eligible():
    assert bulk_skip_reason("campaign_assets", "review", "pass", 0,
                            None) is None


def test_an_overtaken_version_is_skipped_and_names_the_newer_one():
    reason = bulk_skip_reason("campaign_assets", "review", "pass", 0, 4)
    assert reason == "v4 is a newer version awaiting review"


def test_being_overtaken_outranks_a_qa_warning():
    """The message a reviewer acts on. 'has a QA warning -- approve it
    individually' on an overtaken v1 sends them to approve the OLDEST version
    while v3 sits unread: the wrong action, stated confidently."""
    reason = bulk_skip_reason("campaign_assets", "review", "warning", 0, 3)
    assert reason == "v3 is a newer version awaiting review"


def test_being_overtaken_outranks_an_open_edit_request():
    reason = bulk_skip_reason("campaign_assets", "review", "pass", 2, 3)
    assert reason == "v3 is a newer version awaiting review"


def test_already_approved_still_outranks_being_overtaken():
    """'already approved' is the one reason that is not a skip -- approve_many
    drops those rows silently -- so it has to keep winning."""
    assert bulk_skip_reason("campaign_assets", "approved", "pass", 0,
                            9) == "already approved"


def test_a_wrong_status_still_outranks_being_overtaken():
    assert bulk_skip_reason("campaign_assets", "draft", "pass", 0,
                            9) == "status is draft"


def test_the_newer_version_argument_defaults_to_absent():
    """Callers that predate slot ranking -- angles, concepts -- must not have
    their behaviour change."""
    assert bulk_skip_reason("campaign_angles", "draft", None, 0) is None


@pytest.mark.parametrize("qa", ["pass", "warning", "blocked", None])
def test_an_overtaken_version_is_skipped_whatever_its_qa_says(qa):
    assert bulk_skip_reason("campaign_assets", "review", qa, 0, 7) == (
        "v7 is a newer version awaiting review")


# --------------------------------------------------------------------------
# reject(): the action the edit loop was missing
# --------------------------------------------------------------------------

class RejectCursor:
    def __init__(self, status):
        self._status = status
        self.executed: list[tuple] = []

    async def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    async def fetchone(self):
        return {"status": self._status} if self._status else None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def do_reject(monkeypatch, status, table="campaign_assets", note=None):
    cur = RejectCursor(status)
    monkeypatch.setattr(pipeline, "cursor", lambda: cur)
    return asyncio.run(pipeline.reject(table, "r1", "alice", note)), cur


def test_an_approved_version_cannot_be_rejected(monkeypatch):
    """Retiring live copy is a different act. 013's partial unique index means
    the slot would then hold no approved version at all -- supersede it by
    approving another version instead."""
    with pytest.raises(ValueError, match="approved"):
        do_reject(monkeypatch, "approved")


def test_a_superseded_version_cannot_be_rejected(monkeypatch):
    with pytest.raises(ValueError, match="superseded"):
        do_reject(monkeypatch, "superseded")


def test_rejecting_twice_is_idempotent_not_an_error(monkeypatch):
    """Two reviewers, two tabs. The second click should not raise, and must
    not overwrite the first rejecter's name."""
    result, cur = do_reject(monkeypatch, "rejected")
    assert result["already"] is True
    assert not any(sql.startswith("update") for sql, _ in cur.executed)


@pytest.mark.parametrize("status", ["draft", "review"])
def test_an_undecided_version_can_be_rejected(monkeypatch, status):
    result, cur = do_reject(monkeypatch, status)
    assert result["rejected"] is True
    assert result["from"] == status
    update = next(sql for sql, _ in cur.executed
                  if sql.startswith("update"))
    assert "rejected_by = %s" in update
    assert "rejected_at = now()" in update


def test_a_missing_row_is_a_lookup_error(monkeypatch):
    with pytest.raises(LookupError):
        do_reject(monkeypatch, None)


def test_a_table_that_cannot_be_rejected_says_which_can(monkeypatch):
    with pytest.raises(ValueError, match="campaign_assets"):
        do_reject(monkeypatch, "draft", table="campaign_angles")


def test_a_reason_is_optional(monkeypatch):
    """Forcing prose produces 'n/a'. coalesce means an absent note leaves any
    existing note alone rather than blanking it."""
    _, cur = do_reject(monkeypatch, "review", note=None)
    update = next((sql, p) for sql, p in cur.executed
                  if sql.startswith("update"))
    assert "coalesce(%s, notes)" in update[0]
    assert update[1][1] is None


def test_a_reason_is_stored_when_given(monkeypatch):
    _, cur = do_reject(monkeypatch, "review", note="off-brand tone")
    params = next(p for sql, p in cur.executed
                  if sql.startswith("update"))
    assert "off-brand tone" in params


def test_approved_is_not_a_rejectable_source_for_any_table():
    """The table is the guard, so assert the table."""
    for table, sources in pipeline._REJECTABLE.items():
        assert "approved" not in sources, table
        assert "superseded" not in sources, table
        assert "rejected" not in sources, table


def test_only_the_versioned_tables_are_rejectable_here():
    """Angles and concepts already reject through generators.angles/concepts
    with decided_by. Adding them here would give one action two code paths."""
    assert set(pipeline._REJECTABLE) == {"campaign_assets",
                                         "campaign_strategies"}
