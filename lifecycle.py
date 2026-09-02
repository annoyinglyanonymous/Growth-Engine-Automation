"""Stage 9: the campaign's own status.

    python -m lifecycle <campaign>                    # where it is, and why
    python -m lifecycle <campaign> --promote approved --note "..."

Until now campaigns.status had twelve values and four of them were reachable.
A campaign could carry seven approved assets and still read 'validated' -- the
status it was given before a single word was written.

TWO KINDS OF TRANSITION, AND THEY MUST NOT LOOK ALIKE
  automatic   The system observed something. A strategy was approved, so the
              campaign is at 'strategy'. Nobody decided that; it follows.
  manual      A person took responsibility. 'approved' means "everything this
              campaign will publish is signed off, by me".

advance() only ever moves through the automatic band and only ever forwards.
promote() is the manual path and every step of it is guarded. Both write a
campaign_status_events row, and the `automatic` flag keeps them distinguishable
-- an audit that cannot tell an inference from a sign-off is misleading in
exactly the direction that matters.

WHY THE READINESS CHECK IS PURE
approval_readiness() takes gathered facts and returns findings. No database, no
model. That is the same discipline as pipeline.bulk_skip_reason and for the
same reason: the UI shows the checklist and the promote route enforces it, so
if they were computed separately they would eventually disagree, and the
failure mode is a button that offers something it then refuses.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field

from db import cursor, fetch_all, fetch_one

__all__ = ["AUTO_BAND", "MANUAL_MOVES", "advance", "promote",
           "approval_readiness", "history", "Blocker", "slot_label",
           "gather", "earned_status", "ApprovalFacts"]


class TransitionRefused(RuntimeError):
    """A manual transition that the campaign is not entitled to make."""


#: The statuses the system infers, in order. advance() moves forward through
#: this list and never backwards -- re-approving a strategy on a campaign
#: already in 'review' must not drag it back to 'strategy'.
AUTO_BAND: tuple[str, ...] = ("draft", "validated", "strategy", "production",
                              "review")

#: Statuses only a person sets: {from -> to}. Deliberately a whitelist rather
#: than "anything forwards", because the interesting property is that
#: 'approved' has exactly one legal predecessor.
MANUAL_MOVES: dict[str, tuple[str, ...]] = {
    "review": ("approved", "archived"),
    "approved": ("live", "archived"),
    "live": ("completed", "archived"),
    "completed": ("archived",),
    # A campaign can be abandoned from anywhere in the automatic band.
    "draft": ("archived",),
    "validated": ("archived",),
    "strategy": ("archived",),
    "production": ("archived",),
    "blocked": ("archived",),
    "needs_info": ("archived",),
}

#: Statuses that are validation outcomes rather than positions in the flow.
#: advance() leaves them alone: a blocked brief needs re-validating, not
#: nudging forward by a generator side effect.
VALIDATION_STATES = frozenset({"blocked", "needs_info", "validating"})

#: Terminal. Nothing advances out of these automatically.
FROZEN = frozenset({"approved", "live", "completed", "archived"})


@dataclass(frozen=True)
class Blocker:
    """One reason a campaign is not ready to approve."""

    check: str
    message: str
    remedy: str = ""

    def as_dict(self) -> dict:
        out = {"check": self.check, "message": self.message}
        if self.remedy:
            out["remedy"] = self.remedy
        return out


@dataclass
class ApprovalFacts:
    """Everything approval_readiness needs, gathered once.

    A dataclass rather than a dict so a missing field is an AttributeError
    here instead of a silently-absent check in production.
    """

    #: (channel, asset_type, variant, position) -> True when that slot has an
    #: approved row. One entry per slot, not per asset row: a slot can hold
    #: several versions and 013's partial unique index allows exactly one
    #: approved among them, so "all rows approved" is unsatisfiable by design.
    slots: dict[tuple, bool] = field(default_factory=dict)
    #: Channels the brief promises.
    promised_channels: tuple[str, ...] = ()
    #: Channels with at least one approved asset.
    approved_channels: frozenset[str] = frozenset()
    #: Open revision requests, as labels.
    open_revisions: tuple[str, ...] = ()
    #: Status of the most recent brief validation, or None.
    validation_status: str | None = None
    #: Assets whose latest QA is blocked, as labels.
    qa_blocked: tuple[str, ...] = ()
    #: Slots whose live version is older than one still awaiting
    #: review, as "email/A/2 (v2 live, v4 pending)".
    newer_pending: tuple[str, ...] = ()


def approval_readiness(facts: ApprovalFacts) -> list[Blocker]:
    """Everything standing between this campaign and 'approved'.

    Empty list means ready. The order is the order a person would fix them in.
    """
    blockers: list[Blocker] = []

    if not facts.slots:
        blockers.append(Blocker(
            check="no_assets",
            message="This campaign has no assets.",
            remedy="Generate assets for the channels in the brief."))
        # Everything below is about the assets, so stop here rather than
        # emitting five findings that all say the same thing.
        return blockers

    unapproved = sorted(slot_label(slot) for slot, ok in facts.slots.items()
                        if not ok)
    if unapproved:
        blockers.append(Blocker(
            check="assets_not_approved",
            message=f"{len(unapproved)} of {len(facts.slots)} asset slot(s) "
                    f"have no approved version: {', '.join(unapproved)}.",
            remedy="Approve one version of each, or use Approve all."))

    # An approved slot is not necessarily a settled one. Somebody
    # asked for an edit, got v4, and approved v2 -- or never came
    # back to v4 at all. Signing off the campaign now publishes v2
    # and silently abandons a revision that was requested on purpose,
    # which is the same failure as approving over an open edit
    # request, one step later.
    if facts.newer_pending:
        blockers.append(Blocker(
            check="newer_version_pending",
            message=f"{len(facts.newer_pending)} slot(s) have a newer version "
                    f"still awaiting review: "
                    f"{', '.join(facts.newer_pending[:3])}"
                    + (" and more" if len(facts.newer_pending) > 3
                       else "") + ".",
            remedy="Approve the newer version, or reject it to keep "
                   "the one that is live."))

    # A brief that promises email and gets only Meta ads is half a campaign,
    # and the missing half is easy to miss precisely because what exists looks
    # finished.
    missing = [c for c in facts.promised_channels
               if c not in facts.approved_channels]
    if missing:
        blockers.append(Blocker(
            check="channel_not_covered",
            message=f"The brief promises {', '.join(missing)} but no approved "
                    f"asset exists for {'it' if len(missing) == 1 else 'them'}.",
            remedy="Generate and approve assets for every channel in the "
                   "brief, or remove the channel from the brief."))

    if facts.open_revisions:
        blockers.append(Blocker(
            check="open_revision",
            message=f"{len(facts.open_revisions)} edit request(s) are still "
                    f"open: {', '.join(facts.open_revisions[:3])}"
                    + (" and more" if len(facts.open_revisions) > 3 else "")
                    + ".",
            remedy="Let the agent answer them, or withdraw them."))

    if facts.qa_blocked:
        blockers.append(Blocker(
            check="qa_blocked",
            message=f"QA blocked {len(facts.qa_blocked)} asset(s): "
                    f"{', '.join(facts.qa_blocked[:3])}.",
            remedy="Fix the copy with an edit request and re-run QA."))

    # Belt and braces. An asset-level approval cannot happen while the brief
    # itself is blocked, so this should be unreachable -- but a campaign
    # approval is the wrong place to find out that assumption was wrong.
    if facts.validation_status == "blocked":
        blockers.append(Blocker(
            check="brief_blocked",
            message="The most recent brief validation is blocked.",
            remedy="Re-run validation and clear the blockers."))

    return blockers


def slot_label(slot: tuple) -> str:
    channel, _asset_type, variant, position = slot
    label = f"{channel}/{variant}"
    return f"{label}/{position}" if position is not None else label


# --------------------------------------------------------------------------
# Reading state
# --------------------------------------------------------------------------

async def gather(campaign_id: str) -> ApprovalFacts:
    """Load the facts approval_readiness needs."""
    campaign = await fetch_one(
        "select channels from public.campaigns where id = %s", (campaign_id,))
    if not campaign:
        raise LookupError(f"no campaign {campaign_id}")

    rows = await fetch_all(
        "select a.channel, a.asset_type, a.variant, a.position, "
        "       bool_or(a.status = 'approved') as approved, "
        "       max(a.version_number) "
        "         filter (where a.status = 'approved') as live_v, "
        "       max(a.version_number) "
        "         filter (where a.status in ('draft', 'review')) "
        "         as pending_v "
        "from public.campaign_assets a "
        "where a.campaign_id = %s "
        "group by a.channel, a.asset_type, a.variant, a.position",
        (campaign_id,))
    slots = {(r["channel"], r["asset_type"], r["variant"], r["position"]):
             r["approved"] for r in rows}

    # Only a pending version NEWER than the live one counts. Older
    # ones are settled by approve(), which supersedes them.
    newer_pending = tuple(sorted(
        f"{slot_label((r['channel'], r['asset_type'], r['variant'], r['position']))}"
        f" (v{r['live_v']} live, v{r['pending_v']} pending)"
        for r in rows
        if r["live_v"] is not None and r["pending_v"] is not None
        and r["pending_v"] > r["live_v"]))

    approved_channels = frozenset(
        r["channel"] for r in rows if r["approved"])

    open_rev = await fetch_all(
        "select coalesce(concat_ws('/', a.channel, a.variant), "
        "                ang.name, left(cc.hook, 30), 'strategy') as label "
        "from public.revision_requests v "
        "left join public.campaign_assets a on a.id = v.asset_id "
        "left join public.campaign_angles ang on ang.id = v.angle_id "
        "left join public.creative_concepts cc on cc.id = v.concept_id "
        "where v.campaign_id = %s and v.status = 'open'", (campaign_id,))

    blocked = await fetch_all(
        "select concat_ws('/', a.channel, a.variant) as label "
        "from public.campaign_assets a "
        "join ( "
        "  select distinct on (asset_id) asset_id, status "
        "  from public.asset_qa_results order by asset_id, qa_number desc "
        ") q on q.asset_id = a.id "
        "where a.campaign_id = %s and q.status = 'blocked' "
        "  and a.status <> 'superseded'", (campaign_id,))

    latest = await fetch_one(
        "select status from public.campaign_validations "
        "where campaign_id = %s order by validation_number desc limit 1",
        (campaign_id,))

    return ApprovalFacts(
        slots=slots,
        promised_channels=tuple(campaign["channels"] or ()),
        approved_channels=approved_channels,
        open_revisions=tuple(r["label"] for r in open_rev),
        validation_status=(latest or {}).get("status"),
        qa_blocked=tuple(sorted({r["label"] for r in blocked})),
        newer_pending=newer_pending,
    )


async def earned_status(campaign_id: str) -> str:
    """The furthest point in AUTO_BAND this campaign has demonstrably reached.

    Derived from what exists, not from what the status column says, so it is
    idempotent -- calling advance() repeatedly cannot walk a campaign forward
    past what it has actually done.
    """
    counts = await fetch_one(
        "select "
        "  (select count(*) from public.campaign_validations v "
        "    where v.campaign_id = c.id and v.status = 'validated') as valid, "
        "  (select count(*) from public.campaign_strategies s "
        "    where s.campaign_id = c.id and s.status = 'approved') as strat, "
        "  (select count(*) from public.campaign_assets a "
        "    where a.campaign_id = c.id) as assets, "
        "  (select count(*) from public.asset_qa_results q "
        "    join public.campaign_assets a on a.id = q.asset_id "
        "    where a.campaign_id = c.id) as qa "
        "from public.campaigns c where c.id = %s", (campaign_id,))
    if not counts:
        raise LookupError(f"no campaign {campaign_id}")

    if counts["qa"] and counts["assets"]:
        return "review"
    if counts["assets"]:
        return "production"
    if counts["strat"]:
        return "strategy"
    if counts["valid"]:
        return "validated"
    return "draft"


# --------------------------------------------------------------------------
# Transitions
# --------------------------------------------------------------------------

async def record_transition(cur, campaign_id: str,
                            from_status: str | None, to_status: str,
                            changed_by: str, automatic: bool,
                            note: str | None = None) -> None:
    """Write one row of the campaign's status history.

    Public, and takes a cursor instead of opening its own, because
    stage 3 also moves the campaign: validation.brief writes the
    status inside its own transaction and has to record the move in
    the SAME one, or a failure between the two leaves a status that
    no event explains.

    021's campaign_status_events_actually_moved CHECK forbids an
    event whose from_status equals its to_status, so a caller must
    not call this for a re-run that landed on the status the
    campaign already had. That is the constraint doing its job: a
    no-op is not an event.
    """
    await cur.execute(
        "insert into public.campaign_status_events "
        "  (campaign_id, from_status, to_status, changed_by, automatic, note) "
        "values (%s, %s, %s, %s, %s, %s)",
        (campaign_id, from_status, to_status, changed_by, automatic, note))


async def advance(campaign_id: str, changed_by: str) -> dict:
    """Move the campaign forward through AUTO_BAND if it has earned it.

    Called after every generator and QA action. A no-op unless the campaign is
    behind where its own artefacts put it.

    Never moves backwards, never touches a validation state, never touches a
    frozen one. Those three exclusions are the whole safety of doing this
    automatically: without them, approving a strategy on an already-approved
    campaign would quietly un-approve it.
    """
    row = await fetch_one(
        "select status from public.campaigns where id = %s", (campaign_id,))
    if not row:
        raise LookupError(f"no campaign {campaign_id}")
    current = row["status"]

    if current in FROZEN or current in VALIDATION_STATES:
        return {"status": current, "moved": False,
                "why": f"{current} is not advanced automatically"}

    earned = await earned_status(campaign_id)
    if current not in AUTO_BAND:
        return {"status": current, "moved": False,
                "why": f"{current} is outside the automatic band"}
    if AUTO_BAND.index(earned) <= AUTO_BAND.index(current):
        return {"status": current, "moved": False, "why": "already there"}

    async with cursor() as cur:
        await cur.execute(
            "update public.campaigns set status = %s where id = %s",
            (earned, campaign_id))
        await record_transition(cur, campaign_id, current, earned,
                                changed_by, True)
    return {"status": earned, "moved": True, "from": current}


async def promote(campaign_id: str, to_status: str, changed_by: str, *,
                  note: str | None = None) -> dict:
    """A person moves the campaign. Guarded, attributed, recorded.

    review -> approved is the one that matters: it requires every asset slot
    to have an approved version, every promised channel to be covered, no open
    edit requests, and nothing QA-blocked. Those are checked here rather than
    trusted from the UI, because the UI is one caller of several.
    """
    row = await fetch_one(
        "select status, name from public.campaigns where id = %s",
        (campaign_id,))
    if not row:
        raise LookupError(f"no campaign {campaign_id}")
    current = row["status"]

    allowed = MANUAL_MOVES.get(current, ())
    if to_status not in allowed:
        raise TransitionRefused(
            f"a campaign at '{current}' cannot go to '{to_status}'"
            + (f" -- only {', '.join(allowed)}" if allowed
               else " -- it is terminal"))

    if to_status == "approved":
        blockers = approval_readiness(await gather(campaign_id))
        if blockers:
            raise TransitionRefused(
                "not ready to approve: "
                + " ".join(b.message for b in blockers))

    async with cursor() as cur:
        if to_status == "approved":
            await cur.execute(
                "update public.campaigns set status = %s, approved_by = %s, "
                "    approved_at = now() where id = %s",
                (to_status, changed_by, campaign_id))
        else:
            await cur.execute(
                "update public.campaigns set status = %s where id = %s",
                (to_status, campaign_id))
        await record_transition(cur, campaign_id, current, to_status,
                                changed_by, False, note)

    return {"status": to_status, "from": current, "moved": True,
            "by": changed_by}


async def history(campaign_id: str) -> list[dict]:
    return await fetch_all(
        "select from_status, to_status, changed_by, changed_at, automatic, "
        "       note "
        "from public.campaign_status_events where campaign_id = %s "
        "order by changed_at desc, created_at desc", (campaign_id,))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

async def amain() -> int:
    import auth
    from db import pool
    from generators.pipeline import load_campaign

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("campaign")
    ap.add_argument("--promote", default=None,
                    choices=sorted({s for v in MANUAL_MOVES.values()
                                    for s in v}))
    ap.add_argument("--advance", action="store_true",
                    help="move forward through the automatic band if earned")
    ap.add_argument("--note", default=None)
    ap.add_argument("--as", dest="operator", default=None)
    args = ap.parse_args()

    operator = auth.cli_operator(args.operator)
    await pool.open()
    try:
        campaign = await load_campaign(args.campaign)
        cid = campaign["id"]

        if args.advance:
            print(json.dumps(await advance(cid, operator), indent=2))
            return 0
        if args.promote:
            print(json.dumps(await promote(cid, args.promote, operator,
                                           note=args.note), indent=2))
            return 0

        facts = await gather(cid)
        blockers = approval_readiness(facts)
        print(json.dumps({
            "campaign": campaign["name"],
            "status": campaign["status"],
            "earned": await earned_status(cid),
            "can_go_to": list(MANUAL_MOVES.get(campaign["status"], ())),
            "ready_to_approve": not blockers,
            "blockers": [b.as_dict() for b in blockers],
            "slots": {slot_label(s): ok for s, ok in facts.slots.items()},
        }, indent=2, ensure_ascii=False, default=str))
        return 1 if blockers else 0
    except (LookupError, TransitionRefused) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        await pool.close()


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())
