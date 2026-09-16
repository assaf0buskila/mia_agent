"""One-off knowledge ingest: `uv run mia-ingest-knowledge`.

Fetches the configured knowledge files from the website, chunks them by heading, embeds
them and stores them. Idempotent: unchanged sources are skipped on content hash, so
re-running costs one GET per source and no embedding spend.

Run after the site changes, or on a schedule. `--force` re-embeds even when unchanged;
`--dry-run` reports what would happen without writing.
"""

from __future__ import annotations

import argparse
import sys

from app.brain.embeddings import build_embedding_port
from app.brain.knowledge import HttpDocumentFetcher, build_chunks, source_urls
from app.brain.knowledge import ingest_website as run_ingest
from app.brain.site_freshness import HttpSiteFreshnessChecker, check_site_freshness
from app.brain.store import BrainStore
from app.core.config import get_settings
from app.db.session import get_session_factory


def _dry_run(settings) -> int:
    fetcher = HttpDocumentFetcher()
    total = 0
    for source_id, url in source_urls(
        settings.website_url, settings.knowledge_source_list()
    ):
        try:
            body = fetcher.fetch(url)
        except Exception as exc:  # noqa: BLE001 - report and continue
            print(f"{source_id}: FETCH FAILED ({type(exc).__name__})")
            continue
        chunks = build_chunks(
            type("Doc", (), {"source_id": source_id, "url": url, "text": body, "kind": "http"})()
        )
        total += len(chunks)
        print(f"{source_id}: {len(body)} bytes -> {len(chunks)} chunks")
        for chunk in chunks[:3]:
            print(f"    [{chunk.category.value}] {chunk.title[:60]}")
    print(f"total chunks: {total}")
    return 0


def _check_and_record_site_freshness(
    store: BrainStore, *, website_url: str, sources: list[str]
) -> None:
    """Gap 2 (`app/brain/site_freshness.py`): is the site newer than these files?

    Best-effort and side-channel to the actual ingest: a failure here never fails
    the ingest run or loses the chunks it just wrote. Only the verdict (never the
    raw header value) is printed, matching the reason-code-only logging rule.
    """
    try:
        results = check_site_freshness(
            website_url=website_url,
            sources=source_urls(website_url, sources),
            checker=HttpSiteFreshnessChecker(),
        )
        for item in results:
            store.record_site_freshness(
                source_id=item.source_id,
                site_last_modified=item.site_last_modified,
                source_last_modified=item.source_last_modified,
                stale=item.stale,
            )
            print(f"{item.source_id}: site_stale={item.stale}")
    except Exception as exc:  # noqa: BLE001 - never let this abort a successful ingest
        print(f"site freshness check failed: {type(exc).__name__}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest website knowledge into Mia's brain")
    parser.add_argument("--force", action="store_true", help="re-ingest even if unchanged")
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args()

    settings = get_settings()
    sources = settings.knowledge_source_list()
    if not sources:
        print("no knowledge sources configured (MIA_KNOWLEDGE_SOURCES)", file=sys.stderr)
        return 2
    if args.dry_run:
        return _dry_run(settings)

    embedding_port = build_embedding_port(settings)
    if not embedding_port.enabled():
        print(
            "warning: no embedding provider configured; chunks will be stored without "
            "vectors and retrieval will fall back to keyword search",
            file=sys.stderr,
        )
    session = get_session_factory()()
    try:
        store = BrainStore(session)
        reports = run_ingest(
            store,
            website_url=settings.website_url,
            sources=sources,
            fetcher=HttpDocumentFetcher(),
            embedding_port=embedding_port,
            force=args.force,
        )
        _check_and_record_site_freshness(
            store, website_url=settings.website_url, sources=sources
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    failed = 0
    for report in reports:
        print(f"{report.source_id}: {report.status} ({report.chunks} chunks) {report.error}")
        if report.status == "error":
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
