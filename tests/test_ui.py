"""Tests for the UI's pure helpers and its templates.

The routes themselves are thin -- each one calls a generator or validator and
redirects -- so what is worth testing here is the two places a UI bug would be
silent: how form text becomes a list, and whether the flash colour matches the
outcome rather than the HTTP status.

Templates are rendered against fixture data so a Jinja typo fails here rather
than as a 500 in front of someone.
"""

from __future__ import annotations

import pytest

from campaigns import split_list
from ui import _kind_for


# --------------------------------------------------------------------------
# Form parsing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("", []),
    (None, []),
    ("one", ["one"]),
    ("one, two", ["one", "two"]),
    ("one\ntwo", ["one", "two"]),
    # Mixed, because people do this and being strict about it fails silently
    # into one long string where a list was meant.
    ("one\ntwo, three", ["one", "two", "three"]),
    ("  spaced  ,  out  ", ["spaced", "out"]),
    ("trailing,", ["trailing"]),
    ("a\r\nb", ["a", "b"]),
    ("\n\n", []),
])
def test_split_list(raw, expected):
    assert split_list(raw) == expected


# --------------------------------------------------------------------------
# Flash colour follows the outcome, not the request
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status,kind", [
    ("blocked", "error"),
    ("needs_info", "warn"),
    ("warning", "warn"),
    ("pass", "ok"),
    (None, "ok"),          # actions with no meaningful status
    ("", "ok"),
    ("something_new", "ok"),  # an unknown status must not crash the flash
])
def test_kind_for_status(status, kind):
    """The bug this covers: 'validation #1 -> blocked' flashed GREEN because
    the POST succeeded. A misleading-but-technically-true signal is exactly
    what this project exists to prevent.

    _kind_for takes the status rather than the message on purpose. The first
    version sniffed the prose for 'blocked' and turned
    '6 pass, 1 warning, 0 blocked' red -- guessing an outcome from a sentence
    that reports counts cannot be fixed in general, so the actions now pass
    their status explicitly.
    """
    assert _kind_for(status) == kind


def test_qa_worst_outcome_selection():
    """One blocked asset in twenty is a red result, not a mostly-green one.

    Mirrors the expression in ui.do_qa; kept as a test because the precedence
    is the part that would be got wrong silently.
    """
    def worst(r):
        return ("blocked" if r["blocked"] else
                "needs_info" if r["needs_info"] else
                "warning" if r["warning"] else "pass")

    assert worst({"blocked": 1, "needs_info": 0, "warning": 5}) == "blocked"
    assert worst({"blocked": 0, "needs_info": 1, "warning": 5}) == "needs_info"
    assert worst({"blocked": 0, "needs_info": 0, "warning": 1}) == "warning"
    assert worst({"blocked": 0, "needs_info": 0, "warning": 0}) == "pass"


# --------------------------------------------------------------------------
# Templates render
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def env():
    """The same Jinja environment the app uses, minus the request object.

    url_for is the only Starlette-provided global these templates could reach,
    and they deliberately use plain hrefs instead, so a bare environment is
    enough to catch a syntax error or a bad attribute access.
    """
    from jinja2 import Environment, FileSystemLoader, StrictUndefined
    from pathlib import Path
    return Environment(
        loader=FileSystemLoader(str(Path(__file__).resolve().parent.parent
                                    / "templates")),
        undefined=StrictUndefined,
        autoescape=True,
    )


def test_campaign_list_renders_empty(env):
    out = env.get_template("campaigns.html").render(
        campaigns=[], msg=None, kind="ok")
    assert "No campaigns yet" in out
    assert "{{" not in out


def test_campaign_list_renders_rows(env):
    out = env.get_template("campaigns.html").render(campaigns=[{
        "id": "abc", "name": "Test", "status": "validated",
        "channels": ["email", "meta_ads"], "brand_name": "Renegade",
        "product_name": "Franchise", "campaign_type_name": "Franchise",
        "strategies": 2, "assets": 7, "approved_assets": 1,
    }], msg="hello", kind="ok")
    assert "Test" in out
    assert "email, meta_ads" in out
    assert 'class="pill validated"' in out
    assert "1 approved" in out
    assert 'class="flash ok"' in out


def test_brief_form_renders(env):
    out = env.get_template("new.html").render(
        products=[{"id": "p1", "name": "Franchise Program",
                   "brand_name": "Renegade", "approved_for_marketing": False,
                   "brand_slug": "renegade", "primary_cta": "Talk"}],
        campaign_types=[{"id": "t1", "name": "Franchise",
                         "brand_slug": "renegade"}],
        msg=None, kind="ok")
    # The unapproved-product warning has to be visible at brief time, not
    # discovered later when every asset comes back ungrounded.
    assert "not approved for marketing" in out
    assert 'name="channels"' in out


def _asset(**kw) -> dict:
    base = {
        "id": "a1", "channel": "meta_ads", "asset_type": "meta_ad",
        "variant": "A", "position": None, "content": {"headline": "Hi"},
        "status": "review", "version_number": 1, "approved_by": None,
        # 022. Present-but-null, like approved_by: the columns are on
        # the row whether or not anything was ever rejected.
        "rejected_by": None, "notes": None,
        "knowledge_snapshot": {"kb_chunk_ids": [1, 2],
                               "approved_claims": ["x"],
                               "prohibited_claims": []},
        "concept_hook": "A hook", "qa_status": "pass",
        "blockers": [], "warnings": [], "recommendations": [],
        "deterministic_status": "pass", "ai_status": None,
        # 016. Present-but-null is the real shape: the QA left join yields the
        # key whether or not a QA row exists.
        "qa_by": "someone@example.com",
        # 020. pipeline_state.attach() puts these on every item.
        "revisions": [], "open_revision": None, "bulk_skip_reason": None,
    }
    base.update(kw)
    return base


def _state(**kw) -> dict:
    base = {
        "validations": [], "latest_validation": None, "strategies": [],
        "approved_strategy": None, "angles": [], "concepts": [], "assets": [],
        "revisions": [], "open_revisions": [],
        "bulk": {"angles": 0, "concepts": 0, "assets": 0},
        "lifecycle": {"blockers": [], "ready_to_approve": True,
                      "can_go_to": [], "earned": "draft", "history": [],
                      "slots": []},
        "can": {"generate_strategy": True, "generate_angles": False,
                "generate_concepts": False, "generate_assets": False,
                "run_qa": False},
        "counts": {"angles_approved": 0, "concepts_approved": 0, "assets": 0,
                   "assets_blocked": 0, "assets_approved": 0},
    }
    base.update(kw)
    return base


CAMPAIGN = {
    "id": "c1", "name": "Test Campaign", "status": "draft",
    "brand_name": "Renegade", "product_name": "Franchise Program",
    "campaign_type_name": "Franchise Recruitment",
    "channels": ["email", "meta_ads"], "product_marketable": True,
    # 021. campaigns.get() selects c.*, so a real row always has these.
    "approved_by": None, "approved_at": None,
}


def test_campaign_detail_renders_fresh_brief(env):
    out = env.get_template("campaign.html").render(
        c=CAMPAIGN, **_state(), msg=None, kind="ok")
    assert "Stage 3" in out and "Stage 4" in out
    assert "Not validated yet" in out
    # Gating: a fresh brief cannot generate angles or concepts, and the page
    # must say why rather than offering a button that raises StageNotReady.
    assert "needs an approved strategy" in out
    assert "needs an approved angle" in out
    assert "needs an approved concept" in out.lower()


def test_campaign_detail_renders_findings(env):
    validation = {
        "validation_number": 1, "status": "blocked",
        "deterministic_status": "blocked", "ai_status": None,
        "blockers": [{"check": "missing_brief_field", "severity": "blocker",
                      "message": "offer is empty", "field": "offer",
                      "remedy": "fill it in"}],
        "warnings": [], "recommendations": [], "validated_at": None,
        "validated_by": "someone@example.com",
    }
    out = env.get_template("campaign.html").render(
        c=CAMPAIGN, **_state(validations=[validation],
                             latest_validation=validation),
        msg=None, kind="ok")
    assert "missing_brief_field" in out
    assert "[offer]" in out
    assert "fill it in" in out
    assert 'class="pill blocked"' in out


def test_campaign_detail_renders_assets_with_char_counts(env):
    out = env.get_template("campaign.html").render(
        c=CAMPAIGN,
        **_state(assets=[_asset()],
                 can={"generate_strategy": True, "generate_angles": True,
                      "generate_concepts": True, "generate_assets": True,
                      "run_qa": True},
                 counts={"angles_approved": 1, "concepts_approved": 1,
                         "assets": 1, "assets_blocked": 0,
                         "assets_approved": 0}),
        msg=None, kind="ok")
    assert "headline" in out
    assert "(2 chars)" in out          # len("Hi")
    assert "2 chunks" in out
    assert "Approve" in out


def test_unmarketable_product_warns_on_the_detail_page(env):
    out = env.get_template("campaign.html").render(
        c=dict(CAMPAIGN, product_marketable=False), **_state(),
        msg=None, kind="ok")
    assert "not approved for marketing" in out


def test_asset_with_no_qa_is_labelled_rather_than_blank(env):
    """An un-QA'd asset must not look like a passing one."""
    out = env.get_template("campaign.html").render(
        c=CAMPAIGN, **_state(assets=[_asset(qa_status=None)]),
        msg=None, kind="ok")
    assert "no QA yet" in out


def test_blocked_asset_offers_no_approve_button(env):
    """The status guard in the template: only review + pass/warning is
    approvable, so a blocked asset cannot be approved by clicking."""
    out = env.get_template("campaign.html").render(
        c=CAMPAIGN,
        **_state(assets=[_asset(status="rejected", qa_status="blocked",
                                blockers=[{"check": "prohibited_wording",
                                           "severity": "blocker",
                                           "message": "contains $20,000",
                                           "field": "headline",
                                           "evidence": "$20,000"}])]),
        msg=None, kind="ok")
    assert "prohibited_wording" in out
    assert "$20,000" in out
    # The only Approve text on the page would come from an asset card; there
    # are no other approvable objects in this fixture.
    assert ">\n        Approve</button>" not in out
