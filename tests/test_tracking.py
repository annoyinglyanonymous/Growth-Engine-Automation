"""tracking.tracked_url: the utm-tagged link an approved asset ships with.

Determinism is the actual contract. The value stamped into the database and
the value pasted into an ad platform must be the same string, on every call,
forever -- so most of these tests are about inputs that could make two calls
disagree or make the URL quietly wrong: pre-existing query strings, fragments,
unicode campaign names, position zero.
"""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlsplit

import pytest

from generators import pipeline
from tracking import CHANNEL_UTM, campaign_slug, slot_content, tracked_url

BASE = "https://renegadeinsurance.com/franchise"


def build(base=BASE, **kw):
    args = {"campaign_name": "Franchise Recruitment - Captive Agents Q4 2026",
            "channel": "email", "asset_type": "email", "variant": "A",
            "position": 2, "version": 5}
    args.update(kw)
    return tracked_url(base, **args)


def params(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


# --------------------------------------------------------------------------
# The tag set
# --------------------------------------------------------------------------

def test_the_four_utm_parameters_are_present_and_correct():
    p = params(build())
    assert p["utm_source"] == ["email"]
    assert p["utm_medium"] == ["email"]
    assert p["utm_campaign"] == [
        "franchise-recruitment-captive-agents-q4-2026"]
    assert p["utm_content"] == ["email-a-2-v5"]


def test_meta_ads_uses_facebook_as_the_source():
    """GA4 classifies paid-social by recognising the SOURCE against its list
    of social sites; 'facebook' is on it and 'meta' is not. The convention
    loses to the classifier every report depends on."""
    p = params(build(channel="meta_ads", asset_type="meta_ad",
                     position=None))
    assert p["utm_source"] == ["facebook"]
    assert p["utm_medium"] == ["paid_social"]


def test_no_utm_term_is_ever_emitted():
    """utm_term is a paid-search keyword slot. google_ads is deferred, and
    emitting an empty or invented one would pollute reports now to serve a
    channel that does not exist yet."""
    assert "utm_term" not in params(build())


def test_the_path_and_host_survive_untouched():
    url = build()
    parts = urlsplit(url)
    assert parts.netloc == "renegadeinsurance.com"
    assert parts.path == "/franchise"


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------

def test_the_same_asset_produces_the_same_url_twice():
    assert build() == build()


def test_a_different_version_produces_a_different_url():
    """The reason the version is in utm_content at all: when v7 supersedes v5
    next quarter, the two runs must stay distinguishable in the report."""
    assert build(version=5) != build(version=7)
    assert params(build(version=7))["utm_content"] == ["email-a-2-v7"]


def test_variants_are_distinguishable():
    assert (params(build(variant="A"))["utm_content"]
            != params(build(variant="B"))["utm_content"])


# --------------------------------------------------------------------------
# Destinations that already carry things
# --------------------------------------------------------------------------

def test_existing_non_utm_parameters_survive():
    """?ref=partner is someone's deliberate URL. Dropping it would break
    whatever that parameter feeds."""
    p = params(build(base=BASE + "?ref=partner"))
    assert p["ref"] == ["partner"]
    assert p["utm_source"] == ["email"]


def test_the_fragment_survives():
    url = build(base=BASE + "#pricing")
    assert urlsplit(url).fragment == "pricing"
    assert params(url)["utm_campaign"]


def test_existing_utm_parameters_are_replaced_not_duplicated():
    """Two utm_source values in one URL is undefined behaviour in every
    analytics tool. Ours win: at stamping time the per-asset values are the
    correct ones by definition, and brief validation already warned about the
    pre-tagged destination."""
    p = params(build(base=BASE + "?utm_source=oldblast&utm_campaign=ghost"))
    assert p["utm_source"] == ["email"]
    assert p["utm_campaign"] == ["franchise-recruitment-captive-agents-q4-2026"]
    flat = build(base=BASE + "?utm_source=oldblast")
    assert "oldblast" not in flat
    assert flat.count("utm_source=") == 1


def test_a_blank_valued_parameter_survives():
    p = params(build(base=BASE + "?debug="))
    assert p["debug"] == [""]


# --------------------------------------------------------------------------
# Slugs and content labels
# --------------------------------------------------------------------------

def test_the_campaign_slug_is_lowercase_ascii_and_hyphens():
    assert campaign_slug("Franchise Recruitment - Q4 2026") == \
        "franchise-recruitment-q4-2026"


def test_accents_fold_rather_than_vanish():
    assert campaign_slug("Campaña São Paulo") == "campana-sao-paulo"


def test_punctuation_collapses_to_single_hyphens():
    assert campaign_slug("Q4!!! -- (final) [v2]") == "q4-final-v2"


def test_a_position_of_zero_is_not_treated_as_absent():
    """`position is not None`, not `if position` -- the same trap
    lifecycle.slot_label documents. 0 is a real sequence position."""
    assert slot_content("email", "A", 0, 3) == "email-a-0-v3"


def test_a_standalone_asset_has_no_position_segment():
    assert slot_content("meta_ad", "B", None, 4) == "ad-b-v4"


def test_two_asset_types_in_one_slot_get_different_labels():
    """THE REGRESSION THIS GUARDS
    utm_source and utm_medium come from the channel, and a UGC video runs on
    meta_ads exactly like a static ad. Without the type in utm_content, a
    meta_ad and a video_script at the same variant and version produce the
    same tracked URL -- and campaign_assets_one_approved_idx keys on
    asset_type, so both can be approved at once. Two creatives, one link, and
    performance data that cannot be told apart afterwards."""
    shared = dict(campaign_name="Q4 2026", channel="meta_ads", variant="A",
                  position=None, version=3)
    ad = tracked_url(BASE, asset_type="meta_ad", **shared)
    video = tracked_url(BASE, asset_type="video_script", **shared)
    assert ad != video
    assert params(ad)["utm_content"] == ["ad-a-v3"]
    assert params(video)["utm_content"] == ["vid-a-v3"]
    # Everything else about them is identical, which is exactly why
    # utm_content had to carry the difference.
    assert params(ad)["utm_source"] == params(video)["utm_source"]


def test_an_unknown_asset_type_is_labelled_rather_than_refused():
    """Unlike an unknown channel, which has no defensible source/medium: the
    source and medium are still right here, only the label is unfamiliar.
    Refusing to stamp -- or blocking approval -- over a label would be a
    worse outcome than a long token."""
    assert slot_content("carrier_pigeon", "B", None, 1) \
        == "carrier-pigeon-b-v1"


def test_the_content_label_lands_url_safe_in_the_query():
    p = params(build(variant="A/B çtest", position=None, version=2))
    assert p["utm_content"] == ["email-a-b-ctest-v2"]


# --------------------------------------------------------------------------
# Refusals: governed inputs, so a bad value is a caller bug
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "", "renegadeinsurance.com/franchise", "ftp://x.com/a",
    "javascript:alert(1)", "https://",
])
def test_a_non_http_base_raises(bad):
    with pytest.raises(ValueError):
        build(base=bad)


def test_an_unknown_channel_raises_and_names_the_known_ones():
    """google_ads has a schema slot but no utm convention until it is built.
    Guessing one here would bake an unreviewed convention into shipped URLs."""
    with pytest.raises(ValueError, match="email"):
        build(channel="google_ads")


def test_every_declared_channel_convention_actually_works():
    for channel in CHANNEL_UTM:
        assert params(build(channel=channel, asset_type="meta_ad",
                            position=None))["utm_source"]


# --------------------------------------------------------------------------
# The stamp at approval (generators.pipeline.approve)
# --------------------------------------------------------------------------

class StampCursor:
    """Serves approve()'s queries in order and records every statement."""

    def __init__(self, asset_row, campaign_row):
        self._rows = [asset_row, campaign_row]
        self.executed: list[tuple[str, tuple | None]] = []
        self.rowcount = 0

    async def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    async def fetchone(self):
        return self._rows.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def final_update(self):
        return next((sql, p) for sql, p in reversed(self.executed)
                    if sql.startswith("update") and "'approved'" in sql)


def approve_with(monkeypatch, *, destination, channel="email", position=2,
                 asset_type="email"):
    asset = {"status": "review", "version_number": 5,
             "campaign_id": "camp-1", "channel": channel,
             "asset_type": asset_type, "variant": "A", "position": position}
    campaign = {"name": "Franchise Recruitment - Captive Agents Q4 2026",
                "destination_url": destination}
    cur = StampCursor(asset, campaign)
    monkeypatch.setattr(pipeline, "cursor", lambda: cur)
    result = asyncio.run(
        pipeline.approve("campaign_assets", "a-1", "alice"))
    return result, cur


def test_approval_with_a_destination_stamps_the_tracked_link(monkeypatch):
    result, cur = approve_with(monkeypatch, destination=BASE)
    sql, p = cur.final_update
    assert "tracked_url = %s" in sql
    stamped = p[1]
    # The stamp IS the pure function -- one derivation, so the recorded value
    # and any preview can never disagree.
    assert stamped == tracked_url(
        BASE, campaign_name="Franchise Recruitment - Captive Agents Q4 2026",
        channel="email", asset_type="email", variant="A", position=2,
        version=5)
    assert result["tracked_url"] == stamped


def test_approval_without_a_destination_stamps_nothing(monkeypatch):
    """Null, not an error. A missing link is the UI's hint to show, never a
    reason a reviewer cannot sign off copy."""
    result, cur = approve_with(monkeypatch, destination=None)
    sql, _ = cur.final_update
    assert "tracked_url" not in sql
    assert "tracked_url" not in result


def test_a_channel_with_no_utm_convention_stamps_nothing(monkeypatch):
    """google_ads assets cannot exist today (no generator), but the schema
    allows the channel -- and approval of one must not crash on the missing
    convention or invent one."""
    result, cur = approve_with(monkeypatch, destination=BASE,
                               channel="google_ads", position=None)
    sql, _ = cur.final_update
    assert "tracked_url" not in sql
    assert result["superseded"] == 0


def test_a_video_script_and_a_static_ad_are_stamped_differently(
        monkeypatch):
    """The end of the collision, at the point it would actually have bitten:
    approval. Both rows are meta_ads, both variant A, both version 5, and
    campaign_assets_one_approved_idx lets both be approved at once because it
    keys on asset_type. Before 026 they were stamped with the same URL."""
    _, ad_cur = approve_with(monkeypatch, destination=BASE,
                             channel="meta_ads", asset_type="meta_ad",
                             position=None)
    _, video_cur = approve_with(monkeypatch, destination=BASE,
                                channel="meta_ads", asset_type="video_script",
                                position=None)
    ad_link = ad_cur.final_update[1][1]
    video_link = video_cur.final_update[1][1]
    assert ad_link != video_link
    assert "utm_content=ad-a-v5" in ad_link
    assert "utm_content=vid-a-v5" in video_link
    # Same campaign, same channel: everything except the content label is
    # identical, which is why the label had to carry the difference.
    assert ad_link.replace("ad-a-v5", "") == video_link.replace("vid-a-v5", "")


def test_strategies_never_touch_tracking(monkeypatch):
    """campaign_strategies goes through the same approve(); its scope has no
    channel column, so reaching _tracked_link would KeyError. The guard is
    the table name."""
    cur = StampCursor({"status": "review", "version_number": 2,
                       "campaign_id": "camp-1"}, None)
    monkeypatch.setattr(pipeline, "cursor", lambda: cur)
    result = asyncio.run(
        pipeline.approve("campaign_strategies", "s-1", "alice"))
    assert "tracked_url" not in result
    # Only three statements: the locking select and two updates -- no
    # campaign lookup ever happened.
    assert len(cur.executed) == 4  # select, supersede, stale, final update
