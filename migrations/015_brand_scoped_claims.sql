-- 015_brand_scoped_claims.sql
--
-- Makes a claim belong to a BRAND, optionally narrowed to a product.
--
-- THE DEFECT THIS FIXES
-- claims.product_id was NOT NULL with no brand_id, so every governed fact had
-- to be parented to some product. Renegade has a flagship product to hang
-- things on, so 012 worked around it: "Renegade is licensed for P&C in 48
-- states", "Renegade has 200+ agents", "Renegade operates 9 open retail
-- locations" and five more were filed under `franchise-program` with
-- category='company'. None of those are facts about the franchise programme.
-- They are facts about the company, and the workaround has two live costs:
--
--   * A product-narrowed query drops them. A franchise brief asking for
--     franchise claims does not see "48 states", which is one of the strongest
--     legitimate proof points the brand owns.
--   * They inherit the wrong gate. If franchise-program were ever set
--     approved_for_marketing = false, "Renegade has 200+ agents" would
--     silently stop being assertable -- a company fact switched off by a
--     product decision it has nothing to do with.
--
-- 012's own comment called this out and said the fix "belongs in 013". It did
-- not make it into 013. Agency Height is what forces it: that brand has zero
-- products, so on the current schema it cannot hold a single claim. Not
-- "should not" -- CANNOT, the insert fails on a NOT NULL.
--
-- brand_rules already has the right shape (brand_id NOT NULL, product_id
-- nullable). claims had it backwards. This aligns them.
--
-- WHY A COMPOSITE FOREIGN KEY
-- Once both columns exist they can disagree: a claim could name Renegade as
-- its brand and an Agency Height product. A CHECK cannot see the products
-- table, so the guard is the denormalisation pattern 013 already uses --
-- unique (id, brand_id) on products, and a composite FK from claims. Under the
-- default MATCH SIMPLE, a composite FK with any NULL column is not enforced,
-- which is exactly the behaviour a brand-level claim needs: product_id NULL
-- skips the product check while brand_id stays enforced by its own FK.
--
-- Re-runnable. Every step is guarded, so applying twice is a no-op.

begin;

-- --------------------------------------------------------------------------
-- 1. brand_id, backfilled from the product that used to be mandatory
-- --------------------------------------------------------------------------

alter table public.claims
    add column if not exists brand_id uuid;

update public.claims c
   set brand_id = p.brand_id
  from public.products p
 where p.id = c.product_id
   and c.brand_id is null;

-- Any row still null here has no product to inherit from, which on the old
-- schema was impossible. Fail loudly rather than weaken the NOT NULL.
do $$
declare orphans integer;
begin
    select count(*) into orphans from public.claims where brand_id is null;
    if orphans > 0 then
        raise exception
            'cannot set claims.brand_id NOT NULL: % row(s) have no brand and '
            'no product to infer one from. Resolve them first.', orphans;
    end if;
end $$;

alter table public.claims
    alter column brand_id set not null;

do $$
begin
    if not exists (select 1 from pg_constraint
                    where conname = 'claims_brand_id_fkey') then
        alter table public.claims
            add constraint claims_brand_id_fkey
            foreign key (brand_id) references public.brands (id)
            on delete cascade;
    end if;
end $$;

-- --------------------------------------------------------------------------
-- 2. product_id becomes optional, and consistent with brand_id when present
-- --------------------------------------------------------------------------

alter table public.claims
    alter column product_id drop not null;

-- Composite FK target. products already has unique (brand_id, slug); this adds
-- the (id, brand_id) pair the FK below needs.
do $$
begin
    if not exists (select 1 from pg_constraint
                    where conname = 'products_id_brand_id_key') then
        alter table public.products
            add constraint products_id_brand_id_key unique (id, brand_id);
    end if;
end $$;

-- Replace the single-column product FK with the pair. Same ON DELETE CASCADE
-- as before, so deleting a product still removes its product-level claims;
-- brand-level claims have product_id NULL and are untouched by that.
do $$
begin
    if exists (select 1 from pg_constraint
                where conname = 'claims_product_id_fkey') then
        alter table public.claims drop constraint claims_product_id_fkey;
    end if;
    if not exists (select 1 from pg_constraint
                    where conname = 'claims_product_brand_fkey') then
        alter table public.claims
            add constraint claims_product_brand_fkey
            foreign key (product_id, brand_id)
            references public.products (id, brand_id)
            on delete cascade;
    end if;
end $$;

-- A feature belongs to a product, so a claim cannot name a feature without
-- naming the product it belongs to.
do $$
begin
    if not exists (select 1 from pg_constraint
                    where conname = 'claims_feature_requires_product') then
        alter table public.claims
            add constraint claims_feature_requires_product
            check (feature_id is null or product_id is not null);
    end if;
end $$;

-- --------------------------------------------------------------------------
-- 3. A natural key, so seeds can be idempotent
-- --------------------------------------------------------------------------
-- claims had no unique key at all, which is why 012 and 014 guard every insert
-- with `where not exists`. That pattern is correct but fragile: it silently
-- does nothing when the text is edited by a character, leaving two rows with
-- the same meaning and possibly different statuses -- a governance bug that
-- reads as a typo.
--
-- NULLS NOT DISTINCT (PG 15+; this database is 17.6) is what makes it work for
-- brand-level rows. Under the default, every product_id NULL is distinct from
-- every other, so two identical brand-level claims would not collide.

create unique index if not exists claims_natural_key
    on public.claims (brand_id, product_id, claim_text) nulls not distinct;

-- Retrieval now filters on brand first and product second.
-- idx_claims_product_id stays useful for validation/context.py's product path.
create index if not exists idx_claims_brand_status
    on public.claims (brand_id, status);

-- --------------------------------------------------------------------------
-- 4. Re-parent the eight Renegade claims that were never product claims
-- --------------------------------------------------------------------------
-- Scoped narrowly and on purpose: category='company' AND currently parented to
-- franchise-program AND carrying no feature_id. Every one of the eight is a
-- statement about Renegade Insurance the company:
--
--   approved    Renegade has 200+ agents.
--   approved    ... licensed for Property and Casualty in 48 states and DC
--   approved    ... operates 9 open retail agency locations ...
--   prohibited  Renegade is licensed in all 50 states.
--   prohibited  Renegade provides quotes from 100 insurance companies.
--   restricted  Renegade holds a 4.7 out of 5 Google rating.
--   restricted  ... 2026 Global Recognition Award winner.
--   restricted  ... Great Place to Work Certified company.
--
-- brand_id is already correct from the backfill, so this only drops the
-- product parent. The two prohibited rows keep their trigger_phrases and
-- become brand-wide blockers, which is what they always should have been:
-- "licensed in all 50 states" is false in an M&A email too.

update public.claims c
   set product_id = null
  from public.products p, public.brands b
 where p.id = c.product_id
   and b.id = p.brand_id
   and b.slug = 'renegade'
   and p.slug = 'franchise-program'
   and c.category = 'company'
   and c.feature_id is null;

-- If a company-category claim still has a product parent it had a feature_id
-- and needs a human decision, not a silent skip.
do $$
declare stuck integer;
begin
    select count(*) into stuck
      from public.claims c
      join public.products p on p.id = c.product_id
     where c.category = 'company';
    if stuck > 0 then
        raise warning
            'REVIEW: % claim(s) with category=company still have a product '
            'parent (they carry a feature_id). Re-parent them by hand.', stuck;
    end if;
end $$;

comment on column public.claims.brand_id is
    'Owning brand. Always set. A claim is brand-level when product_id is null.';
comment on column public.claims.product_id is
    'Narrows the claim to one product. Null means it applies brand-wide -- a '
    'fact about the company rather than about something it sells.';

commit;
