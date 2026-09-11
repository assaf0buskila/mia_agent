"""Measure the active owner surface with real DB rollback and a recording transport.

Pass this source to run_ecs_command.py -- python -c SOURCE; scripts are not in image.
No real Telegram message, provider action or persistent row is created.
"""

import asyncio
import json
import logging
from time import perf_counter
from uuid import uuid4

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.db.store import LeadStore
from app.domain.events import Channel
from app.integrations.base import RecordingMessagePort
from app.surfaces import owner as owner_surface
from app.tools.registries.owner_tools import tool_names


async def main():
    logging.disable(logging.CRITICAL)
    settings = get_settings()
    owners = settings.telegram_owner_user_id_set()
    if not owners or settings.kill_switch:
        print(json.dumps({"result": "FAIL", "reason": "owner_disabled"}))
        return 1
    actor = sorted(owners)[0]
    forbidden_calls = []

    def forbidden(*args, **kwargs):
        forbidden_calls.append("unexpected_expensive_call")
        raise AssertionError("inventory entered model, history or learning path")

    owner_surface._talk_with_optional_agent = forbidden
    LeadStore.list_conversation_turns = forbidden
    durations = []
    passed = True
    for _ in range(3):
        with get_session_factory()() as db:
            port = RecordingMessagePort()
            store = LeadStore(db)
            event_id = "probe_inventory_" + uuid4().hex
            store.claim_webhook(provider="telegram", provider_event_id=event_id)
            started = perf_counter()
            try:
                result = await owner_surface.run_owner_loop(
                    provider="telegram",
                    channel=Channel.TELEGRAM,
                    item={
                        "id": event_id,
                        "from": actor,
                        "text": "מה הכלים שלך אל תשתמשי בהיסטוריה?",
                    },
                    store=store,
                    port=port,
                    settings=settings,
                    owner_ids=owners,
                )
                durations.append(round((perf_counter() - started) * 1000, 2))
                passed = passed and result.sent and len(port.sent) == 1
                if port.sent:
                    passed = passed and all(name in port.sent[0].text for name in tool_names())
                    passed = passed and port.sent[0].reply_markup is None
            finally:
                db.rollback()
    passed = passed and not forbidden_calls
    print(
        json.dumps(
            {
                "result": "PASS" if passed else "FAIL",
                "server_ms": durations,
                "expensive_calls": len(forbidden_calls),
                "registered": len(tool_names()),
                "transport": "recording",
                "database": "rollback",
            }
        ),
        flush=True,
    )
    return int(not passed)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
