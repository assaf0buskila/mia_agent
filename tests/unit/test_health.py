from app.core.config import MiaEnv, Settings
from app.main import app, brain_health, openapi_surface, owner_integrations
from fastapi.testclient import TestClient


def test_sales_llm_ready_needs_key_and_model() -> None:
    blank = Settings(_env_file=None, openai_api_key="", sales_model="", sales_fallback_model="")
    assert blank.sales_llm_ready() is False
    assert blank.sales_gemini_ready() is False
    assert blank.composio_ready() is False
    keyed = Settings(
        _env_file=None,
        openai_api_key="k",
        sales_model="gpt-5.6-luna",
        sales_fallback_model="",
    )
    assert keyed.sales_llm_ready() is True
    gemini = Settings(
        _env_file=None,
        openai_api_key="",
        sales_model="",
        gemini_api_key="g",
        sales_gemini_model="gemini-3.6-flash",
    )
    assert gemini.sales_llm_ready() is True
    assert gemini.sales_gemini_ready() is True
    assert keyed.sales_gemini_ready() is False
    key_only = Settings(_env_file=None, composio_api_key="k", composio_user_id="")
    assert key_only.composio_ready() is False
    both = Settings(_env_file=None, composio_api_key="k", composio_user_id="user_1")
    assert both.composio_ready() is True
    sqlite = Settings(_env_file=None, database_url="sqlite:///./mia.db")
    assert sqlite.postgres_ready() is False
    pg = Settings(_env_file=None, database_url="postgres://u:p@db:5432/mia")
    assert pg.postgres_ready() is True
    loopback = Settings(_env_file=None, public_base_url="http://127.0.0.1:8000")
    assert loopback.public_https_ready() is False
    tunnel = Settings(
        _env_file=None,
        public_base_url="https://random.trycloudflare.com",
    )
    assert tunnel.public_https_ready() is False
    live_host = Settings(_env_file=None, public_base_url="https://mia.assafweb.com")
    assert live_host.public_https_ready() is True


def test_brain_health_respects_purpose_specific_owner_fallback_readiness() -> None:
    sales_fallback = Settings(
        _env_file=None,
        openai_api_key="key",
        sales_model="sales-model",
        owner_agent_model="",
    )
    health = brain_health(sales_fallback)
    assert health["owner_agent"] == {
        "ready": False,
        "missing": ["MIA_OWNER_AGENT_MODEL"],
        "max_steps": 8,
    }

    gemini_fallback = Settings(
        _env_file=None,
        gemini_api_key="key",
        owner_agent_gemini_model="owner-model",
    )
    health = brain_health(gemini_fallback)
    assert health["owner_agent"]["ready"] is True
    assert health["owner_agent"]["missing"] == []
    assert "memory_extraction" not in health

    missing_openai = brain_health(Settings(_env_file=None, owner_agent_model="owner-model"))
    assert missing_openai["owner_agent"] == {
        "ready": False,
        "missing": ["MIA_OPENAI_API_KEY"],
        "max_steps": 8,
    }

    missing_gemini = brain_health(Settings(_env_file=None, owner_agent_gemini_model="owner-model"))
    assert missing_gemini["owner_agent"] == {
        "ready": False,
        "missing": ["MIA_GEMINI_API_KEY"],
        "max_steps": 8,
    }


def test_voice_health_accepts_configured_gemini_transcription() -> None:
    health = brain_health(
        Settings(
            _env_file=None,
            gemini_api_key="test-key",
            gemini_transcribe_model="audio-model",
            telegram_bot_token="test-token",
            openai_api_key="",
        )
    )
    assert health["voice_in"] == {"ready": True, "missing": []}


def test_health_live_is_minimal() -> None:
    client = TestClient(app)
    response = client.get("/health/live")
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"status"}
    assert body["status"] == "ok"


def test_prod_lifespan_skips_create_all(monkeypatch) -> None:
    called = {"n": 0}

    def _skip() -> None:
        called["n"] += 1

    monkeypatch.setattr("app.main.init_db", _skip)
    monkeypatch.setattr(
        "app.main.get_settings",
        lambda: Settings(_env_file=None, env=MiaEnv.PROD),
    )
    with TestClient(app):
        pass
    assert called["n"] == 0


def test_non_prod_lifespan_runs_create_all(monkeypatch) -> None:
    called = {"n": 0}

    def _mark() -> None:
        called["n"] += 1

    monkeypatch.setattr("app.main.init_db", _mark)
    with TestClient(app):
        pass
    assert called["n"] == 1


def test_health_ready_ok() -> None:
    with TestClient(app) as client:
        response = client.get("/health/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def test_health_ready_unavailable(monkeypatch) -> None:
    monkeypatch.setattr("app.main.database_ready", lambda: False)
    client = TestClient(app)
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_health_is_alive() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["kill_switch"] is False
    assert body["demo"] is False
    assert body["website_url"] == "https://www.assafweb.com"
    assert body["public_base_url"] == "http://127.0.0.1:8000"
    assert body["sales_llm"] is False
    assert body["sales_gemini"] is False
    assert body["composio"] is False
    assert body["postgres"] is False
    assert body["public_https"] is False
    assert body["risk"]["R4_meta_writes"] == "deny"
    # R5 is denied even if a stale legacy approval row exists.
    assert body["risk"]["R5_destructive"] == "deny"
    assert body["risk"]["kill_switch"] is False
    for capability in ("http_api", "risk_policy", "website", "telegram", "canonical_events"):
        assert body["capabilities"][capability] == "alive"
    assert "whatsapp" not in body["capabilities"]
    assert "reconciliation" not in body["capabilities"]
    assert body["v2"] == {
        "owner_enabled": True,
        "website_enabled": True,
        "crm_enabled": True,
        "delivery_configured": False,
        "delivery_paused": False,
        "passive_owner_memory_enabled": False,
    }
    integrations = body["owner_integrations"]
    assert integrations["gmail_send"] is False
    assert integrations["research_apify"] is False
    assert integrations["composio"] is False
    assert "MIA_COMPOSIO_API_KEY" in integrations["missing"]
    assert "MIA_GSC_SITE_URL" in integrations["missing"]
    assert "MIA_GA4_PROPERTY_ID" in integrations["missing"]
    assert "MIA_SHEETS_SPREADSHEET_ID" not in integrations["missing"]
    assert "MIA_FIRECRAWL_API_KEY" in integrations["missing"]


def test_owner_integrations_discovery_off_lists_ids_honestly() -> None:
    settings = Settings(
        _env_file=None,
        composio_api_key="k",
        composio_user_id="u",
        composio_discovery=False,
        firecrawl_api_key="",
        gsc_site_url="",
        ga4_property_id="",
        sheets_spreadsheet_id="",
    )
    integrations = owner_integrations(settings)
    assert integrations["composio"] is True
    assert integrations["search_console"] is False
    assert integrations["ga4"] is False
    assert integrations["sheets_crm"] is True
    assert integrations["sheets_read"] is True
    assert integrations["sheets_update"] is True
    assert integrations["sheets_append"] is True
    assert integrations["linkedin_profile"] is True
    assert integrations["research_firecrawl"] is False
    assert integrations["missing"] == [
        "MIA_FIRECRAWL_API_KEY",
        "MIA_GSC_SITE_URL",
        "MIA_GA4_PROPERTY_ID",
    ]


def test_owner_integrations_discovery_on_drops_listable_ids() -> None:
    settings = Settings(
        _env_file=None,
        composio_api_key="k",
        composio_user_id="u",
        composio_discovery=True,
        firecrawl_api_key="",
        gsc_site_url="",
        ga4_property_id="",
        sheets_spreadsheet_id="",
    )
    integrations = owner_integrations(settings)
    assert integrations["search_console"] is True
    assert integrations["ga4"] is True
    assert integrations["sheets_crm"] is True
    assert integrations["sheets_read"] is True
    assert integrations["sheets_update"] is True
    assert integrations["sheets_append"] is True
    assert "MIA_GSC_SITE_URL" not in integrations["missing"]
    assert "MIA_GA4_PROPERTY_ID" not in integrations["missing"]
    assert integrations["missing"] == [
        "MIA_FIRECRAWL_API_KEY",
    ]


def test_owner_integrations_sheets_allowlist_enables_adr042_operations() -> None:
    settings = Settings(
        _env_file=None,
        composio_api_key="k",
        composio_user_id="u",
        sheets_spreadsheet_id="",
        sheets_allowed_spreadsheet_ids="allowed-one, allowed-two",
    )
    integrations = owner_integrations(settings)
    assert integrations["sheets_crm"] is True
    assert integrations["sheets_read"] is True
    assert integrations["sheets_update"] is True
    assert integrations["sheets_append"] is True
    assert "MIA_SHEETS_SPREADSHEET_ID" not in integrations["missing"]


def test_owner_integrations_apify_covers_research_without_firecrawl() -> None:
    settings = Settings(
        _env_file=None,
        composio_api_key="k",
        composio_user_id="u",
        composio_discovery=True,
        firecrawl_api_key="",
        apify_token="apify-token",
        sheets_spreadsheet_id="sheet",
    )
    integrations = owner_integrations(settings)
    assert integrations["research_firecrawl"] is False
    assert integrations["research_apify"] is True
    assert integrations["missing"] == []


def test_owner_integrations_firecrawl_hides_apify_flag() -> None:
    settings = Settings(
        _env_file=None,
        composio_api_key="k",
        composio_user_id="u",
        composio_discovery=True,
        firecrawl_api_key="fc",
        apify_token="apify-token",
        sheets_spreadsheet_id="sheet",
    )
    integrations = owner_integrations(settings)
    assert integrations["research_firecrawl"] is True
    assert integrations["research_apify"] is False
    assert integrations["missing"] == []


def test_openapi_surface_prod_hides_docs() -> None:
    assert openapi_surface(env=MiaEnv.PROD) == {
        "docs_url": None,
        "redoc_url": None,
        "openapi_url": None,
    }


def test_openapi_surface_dev_keeps_docs() -> None:
    assert openapi_surface(env=MiaEnv.DEV) == {
        "docs_url": "/docs",
        "redoc_url": "/redoc",
        "openapi_url": "/openapi.json",
    }


def test_openapi_surface_test_keeps_docs() -> None:
    assert openapi_surface(env=MiaEnv.TEST) == {
        "docs_url": "/docs",
        "redoc_url": "/redoc",
        "openapi_url": "/openapi.json",
    }


def test_docs_available_in_test_env() -> None:
    client = TestClient(app)
    response = client.get("/docs")
    assert response.status_code == 200


def test_manychat_route_is_gone() -> None:
    client = TestClient(app)
    response = client.post("/v1/manychat/external-request", json={})
    assert response.status_code == 404


def test_cors_allows_assafweb_origin() -> None:
    client = TestClient(app)
    response = client.get("/health", headers={"Origin": "https://www.assafweb.com"})
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "https://www.assafweb.com"
