"""End-to-end acceptance for the Telegram owner timeout fix.

The two chunks were built separately -- TG-CAP (deterministic capability routing) and
TG-DL (the deadline hierarchy) -- and each proved itself at its own seam. This file
covers what neither could: that the routing is actually WIRED into the live owner
surface, and that voice and text genuinely converge on the same post-preprocessing
budget rather than merely each looking correct in isolation.

Every assertion here is about observable behaviour at a real entry point
(`run_owner_loop`, `process_telegram_owner_update`), not about an internal helper.
"""

from __future__ import annotations

import asyncio
from time import monotonic, perf_counter

import pytest
from app.api.owner import OwnerTurnResult
from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.integrations.base import RecordingMessagePort
from app.integrations.transcribe import FakeTranscriptionPort
from app.surfaces.owner import run_owner_loop
from app.tools.registries.owner_tools import tool_names
from app.workers import telegram_owner

ACTOR = "770044"

# A phrase from the capability answer that carries no digits, so it survives the
# egress formatting (`owner_text`/`isolate`) that rewrites LTR number runs.
LIVE_BOUNDARY_LINE = "לא בדיקת חיבור חיה"
A_CATEGORY_LABEL = "זיכרון וידע"


def _claim(event_id: str, *, envelope_kind: str = "text") -> None:
    init_db()
    db = get_session_factory()()
    try:
        assert LeadStore(db).claim_webhook(
            provider="telegram",
            provider_event_id=event_id,
            channel="telegram",
            envelope_kind=envelope_kind,
        )
        db.commit()
    finally:
        db.close()


async def _run_surface(event_id: str, text: str, monkeypatch, *, forbid_model: bool):
    """One owner turn through the real surface. Returns (result, port, elapsed_s)."""
    if forbid_model:

        def fail_talk(**_kwargs):
            raise AssertionError("this turn must never reach the model/provider path")

        monkeypatch.setattr("app.surfaces.owner._talk_with_optional_agent", fail_talk)

    _claim(event_id)
    db = get_session_factory()()
    port = RecordingMessagePort()
    started = perf_counter()
    try:
        result = await run_owner_loop(
            item={"id": event_id, "from": ACTOR, "chat_id": ACTOR, "text": text},
            store=LeadStore(db),
            port=port,
            settings=Settings(_env_file=None),
            owner_ids={ACTOR},
        )
        elapsed = perf_counter() - started
        db.commit()
    finally:
        db.close()
    return result, port, elapsed


# --- Acceptance 1 + 2: capability requests answer from the registry, no model ---


@pytest.mark.asyncio
async def test_assaf_exact_live_sentence_answers_from_the_registry_without_a_model(
    monkeypatch,
) -> None:
    """The sentence that timed out in production, through the real surface.

    This is the acceptance test for the reported symptom. It must be answered from
    the registered tool registry, with no model and no provider call, fast.
    """
    result, port, elapsed = await _run_surface(
        "acceptance-assaf-exact",
        "תפרטי לי פשוט את כל היכולות שלך, הכל",
        monkeypatch,
        forbid_model=True,
    )

    assert result.sent is True
    assert len(port.sent) == 1
    assert LIVE_BOUNDARY_LINE in port.sent[0].text
    assert A_CATEGORY_LABEL in port.sent[0].text
    # Business categories, not a dump of internal Python identifiers.
    assert not any(name in port.sent[0].text for name in tool_names())
    assert elapsed < 1.0, elapsed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "מה היכולות שלך",
        "מה את יכולה לעשות",
        "מה אפשר לעשות איתך",
        "תגידי לי מה את יכולה לעשות",
        "what can you do",
        "show me everything you can do",
    ],
    ids=["cap-plain", "can-do", "with-you", "tell-me", "en-can-do", "en-show"],
)
async def test_natural_capability_variants_answer_through_the_live_surface(
    text: str, monkeypatch
) -> None:
    result, port, _elapsed = await _run_surface(
        f"acceptance-cap-{abs(hash(text))}", text, monkeypatch, forbid_model=True
    )
    assert result.sent is True
    assert LIVE_BOUNDARY_LINE in port.sent[0].text


# --- Acceptance 4: the narrow tool-name inventory is unchanged ---


@pytest.mark.asyncio
async def test_existing_tool_inventory_phrasing_still_lists_the_tool_names(
    monkeypatch,
) -> None:
    """"מה הכלים שלך" keeps its established answer: the actual tool names.

    The capability route is additive. Asking for the *tools* must still produce the
    exact registry listing, because that is the answer this surface already gave and
    the one a drift test elsewhere pins to the live registry.
    """
    result, port, _elapsed = await _run_surface(
        "acceptance-tools-inventory", "מה הכלים שלך", monkeypatch, forbid_model=True
    )
    assert result.sent is True
    sent = port.sent[0].text
    assert all(name in sent for name in tool_names())


# --- Acceptance 3: ordinary business requests still reach the model ---


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "תבדקי לי את המייל האחרון",
        "מה הכלים שלך ותבדקי לי את המייל",
        "מה היכולות שלך ב-CRM",
        "?",
    ],
    ids=["business-email", "capability-plus-action", "scoped-capability", "bare-question-mark"],
)
async def test_ordinary_requests_are_not_hijacked_by_the_capability_route(
    text: str, monkeypatch
) -> None:
    """The route must not answer a real request with a capability list.

    Including "?" -- a one-character message that is a genuine (if minimal) owner turn
    and must exercise the normal model-led path, and the compound
    "capability + action" phrasing, which is the highest-risk false positive.
    """
    seen: list[str] = []

    def record_talk(*, text: str, **_kwargs):
        seen.append(text)
        return "model answered", False

    monkeypatch.setattr("app.surfaces.owner._talk_with_optional_agent", record_talk)

    result, port, _elapsed = await _run_surface(
        f"acceptance-biz-{abs(hash(text))}", text, monkeypatch, forbid_model=False
    )

    assert seen == [text], seen
    assert result.sent is True
    assert LIVE_BOUNDARY_LINE not in port.sent[0].text


# --- The cross-cutting claim: voice and text share one post-preprocessing budget ---


@pytest.mark.asyncio
async def test_voice_and_text_converge_on_the_same_owner_budget(monkeypatch) -> None:
    """A slow transcription must not buy the owner a smaller reasoning budget.

    TG-DL proved the voice case and the text case separately. The claim that actually
    matters for the production symptom is that they AGREE: whatever preprocessing a
    transport needs -- 0s for text, a slow STT for voice -- the owner loop starts with
    the same wall clock. Before the fix these two numbers differed by exactly the STT
    duration, which is why voice failed first and text failed only on a slow model.
    """
    owner_turn_timeout_seconds = 2.0
    stt_sleep_s = 0.4
    settings = Settings(
        _env_file=None,
        owner_turn_timeout_seconds=owner_turn_timeout_seconds,
        telegram_owner_user_ids=ACTOR,
    )
    monkeypatch.setattr(telegram_owner, "get_settings", lambda: settings)

    async def slow_but_successful_stt(*, item, media, transcribe_port):
        del media, transcribe_port
        await asyncio.sleep(stt_sleep_s)
        item["text"] = "מה יש לי מחר"
        item["source"] = "audio"
        item["stt_provider"] = "fake"
        item["stt_model"] = "fake-model"
        item["language"] = "he"
        item["duration_ms"] = "500"
        item["confidence"] = "0.9"
        item["stt_latency_ms"] = str(int(stt_sleep_s * 1000))
        return item, "", int(stt_sleep_s * 1000)

    monkeypatch.setattr("app.api.telegram._transcribe_telegram_voice", slow_but_successful_stt)

    remaining_by_transport: dict[str, float] = {}

    def _loop_for(transport: str):
        async def fake_owner_loop(
            *, item, store, port, settings, owner_ids, deadline_at=None, delivery_state=None
        ):
            del store, owner_ids, settings
            remaining_by_transport[transport] = (
                deadline_at - monotonic() if deadline_at is not None else float("inf")
            )
            await port.send(
                telegram_owner.outbound_reply(item, text="answer", channel=Channel.TELEGRAM)
            )
            if delivery_state is not None:
                delivery_state["sent"] = True
            return OwnerTurnResult(processed=True, sent=True, last_reply="answer")

        return fake_owner_loop

    for transport, envelope_kind, voice_file_id in (
        ("text", "text", None),
        ("voice", "audio", "voice-file-converge"),
    ):
        event_id = f"acceptance-converge-{transport}"
        _claim(event_id, envelope_kind=envelope_kind)
        monkeypatch.setattr(telegram_owner, "run_owner_loop", _loop_for(transport))
        await telegram_owner.process_telegram_owner_update(
            item={
                "id": event_id,
                "from": ACTOR,
                "chat_id": ACTOR,
                "message_id": "1",
                "text": "" if voice_file_id else "מה יש לי מחר",
            },
            envelope_kind=envelope_kind,
            voice_file_id=voice_file_id,
            port=RecordingMessagePort(),
            transcribe_port=FakeTranscriptionPort("unused"),
        )

    text_remaining = remaining_by_transport["text"]
    voice_remaining = remaining_by_transport["voice"]

    # Each transport starts the owner loop with essentially the whole budget ...
    assert text_remaining >= owner_turn_timeout_seconds - 0.15, text_remaining
    assert voice_remaining >= owner_turn_timeout_seconds - 0.15, voice_remaining
    # ... and, the load-bearing part, they agree with each other. The gap must be far
    # smaller than the STT sleep: before the fix it WAS the STT sleep.
    assert abs(text_remaining - voice_remaining) < stt_sleep_s / 2, remaining_by_transport
