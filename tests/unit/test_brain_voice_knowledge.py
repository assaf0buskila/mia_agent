"""Voice transcription contracts and website knowledge ingestion."""

from __future__ import annotations

import httpx
import pytest
from app.brain.embeddings import (
    DisabledEmbeddingPort,
    FakeEmbeddingPort,
    OpenAIEmbeddingPort,
    build_embedding_port,
)
from app.brain.knowledge import (
    FakeDocumentFetcher,
    KnowledgeFetchError,
    build_chunks,
    classify_heading,
    ingest_source,
    ingest_website,
    source_urls,
    split_markdown_sections,
)
from app.brain.schemas import KnowledgeCategory
from app.brain.store import BrainStore
from app.core.config import get_settings
from app.db.session import get_session_factory, init_db
from app.integrations.transcribe import (
    OpenAITranscribePort,
    detected_duration_ms,
    detected_language,
    transcription_request_fields,
)

SITE = "https://www.assafweb.com"


def _brain() -> BrainStore:
    init_db()
    return BrainStore(get_session_factory()())


# --------------------------------------------------------------------- voice


def test_gpt_transcribe_never_receives_verbose_json() -> None:
    """`verbose_json` is a whisper-1 format; sending it made every voice note 400."""
    fields = transcription_request_fields(
        model="gpt-transcribe", prompt="p", languages=("he", "en"), keywords=("Mia",)
    )
    assert fields["response_format"] == "json"


def test_gpt_transcribe_uses_plural_languages_and_never_both_forms() -> None:
    """The docs are explicit: for gpt-transcribe, "don't send both fields"."""
    fields = transcription_request_fields(
        model="gpt-transcribe", prompt="p", languages=("he", "en"), keywords=()
    )
    assert fields["languages[]"] == ["he", "en"]
    assert "language" not in fields


def test_whisper_keeps_verbose_json_and_singular_language() -> None:
    fields = transcription_request_fields(
        model="whisper-1", prompt="p", languages=("he",), keywords=("Mia",)
    )
    assert fields["response_format"] == "verbose_json"
    assert fields["language"] == "he"
    assert "languages[]" not in fields
    # whisper-1 does not document a keywords parameter.
    assert "keywords[]" not in fields


def test_gpt_4o_transcribe_uses_json_and_singular_language() -> None:
    fields = transcription_request_fields(
        model="gpt-4o-transcribe", prompt="p", languages=("he",), keywords=("Mia",)
    )
    assert fields["response_format"] == "json"
    assert fields["language"] == "he"
    assert "languages[]" not in fields


def test_keywords_with_forbidden_characters_are_dropped() -> None:
    """The API rejects the entire request on a keyword containing < > CR or LF."""
    port = OpenAITranscribePort(
        api_key="k", model="gpt-transcribe", keywords=("ok", "ba<d", "line\nbreak")
    )
    assert port._keywords == ("ok",)


def test_language_is_read_from_the_languages_array() -> None:
    assert detected_language({"text": "x", "languages": [{"code": "he"}]}) == "he"


def test_empty_languages_array_means_no_detection() -> None:
    assert detected_language({"text": "x", "languages": []}) == ""


def test_verbose_language_name_is_rejected_as_not_a_code() -> None:
    """verbose_json documents a language *name* ("english"), which is not an ISO code."""
    assert detected_language({"text": "x", "language": "english"}) == ""
    assert detected_language({"text": "x", "language": "he"}) == "he"


def test_duration_falls_back_to_usage_seconds() -> None:
    assert detected_duration_ms({"usage": {"type": "duration", "seconds": 27}}) == 27000
    assert detected_duration_ms({"duration": 1.5}) == 1500
    assert detected_duration_ms({}) == 0


@pytest.mark.asyncio
async def test_transcription_sends_explicit_filename_and_content_type() -> None:
    """The spec requires "enough format metadata for the file to be identified"."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode(errors="replace")
        return httpx.Response(200, json={"text": "שלום", "languages": [{"code": "he"}]})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    port = OpenAITranscribePort(api_key="k", model="gpt-transcribe", client=client)
    result = await port.transcribe(audio=b"abc", mime_type="audio/ogg", filename="voice.ogg")
    await client.aclose()

    assert "voice.ogg" in captured["body"]
    assert "audio/ogg" in captured["body"]
    assert result.text == "שלום"
    assert result.language == "he"


# ----------------------------------------------------------------- knowledge


def test_headings_split_into_sections_with_ancestry() -> None:
    sections = split_markdown_sections(
        "# Site\nintro\n\n## שאלות נפוצות\n\n### כמה זה עולה?\ntext here\n"
    )
    headings = [heading for heading, _path, _body in sections]
    assert "שאלות נפוצות" in headings
    paths = {heading: path for heading, path, _body in sections}
    assert "שאלות נפוצות" in paths["כמה זה עולה?"]


def test_subheadings_inherit_their_section_category() -> None:
    """A `###` question under `## שאלות נפוצות` must classify as FAQ, not OTHER."""
    chunks = build_chunks(
        type(
            "Doc",
            (),
            {
                "source_id": "s",
                "url": SITE,
                "kind": "http",
                "text": (
                    "## שאלות נפוצות\n\n### כמה עולה פתרון AI?\n"
                    "תלוי מה בונים ובאיזה היקף בדיוק.\n"
                ),
            },
        )()
    )
    assert any(chunk.category is KnowledgeCategory.FAQ for chunk in chunks)


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("מי זה אסף בוסקילה", KnowledgeCategory.PERSONAL),
        ("השירותים (פתרונות)", KnowledgeCategory.SERVICE),
        ("תהליך העבודה", KnowledgeCategory.PROCESS),
        ("המלצות מלקוחות", KnowledgeCategory.TESTIMONIAL),
        ("יצירת קשר", KnowledgeCategory.CONTACT),
        ("מדיניות מחירים", KnowledgeCategory.PRICING),
        ("Pricing", KnowledgeCategory.PRICING),
        ("Contact", KnowledgeCategory.CONTACT),
        ("Nonsense heading", KnowledgeCategory.OTHER),
    ],
)
def test_headings_classify_into_the_taxonomy(heading: str, expected: KnowledgeCategory) -> None:
    assert classify_heading(heading) is expected


def test_source_urls_are_built_from_the_website_root() -> None:
    assert source_urls(SITE + "/", ["llms.txt", "/pricing.md", ""]) == [
        ("llms.txt", f"{SITE}/llms.txt"),
        ("pricing.md", f"{SITE}/pricing.md"),
    ]


def test_ingest_is_idempotent_on_content_hash() -> None:
    brain = _brain()
    body = "# Site\n\n## Services\nAutomations and AI agents for small businesses.\n"
    fetcher = FakeDocumentFetcher({f"{SITE}/llms.txt": body})
    first = ingest_source(
        brain,
        source_id="llms.txt",
        url=f"{SITE}/llms.txt",
        fetcher=fetcher,
        embedding_port=FakeEmbeddingPort(),
    )
    second = ingest_source(
        brain,
        source_id="llms.txt",
        url=f"{SITE}/llms.txt",
        fetcher=fetcher,
        embedding_port=FakeEmbeddingPort(),
    )
    assert first.status == "ingested"
    assert first.chunks > 0
    assert second.status == "unchanged"
    assert second.chunks == 0


def test_changed_content_retires_old_chunks_and_writes_new() -> None:
    brain = _brain()
    url = f"{SITE}/llms.txt"
    ingest_source(
        brain,
        source_id="changing",
        url=url,
        fetcher=FakeDocumentFetcher(
            {url: "# A\n\n## Services\nOld description of the automation service offered.\n"}
        ),
        embedding_port=FakeEmbeddingPort(),
    )
    before = [chunk.text for chunk in brain.list_knowledge_chunks(source_id="changing")]
    ingest_source(
        brain,
        source_id="changing",
        url=url,
        fetcher=FakeDocumentFetcher(
            {url: "# A\n\n## Services\nBrand new description of the automation service.\n"}
        ),
        embedding_port=FakeEmbeddingPort(),
    )
    after = [chunk.text for chunk in brain.list_knowledge_chunks(source_id="changing")]
    assert any("Old description" in text for text in before)
    assert not any("Old description" in text for text in after)
    assert any("Brand new description" in text for text in after)


def test_one_failing_source_does_not_abort_the_others() -> None:
    brain = _brain()
    fetcher = FakeDocumentFetcher(
        {f"{SITE}/good.md": "# G\n\n## Services\nWorking content here for the chunker.\n"}
    )
    reports = ingest_website(
        brain,
        website_url=SITE,
        sources=["missing.md", "good.md"],
        fetcher=fetcher,
        embedding_port=FakeEmbeddingPort(),
    )
    statuses = {report.source_id: report.status for report in reports}
    assert statuses["missing.md"] == "error"
    assert statuses["good.md"] == "ingested"


def test_fetch_error_is_recorded_against_the_source() -> None:
    brain = _brain()
    report = ingest_source(
        brain,
        source_id="broken",
        url=f"{SITE}/broken",
        fetcher=FakeDocumentFetcher({}),
        embedding_port=FakeEmbeddingPort(),
    )
    assert report.status == "error"
    assert brain.knowledge_source_hash("broken") == ""


def test_chunks_are_stored_without_vectors_when_embeddings_are_off() -> None:
    """No embedding provider must still yield a keyword-searchable corpus."""
    brain = _brain()
    url = f"{SITE}/llms.txt"
    ingest_source(
        brain,
        source_id="novec",
        url=url,
        fetcher=FakeDocumentFetcher(
            {url: "# S\n\n## Services\nA description long enough to chunk.\n"}
        ),
        embedding_port=DisabledEmbeddingPort(),
    )
    assert brain.list_knowledge_chunks(source_id="novec")
    assert brain.knowledge_vectors(source_id="novec") == []


def test_fetch_rejects_an_oversized_document() -> None:
    class _Huge:
        def fetch(self, url: str) -> str:
            raise KnowledgeFetchError("document too large")

    brain = _brain()
    report = ingest_source(
        brain,
        source_id="huge",
        url=f"{SITE}/huge",
        fetcher=_Huge(),
        embedding_port=FakeEmbeddingPort(),
    )
    assert report.status == "error"


# --------------------------------------------------------------- embeddings


def test_embedding_port_is_disabled_without_configuration() -> None:
    settings = get_settings()
    settings.embedding_model = ""
    assert build_embedding_port(settings).enabled() is False


def test_embeddings_are_returned_in_index_order_not_response_order() -> None:
    """The API documents an `index` per item; assuming array order corrupts every vector."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0]},
                    {"index": 0, "embedding": [1.0, 0.0]},
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    port = OpenAIEmbeddingPort(api_key="k", model="m", dim=2, client=client)
    vectors = port.embed(["first", "second"])
    assert vectors[0][0] == pytest.approx(1.0)
    assert vectors[1][1] == pytest.approx(1.0)


def test_embeddings_are_normalized_on_the_way_in() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [3.0, 4.0]}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    port = OpenAIEmbeddingPort(api_key="k", model="m", dim=2, client=client)
    vector = port.embed(["x"])[0]
    assert sum(value * value for value in vector) == pytest.approx(1.0)


def test_fake_embedding_is_deterministic_across_calls() -> None:
    left = FakeEmbeddingPort().embed(["Assaf builds digital workers"])[0]
    right = FakeEmbeddingPort().embed(["Assaf builds digital workers"])[0]
    assert left == right
