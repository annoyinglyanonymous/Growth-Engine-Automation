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
        # 025. Stamped at approval; null until then.
        "tracked_url": None,
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
    # 021, 025. campaigns.get() selects c.*, so a real row always has these.
    "approved_by": None, "approved_at": None, "destination_url": None,
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


VIDEO_CONTENT = {
    "duration_target_seconds": 30,
    "scenes": [
        {"n": 1, "seconds": 3, "spoken": "I quoted fourteen carriers today.",
         "on_screen": "14 carriers. One day.",
         "visual_prompt": "handheld selfie, agent at a desk, 9:16"},
        {"n": 2, "seconds": 27, "spoken": "It used to take me all week.",
         "on_screen": "A week, every week",
         "visual_prompt": "over-shoulder of a laptop"},
    ],
    "cta": "Book a 15-minute licensing call",
    "caption": "Quoting used to take my whole week.",
}


def test_campaign_detail_renders_a_video_shot_list(env):
    """THE BUG THIS COVERS
    The asset card rendered {{ value }} for every content field, which was
    fine while every value was a string. A video script's `scenes` is a list
    of objects, and printing one straight gives the reviewer a Python repr --
    [{'n': 1, 'seconds': 3, ...}] -- which is unreadable and, worse, looks
    like the data is broken rather than the template.
    """
    out = env.get_template("campaign.html").render(
        c=CAMPAIGN,
        **_state(assets=[_asset(asset_type="video_script",
                                content=VIDEO_CONTENT)],
                 can={"generate_strategy": True, "generate_angles": True,
                      "generate_concepts": True, "generate_assets": True,
                      "run_qa": True},
                 counts={"angles_approved": 1, "concepts_approved": 1,
                         "assets": 1, "assets_blocked": 0,
                         "assets_approved": 0}),
        msg=None, kind="ok")
    flat = " ".join(out.split())

    # No Python repr anywhere.
    assert "{'n':" not in out and "'spoken'" not in out

    # Every shot's three pieces of copy are on the page, labelled.
    assert "I quoted fourteen carriers today." in flat
    assert "It used to take me all week." in flat
    assert "handheld selfie, agent at a desk, 9:16" in flat
    assert flat.count("<dt>spoken</dt>") == 2
    assert flat.count("<dt>visual_prompt</dt>") == 2

    # Structure, not just text: an ordered list so shot 2 reads as shot 2.
    assert '<ol class="nested">' in out
    assert "video_script" in flat


def test_the_shot_count_is_shown_beside_the_scenes_field(env):
    """A string field shows its character count; a list shows how many
    entries it has. Same purpose -- the reviewer should not have to count."""
    out = env.get_template("campaign.html").render(
        c=CAMPAIGN,
        **_state(assets=[_asset(asset_type="video_script",
                                content=VIDEO_CONTENT)],
                 can={"generate_strategy": True, "generate_angles": True,
                      "generate_concepts": True, "generate_assets": True,
                      "run_qa": True},
                 counts={"angles_approved": 1, "concepts_approved": 1,
                         "assets": 1, "assets_blocked": 0,
                         "assets_approved": 0}),
        msg=None, kind="ok")
    flat = " ".join(out.split())
    assert "<dt>scenes <span class=\"muted\">(2)</span>" in flat


def test_the_video_button_follows_the_meta_ads_channel(env):
    """026: gated on meta_ads rather than a channel of its own, because a UGC
    video ad IS a Meta ad and inherits its utm convention."""
    on = env.get_template("campaign.html").render(
        c=CAMPAIGN,
        **_state(can={"generate_strategy": True, "generate_angles": True,
                      "generate_concepts": True, "generate_assets": True,
                      "run_qa": True},
                 counts={"angles_approved": 1, "concepts_approved": 1,
                         "assets": 0, "assets_blocked": 0,
                         "assets_approved": 0}),
        msg=None, kind="ok")
    assert "Generate UGC video scripts" in on
    assert 'name="seconds"' in on

    off = env.get_template("campaign.html").render(
        c=dict(CAMPAIGN, channels=["email"]),
        **_state(can={"generate_strategy": True, "generate_angles": True,
                      "generate_concepts": True, "generate_assets": True,
                      "run_qa": True},
                 counts={"angles_approved": 1, "concepts_approved": 1,
                         "assets": 0, "assets_blocked": 0,
                         "assets_approved": 0}),
        msg=None, kind="ok")
    assert "Generate UGC video scripts" not in off


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


# --------------------------------------------------------------------------
# The standing do-not-use list (023)
# --------------------------------------------------------------------------

def _exclusion(**kw) -> dict:
    from datetime import datetime, timezone
    base = {
        "id": "e1", "phrase": "free forever", "note": "Legal asked us to.",
        "added_by": "alice@example.com",
        "added_at": datetime(2026, 9, 2, tzinfo=timezone.utc),
        "active": True, "retired_by": None, "retired_at": None,
        "brand_slug": "renegade", "brand_name": "Renegade Insurance",
    }
    base.update(kw)
    return base


def test_exclusions_page_renders_empty(env):
    """The empty state has to say what the consequence of empty IS. A blank
    table reads as "nothing to see"; the truth is "no wording is blocked"."""
    out = env.get_template("exclusions.html").render(
        rules=[], brands=[{"slug": "renegade", "name": "Renegade"}],
        msg=None, kind="ok")
    # Whitespace-normalised: the sentence wraps in the template, and an
    # assertion that depends on where it wraps breaks on reflow.
    flat = " ".join(out.split())
    assert "Nothing excluded yet" in flat
    assert "no wording is blocked" in flat
    assert "{{" not in out


def test_exclusions_page_lists_an_active_rule(env):
    out = env.get_template("exclusions.html").render(
        rules=[_exclusion()],
        brands=[{"slug": "renegade", "name": "Renegade"}],
        msg=None, kind="ok")
    assert "free forever" in out
    assert "Legal asked us to." in out
    assert "alice@example.com" in out
    assert "/retire" in out


def test_a_retired_rule_shows_who_retired_it_and_offers_no_button(env):
    """Retired rows are kept so the record survives, which only helps if the
    page says who ended it -- and it must not offer to retire it twice."""
    from datetime import datetime, timezone
    out = env.get_template("exclusions.html").render(
        rules=[_exclusion(active=False, retired_by="bob@example.com",
                          retired_at=datetime(2026, 9, 3,
                                              tzinfo=timezone.utc))],
        brands=[{"slug": "renegade", "name": "Renegade"}],
        msg=None, kind="ok")
    assert "retired" in out
    assert "bob@example.com" in out
    assert "/retire" not in out


def test_the_brief_form_offers_a_per_campaign_exclusion_field(env):
    out = env.get_template("new.html").render(
        products=[{"id": "p1", "name": "Franchise Program",
                   "brand_name": "Renegade", "approved_for_marketing": False,
                   "brand_slug": "renegade", "primary_cta": "Talk"}],
        campaign_types=[{"id": "t1", "name": "Franchise",
                         "brand_slug": "renegade"}],
        msg=None, kind="ok")
    assert 'name="do_not_mention"' in out
    # And it must point at the standing list, or every one-off gets typed
    # into every brief for ever.
    assert "/exclusions" in out


# --------------------------------------------------------------------------
# 025: the tracked link on an approved asset
# --------------------------------------------------------------------------

TRACKED = ("https://renegadeinsurance.com/franchise?utm_source=email"
           "&utm_medium=email&utm_campaign=test&utm_content=a-1-v5")


def test_an_approved_asset_shows_its_tracked_link(env):
    out = env.get_template("campaign.html").render(
        c=dict(CAMPAIGN, destination_url="https://renegadeinsurance.com/f"),
        **_state(assets=[_asset(status="approved", approved_by="alice",
                                tracked_url=TRACKED)]),
        msg=None, kind="ok")
    # Autoescaped inside the value attribute -- & becomes &amp;, which is
    # correct HTML and un-escapes on copy. Asserting the raw URL would demand
    # broken output.
    assert TRACKED.replace("&", "&amp;") in out
    assert "tracked link" in out


def test_an_asset_approved_before_025_says_how_to_get_a_link(env):
    """tracked_url is stamped at approval, so a pre-025 approval has none.
    The page has to say the way out -- re-approve a new version -- rather
    than showing a blank that reads like a bug."""
    out = env.get_template("campaign.html").render(
        c=dict(CAMPAIGN, destination_url="https://renegadeinsurance.com/f"),
        **_state(assets=[_asset(status="approved", approved_by="alice",
                                tracked_url=None)]),
        msg=None, kind="ok")
    flat = " ".join(out.split())
    assert "re-approve a new version" in flat


def test_a_campaign_with_no_destination_says_so_on_the_asset(env):
    out = env.get_template("campaign.html").render(
        c=CAMPAIGN,
        **_state(assets=[_asset(status="approved", approved_by="alice",
                                tracked_url=None)]),
        msg=None, kind="ok")
    flat = " ".join(out.split())
    assert "no destination URL" in flat


def test_an_unapproved_asset_shows_no_link_block(env):
    """The link is stamped at approval; previewing one on a pending version
    would show a URL that may never exist."""
    out = env.get_template("campaign.html").render(
        c=CAMPAIGN, **_state(assets=[_asset(status="review")]),
        msg=None, kind="ok")
    assert "tracked link" not in out
    assert "no destination URL" not in out


def test_the_brief_form_offers_a_destination_url_field(env):
    out = env.get_template("new.html").render(
        products=[{"id": "p1", "name": "Franchise Program",
                   "brand_name": "Renegade", "approved_for_marketing": False,
                   "brand_slug": "renegade", "primary_cta": "Talk"}],
        campaign_types=[{"id": "t1", "name": "Franchise",
                         "brand_slug": "renegade"}],
        msg=None, kind="ok")
    assert 'name="destination_url"' in out
    assert 'type="url"' in out
