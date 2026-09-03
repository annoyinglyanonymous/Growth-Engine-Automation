"""End-to-end proof of 026, inside a transaction that is ROLLED BACK.

    python scripts/verify_026.py

Run this AFTER `python migrate.py` has applied 026. Everything it does runs
against the real schema and the real constraints and is then discarded --
inserting a script and approving it are both writes, and approving anything
is the operator's call, so nothing here is allowed to persist.

What it proves, in order:
  1. the widened CHECK accepts asset_type = 'video_script'
  2. it still refuses a type nobody declared
  3. a script row survives the position constraint with position NULL
  4. approval stamps a tracked link whose utm_content says 'vid'
  5. the collision is gone: the sibling static ad in the same slot, at the
     same variant and version, gets a DIFFERENT url -- and both are approved
     at once, which is what made the old behaviour dangerous
  6. QA reads the spoken lines inside the shots (the silent hole 026 closed)
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import contextlib  # noqa: E402
import json  # noqa: E402

import db  # noqa: E402
import tracking  # noqa: E402
from generators import pipeline  # noqa: E402
from validation.checks import CheckContext, run_asset_checks  # noqa: E402

BASE = "https://renegadeinsurance.com/franchise?ref=partner#apply"

SCRIPT = {
    "duration_target_seconds": 30,
    "scenes": [
        {"n": 1, "seconds": 3,
         "spoken": "I quoted fourteen carriers before lunch.",
         "on_screen": "14 carriers. Before lunch.",
         "visual_prompt": "handheld selfie, agent at a desk, warm light, 9:16"},
        {"n": 2, "seconds": 13,
         "spoken": "It used to take me the better part of a week, and half of "
                   "that was re-typing the same forms into different portals.",
         "on_screen": "Same forms. Six portals.",
         "visual_prompt": "over-shoulder of a laptop, paperwork beside it"},
        {"n": 3, "seconds": 14,
         "spoken": "Now it is one submission, and I spend the rest of the day "
                   "actually talking to people who want cover.",
         "on_screen": "One submission",
         "visual_prompt": "agent on the phone, relaxed, natural light"},
    ],
    "cta": "Book a fifteen minute licensing call",
    "caption": "Quoting used to take my whole week.",
}


async def main() -> int:
    await db.pool.open()
    conn = await db.pool.getconn()
    try:
        await conn.set_autocommit(False)
        shared = conn.cursor()

        @contextlib.asynccontextmanager
        async def one_cursor():
            yield shared

        async def fetch_all(sql, params=()):
            await shared.execute(sql, params)
            return await shared.fetchall()

        async def fetch_one(sql, params=()):
            await shared.execute(sql, params)
            return await shared.fetchone()

        pipeline.cursor = one_cursor
        pipeline.fetch_all = fetch_all
        pipeline.fetch_one = fetch_one

        camp = await fetch_one(
            "select id, name from public.campaigns order by created_at "
            "limit 1")
        cid = str(camp["id"])
        print(f"campaign: {camp['name']}\n")

        await shared.execute(
            "update public.campaigns set destination_url = %s where id = %s",
            (BASE, cid))

        # A concept is mandatory by construction (013 makes concept_id NOT
        # NULL), so borrow the one the existing assets were written from.
        sibling = await fetch_one(
            "select concept_id, variant, version_number "
            "from public.campaign_assets "
            "where campaign_id = %s and asset_type = 'meta_ad' "
            "order by version_number desc limit 1", (cid,))
        if not sibling:
            print("no existing meta_ad to compare against; run the meta ads "
                  "generator first")
            return 1
        variant, version = sibling["variant"], sibling["version_number"]

        print("1. the widened CHECK accepts a video_script row")
        await shared.execute(
            "insert into public.campaign_assets "
            "  (campaign_id, concept_id, channel, asset_type, variant, "
            "   position, content, version_number, status, "
            "   knowledge_snapshot, generator_version) "
            "values (%s, %s, 'meta_ads', 'video_script', %s, null, %s, %s, "
            "        'review', %s, 'verify_026') "
            "returning id",
            (cid, sibling["concept_id"], variant, json.dumps(SCRIPT), version,
             json.dumps({"kb_chunk_ids": [], "approved_claims": ["x"],
                         "prohibited_claims": []})))
        video_id = str((await shared.fetchone())["id"])
        print(f"   inserted {video_id}, position NULL\n")

        print("2. it still refuses a type nobody declared")
        await shared.execute("savepoint s1")
        try:
            await shared.execute(
                "update public.campaign_assets set asset_type = 'tiktok_clip' "
                "where id = %s", (video_id,))
            print("   ACCEPTED  <-- wrong\n")
        except Exception as exc:
            print(f"   refused: {type(exc).__name__}\n")
        finally:
            await shared.execute("rollback to savepoint s1")

        print("3. approve the script and the sibling static ad")
        await shared.execute(
            "update public.campaign_assets set status = 'review' "
            "where campaign_id = %s and asset_type = 'meta_ad' "
            "  and variant = %s and version_number = %s",
            (cid, variant, version))
        ad_row = await fetch_one(
            "select id from public.campaign_assets "
            "where campaign_id = %s and asset_type = 'meta_ad' "
            "  and variant = %s and version_number = %s",
            (cid, variant, version))
        for label, row_id in (("video", video_id), ("ad", str(ad_row["id"]))):
            r = await pipeline.approve("campaign_assets", row_id, "verify-026")
            print(f"   {label:5} -> approved, {r['superseded']} superseded")
        print()

        print("4. both are approved at once, with DIFFERENT links")
        rows = await fetch_all(
            "select asset_type, variant, version_number, tracked_url "
            "from public.campaign_assets "
            "where campaign_id = %s and status = 'approved' "
            "  and variant = %s and version_number = %s "
            "order by asset_type", (cid, variant, version))
        for r in rows:
            expected = tracking.tracked_url(
                BASE, campaign_name=camp["name"], channel="meta_ads",
                asset_type=r["asset_type"], variant=r["variant"],
                position=None, version=r["version_number"])
            verdict = "MATCHES" if r["tracked_url"] == expected else "DIFFERS"
            print(f"   {r['asset_type']:13} v{r['version_number']} {verdict}")
            print(f"      {r['tracked_url']}")
        links = {r["tracked_url"] for r in rows}
        print(f"\n   {len(rows)} approved in one slot, "
              f"{len(links)} distinct links "
              f"-- {'no collision' if len(links) == len(rows) else 'COLLIDING'}"
              f"\n")

        print("5. the stray param and the fragment survived")
        one = next(iter(links))
        print(f"   ref=partner: {'ref=partner' in one}   "
              f"#apply: {one.endswith('#apply')}\n")

        print("6. QA reads the spoken lines inside the shots")
        ctx = CheckContext(
            campaign={"do_not_mention": []},
            exclusions=[{"phrase": "before lunch", "note": "probe only",
                         "scope": "brand"}])
        findings = run_asset_checks(SCRIPT, "meta_ads", "video_script", ctx)
        hits = [f for f in findings if f.check == "excluded_wording"]
        for f in hits:
            print(f"   {f.severity}: {f.field_name} -- {f.evidence!r}")
        print(f"   {'the shot text IS scanned' if hits else 'NOTHING CAUGHT'}")
        return 0
    finally:
        await conn.rollback()
        print("\n>>> ROLLED BACK -- nothing above persists <<<")
        await db.pool.putconn(conn)
        await db.pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
