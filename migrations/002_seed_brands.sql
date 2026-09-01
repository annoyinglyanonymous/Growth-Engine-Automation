-- 002_seed_brands.sql
--
-- The loader resolves brand_slug -> public.brands.id. If rows for these two
-- slugs do not exist, step 7 fails on every document.
--
-- ON CONFLICT DO NOTHING makes this safe and idempotent: if you have already
-- created these brands (with better names or descriptions than these), nothing
-- here touches them. brands.slug is UNIQUE, which is what makes that work.
--
-- If your brands table already has these rows, this migration is a no-op and
-- you can skip it entirely.

begin;

insert into public.brands (name, slug, description, status)
values
    ('Renegade Insurance', 'renegade',
     'P&C insurance company with a retail agency network; sells coverage to '
     'consumers and agency ownership to agents.',
     'active'),
    ('Agency Height', 'agencyheight',
     'B2B platform selling carrier market access, leads and CRM tools to '
     'independent insurance agents. Does not sell insurance.',
     'active')
on conflict (slug) do nothing;

commit;
