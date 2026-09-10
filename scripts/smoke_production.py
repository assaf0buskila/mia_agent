"""Post-deploy read-only smoke for the credentialed website v2 flow."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

WEBSITE_ORIGIN = "https://www.assafweb.com"
DEFAULT_BASE = "https://mia.assafweb.com"


class SmokeFailure(Exception):
    """A check that should fail the release."""


def _post(
    base: str,
    path: str,
    payload: dict | None = None,
    *,
    credential: str = "",
) -> dict:
    headers = {"Content-Type": "application/json", "Origin": WEBSITE_ORIGIN}
    if credential:
        headers["X-Mia-Session-Credential"] = credential
    request = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload or {}).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.loads(response.read().decode())


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(f"{base}{path}", timeout=45) as response:
        return json.loads(response.read().decode())


def check_version(base: str, expected_sha: str) -> str:
    deployment = _get(base, "/health").get("deployment") or {}
    running = str(deployment.get("commit_sha") or "").strip()
    if not running:
        raise SmokeFailure("production reports no commit sha")
    if not (running.startswith(expected_sha) or expected_sha.startswith(running)):
        raise SmokeFailure(f"production runs {running[:12]}, expected {expected_sha[:12]}")
    return f"running {running[:12]}"


def check_approval_policy(base: str) -> str:
    value = str((_get(base, "/health").get("risk") or {}).get("R5_destructive") or "")
    if value != "deny":
        raise SmokeFailure(f"R5_destructive reports {value!r}, expected 'deny'")
    return "R5 reports deny"


def check_website_v2(base: str) -> str:
    created = _post(base, "/v1/website/sessions")
    session_id = str(created.get("session_id") or "")
    credential = str(created.get("session_credential") or "")
    if not session_id or not credential:
        raise SmokeFailure("session response omitted its id or credential")
    payload = {
        "text": "היי, מה אתם עושים?",
        "client_message_id": "production-smoke-1",
    }
    path = f"/v1/website/sessions/{session_id}/messages"
    first = _post(base, path, payload, credential=credential)
    replay = _post(base, path, payload, credential=credential)
    if not str(first.get("message") or "").strip():
        raise SmokeFailure("website turn returned an empty reply")
    if first != replay:
        raise SmokeFailure("same client message id did not replay the committed response")
    if first.get("next_action") not in {"answer", "contact_saved"}:
        raise SmokeFailure(f"website returned unknown action {first.get('next_action')!r}")
    return "credential accepted; reply persisted and replayed"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True)
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    checks = (
        ("version", lambda: check_version(base, args.sha.strip())),
        ("approval", lambda: check_approval_policy(base)),
        ("website v2", lambda: check_website_v2(base)),
    )
    failures = 0
    for name, run in checks:
        try:
            print(f"PASS {name}: {run()}")
        except (SmokeFailure, urllib.error.HTTPError, Exception) as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
