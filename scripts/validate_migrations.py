"""Static checks on migration SQL. No database connection, nothing applied.

    python scripts/validate_migrations.py
    python scripts/validate_migrations.py 015 019

Exists because the seed files in this project are long VALUES lists, and the
two ways they break are both invisible to a careful read:

  * a row with the wrong number of fields. Postgres reports this as a type
    error on some unrelated column twelve rows away, and only when it runs.
  * a status or severity string outside the column's CHECK vocabulary. Same --
    a runtime failure on a file that has already half-applied its earlier
    statements if the author forgot a transaction.

Both are cheap to catch here. What this CANNOT catch is anything needing a
catalogue: a wrong column name, a missing table, a type mismatch. Those need
the server, so this is a first pass and not a substitute for review.

An earlier version of this check reported a false FAIL by grepping the whole
file for quoted words and testing them against claims.status -- it flagged
'active', which was a brand_rules.status value. Vocabulary is therefore
matched per column-list, never file-wide.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "migrations"

#: Column -> permitted values, applied only when that column appears in the
#: insert's column list AND the value sits in the matching VALUES position.
VOCAB: dict[str, set[str]] = {
    "status": {
        # union across tables; the per-file column list decides which apply
        "approved", "restricted", "prohibited", "pending_review",
        "active", "inactive", "coming_soon", "deprecated", "archived",
        "draft", "review", "rejected", "superseded",
        "validating", "blocked", "needs_info", "validated",
        "strategy", "production", "live", "completed",
        "pass", "warning",
    },
    "severity": {"blocker", "warning", "info"},
}

CHANNELS = {"email", "meta_ads", "google_ads", "landing_page", "sms",
            "video", "organic_social"}


def strip_sql(text: str) -> str:
    """Blank out comments and string bodies, preserving length and newlines.

    Length preservation matters: it keeps reported line numbers honest.
    """
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":   # '' escape
                        out[j] = out[j + 1] = " "
                        j += 2
                        continue
                    break
                if text[j] != "\n":
                    out[j] = " "
                j += 1
            i = j + 1
            continue
        if ch == "-" and i + 1 < n and text[i + 1] == "-":
            j = i
            while j < n and text[j] != "\n":
                out[j] = " "
                j += 1
            i = j
            continue
        if text.startswith("$$", i):
            j = text.find("$$", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if text[k] != "\n":
                    out[k] = " "
            i = j
            continue
        i += 1
    return "".join(out)


def check_balance(name: str, raw: str, bare: str) -> list[str]:
    bad = []
    if raw.count("begin;") != raw.count("commit;"):
        bad.append(f"{name}: begin;/commit; mismatch "
                   f"({raw.count('begin;')}/{raw.count('commit;')})")

    # $$ bodies must pair up
    if raw.count("$$") % 2:
        bad.append(f"{name}: odd number of $$ markers ({raw.count('$$')})")

    do_blocks = len(re.findall(r"\bdo\s*\$\$", raw))
    end_blocks = len(re.findall(r"\bend\s*\$\$\s*;", raw))
    if do_blocks != end_blocks:
        bad.append(f"{name}: do $$ / end $$; mismatch "
                   f"({do_blocks}/{end_blocks})")

    # Unterminated quote: an odd count survives stripping as a run to EOF.
    depth, line = 0, 1
    for ch in bare:
        if ch == "\n":
            line += 1
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                bad.append(f"{name}:{line}: unbalanced ')'")
                return bad
    if depth:
        bad.append(f"{name}: {depth} unclosed '(' at end of file")
    return bad


def split_rows(body: str) -> list[str]:
    """Top-level (...) groups inside a VALUES body."""
    rows, depth, start, bracket = [], 0, None, 0
    for i, ch in enumerate(body):
        if ch == "[":
            bracket += 1
        elif ch == "]":
            bracket -= 1
        elif bracket == 0 and ch == "(":
            if depth == 0:
                start = i + 1
            depth += 1
        elif bracket == 0 and ch == ")":
            depth -= 1
            if depth == 0 and start is not None:
                rows.append(body[start:i])
                start = None
    return rows


def count_fields(row: str) -> int:
    """Commas at depth zero, +1. Brackets and parens both nest."""
    depth = commas = 0
    for ch in row:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == "," and depth == 0:
            commas += 1
    return commas + 1


def find_matching(text: str, open_at: int) -> int:
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def check_values(name: str, raw: str, bare: str) -> list[str]:
    """Every VALUES row must match its `as v(...)` alias arity."""
    bad = []
    for m in re.finditer(r"\(\s*values\b", bare, re.I):
        open_at = m.start()
        close_at = find_matching(bare, open_at)
        if close_at < 0:
            bad.append(f"{name}: unterminated (values ...)")
            continue
        body = bare[open_at + 1:close_at]
        body = body[body.lower().index("values") + 6:]

        alias = re.match(r"\s*as\s+\w+\s*\(", bare[close_at + 1:], re.I)
        if not alias:
            bad.append(f"{name}: (values ...) with no `as v(...)` alias")
            continue
        a_open = close_at + 1 + alias.end() - 1
        a_close = find_matching(bare, a_open)
        names = [c.strip() for c in bare[a_open + 1:a_close].split(",")]
        want = len(names)

        rows = split_rows(body)
        if not rows:
            bad.append(f"{name}: (values ...) with no rows")
        line0 = bare.count("\n", 0, open_at) + 1
        for idx, row in enumerate(rows, 1):
            got = count_fields(row)
            if got != want:
                bad.append(
                    f"{name}: VALUES near line {line0}: row {idx} has {got} "
                    f"field(s), alias declares {want} ({', '.join(names)})")

        # Vocabulary, positionally, using the RAW text of each row so the
        # string literals are visible again.
        raw_body = raw[open_at + 1:close_at]
        raw_body = raw_body[raw_body.lower().index("values") + 6:]
        for idx, row in enumerate(split_rows(raw_body), 1):
            fields = split_fields_raw(row)
            if len(fields) != want:
                continue          # arity already reported
            for col, val in zip(names, fields):
                bad += check_vocab(name, line0, idx, col, val)
    return bad


def split_fields_raw(row: str) -> list[str]:
    depth, cur, out, q = 0, [], [], False
    i = 0
    while i < len(row):
        ch = row[i]
        if q:
            if ch == "'":
                if i + 1 < len(row) and row[i + 1] == "'":
                    cur.append("''")
                    i += 2
                    continue
                q = False
            cur.append(ch)
        elif ch == "'":
            q = True
            cur.append(ch)
        elif ch in "([":
            depth += 1
            cur.append(ch)
        elif ch in ")]":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    out.append("".join(cur))
    return out


LITERAL = re.compile(r"^\s*'((?:[^']|'')*)'(?:::\w+(?:\[\])?)?\s*$")


def check_vocab(name: str, line0: int, idx: int, col: str,
                val: str) -> list[str]:
    col = col.strip().lower()
    if col in VOCAB:
        m = LITERAL.match(val)
        if m and m.group(1).replace("''", "'") not in VOCAB[col]:
            return [f"{name}: VALUES near line {line0}: row {idx} column "
                    f"{col!r} = {m.group(1)!r} is outside the known "
                    f"vocabulary"]
    if col in ("channels", "applies_to_channels"):
        for ch in re.findall(r"'([^']+)'", val):
            if ch not in CHANNELS:
                return [f"{name}: VALUES near line {line0}: row {idx} "
                        f"channel {ch!r} is not a known channel"]
    return []


def main() -> int:
    wanted = sys.argv[1:]
    files = sorted(MIGRATIONS.glob("*.sql"))
    if wanted:
        files = [f for f in files
                 if any(f.name.startswith(w) or w in f.name for w in wanted)]
    if not files:
        print("no migration files matched")
        return 1

    problems: list[str] = []
    for path in files:
        raw = path.read_text(encoding="utf-8")
        bare = strip_sql(raw)
        found = (check_balance(path.name, raw, bare)
                 + check_values(path.name, raw, bare))
        status = "FAIL" if found else "ok"
        rows = len(re.findall(r"\(\s*values\b", bare, re.I))
        print(f"  {status:4}  {path.name:46} "
              f"{len(raw.splitlines()):4} lines, {rows} values block(s)")
        problems += found

    if problems:
        print(f"\n{len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"\nall {len(files)} file(s) pass static checks "
          f"(syntax balance, VALUES arity, status/channel vocabulary)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
