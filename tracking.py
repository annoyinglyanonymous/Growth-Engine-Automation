"""Tagged destination URLs: one pure function, stdlib only.

    tracked_url("https://renegadeinsurance.com/franchise",
                campaign_name="Franchise Recruitment - Q4 2026",
                channel="email", variant="A", position=2, version=5)
    -> https://renegadeinsurance.com/franchise?utm_source=email&utm_medium=email
       &utm_campaign=franchise-recruitment-q4-2026&utm_content=a-2-v5

WHY THIS EXISTS
The brief requires a primary_kpi, validation blocks without one, and nothing
in the system ever measures it. Measurement starts with each published asset
pointing at a URL that names the asset. This module is the naming; the
performance import that reads the names back is a later phase.

PURE ON PURPOSE
No database, no settings, no clock. generators.pipeline stamps the result onto
the asset row at approval, and anything that wants to PREVIEW a link calls the
same function -- one derivation, so the stamp and the preview cannot disagree.
Same discipline as lifecycle.approval_readiness and bulk_skip_reason.

Determinism is the actual contract: the same asset must produce the same URL
on every call, forever, because the value shipped to an ad platform and the
value in the database have to be the same string.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: channel -> (utm_source, utm_medium). One place to change conventions.
#:
#: 'facebook' and not 'meta': GA4's default channel grouping classifies
#: paid-social by recognising the SOURCE against its list of social sites,
#: and 'facebook' is on that list while 'meta' is not. Sentiment loses to
#: the classifier that every report downstream depends on.
#:
#: google_ads is deliberately absent until that channel is built: its
#: utm_term/keyword conventions are a paid-search question this map should
#: not guess at in advance.
CHANNEL_UTM: dict[str, tuple[str, str]] = {
    "email": ("email", "email"),
    "meta_ads": ("facebook", "paid_social"),
}

#: The parameters this module owns. Any of these already present on the
#: destination are replaced, not duplicated -- two utm_source values in one
#: URL is undefined behaviour in every analytics tool.
_UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content",
             "utm_term")


def campaign_slug(name: str) -> str:
    """A campaign name as a utm_campaign value: lowercase ASCII and hyphens.

    Deterministic and lossy, and the loss is fine -- the slug's job is to be
    stable, greppable in an analytics report, and safe in a query string, not
    to round-trip the name. Accents fold to ASCII ('Sao Paulo' from
    'São Paulo'), everything non-alphanumeric collapses to single hyphens.
    """
    folded = (unicodedata.normalize("NFKD", name or "")
              .encode("ascii", "ignore").decode("ascii"))
    return re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")


def slot_content(variant: str, position: int | None, version: int) -> str:
    """utm_content for one asset: variant, sequence position, version.

    'a-2-v5' is email variant A, sequence step 2, version 5; 'b-v4' is a
    standalone variant B at version 4. The version is included because that is
    the whole point of stamping at approval -- when v7 supersedes v5 next
    quarter, the two runs stay distinguishable in the report.

    `position is not None`, not `if position`: 0 is a real position and must
    not vanish (same trap slot_label already documents in lifecycle.py).
    """
    parts = [campaign_slug(variant) or "x"]
    if position is not None:
        parts.append(str(position))
    parts.append(f"v{version}")
    return "-".join(parts)


def tracked_url(base: str, *, campaign_name: str, channel: str,
                variant: str, position: int | None, version: int) -> str:
    """The destination with this asset's utm parameters appended.

    Existing NON-utm query parameters and the fragment survive -- a
    destination like ?ref=partner#pricing is someone's deliberate URL and this
    function has no business dropping parts of it. Existing utm_* parameters
    are replaced by ours: the brief-level validation already warns when a
    destination arrives pre-tagged, and at stamping time the per-asset values
    are the correct ones by definition.

    Raises ValueError rather than guessing: base and channel come from
    governed data (a CHECK-constrained column and the channel vocabulary), so
    a bad value here is a caller bug, not user input to be tolerated.
    """
    parts = urlsplit(base or "")
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"not an http(s) URL: {base!r}")
    try:
        source, medium = CHANNEL_UTM[channel]
    except KeyError:
        raise ValueError(
            f"no utm convention for channel {channel!r} "
            f"({', '.join(sorted(CHANNEL_UTM))} have one)") from None

    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k not in _UTM_KEYS]
    query += [
        ("utm_source", source),
        ("utm_medium", medium),
        ("utm_campaign", campaign_slug(campaign_name)),
        ("utm_content", slot_content(variant, position, version)),
    ]
    return urlunsplit(parts._replace(query=urlencode(query)))
