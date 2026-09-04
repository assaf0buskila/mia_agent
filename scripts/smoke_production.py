"""Post-deploy smoke test. Exit 0 means the release is good, non-zero means roll back.

Read-only from Mia's side: it opens website sessions and posts prospect turns exactly
as a visitor would, and never supplies a phone or an email — so no CRM row is written
and no Telegram notification reaches Assaf. Nothing here is destructive.

Usage:
    python scripts/smoke_production.py --sha $(git rev-parse HEAD)
    python scripts/smoke_production.py --sha SHA --base-url https://mia.assafweb.com

Checks:
    A  version   production reports the commit that was just deployed
    B  ladder    a real conversation reaches an offer instead of asking forever
    C  pricing   a price question never invents a number
    D  approval  R5 reports the current approval policy, not the stale deny
    E  turn      a normal website turn succeeds without a server error
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

WEBSITE_ORIGIN = "https://www.assafweb.com"
DEFAULT_BASE = "https://mia.assafweb.com"
# The burst window is 4s; stay clear so turns are not stitched into one thought.
TURN_GAP_S = 5.0

# A real Israeli SMB conversation. No phone, no email: nothing is written anywhere.
LADDER_TURNS = (
    "היי, יש לי מספרה ואני צריך עזרה בניהול התורים בוואטסאפ",
    "רוב היום עובר לי על תיאום תורים והודעות",
    "לקוחות כותבים בוואטסאפ ואני עונה ידנית",
    "זהו",
)
PRICE_QUESTION = "כמה עולה לבנות אתר?"
# The exact refusal. If she cannot quote a published price she must say this and
# nothing else; anything with a number in it would be invented.
NO_PRICE_MARKER = "אין מחיר מפורסם"


class SmokeFailure(Exception):
    """A check that should fail the release."""


def _post(base: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(
        f"{base}{path}",
        data=data,
        headers={"Content-Type": "application/json", "Origin": WEBSITE_ORIGIN},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.loads(response.read().decode("utf-8"))


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(f"{base}{path}", timeout=45) as response:
        return json.loads(response.read().decode("utf-8"))


def _conversation(base: str, turns: tuple[str, ...]) -> list[dict]:
    session_id = _post(base, "/v1/website/sessions")["session_id"]
    out: list[dict] = []
    for index, text in enumerate(turns):
        if index:
            time.sleep(TURN_GAP_S)
        out.append(_post(base, f"/v1/website/sessions/{session_id}/messages", {"text": text}))
    return out


def check_version(base: str, expected_sha: str) -> str:
    health = _get(base, "/health")
    deployment = health.get("deployment") or {}
    running = str(deployment.get("commit_sha") or "").strip()
    if not running:
        raise SmokeFailure(
            "production reports no commit sha. The image predates the build stamp, or "
            "MIA_BUILD_SHA was not set on the task definition."
        )
    if not (running.startswith(expected_sha) or expected_sha.startswith(running)):
        raise SmokeFailure(
            f"production is running {running[:12]}, expected {expected_sha[:12]}. "
            "The deploy did not land."
        )
    return (
        f"running {running[:12]} · prompt {deployment.get('prompt_version')} · "
        f"schema {deployment.get('schema_version') or 'unknown'}"
    )


def check_sales_ladder(base: str) -> str:
    replies = _conversation(base, LADDER_TURNS)
    actions = [str(reply.get("next_action") or "?") for reply in replies]
    if "ask_contact" not in actions and "confirm_contact" not in actions:
        raise SmokeFailure(
            f"the conversation never reached an offer: {' -> '.join(actions)}. "
            "Mia is asking discovery questions forever, so the sell ladder is not live."
        )
    texts = [str(reply.get("message") or "") for reply in replies]
    if len(set(texts)) != len(texts):
        raise SmokeFailure("Mia repeated a reply verbatim within one conversation")
    return " -> ".join(actions)


def check_pricing(base: str, *, require_quote: bool) -> str:
    reply = _conversation(base, (PRICE_QUESTION,))[0]
    action = str(reply.get("next_action") or "?")
    message = str(reply.get("message") or "")
    if action not in {"answer", "no_price"}:
        raise SmokeFailure(f"a price question produced action={action}")
    quoted = action == "answer"
    if quoted:
        # Retrieval found a price, so the reply must not also deny that one exists.
        # The pre-v11 prompt asserted "there is no public price list" as a fact and
        # contradicted its own evidence in the same breath.
        for denial in ("אין מחירון", "אין מחיר מפורסם", "no public price"):
            if denial in message:
                raise SmokeFailure(
                    "a published price was retrieved but the reply still denies one "
                    f"exists: {message[:120]!r}"
                )
    if not quoted:
        # Refusing is allowed; inventing a number is not.
        if NO_PRICE_MARKER not in message:
            raise SmokeFailure(
                "Mia declined to quote a price but did not use the published refusal: "
                f"{message[:120]!r}"
            )
        if require_quote:
            raise SmokeFailure(
                "no published price was quoted. The corpus has no PRICING chunk for "
                "this question, or the ingest has not run."
            )
        return "refused honestly (no published price retrieved)"
    return f"quoted a published price: {message[:80]!r}"


def check_approval_policy(base: str) -> str:
    risk = (_get(base, "/health").get("risk") or {})
    value = str(risk.get("R5_destructive") or "")
    if value != "approval":
        raise SmokeFailure(
            f"R5_destructive reports {value!r}, expected 'approval'. Production is "
            "stale, or the policy changed."
        )
    return "R5 reports approval"


def check_basic_turn(base: str) -> str:
    reply = _conversation(base, ("היי, מה אתם עושים?",))[0]
    if not str(reply.get("message") or "").strip():
        raise SmokeFailure("a normal website turn returned an empty reply")
    return "a normal turn answered"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True, help="the commit that was deployed")
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument(
        "--require-price-quote",
        action="store_true",
        help="fail when no published price is quoted, not only when one is invented",
    )
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    sha = args.sha.strip()

    checks = (
        ("A version", lambda: check_version(base, sha)),
        ("B ladder", lambda: check_sales_ladder(base)),
        ("C pricing", lambda: check_pricing(base, require_quote=args.require_price_quote)),
        ("D approval", lambda: check_approval_policy(base)),
        ("E basic turn", lambda: check_basic_turn(base)),
    )

    failures = 0
    for name, run in checks:
        try:
            detail = run()
        except SmokeFailure as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
        except urllib.error.HTTPError as exc:
            failures += 1
            print(f"FAIL {name}: HTTP {exc.code} from production")
        except Exception as exc:  # noqa: BLE001 - a smoke test reports, never crashes
            failures += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {name}: {detail}")

    if failures:
        print(f"\n{failures} check(s) failed. Treat this release as failed and roll back.")
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
