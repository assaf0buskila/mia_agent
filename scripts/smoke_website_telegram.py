"""Opt-in live contact/handoff test; creates one labelled test contact and owner brief.

Unlike smoke_production.py this has deliberate external effects. Run only during an
authorized release test. It never prints tokens, visitor content, or contact values.
The reserved .invalid email cannot address a real customer. No WhatsApp URL is opened.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error

from smoke_production import DEFAULT_BASE, SmokeFailure, _post, check_version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--send-test-summary", action="store_true", required=True)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    try:
        check_version(base, args.sha)
        sid = _post(base, "/v1/website/sessions")["session_id"]
        first = _post(
            base,
            f"/v1/website/sessions/{sid}/messages",
            {
                "text": (
                    "יש לי סטודיו לציפורניים. אני מתאמת תורים ידנית בוואטסאפ "
                    "ומפספסת הודעות בזמן טיפול."
                ),
            },
        )
        if first.get("next_action") != "ask_contact":
            raise SmokeFailure("business and friction did not reach contact capture")
        time.sleep(5)
        contact = _post(
            base,
            f"/v1/website/sessions/{sid}/messages",
            {
                "text": "אלה פרטי הקשר לבדיקת השחרור",
                "name": "בדיקת שחרור מיה",
                "email": f"mia-release-{args.sha[:12]}@example.invalid",
            },
        )
        if contact.get("next_action") not in {"confirm_contact", "handoff"}:
            raise SmokeFailure("structured contact was not confirmed")
        # Inspect the same operation repeatedly. The server owns duplicate prevention;
        # this is not a second synthetic lead or a manual resend of an ambiguous send.
        statuses: list[str] = []
        for _ in range(4):
            result = _post(base, f"/v1/website/sessions/{sid}/handoff")
            statuses.append(str(result.get("notification_status", "")))
            if statuses[-1] == "delivered":
                break
            time.sleep(3)
        if statuses[-1] != "delivered":
            raise SmokeFailure("no confirmed owner delivery: " + ",".join(statuses))
        repeated = _post(base, f"/v1/website/sessions/{sid}/handoff")
        if repeated.get("notification_status") != "delivered":
            raise SmokeFailure("repeat handoff lost confirmed delivery")
        print(
            json.dumps(
                {
                    "result": "PASS",
                    "contact_confirmed": True,
                    "owner_delivery": "accepted",
                    "repeat_handoff": "accepted",
                    "session_id": sid,
                    "test_contact_created": True,
                }
            )
        )
        return 0
    except urllib.error.HTTPError as exc:
        print(f"FAIL website Telegram test: HTTP {exc.code}")
    except SmokeFailure as exc:
        print(f"FAIL website Telegram test: {exc}")
    except Exception as exc:
        print(f"FAIL website Telegram test: {type(exc).__name__}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
