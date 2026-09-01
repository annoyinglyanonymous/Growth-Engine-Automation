"""Operator identity for the UI.

WHAT THIS IS
Attribution. It puts a real name in approved_by, decided_by and validated_by
instead of the placeholder 'local-ui', so the approval trail can answer "who
approved this" without relying on anyone's memory.

WHAT THIS IS NOT
Access control, and the distinction is not pedantry. The app connects to
Postgres as a superuser with BYPASSRLS, so anyone who reaches the UI can drive
every write the buttons expose. A cookie does not change that. What it changes
is whether the row records who did it.

Claiming more than this would be worse than claiming nothing: a login page
implies a security boundary, and someone would reasonably conclude it was safe
to bind the app to 0.0.0.0. It is not. Real auth means a non-superuser DB role,
RLS policies that do not exist yet, TLS and a deploy target -- Phase 2, and its
own decision.

WHY THE AUDIT TRAIL IS THE THING WORTH FIXING FIRST
The strictest rule in the system is the FDD blocker from 012: franchise
marketing is not an offer to sell a franchise, and no offer may be made to
residents of states where the franchise is not registered. Every asset carries
a knowledge_snapshot recording the chunks, claims and rules that produced it,
and approved_at records when it was cleared. The chain was complete except for
the name -- and unlike a cookie, that gap gets permanently worse with time.
Every asset approved as 'local-ui' is a row nobody can retroactively attribute.

TWO MODES, AND THE UI SAYS WHICH
  no passcode set   sign-in is "pick your name from the allowlist". Forgeable
                    by anyone at the keyboard, and honest about it. Still an
                    improvement: the name is real, and it is one of a known
                    set rather than free text.
  passcode set      a shared secret is required before a name can be claimed.
                    Weak -- shared secrets do not distinguish people -- but it
                    stops a stray local process and a passing colleague.

Neither mode is authentication of a person. OPERATOR_PASSCODE is left unset by
default because inventing a secret on someone's behalf is how secrets end up
in git.

WHY STDLIB HMAC AND NOT A SESSION FRAMEWORK
Starlette's SessionMiddleware needs itsdangerous, which is not installed. For a
single signed value carrying a name and a timestamp, the dependency costs more
than the 30 lines below. No server-side session store either: there is nothing
to store beyond the name, and a store would need eviction, which is a second
thing to get wrong.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

from fastapi import Request

from config import settings

#: Cookie name. Prefixed so it cannot collide with anything the KB API sets.
COOKIE = "mge_operator"

#: What gets written when no operators are configured. Same value the UI used
#: before this module existed, so an unconfigured install behaves exactly as it
#: did rather than breaking -- but campaign.html shows a standing banner, and
#: startup_warning() below is printed once at boot.
UNCONFIGURED = "local-ui"


class NotSignedIn(Exception):
    """No valid identity on the request.

    A domain exception rather than an HTTPException, matching kb_context's
    UnknownBrand: the module does not know whether its caller wants a redirect,
    a 401, or a CLI prompt. main.py maps it to a redirect.
    """

    def __init__(self, next_url: str = "/") -> None:
        super().__init__("not signed in")
        self.next_url = next_url


@dataclass(frozen=True)
class Identity:
    operator: str
    issued_at: int


def operators() -> list[str]:
    """The allowlist, in configured order. Empty means identity is off."""
    return [p.strip() for p in (settings.operators or "").split(",")
            if p.strip()]


def enabled() -> bool:
    return bool(operators())


def passcode_required() -> bool:
    return bool(settings.operator_passcode)


def startup_warning() -> str | None:
    """One line for the boot log, or None when configured."""
    if enabled():
        mode = "passcode" if passcode_required() else "name only"
        return (f"identity: {len(operators())} operator(s), {mode}. "
                f"Approvals are attributed.")
    return ("identity: OPERATORS is unset -- approvals will be recorded as "
            f"'{UNCONFIGURED}', which names nobody. See .env.example.")


# --------------------------------------------------------------------------
# Cookie
# --------------------------------------------------------------------------

def _secret() -> bytes:
    """Signing key.

    A missing SESSION_SECRET falls back to a per-process random value, which
    works and invalidates every session on restart. That is a deliberate
    trade: a hard-coded default would be a signing key published in the source
    of every install, which is strictly worse than making people sign in again.
    """
    configured = (settings.session_secret or "").strip()
    if configured:
        return configured.encode("utf-8")
    global _EPHEMERAL
    if _EPHEMERAL is None:
        _EPHEMERAL = secrets.token_bytes(32)
    return _EPHEMERAL


_EPHEMERAL: bytes | None = None


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def issue(operator: str) -> str:
    """Signed cookie value for an operator. Caller must have verified them."""
    payload = json.dumps({"op": operator, "iat": int(time.time())},
                         separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(_secret(), payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(sig)}"


def read(value: str | None) -> Identity | None:
    """Verify a cookie value. None on anything wrong, never an exception.

    Rejects on: malformed shape, bad signature, expired age, and an operator
    no longer on the allowlist. That last one matters -- removing a name from
    OPERATORS must end their session immediately, not in twelve hours.
    """
    if not value or "." not in value:
        return None
    body, _, sig = value.rpartition(".")
    try:
        payload = _unb64(body)
        given = _unb64(sig)
    except Exception:      # noqa: BLE001 -- any decode failure is a rejection
        return None

    expected = hmac.new(_secret(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, given):
        return None

    try:
        data = json.loads(payload)
        operator = str(data["op"])
        issued_at = int(data["iat"])
    except Exception:      # noqa: BLE001
        return None

    if issued_at > time.time() + 60:
        return None                                   # clock skew or forgery
    if time.time() - issued_at > settings.session_max_age:
        return None
    if operator not in operators():
        return None
    return Identity(operator=operator, issued_at=issued_at)


def verify_passcode(given: str) -> bool:
    """Constant-time compare against the configured passcode."""
    expected = (settings.operator_passcode or "").strip()
    if not expected:
        return True
    return hmac.compare_digest(expected.encode("utf-8"),
                               (given or "").encode("utf-8"))


# --------------------------------------------------------------------------
# FastAPI plumbing
# --------------------------------------------------------------------------

def current(request: Request) -> str | None:
    """Operator on this request, or None. Never raises. For templates."""
    if not enabled():
        return None
    identity = read(request.cookies.get(COOKIE))
    return identity.operator if identity else None


def cli_operator(explicit: str | None = None) -> str:
    """Identity for a command-line action.

    An explicit --as wins. Otherwise the OS account, prefixed so it is
    obviously a different kind of identity from a signed-in UI operator: an
    audit trail that mixes 'chris@example.com' and 'cli:Renegade' is telling
    the truth about two different provenances, and flattening them into one
    format would hide that.

    Deliberately NOT the first name in OPERATORS. That would attribute every
    CLI action to whoever happens to be listed first, which is fabrication
    dressed as a default -- the same reason 016 backfills a sentinel instead of
    a plausible name.

    The existing --approved-by flags on the strategy and asset CLIs are left
    required rather than defaulted here. Approving copy is the one action where
    being asked who you are is the correct amount of friction.
    """
    if explicit and explicit.strip():
        return explicit.strip()
    import getpass
    try:
        return f"cli:{getpass.getuser()}"
    except Exception:      # noqa: BLE001 -- no OS identity available
        return "cli:unknown"


def require_operator(request: Request) -> str:
    """Dependency. -> the operator to attribute writes to.

    When OPERATORS is unset this returns UNCONFIGURED rather than raising, so
    an install that has not configured identity keeps working exactly as it did
    before. That is the one case where this function does not enforce anything,
    and it is why startup_warning() is loud about it.
    """
    if not enabled():
        return UNCONFIGURED
    identity = read(request.cookies.get(COOKIE))
    if identity is None:
        raise NotSignedIn(next_url=request.url.path)
    return identity.operator
