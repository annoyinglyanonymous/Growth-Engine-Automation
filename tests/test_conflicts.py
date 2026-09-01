"""Validation of KB/conflicts.json -- the hand-maintained conflict register.

This file is edited by people, not generated, and the loader upserts it
verbatim. A malformed entry becomes a bad row in kb.conflicts, and a resolved
conflict missing its resolution becomes a claim the table cannot support.
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONFLICTS = ROOT / "KB" / "conflicts.json"

SEVERITIES = {"info", "warning", "blocker"}
STATUSES = {"active", "resolved"}


@pytest.fixture(scope="module")
def conflicts():
    return json.loads(CONFLICTS.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def corpus_text():
    """All chunk text, concatenated. Skipped if build/ is absent."""
    parts = []
    for name in ("chunks", "entity_chunks"):
        path = ROOT / "build" / f"{name}.jsonl"
        if not path.is_file():
            pytest.skip(f"{path} not built")
        with path.open(encoding="utf-8") as fh:
            parts += [json.loads(line)["text"] for line in fh if line.strip()]
    return "\n".join(parts)


def test_topics_are_unique_per_brand(conflicts):
    """kb.conflicts has UNIQUE (brand_id, topic); a duplicate silently
    overwrites the first rather than erroring."""
    keys = [(c["brand_slug"], c["topic"]) for c in conflicts]
    assert len(keys) == len(set(keys))


def test_enum_values_match_the_check_constraints(conflicts):
    for c in conflicts:
        assert c["severity"] in SEVERITIES, c["topic"]
        assert c.get("status", "active") in STATUSES, c["topic"]


def test_every_conflict_cites_a_source(conflicts):
    """A conflict nobody can verify is an assertion, not a record."""
    for c in conflicts:
        assert c.get("sources"), c["topic"]
        for url in c["sources"]:
            assert url.startswith("https://"), (c["topic"], url)


def test_resolved_conflicts_are_complete(conflicts):
    """Mirrors the conflicts_resolution_complete CHECK constraint, so a bad
    entry fails here rather than at load time."""
    for c in conflicts:
        if c.get("status") != "resolved":
            continue
        assert c.get("resolution"), c["topic"]
        assert c.get("resolved_at"), c["topic"]
        assert c.get("resolved_by"), c["topic"]
        assert c.get("correct_value"), c["topic"]


def test_active_conflicts_claim_no_answer(conflicts):
    """correct_value on an active conflict means someone recorded the answer
    and forgot to flip the status -- the agent would then still be told to
    quote neither figure."""
    for c in conflicts:
        if c.get("status", "active") == "active":
            assert not c.get("correct_value"), c["topic"]
            assert not c.get("stale_values"), c["topic"]


def test_stale_values_still_exist_in_the_corpus(conflicts, corpus_text):
    """The useful direction of this check is FAILURE.

    stale_values lists figures known wrong and still published. When one stops
    appearing in the corpus, the website has been corrected and the entry is
    obsolete -- so this test failing is the signal to retire it, not a bug.
    """
    for c in conflicts:
        for value in c.get("stale_values", []):
            assert value in corpus_text, (
                f"{c['topic']}: {value!r} no longer appears in the corpus -- "
                f"the site may have been corrected; retire this stale_value"
            )


def test_franchise_fee_is_resolved_to_25000(conflicts):
    """Confirmed 2026-08-26. Pinned because $20,000 is still live on
    /about-us/ and reachable through 16 KB documents, so a regression here
    would re-legitimise the wrong figure."""
    fee = next(c for c in conflicts if c["topic"] == "franchise-fee")
    assert fee["status"] == "resolved"
    assert fee["correct_value"] == "$25,000"
    assert fee["stale_values"] == ["$20,000"]
    assert fee["severity"] == "blocker", (
        "severity describes consequence, not open-ness: writing $20,000 is "
        "still a blocker now that we know it is wrong"
    )


def test_no_conflict_detail_contradicts_its_own_correct_value(conflicts):
    """A resolved conflict's detail must not still present the stale figure as
    a live alternative -- that text goes straight into the agent prompt."""
    for c in conflicts:
        correct = c.get("correct_value")
        if not correct:
            continue
        assert correct in c["detail"], c["topic"]
        for stale in c.get("stale_values", []):
            near = re.search(
                rf"{re.escape(stale)}\s*(?:or|vs\.?|versus)\s*{re.escape(correct)}",
                c["detail"], re.I)
            assert near is None, (
                f"{c['topic']}: detail still offers {stale} as an alternative")
