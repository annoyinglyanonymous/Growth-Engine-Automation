"""check_claim_freshness: approved claims quoted in copy that nobody has
looked at lately.

THE FAILURE IT CATCHES IS SILENT
An approved claim is asserted as fact for ever and nothing notices when the
world moves. There are seven exact prices in public.claims with no expiry on
any of them; the first anyone would learn one had changed is a customer
reading it.

A WARNING, not an effective_until date. A true claim must not stop being
usable because a date somebody invented passed -- that disables correct copy
on a schedule nobody chose. What a reviewer needs is to be told which figures
are old while looking at the asset that quotes them.

The clock is injected, so these tests pin behaviour at fixed dates rather
than being time bombs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from validation.checks import CheckContext, check_claim_freshness

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


def claim(**kw) -> dict:
    base = {
        "status": "approved",
        "claim_text": "The Premium plan is $39.99 per month.",
        "approved_wording": None,
        "category": "pricing",
        "last_reviewed_at": NOW - timedelta(days=10),
    }
    base.update(kw)
    return base


def ctx(*claims, days=180, by_category=None, now=NOW) -> CheckContext:
    return CheckContext(
        claims=list(claims), now=now, review_days=days,
        review_days_by_category=by_category or {"pricing": 90})


def run(content, context):
    return check_claim_freshness(content, context)


# --------------------------------------------------------------------------
# Only claims the copy actually uses
# --------------------------------------------------------------------------

def test_a_claim_the_copy_does_not_quote_is_not_reported():
    """Warning about a claim this asset does not use is noise, and noise in
    warnings teaches people to skim them."""
    out = run({"headline": "Talk to a specialist today."},
              ctx(claim(last_reviewed_at=NOW - timedelta(days=900))))
    assert out == []


def test_a_price_is_matched_by_its_figure_not_its_sentence():
    """The case that matters most. Copy says "$39.99/mo", never the whole
    claim sentence, so phrase matching alone would miss every price."""
    out = run({"headline": "Just $39.99/mo"},
              ctx(claim(last_reviewed_at=NOW - timedelta(days=200))))
    assert len(out) == 1
    assert out[0].severity == "warning"


def test_a_claim_quoted_verbatim_is_matched():
    out = run({"body": "Renegade operates 9 retail agency locations."},
              ctx(claim(claim_text="Renegade operates 9 retail agency "
                                   "locations.",
                        category="company",
                        last_reviewed_at=NOW - timedelta(days=400))))
    assert len(out) == 1


def test_the_approved_wording_is_what_gets_matched_when_present():
    """claim_text is the internally precise version; approved_wording is what
    reaches a customer. The copy will contain the second."""
    out = run({"body": "9 retail agency locations"},
              ctx(claim(claim_text="9 open retail agency locations "
                                   "excluding two closed in Florida",
                        approved_wording="9 retail agency locations",
                        category="company",
                        last_reviewed_at=NOW - timedelta(days=400))))
    assert len(out) == 1


def test_empty_copy_reports_nothing():
    out = run({}, ctx(claim(last_reviewed_at=None)))
    assert out == []


# --------------------------------------------------------------------------
# Only approved claims
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["prohibited", "restricted",
                                    "pending_review"])
def test_only_approved_claims_are_checked(status):
    """A prohibited claim's age is irrelevant -- it is not being asserted.
    check_prohibited_wording is what has an opinion about those."""
    out = run({"h": "Just $39.99/mo"},
              ctx(claim(status=status,
                        last_reviewed_at=NOW - timedelta(days=900))))
    assert out == []


# --------------------------------------------------------------------------
# The horizons
# --------------------------------------------------------------------------

def test_a_recently_reviewed_claim_passes():
    out = run({"h": "Just $39.99/mo"},
              ctx(claim(last_reviewed_at=NOW - timedelta(days=10))))
    assert out == []


def test_pricing_uses_the_shorter_horizon():
    """A price is the claim most likely to change without anyone updating
    marketing, and the most damaging in front of a customer."""
    hundred = claim(last_reviewed_at=NOW - timedelta(days=100))
    assert run({"h": "$39.99/mo"}, ctx(hundred, days=180))
    # The same age under the general horizon would have been fine.
    company = claim(category="company",
                    last_reviewed_at=NOW - timedelta(days=100))
    assert run({"h": "$39.99/mo"}, ctx(company, days=180)) == []


def test_a_category_with_no_override_uses_the_general_horizon():
    out = run({"h": "$39.99/mo"},
              ctx(claim(category="technology",
                        last_reviewed_at=NOW - timedelta(days=200)),
                  days=180))
    assert len(out) == 1


def test_the_boundary_is_exclusive():
    """Exactly at the limit is not yet overdue, so a claim reviewed every
    90 days does not oscillate."""
    at = claim(last_reviewed_at=NOW - timedelta(days=90))
    assert run({"h": "$39.99/mo"}, ctx(at)) == []
    past = claim(last_reviewed_at=NOW - timedelta(days=91))
    assert run({"h": "$39.99/mo"}, ctx(past))


def test_the_message_states_the_age_and_the_limit():
    """"Old" is not actionable. A reviewer needs to know how old and against
    what, to judge whether to bother."""
    out = run({"h": "$39.99/mo"},
              ctx(claim(last_reviewed_at=NOW - timedelta(days=200))))
    assert "200 days" in out[0].message
    assert "90-day" in out[0].message
    assert "pricing" in out[0].message


# --------------------------------------------------------------------------
# Never reviewed
# --------------------------------------------------------------------------

def test_a_claim_nobody_has_confirmed_is_reported():
    """The live state after 023: it cleared the seed-written review stamps,
    so every approved claim is currently unconfirmed. Saying so is the point
    -- the alternative is treating "no date" as "recently checked"."""
    out = run({"h": "$39.99/mo"}, ctx(claim(last_reviewed_at=None)))
    assert len(out) == 1
    assert "nobody has confirmed" in out[0].message


def test_the_unconfirmed_remedy_points_at_the_worksheet():
    out = run({"h": "$39.99/mo"}, ctx(claim(last_reviewed_at=None)))
    assert "claim_review" in out[0].remedy


def test_a_missing_review_date_is_not_treated_as_fresh():
    """Defaulting the other way would make the check silent exactly where
    there is least evidence."""
    assert run({"h": "$39.99/mo"}, ctx(claim(last_reviewed_at=None)))


# --------------------------------------------------------------------------
# It cannot fail
# --------------------------------------------------------------------------

def test_no_clock_reports_an_info_rather_than_passing_silently():
    """A runner that forgets to supply `now` must not look like a clean pass.
    Same principle as check_character_limits with no limits defined."""
    out = check_claim_freshness(
        {"h": "$39.99/mo"},
        CheckContext(claims=[claim(last_reviewed_at=None)]))
    assert len(out) == 1
    assert out[0].severity == "info"
    assert "not checked" in out[0].message


def test_a_naive_datetime_does_not_raise():
    """last_reviewed_at is timestamptz so psycopg returns an aware value, but
    a check whose contract is that it cannot fail must survive a naive one
    arriving by some other route."""
    out = run({"h": "$39.99/mo"},
              ctx(claim(last_reviewed_at=datetime(2020, 1, 1))))
    assert len(out) == 1


def test_a_nonsense_review_value_is_skipped_not_raised():
    out = run({"h": "$39.99/mo"}, ctx(claim(last_reviewed_at="yesterday")))
    assert out == []


def test_a_claim_with_no_text_at_all_is_skipped():
    out = run({"h": "$39.99/mo"},
              ctx(claim(claim_text=None, approved_wording=None,
                        last_reviewed_at=None)))
    assert out == []


# --------------------------------------------------------------------------
# One finding per claim
# --------------------------------------------------------------------------

def test_a_claim_mentioned_in_two_fields_is_reported_once():
    """One stale price is one thing to check, however many times the asset
    mentions it."""
    out = run({"headline": "$39.99/mo", "body": "only $39.99 per month"},
              ctx(claim(last_reviewed_at=None)))
    assert len(out) == 1


def test_two_stale_claims_are_both_reported():
    out = run({"h": "$39.99/mo and $99.99/mo"},
              ctx(claim(claim_text="Premium is $39.99 per month.",
                        last_reviewed_at=None),
                  claim(claim_text="Enterprise is $99.99 per month.",
                        last_reviewed_at=None)))
    assert len(out) == 2


def test_every_finding_carries_a_remedy_and_evidence():
    out = run({"h": "$39.99/mo"},
              ctx(claim(last_reviewed_at=None),
                  claim(claim_text="Enterprise is $99.99 per month.",
                        last_reviewed_at=NOW - timedelta(days=400))))
    for f in out:
        assert f.remedy, f.message
        assert f.evidence, f.message
