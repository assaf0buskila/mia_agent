"""Real HTTP app, V2-37 gateway, and V2-26 service on the ledger test worlds."""

import pytest
from app.api.assistant import os as assistant_os
from app.core.config import get_settings
from app.main import app
from fastapi.testclient import TestClient
from tests.services.os_ledger.assistant.support import IP_A, AWorld
from tests.services.os_ledger.statements.support import pg_url


@pytest.fixture
def env(monkeypatch):
    aw = AWorld("fake")
    monkeypatch.setattr(assistant_os, "gateway_for", lambda settings: aw.gateway)
    app.dependency_overrides[get_settings] = lambda: aw.config
    with TestClient(app, base_url="https://api.assafweb.test", client=(IP_A, 50000)) as client:
        yield aw, client
    app.dependency_overrides.pop(get_settings, None)


@pytest.fixture(params=["fake", *(["pg"] if pg_url() else [])])
def control_world(request):
    return AWorld(request.param, request=request)
