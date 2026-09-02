-- ---------------------------------------------------------------------------
-- 025  Tracked destination URLs
--
-- THE GAP THIS CLOSES
-- The brief REQUIRES a primary_kpi: validation blocks without it, and it goes
-- into every generation prompt. Nothing ever measures it. The precondition
-- for ever answering "how did variant B perform" is that each published asset
-- points at a URL that names the asset -- and today no URL exists anywhere in
-- the system: campaigns has no destination field and asset content carries
-- only copy.
--
-- TWO COLUMNS, TWO DIFFERENT MOMENTS
--   campaigns.destination_url   -- where this campaign's traffic lands.
--                                  Filed on the brief, because the person
--                                  filing it is the one who knows.
--   campaign_assets.tracked_url -- that URL with utm_source / utm_medium /
--                                  utm_campaign / utm_content appended,
--                                  derived per slot and version.
--
-- tracked_url is STAMPED AT APPROVAL, not computed on read. An approved
-- asset's link is part of what was approved: if the campaign is renamed a
-- month after launch, the URL that actually shipped must not silently drift
-- to a utm_campaign value that never ran. Same reasoning as knowledge_snapshot
-- -- the row records what was true when the decision was made.
--
-- Both columns are nullable, deliberately:
--   * a campaign with no destination (pure brand awareness, or filed before
--     this migration) still validates -- stage 3 warns rather than blocks,
--     because re-validating an existing campaign must not fail retroactively;
--   * assets approved before this migration keep tracked_url null. There was
--     no destination to derive from, and inventing one now would claim a link
--     shipped that never did. The UI says "no tracked link" instead.
--
-- The derivation itself (slug rules, the channel -> utm_source/medium map,
-- query-string merging) lives in tracking.py, in one pure function, so the
-- stamp and any preview always agree.
--
-- REVERSIBLE
--   alter table public.campaigns
--       drop constraint campaigns_destination_url_shape,
--       drop column destination_url;
--   alter table public.campaign_assets drop column tracked_url;
-- ---------------------------------------------------------------------------

alter table public.campaigns
    add column if not exists destination_url text;

-- Shape only, not reachability. 'http(s)://' is the one property a typo is
-- likely to violate and the one thing every downstream consumer assumes; a
-- full URL grammar in a CHECK would reject working URLs over pedantry.
do $$
begin
    if not exists (select 1 from pg_constraint
                    where conname = 'campaigns_destination_url_shape')
    then
        alter table public.campaigns
            add constraint campaigns_destination_url_shape
            check (destination_url is null
                   or destination_url ~ '^https?://');
    end if;
end $$;

alter table public.campaign_assets
    add column if not exists tracked_url text;

comment on column public.campaigns.destination_url is
    'Where this campaign''s traffic lands. Tagged per asset into '
    'campaign_assets.tracked_url at approval (025).';

comment on column public.campaign_assets.tracked_url is
    'destination_url with utm parameters for this slot and version, stamped '
    'at approval so the recorded link cannot drift after shipping (025). '
    'Null: approved before 025, or the brief has no destination_url.';
