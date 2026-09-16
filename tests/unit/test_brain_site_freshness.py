"""Site-vs-files staleness (C12, gap 2): does assafweb.com outrun the knowledge files?

Pure-function coverage only -- no network. `FakeSiteFreshnessChecker` stands in for
the real `HEAD` request; `app/workers/ingest_knowledge.py` wires the real one in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.brain.site_freshness import (
    FakeSiteFreshnessChecker,
    check_site_freshness,
    compare_staleness,
    parse_http_date,
)

_OLD = "Mon, 14 Sep 2026 19:14:00 GMT"
_NEW = "Wed, 16 Sep 2026 09:00:00 GMT"
# Only a few minutes ahead -- well under MATERIAL_STALENESS -- so this must read
# "fresh", not "stale": ordinary clock/CDN skew is not a real staleness signal.
_NEW_BUT_CLOSE = "Mon, 14 Sep 2026 19:20:00 GMT"


def test_parse_http_date_rejects_garbage_and_naive_strings() -> None:
    assert parse_http_date(None) is None
    assert parse_http_date("") is None
    assert parse_http_date("not a date") is None
    # No timezone/offset at all -> unanchored, treated as unusable rather than
    # guessed at.
    assert parse_http_date("2026-09-16 09:00:00") is None


def test_parse_http_date_accepts_rfc1123() -> None:
    parsed = parse_http_date(_NEW)
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.year == 2026 and parsed.month == 9 and parsed.day == 16


def test_compare_staleness_flags_when_site_materially_newer() -> None:
    assert (
        compare_staleness(site_last_modified=_NEW, source_last_modified=_OLD)
        == "stale"
    )


def test_compare_staleness_quiet_when_gap_is_not_material() -> None:
    assert (
        compare_staleness(site_last_modified=_NEW_BUT_CLOSE, source_last_modified=_OLD)
        == "fresh"
    )


def test_compare_staleness_quiet_when_site_is_older_or_equal() -> None:
    assert (
        compare_staleness(site_last_modified=_OLD, source_last_modified=_NEW) == "fresh"
    )
    assert compare_staleness(site_last_modified=_OLD, source_last_modified=_OLD) == "fresh"


def test_compare_staleness_unknown_when_site_header_missing() -> None:
    assert compare_staleness(site_last_modified=None, source_last_modified=_OLD) == "unknown"


def test_compare_staleness_unknown_when_source_header_missing() -> None:
    assert compare_staleness(site_last_modified=_NEW, source_last_modified=None) == "unknown"


def test_compare_staleness_unknown_when_both_missing() -> None:
    assert compare_staleness(site_last_modified=None, source_last_modified=None) == "unknown"


def test_compare_staleness_unknown_never_fresh_on_unparsable_header() -> None:
    """A malformed header must never be silently read as fresh."""
    assert (
        compare_staleness(site_last_modified="garbage", source_last_modified=_OLD)
        == "unknown"
    )
    assert (
        compare_staleness(site_last_modified=_NEW, source_last_modified="garbage")
        == "unknown"
    )


def test_check_site_freshness_flags_the_stale_source_only() -> None:
    checker = FakeSiteFreshnessChecker(
        {
            "https://www.assafweb.com": _NEW,
            "https://www.assafweb.com/llms.txt": _OLD,
            # A few hours behind the site root -- inside MATERIAL_STALENESS, so
            # this source must read "fresh", unlike llms.txt above.
            "https://www.assafweb.com/pricing.md": "Wed, 16 Sep 2026 05:00:00 GMT",
        }
    )
    results = check_site_freshness(
        website_url="https://www.assafweb.com",
        sources=[
            ("llms.txt", "https://www.assafweb.com/llms.txt"),
            ("pricing.md", "https://www.assafweb.com/pricing.md"),
        ],
        checker=checker,
    )
    by_id = {item.source_id: item for item in results}
    assert by_id["llms.txt"].stale == "stale"
    assert by_id["llms.txt"].site_last_modified == _NEW
    assert by_id["llms.txt"].source_last_modified == _OLD
    assert by_id["pricing.md"].stale == "fresh"
    # The site root is HEAD-ed once and reused for every source's comparison, not
    # once per source.
    assert checker.requested.count("https://www.assafweb.com") == 1


def test_check_site_freshness_unknown_when_head_fails_for_a_source() -> None:
    """A fake standing in for a failed request: the url is simply absent -> None."""
    checker = FakeSiteFreshnessChecker({"https://www.assafweb.com": _NEW})
    results = check_site_freshness(
        website_url="https://www.assafweb.com",
        sources=[("llms-full.txt", "https://www.assafweb.com/llms-full.txt")],
        checker=checker,
    )
    assert len(results) == 1
    assert results[0].stale == "unknown"
    assert results[0].source_last_modified == ""


def test_check_site_freshness_unknown_when_site_root_head_fails() -> None:
    checker = FakeSiteFreshnessChecker(
        {"https://www.assafweb.com/llms.txt": _OLD}
    )
    results = check_site_freshness(
        website_url="https://www.assafweb.com",
        sources=[("llms.txt", "https://www.assafweb.com/llms.txt")],
        checker=checker,
    )
    assert results[0].stale == "unknown"
    assert results[0].site_last_modified == ""


def test_material_staleness_window_is_hours_not_minutes() -> None:
    from app.brain.site_freshness import MATERIAL_STALENESS

    assert MATERIAL_STALENESS >= timedelta(hours=1)
    # Sanity: a boundary just past the window is "stale", just under it is "fresh".
    base = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    just_under = base + MATERIAL_STALENESS - timedelta(minutes=1)
    just_over = base + MATERIAL_STALENESS + timedelta(minutes=1)
    fmt = "%a, %d %b %Y %H:%M:%S GMT"
    assert (
        compare_staleness(
            site_last_modified=just_under.strftime(fmt),
            source_last_modified=base.strftime(fmt),
        )
        == "fresh"
    )
    assert (
        compare_staleness(
            site_last_modified=just_over.strftime(fmt),
            source_last_modified=base.strftime(fmt),
        )
        == "stale"
    )
