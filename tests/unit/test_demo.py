import json

from app.core.config import MiaEnv, Settings
from app.core.demo import demo_mode_active
from app.db.models import CanonicalEventRow
from app.db.session import get_session_factory, init_db
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import select


def test_health_demo_false_by_default() -> None:
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["demo"] is False


def test_demo_mode_active_false_in_prod() -> None:
    settings = Settings(env=MiaEnv.PROD, demo_mode=True)
    assert demo_mode_active(settings) is False


def test_demo_mode_active_true_when_flag_on_non_prod() -> None:
    settings = Settings(env=MiaEnv.TEST, demo_mode=True)
    assert demo_mode_active(settings) is True
    settings_dev = Settings(env=MiaEnv.DEV, demo_mode=True)
    assert demo_mode_active(settings_dev) is True


def test_demo_mode_active_false_when_flag_off() -> None:
    settings = Settings(env=MiaEnv.TEST, demo_mode=False)
    assert demo_mode_active(settings) is False


def test_demo_endpoints_404_when_inactive() -> None:
    with TestClient(app) as client:
        assert client.get("/v1/demo/status").status_code == 404
        assert client.post("/v1/demo/scripted").status_code == 404


def test_website_config_demo_true_when_flag_on(monkeypatch) -> None:
    monkeypatch.setenv("MIA_DEMO_MODE", "true")
    with TestClient(app) as client:
        body = client.get("/v1/website/config").json()
    assert body["demo"] is True


def test_demo_website_session_preserves_supplied_anonymous_attribution(monkeypatch) -> None:
    monkeypatch.setenv("MIA_DEMO_MODE", "true")
    init_db()
    with TestClient(app) as client:
        created = client.post(
            "/v1/website/sessions",
            params={"utm_source": "meta", "utm_campaign": "yuma"},
        )
        assert created.status_code == 200
        session_id = created.json()["session_id"]
        assert created.json()["lead_id"] == ""
    db = get_session_factory()()
    try:
        attr_rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.conversation_id == session_id,
                    CanonicalEventRow.event_type == "attribution",
                )
            )
        )
        assert len(attr_rows) == 1
        assert attr_rows[0].lead_id is None
        assert json.loads(attr_rows[0].payload_json)["utm_source"] == "meta"
    finally:
        db.close()
