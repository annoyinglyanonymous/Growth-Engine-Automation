"""Tagged destination URLs: one pure function, stdlib only.

    tracked_url("https://renegadeinsurance.com/franchise",
                campaign_name="Franchise Recruitment - Q4 2026",
                channel="email", asset_type="email", variant="A",
                position=2, version=5)
    -> https://renegadeinsurance.com/franchise?utm_source=email&utm_medium=email
       &utm_campaign=franchise-recruitment-q4-2026&utm_content=email-a-2-v5

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


#: asset_type -> a short token for utm_content. Short because utm_content is
#: read in a report column, not parsed.
#:
#: This map exists because of a collision, not for tidiness. utm_source and
#: utm_medium come from the CHANNEL, and a UGC video runs on meta_ads exactly
#: like a static ad does -- so before 026 a meta_ad and a video_script at the
#: same variant and version produced BYTE-IDENTICAL tracked URLs, while
#: campaign_assets_one_approved_idx keys on asset_type and happily lets both
#: be approved at once. Two different creatives, one link, one merged row of
#: performance data, and no way to tell afterwards which one earned it.
#:
#: Unknown types fall back to the slugged type name rather than raising. An
#: unknown CHANNEL has no defensible source/medium and _tracked_link declines
#: to invent one; an unknown asset type is different -- source and medium are
#: still correct, only the label is unfamiliar, and refusing to stamp (or
#: worse, blocking approval) over a label would be a bad trade.
_ASSET_TOKEN: dict[str, str] = {
    "email": "email",
    "meta_ad": "ad",
    "video_script": "vid",
    "google_ad": "gad",
    "landing_page_section": "lp",
    "sms": "sms",
}


def slot_content(asset_type: str, variant: str, position: int | None,
                 version: int) -> str:
    """utm_content for one asset: type, variant, sequence position, version.

    'email-a-2-v5' is email variant A, sequence step 2, version 5; 'vid-b-v4'
    is a standalone video script, variant B, version 4. The version is
    included because that is the whole point of stamping at approval -- when
    v7 supersedes v5 next quarter, the two runs stay distinguishable in the
    report. The type is included because without it they are not
    distinguishable from each other at all (see _ASSET_TOKEN).

    `position is not None`, not `if position`: 0 is a real position and must
    not vanish (same trap slot_label already documents in lifecycle.py).
    """
    token = _ASSET_TOKEN.get(asset_type) or campaign_slug(asset_type) or "x"
    parts = [token, campaign_slug(variant) or "x"]
    if position is not None:
        parts.append(str(position))
    parts.append(f"v{version}")
    return "-".join(parts)


def tracked_url(base: str, *, campaign_name: str, channel: str,
                asset_type: str, variant: str, position: int | None,
                version: int) -> str:
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
        ("utm_content",
         slot_content(asset_type, variant, position, version)),
    ]
    return urlunsplit(parts._replace(query=urlencode(query)))
