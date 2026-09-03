"""Route-level tests for the identity gate.

No database. TestClient is constructed without its context manager on purpose,
so the lifespan never runs and the pool is never opened -- every test here is
about what happens BEFORE a handler executes, and a handler that ran would be
the failure.

The structural test at the bottom is the one that will earn its keep. The gate
is a router-level dependency, so a route is protected by which router it is
registered on -- which is exactly the kind of thing that goes wrong six months
later when someone adds a screen and reaches for the wrong name.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import auth
import ui
from config import settings
from main import app

ALICE = "alice@example.com"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "operators", ALICE)
    monkeypatch.setattr(settings, "operator_passcode", "")
    monkeypatch.setattr(settings, "session_secret", "route-test-secret")
    monkeypatch.setattr(settings, "session_max_age", 3600)
    return TestClient(app, follow_redirects=False)


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/",
    "/campaigns/new",
    "/campaigns/00000000-0000-0000-0000-000000000000",
])
def test_screens_redirect_to_signin_when_unidentified(client, path):
    r = client.get(path)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/signin?next=")


def test_the_redirect_carries_the_path_to_come_back_to(client):
    r = client.get("/campaigns/new")
    assert "next=%2Fcampaigns%2Fnew" in r.headers["location"]


@pytest.mark.parametrize("path", [
    "/campaigns",
    "/campaigns/abc/validate",
    "/campaigns/abc/strategy",
    "/campaigns/abc/qa",
    "/campaigns/abc/assets/meta",
])
def test_write_actions_are_gated(client, path):
    """303 and not 422: the gate has to fire before body validation, otherwise
    an unidentified caller learns the shape of the form before being turned
    away -- and worse, a route with no required fields would run."""
    r = client.post(path, data={})
    assert r.status_code == 303, f"{path} -> {r.status_code}"
    assert r.headers["location"].startswith("/signin")


def test_a_gated_post_with_a_valid_body_still_does_not_run(client):
    """The one that matters. A well-formed brief submission from someone with
    no identity must not reach campaigns.create -- if it did, the insert would
    fail on created_by, but only after the request was accepted."""
    r = client.post("/campaigns", data={
        "name": "Should never be filed",
        "product_id": "00000000-0000-0000-0000-000000000000",
    })
    assert r.status_code == 303
    assert r.headers["location"].startswith("/signin")


# --------------------------------------------------------------------------
# Signing in
# --------------------------------------------------------------------------

def test_signin_form_renders_and_lists_the_operators(client):
    r = client.get("/signin")
    assert r.status_code == 200
    assert ALICE in r.text
    # The honesty note is part of the deliverable, not decoration.
    assert "not access control" in r.text
    assert "{{" not in r.text


def test_signin_form_states_the_no_passcode_mode(client):
    r = client.get("/signin")
    assert "no passcode set" in r.text
    assert "not access control" in r.text
    assert "OPERATOR_PASSCODE" in r.text


def test_signin_form_states_the_passcode_mode(client, monkeypatch):
    monkeypatch.setattr(settings, "operator_passcode", "hunter2")
    r = client.get("/signin")
    assert "passcode is shared" in r.text
    assert "not access control" in r.text
    assert 'type="password"' in r.text


def test_unknown_operator_is_refused(client):
    r = client.post("/signin", data={"operator": "mallory@example.com",
                                     "next": "/"})
    assert r.status_code == 303
    assert "sign-in%20failed" in r.headers["location"]
    assert auth.COOKIE not in r.cookies


def test_wrong_passcode_is_refused(client, monkeypatch):
    monkeypatch.setattr(settings, "operator_passcode", "hunter2")
    r = client.post("/signin", data={"operator": ALICE, "passcode": "nope",
                                     "next": "/"})
    assert r.status_code == 303
    assert "sign-in%20failed" in r.headers["location"]
    assert auth.COOKIE not in r.cookies


def test_the_two_failures_are_indistinguishable(client, monkeypatch):
    """Same message for a bad name and a bad passcode. Telling a caller which
    half was wrong is free help for guessing the other."""
    monkeypatch.setattr(settings, "operator_passcode", "hunter2")
    bad_name = client.post("/signin", data={"operator": "mallory@example.com",
                                            "passcode": "hunter2"})
    bad_code = client.post("/signin", data={"operator": ALICE,
                                            "passcode": "wrong"})
    assert bad_name.headers["location"] == bad_code.headers["location"]


def test_valid_signin_sets_a_verifiable_cookie(client, monkeypatch):
    monkeypatch.setattr(settings, "operator_passcode", "hunter2")
    r = client.post("/signin", data={"operator": ALICE, "passcode": "hunter2",
                                     "next": "/campaigns/new"})
    assert r.status_code == 303
    assert r.headers["location"] == "/campaigns/new"
    raw = r.cookies.get(auth.COOKIE)
    assert raw
    identity = auth.read(raw)
    assert identity is not None and identity.operator == ALICE


def test_the_cookie_is_httponly(client):
    r = client.post("/signin", data={"operator": ALICE, "next": "/"})
    header = r.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=lax" in header


@pytest.mark.parametrize("hostile", [
    "https://evil.example/",
    "http://evil.example",
    # The one a startswith("/") check lets through. Browsers read a leading
    # "//" as scheme-relative, so this leaves the site entirely.
    "//evil.example/",
    "///evil.example",
    "/\\evil.example",
    "javascript:alert(1)",
])
def test_an_offsite_next_is_not_honoured(client, hostile):
    """A signed-in redirect must not become an open redirect."""
    r = client.post("/signin", data={"operator": ALICE, "next": hostile})
    assert r.headers["location"] == "/", hostile


@pytest.mark.parametrize("hostile", ["//evil.example/", "https://evil.example"])
def test_a_hostile_next_survives_neither_success_nor_failure(client, hostile):
    """Both branches route through the same guard. The failure branch matters
    too -- it reflects `next` straight back into the sign-in URL."""
    failed = client.post("/signin", data={"operator": "nobody@example.com",
                                          "next": hostile})
    assert "evil.example" not in failed.headers["location"], hostile
    ok = client.post("/signin", data={"operator": ALICE, "next": hostile})
    assert "evil.example" not in ok.headers["location"], hostile


def test_a_local_next_is_preserved(client):
    r = client.post("/signin", data={"operator": ALICE,
                                     "next": "/campaigns/new"})
    assert r.headers["location"] == "/campaigns/new"


def test_signout_clears_the_cookie(client):
    signed_in = client.post("/signin", data={"operator": ALICE, "next": "/"})
    assert signed_in.cookies.get(auth.COOKIE)
    r = client.post("/signout")
    assert r.status_code == 303
    # Cleared by an expiry in the past rather than by absence.
    header = r.headers["set-cookie"].lower()
    assert f"{auth.COOKIE}=" in header
    assert "expires=thu, 01 jan 1970" in header or "max-age=0" in header


def test_signin_is_a_noop_when_identity_is_off(client, monkeypatch):
    monkeypatch.setattr(settings, "operators", "")
    r = client.get("/signin")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/?msg=")


def _mounted_paths(target) -> set[str]:
    """Every path the mounted app can actually resolve.

    Not a flat comprehension over app.routes. This FastAPI version's
    include_router does not copy routes into app.routes -- it inserts a lazy
    _IncludedRouter wrapper holding the original router by reference, and that
    wrapper has no .path. A flat walk therefore returns main.py's own routes
    and none of the UI's, which is worse than not looking: the assertions
    below still pass, on nothing.
    """
    found: set[str] = set()
    stack = list(getattr(target, "routes", []))
    seen: set[int] = set()
    while stack:
        route = stack.pop()
        if id(route) in seen:
            continue
        seen.add(id(route))
        path = getattr(route, "path", None)
        if isinstance(path, str):
            found.add(path)
        # An include wrapper (no path of its own) or a sub-application.
        inner = getattr(route, "original_router", None)
        if inner is None and not isinstance(path, str):
            inner = route
        if inner is not None:
            stack.extend(getattr(inner, "routes", []))
    return found


# --------------------------------------------------------------------------
# Structure: which router is a route on?
# --------------------------------------------------------------------------

def _paths(router) -> set[tuple[str, frozenset]]:
    return {(r.path, frozenset(r.methods)) for r in router.routes}


def test_only_signin_and_signout_are_reachable_without_an_identity():
    """The invariant the two-router split exists to hold.

    A new screen belongs on ui.router and is then gated by default. If this
    fails, something was added to public_router -- which is a security change
    and should have to be made deliberately, in this list.
    """
    assert _paths(ui.public_router) == {
        ("/signin", frozenset({"GET"})),
        ("/signin", frozenset({"POST"})),
        ("/signout", frozenset({"POST"})),
    }


#: Path prefixes that belong on the gated router. An allowlist rather than a
#: wildcard, so adding a screen means saying out loud that it needs the gate.
GATED_PREFIXES = ("/campaigns", "/exclusions")


def test_every_campaign_route_is_gated():
    gated = {r.path for r in ui.router.routes}
    assert gated, "the gated router is empty -- the gate is not mounted"
    for path in gated:
        assert path == "/" or path.startswith(GATED_PREFIXES), path
    # Spot-check that the writes really are here rather than on the open one.
    for path in ("/campaigns", "/campaigns/{campaign_id}/validate",
                 "/campaigns/{campaign_id}/qa"):
        assert path in gated, path


def test_the_gate_is_a_router_level_dependency():
    """Belt and braces: a per-route Depends would be easy to forget on a new
    route, so the gate lives on the router. Assert it is actually there."""
    calls = [d.dependency for d in ui.router.dependencies]
    assert auth.require_operator in calls


def test_the_kb_api_is_deliberately_not_gated():
    """A documented boundary, not an oversight. The agent and any n8n workflow
    call /search and /context with no cookie and no way to hold one. Those
    routes read; they do not write and they do not approve, so the audit trail
    this module protects is unaffected.

    If a future API route ever writes, it needs its own decision -- which is
    what this test is here to force.
    """
    api = {r.path for r in app.routes
           if getattr(r, "path", "").startswith(("/search", "/context",
                                                 "/documents", "/brands",
                                                 "/health"))}
    assert "/health" in api and "/search" in api
    gated = {r.path for r in ui.router.routes}
    assert not (api & gated)


# --------------------------------------------------------------------------
# Boot preflight
# --------------------------------------------------------------------------

def test_preflight_names_migrations_that_exist():
    """The preflight message tells someone what to run, so a typo in a
    filename would point them at nothing. Cheap to pin, and the failure is
    otherwise only visible in the window where the migration is pending."""
    from pathlib import Path

    import main

    folder = Path(main.__file__).resolve().parent / "migrations"
    assert main.REQUIRED_SCHEMA, "preflight has nothing to check"
    for filename, spec in main.REQUIRED_SCHEMA.items():
        assert (folder / filename).exists(), f"no such migration: {filename}"
        assert spec.get("breaks"), f"{filename} does not say what it breaks"
        needed = spec.get("columns", ())
        assert needed or spec.get("tables") or spec.get("constraints"),             f"{filename} requires nothing"
        sql = (folder / filename).read_text(encoding="utf-8")
        for table in spec.get("tables", ()):
            assert table in sql, f"{filename} never mentions table {table}"
        for table, column, want in needed:
            # These are information_schema.columns.data_type values, not
            # Postgres type names, and the two differ where it matters:
            # text[] reports as 'ARRAY' (udt_name '_text'). Getting this
            # wrong makes the preflight report a present column as missing.
            assert want in ("text", "uuid", "boolean", "integer",
                            "timestamp with time zone", "ARRAY"), want
            # A column the code needs must actually be added by that file.
            assert column in sql, f"{filename} never mentions {column}"
            assert table in sql, f"{filename} never mentions {table}"
        # 026 widens a CHECK rather than adding a column, so the spec names
        # the constraint and the value it must now permit. Same contract: the
        # file the message tells someone to run has to be the file that does
        # it.
        for table, constraint, want in spec.get("constraints", ()):
            assert table in sql, f"{filename} never mentions {table}"
            assert constraint in sql,                 f"{filename} never mentions {constraint}"
            assert want in sql, f"{filename} never permits {want!r}"


def test_revise_and_withdraw_paths_cannot_shadow_each_other():
    """A real bug, caught before it shipped.

    /revise/{kind}/{target_id} and /revise/{request_id}/withdraw both match
    "/revise/abc/withdraw". FastAPI matches in registration order, so the
    first would win and bind kind="abc", target_id="withdraw" -- a silent
    404-shaped failure on the withdraw button.

    Reordering would only hide it until someone adds a third route, so the fix
    was to make the two patterns unable to describe the same path: the
    withdraw route lives under /revisions/ and this pins that apart.
    """
    paths = {r.path for r in ui.router.routes}
    # "revis", not "/revise": the latter is not a substring of "/revisions"
    # and would silently match only one of the two routes -- which is how this
    # test first passed against a set of one.
    revise_paths = {p for p in paths if "revis" in p}
    assert revise_paths == {
        "/campaigns/{campaign_id}/revise/{kind}/{target_id}",
        "/campaigns/{campaign_id}/revisions/{request_id}/withdraw",
    }, revise_paths


def test_no_undeclared_route_overlaps():
    """Any two same-depth routes that can match one URL must be declared safe.

    Overlap is not automatically a bug -- FastAPI resolves it by registration
    order, and sometimes that is exactly right. It is a bug when nobody
    decided. So this enumerates every pair that overlaps and requires each to
    be in the allowlist below with a reason, which turns "we got lucky on
    ordering" into "we chose this".

    The pair that motivated it was /revise/{kind}/{target_id} against
    /revise/{request_id}/withdraw: two wildcards in the same position, so
    nothing could discriminate them and the withdraw button silently hit the
    wrong handler.
    """
    #: frozenset of two paths -> why the overlap is harmless.
    known_safe = {
        frozenset({"/campaigns/new", "/campaigns/{campaign_id}"}):
            "campaign_id is always a uuid, so it can never be the literal "
            "'new'; /campaigns/new is registered first regardless",
    }

    # The whole app, not one router. The pair that slipped through last time
    # sat on one router, but a pair spanning ui.router and public_router -- or
    # ui.router and main.py's own /documents and /brands routes -- is exactly
    # as ambiguous and was invisible here.
    paths = sorted(_mounted_paths(app))

    # This walk is the test's eyes, so prove it can see. Widening the scope
    # naively made this check examine 10 paths and none of the UI's 22, which
    # would have quietly switched off the only thing that catches shadowing.
    should_see = ({r.path for r in ui.router.routes}
                  | {r.path for r in ui.public_router.routes})
    missing = should_see - set(paths)
    assert not missing, (
        f"the walk is blind to {len(missing)} route(s), so this test is not "
        f"checking them: {sorted(missing)}")
    by_depth: dict[int, list[str]] = {}
    for path in paths:
        by_depth.setdefault(len(path.strip("/").split("/")), []).append(path)

    undeclared = []
    for group in by_depth.values():
        for i, a in enumerate(group):
            for bb in group[i + 1:]:
                pa, pb = a.strip("/").split("/"), bb.strip("/").split("/")
                # They can match the same URL when no position has two
                # DIFFERENT literals.
                overlaps = not any(
                    not x.startswith("{") and not y.startswith("{") and x != y
                    for x, y in zip(pa, pb))
                if overlaps and frozenset({a, bb}) not in known_safe:
                    undeclared.append((a, bb))

    assert not undeclared, (
        "route pairs that can match the same URL and are not declared safe:\n"
        + "\n".join(f"  {a}\n  {b}\n" for a, b in undeclared))


def test_the_two_routers_share_no_path():
    """A path registered on both routers is ambiguous in a way that matters.

    public_router is mounted first (main.py), so the public handler wins and
    the gated screen silently becomes the sign-in page -- a 200 with the wrong
    body, which is harder to notice than a 500.
    """
    shared = ({r.path for r in ui.router.routes}
              & {r.path for r in ui.public_router.routes})
    assert not shared, f"on both routers: {sorted(shared)}"
