"""Shared plumbing for the stage generators.

What lives here and why:

  load_campaign      one JOIN, reused by every stage, and the place where
                     "which product, which campaign type" is resolved once.
  next_version       version numbers are allocated from the database, not
                     counted in Python, so two concurrent generations cannot
                     both write V2.
  approve            the two-step dance (supersede the old, approve the new)
                     in one transaction, matching the partial unique index in
                     013 that allows at most one approved row at a time.
  context_for        the one place a campaign is turned into a retrieval
                     query and a build_context call.

Generators are async because build_context is async; they run on db.py's pool
and therefore under the same Windows selector-loop requirement as main.py --
import db before creating any event loop.
"""

from __future__ import annotations

import json

import tracking
from db import cursor, fetch_all, fetch_one
from kb_context import build_context


class CampaignNotFound(LookupError):
    pass


class StageNotReady(RuntimeError):
    """The upstream stage this generator depends on has no approved row.

    Named loudly because the pipeline's ordering is a process guarantee: angles
    from an unapproved strategy are angles someone will have to regenerate, and
    assets from an unapproved concept violate the PDF's core rule. The schema
    enforces existence (NOT NULL foreign keys); this enforces approval.
    """


async def load_campaign(campaign_ref: str) -> dict:
    """Campaign plus its brand, product and campaign-type context.

    campaign_ref is a UUID or an exact name. Name lookup exists for the CLI --
    nobody types UUIDs -- but is exact rather than fuzzy: generating against
    the wrong campaign because of a LIKE match would be expensive.
    """
    row = await fetch_one(
        "select c.*, "
        "       b.slug as brand_slug, b.name as brand_name, "
        "       p.slug as product_slug, p.name as product_name, "
        "       p.approved_for_marketing as product_marketable, "
        "       ct.slug as campaign_type_slug, ct.name as campaign_type_name, "
        "       ct.allowed_themes, ct.prohibited_themes "
        "from public.campaigns c "
        "join public.brands b on b.id = c.brand_id "
        "join public.products p on p.id = c.product_id "
        "left join public.campaign_types ct on ct.id = c.campaign_type_id "
        "where c.id::text = %s or c.name = %s",
        (campaign_ref, campaign_ref),
    )
    if not row:
        known = await fetch_all(
            "select name, status from public.campaigns order by created_at desc "
            "limit 10")
        raise CampaignNotFound(
            f"no campaign {campaign_ref!r}. recent: "
            f"{[(r['name'], r['status']) for r in known]}")
    return row


async def next_version(table: str, campaign_id: str) -> int:
    """max(version_number) + 1, from the database at write time.

    table is interpolated from a fixed allowlist -- it arrives from our own
    code, but string-formatting SQL is a habit that should hurt a little.
    """
    assert table in ("campaign_strategies", "campaign_assets"), table
    row = await fetch_one(
        f"select coalesce(max(version_number), 0) + 1 as v "
        f"from public.{table} where campaign_id = %s",
        (campaign_id,),
    )
    return row["v"]


async def context_for(campaign: dict, *, stage_query: str,
                      token_budget: int | None = None) -> dict:
    """build_context, scoped the way a campaign should be.

    The query is the stage's own question plus the brief's audience and
    problem, because those two fields carry the campaign's actual vocabulary.
    product_slug scopes claims to the campaign's product -- a franchise brief
    must not see M&A claims (the product gate in kb_context.fetch_claims
    handles approval; this handles relevance).
    """
    query = " ".join(filter(None, [
        stage_query,
        campaign.get("target_audience") or "",
        campaign.get("customer_problem") or "",
    ]))[:500]
    ctx = await build_context(
        campaign["brand_slug"], query,
        token_budget=token_budget,
        product_slug=campaign["product_slug"],
    )
    # build_context knows the brand, not the campaign, so the brief's
    # own do_not_mention is added here -- the one place every
    # generator already passes through. Appended rather than merged
    # into the brand list so a blocker can still say which one
    # caught the line.
    ctx["exclusions"] = list(ctx.get("exclusions") or []) + [
        {"phrase": p, "note": None, "scope": "brief"}
        for p in (campaign.get("do_not_mention") or [])
        if (p or "").strip()
    ]
    return ctx


def brief_block(campaign: dict) -> str:
    """The stage-1 brief, rendered for a user message.

    Every generator sends this: the strategy exists to serve THIS brief, not a
    generic one for the product. Only fields with content are rendered --
    "Offer: None" teaches the model to write 'None' into copy.
    """
    fields = [
        ("Campaign", campaign.get("name")),
        ("Campaign type", campaign.get("campaign_type_name")),
        ("Objective", campaign.get("objective")),
        ("Target audience", campaign.get("target_audience")),
        ("Customer problem", campaign.get("customer_problem")),
        ("Offer", campaign.get("offer")),
        ("Primary benefit", campaign.get("primary_benefit")),
        ("Supporting benefits", ", ".join(campaign.get("supporting_benefits")
                                          or [])),
        ("Proof points", ", ".join(campaign.get("proof_points") or [])),
        ("Primary CTA", campaign.get("primary_cta")),
        ("Secondary CTA", campaign.get("secondary_cta")),
        ("Channels", ", ".join(campaign.get("channels") or [])),
        ("Primary KPI", campaign.get("primary_kpi")),
        ("Geographic target", ", ".join(campaign.get("geographic_target")
                                        or [])),
        ("Additional context", campaign.get("additional_context")),
    ]
    lines = ["## Campaign brief"]
    lines += [f"- {label}: {value}" for label, value in fields if value]

    if campaign.get("prohibited_themes"):
        lines += ["", "## Themes prohibited for this campaign type",
                  "Any of these appearing in output is a blocking error:"]
        lines += [f"- {t}" for t in campaign["prohibited_themes"]]
    return "\n".join(lines)


def as_jsonb(value) -> str:
    """psycopg adapts dicts natively only with the jsonb adapter registered;
    explicit dumps keeps the write sites obvious and identical."""
    return json.dumps(value, ensure_ascii=False)


#: What "the previously approved version" means, per table. It is NOT the same
#: scope: a campaign has one approved strategy, but one approved asset PER SLOT
#: (channel, type, variant, sequence position) -- that is exactly what 013's
#: partial unique indexes say. A campaign-wide supersede on assets would retire
#: the whole approved asset pack the moment one ad variant is re-approved.
_SUPERSEDE_SCOPE = {
    "campaign_strategies": ("campaign_id",),
    "campaign_assets": ("campaign_id", "channel", "asset_type", "variant",
                        "position"),
}


async def _tracked_link(cur, row: dict) -> str | None:
    """The utm-tagged link this asset ships with, or None when there is
    nothing to tag.

    None is a normal outcome, not a failure: the brief may have no
    destination_url (pure brand awareness, or filed before 025), and a channel
    without a utm convention (google_ads, until it is built) has no defensible
    source/medium to invent. Approval proceeds either way -- a missing link is
    the UI's hint to show, never a reason a reviewer cannot sign off copy.
    """
    if row["channel"] not in tracking.CHANNEL_UTM:
        return None
    await cur.execute(
        "select name, destination_url from public.campaigns where id = %s",
        (row["campaign_id"],))
    camp = await cur.fetchone()
    if not camp or not camp["destination_url"]:
        return None
    return tracking.tracked_url(
        camp["destination_url"], campaign_name=camp["name"],
        channel=row["channel"], asset_type=row["asset_type"],
        variant=row["variant"], position=row["position"],
        version=row["version_number"])


async def approve(table: str, row_id: str, approved_by: str) -> dict:
    """Approve one version; supersede whatever was approved in its scope.

    One transaction, matching 013's partial unique indexes (at most one
    approved row per scope). Doing the supersede first is not optional: with
    the index in place, approving the new row while the old one is still
    approved is a constraint violation.

    approved_by is recorded because 013's CHECK requires it: an approval with
    no approver is not an approval.

    For an asset, the utm-tagged link is STAMPED here, in the same UPDATE.
    Approval is the moment the link becomes part of what shipped, and stamping
    rather than computing on read means a later campaign rename cannot drift
    the recorded URL away from the one that actually ran -- the same reasoning
    as knowledge_snapshot.
    """
    scope = _SUPERSEDE_SCOPE[table]  # KeyError for an unknown table is correct
    async with cursor() as cur:
        await cur.execute(
            f"select status, version_number, {', '.join(scope)} "
            f"from public.{table} where id = %s for update",
            (row_id,))
        row = await cur.fetchone()
        if not row:
            raise LookupError(f"no {table} row {row_id}")
        if row["status"] == "approved":
            return {"id": row_id, "already": True}

        # "col is not distinct from %s" rather than "=", because position is
        # NULL for non-sequence assets and NULL = NULL is not true.
        where = " and ".join(f"{c} is not distinct from %s" for c in scope)
        await cur.execute(
            f"update public.{table} set status = 'superseded' "
            f"where status = 'approved' and {where}",
            tuple(row[c] for c in scope))
        superseded = cur.rowcount

        # Approving v3 also settles v1. An older version left at
        # 'review' is outstanding work nobody will ever do, it makes
        # the slot read as unfinished, and Approve all would offer to
        # approve it. Strictly older: a NEWER pending version is a
        # real decision someone still owes, and lifecycle's
        # newer_version_pending blocker is what asks for it.
        await cur.execute(
            f"update public.{table} set status = 'superseded' "
            f"where status in ('draft', 'review') "
            f"  and version_number < %s and {where}",
            (row["version_number"], *(row[c] for c in scope)))
        stale = cur.rowcount

        link = (await _tracked_link(cur, row)
                if table == "campaign_assets" else None)
        await cur.execute(
            f"update public.{table} "
            f"set status = 'approved', approved_by = %s, approved_at = now()"
            + (", tracked_url = %s " if link is not None else " ")
            + f"where id = %s",
            (approved_by, link, row_id) if link is not None
            else (approved_by, row_id))
    result = {"id": row_id, "superseded": superseded, "stale": stale}
    if link is not None:
        result["tracked_url"] = link
    return result


#: Bulk-approve eligibility, per table: the status a row must be in, and
#: whether QA has a say.
#:
#: campaign_strategies is absent deliberately. Its partial unique index allows
#: one approved row per campaign, so "approve all strategies" either means
#: "approve exactly one" -- which is the button that already exists -- or it
#: means a constraint violation.
#: `slot` names the columns that make a row one version of one thing.
#: Where it is set, only the NEWEST pending version in a slot is a
#: bulk-approve candidate -- see newer_pending_versions.
_BULK_ELIGIBLE = {
    "campaign_assets": {"from_status": "review", "qa_gated": True,
                        "slot": ("channel", "asset_type", "variant",
                                 "position")},
    "campaign_angles": {"from_status": "draft", "qa_gated": False},
    "creative_concepts": {"from_status": "draft", "qa_gated": False},
}

#: QA outcomes and whether bulk approval may sweep them up.
#:
#: 'warning' is EXCLUDED, and that is the whole point of this function having
#: skip reasons at all. A warning is a truthful finding -- the live example is
#: an ad whose copy genuinely has no call to action -- and bulk-approving it
#: means somebody cleared it without reading it. The single Approve button
#: still accepts a warning, so nothing is blocked; it just costs one deliberate
#: click per finding, which is the right price.
_BULK_QA_OK = {"pass"}

_QA_SKIP_REASON = {
    "warning": "has a QA warning -- read it and approve individually",
    "needs_info": "QA needs more information",
    "blocked": "QA blocked it",
    None: "no QA result yet -- run QA first",
}


def _rank_slots(rows, from_status: str,
                slot: tuple[str, ...]) -> dict[tuple, dict]:
    """slot key -> the newest row in it that is still a candidate."""
    best: dict[tuple, dict] = {}
    for row in rows:
        if row["status"] != from_status:
            continue
        key = tuple(row[c] for c in slot)
        if (key not in best
                or row["version_number"] > best[key]["version_number"]):
            best[key] = row
    return best


def slot_winners(rows, from_status: str,
                 slot: tuple[str, ...]) -> dict[str, str]:
    """-> {overtaken row id: id of the row that beat it}.

    Same ranking as newer_pending_versions, different view of it.
    approve_many needs it to tell "still waiting on v3" apart from
    "retired, because v3 was approved a moment ago" -- those read the
    same to the reviewer and mean opposite things.
    """
    if not slot:
        return {}
    best = _rank_slots(rows, from_status, slot)
    return {
        str(row["id"]): str(best[tuple(row[c] for c in slot)]["id"])
        for row in rows
        if row["status"] == from_status
        and row["id"] != best[tuple(row[c] for c in slot)]["id"]
    }


def newer_pending_versions(rows, from_status: str,
                           slot: tuple[str, ...]) -> dict[str, int]:
    """-> {row id: the newer pending version number in its slot}.

    Only rows that lose to a newer sibling appear. Pure, and shared
    by approve_many and campaigns.pipeline_state, for the same reason
    bulk_skip_reason is: a count computed one way and an action taken
    another is how "Approve all (7)" comes to approve six.

    THE CASE THIS EXISTS FOR IS ORDINARY, NOT EXOTIC
    An edit request creates v4 while v2 is still sitting in review,
    so a slot legitimately holds two candidates. Approving both in
    turn does not raise -- approve() supersedes as it goes -- it just
    means the surviving version is whichever the loop reached last,
    and the report claims two approvals for one live asset.

    Approving an OLDER version over a newer one is never what a
    reviewer means. So the newest pending version is the only
    candidate, and if it is ineligible the whole slot waits.
    """
    if not slot:
        return {}
    newest = _rank_slots(rows, from_status, slot)
    return {
        str(row["id"]): newest[tuple(row[c] for c in slot)][
            "version_number"]
        for row in rows
        if row["status"] == from_status
        and row["id"] != newest[tuple(row[c] for c in slot)]["id"]
    }


def bulk_skip_reason(table: str, status: str, qa_status: str | None,
                     open_requests: int,
                     newer_version: int | None = None) -> str | None:
    """None if this row may be bulk-approved, else why not.

    Pure, and shared by approve_many and the UI's button state. Those two
    disagreeing is the specific bug worth designing out: a count computed one
    way and an action taken another produces an "Approve all (7)" button that
    approves six and reports a skip, which reads as a failure rather than as
    the intended caution.
    """
    rules = _BULK_ELIGIBLE.get(table)
    if rules is None:
        return f"{table} does not support bulk approval"
    if status == "approved":
        return "already approved"
    if status != rules["from_status"]:
        return f"status is {status}"
    # Before QA, deliberately. An older version that has been
    # overtaken should not advertise its own QA finding: "has a QA
    # warning -- approve it individually" would send a reviewer to
    # approve v1 while v3 sits unread, which is the wrong action
    # stated confidently.
    if newer_version is not None:
        return f"v{newer_version} is a newer version awaiting review"
    if open_requests:
        return "an edit was requested and is still open"
    if rules["qa_gated"] and qa_status not in _BULK_QA_OK:
        return _QA_SKIP_REASON.get(qa_status, f"QA status {qa_status}")
    return None


#: Tables whose rows can be rejected outright, and the statuses a row may be
#: rejected FROM. Not 'approved': retiring live copy is a different act with
#: different consequences, and 013's partial unique index means the slot would
#: then have no live version at all. Supersede it by approving another
#: version instead.
_REJECTABLE = {
    "campaign_assets": ("draft", "review"),
    "campaign_strategies": ("draft", "review"),
}


async def reject(table: str, row_id: str, rejected_by: str,
                 note: str | None = None) -> dict:
    """Turn down one version. Requires 022.

    THE ACTION THIS COMPLETES
    The revision loop could produce a version nobody wanted and offered no way
    to say so: approve it, or ask for yet another edit. So an unwanted v4 sat
    at 'review' indefinitely, and lifecycle's newer_version_pending blocker
    would hold the whole campaign on it with no way to clear it. Rejecting v4
    leaves v2 live and settles the slot.

    Rejection does NOT create a version or touch the approved one. The live
    copy is already correct; what changes is that the alternative stops being
    an open question.
    """
    from_statuses = _REJECTABLE.get(table)
    if from_statuses is None:
        raise ValueError(f"{table} rows cannot be rejected "
                         f"({', '.join(sorted(_REJECTABLE))} can)")

    async with cursor() as cur:
        await cur.execute(
            f"select status from public.{table} where id = %s for update",
            (row_id,))
        row = await cur.fetchone()
        if not row:
            raise LookupError(f"no {table} row {row_id}")
        if row["status"] == "rejected":
            return {"id": row_id, "already": True}
        if row["status"] not in from_statuses:
            raise ValueError(
                f"a {table} row at '{row['status']}' cannot be rejected "
                f"-- only {', '.join(from_statuses)}")

        # note goes into `notes` rather than a new column: it is the same
        # field an approval note would use, and a rejection without a reason
        # is allowed on purpose -- forcing prose produces "n/a".
        await cur.execute(
            f"update public.{table} set status = 'rejected', "
            f"    rejected_by = %s, rejected_at = now(), "
            f"    notes = coalesce(%s, notes) "
            f"where id = %s", (rejected_by, note, row_id))

    return {"id": row_id, "rejected": True, "from": row["status"]}


async def approve_many(table: str, campaign_id: str, approved_by: str) -> dict:
    """Approve every eligible row for a campaign. Reports what it skipped.

    Returns {approved: [...], skipped: [{id, label, reason}], ...}.

    THE SKIPS ARE THE INTERESTING HALF
    A bulk action that quietly approves six of seven items and says "done"
    reads as success. The seventh -- the one with a finding on it -- is exactly
    the one someone needed to see. So every skip carries a reason, and the
    caller is expected to show them.

    Rows with an OPEN revision request are also skipped. Someone asked for a
    change and has not got it yet; approving it underneath them would strand
    their feedback against text that is now signed off.
    """
    rules = _BULK_ELIGIBLE.get(table)
    if rules is None:
        raise ValueError(
            f"{table} does not support bulk approval "
            f"({', '.join(sorted(_BULK_ELIGIBLE))} do)")

    slot: tuple[str, ...] = tuple(rules.get("slot") or ())
    rows = await fetch_all(
        f"select r.id, r.status, "
        + ("r.version_number, " + "".join(f"r.{c}, " for c in slot)
           if slot else "")
        + f"       {_bulk_label_sql(table)} as label, "
        f"       {'q.status' if rules['qa_gated'] else 'null'} as qa_status, "
        f"       (select count(*) from public.revision_requests v "
        f"         where v.{_REVISION_COLUMN[table]} = r.id "
        f"           and v.status = 'open') as open_requests "
        f"from public.{table} r "
        + (
            "left join ( "
            "  select distinct on (asset_id) asset_id, status "
            "  from public.asset_qa_results order by asset_id, qa_number desc "
            ") q on q.asset_id = r.id "
            if rules["qa_gated"] else ""
        )
        + f"where r.campaign_id = %s order by label"
        + (", r.version_number desc" if slot else ""),
        (campaign_id,))

    # Newest-first within a slot, so the row that survives is the one
    # chosen rather than the one the loop happened to reach last.
    overtaken = newer_pending_versions(rows, rules["from_status"],
                                       slot)
    beat_by = slot_winners(rows, rules["from_status"], slot)

    approved: list[dict] = []
    skipped: list[dict] = []
    for row in rows:
        entry = {"id": str(row["id"]), "label": row["label"]}
        if slot:
            entry["label"] += f" v{row['version_number']}"
        reason = bulk_skip_reason(table, row["status"], row["qa_status"],
                                  row["open_requests"],
                                  overtaken.get(str(row["id"])))
        if reason == "already approved":
            continue                        # nothing to do, not a skip
        if reason:
            skipped.append({**entry, "reason": reason})
            continue

        if table in _SUPERSEDE_SCOPE:
            result = await approve(table, str(row["id"]), approved_by)
            entry["superseded"] = result.get("superseded", 0)
            # Older pending versions this approval retired. Reported
            # rather than dropped: it is the count that explains why
            # rows the reviewer could see a moment ago are gone.
            entry["stale"] = result.get("stale", 0)
        else:
            # Angles and concepts approve additively -- several can stand at
            # once, because the next stage fans out across all of them. That
            # is why they are not in _SUPERSEDE_SCOPE.
            async with cursor() as cur:
                await cur.execute(
                    f"update public.{table} set status = 'approved', "
                    f"    decided_by = %s, decided_at = now() "
                    f"where id = %s and status = %s",
                    (approved_by, row["id"], rules["from_status"]))
                if not cur.rowcount:
                    skipped.append({**entry,
                                    "reason": "changed while approving"})
                    continue
        approved.append(entry)

    # "v3 is a newer version awaiting review" is true when the batch
    # starts and misleading by the time it is read: v3 was approved
    # in this same batch, so approve() already set this row to
    # 'superseded'. Left alone, the report sends a reviewer looking
    # for something already decided.
    approved_ids = {entry["id"] for entry in approved}
    for entry in skipped:
        version = overtaken.get(entry["id"])
        if version is not None and beat_by.get(
                entry["id"]) in approved_ids:
            entry["reason"] = f"retired -- v{version} was approved"

    return {
        "table": table,
        "approved": approved,
        "skipped": skipped,
        "counts": {"approved": len(approved), "skipped": len(skipped)},
    }


#: revision_requests column pointing at each table.
_REVISION_COLUMN = {
    "campaign_assets": "asset_id",
    "campaign_angles": "angle_id",
    "creative_concepts": "concept_id",
    "campaign_strategies": "strategy_id",
}


def _bulk_label_sql(table: str) -> str:
    """How to name a row in a skip message.

    An id is useless in "1 skipped"; the reviewer needs to know WHICH ad.
    """
    if table == "campaign_assets":
        return ("concat_ws('/', r.channel, r.variant, "
                "nullif(r.position::text, ''))")
    if table == "campaign_angles":
        return "r.name"
    return "left(r.hook, 48)"


async def approved_strategy(campaign_id: str) -> dict:
    row = await fetch_one(
        "select * from public.campaign_strategies "
        "where campaign_id = %s and status = 'approved'",
        (campaign_id,),
    )
    if not row:
        raise StageNotReady(
            "no approved strategy for this campaign. Generate one and approve "
            "it first: angles derived from a draft strategy get regenerated "
            "when the strategy changes, which is the wasted pass this check "
            "prevents.")
    return row
