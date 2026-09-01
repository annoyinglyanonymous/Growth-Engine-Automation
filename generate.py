"""Ad-hoc grounded generation from the command line.

The exploratory tool, not the pipeline. The campaign pipeline lives in
generators/ -- strategy, angles, concepts, meta_ads, email_sequence -- each
writing versioned rows plus a knowledge snapshot. This script is for quick
what-would-the-model-say checks against an arbitrary topic and brief, and it
writes nothing anywhere.

It stays on HTTP (unlike the generators, which import kb_context directly)
precisely because it is the out-of-process client: it exercises the same
/context endpoint that n8n or any external caller would use, so if the API
breaks, this breaks the same way.

    python main.py --port 8000        # terminal 1: the KB
    python generate.py                # terminal 2: this

    python generate.py --provider openai
    python generate.py --show-prompt  # no API key needed

Keys come from .env via config.Settings -- GEMINI_API and OPEN_AI_API. Neither
matches the name its SDK auto-detects, so both are passed explicitly.

Prompt assembly and the model call live in generators/prompting.py and are
shared with the pipeline; this file owns only the HTTP fetch and the CLI.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

from config import settings
from generators.prompting import (
    TruncatedResponse,
    complete,
    resolve_provider,  # noqa: F401  -- re-exported; scripts import it from here
    to_system_prompt,
)

# Windows consoles default to cp1252 and this script's whole job is printing
# corpus text. The Renegade corpus contained U+202F (narrow no-break space), so
# --show-prompt died with UnicodeEncodeError on precisely the pages it exists
# to inspect. Reconfigure rather than asking every caller to remember
# PYTHONIOENCODING=utf-8. (U+202F is now stripped at ingestion, but the class
# of character is bigger than one codepoint.)
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

KB_URL = os.environ.get("KB_URL", "http://127.0.0.1:8000")


def fetch_context(brand: str, topic: str, budget: int = 6000) -> dict:
    """One call. Returns primer + rules + claims + passages + conflicts,
    already packed to a token budget by the KB."""
    try:
        r = httpx.post(f"{KB_URL}/context", timeout=30, json={
            "brand": brand, "query": topic, "token_budget": budget})
    except httpx.ConnectError:
        # By far the most common failure here, and httpx's own traceback is
        # forty lines of httpcore internals that never name the cause.
        port = KB_URL.rsplit(":", 1)[-1]
        raise SystemExit(
            f"""Cannot reach the KB at {KB_URL} -- it is not running.

Start it in another terminal:
    python main.py --port {port}

Or point this at a different host:  KB_URL=http://host:port python generate.py"""
        ) from None
    except httpx.ReadTimeout:
        raise SystemExit(
            f"""The KB at {KB_URL} accepted the connection but did not answer
in 30s. On Windows that is usually the psycopg event-loop problem: start the
KB with `python main.py`, never with the bare `uvicorn main:app` CLI."""
        ) from None

    if r.status_code == 404:
        raise SystemExit(r.json().get("detail", r.text))
    if r.status_code >= 500:
        raise SystemExit(
            f"""The KB returned {r.status_code}. Its response body is empty by
design -- the traceback is in the KB's own terminal.

{r.text}"""
        )
    r.raise_for_status()
    return r.json()


def generate(brand: str, topic: str, brief: str, budget: int = 6000,
             model: str | None = None,
             provider: str | None = None) -> tuple[str, dict, str]:
    """-> (copy, snapshot, system_prompt).

    The snapshot must come from the same response that produced the prompt --
    refetching later would record a different corpus state than the one the
    copy was actually written from.
    """
    ctx = fetch_context(brand, topic, budget)
    system = to_system_prompt(ctx)
    try:
        copy = complete(system, brief, provider=provider, model=model)
    except TruncatedResponse as exc:
        # For the interactive tool a truncation is a warning, not a failure:
        # a human is looking at the output and can judge the fragment. The
        # pipeline generators let this raise instead, because they are about
        # to store the result as a finished asset.
        print(f"WARNING: {exc}", file=sys.stderr)
        copy = ""
    return copy, ctx["snapshot"], system


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--brand", default="renegade")
    ap.add_argument("--topic", default="franchise ownership back office support",
                    help="what to retrieve from the KB")
    ap.add_argument("--brief", default="Write 5 headline options for a "
                                       "franchise recruitment ad.",
                    help="what to write")
    ap.add_argument("--budget", type=int, default=6000)
    ap.add_argument("--model", default=None,
                    help="override the provider's default model")
    ap.add_argument("--provider", default=None, choices=["gemini", "openai"],
                    help=f"default: {settings.provider} (set PROVIDER= in .env)")
    ap.add_argument("--show-prompt", action="store_true",
                    help="print the assembled system prompt and exit")
    args = ap.parse_args()

    if args.show_prompt:
        # No API key needed for this path -- useful for checking that rules and
        # claims are actually reaching the prompt.
        ctx = fetch_context(args.brand, args.topic, args.budget)
        print(to_system_prompt(ctx))
        return 0

    copy, snapshot, _ = generate(args.brand, args.topic, args.brief,
                                 args.budget, args.model, args.provider)
    print(copy)
    print("\n--- snapshot (for campaign_validations.knowledge_snapshot) ---")
    print(json.dumps(snapshot, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
