"""The minimal web UI, mounted on the same FastAPI app as the KB.

    python main.py --port 8000    then open http://127.0.0.1:8000/

No node, no build step, no second deployment: the UI ships with the API it
talks to, and calls kb_context / generators / validation as Python rather than
over HTTP.

WHY PLAIN FORMS AND NOT HTMX
The plan called for vendored HTMX. Building it, HTMX earned nothing here: every
interaction on these four screens is "do one slow thing, then show the whole
new state", which is a form POST and a redirect. What HTMX would have added is
a partial-render path to maintain alongside the full-page one, plus a vendored
asset to keep current. The one genuine benefit -- feedback during a 30-to-120
second generation -- is 12 lines of inline script that disables the button and
says what is running.

So: no dependency, no CDN, no build. If a future screen needs real partial
updates, that is the point to reconsider.

WHY GENERATION HAPPENS INSIDE THE REQUEST
A model call takes 30 to 120 seconds and the browser waits. That is honest for
a single-user internal tool -- the alternative is a job table, a poller and a
progress endpoint, which is Phase 2 work for a team that does not exist yet.
The generators use acomplete_json, so the call runs off the event loop and a
second tab stays responsive while one generates.

IDENTITY
Every route that writes takes an operator from auth.require_operator and passes
it to whatever it calls, so approved_by / decided_by / validated_by / created_by
name a person rather than a placeholder. auth.py's docstring is explicit that
this is attribution and NOT access control: the app still connects to Postgres
as a superuser, so a cookie changes who the row credits, not what a local
process can do.

Two routers, because that boundary has to be visible in the code rather than
remembered: `router` carries the gate as a router-level dependency, and
`public_router` holds sign-in and sign-out. A new screen added to `router` is
gated by default, which is the failure mode worth designing for.

Bound to 127.0.0.1 in main.py's __main__, and that bind is still the whole of
the security story. Reaching this over a network needs a non-superuser DB role,
the RLS policies that do not exist yet, TLS and a deploy target.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import auth
import campaigns
import exclusions
import lifecycle
from generators import angles as gen_angles
from generators import concepts as gen_concepts
from generators import email_sequence as gen_email
from generators import meta_ads as gen_meta
from generators import revise
from generators import strategy as gen_strategy
from generators.pipeline import (
    StageNotReady,
    approve,
    approve_many,
    reject,
)
from validation import asset_qa, brief

ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))

#: Screens and actions. Every route here requires an identity.
router = APIRouter(tags=["ui"], include_in_schema=False,
                   dependencies=[Depends(auth.require_operator)])

#: Sign-in and sign-out only. Anything added here is reachable without an
#: identity, so the list stays short enough to audit at a glance.
public_router = APIRouter(tags=["ui"], include_in_schema=False)


def _flash(url: str, message: str, kind: str = "ok") -> RedirectResponse:
    """Redirect carrying a one-line result.

    In the query string rather than a session cookie: the identity cookie is
    the only cookie this app sets, and widening it into a general session store
    to carry a status line would be the first piece of infrastructure nobody
    asked for.
    """
    sep = "&" if "?" in url else "?"
    from urllib.parse import quote
    return RedirectResponse(f"{url}{sep}msg={quote(message)}&kind={kind}",
                            status_code=303)


def _kind_for(status: str | None) -> str:
    """Flash colour for a pipeline status value.

    A successful request can still carry bad news: "validation #1 -> blocked"
    flashed green because the POST succeeded, which is precisely the
    misleading-but-technically-true signal this project exists to prevent.

    Takes the STATUS, not the message. The first version of this sniffed the
    prose for the word "blocked" and turned "6 pass, 1 warning, 0 blocked" red
    -- guessing an outcome from a sentence that reports counts is unfixable in
    general, so the actions pass their status explicitly instead.
    """
    return {
        "blocked": "error",
        "needs_info": "warn",
        "warning": "warn",
        "pass": "ok",
    }.get(status or "", "ok")


def _safe_next(value: str | None) -> str:
    """A local path to return to after sign-in, or "/".

    Rejecting anything that is not a same-site path, and the case worth naming
    is the one the obvious check misses: "//evil.example/" starts with "/", so
    a `value.startswith("/")` test passes it -- and a browser reads a leading
    "//" as scheme-relative and leaves the site. Backslash variants ("/\\host")
    are folded the same way by some browsers, so they go too.

    Everything here is an open-redirect guard rather than a correctness one.
    The value arrives from a query string, so it is attacker-controlled by
    definition even on a localhost tool.
    """
    if not value or not value.startswith("/"):
        return "/"
    if value.startswith(("//", "/\\")):
        return "/"
    return value


def _chrome(request: Request) -> dict:
    """Template context every screen shares: who is signed in, and the mode.

    identity_enabled is passed so base.html can show a standing warning when
    OPERATORS is unset. An install that silently records approvals as
    'local-ui' should say so on every page, not once in a boot log nobody
    scrolls back to.
    """
    return {
        "operator": auth.current(request),
        "identity_enabled": auth.enabled(),
        "unattributed_as": auth.UNCONFIGURED,
    }


async def _act(request: Request, campaign_id: str, label: str, coro):
    """Run one pipeline action, turning its outcome into a flash.

    The coroutine returns either a message, or (message, status) where status
    is a pipeline status value -- so the flash colour comes from the outcome
    rather than from parsing the message.

    StageNotReady is the interesting failure: it means the user pressed a
    button the pipeline is not ready for. That is a message, not a traceback --
    and the button should have been disabled, so seeing this in practice is a
    bug in pipeline_state's `can` flags rather than user error.
    """
    url = f"/campaigns/{campaign_id}"
    try:
        result = await coro
    except StageNotReady as exc:
        return _flash(url, f"{label}: {exc}", "warn")
    except Exception as exc:  # noqa: BLE001 -- surfaced, not swallowed
        return _flash(url, f"{label} failed -- {type(exc).__name__}: {exc}",
                      "error")

    if isinstance(result, tuple):
        message, status = result
    else:
        message, status = result, None

    # Every successful action gets a lifecycle advance, here rather than in the
    # individual routes. Approving a strategy, generating assets and running QA
    # all change what the campaign has earned, and a per-route call is a call
    # somebody eventually forgets to add. advance() is a no-op unless the
    # campaign is behind its own artefacts, so the cost is one small query.
    try:
        moved = await lifecycle.advance(campaign_id,
                                        auth.require_operator(request))
        if moved["moved"]:
            message += f" (campaign -> {moved['status']})"
    except Exception as exc:  # noqa: BLE001
        # Never let a lifecycle problem hide the result of the thing the
        # person actually asked for -- but do not swallow it either.
        message += f" (lifecycle advance failed: {type(exc).__name__})"

    return _flash(url, f"{label}: {message}", _kind_for(status))


# --------------------------------------------------------------------------
# Sign in / out
# --------------------------------------------------------------------------

@public_router.get("/signin", response_class=HTMLResponse)
async def signin_form(request: Request, next: str = "/",
                      msg: str | None = None, kind: str = "ok"):
    if not auth.enabled():
        return _flash("/", "identity is not configured -- OPERATORS is unset",
                      "warn")
    if auth.current(request):
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "signin.html", {
        "operators": auth.operators(),
        "passcode_required": auth.passcode_required(),
        "next": _safe_next(next),
        "msg": msg, "kind": kind, **_chrome(request),
    })


@public_router.post("/signin")
async def signin(request: Request, operator: str = Form(...),
                 passcode: str = Form(""), next: str = Form("/")):
    if not auth.enabled():
        return _flash("/", "identity is not configured", "warn")

    # Allowlist check first, and the same message for both failures: telling a
    # caller which half was wrong is free help for guessing the other.
    if operator not in auth.operators() or not auth.verify_passcode(passcode):
        from urllib.parse import quote
        return _flash(f"/signin?next={quote(_safe_next(next), safe='')}",
                      "sign-in failed", "error")

    response = RedirectResponse(_safe_next(next), status_code=303)
    response.set_cookie(
        auth.COOKIE, auth.issue(operator),
        max_age=None,            # session cookie; the signature carries expiry
        httponly=True,           # no reason for script to read it
        samesite="lax",
        # secure=False deliberately: this is served over http on 127.0.0.1, and
        # a Secure cookie would simply never be stored. It becomes required the
        # moment there is TLS, which is the same moment real auth is needed.
        secure=False,
        path="/",
    )
    return response


@public_router.post("/signout")
async def signout(request: Request):
    response = _flash("/signin", "signed out", "ok")
    response.delete_cookie(auth.COOKIE, path="/")
    return response


# --------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def index(request: Request, msg: str | None = None,
                kind: str = "ok"):
    return templates.TemplateResponse(request, "campaigns.html", {
        "campaigns": await campaigns.list_campaigns(),
        "msg": msg, "kind": kind, **_chrome(request),
    })


# --------------------------------------------------------------------------
# The standing do-not-use list (023)
# --------------------------------------------------------------------------

@router.get("/exclusions", response_class=HTMLResponse)
async def exclusions_page(request: Request, msg: str | None = None,
                          kind: str = "ok"):
    return templates.TemplateResponse(request, "exclusions.html", {
        "rules": await exclusions.for_brand(),
        "brands": await exclusions.brands(),
        "msg": msg, "kind": kind, **_chrome(request),
    })


@router.post("/exclusions")
async def add_exclusion(request: Request,
                        operator: str = Depends(auth.require_operator),
                        brand_slug: str = Form(...),
                        phrase: str = Form(...),
                        note: str = Form("")):
    try:
        row = await exclusions.add(brand_slug, phrase, operator, note)
    except exclusions.ExclusionProblem as exc:
        return _flash("/exclusions", str(exc), "error")
    return _flash("/exclusions",
                  f"{row['phrase']!r} will now block any asset that uses it",
                  "ok")


# The literal comes last here and there is no other route at this depth under
# /exclusions, so nothing to shadow -- but see the overlap test.
@router.post("/exclusions/{exclusion_id}/retire")
async def retire_exclusion(request: Request, exclusion_id: str,
                           operator: str = Depends(auth.require_operator)):
    try:
        row = await exclusions.retire(exclusion_id, operator)
    except exclusions.ExclusionProblem as exc:
        return _flash("/exclusions", str(exc), "error")
    if row.get("already"):
        return _flash("/exclusions", "already retired", "warn")
    return _flash("/exclusions",
                  f"{row['phrase']!r} is no longer enforced", "ok")


@router.get("/campaigns/new", response_class=HTMLResponse)
async def new_campaign(request: Request, msg: str | None = None,
                       kind: str = "ok"):
    return templates.TemplateResponse(request, "new.html", {
        **await campaigns.form_options(), "msg": msg, "kind": kind,
        **_chrome(request),
    })


@router.post("/campaigns")
async def create_campaign(
    request: Request,
    operator: str = Depends(auth.require_operator),
    name: str = Form(...),
    product_id: str = Form(...),
    campaign_type_id: str = Form(""),
    objective: str = Form(""),
    target_audience: str = Form(""),
    customer_problem: str = Form(""),
    offer: str = Form(""),
    primary_benefit: str = Form(""),
    supporting_benefits: str = Form(""),
    proof_points: str = Form(""),
    primary_cta: str = Form(""),
    secondary_cta: str = Form(""),
    channels: list[str] = Form([]),
    primary_kpi: str = Form(""),
    geographic_target: str = Form(""),
    additional_context: str = Form(""),
    do_not_mention: str = Form(""),
):
    try:
        row = await campaigns.create(locals(), created_by=operator)
    except Exception as exc:  # noqa: BLE001
        return _flash("/campaigns/new",
                      f"could not create: {type(exc).__name__}: {exc}",
                      "error")
    return _flash(f"/campaigns/{row['id']}",
                  "brief filed as draft -- run validation next", "ok")


@router.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
async def campaign_detail(request: Request, campaign_id: str,
                          msg: str | None = None, kind: str = "ok"):
    campaign = await campaigns.get(campaign_id)
    if not campaign:
        return _flash("/", f"no campaign {campaign_id}", "error")
    state = await campaigns.pipeline_state(campaign["id"])
    return templates.TemplateResponse(request, "campaign.html", {
        "c": campaign, **state, "msg": msg, "kind": kind, **_chrome(request),
    })


# --------------------------------------------------------------------------
# Actions -- each mirrors one CLI command
# --------------------------------------------------------------------------

@router.post("/campaigns/{campaign_id}/validate")
async def do_validate(request: Request, campaign_id: str,
                      operator: str = Depends(auth.require_operator),
                      use_ai: str = Form("")):
    async def run():
        r = await brief.validate(campaign_id, validated_by=operator,
                                 use_ai=bool(use_ai))
        return (f"validation #{r['validation_number']} -> {r['status']} "
                f"({r['counts']['blockers']} blockers, "
                f"{r['counts']['warnings']} warnings)", r["status"])
    return await _act(request, campaign_id, "Validation", run())


@router.post("/campaigns/{campaign_id}/strategy")
async def do_strategy(request: Request, campaign_id: str):
    async def run():
        r = await gen_strategy.generate(campaign_id)
        return (f"strategy V{r['version']} drafted from "
                f"{r['grounding']['kb_chunks']} chunks and "
                f"{r['grounding']['approved_claims_offered']} approved claims")
    return await _act(request, campaign_id, "Generate strategy", run())


@router.post("/campaigns/{campaign_id}/strategy/{strategy_id}/approve")
async def do_approve_strategy(request: Request, campaign_id: str,
                             strategy_id: str,
                             operator: str = Depends(auth.require_operator)):
    async def run():
        r = await approve("campaign_strategies", strategy_id, operator)
        if r.get("already"):
            return "already approved"
        return (f"approved as {operator}; {r['superseded']} previous "
                f"version(s) superseded")
    return await _act(request, campaign_id, "Approve strategy", run())


@router.post("/campaigns/{campaign_id}/angles")
async def do_angles(request: Request, campaign_id: str):
    async def run():
        r = await gen_angles.generate(campaign_id)
        return f"{len(r['angles'])} angle(s) drafted"
    return await _act(request, campaign_id, "Generate angles", run())


@router.post("/campaigns/{campaign_id}/angles/{angle_id}/{decision}")
async def do_decide_angle(request: Request, campaign_id: str, angle_id: str,
                          decision: str,
                          operator: str = Depends(auth.require_operator)):
    if decision not in ("approve", "reject"):
        return _flash(f"/campaigns/{campaign_id}",
                      f"unknown decision {decision!r}", "error")

    async def run():
        r = await gen_angles.decide(
            angle_id, "approved" if decision == "approve" else "rejected",
            operator)
        return f"{r['name']} -> {r['status']} by {r['decided_by']}"
    return await _act(request, campaign_id, "Angle", run())


@router.post("/campaigns/{campaign_id}/concepts")
async def do_concepts(request: Request, campaign_id: str):
    async def run():
        r = await gen_concepts.generate(campaign_id)
        return f"{len(r['concepts'])} concept(s) drafted"
    return await _act(request, campaign_id, "Generate concepts", run())


@router.post("/campaigns/{campaign_id}/concepts/{concept_id}/{decision}")
async def do_decide_concept(request: Request, campaign_id: str,
                            concept_id: str, decision: str,
                            operator: str = Depends(auth.require_operator)):
    if decision not in ("approve", "reject"):
        return _flash(f"/campaigns/{campaign_id}",
                      f"unknown decision {decision!r}", "error")

    async def run():
        r = await gen_concepts.decide(
            concept_id, "approved" if decision == "approve" else "rejected",
            operator)
        return f"{r['status']} by {r['decided_by']}"
    return await _act(request, campaign_id, "Concept", run())


@router.post("/campaigns/{campaign_id}/assets/meta")
async def do_meta(request: Request, campaign_id: str,
                  concept_id: str = Form("")):
    async def run():
        r = await gen_meta.generate(campaign_id, concept_id=concept_id or None)
        return f"{len(r['assets'])} Meta ad(s) drafted"
    return await _act(request, campaign_id, "Generate Meta ads", run())


@router.post("/campaigns/{campaign_id}/assets/email")
async def do_email(request: Request, campaign_id: str,
                   concept_id: str = Form(""), emails: int = Form(3)):
    async def run():
        r = await gen_email.generate(campaign_id,
                                     concept_id=concept_id or None,
                                     emails=emails)
        return f"{len(r['emails'])}-email sequence drafted (v{r['version']})"
    return await _act(request, campaign_id, "Generate email sequence", run())


@router.post("/campaigns/{campaign_id}/qa")
async def do_qa(request: Request, campaign_id: str,
                operator: str = Depends(auth.require_operator),
                use_ai: str = Form("")):
    async def run():
        r = await asset_qa.qa(campaign_id, validated_by=operator,
                              use_ai=bool(use_ai))
        if not r.get("assets"):
            return r.get("note", "nothing to QA")
        # The worst individual outcome sets the colour: one blocked asset in
        # twenty is a red result, not a mostly-green one.
        worst = ("blocked" if r["blocked"] else
                 "needs_info" if r["needs_info"] else
                 "warning" if r["warning"] else "pass")
        return (f"{r['assets']} asset(s): {r['pass']} pass, "
                f"{r['warning']} warning, {r['needs_info']} needs info, "
                f"{r['blocked']} blocked", worst)
    return await _act(request, campaign_id, "QA", run())


@router.post("/campaigns/{campaign_id}/assets/{asset_id}/approve")
async def do_approve_asset(request: Request, campaign_id: str, asset_id: str,
                           operator: str = Depends(auth.require_operator)):
    async def run():
        r = await approve("campaign_assets", asset_id, operator)
        if r.get("already"):
            return "already approved"
        return f"approved as {operator}; {r['superseded']} superseded"
    return await _act(request, campaign_id, "Approve asset", run())


@router.post("/campaigns/{campaign_id}/assets/{asset_id}/reject")
async def do_reject_asset(request: Request, campaign_id: str, asset_id: str,
                          operator: str = Depends(auth.require_operator),
                          note: str = Form("")):
    """Turn down one version and leave the live one alone.

    The action the revision loop was missing. A reviewer who asks for an edit
    and dislikes the result could previously only approve it or ask again;
    lifecycle's newer_version_pending blocker would then hold the campaign on
    a version nobody wanted, with no way to clear it.
    """
    async def run():
        r = await reject("campaign_assets", asset_id, operator,
                         note.strip() or None)
        if r.get("already"):
            return "already rejected"
        return f"rejected as {operator}"
    return await _act(request, campaign_id, "Reject asset", run())


# --------------------------------------------------------------------------
# Approve all
# --------------------------------------------------------------------------

#: URL segment -> table. Strategy is absent: one approved strategy per
#: campaign, so bulk approval of it is either a no-op or a constraint
#: violation. approve_many raises for anything not here.
_BULK_TABLES = {
    "angles": "campaign_angles",
    "concepts": "creative_concepts",
    "assets": "campaign_assets",
}


# "/approve-all/{stage}" and not "/{stage}/approve-all". The latter has a
# wildcard where /promote/{to_status} has a literal, so the two patterns can
# both match "/campaigns/X/promote/approve-all" -- the same class of ambiguity
# the revise/withdraw pair had. Putting the literal first makes them
# structurally distinct instead of relying on registration order.
@router.post("/campaigns/{campaign_id}/approve-all/{stage}")
async def do_approve_all(request: Request, campaign_id: str, stage: str,
                         operator: str = Depends(auth.require_operator)):
    table = _BULK_TABLES.get(stage)
    if not table:
        return _flash(f"/campaigns/{campaign_id}",
                      f"cannot bulk-approve {stage!r}", "error")

    async def run():
        r = await approve_many(table, campaign_id, operator)
        done, skipped = r["counts"]["approved"], r["counts"]["skipped"]
        if not done and not skipped:
            return "nothing eligible", None
        # The skips are the half worth reading, so they go in the message and
        # the colour reflects that something was held back rather than done.
        detail = "; ".join(f"{s['label']} -- {s['reason']}"
                           for s in r["skipped"][:4])
        if skipped > 4:
            detail += f"; and {skipped - 4} more"
        message = f"{done} approved"
        if skipped:
            message += f", {skipped} left for you: {detail}"
        return message, ("warning" if skipped else "pass")
    return await _act(request, campaign_id, f"Approve all {stage}", run())


# --------------------------------------------------------------------------
# Request an edit
# --------------------------------------------------------------------------

@router.post("/campaigns/{campaign_id}/revise/{kind}/{target_id}")
async def do_revise(request: Request, campaign_id: str, kind: str,
                    target_id: str,
                    operator: str = Depends(auth.require_operator),
                    feedback: str = Form("")):
    """File feedback and immediately generate a revision.

    Two calls rather than one, so a model failure leaves the feedback stored
    and the action retryable -- the person's typed paragraph is not collateral
    for a timeout. The flash says which half failed.
    """
    if kind not in revise.KINDS:
        return _flash(f"/campaigns/{campaign_id}",
                      f"cannot revise {kind!r}", "error")

    async def run():
        filed = await revise.request(kind, target_id, feedback, operator)
        try:
            result = await revise.apply(filed["request_id"], operator)
        except Exception as exc:  # noqa: BLE001
            return (f"feedback saved, but the revision failed -- "
                    f"{type(exc).__name__}: {exc}. Retry from the item.",
                    "blocked")

        where = ("updated in place" if result["in_place"]
                 else f"new version v{result['version']}")
        message = f"{result['kind']} revised ({where})"
        if result["needs_qa"]:
            message += " -- run QA before approving"
        if result["agent_note"]:
            # Surfaced, not buried: this is the agent saying it could not do
            # part of what was asked, which the reviewer must see.
            message += f". Agent note: {result['agent_note']}"
            return message, "warning"
        return message, "pass"
    return await _act(request, campaign_id, "Edit", run())


# "revisions" and not "revise": /revise/{kind}/{target_id} is registered above
# and would shadow /revise/{request_id}/withdraw, binding kind="<uuid>" and
# target_id="withdraw". FastAPI matches in registration order, so the two
# patterns must not be able to describe the same path in the first place --
# reordering would only hide the ambiguity until someone adds a third route.
# --------------------------------------------------------------------------
# Stage 9 -- the campaign's own status
# --------------------------------------------------------------------------

@router.post("/campaigns/{campaign_id}/promote/{to_status}")
async def do_promote(request: Request, campaign_id: str, to_status: str,
                     operator: str = Depends(auth.require_operator),
                     note: str = Form("")):
    """A person moves the campaign. lifecycle.promote does the guarding.

    The guards live there and not here because the UI is one caller of
    several -- the CLI takes the same path, and a check in a route handler
    protects exactly one door.
    """
    async def run():
        r = await lifecycle.promote(campaign_id, to_status, operator,
                                    note=note.strip() or None)
        message = f"{r['from']} -> {r['status']} by {operator}"
        # Reaching 'approved' is the one transition worth colouring, because
        # it is the moment someone takes responsibility for everything the
        # campaign publishes.
        return message, ("pass" if to_status == "approved" else None)
    return await _act(request, campaign_id, "Campaign", run())


@router.post("/campaigns/{campaign_id}/revisions/{request_id}/withdraw")
async def do_withdraw_revision(
        request: Request, campaign_id: str, request_id: str,
        operator: str = Depends(auth.require_operator)):
    async def run():
        await revise.withdraw(request_id, operator)
        return "edit request withdrawn"
    return await _act(request, campaign_id, "Edit", run())
