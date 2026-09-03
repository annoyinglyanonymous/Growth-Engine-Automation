"""UGC video scripts (026): nested content, timing, and the shot list.

Three separate things are under test here, and they are in one file because
they are one change: a video script is the first asset whose content is NOT a
flat bag of strings, and three pieces of shared machinery quietly assumed it
was.

  * validation.checks.fields_of -- every deterministic check reads its text
    through it. While it returned only top-level values, a prohibited claim
    in scenes[2].spoken was invisible to all seven checks and passed QA
    looking clean. The tests that matter most here are the ones proving the
    FLAT behaviour did not change, because that is what the existing corpus
    of findings depends on.
  * generators.revise.validate -- required every value to be a non-empty
    string, so it rejected (twice, then raised) every legitimate revision of
    a nested asset. It also rejected every strategy revision, because
    objections is a list of objects; that regression is pinned below.
  * tracking.slot_content -- utm_source and utm_medium come from the channel,
    and a UGC video runs on meta_ads exactly like a static ad, so without the
    asset type in utm_content the two shipped the same URL.
"""

from __future__ import annotations

import copy

import pytest

from generators import revise, video_script
from tracking import slot_content
from validation.checks import (
    CheckContext,
    HOOK_MAX_SECONDS,
    ON_SCREEN_MAX_CHARS,
    check_video_script,
    fields_of,
    run_asset_checks,
)


def script(**kw) -> dict:
    """A well-formed 30-second script. Deliberately valid, so each test
    breaks exactly one thing and nothing else can explain the finding."""
    base = {
        "duration_target_seconds": 30,
        "scenes": [
            {"n": 1, "seconds": 3,
             "spoken": "I quoted fourteen carriers today.",
             "on_screen": "14 carriers. One day.",
             "visual_prompt": "handheld selfie, agent at a desk, 9:16"},
            {"n": 2, "seconds": 9, "spoken": " ".join(["word"] * 22),
             "on_screen": "It used to take a week",
             "visual_prompt": "over-shoulder of a laptop"},
            {"n": 3, "seconds": 9, "spoken": " ".join(["word"] * 22),
             "on_screen": "Same forms, one place",
             "visual_prompt": "close on hands typing"},
            {"n": 4, "seconds": 9, "spoken": " ".join(["word"] * 22),
             "on_screen": "Book a call",
             "visual_prompt": "agent talking to camera"},
        ],
        "cta": "Book a 15-minute licensing call",
        "caption": "Quoting used to take my whole week.",
    }
    base.update(kw)
    return base


def broken(**kw):
    """A copy of `script` with one thing changed deep inside it."""
    s = copy.deepcopy(script())
    for path, value in kw.items():
        s[path] = value
    return s


# --------------------------------------------------------------------------
# fields_of: the walk every check reads through
# --------------------------------------------------------------------------

def test_flat_content_is_unchanged_by_the_deeper_walk():
    """THE REGRESSION THAT MATTERS. Every stored finding, every message and
    every existing test names fields by their top-level key. Recursing must
    not rename any of them."""
    assert fields_of({"headline": "H", "primary_text": "P",
                      "description": "D"}) == [
        ("headline", "H"), ("primary_text", "P"), ("description", "D")]


def test_nested_strings_are_found_and_named_by_path():
    found = dict(fields_of(script()))
    assert found["scenes[0].spoken"] == "I quoted fourteen carriers today."
    assert found["scenes[3].on_screen"] == "Book a call"
    assert found["cta"] == "Book a 15-minute licensing call"


def test_every_spoken_line_is_reachable():
    """The point of the change: four shots, four spoken lines under a check's
    nose. Before, the count was zero."""
    spoken = [t for name, t in fields_of(script())
              if name.endswith(".spoken")]
    assert len(spoken) == 4


def test_non_strings_are_skipped_rather_than_stringified():
    """A shot's `seconds` is an int. Matching prohibited wording against
    "3" would be theatre, and worse, "$20,000" against a number would look
    like it was doing something."""
    names = dict(fields_of(script()))
    assert "scenes[0].seconds" not in names
    assert "duration_target_seconds" not in names


def test_arbitrary_depth_and_lists_of_lists():
    assert fields_of({"a": {"b": [{"c": "deep"}]}}) == [("a.b[0].c", "deep")]
    assert fields_of({"a": [["x"]]}) == [("a[0][0]", "x")]


@pytest.mark.parametrize("content", [None, {}, {"n": 3}, {"a": None},
                                     {"a": []}, {"a": {}}])
def test_contentless_input_is_safe(content):
    assert fields_of(content) == []


def test_a_prohibited_phrase_inside_a_shot_is_now_caught():
    """End to end through the real check, not just the walk: this is the
    silent hole 026 closed."""
    ctx = CheckContext(
        campaign={"do_not_mention": []},
        exclusions=[{"phrase": "guaranteed leads", "note": None,
                     "scope": "brand"}])
    hidden = broken(scenes=[dict(script()["scenes"][0],
                                 spoken="We send you guaranteed leads.")])
    findings = run_asset_checks(hidden, "meta_ads", "video_script", ctx)
    caught = [f for f in findings if f.check == "excluded_wording"]
    assert caught, "an exclusion inside a shot must be caught"
    assert caught[0].field_name == "scenes[0].spoken"
    assert caught[0].severity == "blocker"


# --------------------------------------------------------------------------
# check_video_script
# --------------------------------------------------------------------------

def test_a_well_formed_script_is_clean():
    assert check_video_script(script(), "video_script") == []


@pytest.mark.parametrize("asset_type", ["meta_ad", "email", "sms", ""])
def test_it_does_not_run_on_other_asset_types(asset_type):
    """run_asset_checks calls this for every asset. A Meta ad has no scenes
    and must not be told so."""
    assert check_video_script({"headline": "H"}, asset_type) == []


@pytest.mark.parametrize("scenes", [None, [], "one long script", {}, 0])
def test_a_script_without_shots_is_blocked(scenes):
    findings = check_video_script(broken(scenes=scenes), "video_script")
    assert [f.severity for f in findings] == ["blocker"]
    assert findings[0].field_name == "scenes"


def test_a_shot_missing_a_key_is_blocked_and_names_it():
    s = copy.deepcopy(script())
    del s["scenes"][1]["visual_prompt"]
    findings = check_video_script(s, "video_script")
    blocker = next(f for f in findings if f.severity == "blocker")
    assert "visual_prompt" in blocker.message
    assert "Shot 2" in blocker.message


def test_a_shot_that_is_not_an_object_is_blocked():
    s = copy.deepcopy(script())
    s["scenes"][2] = "just a line of dialogue"
    findings = check_video_script(s, "video_script")
    assert any(f.severity == "blocker" and "Shot 3" in f.message
               for f in findings)


def test_gapped_shot_numbers_are_blocked():
    """A shot list that cannot be put in order is not a shot list."""
    s = copy.deepcopy(script())
    s["scenes"][2]["n"] = 7
    findings = check_video_script(s, "video_script")
    assert any(f.severity == "blocker" and "cannot be assembled" in f.message
               for f in findings)


def test_a_line_too_long_to_say_is_a_warning_with_a_word_budget():
    """The check that earns this module its keep: 20 words in a 3-second shot
    is not a style preference, it is undeliverable."""
    s = copy.deepcopy(script())
    s["scenes"][0]["spoken"] = " ".join(["word"] * 20)
    findings = [f for f in check_video_script(s, "video_script")
                if f.field_name == "scenes[0].spoken"]
    assert len(findings) == 1
    assert findings[0].severity == "warning"
    assert "20 words" in findings[0].message
    # The remedy has to be actionable: how many words actually fit.
    assert "7 words" in findings[0].remedy


def test_a_line_slightly_over_is_tolerated():
    """15% slack, so a normal edit is not flagged. 8 words in 3s needs 3.2s;
    the tolerance covers it. Without this the check would cry wolf on almost
    every script and get ignored."""
    s = copy.deepcopy(script())
    s["scenes"][0]["spoken"] = " ".join(["word"] * 8)
    assert not [f for f in check_video_script(s, "video_script")
                if f.field_name == "scenes[0].spoken"]


def test_a_slow_hook_is_a_warning():
    s = copy.deepcopy(script())
    s["scenes"][0]["seconds"] = 8
    s["scenes"][3]["seconds"] = 4          # keep the total on target
    findings = [f for f in check_video_script(s, "video_script")
                if f.field_name == "scenes[0].seconds"]
    assert len(findings) == 1
    assert str(HOOK_MAX_SECONDS) in findings[0].message


def test_only_the_opening_shot_is_held_to_the_hook_rule():
    """A 9-second shot 2 is normal; a 9-second shot 1 is a lost audience."""
    assert not [f for f in check_video_script(script(), "video_script")
                if f.field_name == "scenes[1].seconds"]


def test_an_overlong_on_screen_caption_is_a_warning():
    s = copy.deepcopy(script())
    s["scenes"][1]["on_screen"] = "x" * (ON_SCREEN_MAX_CHARS + 20)
    findings = [f for f in check_video_script(s, "video_script")
                if f.field_name == "scenes[1].on_screen"]
    assert len(findings) == 1
    assert findings[0].severity == "warning"


def test_a_runtime_far_from_the_target_is_a_warning():
    s = copy.deepcopy(script())
    s["scenes"][3]["seconds"] = 30         # 30s of shots against a 30s target
    findings = [f for f in check_video_script(s, "video_script")
                if f.field_name == "scenes" and f.severity == "warning"]
    assert len(findings) == 1
    assert "51s" in findings[0].message and "30s" in findings[0].message


def test_a_runtime_within_tolerance_is_not_flagged():
    s = copy.deepcopy(script())
    s["scenes"][3]["seconds"] = 13         # 34s against 30s: a normal trim
    assert not [f for f in check_video_script(s, "video_script")
                if f.field_name == "scenes"]


def test_a_missing_duration_target_is_info_not_a_warning():
    """It reports that something could NOT be checked. Silence would let a
    script with no target look like a script that met one."""
    s = copy.deepcopy(script())
    del s["duration_target_seconds"]
    findings = [f for f in check_video_script(s, "video_script")
                if f.field_name == "duration_target_seconds"]
    assert [f.severity for f in findings] == ["info"]


def test_a_boolean_is_not_accepted_as_a_duration():
    """True is an int in Python. A shot that runs `true` seconds would
    otherwise pass the numeric guard and then sum to 1."""
    s = copy.deepcopy(script())
    s["scenes"][0]["seconds"] = True
    findings = check_video_script(s, "video_script")
    assert any(f.severity == "blocker" for f in findings)


# --------------------------------------------------------------------------
# revise.validate on a nested shape
# --------------------------------------------------------------------------

ASSET = revise.KINDS["asset"]


def test_a_revised_script_passes():
    revised = copy.deepcopy(script())
    revised["scenes"][0]["spoken"] = "I quoted fourteen carriers this week."
    assert revise.validate(ASSET, {"revised": revised}, script()) == []


@pytest.mark.parametrize("delta", [-1, 1])
def test_the_shot_count_may_change(delta):
    """"Make it shorter" legitimately means four shots instead of five. Shot
    count is a creative decision; a renamed key is a broken asset."""
    revised = copy.deepcopy(script())
    if delta < 0:
        revised["scenes"].pop()
    else:
        revised["scenes"].append(dict(revised["scenes"][0], n=5))
    assert revise.validate(ASSET, {"revised": revised}, script()) == []


def test_a_dropped_shot_key_is_rejected():
    revised = copy.deepcopy(script())
    del revised["scenes"][1]["on_screen"]
    problems = revise.validate(ASSET, {"revised": revised}, script())
    assert any("on_screen" in p and "scenes[1]" in p for p in problems)


def test_a_renamed_shot_key_is_rejected():
    revised = copy.deepcopy(script())
    revised["scenes"][0]["vo"] = revised["scenes"][0].pop("spoken")
    problems = revise.validate(ASSET, {"revised": revised}, script())
    assert any("spoken" in p for p in problems)
    assert any("vo" in p for p in problems)


def test_flattening_the_shot_list_into_prose_is_rejected():
    """The failure mode a model reaches for when asked to shorten something:
    collapse the structure. That is a different asset, not a shorter one."""
    problems = revise.validate(
        ASSET, {"revised": broken(scenes="One continuous read, 30 seconds.")},
        script())
    assert any("scenes" in p and "list" in p for p in problems)


def test_emptying_a_list_that_had_entries_is_rejected():
    problems = revise.validate(ASSET, {"revised": broken(scenes=[])},
                               script())
    assert any("scenes" in p for p in problems)


def test_a_number_turning_into_text_is_rejected():
    revised = copy.deepcopy(script())
    revised["scenes"][0]["seconds"] = "three"
    problems = revise.validate(ASSET, {"revised": revised}, script())
    assert any("scenes[0].seconds" in p for p in problems)


def test_a_strategy_revision_that_leaves_objections_alone_passes():
    """PINNED REGRESSION. campaign_strategies stores objections as a list of
    OBJECTS. The old validator demanded lists of strings, so it reported a
    problem on every strategy revision, retried, reported again and raised --
    which means the Edit button on a strategy had never once worked."""
    previous = {"core_message": "m", "positioning": "p",
                "pain_points": ["a"], "benefits": ["b"], "proof_points": ["c"],
                "objections": [{"objection": "o", "response": "r"}],
                "hypothesis": "h"}
    data = {"revised": dict(previous, core_message="a new core message")}
    assert revise.validate(revise.KINDS["strategy"], data, previous) == []


def test_a_malformed_objection_is_still_rejected():
    """The fix must not become a licence to return anything: the entries are
    checked against the shape of the entry already stored."""
    previous = {"core_message": "m", "positioning": "p",
                "pain_points": ["a"], "benefits": ["b"], "proof_points": ["c"],
                "objections": [{"objection": "o", "response": "r"}],
                "hypothesis": "h"}
    data = {"revised": dict(previous, objections=[{"objection": "o"}])}
    problems = revise.validate(revise.KINDS["strategy"], data, previous)
    assert any("response" in p for p in problems)


def test_a_previously_empty_field_may_be_filled_in():
    """A null rationale being written is the point of asking for a revision,
    not a shape violation."""
    previous = {"name": "Commission", "hypothesis": "H", "rationale": None}
    data = {"revised": dict(previous, rationale="Because agents said so.")}
    assert revise.validate(revise.KINDS["angle"], data, previous) == []
    still_empty = {"revised": dict(previous, rationale="   ")}
    assert revise.validate(revise.KINDS["angle"], still_empty, previous)


# --------------------------------------------------------------------------
# The generator's own structural gate
# --------------------------------------------------------------------------

def test_a_valid_script_passes_the_generator_gate():
    assert video_script.validate(script(), 30) == []


def test_the_generator_enforces_the_requested_length():
    """--seconds 15 must not quietly yield a 30-second script."""
    problems = video_script.validate(script(), 15)
    assert any("duration_target_seconds must be 15" in p for p in problems)


@pytest.mark.parametrize("count", [1, 2, 9])
def test_the_shot_count_bounds_are_enforced(count):
    s = copy.deepcopy(script())
    one = s["scenes"][0]
    s["scenes"] = [dict(one, n=i + 1) for i in range(count)]
    assert any("shots" in p for p in video_script.validate(s, 30))


@pytest.mark.parametrize("key", ["cta", "caption"])
def test_the_whole_ad_fields_are_required(key):
    s = copy.deepcopy(script())
    del s[key]
    assert any(key in p for p in video_script.validate(s, 30))


def test_the_generator_enforces_the_caption_limit():
    assert any("caption" in p
               for p in video_script.validate(broken(caption="x" * 200), 30))


def test_the_generator_gate_reuses_the_qa_rules():
    """Not a restatement of them. A generator aiming at one set of numbers
    while QA enforces another is how copy gets rewritten on every QA pass --
    so an undeliverable line must fail HERE, in the same words."""
    s = copy.deepcopy(script())
    s["scenes"][0]["spoken"] = " ".join(["word"] * 20)
    from_generator = video_script.validate(s, 30)
    from_qa = [f.message for f in check_video_script(s, "video_script")]
    assert any(m in from_generator for m in from_qa)


def test_a_non_list_scenes_value_stops_the_gate_early():
    """Returning early matters: every later rule indexes into the list."""
    assert video_script.validate({"scenes": "nope"}, 30) == [
        "scenes must be a list of shot objects"]


# --------------------------------------------------------------------------
# utm_content: two creatives must not share a link
# --------------------------------------------------------------------------

def test_a_video_script_gets_its_own_utm_label():
    assert slot_content("video_script", "A", None, 3) == "vid-a-v3"


def test_the_asset_type_is_what_separates_a_video_from_a_static_ad():
    assert slot_content("meta_ad", "A", None, 3) \
        != slot_content("video_script", "A", None, 3)


def test_an_email_keeps_its_sequence_position():
    assert slot_content("email", "A", 2, 5) == "email-a-2-v5"
