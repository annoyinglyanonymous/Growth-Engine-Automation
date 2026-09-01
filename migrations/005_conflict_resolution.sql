-- 005_conflict_resolution.sql
--
-- Conflicts had no lifecycle: the only way to retire one was to edit
-- KB/conflicts.json and re-run, which loses the record of what was
-- contradicted and what the answer turned out to be.
--
-- A resolved conflict is MORE useful to an agent than an unresolved one, not
-- less. Unresolved says "the sites disagree, quote neither". Resolved says
-- "the figure is X; Y still appears in the corpus and is stale, never write
-- it". The second is an instruction; the first is only a warning. So resolved
-- conflicts stay in the table and keep being surfaced.
--
-- severity is unchanged and keeps its meaning: the consequence of getting it
-- wrong. status is orthogonal: whether we know the answer yet. franchise-fee
-- stays severity='blocker' after resolution, because writing $20,000 is still
-- a blocker -- we now simply know which figure is the bad one.

begin;

alter table kb.conflicts
    add column status       text        not null default 'active'
                            check (status in ('active', 'resolved')),
    -- The verified value, as it should be written. Prose, not numeric: some
    -- resolutions are "the 11 named carriers", not a scalar.
    add column correct_value text,
    -- Figures now known to be wrong and still present in the corpus. A plain
    -- string array so a validator can substring-match campaign copy without
    -- parsing anything.
    add column stale_values  text[]     not null default '{}',
    add column resolution    text,
    add column resolved_at   timestamptz,
    add column resolved_by   text;

-- A resolved row without a resolution is a lie the table would keep telling.
alter table kb.conflicts
    add constraint conflicts_resolution_complete check (
        status = 'active'
        or (resolution is not null and resolved_at is not null)
    );

comment on column kb.conflicts.status is
    'active = sites disagree and we do not know which is right. '
    'resolved = we know; correct_value holds the answer and stale_values holds '
    'the figures still living in the corpus that must never be written.';

comment on column kb.conflicts.stale_values is
    'Substring-matchable. A validator rejects campaign copy containing any of '
    'these for this brand.';


-- ---------------------------------------------------------------------------
-- kb.brand_context -- now also reads public.brand_rules.
--
-- Recording an approved figure and never wiring it into what the agent reads
-- is filing paperwork. brand_rules is the authority side: kb.conflicts says
-- what the SITES claim, brand_rules says what you MAY WRITE. The agent needs
-- both, and it needs to be able to tell them apart -- so they stay separate
-- keys rather than being merged into one list.
-- ---------------------------------------------------------------------------
create or replace function kb.brand_context(p_brand_slug text)
returns jsonb
language sql
stable
as $$
    select jsonb_build_object(
        'brand_slug', b.slug,
        'brand_name', b.name,
        'primer', (
            select c.text
            from kb.documents d
            join kb.chunks c on c.doc_id = d.doc_id
            where d.brand_id = b.id and d.kind = 'primer'
            order by c.ordinal
            limit 1
        ),
        -- Unresolved first, then by severity: an open blocker is the single
        -- most important thing in this payload.
        'conflicts', coalesce((
            select jsonb_agg(jsonb_build_object(
                       'topic', k.topic,
                       'detail', k.detail,
                       'severity', k.severity,
                       'status', k.status,
                       'correct_value', k.correct_value,
                       'stale_values', k.stale_values,
                       'resolution', k.resolution,
                       'sources', k.sources)
                   order by
                       case k.status when 'active' then 0 else 1 end,
                       case k.severity
                           when 'blocker' then 0
                           when 'warning' then 1
                           else 2
                       end,
                       k.topic)
            from kb.conflicts k
            where k.brand_id = b.id
        ), '[]'::jsonb),
        -- Approved, human-governed. Nothing in the ingestion pipeline can
        -- write here; that is the point of the separation.
        'rules', coalesce((
            select jsonb_agg(jsonb_build_object(
                       'category', r.category,
                       'rule', r.rule_text,
                       'severity', r.severity,
                       'good_example', r.good_example,
                       'bad_example', r.bad_example)
                   order by
                       case r.severity
                           when 'blocker' then 0
                           when 'warning' then 1
                           else 2
                       end,
                       r.category)
            from public.brand_rules r
            where r.brand_id = b.id and r.status = 'active'
        ), '[]'::jsonb),
        'corpus', jsonb_build_object(
            'documents', (select count(*) from kb.documents where brand_id = b.id),
            'chunks',    (select count(*) from kb.chunks    where brand_id = b.id),
            'newest_page', (select max(date_modified) from kb.documents
                            where brand_id = b.id),
            'open_conflicts', (select count(*) from kb.conflicts
                               where brand_id = b.id and status = 'active')
        )
    )
    from public.brands b
    where b.slug = p_brand_slug;
$$;

comment on function kb.brand_context is
    'Always-inject context for a brand: hand-written primer, documented site '
    'contradictions (with resolutions where known), active brand rules, and '
    'corpus stats.';

commit;
