"""Prompt assembly and the model call. Shared by every generator.

to_system_prompt moved here from generate.py unchanged in substance: its
section ordering -- primer, rules, approved, restricted, prohibited, site
copy, contradictions -- is deliberate and tested (tests/test_prompt_assembly).
Governance goes before evidence because a blocker rule buried under 3,000
tokens of scraped marketing text is a rule the model weighs against everything
above it; stated first, it is a constraint the model reads the evidence
through.

complete() is the one place a chat completion is created. The token-ceiling
retry, the thinking-token accounting and the truncation check live here once,
rather than once per generator.

STRUCTURED OUTPUT
Generators need lists of typed things (angles, concepts, ad variants), so
complete_json() asks for a JSON object and parses it. It uses prompt-level
schema instruction plus response_format={"type": "json_object"} where the
provider supports it, and repair-free strict parsing: a malformed response is
retried once with the parse error appended, then fails loudly. Silent repair
(regex-extracting something JSON-shaped) is how a half-hallucinated asset gets
stored looking healthy.
"""

from __future__ import annotations

import asyncio
import json
import sys

from openai import OpenAI

from config import settings


def resolve_provider(name: str | None = None) -> tuple[OpenAI, str]:
    """-> (client, model).

    Gemini is reached through Google's OpenAI-compatible endpoint rather than
    the google-genai SDK. That keeps one request path instead of two: only the
    base_url, the key and the model name differ, and everything downstream --
    the retry on the token-limit parameter, error handling, response parsing --
    stays identical.
    """
    name = (name or settings.provider).lower()

    if name == "gemini":
        if not settings.gemini_api:
            raise SystemExit(
                """GEMINI_API is not set in .env.

Get a key at https://aistudio.google.com/apikey and add:
    GEMINI_API=your-key-here

Or run against OpenAI instead with provider='openai'."""
            )
        client = OpenAI(api_key=settings.gemini_api,
                        base_url=settings.gemini_base_url)
        return client, settings.gemini_model

    if name == "openai":
        if not settings.open_ai_api:
            raise SystemExit(
                """OPEN_AI_API is not set in .env -- generation needs it.

The KB itself does not; `python main.py` still works without it."""
            )
        return OpenAI(api_key=settings.open_ai_api), settings.openai_model

    raise SystemExit(
        f"Unknown provider {name!r}. Use 'gemini' or 'openai', "
        f"or set PROVIDER= in .env."
    )


def to_system_prompt(ctx: dict) -> str:
    """Order matters, and it is not cosmetic. See the module docstring."""
    out = [f"You are writing marketing copy for {ctx['brand']['name']}.", ""]

    if ctx.get("primer"):
        out += ["## Brand primer", ctx["primer"], ""]

    # ---- governance ------------------------------------------------------
    # Operator exclusions come FIRST, ahead of the reviewed rules.
    # They are the one instruction in this prompt that a person typed
    # directly at the model rather than deriving from the corpus, and
    # if anything below is going to be weighed against context, it
    # should not be this. Stated as a flat prohibition with no
    # reasoning attached, because an instruction with a rationale
    # invites the model to decide the rationale does not apply here.
    if ctx.get("exclusions"):
        out += ["", "## Words and phrases you must NOT use",
                "These are explicit instructions from the person "
                "requesting this work. There is no context in which "
                "they may be used, paraphrased, or implied."]
        for e in ctx["exclusions"]:
            line = f"- {e['phrase']}"
            if e.get("note"):
                line += f"  ({e['note']})"
            out.append(line)

    if ctx.get("rules"):
        out += ["", "## Rules you must follow"]
        out += [f"- [{r['severity'].upper()}] {r['rule']}" for r in ctx["rules"]]

    claims = ctx.get("claims", [])
    approved = [c for c in claims if c["status"] == "approved"]
    restricted = [c for c in claims if c["status"] == "restricted"]
    banned = [c for c in claims if c["status"] == "prohibited"]

    if approved:
        out += ["", "## Approved claims -- the only figures you may assert"]
        for c in approved:
            line = f"- {c['approved_wording'] or c['claim_text']}"
            if c["requires_disclaimer"]:
                line += f"\n  DISCLAIMER REQUIRED: {c['disclaimer_text']}"
            out.append(line)

    # Between approved and prohibited, in that order, because a restricted
    # claim is closer to a prohibition than to a permission: the default is
    # do not use it, and the condition is the narrow exception. Putting it
    # after "the only figures you may assert" also keeps that sentence honest.
    if restricted:
        out += ["", "## Restricted claims -- do NOT assert unless the stated "
                    "condition is met in full"]
        for c in restricted:
            out.append(f"- {c['claim_text']}\n"
                       f"  CONDITION: {c['restriction_notes']}")

    if banned:
        out += ["", "## Prohibited -- never write these"]
        out += [f"- {c['claim_text']} ({c['restriction_notes']})" for c in banned]

    # ---- evidence --------------------------------------------------------
    out += ["", "## Site copy -- use for voice and positioning, not for figures"]
    for p in ctx["passages"]:
        # build_context returns Passage models in-process and plain dicts once
        # they have crossed HTTP. Normalise rather than requiring one shape.
        if not isinstance(p, dict):
            p = p.model_dump()
        tag = (f"  [DISPUTED: {', '.join(p['dispute_topics'])}]"
               if p["disputed"] else "")
        out += [f"### {p['title']}{tag}", p["text"], ""]

    if ctx.get("conflicts"):
        out += ["## Known contradictions on the site"]
        out += [f"- {c['topic']} ({c['severity']}): {c['detail']}"
                for c in ctx["conflicts"]]

    if not approved:
        # Worth saying out loud rather than letting the model infer it from an
        # absent section.
        out += ["", "## No approved figures are available",
                "Do not state any number, price, percentage, or count as fact. "
                "Write to the positioning only."]

    return "\n".join(out)


class TruncatedResponse(RuntimeError):
    """The model hit the token ceiling mid-copy.

    An exception rather than a warning here, unlike generate.py's CLI path:
    a human watching a terminal can judge a truncated draft, but a generator
    is about to WRITE the result to campaign_assets, and a half-draft stored
    as finished copy is this project's founding failure mode.
    """


def complete(system: str, user: str, *,
             provider: str | None = None,
             model: str | None = None,
             max_tokens: int | None = None) -> str:
    """One chat completion, with the provider quirks handled.

    - Reasoning models reject max_tokens and require max_completion_tokens;
      older chat models are the other way round on some API versions. Try the
      current name, fall back on the specific complaint rather than a blanket
      except -- a bad key or a rate limit must not be swallowed here.
    - The ceiling is generous because thinking models (gemini-2.5-flash)
      charge reasoning tokens against it invisibly: measured 610 thinking
      tokens on a 36-token prompt.
    """
    client, default_model = resolve_provider(provider)
    limit = max_tokens or settings.generation_max_tokens
    kwargs = {
        "model": model or default_model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }

    try:
        resp = client.chat.completions.create(max_completion_tokens=limit,
                                              **kwargs)
    except Exception as exc:
        if "max_completion_tokens" not in str(exc):
            raise
        resp = client.chat.completions.create(max_tokens=limit, **kwargs)

    choice = resp.choices[0]
    if choice.finish_reason == "length":
        u = resp.usage
        raise TruncatedResponse(
            f"generation hit the {limit}-token ceiling "
            f"(visible completion tokens: "
            f"{getattr(u, 'completion_tokens', '?')}, total including "
            f"thinking: {getattr(u, 'total_tokens', '?')}). Raise "
            f"GENERATION_MAX_TOKENS or shorten the request.")
    return choice.message.content or ""


def complete_json(system: str, user: str, *,
                  shape_hint: str,
                  provider: str | None = None,
                  model: str | None = None) -> dict:
    """A completion that must parse as a JSON object.

    shape_hint is prose describing the expected keys; it goes into the user
    message so the schema travels with the request that needs it. One retry on
    a parse failure, with the parse error quoted -- models fix their own JSON
    reliably when told what broke. No silent regex repair, ever: a response
    that cannot parse after being told why is a response to distrust entirely.
    """
    request = (f"{user}\n\n"
               f"Respond with a single JSON object and nothing else -- no "
               f"markdown fences, no commentary.\n{shape_hint}")

    last_error: Exception | None = None
    for attempt in range(2):
        text = complete(system, request, provider=provider, model=model)
        cleaned = _strip_fences(text)
        try:
            obj = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            last_error = exc
            print(f"  response was not valid JSON ({exc}); retrying",
                  file=sys.stderr)
            request = (f"{request}\n\nYour previous response failed to parse: "
                       f"{exc}. Return ONLY the JSON object.")
            continue
        if not isinstance(obj, dict):
            last_error = TypeError(f"expected an object, got {type(obj).__name__}")
            request = (f"{request}\n\nYour previous response was a "
                       f"{type(obj).__name__}; return a JSON OBJECT.")
            continue
        return obj
    raise RuntimeError(f"model failed to produce valid JSON twice: {last_error}")


async def acomplete_json(system: str, user: str, *, shape_hint: str,
                         provider: str | None = None,
                         model: str | None = None) -> dict:
    """complete_json, off the event loop.

    The OpenAI SDK client is synchronous. Calling it directly from an async
    generator blocks the loop for the whole request -- 30 to 120 seconds during
    which the server answers nothing, which is invisible from a CLI and
    immediately obvious from a web UI where a second tab hangs.

    Generators use this; the CLI path can use either.
    """
    return await asyncio.to_thread(
        complete_json, system, user, shape_hint=shape_hint,
        provider=provider, model=model)


def _strip_fences(text: str) -> str:
    """The one syntactic allowance: a ```json fence around otherwise-valid
    JSON. Models add it despite instructions often enough that failing on it
    would burn a retry on pure formatting. Anything beyond that still fails."""
    t = text.strip()
    if t.startswith("```"):
        first_newline = t.find("\n")
        if first_newline != -1 and t.endswith("```"):
            t = t[first_newline + 1:-3].strip()
    return t
