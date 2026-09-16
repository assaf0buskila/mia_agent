"""Site-vs-files staleness (C12, gap 2): does assafweb.com outrun the knowledge files?

Pure-function coverage only -- no network. `FakeSiteFreshnessChecker` stands in for
the real `HEAD` request; `app/workers/ingest_knowledge.py` wires the real one in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.brain.embeddings import FakeEmbeddingPort
from app.brain.knowledge import FakeDocumentFetcher, ingest_source
from app.brain.site_freshness import (
    FakeSiteFreshnessChecker,
    SourceFreshness,
    check_site_freshness,
    compare_staleness,
    parse_http_date,
)
from app.brain.store import BrainStore
from app.db.models import KnowledgeSourceRow
from app.db.session import get_session_factory, init_db
from app.workers import ingest_knowledge as ingest_worker

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


# --- the worker wiring: a freshness failure must not cost the ingest -----------
#
# `_check_and_record_site_freshness` runs INSIDE the ingest's open transaction and
# `main()` commits unconditionally afterwards. Before the savepoint, a DB error here
# left the Session in pending-rollback, the commit raised, and the outer handler
# rolled back every chunk the run had just fetched and embedded.


def _fresh_brain():
    init_db()
    session = get_session_factory()()
    return session, BrainStore(session)


def test_freshness_db_failure_does_not_discard_the_chunks_just_ingested(monkeypatch) -> None:
    """The P1 regression, with a GENUINE DB error rather than a mocked one.

    Forces the `uq_brain_knowledge_source` conflict the reviewer reproduced -- the
    exact shape two overlapping ingest runs produce, which moving to hourly makes
    far more likely -- and asserts the freshly-embedded chunks survive the commit.
    """
    session, brain = _fresh_brain()
    source_id = "p1-regression.txt"
    url = f"https://example.invalid/{source_id}"
    try:
        ingest_source(
            brain,
            source_id=source_id,
            url=url,
            fetcher=FakeDocumentFetcher(
                {url: "# S\n\n## Services\nA description long enough to chunk.\n"}
            ),
            embedding_port=FakeEmbeddingPort(),
        )
        chunks_before = brain.count_knowledge_chunks()
        assert chunks_before > 0, "the test needs real chunks pending in this transaction"

        def _duplicate_source(self, **kwargs) -> None:
            # A real IntegrityError from the database, not a raised stub: a second
            # row for a source_id that already exists violates uq_brain_knowledge_source.
            self.session.add(KnowledgeSourceRow(source_id=source_id))
            self.session.flush()

        monkeypatch.setattr(BrainStore, "record_site_freshness", _duplicate_source)
        monkeypatch.setattr(
            ingest_worker,
            "check_site_freshness",
            lambda **kwargs: [
                SourceFreshness(
                    source_id=source_id,
                    site_last_modified=_NEW,
                    source_last_modified=_OLD,
                    stale="stale",
                )
            ],
        )
        monkeypatch.setattr(ingest_worker, "HttpSiteFreshnessChecker", lambda *a, **k: None)

        # Must not raise, and must leave the outer transaction committable.
        ingest_worker._check_and_record_site_freshness(
            brain, website_url="https://example.invalid", sources=[source_id]
        )
        session.commit()
    finally:
        session.close()

    verify = get_session_factory()()
    try:
        assert BrainStore(verify).count_knowledge_chunks() >= chunks_before
    finally:
        verify.close()


def test_freshness_db_failure_on_one_source_still_records_the_others(monkeypatch) -> None:
    """The savepoint is per item, so one bad source does not skip the rest."""
    session, brain = _fresh_brain()
    good, bad = "good-source.txt", "bad-source.txt"
    try:
        real = BrainStore.record_site_freshness

        def _fail_one(self, **kwargs):
            if kwargs.get("source_id") == bad:
                self.session.add(KnowledgeSourceRow(source_id=bad))
                self.session.add(KnowledgeSourceRow(source_id=bad))
                self.session.flush()
                return None
            return real(self, **kwargs)

        monkeypatch.setattr(BrainStore, "record_site_freshness", _fail_one)
        monkeypatch.setattr(
            ingest_worker,
            "check_site_freshness",
            lambda **kwargs: [
                SourceFreshness(
                    source_id=bad,
                    site_last_modified=_NEW,
                    source_last_modified=_OLD,
                    stale="stale",
                ),
                SourceFreshness(
                    source_id=good,
                    site_last_modified=_NEW,
                    source_last_modified=_OLD,
                    stale="stale",
                ),
            ],
        )
        monkeypatch.setattr(ingest_worker, "HttpSiteFreshnessChecker", lambda *a, **k: None)

        ingest_worker._check_and_record_site_freshness(
            brain, website_url="https://example.invalid", sources=[bad, good]
        )
        session.commit()
    finally:
        session.close()

    verify = get_session_factory()()
    try:
        statuses = {
            s.source_id: s
            for s in BrainStore(verify).list_knowledge_source_statuses([good, bad])
        }
        assert statuses[good].site_stale == "stale", "the good source must still be recorded"
    finally:
        verify.close()
