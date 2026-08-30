"""Run the Defect A transcript against a live Mia deployment and print replies.

Read-only for the owner: creates one website session and posts prospect turns,
exactly like a real visitor. Prints each reply so a human can judge progression
rather than trusting a passing test.
"""

from __future__ import annotations

import argparse
import json
import urllib.request

DEFECT_A: tuple[str, ...] = (
    "אני מוכר נעליים יש לי עיסוק רק במלאי",
    "להכניס הכל לשיטס",
    "נעליים מידות דגמים",
    "בערך שעתיים כל פעם",
)
WEBSITE_ORIGIN = "https://www.assafweb.com"


def _post(url: str, payload: dict | None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else b"{}"
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Origin": WEBSITE_ORIGIN},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.loads(response.read().decode("utf-8"))


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=45) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="https://mia.assafweb.com")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    config = _get(f"{base}/v1/website/config")
    print(f"widget={config['widget']} demo={config['demo']}")
    print(f"Mia: {config['opening']}")

    session = _post(f"{base}/v1/website/sessions", None)
    session_id = session["session_id"]
    print(f"session={session_id}\n")

    replies: list[str] = []
    for text in DEFECT_A:
        out = _post(
            f"{base}/v1/website/sessions/{session_id}/messages",
            {"text": text},
        )
        reply = out["message"]
        replies.append(reply)
        print(f"Prospect: {text}")
        print(f"Mia: {reply}")
        print(f"  action={out['next_action']} lead={out['lead_id']}\n")

    print(f"distinct replies: {len(set(replies))}/{len(replies)}")
    looped = [r for r in replies if "יום רגיל בעסק" in r]
    print(f"opening-question restarts: {len(looped)}")

    handoff = _post(
        f"{base}/v1/website/sessions/{session_id}/handoff", None
    )
    token = handoff["token"]
    print(f"\nhandoff token: {token[:6]}… expires {handoff['expires_at']}")
    print(f"whatsapp_url present: {handoff.get('whatsapp_url') is not None}")


if __name__ == "__main__":
    main()
