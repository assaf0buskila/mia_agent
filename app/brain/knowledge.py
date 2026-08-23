"""Website and business knowledge ingestion.

`www.assafweb.com` publishes an agent-oriented corpus — `llms.txt`, `llms-full.txt`,
`pricing.md` — which is clean structured markdown the owner already maintains. That is the
primary source: no crawler, no scraping noise, no Firecrawl credits, and it is
deterministic enough to diff on a content hash. Firecrawl stays available as the fallback
for pages the corpus does not cover.

Content is split on markdown headings so each chunk is a real topic rather than a fixed
character window, then classified into the knowledge taxonomy by heading. Re-ingest is
idempotent: unchanged sources are skipped on hash, changed ones retire their old chunks.

Fetched page text is untrusted data. It is stored and retrieved as knowledge; it never
becomes an instruction to the model.
"""

from __future__ import annotations

import re
from typing import NamedTuple, Protocol

import httpx

from app.brain.embeddings import EmbeddingError, EmbeddingPort
from app.brain.schemas import KnowledgeCategory, KnowledgeChunk
from app.brain.store import BrainStore, chunk_id_for, content_hash

MAX_DOCUMENT_BYTES = 512_000
MAX_CHUNK_CHARS = 1800
MIN_CHUNK_CHARS = 40
_TIMEOUT = 20.0
_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*)$")

# Heading keyword -> category. Hebrew first because the corpus is Hebrew-first.
_CATEGORY_RULES: tuple[tuple[tuple[str, ...], KnowledgeCategory], ...] = (
    (("מי זה", "מי אני", "about", "who is"), KnowledgeCategory.PERSONAL),
    (("שירות", "פתרונות", "service", "solutions", "tracks"), KnowledgeCategory.SERVICE),
    (("תהליך", "איך זה עובד", "process", "how it works"), KnowledgeCategory.PROCESS),
    (("שאלות", "faq", "שאלות נפוצות"), KnowledgeCategory.FAQ),
    (("המלצות", "testimonial", "reviews"), KnowledgeCategory.TESTIMONIAL),
    (("מחיר", "pricing", "price", "scope policy"), KnowledgeCategory.PRICING),
    (("יצירת קשר", "contact", "צור קשר"), KnowledgeCategory.CONTACT),
    (("פרויקט", "project", "portfolio", "עבודות", "proof"), KnowledgeCategory.PORTFOLIO),
    (("מוצר", "product"), KnowledgeCategory.PRODUCT),
    (("ניסיון", "experience", "background"), KnowledgeCategory.EXPERIENCE),
    (("כישור", "skill", "stack", "טכנולוג"), KnowledgeCategory.SKILL),
    (("עסק", "business", "company"), KnowledgeCategory.BUSINESS),
    (("עכשיו", "current", "now", "building"), KnowledgeCategory.CURRENT_WORK),
)


class FetchedDocument(NamedTuple):
    source_id: str
    url: str
    text: str
    kind: str


class IngestReport(NamedTuple):
    source_id: str
    url: str
    status: str
    chunks: int
    error: str = ""


class DocumentFetcher(Protocol):
    def fetch(self, url: str) -> str: ...


class HttpDocumentFetcher:
    """Plain HTTPS GET. Only the configured website origin is ever fetched."""

    def __init__(self, *, client: httpx.Client | None = None) -> None:
        self._client = client

    def fetch(self, url: str) -> str:
        try:
            if self._client is not None:
                response = self._client.get(url)
            else:
                with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
                    response = client.get(url)
        except httpx.HTTPError as exc:
            raise KnowledgeFetchError(f"fetch failed for {url}") from exc
        if response.status_code >= 400:
            raise KnowledgeFetchError(f"fetch failed for {url}: HTTP {response.status_code}")
        content = response.text
        if len(content.encode("utf-8", errors="ignore")) > MAX_DOCUMENT_BYTES:
            raise KnowledgeFetchError(f"document too large: {url}")
        return content


class FakeDocumentFetcher:
    """Test double mapping url -> body."""

    def __init__(self, documents: dict[str, str]) -> None:
        self._documents = dict(documents)
        self.requested: list[str] = []

    def fetch(self, url: str) -> str:
        self.requested.append(url)
        if url not in self._documents:
            raise KnowledgeFetchError(f"no fake document for {url}")
        return self._documents[url]


class KnowledgeFetchError(RuntimeError):
    """Raised when a knowledge source cannot be retrieved."""


def classify_heading(heading: str) -> KnowledgeCategory:
    """Map a markdown heading to the knowledge taxonomy. Unknown headings stay OTHER."""
    folded = heading.strip().lower()
    if not folded:
        return KnowledgeCategory.OTHER
    for needles, category in _CATEGORY_RULES:
        if any(needle in folded for needle in needles):
            return category
    return KnowledgeCategory.OTHER


def split_markdown_sections(text: str) -> list[tuple[str, str, str]]:
    """Split on markdown headings into `(heading, heading_path, body)`.

    Splitting on structure rather than a fixed window keeps each chunk a coherent topic,
    which is what makes a retrieved chunk readable on its own in a prompt.

    `heading_path` carries the ancestor headings, so a `###` question under
    `## שאלות נפוצות` still classifies as FAQ instead of falling through to OTHER.
    """
    sections: list[tuple[str, str, list[str]]] = []
    ancestors: dict[int, str] = {}
    current_heading = ""
    current_path = ""
    current_body: list[str] = []
    for line in text.splitlines():
        match = _HEADING_RE.match(line.strip())
        if match is not None:
            if current_heading or current_body:
                sections.append((current_heading, current_path, current_body))
            level = len(match.group(1))
            current_heading = match.group(2).strip()
            ancestors = {
                depth: value for depth, value in ancestors.items() if depth < level
            }
            ancestors[level] = current_heading
            current_path = " > ".join(
                ancestors[depth] for depth in sorted(ancestors) if ancestors[depth]
            )
            current_body = []
            continue
        current_body.append(line)
    if current_heading or current_body:
        sections.append((current_heading, current_path, current_body))
    result: list[tuple[str, str, str]] = []
    for heading, path, body in sections:
        joined = "\n".join(body).strip()
        if not joined and not heading:
            continue
        result.append((heading, path or heading, joined))
    return result


def _split_long_body(heading: str, body: str) -> list[str]:
    """Break an over-long section on blank lines, never mid-sentence."""
    if len(body) <= MAX_CHUNK_CHARS:
        return [body]
    parts: list[str] = []
    buffer: list[str] = []
    size = 0
    for paragraph in body.split("\n\n"):
        stripped = paragraph.strip()
        if not stripped:
            continue
        if size + len(stripped) + 2 > MAX_CHUNK_CHARS and buffer:
            parts.append("\n\n".join(buffer))
            buffer = []
            size = 0
        buffer.append(stripped)
        size += len(stripped) + 2
    if buffer:
        parts.append("\n\n".join(buffer))
    return parts or [body[:MAX_CHUNK_CHARS]]


def build_chunks(document: FetchedDocument) -> list[KnowledgeChunk]:
    """Turn one fetched document into typed, categorized, retrievable chunks."""
    chunks: list[KnowledgeChunk] = []
    ordinal = 0
    for heading, heading_path, body in split_markdown_sections(document.text):
        # Classify on the deepest heading first, then fall back to its ancestors, so a
        # specific match wins but an unrecognised sub-heading still inherits its section.
        category = classify_heading(heading)
        if category is KnowledgeCategory.OTHER:
            category = classify_heading(heading_path)
        for part in _split_long_body(heading, body):
            text = f"{heading}\n{part}".strip() if heading else part.strip()
            if len(text) < MIN_CHUNK_CHARS:
                continue
            chunks.append(
                KnowledgeChunk(
                    chunk_id=chunk_id_for(document.source_id, ordinal, text),
                    source_id=document.source_id,
                    category=category,
                    title=heading[:255],
                    text=text,
                    url=document.url,
                    ordinal=ordinal,
                    content_hash=content_hash(text),
                )
            )
            ordinal += 1
    return chunks


def source_urls(website_url: str, sources: list[str]) -> list[tuple[str, str]]:
    """`(source_id, url)` for each configured knowledge file."""
    base = website_url.strip().rstrip("/")
    pairs: list[tuple[str, str]] = []
    for name in sources:
        cleaned = name.strip().lstrip("/")
        if not cleaned:
            continue
        pairs.append((cleaned, f"{base}/{cleaned}"))
    return pairs


def ingest_source(
    store: BrainStore,
    *,
    source_id: str,
    url: str,
    fetcher: DocumentFetcher,
    embedding_port: EmbeddingPort,
    force: bool = False,
) -> IngestReport:
    """Fetch, chunk, embed and store one knowledge source.

    Unchanged content is skipped on hash so a re-ingest costs one GET and no embeddings.
    """
    try:
        body = fetcher.fetch(url)
    except KnowledgeFetchError as exc:
        store.upsert_knowledge_source(
            source_id=source_id,
            url=url,
            kind="http",
            source_hash="",
            chunk_count=0,
            error=str(exc)[:255],
        )
        return IngestReport(source_id, url, "error", 0, str(exc)[:255])
    digest = content_hash(body)
    if not force and digest == store.knowledge_source_hash(source_id):
        return IngestReport(source_id, url, "unchanged", 0)
    document = FetchedDocument(source_id=source_id, url=url, text=body, kind="http")
    chunks = build_chunks(document)
    if not chunks:
        store.upsert_knowledge_source(
            source_id=source_id,
            url=url,
            kind="http",
            source_hash=digest,
            chunk_count=0,
            error="no chunks produced",
        )
        return IngestReport(source_id, url, "empty", 0, "no chunks produced")
    embeddings = _embed_chunks(embedding_port, chunks)
    written = store.replace_knowledge_chunks(
        source_id=source_id,
        chunks=list(zip(chunks, embeddings, strict=True)),
        embedding_model=embedding_port.model,
    )
    store.upsert_knowledge_source(
        source_id=source_id,
        url=url,
        kind="http",
        source_hash=digest,
        chunk_count=written,
    )
    return IngestReport(source_id, url, "ingested", written)


def _embed_chunks(
    port: EmbeddingPort, chunks: list[KnowledgeChunk]
) -> list[list[float] | None]:
    """Embed every chunk, or store them unembedded so keyword retrieval still works."""
    if not port.enabled():
        return [None] * len(chunks)
    try:
        vectors = port.embed([chunk.text for chunk in chunks])
    except EmbeddingError:
        return [None] * len(chunks)
    if len(vectors) != len(chunks):
        return [None] * len(chunks)
    return list(vectors)


def ingest_website(
    store: BrainStore,
    *,
    website_url: str,
    sources: list[str],
    fetcher: DocumentFetcher,
    embedding_port: EmbeddingPort,
    force: bool = False,
) -> list[IngestReport]:
    """Ingest every configured source. One failing source never aborts the others."""
    reports: list[IngestReport] = []
    for source_id, url in source_urls(website_url, sources):
        reports.append(
            ingest_source(
                store,
                source_id=source_id,
                url=url,
                fetcher=fetcher,
                embedding_port=embedding_port,
                force=force,
            )
        )
    return reports
