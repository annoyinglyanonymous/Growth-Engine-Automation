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


def test_every_campaign_route_is_gated():
    gated = {r.path for r in ui.router.routes}
    assert gated, "the gated router is empty -- the gate is not mounted"
    for path in gated:
        assert path == "/" or path.startswith("/campaigns"), path
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
    assert main.REQUIRED_COLUMNS, "preflight has nothing to check"
    for filename, (needed, breaks) in main.REQUIRED_COLUMNS.items():
        assert (folder / filename).exists(), f"no such migration: {filename}"
        assert needed, f"{filename} lists no columns"
        assert breaks, f"{filename} does not say what it breaks"
        for table, column, want in needed:
            assert want in ("text", "uuid", "boolean", "integer",
                            "timestamp with time zone"), want
            # A column the code needs must actually be added by that file.
            sql = (folder / filename).read_text(encoding="utf-8")
            assert column in sql, f"{filename} never mentions {column}"
            assert table in sql, f"{filename} never mentions {table}"
