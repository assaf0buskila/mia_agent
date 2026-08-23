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
        reports = run_ingest(
            BrainStore(session),
            website_url=settings.website_url,
            sources=sources,
            fetcher=HttpDocumentFetcher(),
            embedding_port=embedding_port,
            force=args.force,
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
