"""The deterministic checks. Pure functions, no database, no model.

Every function here takes plain data and returns Finding objects. That is the
whole design decision: these are the checks that must never produce a false
positive, so they must be testable exhaustively without a corpus, an API key
or a network. The fetching lives in brief.py and asset_qa.py; the judgement
lives here.

SHARED BETWEEN STAGES ON PURPOSE
A brief and an asset are both "text plus a campaign context", and the same
seven questions apply to both: does it contain a prohibited claim, is the
pricing consistent, does the CTA match, does it promote an unavailable
feature, does it mix campaign types, is anything required missing, does it fit
the channel's limits. Duplicating them per stage is how the two copies drift
until only one of them catches the $20,000.

WHY WORD-BOUNDARY MATCHING AND NOT SUBSTRING
"$20,000" inside "$20,000,000" is not the prohibited claim, and "exit" inside
"existing" is not exit messaging. Every text match here is anchored on word
boundaries, and the money matcher is numeric-aware. The first draft of this
module used `in` and flagged "no earnout" against the word "learnout" in a
test fixture -- a false positive in a blocker is worse than a missed warning,
because it teaches people to override the checker.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

Severity = Literal["blocker", "warning", "info"]


@dataclass(frozen=True)
class Finding:
    """One problem, with enough detail to fix it without re-running anything."""

    check: str
    severity: Severity
    message: str
    #: Which field of the asset or brief. None for whole-object findings.
    field_name: str | None = None
    #: What was found, quoted, so a reviewer can search for it.
    evidence: str | None = None
    #: What to do instead, when there is a governed answer.
    remedy: str | None = None

    def as_dict(self) -> dict:
        return {k: v for k, v in {
            "check": self.check, "severity": self.severity,
            "message": self.message, "field": self.field_name,
            "evidence": self.evidence, "remedy": self.remedy,
        }.items() if v is not None}


@dataclass
class CheckContext:
    """Everything the checks need, already fetched.

    Assembled by the runners so the checks stay pure. Defaults are empty
    rather than None so a partially-populated context degrades to "fewer
    checks run" rather than AttributeError -- but see missing_brief_fields:
    an empty governance set is itself reported, so silence is never mistaken
    for a pass.
    """

    #: public.claims rows for the campaign's product.
    claims: list[dict] = field(default_factory=list)
    #: public.product_features rows.
    features: list[dict] = field(default_factory=list)
    #: The campaign row (brief).
    campaign: dict = field(default_factory=dict)
    #: campaign_types.prohibited_themes / allowed_themes.
    prohibited_themes: list[str] = field(default_factory=list)
    allowed_themes: list[str] = field(default_factory=list)
    #: kb.conflicts stale_values, flattened.
    stale_values: list[str] = field(default_factory=list)
    #: Operator-declared exclusions (023), from both lists, each as
    #: {phrase, note, scope}. scope is "brand" or "brief" and exists
    #: only so a blocker can say which list caught the line -- the
    #: reviewer needs to know whether to argue with the brief or with
    #: the brand. Enforcement is identical for both.
    exclusions: list[dict] = field(default_factory=list)
    #: The moment to measure claim staleness against, timezone-aware. Passed
    #: in rather than read from a clock so this module stays pure and the
    #: freshness check is testable at any date. None means the runner did not
    #: supply one, which is reported as an info finding rather than silently
    #: passing.
    now: object | None = None
    #: Days an approved claim stays trusted without another look.
    review_days: int = 180
    #: Per-category override, e.g. {"pricing": 90}. A price is the claim most
    #: likely to change without anyone updating marketing.
    review_days_by_category: dict[str, int] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Text normalisation
# --------------------------------------------------------------------------

#: Curly quotes, dashes and non-breaking spaces all appear in generated copy
#: and must not let a prohibited phrase slip past. Normalise before matching.
_PUNCT_MAP = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "−": "-",
    " ": " ", " ": " ", " ": " ",
}


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    for src, dst in _PUNCT_MAP.items():
        text = text.replace(src, dst)
    return re.sub(r"\s+", " ", text).strip()


def contains_phrase(haystack: str, needle: str) -> bool:
    """Word-boundary-anchored, case-insensitive phrase match.

    Internal whitespace in the needle matches any whitespace run, so
    "cash at close" matches "cash  at\\nclose". Punctuation inside the needle
    is escaped, and \\b is applied only where the edge character is a word
    character -- "$20,000" starts with '$', where \\b would never match.
    """
    n = normalise(needle)
    h = normalise(haystack)
    if not n or not h:
        return False
    pattern = r"\s+".join(re.escape(part) for part in n.split())
    left = r"\b" if n[0].isalnum() else ""
    right = r"\b" if n[-1].isalnum() else ""
    return re.search(f"{left}{pattern}{right}", h, re.I) is not None


_MONEY_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)")


def money_amounts(text: str) -> set[float]:
    """Every dollar amount in the text, as numbers.

    Numeric rather than textual so "$25,000" and "$25000" and "$ 25,000"
    compare equal, and so "$20,000,000" does not read as "$20,000".
    """
    out = set()
    for m in _MONEY_RE.finditer(normalise(text)):
        try:
            out.add(float(m.group(1).replace(",", "")))
        except ValueError:
            continue
    return out


def fields_of(content: dict) -> list[tuple[str, str]]:
    """(field_name, text) for every string field, so findings can name one."""
    return [(k, v) for k, v in (content or {}).items() if isinstance(v, str)]


# --------------------------------------------------------------------------
# 1. Prohibited wording
# --------------------------------------------------------------------------

#: Shortest exclusion that will be enforced, mirroring 023's CHECK on
#: brand_exclusions.phrase. A constant so the two cannot drift: if the schema
#: relaxes, this is the other place to look.
MIN_EXCLUSION_CHARS = 2


def _age_days(now, then) -> int | None:
    """Whole days between two datetimes, or None if they cannot be compared.

    last_reviewed_at is timestamptz so psycopg hands back an aware datetime,
    and the runner supplies an aware `now`. A naive value on either side would
    otherwise raise TypeError inside a check whose entire contract is that it
    cannot fail -- so it is coerced to UTC rather than trusted, and an
    genuinely incomparable pair returns None and is skipped.
    """
    try:
        if getattr(now, "tzinfo", None) is None or \
                getattr(then, "tzinfo", None) is None:
            now = now.replace(tzinfo=None)
            then = then.replace(tzinfo=None)
        return (now - then).days
    except (AttributeError, TypeError, ValueError):
        return None


def check_claim_freshness(content: dict,
                          ctx: CheckContext) -> list[Finding]:
    """Approved claims quoted in this copy that nobody has looked at lately.

    THE FAILURE THIS CATCHES IS SILENT
    An approved claim is asserted as fact for ever and nothing notices when
    the world moves. There are seven exact prices in public.claims with no
    expiry on any of them; the first anyone would learn that one had changed
    is a customer reading it.

    A WARNING, and deliberately not an effective_until date. A true claim must
    not stop being usable because a date somebody invented passed -- that
    disables correct copy on a schedule nobody chose. What a reviewer needs is
    to be told which figures are old while looking at the asset quoting them.

    Only claims the copy ACTUALLY USES. Warning about a claim this asset does
    not quote is noise, and noise in warnings teaches people to skim them.
    Matched two ways: the wording verbatim, and the money figures inside it --
    the second is what catches pricing, because copy says "$39.99/mo" rather
    than repeating the whole claim sentence.
    """
    if ctx.now is None:
        return [Finding(
            check="claim_freshness", severity="info",
            message="Claim review dates were not checked (no clock supplied).",
        )]

    text = " ".join(t for _, t in fields_of(content))
    if not text.strip():
        return []
    amounts = money_amounts(text)

    findings: list[Finding] = []
    for claim in ctx.claims:
        if claim.get("status") != "approved":
            continue
        wording = (claim.get("approved_wording")
                   or claim.get("claim_text") or "")
        if not (contains_phrase(text, wording)
                or (money_amounts(claim.get("claim_text") or "") & amounts)):
            continue

        horizon = ctx.review_days_by_category.get(
            claim.get("category"), ctx.review_days)
        reviewed = claim.get("last_reviewed_at")
        label = wording[:60]

        if reviewed is None:
            findings.append(Finding(
                check="claim_freshness", severity="warning",
                message="This copy quotes an approved claim that nobody has "
                        "confirmed.",
                evidence=label,
                remedy="Check it is still true, then set last_reviewed_at -- "
                       "scripts/claim_review.py lists them with their "
                       "sources.",
            ))
            continue

        age = _age_days(ctx.now, reviewed)
        if age is not None and age > horizon:
            findings.append(Finding(
                check="claim_freshness", severity="warning",
                message=f"This copy quotes a claim last reviewed {age} days "
                        f"ago ({horizon}-day limit for "
                        f"{claim.get('category') or 'this category'}).",
                evidence=label,
                remedy="Confirm it against the source and update "
                       "last_reviewed_at, or correct the claim.",
            ))

    # Per claim, not per field. One stale price is one thing to check, however
    # many times the asset mentions it.
    return _dedupe(findings)


def check_excluded_wording(content: dict,
                           ctx: CheckContext) -> list[Finding]:
    """Wording the operator said not to use (023).

    Separate from check_prohibited_wording on purpose, and it is the
    difference that matters: a prohibited CLAIM is a judgement about
    what is true, reached by review. An exclusion is an instruction,
    given by a person, needing no justification. Merging them would
    mean an operator typing "free forever" had to be modelled as a
    claim with a status and a source, which is both wrong and enough
    friction that nobody would do it.

    A blocker, not a warning. "Do not say this" is not advice, and a
    warning that can be clicked past is not a prohibition.
    """
    findings: list[Finding] = []
    for name, text in fields_of(content):
        for rule in ctx.exclusions:
            phrase = (rule.get("phrase") or "").strip()
            # Enforced here and not only in 023, because the two lists have
            # different guards: brand_exclusions has a CHECK, but
            # campaigns.do_not_mention is a text[] and Postgres cannot express
            # a per-element CHECK without a subquery. So a single character
            # typed into the brief reaches this function -- and
            # contains_phrase is word-boundary anchored, so "a" genuinely IS
            # a word in "a nice offer". One keystroke would block every asset
            # in the campaign, with a finding nobody could diagnose.
            if len(phrase) < MIN_EXCLUSION_CHARS:
                continue
            if not contains_phrase(text, phrase):
                continue
            where = ("the brief" if rule.get("scope") == "brief"
                     else "this brand")
            findings.append(Finding(
                check="excluded_wording", severity="blocker",
                field_name=name,
                message=f"Uses wording excluded on {where}.",
                evidence=phrase,
                # The operator's own note if they left one. Falling
                # back to a restatement rather than None keeps every
                # blocker actionable -- a finding with no remedy is a
                # complaint.
                remedy=(rule.get("note")
                        or f"Rewrite without {phrase!r}."),
            ))
    return findings


def check_prohibited_wording(content: dict, ctx: CheckContext) -> list[Finding]:
    """Prohibited claims and known-stale figures, per field.

    Sources are public.claims where status='prohibited' and
    kb.conflicts.stale_values. Both are needed: a claim row carries the
    reasoning and the remedy, while stale_values catches a figure whose
    conflict is recorded but which nobody has written a claim row for yet.
    """
    findings: list[Finding] = []
    approved_wordings = [
        c.get("approved_wording") or c.get("claim_text") or ""
        for c in ctx.claims if c.get("status") == "approved"
    ]

    for name, text in fields_of(content):
        for claim in ctx.claims:
            status = claim.get("status")
            if status not in ("prohibited", "restricted"):
                continue

            # Three ways a claim can be present, in order of reliability:
            #
            # 1. trigger_phrases -- written down and reviewed (migration 014).
            #    The only mechanism that catches a PARAPHRASE. Without it,
            #    "Licensed in all 50 states" passed clean while the prohibited
            #    claim "Renegade is licensed in all 50 states." sat unmatched
            #    in the table two rows away.
            # 2. the money figures inside the claim.
            # 3. the whole claim sentence, verbatim.
            triggered = [p for p in (claim.get("trigger_phrases") or [])
                         if contains_phrase(text, p)]
            hits = money_amounts(claim.get("claim_text", "")) & \
                money_amounts(text)
            phrase_hit = contains_phrase(text, claim.get("claim_text", ""))
            # A prohibited money figure is only a problem if no approved claim
            # legitimately uses the same number. $25,000 approved and $20,000
            # prohibited share no digits, but this guard keeps the check honest
            # if a future pair overlaps.
            if hits:
                hits -= {a for w in approved_wordings for a in money_amounts(w)}
            if not (triggered or hits or phrase_hit):
                continue

            ev = (triggered[0] if triggered
                  else f"${min(hits):,.0f}" if hits
                  else claim.get("claim_text", "")[:60])
            if status == "prohibited":
                findings.append(Finding(
                    check="prohibited_wording", severity="blocker",
                    field_name=name,
                    message="Contains a prohibited claim.",
                    evidence=ev,
                    remedy=claim.get("restriction_notes"),
                ))
            else:
                # A restricted claim is not an error on sight: it is usable
                # when its condition is met, and only a human can confirm
                # that. A warning puts the condition in front of the reviewer,
                # which is the entire purpose of the status.
                findings.append(Finding(
                    check="restricted_claim_used", severity="warning",
                    field_name=name,
                    message="Uses a restricted claim; its condition must be "
                            "met in full before this ships.",
                    evidence=ev,
                    remedy=claim.get("restriction_notes"),
                ))

        for stale in ctx.stale_values:
            stale_money = money_amounts(stale)
            if stale_money:
                overlap = stale_money & money_amounts(text)
                overlap -= {a for w in approved_wordings
                            for a in money_amounts(w)}
                if overlap:
                    findings.append(Finding(
                        check="stale_figure", severity="blocker",
                        field_name=name,
                        message="Contains a figure recorded as stale in "
                                "kb.conflicts.",
                        evidence=f"${min(overlap):,.0f}",
                        remedy="Use the resolved value from the conflict "
                               "record.",
                    ))
            elif contains_phrase(text, stale):
                findings.append(Finding(
                    check="stale_figure", severity="blocker",
                    field_name=name,
                    message="Contains a value recorded as stale in "
                            "kb.conflicts.",
                    evidence=stale,
                ))
    return _dedupe(findings)


# --------------------------------------------------------------------------
# 2. Price consistency
# --------------------------------------------------------------------------

def check_price_consistency(contents: list[dict],
                            ctx: CheckContext) -> list[Finding]:
    """Every price across a set of assets must agree, and match the approved
    pricing claim.

    Takes a LIST because inconsistency is only visible across assets: two ads
    each stating a defensible price, differing from each other, is the failure
    this catches and no single-asset check can.
    """
    findings: list[Finding] = []
    approved_prices: set[float] = set()
    for c in ctx.claims:
        if c.get("status") == "approved" and c.get("category") == "pricing":
            approved_prices |= money_amounts(
                c.get("approved_wording") or c.get("claim_text") or "")

    seen: set[float] = set()
    for content in contents:
        for _, text in fields_of(content):
            seen |= money_amounts(text)

    if approved_prices:
        unknown = seen - approved_prices
        if unknown:
            findings.append(Finding(
                check="price_consistency", severity="blocker",
                message="A price appears that no approved pricing claim "
                        "supports.",
                evidence=", ".join(f"${a:,.0f}" for a in sorted(unknown)),
                remedy="Approved: " + ", ".join(
                    f"${a:,.0f}" for a in sorted(approved_prices)),
            ))
    elif seen:
        findings.append(Finding(
            check="price_consistency", severity="blocker",
            message="Copy states a price but no approved pricing claim "
                    "exists for this product.",
            evidence=", ".join(f"${a:,.0f}" for a in sorted(seen)),
        ))
    return findings


# --------------------------------------------------------------------------
# 3. CTA consistency
# --------------------------------------------------------------------------

#: Words that carry the CTA's intent. Matching the brief's CTA verbatim would
#: fail every well-written ad -- "Talk to a Franchise Success Specialist" does
#: not fit in a 30-character description -- so the check is that the asset
#: shares the CTA's ACTION, not its wording.
_STOPWORDS = frozenset({
    "a", "an", "the", "to", "with", "for", "your", "our", "and", "or", "of",
    "in", "on", "at", "get", "us", "me", "my",
})


def cta_tokens(cta: str) -> frozenset[str]:
    return frozenset(w for w in re.findall(r"[a-z]+", normalise(cta).lower())
                     if w not in _STOPWORDS and len(w) > 2)


def check_cta_consistency(content: dict, ctx: CheckContext) -> list[Finding]:
    """The asset must gesture at the campaign's CTA, not a different action."""
    primary = ctx.campaign.get("primary_cta") or ""
    if not primary:
        return []
    wanted = cta_tokens(primary)
    secondary = cta_tokens(ctx.campaign.get("secondary_cta") or "")
    if not wanted:
        return []

    body = normalise(" ".join(t for _, t in fields_of(content))).lower()
    present = frozenset(re.findall(r"[a-z]+", body))
    if wanted & present or (secondary and secondary & present):
        return []
    return [Finding(
        check="cta_consistency", severity="warning",
        message="No wording in this asset echoes the campaign CTA.",
        evidence=f"expected something like: {primary}",
        remedy="Align the closing line with the campaign's primary or "
               "secondary CTA.",
    )]


# --------------------------------------------------------------------------
# 4. Unavailable features
# --------------------------------------------------------------------------

def check_unavailable_features(content: dict,
                               ctx: CheckContext) -> list[Finding]:
    """Never promote a feature that is unavailable or unapproved.

    Two different failures, two different severities: promoting something the
    product does not have is a blocker; promoting something real but not
    cleared for marketing is a warning, because the fix may be to approve the
    feature rather than to change the copy.
    """
    findings: list[Finding] = []
    for name, text in fields_of(content):
        for f in ctx.features:
            label = f.get("name") or ""
            if not label or not contains_phrase(text, label):
                continue
            if not f.get("available", True):
                findings.append(Finding(
                    check="unavailable_feature", severity="blocker",
                    field_name=name,
                    message=f"Promotes {label!r}, which is not available.",
                    evidence=label,
                    remedy=f.get("restrictions"),
                ))
            elif not f.get("approved_for_marketing", False):
                findings.append(Finding(
                    check="unapproved_feature", severity="warning",
                    field_name=name,
                    message=f"Promotes {label!r}, which is available but not "
                            f"approved for marketing.",
                    evidence=label,
                    remedy=f.get("marketing_notes"),
                ))
    return _dedupe(findings)


# --------------------------------------------------------------------------
# 5. Campaign-type mixing
# --------------------------------------------------------------------------

def check_campaign_type_mixing(content: dict,
                               ctx: CheckContext) -> list[Finding]:
    """A franchise ad must not sell an exit, and vice versa.

    prohibited_themes on the campaign type is the source of truth. It is a
    blocker: an asset addressing two audiences is unusable rather than
    improvable.
    """
    findings: list[Finding] = []
    for name, text in fields_of(content):
        for theme in ctx.prohibited_themes:
            if contains_phrase(text, theme):
                findings.append(Finding(
                    check="campaign_type_mixing", severity="blocker",
                    field_name=name,
                    message="Contains a theme prohibited for this campaign "
                            "type.",
                    evidence=theme,
                    remedy="One asset addresses one audience. Remove the "
                           "theme or move this asset to the right campaign "
                           "type.",
                ))
    return _dedupe(findings)


# --------------------------------------------------------------------------
# 6. Missing brief fields
# --------------------------------------------------------------------------

#: Required for any campaign. The database already enforces NOT NULL on most;
#: these are the ones that can be present-but-empty, plus the array fields
#: NOT NULL cannot police.
_REQUIRED_BRIEF = (
    "name", "objective", "target_audience", "customer_problem",
    "primary_benefit", "primary_cta", "primary_kpi",
)

#: Channel-specific brief requirements. Generating email copy without knowing
#: the offer produces a nurture sequence with nothing to nurture toward.
_REQUIRED_BY_CHANNEL = {
    "email": ("offer",),
    "meta_ads": ("offer",),
}


def check_missing_brief_fields(ctx: CheckContext) -> list[Finding]:
    findings: list[Finding] = []
    campaign = ctx.campaign
    for key in _REQUIRED_BRIEF:
        value = campaign.get(key)
        empty = not value.strip() if isinstance(value, str) else not value
        if empty:
            findings.append(Finding(
                check="missing_brief_field", severity="blocker",
                field_name=key,
                message=f"Brief field {key!r} is required and is empty.",
            ))

    channels = campaign.get("channels") or []
    if not channels:
        findings.append(Finding(
            check="missing_brief_field", severity="blocker",
            field_name="channels",
            message="Brief names no channels, so no asset type is defined.",
        ))
    for channel in channels:
        for key in _REQUIRED_BY_CHANNEL.get(channel, ()):
            if not campaign.get(key):
                findings.append(Finding(
                    check="missing_brief_field", severity="blocker",
                    field_name=key,
                    message=f"Channel {channel!r} requires brief field "
                            f"{key!r}, which is empty.",
                ))

    if not campaign.get("proof_points"):
        findings.append(Finding(
            check="missing_brief_field", severity="warning",
            field_name="proof_points",
            message="Brief lists no proof points, so copy has nothing "
                    "governed to lean on.",
        ))

    # 025. A WARNING, not a blocker, for two reasons: campaigns filed before
    # the column existed must not fail a re-validation retroactively, and a
    # pure awareness campaign may genuinely have nowhere to send anyone. The
    # cost of ignoring it is stated plainly -- unmeasurable is the word.
    destination = (campaign.get("destination_url") or "").strip()
    if channels and not destination:
        findings.append(Finding(
            check="missing_brief_field", severity="warning",
            field_name="destination_url",
            message="Brief has no destination URL, so approved assets will "
                    "carry no tracked link and the campaign ships "
                    "unmeasurable.",
            remedy="Set destination_url on the brief before approving "
                   "assets; the link is stamped at approval.",
        ))
    elif "utm_" in destination:
        # Pre-tagged destinations happen when someone pastes a link out of an
        # old campaign. Ours replace theirs at approval -- saying so now beats
        # a report next quarter with half the traffic under a ghost campaign.
        findings.append(Finding(
            check="pretagged_destination", severity="warning",
            field_name="destination_url",
            message="The destination URL already carries utm parameters; "
                    "they will be replaced per asset at approval.",
            evidence=destination,
            remedy="Use the bare landing URL as the destination.",
        ))

    # Governance emptiness is a finding, not silence. A brief that validates
    # cleanly against zero approved claims has not really been checked.
    if not any(c.get("status") == "approved" for c in ctx.claims):
        findings.append(Finding(
            check="no_approved_claims", severity="blocker",
            message="No approved claims exist for this campaign's product, so "
                    "no figure can be asserted in any asset.",
            remedy="Approve claims for the product, or set "
                   "products.approved_for_marketing if it is already cleared.",
        ))
    return findings


# --------------------------------------------------------------------------
# 7. Character limits
# --------------------------------------------------------------------------

#: Per channel and asset type, per field. Display limits rather than API
#: maxima: platforms truncate around these, and truncated copy is copy nobody
#: approved. Shared with the generators, which aim at the same numbers --
#: this module is the source of truth and generators import from here.
CHANNEL_LIMITS: dict[tuple[str, str], dict[str, int]] = {
    ("meta_ads", "meta_ad"): {"primary_text": 125, "headline": 40,
                              "description": 30},
    ("email", "email"): {"subject": 60, "preheader": 90, "body": 1800},
}


def check_character_limits(content: dict, channel: str,
                           asset_type: str) -> list[Finding]:
    limits = CHANNEL_LIMITS.get((channel, asset_type))
    if not limits:
        return [Finding(
            check="character_limits", severity="info",
            message=f"No character limits defined for "
                    f"{channel}/{asset_type}; field lengths unchecked.",
        )]
    findings = []
    for key, limit in limits.items():
        val = content.get(key)
        if not isinstance(val, str):
            continue
        if len(val) > limit:
            findings.append(Finding(
                check="character_limits", severity="warning",
                field_name=key,
                message=f"{key} is {len(val)} characters; the display limit "
                        f"is {limit}.",
                evidence=val[limit:limit + 40],
                remedy="Tighten the phrasing rather than truncating.",
            ))
    return findings


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

def _dedupe(findings: list[Finding]) -> list[Finding]:
    """Same check, field and evidence reported once.

    A prohibited figure appearing twice in one field is one problem to fix.
    """
    seen: set[tuple] = set()
    out = []
    for f in findings:
        key = (f.check, f.field_name, f.evidence)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


_RANK = {"blocker": 0, "warning": 1, "info": 2}


def status_for(findings: list[Finding]) -> str:
    """One status from a finding set, matching the CHECK vocabularies in 013
    and campaign_validations: blocked > warning > pass."""
    if any(f.severity == "blocker" for f in findings):
        return "blocked"
    if any(f.severity == "warning" for f in findings):
        return "warning"
    return "pass"


def split(findings: list[Finding]) -> dict[str, list[dict]]:
    """Findings bucketed for the blockers/warnings/recommendations columns,
    severest first within each bucket."""
    ordered = sorted(findings, key=lambda f: (_RANK[f.severity], f.check))
    return {
        "blockers": [f.as_dict() for f in ordered if f.severity == "blocker"],
        "warnings": [f.as_dict() for f in ordered if f.severity == "warning"],
        "recommendations": [f.as_dict() for f in ordered
                            if f.severity == "info"],
    }


def run_asset_checks(content: dict, channel: str, asset_type: str,
                     ctx: CheckContext) -> list[Finding]:
    """Every per-asset check. Price consistency is excluded deliberately --
    it is a cross-asset check and lives in the runner."""
    return [
        # First, because an operator instruction outranks an inferred
        # one: if a line breaks both an exclusion and something
        # subtler, the exclusion is the finding the reviewer acts on.
        *check_excluded_wording(content, ctx),
        *check_prohibited_wording(content, ctx),
        *check_campaign_type_mixing(content, ctx),
        *check_unavailable_features(content, ctx),
        *check_cta_consistency(content, ctx),
        *check_character_limits(content, channel, asset_type),
        # Last: it is the only check here that reports on something
        # the copy got RIGHT but may have got right about a stale
        # fact, so it belongs below the findings about the copy.
        *check_claim_freshness(content, ctx),
    ]
