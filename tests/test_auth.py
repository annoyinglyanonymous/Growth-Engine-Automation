"""Tests for operator identity.

The cookie is signed by hand rather than by a library, so it gets the tests a
library would have come with. Every case below is a way a signed value can be
wrong, and each one must fail closed -- read() returns None and never raises,
because a malformed cookie arriving from anywhere must not be able to 500 the
sign-in page.

What is NOT claimed here, and is worth restating because a test file called
test_auth.py implies otherwise: this is attribution, not access control. The app
connects to Postgres as a superuser that bypasses RLS. These tests prove the
name in approved_by cannot be forged through the cookie; they prove nothing
about what a local process can reach.
"""

from __future__ import annotations

import base64
import json
import time

import pytest

import auth
from config import settings

SECRET = "test-secret-not-the-real-one"
ALICE = "alice@example.com"
BOB = "bob@example.com"


@pytest.fixture
def configured(monkeypatch):
    """Identity on, two operators, known signing key, no passcode."""
    monkeypatch.setattr(settings, "operators", f"{ALICE},{BOB}")
    monkeypatch.setattr(settings, "operator_passcode", "")
    monkeypatch.setattr(settings, "session_secret", SECRET)
    monkeypatch.setattr(settings, "session_max_age", 3600)
    return settings


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("", []),
    ("a@b.c", ["a@b.c"]),
    ("a@b.c,d@e.f", ["a@b.c", "d@e.f"]),
    ("  a@b.c ,  d@e.f  ", ["a@b.c", "d@e.f"]),
    ("a@b.c,,d@e.f", ["a@b.c", "d@e.f"]),      # a stray comma is not an entry
    (",", []),
])
def test_operators_parsing(monkeypatch, raw, expected):
    monkeypatch.setattr(settings, "operators", raw)
    assert auth.operators() == expected
    assert auth.enabled() is bool(expected)


def test_unconfigured_startup_warning_names_the_placeholder(monkeypatch):
    """An install that records approvals as nobody has to say so."""
    monkeypatch.setattr(settings, "operators", "")
    warning = auth.startup_warning()
    assert warning is not None
    assert auth.UNCONFIGURED in warning


def test_configured_startup_line_states_the_mode(configured, monkeypatch):
    assert "name only" in auth.startup_warning()
    monkeypatch.setattr(settings, "operator_passcode", "hunter2")
    assert "passcode" in auth.startup_warning()


# --------------------------------------------------------------------------
# The cookie: happy path
# --------------------------------------------------------------------------

def test_round_trip(configured):
    identity = auth.read(auth.issue(ALICE))
    assert identity is not None
    assert identity.operator == ALICE
    assert abs(identity.issued_at - time.time()) < 5


def test_two_operators_get_distinguishable_cookies(configured):
    assert auth.issue(ALICE) != auth.issue(BOB)
    assert auth.read(auth.issue(BOB)).operator == BOB


# --------------------------------------------------------------------------
# The cookie: every way it can be wrong
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    None,
    "",
    "no-dot-at-all",
    ".",
    "a.b",                                   # not base64 of anything useful
    "!!!.!!!",                               # invalid base64 alphabet
    "e30.",                                  # empty signature
    ".c2ln",                                 # empty payload
])
def test_malformed_values_are_rejected_without_raising(configured, value):
    """Fails closed and never raises: a malformed cookie must not be able to
    500 the page that would let someone sign in again."""
    assert auth.read(value) is None


def test_tampered_payload_is_rejected(configured):
    """The point of signing. Swap the name, keep the signature."""
    good = auth.issue(ALICE)
    body, _, sig = good.rpartition(".")
    forged_payload = json.dumps({"op": BOB, "iat": int(time.time())},
                                separators=(",", ":"),
                                sort_keys=True).encode()
    forged = (base64.urlsafe_b64encode(forged_payload)
              .decode().rstrip("=") + "." + sig)
    assert forged != good
    assert auth.read(forged) is None


def test_tampered_signature_is_rejected(configured):
    body, _, sig = auth.issue(ALICE).rpartition(".")
    flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
    assert auth.read(f"{body}.{flipped}") is None


def test_cookie_from_a_different_secret_is_rejected(configured, monkeypatch):
    """Rotating SESSION_SECRET invalidates outstanding cookies."""
    issued = auth.issue(ALICE)
    monkeypatch.setattr(settings, "session_secret", "a-different-secret")
    assert auth.read(issued) is None


def test_expired_cookie_is_rejected(configured, monkeypatch):
    monkeypatch.setattr(settings, "session_max_age", 60)
    old = json.dumps({"op": ALICE, "iat": int(time.time()) - 3600},
                     separators=(",", ":"), sort_keys=True).encode()
    assert auth.read(_sign(old)) is None


def test_future_dated_cookie_is_rejected(configured):
    """A far-future iat would otherwise never expire. 60s of skew is allowed;
    an hour is not a clock problem."""
    future = json.dumps({"op": ALICE, "iat": int(time.time()) + 3600},
                        separators=(",", ":"), sort_keys=True).encode()
    assert auth.read(_sign(future)) is None


def test_small_clock_skew_is_tolerated(configured):
    skewed = json.dumps({"op": ALICE, "iat": int(time.time()) + 10},
                        separators=(",", ":"), sort_keys=True).encode()
    assert auth.read(_sign(skewed)) is not None


def test_removing_an_operator_ends_their_session_immediately(configured,
                                                            monkeypatch):
    """The reason read() re-checks the allowlist rather than trusting the
    signature. Taking someone off OPERATORS must not leave them signed in for
    the rest of the cookie lifetime."""
    issued = auth.issue(BOB)
    assert auth.read(issued) is not None
    monkeypatch.setattr(settings, "operators", ALICE)
    assert auth.read(issued) is None


def test_payload_that_is_signed_but_not_json_is_rejected(configured):
    assert auth.read(_sign(b"not json at all")) is None


def test_payload_missing_fields_is_rejected(configured):
    assert auth.read(_sign(b'{"op":"' + ALICE.encode() + b'"}')) is None
    assert auth.read(_sign(b'{"iat":0}')) is None


def _sign(payload: bytes) -> str:
    import hashlib
    import hmac
    sig = hmac.new(SECRET.encode(), payload, hashlib.sha256).digest()
    enc = lambda raw: base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{enc(payload)}.{enc(sig)}"


# --------------------------------------------------------------------------
# Passcode
# --------------------------------------------------------------------------

def test_passcode_unset_accepts_anything(configured):
    """Documenting the mode rather than endorsing it: with no passcode set,
    sign-in is a name claim. The sign-in page says so in those words."""
    assert auth.verify_passcode("") is True
    assert auth.verify_passcode("whatever") is True


def test_passcode_set_requires_exact_match(configured, monkeypatch):
    monkeypatch.setattr(settings, "operator_passcode", "correct horse")
    assert auth.verify_passcode("correct horse") is True
    assert auth.verify_passcode("correct hors") is False
    assert auth.verify_passcode("Correct Horse") is False
    assert auth.verify_passcode("") is False
    assert auth.verify_passcode(None) is False


def test_configured_passcode_is_whitespace_trimmed(configured, monkeypatch):
    """A trailing newline in .env must not silently become part of the
    secret -- that failure looks like 'the passcode does not work'."""
    monkeypatch.setattr(settings, "operator_passcode", "  secret\n")
    assert auth.verify_passcode("secret") is True


# --------------------------------------------------------------------------
# CLI identity
# --------------------------------------------------------------------------

def test_cli_operator_prefers_explicit():
    assert auth.cli_operator("chris@example.com") == "chris@example.com"
    assert auth.cli_operator("  spaced  ") == "spaced"


def test_cli_operator_falls_back_to_the_os_user():
    """Prefixed so a CLI identity is visibly a different kind of thing from a
    signed-in UI operator. Flattening the two formats would hide that."""
    value = auth.cli_operator(None)
    assert value.startswith("cli:")
    assert len(value) > len("cli:")


def test_cli_operator_never_borrows_a_configured_name(configured):
    """Defaulting to the first entry in OPERATORS would attribute every CLI
    action to whoever happens to be listed first -- fabrication dressed as a
    default, and the same mistake 016 avoids by backfilling a sentinel."""
    assert auth.cli_operator(None) not in (ALICE, BOB)


# --------------------------------------------------------------------------
# require_operator
# --------------------------------------------------------------------------

class _Req:
    """Just enough of a Request for the dependency."""

    def __init__(self, cookies: dict, path: str = "/campaigns/x"):
        self.cookies = cookies
        self.url = type("U", (), {"path": path})()


def test_require_operator_returns_the_signed_in_name(configured):
    req = _Req({auth.COOKIE: auth.issue(ALICE)})
    assert auth.require_operator(req) == ALICE
    assert auth.current(req) == ALICE


def test_require_operator_raises_with_the_path_to_return_to(configured):
    with pytest.raises(auth.NotSignedIn) as caught:
        auth.require_operator(_Req({}, path="/campaigns/abc"))
    assert caught.value.next_url == "/campaigns/abc"


def test_require_operator_degrades_when_identity_is_off(monkeypatch):
    """An install that has not configured OPERATORS keeps working exactly as
    it did before auth.py existed. The banner and the boot line are what stop
    that being silent."""
    monkeypatch.setattr(settings, "operators", "")
    assert auth.require_operator(_Req({})) == auth.UNCONFIGURED
    assert auth.current(_Req({})) is None
