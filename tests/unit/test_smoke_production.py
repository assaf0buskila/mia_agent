"""The smoke test must be able to fail a release, and must not write anything.

A smoke test that always passes is worse than none: it converts an unverified deploy
into a verified-looking one.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "smoke_production", pathlib.Path("scripts/smoke_production.py")
)
smoke = importlib.util.module_from_spec(_SPEC)
sys.modules["smoke_production"] = smoke
assert _SPEC.loader is not None
_SPEC.loader.exec_module(smoke)

SHA = "abc123def4567890"


def _health(**over) -> dict:
    body = {
        "deployment": {
            "commit_sha": SHA,
            "prompt_version": "sales_reply_v11",
            "schema_version": "20260904_website_session_state.sql",
        },
        "risk": {"R5_destructive": "approval"},
    }
    body.update(over)
    return body


def test_version_passes_when_the_deploy_landed(monkeypatch) -> None:
    monkeypatch.setattr(smoke, "_get", lambda base, path: _health())
    assert SHA[:12] in smoke.check_version("http://x", SHA)


def test_version_fails_when_production_is_stale(monkeypatch) -> None:
    monkeypatch.setattr(
        smoke, "_get", lambda base, path: _health(deployment={"commit_sha": "0000ffff"})
    )
    with pytest.raises(smoke.SmokeFailure, match="did not land"):
        smoke.check_version("http://x", SHA)


def test_version_fails_when_the_image_carries_no_commit(monkeypatch) -> None:
    monkeypatch.setattr(smoke, "_get", lambda base, path: _health(deployment={}))
    with pytest.raises(smoke.SmokeFailure, match="no commit sha"):
        smoke.check_version("http://x", SHA)


def test_ladder_fails_when_mia_only_asks_questions(monkeypatch) -> None:
    """The exact production regression this batch is gating against."""
    monkeypatch.setattr(
        smoke,
        "_conversation",
        lambda base, turns: [
            {"next_action": "answer", "message": f"q{i}"} for i in range(len(turns))
        ],
    )
    with pytest.raises(smoke.SmokeFailure, match="never reached an offer"):
        smoke.check_sales_ladder("http://x")


def test_ladder_passes_when_the_conversation_reaches_an_offer(monkeypatch) -> None:
    monkeypatch.setattr(
        smoke,
        "_conversation",
        lambda base, turns: [
            {"next_action": "answer", "message": "a"},
            {"next_action": "answer", "message": "b"},
            {"next_action": "answer", "message": "c"},
            {"next_action": "ask_contact", "message": "d"},
        ],
    )
    assert "ask_contact" in smoke.check_sales_ladder("http://x")


def test_ladder_fails_on_a_verbatim_repeat(monkeypatch) -> None:
    monkeypatch.setattr(
        smoke,
        "_conversation",
        lambda base, turns: [
            {"next_action": "answer", "message": "same"},
            {"next_action": "answer", "message": "same"},
            {"next_action": "ask_contact", "message": "offer"},
        ],
    )
    with pytest.raises(smoke.SmokeFailure, match="repeated"):
        smoke.check_sales_ladder("http://x")


def test_relaying_the_published_position_is_not_a_failure(monkeypatch) -> None:
    """This gate used to fail Mia for being right.

    assafweb.com publishes no price: `pricing.md` ingests as scope and service copy,
    and no chunk carries an amount. The published position IS "it depends on scope",
    so a faithful reply says so. The old check read `action == "answer"` as proof that
    a NUMBER had been retrieved and called the honest answer a contradiction — it
    failed about two live runs in three with nothing wrong.
    """
    monkeypatch.setattr(
        smoke,
        "_conversation",
        lambda base, turns: [
            {
                "next_action": "answer",
                "message": "מחיר לבניית אתר תלוי במה שבונים, ואין מחירון ציבורי קבוע.",
            }
        ],
    )
    assert "without inventing" in smoke.check_pricing("http://x", require_quote=False)


def test_pricing_fails_on_an_invented_amount(monkeypatch) -> None:
    """The invariant this check is actually for: never state a number nobody published."""
    monkeypatch.setattr(
        smoke,
        "_conversation",
        lambda base, turns: [
            {"next_action": "answer", "message": 'בניית אתר עולה 4,500 ש"ח.'}
        ],
    )
    with pytest.raises(smoke.SmokeFailure, match="invented number"):
        smoke.check_pricing("http://x", require_quote=False)


def test_a_number_without_a_currency_is_not_a_price(monkeypatch) -> None:
    """A timeline or a count must not be mistaken for money."""
    monkeypatch.setattr(
        smoke,
        "_conversation",
        lambda base, turns: [
            {"next_action": "answer", "message": "בונים תוך 2-3 שבועות, תלוי ב-100 עמודים."}
        ],
    )
    assert "without inventing" in smoke.check_pricing("http://x", require_quote=False)


def test_require_price_quote_demands_an_actual_amount(monkeypatch) -> None:
    """Strict mode is for a corpus known to publish a price."""
    monkeypatch.setattr(
        smoke,
        "_conversation",
        lambda base, turns: [
            {"next_action": "answer", "message": "המחיר תלוי בהיקף."}
        ],
    )
    with pytest.raises(smoke.SmokeFailure, match="states no amount"):
        smoke.check_pricing("http://x", require_quote=True)


def test_pricing_allows_an_honest_refusal(monkeypatch) -> None:
    monkeypatch.setattr(
        smoke,
        "_conversation",
        lambda base, turns: [
            {"next_action": "no_price", "message": "אין מחיר מפורסם באתר. אסף יגיד."}
        ],
    )
    assert "refused honestly" in smoke.check_pricing("http://x", require_quote=False)


def test_pricing_fails_on_a_refusal_that_is_not_the_published_wording(monkeypatch) -> None:
    """Anything other than the exact refusal risks an invented number."""
    monkeypatch.setattr(
        smoke,
        "_conversation",
        lambda base, turns: [{"next_action": "no_price", "message": "בערך 5000 שקל."}],
    )
    with pytest.raises(smoke.SmokeFailure):
        smoke.check_pricing("http://x", require_quote=False)


def test_approval_policy_fails_on_the_stale_deny(monkeypatch) -> None:
    monkeypatch.setattr(
        smoke, "_get", lambda base, path: _health(risk={"R5_destructive": "deny"})
    )
    with pytest.raises(smoke.SmokeFailure, match="stale"):
        smoke.check_approval_policy("http://x")


def test_basic_turn_fails_on_an_empty_reply(monkeypatch) -> None:
    monkeypatch.setattr(
        smoke, "_conversation", lambda base, turns: [{"next_action": "answer", "message": "  "}]
    )
    with pytest.raises(smoke.SmokeFailure, match="empty reply"):
        smoke.check_basic_turn("http://x")


def test_the_smoke_conversation_never_sends_contact_details() -> None:
    """No phone or email means no CRM row and no Telegram notification."""
    blob = " ".join(smoke.LADDER_TURNS) + smoke.PRICE_QUESTION
    assert "@" not in blob
    assert not any(char.isdigit() for char in blob)
