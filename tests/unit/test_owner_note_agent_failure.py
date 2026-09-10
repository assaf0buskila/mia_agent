"""Live Telegram: a NOTE / capabilities question must not die as a silent classifier miss.

Assaf's owner bot greets, then every real question answered
'הבדיקה לא עברה כרגע. תנסה שוב.' That line is the NOTE-agent-failure copy, so the
classifier tagged the ask as NOTE and the agent turn failed. This file pins both halves:
the classifier, and the Telegram text Assaf actually receives when the model call fails.
"""

from __future__ import annotations

import httpx
from app.api.owner import process_owner_texts
from app.brain.embeddings import FakeEmbeddingPort
from app.core.config import Settings, get_settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.integrations.base import RecordingMessagePort
from app.integrations.llm_client import LlmClient, LlmModelChain

OWNER_ID = "550077"


def _failing_chain(_settings: Settings) -> LlmModelChain:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, json={"error": {"message": "no"}})

    return LlmModelChain(
        [
            LlmClient(
                api_key="k",
                model="broken-owner",
                client=httpx.Client(transport=httpx.MockTransport(handler)),
            )
        ]
    )


async def test_failed_note_agent_tells_assaf_the_failure_class(monkeypatch) -> None:
    """Repro for the live fallback. The classifier's 'could not classify' line must not
    be what Assaf sees, and the generic NOTE failure must name the class (provider
    error) without a model id or a secret."""
    monkeypatch.setattr("app.domain.owner.brain.build_agent_client", _failing_chain)
    monkeypatch.setattr(
        "app.domain.owner.brain.build_embedding_port", lambda _s: FakeEmbeddingPort()
    )
    settings = get_settings()
    settings.openai_api_key = "k"
    settings.owner_agent_model = "broken-owner"
    settings.memory_enabled = True
    monkeypatch.setattr("app.api.owner.get_settings", lambda: settings)

    init_db()
    session = get_session_factory()()
    port = RecordingMessagePort()
    try:
        result = await process_owner_texts(
            provider="telegram",
            channel=Channel.TELEGRAM,
            items=[
                {
                    "id": "evt.owner.note.fail.1",
                    "from": OWNER_ID,
                    "text": "מה היכולות שלך?",
                }
            ],
            store=LeadStore(session),
            port=port,
            kill_switch=False,
            owner_ids={OWNER_ID},
        )
        session.commit()
    finally:
        session.close()

    assert result["processed"] == 1
    assert port.sent, "no reply was sent to Telegram"
    text = port.sent[0].text
    assert text.startswith("הבדיקה לא עברה כרגע")
    assert "שגיאת ספק" in text
    assert "מה שהבנתי" not in text
    assert "broken-owner" not in text
    assert "k" not in text
    assert "(paraphrased for test)" not in text
