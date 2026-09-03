-- ---------------------------------------------------------------------------
-- 026  UGC video scripts as an asset type
--
-- THE GAP THIS CLOSES
-- The team produces UGC video ads on AI video platforms (Higgsfield), and
-- those platforms take a SCRIPT as input: per shot, a spoken line, an
-- on-screen caption and a visual prompt. Stage 7 writes static Meta ads and
-- email sequences only, so that script is written by hand -- outside the
-- claim governance, the exclusion lists and the QA tier that exist precisely
-- so a figure nobody approved cannot ship. A video ad is the one format where
-- an unapproved number is hardest to retract afterwards.
--
-- ONE NEW asset_type, NO NEW channel
-- The video runs as a Meta ad (Facebook/Instagram Reels), so channel stays
-- 'meta_ads'. That is not a shortcut: it means the asset inherits the
-- facebook / paid_social utm convention already in tracking.CHANNEL_UTM
-- rather than inventing a second one, it needs no new brief checkbox, and it
-- passes the generator's existing "meta_ads is among the brief's channels"
-- gate. If TikTok or YouTube is added later, THAT is a genuinely new channel
-- with its own utm source, and it gets decided then rather than guessed now.
--
-- ONE ROW PER SCRIPT, position NULL
-- Unlike an email sequence -- N rows sharing a variant, differing by position,
-- so QA can fail email 2 of 3 -- a script's shots are not independently
-- shippable. A reviewer approves or rejects a whole script, so the shots live
-- nested in content.scenes and the row keeps position NULL. This needs no
-- change to campaign_assets_position_required_for_email, which reads
-- (asset_type = 'email') = (position is not null): false = false holds.
-- Several scripts for one concept are variants A/B, as with meta ads.
--
-- WHY A DROP-AND-ADD AND NOT AN ALTER
-- Postgres cannot amend a CHECK in place. The constraint was created inline
-- by 013, so it carries the auto-generated name campaign_assets_asset_type_check
-- (confirmed against pg_constraint before writing this). Dropping and
-- re-adding it with the full vocabulary is the only way, and it is why the
-- full list is repeated here rather than appended to.
--
-- A NOTE ON WHAT THIS MIGRATION DOES NOT DO
-- The nested content shape is only governable because validation.checks
-- fields_of walks nested structures as of this change. Before it did, every
-- deterministic check read top-level strings only and a prohibited claim in
-- scenes[2].spoken would have passed QA silently. The schema cannot enforce
-- that; it is stated here so the connection is not lost.
--
-- REVERSIBLE (only while no video_script rows exist; delete them first)
--   alter table public.campaign_assets
--       drop constraint campaign_assets_asset_type_check;
--   alter table public.campaign_assets
--       add constraint campaign_assets_asset_type_check
--       check (asset_type in ('meta_ad', 'email', 'google_ad',
--                             'landing_page_section', 'sms'));
-- ---------------------------------------------------------------------------

-- Idempotent: re-running must not fail, and must not drop the constraint and
-- leave it off if the add half is skipped.
do $$
begin
    if not exists (
        select 1 from pg_constraint
         where conname = 'campaign_assets_asset_type_check'
           and conrelid = 'public.campaign_assets'::regclass
           -- strpos and not LIKE: a percent sign anywhere in migration
           -- SQL becomes a placeholder the moment anyone passes psycopg
           -- parameters alongside it. Avoided rather than relied upon.
           and strpos(pg_get_constraintdef(oid), 'video_script') > 0)
    then
        alter table public.campaign_assets
            drop constraint if exists campaign_assets_asset_type_check;
        alter table public.campaign_assets
            add constraint campaign_assets_asset_type_check
            check (asset_type in ('meta_ad', 'email', 'google_ad',
                                  'landing_page_section', 'sms',
                                  'video_script'));
    end if;
end $$;

comment on column public.campaign_assets.asset_type is
    'What kind of asset this row is. video_script (026) holds a UGC shot '
    'list in content: {duration_target_seconds, scenes: [{n, seconds, '
    'spoken, on_screen, visual_prompt}], cta, caption}. One row per script, '
    'position null -- the shots are reviewed together, not individually. '
    'There is no separate hook field: the hook IS scenes[0].spoken, and '
    'storing it twice is a value two revisions could disagree about.';
