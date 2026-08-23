"""Replay the owner messages from Defect B and print the reply Mia actually sends.

Reads only. No sends, no model calls, no network. Run with:
    uv run python scripts/probe_owner_telegram.py
"""

from __future__ import annotations

import asyncio

from app.api.inbound import process_inbound_texts
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.sales import PainLevel, SalesState
from app.integrations.base import RecordingMessagePort

OWNER_ID = "700100200"

MESSAGES: tuple[str, ...] = (
    "מה קרה היום?",
    "תראה לי לידים חמים",
    "תספרי לי על ליד מסוים",
    "מה מחכה לאישור?",
    "תנתחי את השיחות מהאתר",
    "מחר תבדקי אם הליד חזר אלינו",
    "היי",
    "מה המצב",
    "remind me tomorrow about the thing",
)


# Phase 5: the owner conversation has to carry context across turns, so this runs
# as one thread rather than nine independent messages.
FOLLOW_UP: tuple[str, ...] = (
    "מה קרה היום?",
    "מה הכי מעניין?",
    "תבדקי איתו את זה",
)


def _seed_website_conversation(store: LeadStore) -> str:
    """One shoe-store conversation that reached a real manual step."""
    _, lead_id = store.open_channel_lead(
        channel=Channel.WEBSITE, external_id="web_probe_owner_shoes"
    )
    store.save_sales(
        SalesState(
            lead_id=lead_id,
            workflow_known=True,
            manual_step_known=True,
            pain_level=PainLevel.P3,
            impact_confirmed=True,
            discovery_turns=4,
        )
    )
    return lead_id


async def _say(store: LeadStore, port: RecordingMessagePort, db, text: str, tag: str) -> str:
    await process_inbound_texts(
        provider="telegram",
        channel=Channel.TELEGRAM,
        items=[{"id": f"evt.probe.owner.{tag}", "from": OWNER_ID, "text": text}],
        store=store,
        port=port,
        kill_switch=False,
        owner_ids={OWNER_ID},
    )
    db.commit()
    reply = port.sent[-1].text if port.sent else "(no reply)"
    print(f"\n> {text}")
    for line in reply.splitlines():
        print(f"  {line}")
    return reply


async def main() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        print("=== distinct-reply check (Defect B) ===")
        for index, text in enumerate(MESSAGES):
            await _say(store, port, db, text, str(index))
        replies = [message.text for message in port.sent]
        distinct = len(set(replies))
        print(f"\n{distinct} distinct replies for {len(replies)} messages")

        print("\n=== multi-turn owner thread (Phase 5) ===")
        lead_id = _seed_website_conversation(store)
        db.commit()
        print(f"(seeded website lead {lead_id})")
        thread_port = RecordingMessagePort()
        for index, text in enumerate(FOLLOW_UP):
            await _say(store, thread_port, db, text, f"thread.{index}")
        thread = [message.text for message in thread_port.sent]
        print(f"\n{len(set(thread))} distinct replies for {len(thread)} thread messages")
        anchored = sum(1 for reply in thread if lead_id in reply)
        print(f"{anchored} of {len(thread)} replies name the seeded lead")
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
