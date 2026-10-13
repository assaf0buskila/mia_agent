"""Local probe: print the website next-action sequence for a scripted conversation.

Not a test. Used to inspect real multi-turn behavior while tuning the ladder.
Run: uv run python scripts/probe_website_flow.py
"""

from __future__ import annotations

import os
import sys
from uuid import uuid4

# This probe is deliberately synthetic. Test mode prevents dotenv loading, and every
# inherited database/provider setting that could escape the process is replaced before
# importing the app.
_SYNTHETIC_ENV = {
    "MIA_ENV": "test",
    "MIA_DATABASE_URL": "sqlite:///:memory:",
    "MIA_CRM_V2_ENABLED": "false",
    "MIA_SALES_MODEL": "",
    "MIA_SALES_FALLBACK_MODEL": "",
    "MIA_OPENAI_API_KEY": "",
    "MIA_GEMINI_API_KEY": "",
    "MIA_SALES_GEMINI_MODEL": "",
    "MIA_OPENAI_TRANSCRIBE_FALLBACK_MODEL": "",
    "MIA_TELEGRAM_BOT_TOKEN": "",
    "MIA_TELEGRAM_WEBHOOK_SECRET": "",
    "MIA_TELEGRAM_OWNER_USER_IDS": "",
    "MIA_COMPOSIO_API_KEY": "",
    "MIA_COMPOSIO_USER_ID": "",
    "MIA_COMPOSIO_WEBHOOK_SECRET": "",
    "MIA_COMPOSIO_DISCOVERY": "false",
    "MIA_GMAIL_SEND": "false",
    "MIA_META_WRITE": "false",
    "MIA_WHATSAPP_ACCESS_TOKEN": "",
    "MIA_WHATSAPP_APP_SECRET": "",
    "MIA_WHATSAPP_VERIFY_TOKEN": "",
}
os.environ.update(_SYNTHETIC_ENV)

from app.db.session import init_db  # noqa: E402
from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

CONVERSATIONS: dict[str, list[str]] = {
    "shoes_he": [
        "היי",
        "אני מוכר נעליים יש לי עיסוק רק במלאי",
        "להכניס הכל לשיטס",
        "נעליים מידות דגמים",
        "כל יום שעה",
    ],
    "clinic_en": [
        "We run a clinic and miss calls all day.",
        "ok that's right",
        "I decide this quarter",
        "let's book a meeting",
    ],
    "prelaunch_he": [
        "היי",
        "אני האמת לא עוסק כרגע אני רוצה לפתוח עסק והייתי רוצה אולי לבנות אתר",
        "בי תודה",
    ],
    "one_word_he": ["היי", "מלאי", "כן", "לא יודע", "שיטס"],
    "clinic_objection_en": [
        "We run a clinic and miss calls all day.",
        "we call everyone back by hand from a list",
        "about two hours every day",
        "sure, but that's too expensive",
    ],
    "clinic_meeting_en": [
        "We run a clinic and miss calls all day.",
        "we call everyone back by hand from a list",
        "about two hours every day",
        "I decide this quarter",
        "let's book a meeting",
    ],
}
WEBSITE_ORIGIN = "https://www.assafweb.com"


def main() -> int:
    init_db()
    with TestClient(app) as client:
        for name, messages in CONVERSATIONS.items():
            session = client.post(
                "/v1/website/sessions", headers={"Origin": WEBSITE_ORIGIN}
            ).json()
            session_id = session["session_id"]
            headers = {
                "Origin": WEBSITE_ORIGIN,
                "X-Mia-Session-Credential": session["session_credential"],
            }
            print(f"\n=== {name} ===")
            for text in messages:
                body = client.post(
                    f"/v1/website/sessions/{session_id}/messages",
                    json={"text": text, "client_message_id": f"probe-{uuid4().hex}"},
                    headers=headers,
                ).json()
                print(f"  > {text}")
                print(f"  {body['next_action']:<18} | {body['message']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
