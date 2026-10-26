"""Static bearer refusal keeps its uniform envelope with the existing local base setting."""

from app.api.assistant import os as assistant_os
from app.core.config import get_settings
from app.main import app
from fastapi.testclient import TestClient

from tests.services.os_ledger.assistant.support import AWorld


def test_mcp_missing_static_bearer_with_local_public_base_is_uniform401(monkeypatch):
    aw = AWorld("fake", public_base_url="http://127.0.0.1:8000")
    monkeypatch.setattr(assistant_os, "gateway_for", lambda _: aw.gateway)
    app.dependency_overrides[get_settings] = lambda: aw.config
    try:
        response = TestClient(app).post("/v1/assistant/mcp", json={})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "assistant_auth_required"
        assert response.headers["www-authenticate"] == (
            'Bearer resource_metadata="http://127.0.0.1:8000'
            '/.well-known/oauth-protected-resource/v1/assistant/mcp"')
    finally:
        app.dependency_overrides.pop(get_settings, None)
