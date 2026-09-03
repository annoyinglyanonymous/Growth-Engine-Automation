"""Stage 7, UGC video scripts.

    python -m generators.video_script "Franchise Recruitment - Captive Agents Q4 2026"
    python -m generators.video_script <ref> --seconds 15 --concept <concept-uuid>
    python -m generators.video_script <ref> --approve <asset-uuid> --approved-by <who>

Writes 2 variants (A/B) per approved concept, each ONE campaign_assets row
holding a shot list:

    {"duration_target_seconds": 30,
     "scenes": [{"n": 1, "seconds": 3, "spoken": ..., "on_screen": ...,
                 "visual_prompt": ...}, ...],
     "cta": ..., "caption": ...}

The output is an input to something else: `visual_prompt` is pasted into an AI
video platform (Higgsfield) to generate the clip, `spoken` is what the person
on camera says, `on_screen` is the burned-in caption. So the shot is the unit,
not the paragraph -- a single block of prose would have to be cut up by hand,
and the cutting is where the timing goes wrong.

ONE ROW PER SCRIPT, NOT ONE PER SHOT
The opposite of email_sequence, and for a stated reason. An email sequence is
N rows because QA must be able to fail email 2 of 3 and a reviewer approves
each step separately. A script's shots are not independently shippable: shot 3
without shot 2 is not a shorter ad, it is a broken one. So the shots nest
inside content and the row keeps position NULL.

That nesting is only safe because validation.checks.fields_of walks into it.
Before 026 it read top-level strings only, and a prohibited claim in
scenes[2].spoken would have passed every deterministic check silently.

WHY THE LENGTH IS AN ARGUMENT AND NOT A CONSTANT
15 and 30 seconds are different ads, not the same ad trimmed -- the 15 has no
room to handle an objection. The operator picks per run, the same way
email_sequence takes --emails.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from db import cursor, pool
from generators.pipeline import (
    StageNotReady,
    approve,
    approved_strategy,
    as_jsonb,
    brief_block,
    context_for,
    load_campaign,
    next_version,
)
from generators.prompting import acomplete_json, to_system_prompt

# Imported, not re-declared: these are pure and meta_ads is their only other
# caller, so a second copy would be a second thing to keep true.
from generators.meta_ads import approved_concepts, concept_block
from validation.checks import (
    CHANNEL_LIMITS,
    HOOK_MAX_SECONDS,
    ON_SCREEN_MAX_CHARS,
    WORDS_PER_SECOND,
    check_character_limits,
    check_video_script,
)

#: Imported, not redefined -- see the note in generators/meta_ads.py.
LIMITS = CHANNEL_LIMITS[("meta_ads", "video_script")]

#: The lengths on offer. Not a free-form integer: these are the durations the
#: platforms and the team actually cut for, and a 37-second target would just
#: be a number nobody chose.
SECONDS_CHOICES = (15, 30, 45, 60)

#: Shot count bounds. Below 3 there is no room for hook / proof / ask; above 8
#: the shots are too short to generate anything watchable from.
MIN_SCENES, MAX_SCENES = 3, 8

VARIANTS = ("A", "B")

INSTRUCTIONS_TMPL = """\
Write ONE user-generated-content (UGC) video ad script from the concept below,
to run as a Meta ad on Facebook and Instagram Reels.

It is a {seconds}-second script, told in {min_scenes} to {max_scenes} shots.

This is a person talking to their phone camera, not a brand talking to a
market. Write what they SAY:
- Contractions, plain words, one idea per shot. Read it aloud in your head; if
  it sounds like a press release, it is wrong.
- No "Introducing", no "Are you tired of", no "game-changing", no slogan
  voice, no exclamation pile-ups, no ALL CAPS.
- The speaker is a licensed insurance agent talking to other agents. They may
  describe their own working experience. They may NOT claim an outcome that is
  not in the approved claims.

Per shot:
- n: the shot number, starting at 1, no gaps.
- seconds: how long the shot runs. Shot 1 is at most {hook_seconds} seconds --
  on Reels the first {hook_seconds} seconds decide whether the rest is
  watched. The shot lengths must add up to about {seconds}.
- spoken: the line said aloud in this shot. At most {wps} words per second of
  that shot's length -- a {hook_seconds}-second shot holds about
  {hook_words} words. This is a hard constraint, not a style note: a line
  that cannot be said in the time does not get said.
- on_screen: the burned-in caption for this shot, at most {on_screen} chars.
  It reinforces the spoken line; it does not repeat it word for word.
- visual_prompt: what to generate for this shot, written as a prompt for an AI
  video tool. Describe the shot: framing, who is in it, where, the light, what
  they are doing. Vertical 9:16, handheld, natural light, real texture.
  * Do NOT name or describe a real, identifiable person.
  * Do NOT frame the speaker as a named customer giving a testimonial, and do
    not write review-style set dressing. A generated face presented as a real
    customer is a fabricated endorsement no matter how well sourced the words
    are -- and unlike a wrong figure, nothing downstream can catch it.
  * No on-screen text in the visual prompt; the caption is added in the edit.

Then, for the ad as a whole:
- cta: the ask, in the speaker's voice, matching the campaign's primary CTA in
  intent -- a qualifying conversation, not a purchase.
- caption: the feed caption, at most {caption} characters.

Every figure spoken or captioned must come from the approved claims, with any
required disclaimer carried in the same shot. If the concept's hook implies a
figure that is not approved, express the idea without the number.

The variant instruction below says how this variant must differ.
"""

SHAPE_HINT = """\
Keys and types:
{
  "duration_target_seconds": 30,
  "scenes": [
    {
      "n": 1,
      "seconds": 3,
      "spoken": "string",
      "on_screen": "string",
      "visual_prompt": "string"
    },
    ...
  ],
  "cta": "string",
  "caption": "string"
}"""

VARIANT_TWIST = {
    "A": "Variant A: open on the speaker's own frustration in the first "
         "shot; the benefit arrives as relief. No figure in shot 1.",
    "B": "Variant B: open on the strongest approved figure the concept "
         "supports, said plainly as a fact of the speaker's week; the "
         "frustration is implied.",
}


def instructions(seconds: int) -> str:
    return INSTRUCTIONS_TMPL.format(
        seconds=seconds, min_scenes=MIN_SCENES, max_scenes=MAX_SCENES,
        hook_seconds=HOOK_MAX_SECONDS, wps=WORDS_PER_SECOND,
        hook_words=int(HOOK_MAX_SECONDS * WORDS_PER_SECOND),
        on_screen=ON_SCREEN_MAX_CHARS, caption=LIMITS["caption"])


def validate(data: dict, seconds: int) -> list[str]:
    """Structural problems with the model's answer, as fixable sentences.

    The timing and shot-structure rules are NOT restated here: they are read
    straight out of validation.checks, the same functions stage 8 will run.
    A generator aiming at one set of numbers while QA enforces another is how
    copy ends up rewritten on every QA pass -- the same reasoning that makes
    LIMITS an import rather than a literal.
    """
    problems: list[str] = []

    scenes = data.get("scenes")
    if not isinstance(scenes, list):
        return ["scenes must be a list of shot objects"]
    if not MIN_SCENES <= len(scenes) <= MAX_SCENES:
        problems.append(f"scenes has {len(scenes)} shots; use "
                        f"{MIN_SCENES} to {MAX_SCENES}")

    for key in ("cta", "caption"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            problems.append(f"missing {key}")

    if data.get("duration_target_seconds") != seconds:
        problems.append(f"duration_target_seconds must be {seconds}")

    # The QA rules themselves. Info findings are not problems: they report
    # that something could not be checked, which a retry cannot fix.
    content = dict(data, duration_target_seconds=seconds)
    problems += [f.message for f in check_video_script(content, "video_script")
                 if f.severity != "info"]
    problems += [f.message for f in
                 check_character_limits(content, "meta_ads", "video_script")]
    return problems


async def generate(campaign_ref: str, *, concept_id: str | None = None,
                   seconds: int = 30, provider: str | None = None,
                   show_prompt: bool = False) -> dict:
    if seconds not in SECONDS_CHOICES:
        raise SystemExit(
            f"--seconds {seconds}: choose one of "
            f"{', '.join(str(s) for s in SECONDS_CHOICES)}")

    campaign = await load_campaign(campaign_ref)
    # meta_ads, not a 'video' channel: the ad runs on Meta, so it inherits
    # Meta's utm convention instead of inventing a second one. See 026.
    if "meta_ads" not in (campaign["channels"] or []):
        raise StageNotReady(
            f"campaign channels are {campaign['channels']}; meta_ads is not "
            f"among them, and a UGC video ad runs on meta_ads. Generating "
            f"assets for a channel the brief did not ask for is scope creep "
            f"with a QA cost.")
    await approved_strategy(campaign["id"])  # ordering guard only
    concepts = await approved_concepts(campaign["id"], concept_id)

    ctx = await context_for(
        campaign,
        stage_query=f"{campaign['product_name']} "
                    f"{' '.join(c['hook'] for c in concepts[:3])}",
    )
    system = to_system_prompt(ctx)

    if show_prompt:
        angle = {"name": concepts[0]["angle_name"],
                 "hypothesis": concepts[0]["angle_hypothesis"]}
        print(system, "\n" + "=" * 74 + "\n",
              f"{instructions(seconds)}\n{VARIANT_TWIST['A']}\n\n"
              f"{brief_block(campaign)}\n\n"
              f"{concept_block(concepts[0], angle)}")
        return {}

    written = []
    for concept in concepts:
        angle = {"name": concept["angle_name"],
                 "hypothesis": concept["angle_hypothesis"]}
        for variant in VARIANTS:
            # One call per variant, and the whole shot list in it: the shots
            # build one argument across the ad and refer back to each other.
            # Per-shot calls produce five openers -- the same reasoning
            # email_sequence gives for generating a sequence in one call.
            user = (f"{instructions(seconds)}\n{VARIANT_TWIST[variant]}\n\n"
                    f"{brief_block(campaign)}\n\n"
                    f"{concept_block(concept, angle)}")
            data = await acomplete_json(system, user, shape_hint=SHAPE_HINT,
                                        provider=provider)
            problems = validate(data, seconds)
            if problems:
                data = await acomplete_json(
                    system,
                    f"{user}\n\nYour previous script had these problems: "
                    f"{'; '.join(problems)}. Rewrite it within the "
                    f"constraints -- fewer words per shot, not a faster "
                    f"read.",
                    shape_hint=SHAPE_HINT, provider=provider)
                problems = validate(data, seconds)
                if problems:
                    raise RuntimeError(
                        f"variant {variant} of concept {concept['id']} failed "
                        f"validation twice: {problems}")

            content = {
                "duration_target_seconds": seconds,
                "scenes": [{"n": s["n"], "seconds": s["seconds"],
                            "spoken": s["spoken"], "on_screen": s["on_screen"],
                            "visual_prompt": s["visual_prompt"]}
                           for s in data["scenes"]],
                "cta": data["cta"],
                "caption": data["caption"],
            }

            version = await next_version("campaign_assets", campaign["id"])
            async with cursor() as cur:
                await cur.execute(
                    "insert into public.campaign_assets "
                    "  (campaign_id, concept_id, channel, asset_type, "
                    "   variant, position, content, version_number, status, "
                    "   knowledge_snapshot, generator_version) "
                    "values (%s, %s, 'meta_ads', 'video_script', %s, null, "
                    "        %s, %s, 'draft', %s, 'video_script/1') "
                    "returning id",
                    (campaign["id"], concept["id"], variant,
                     as_jsonb(content), version, as_jsonb(ctx["snapshot"])))
                row = await cur.fetchone()

            written.append({
                "asset_id": str(row["id"]),
                "concept": concept["hook"][:48],
                "variant": variant,
                "version": version,
                "seconds": seconds,
                "shots": len(content["scenes"]),
                "hook": content["scenes"][0]["spoken"],
                "runtime": sum(s["seconds"] for s in content["scenes"]),
            })

    return {"campaign": campaign["name"], "target_seconds": seconds,
            "assets": written}


async def amain() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("campaign", help="campaign UUID or exact name")
    ap.add_argument("--concept", default=None,
                    help="generate for this one approved concept only")
    ap.add_argument("--seconds", type=int, default=30,
                    choices=SECONDS_CHOICES,
                    help="target length of the finished video")
    ap.add_argument("--provider", default=None, choices=["gemini", "openai"])
    ap.add_argument("--show-prompt", action="store_true")
    ap.add_argument("--approve", metavar="ASSET_ID", default=None,
                    help="approve ONE script (approval is per slot)")
    ap.add_argument("--approved-by", default=None)
    args = ap.parse_args()

    await pool.open()
    try:
        if args.approve:
            if not args.approved_by:
                raise SystemExit("--approve requires --approved-by")
            result = await approve("campaign_assets", args.approve,
                                   args.approved_by)
        else:
            result = await generate(args.campaign, concept_id=args.concept,
                                    seconds=args.seconds,
                                    provider=args.provider,
                                    show_prompt=args.show_prompt)
        if result:
            print(json.dumps(result, indent=2, ensure_ascii=False))
    finally:
        await pool.close()
    return 0


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())
