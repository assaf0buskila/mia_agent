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
        "deployment": {"commit_sha": SHA},
        "risk": {"R5_destructive": "deny"},
    }
    body.update(over)
    return body


def test_version_requires_the_deployed_sha(monkeypatch) -> None:
    monkeypatch.setattr(smoke, "_get", lambda base, path: _health())
    assert SHA[:12] in smoke.check_version("http://x", SHA)
    monkeypatch.setattr(
        smoke, "_get", lambda base, path: _health(deployment={"commit_sha": "stale"})
    )
    with pytest.raises(smoke.SmokeFailure, match="expected"):
        smoke.check_version("http://x", SHA)


def test_approval_policy_remains_deny(monkeypatch) -> None:
    monkeypatch.setattr(smoke, "_get", lambda base, path: _health())
    assert smoke.check_approval_policy("http://x") == "R5 reports deny"
    monkeypatch.setattr(
        smoke, "_get", lambda base, path: _health(risk={"R5_destructive": "approval"})
    )
    with pytest.raises(smoke.SmokeFailure, match="expected 'deny'"):
        smoke.check_approval_policy("http://x")


def test_website_smoke_uses_credential_and_checks_dedup(monkeypatch) -> None:
    calls: list[tuple[str, dict | None, str]] = []
    response = {"message": "שלום", "next_action": "answer", "lead_id": ""}

    def fake_post(base, path, payload=None, *, credential=""):
        calls.append((path, payload, credential))
        if path == "/v1/website/sessions":
            return {"session_id": "web_0000000000000001", "session_credential": "secret"}
        return response

    monkeypatch.setattr(smoke, "_post", fake_post)
    assert "persisted and replayed" in smoke.check_website_v2("http://x")
    assert calls[1] == calls[2]
    assert calls[1][2] == "secret"
    assert calls[1][1] == {
        "text": "היי, מה אתם עושים?",
        "client_message_id": "production-smoke-1",
    }


def test_website_smoke_rejects_missing_credential_or_changed_replay(monkeypatch) -> None:
    monkeypatch.setattr(
        smoke,
        "_post",
        lambda *args, **kwargs: {"session_id": "web_0000000000000001"},
    )
    with pytest.raises(smoke.SmokeFailure, match="credential"):
        smoke.check_website_v2("http://x")

    replies = iter(
        (
            {"session_id": "web_0000000000000001", "session_credential": "secret"},
            {"message": "first", "next_action": "answer"},
            {"message": "second", "next_action": "answer"},
        )
    )
    monkeypatch.setattr(smoke, "_post", lambda *args, **kwargs: next(replies))
    with pytest.raises(smoke.SmokeFailure, match="replay"):
        smoke.check_website_v2("http://x")
