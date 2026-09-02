"""The transition half of stage 3: what validation does to campaigns.status.

Fully faked -- no database, no checks. The deterministic checks have 54 tests
of their own; what had none is the part 021 made load-bearing. Stage 3 moves
the campaign, so stage 3 has to record the move, and has to NOT record a move
that did not happen: campaign_status_events_actually_moved rejects an event
whose from_status equals its to_status, so a spurious record is not a bad row
in the history -- it raises and takes the whole validation down with it.

The most likely real sequence is the one that would have hit it: a reviewer
fixes a brief, re-runs validation, and it is still blocked.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from validation import brief
from validation.checks import CheckContext

CAMPAIGN = {"id": str(uuid.uuid4()), "name": "Franchise Q4",
            "objective": "recruit agents"}


class FakeCursor:
    """Records what was executed. Hands back the two rows validate() reads:
    the next validation_number, then the inserted row id."""

    def __init__(self):
        self.executed: list[tuple[str, tuple | None]] = []
        self._rows: list[dict] = [{"n": 4}, {"id": uuid.uuid4()}]

    async def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    async def fetchone(self):
        return self._rows.pop(0)

    @property
    def status_writes(self) -> list[str]:
        return [params[0] for sql, params in self.executed
                if sql.startswith("update public.campaigns set status")]


class FakeCursorCM:
    def __init__(self, cur):
        self._cur = cur

    async def __aenter__(self):
        return self._cur

    async def __aexit__(self, *exc):
        return False


def run_validate(monkeypatch, *, current: str | None, overall: str):
    """-> (result, cursor, events).

    `overall` drives the deterministic status directly; combined_status maps
    pass / warning / blocked through unchanged when there is no AI tier.
    """
    cur = FakeCursor()
    events: list[dict] = []

    async def fake_load(ref):
        return dict(CAMPAIGN), CheckContext()

    async def fake_fetch_one(sql, params=None):
        return {"status": current}

    async def fake_record(c, campaign_id, from_status, to_status, changed_by,
                          automatic, note=None):
        # Same cursor, so the status and the event explaining it commit
        # together or not at all.
        assert c is cur, "the event was written on a different transaction"
        events.append({"from": from_status, "to": to_status, "by": changed_by,
                       "automatic": automatic, "note": note})

    monkeypatch.setattr(brief.vctx, "load", fake_load)
    monkeypatch.setattr(brief, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(brief, "cursor", lambda: FakeCursorCM(cur))
    monkeypatch.setattr(brief, "status_for", lambda findings: overall)
    monkeypatch.setattr(brief.lifecycle, "record_transition", fake_record)

    result = asyncio.run(brief.validate("ref", validated_by="alice"))
    return result, cur, events


# --------------------------------------------------------------------------
# A move happened: record it
# --------------------------------------------------------------------------

def test_a_first_pass_moves_draft_to_validated_and_records_it(monkeypatch):
    result, cur, events = run_validate(monkeypatch, current="draft",
                                       overall="pass")
    assert result["campaign_status"] == "validated"
    assert cur.status_writes == ["validated"]
    assert len(events) == 1
    assert (events[0]["from"], events[0]["to"]) == ("draft", "validated")
    assert events[0]["by"] == "alice"


def test_the_event_names_the_validation_that_caused_it(monkeypatch):
    """A history row reading 'draft -> blocked' and nothing else sends the
    reader hunting. The validation number is the thing to look up."""
    _, _, events = run_validate(monkeypatch, current="draft",
                                overall="blocked")
    assert events[0]["note"] == "validation #4: blocked"


def test_the_event_is_automatic(monkeypatch):
    """A person clicked Validate, but nobody CHOSE 'blocked' -- the checks
    derived it. Same character as lifecycle.advance, and an audit that cannot
    tell a derivation from a decision is misleading."""
    _, _, events = run_validate(monkeypatch, current="draft",
                                overall="blocked")
    assert events[0]["automatic"] is True


def test_a_warning_still_counts_as_reaching_validated(monkeypatch):
    _, cur, events = run_validate(monkeypatch, current="draft",
                                  overall="warning")
    assert cur.status_writes == ["validated"]
    assert events[0]["to"] == "validated"


def test_a_blocker_moves_a_campaign_that_had_reached_review(monkeypatch):
    """Bad news travels. The no-regression rule protects progress from a
    PASS, not from a blocker."""
    result, cur, events = run_validate(monkeypatch, current="review",
                                       overall="blocked")
    assert result["campaign_status"] == "blocked"
    assert cur.status_writes == ["blocked"]
    assert (events[0]["from"], events[0]["to"]) == ("review", "blocked")


# --------------------------------------------------------------------------
# No move happened: write nothing
# --------------------------------------------------------------------------

def test_re_running_validation_on_a_still_blocked_brief_writes_nothing(
        monkeypatch):
    """The likeliest sequence in real use: fix something, re-run, still
    blocked. The status did not change, so there is no event -- and an event
    here would violate campaign_status_events_actually_moved and abort the
    very validation the reviewer was trying to run."""
    result, cur, events = run_validate(monkeypatch, current="blocked",
                                       overall="blocked")
    assert result["campaign_status"] == "blocked"
    assert cur.status_writes == []
    assert events == []


def test_re_validating_an_already_validated_campaign_writes_nothing(
        monkeypatch):
    _, cur, events = run_validate(monkeypatch, current="validated",
                                  overall="pass")
    assert cur.status_writes == []
    assert events == []


def test_a_pass_does_not_drag_a_campaign_back_from_review(monkeypatch):
    """The bug 021 exposed: this used to set 'validated', and advance() then
    moved it forward again -- right final state, two spurious history rows,
    and a window where the campaign read as less progressed than it was."""
    result, cur, events = run_validate(monkeypatch, current="review",
                                       overall="pass")
    assert result["campaign_status"] == "review"
    assert cur.status_writes == []
    assert events == []


@pytest.mark.parametrize("current", ["approved", "live", "completed",
                                     "strategy", "production"])
def test_a_pass_never_moves_a_campaign_past_validated_backwards(
        monkeypatch, current):
    result, cur, events = run_validate(monkeypatch, current=current,
                                       overall="pass")
    assert result["campaign_status"] == current
    assert events == []


def test_a_dry_run_writes_nothing_at_all(monkeypatch):
    cur = FakeCursor()
    events: list = []

    async def fake_load(ref):
        return dict(CAMPAIGN), CheckContext()

    async def boom(*a, **k):
        raise AssertionError("a dry run must not read or write the campaign")

    monkeypatch.setattr(brief.vctx, "load", fake_load)
    monkeypatch.setattr(brief, "fetch_one", boom)
    monkeypatch.setattr(brief, "cursor", lambda: FakeCursorCM(cur))
    monkeypatch.setattr(brief, "status_for", lambda findings: "blocked")
    monkeypatch.setattr(brief.lifecycle, "record_transition",
                        lambda *a, **k: events.append(a))

    result = asyncio.run(brief.validate("ref", validated_by="alice",
                                        dry_run=True))
    assert result["written"] is False
    assert cur.executed == []
    assert events == []


# --------------------------------------------------------------------------
# The constraint, mirrored
# --------------------------------------------------------------------------

@pytest.mark.parametrize("current", ["draft", "validating", "blocked",
                                     "needs_info", "validated", "strategy",
                                     "production", "review"])
@pytest.mark.parametrize("overall", ["pass", "warning", "blocked"])
def test_no_event_is_ever_written_with_from_equal_to_to(monkeypatch, current,
                                                        overall):
    """The Python mirror of campaign_status_events_actually_moved. Every
    reachable combination, because the constraint does not fail gracefully --
    it aborts the validation."""
    _, _, events = run_validate(monkeypatch, current=current, overall=overall)
    for event in events:
        assert event["from"] != event["to"], (current, overall, event)


@pytest.mark.parametrize("current", ["draft", "blocked", "needs_info",
                                     "validated", "review"])
@pytest.mark.parametrize("overall", ["pass", "warning", "blocked"])
def test_a_status_write_and_an_event_always_come_in_pairs(monkeypatch,
                                                          current, overall):
    """Neither without the other. A status with no event is the hole this
    fixed; an event with no status write would be a history that lies."""
    _, cur, events = run_validate(monkeypatch, current=current,
                                  overall=overall)
    assert len(cur.status_writes) == len(events)
