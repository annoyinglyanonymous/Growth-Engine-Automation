"""Application settings, read from .env."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str

    #: Supavisor session mode (port 5432). Small on purpose: FastAPI is one
    #: long-lived process and pooler connections are a shared budget.
    #: If you ever move to transaction mode (port 6543) you must also set
    #: prepare_threshold=None -- psycopg3 auto-prepares after 5 executions and
    #: transaction pooling does not support prepared statements.
    pool_min_size: int = 1
    pool_max_size: int = 5

    #: Seconds an idle connection above min_size is kept before being closed.
    #: Under Supabase's pooler, which closes idle server connections on its own
    #: schedule, retiring ours first turns a request-time OperationalError into
    #: a reconnect nobody sees. 240s is comfortably inside the pooler's window.
    pool_max_idle: float = 240.0

    #: Default context budget in tokens for POST /context.
    default_token_budget: int = 6_000

    #: Rough chars-per-token for English prose. Approximate by design: this
    #: decides how much fits in a prompt, not anything a model parses.
    chars_per_token: int = 4

    #: At most this many chunks from one document, so a single long page cannot
    #: consume the whole budget and crowd out other sources.
    max_chunks_per_document: int = 2

    # ---- generation (generate.py only) ----------------------------------
    #
    # Optional on purpose. The KB server must start without a generation key:
    # retrieval and copywriting are separate concerns and separate failures.
    # main.py never reads either of these.
    # Named to match the .env variable already in place (OPEN_AI_API).
    # pydantic-settings matches env names case-insensitively, so the field is
    # open_ai_api rather than the SDK's conventional OPENAI_API_KEY -- which
    # also means the SDK will NOT pick it up on its own and generate.py has to
    # pass it explicitly.
    open_ai_api: str | None = None

    #: Override in .env as OPENAI_MODEL without touching code.
    openai_model: str = "gpt-4o"

    #: Gemini rides the OpenAI SDK through Google's OpenAI-compatible endpoint.
    #: No second SDK and no second request path -- only the base_url, key and
    #: model differ. Add GEMINI_API to .env.
    gemini_api: str | None = None
    gemini_model: str = "gemini-3.6-flash"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"

    #: Which provider generate.py uses when --provider is not passed.
    provider: str = "gemini"

    # ---- hybrid retrieval ------------------------------------------------
    #
    #: Cosine-distance cap on the vector arm of kb.search_kb.
    #:
    #: CALIBRATED, not guessed -- scripts/calibrate_retrieval.py, 2026-08-31,
    #: 1,755 embedded chunks, brand=renegade:
    #:
    #:   must_match       0.1983 .. 0.3829  (median 0.3015)
    #:   absent_adjacent  0.3448 .. 0.4128
    #:   absent_offdomain 0.4674 .. 0.4918
    #:
    #: The populations OVERLAP: "errors and omissions coverage" reaches its
    #: correct answer only at 0.386, while "how do I handle a lapsed policy
    #: renewal" -- content a marketing corpus does not contain -- reaches an
    #: unrelated servicing FAQ at 0.345. No cap satisfies both.
    #:
    #: 0.335 takes the conservative branch: excludes every absent probe (closest
    #: is 0.3448) while keeping 7 of 8 must-match probes -- the 8th being the
    #: E&O vocabulary case at 0.3829, which no safe cap reaches.
    #:
    #: 0.33 was the first choice and it was wrong by 0.0003: "switching careers
    #: into insurance" sits at 0.3303 and was silently excluded. Margins here are
    #: thin (0.0068 of headroom), so re-run the calibration after any corpus or
    #: model change rather than assuming this number still holds.
    #:
    #: Biasing tight is right here because the two
    #: failures are not symmetric -- under-retrieving falls back to the lexical
    #: arm, while over-retrieving hands the agent unrelated content that reads
    #: as evidence. Off-domain nonsense sits at 0.4674+, comfortably excluded.
    #:
    #: NOTE: migration 009 declares the SQL-level default as 0.45. main.py
    #: always passes this value explicitly, so it is authoritative for the API.
    #: Anything calling kb.search_kb directly should pass it too.
    vector_max_distance: float = 0.335

    #: lexical | vector | hybrid
    search_mode: str = "hybrid"

    #: Query embeddings are cached in-process. Every search otherwise costs one
    #: request against a 1,000/day free-tier cap, and development repeats the
    #: same queries constantly.
    query_embedding_cache: int = 512

    #: Word-set (Jaccard) overlap above which a passage is treated as
    #: restating one already selected.
    #:
    #: MEASURED by scripts/calibrate_dedupe.py: 2,231 comparable pairs pooled
    #: across six probe queries, 40 candidates each, against the live corpus.
    #:
    #:   duplicate population   >= 0.6815   122 pairs
    #:   distinct population    <= 0.5449   2,109 pairs
    #:   widest gap             0.5449 -> 0.6815   (width 0.1366)
    #:
    #: Unlike vector_max_distance, whose populations genuinely overlap, these
    #: two separate cleanly, so 0.61 is the gap's midpoint and not a
    #: compromise.
    #:
    #: This was 0.72 first, set from a three-query sample whose distinct
    #: population topped out at 0.4675. Adding "selling my insurance agency"
    #: raised that ceiling to 0.5449 and revealed 0.72 sat ABOVE the gap --
    #: loose enough to miss five duplicate pairs scoring 0.68-0.72. 0.61
    #: catches those and loses nothing, since no distinct pair reaches it.
    #: Re-run the script after any corpus change rather than trusting this.
    #:
    #: Effect on the query that motivated the whole thing, "what makes us
    #: different from captive agencies": 40 candidates -> 13 dropped, which is
    #: the templated per-location FAQ family plus the HIG-branded restatements
    #: of FAQs already selected in their Renegade form. Passages carrying
    #: distinct information roughly doubled at identical token spend.
    near_duplicate_threshold: float = 0.61

    #: Passages shorter than this are exempt. They are cheap, and two brief
    #: passages on one topic can legitimately share most of their words.
    near_duplicate_min_chars: int = 240

    #: Ceiling for one generation call.
    #:
    #: Deliberately generous because thinking models charge reasoning against
    #: this same ceiling without reporting it in usage.completion_tokens.
    #: gemini-2.5-flash spent 610 thinking tokens on a 36-token prompt, so a
    #: 4,000-token governed prompt plus three ad variants overran the previous
    #: 2,000 and truncated mid-sentence. generate.py warns when a response
    #: finishes on 'length' rather than returning half a draft as if it were
    #: copy.
    generation_max_tokens: int = 8000

    # ---- operator identity (auth.py, ui.py) -----------------------------
    #
    # Attribution, not access control. See auth.py's module docstring for why
    # that distinction is load-bearing.

    #: Comma-separated allowlist of operator names, as they should appear in
    #: approved_by / decided_by / validated_by. Use whatever identifies a real
    #: person to whoever will read the audit trail -- an email is the obvious
    #: choice since two approved_by rows already carry one.
    #:
    #: EMPTY DISABLES IDENTITY. The UI then keeps working and records approvals
    #: as 'local-ui', exactly as it did before auth.py existed, and prints a
    #: warning at startup. An unconfigured install degrades rather than breaks;
    #: it just does not fix anything.
    operators: str = ""

    #: Shared secret required at sign-in. Unset means a name can be claimed
    #: without one -- forgeable by anyone at the keyboard, and the sign-in page
    #: says so rather than implying a security boundary that is not there.
    #:
    #: Deliberately no default. A default passcode is a published passcode.
    operator_passcode: str = ""

    #: HMAC key for the identity cookie. Unset falls back to a per-process
    #: random key, which works and invalidates sessions on every restart. A
    #: hard-coded default would ship one signing key to every install, which
    #: is worse than making people sign in again.
    session_secret: str = ""

    #: Cookie lifetime in seconds. 12 hours: long enough for a working day,
    #: short enough that a forgotten browser on a shared desk expires.
    session_max_age: int = 43_200


settings = Settings()  # type: ignore[call-arg]
