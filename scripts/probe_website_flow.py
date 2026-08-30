"""Local probe: print the website next-action sequence for a scripted conversation.

Not a test. Used to inspect real multi-turn behavior while tuning the ladder.
Run: uv run python scripts/probe_website_flow.py
"""

from __future__ import annotations

import sys

from app.db.session import init_db
from app.main import app
from fastapi.testclient import TestClient

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
            session_id = client.post(
                "/v1/website/sessions", headers={"Origin": WEBSITE_ORIGIN}
            ).json()["session_id"]
            print(f"\n=== {name} ===")
            for text in messages:
                body = client.post(
                    f"/v1/website/sessions/{session_id}/messages",
                    json={"text": text},
                    headers={"Origin": WEBSITE_ORIGIN},
                ).json()
                print(f"  > {text}")
                print(f"  {body['next_action']:<18} | {body['message']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
