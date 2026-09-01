"""Look for credential-shaped strings in this repository.

    python scripts/scan_secrets.py            # everything git tracks
    python scripts/scan_secrets.py --staged   # only what is staged (the hook)
    python scripts/scan_secrets.py --install-hook

WHY THIS EXISTS
.gitignore already keeps .env out of the repository, and it worked -- the
database password and the session secret never entered git. It cannot help with
the failure that actually happened: a credential pasted directly into a tracked
source file. An orphaned commented-out line sat in config.py and went into the
first commit, and GitHub's push protection was what caught it, after the push.

An ignore rule protects files. This protects content.

WHAT IT WILL AND WILL NOT CATCH
The patterns below are the shapes of real credentials -- Google, OpenAI, AWS,
Slack, JWTs, Postgres URLs with inline passwords, PEM blocks. A secret with no
recognisable shape (a bare 12-character password, an internal token format)
will pass, so this lowers the odds rather than removing them. GitHub's own
scanning stays the backstop; the point of running it here is to find things
before they are in history, because removing a secret from history is
strictly harder than not committing it.

NEVER PRINTS A SECRET
Hits are reported as a six-character prefix and a length. A scanner that echoes
what it found puts the secret in a terminal log, a CI log, and whatever
scrollback the log lands in.

FALSE POSITIVES
Obvious placeholders are skipped, and so are values that are self-evidently
test fixtures. tests/test_auth.py deliberately assigns a constant named SECRET;
it holds the string "test-secret-not-the-real-one", which is the kind of thing
an allowlist is for. Add to ALLOW rather than loosening a pattern -- a pattern
loose enough to skip one real fixture is loose enough to miss a real key.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PATTERNS: dict[str, re.Pattern] = {
    "Google API key": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    # The shape that got through: GitHub classifies it as a GCP API key bound
    # to a service account.
    "Google AQ token": re.compile(r"\bAQ\.[A-Za-z0-9_\-]{20,}"),
    "Google OAuth token": re.compile(r"\bya29\.[A-Za-z0-9_\-]{20,}"),
    "OpenAI key": re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_\-]{20,}"),
    "JWT / Supabase key": re.compile(
        r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    "Postgres URL with password": re.compile(
        r"postgres(?:ql)?://[^\s:/]+:[^\s@]{6,}@"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}"),
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "assigned long secret": re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|credential)\b"
        r"\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{24,}"),
}

#: Substrings that mark a match as documentation rather than a credential.
PLACEHOLDERS = ("<", ">", "REDACTED", "xxx", "XXX", "your-", "your_",
                "example", "changeme", "placeholder", "dummy", "fake")

#: (path suffix, substring) pairs that are known, reviewed fixtures.
ALLOW: tuple[tuple[str, str], ...] = (
    ("tests/test_auth.py", "test-secret-not-the-real-one"),
    ("tests/test_ui_routes.py", "route-test-secret"),
    (".env.example", "password"),
)

SKIP_SUFFIXES = (".pyc", ".pyo", ".png", ".jpg", ".jpeg", ".gif", ".ico",
                 ".pdf", ".woff", ".woff2", ".ttf", ".zip", ".gz")


def tracked(staged: bool) -> list[str]:
    if staged:
        cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"]
    else:
        cmd = ["git", "ls-files"]
    out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                         check=True).stdout
    return [p for p in out.replace("\r", "").split("\n") if p]


def allowed(path: str, matched: str) -> bool:
    if any(token in matched for token in PLACEHOLDERS):
        return True
    return any(path.endswith(suffix) and needle in matched
               for suffix, needle in ALLOW)


def scan_text(path: str, text: str) -> list[str]:
    hits = []
    for lineno, line in enumerate(text.split("\n"), 1):
        for label, rx in PATTERNS.items():
            found = rx.search(line)
            if not found:
                continue
            raw = found.group(0)
            if allowed(path, raw):
                continue
            hits.append(f"{path}:{lineno}  [{label}]  "
                        f"{raw[:6]}<{len(raw)} chars, not shown>")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--staged", action="store_true",
                    help="scan staged changes only (used by the git hook)")
    ap.add_argument("--install-hook", action="store_true",
                    help="write .git/hooks/pre-commit")
    args = ap.parse_args()

    if args.install_hook:
        return install_hook()

    files = tracked(args.staged)
    if not files:
        print("nothing to scan")
        return 0

    hits: list[str] = []
    scanned = 0
    for path in files:
        if path.endswith(SKIP_SUFFIXES):
            continue
        full = ROOT / path
        if not full.exists():          # staged deletion
            continue
        try:
            text = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        hits += scan_text(path, text)

    if hits:
        print(f"BLOCKED: {len(hits)} possible secret(s) in "
              f"{'staged changes' if args.staged else 'tracked files'}:\n",
              file=sys.stderr)
        for hit in hits:
            print(f"  {hit}", file=sys.stderr)
        print("\nRemove the value and read it from .env instead. If it is a "
              "false positive, add it to ALLOW in scripts/scan_secrets.py "
              "with a reason.\n"
              "If it has already been committed, the credential must be "
              "rotated -- assume it is public.", file=sys.stderr)
        return 1

    print(f"{scanned} file(s) scanned, no credential shapes found")
    return 0


HOOK = """#!/bin/sh
# Installed by scripts/scan_secrets.py --install-hook
# Blocks a commit that stages a credential-shaped string. Delete this file to
# remove it. `git commit --no-verify` bypasses it, which should feel wrong.
python scripts/scan_secrets.py --staged || exit 1
"""


def install_hook() -> int:
    hooks = ROOT / ".git" / "hooks"
    if not hooks.is_dir():
        print("no .git/hooks directory -- is this a git repository?",
              file=sys.stderr)
        return 1
    target = hooks / "pre-commit"
    if target.exists() and "scan_secrets" not in target.read_text(
            encoding="utf-8", errors="replace"):
        print(f"{target} already exists and is not ours -- not overwriting. "
              f"Add this line to it yourself:\n"
              f"  python scripts/scan_secrets.py --staged || exit 1",
              file=sys.stderr)
        return 1
    target.write_text(HOOK, encoding="utf-8", newline="\n")
    target.chmod(0o755)
    print(f"installed {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
