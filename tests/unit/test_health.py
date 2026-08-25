from app.core.config import MiaEnv, Settings
from app.main import app, openapi_surface, owner_integrations
from fastapi.testclient import TestClient


def test_sales_llm_ready_needs_key_and_model() -> None:
    blank = Settings(_env_file=None, openai_api_key="", sales_model="", sales_fallback_model="")
    assert blank.sales_llm_ready() is False
    assert blank.sales_gemini_ready() is False
    assert blank.composio_ready() is False
    assert blank.composio_webhook_ready() is False
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
    assert both.composio_webhook_ready() is False
    hook = Settings(_env_file=None, composio_webhook_secret="s")
    assert hook.composio_webhook_ready() is True
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
    no_owner = Settings(_env_file=None, whatsapp_owner_phones="")
    assert no_owner.whatsapp_owner_ready() is False
    assert no_owner.whatsapp_ingest_ready() is False
    owner = Settings(
        _env_file=None,
        whatsapp_owner_phones="972523393768",
        whatsapp_verify_token="v",
        whatsapp_app_secret="s",
    )
    assert owner.whatsapp_owner_ready() is True
    assert owner.whatsapp_ingest_ready() is True
    composio_only = Settings(
        _env_file=None,
        composio_api_key="k",
        composio_user_id="u",
        whatsapp_sender="composio",
        whatsapp_verify_token="",
        whatsapp_app_secret="",
    )
    assert composio_only.whatsapp_ingest_ready() is False
    assert composio_only.whatsapp_provider_label() == "composio"
    assert composio_only.whatsapp_connected_ready() is True
    assert composio_only.whatsapp_send_ready() is False
    composio_send = Settings(
        _env_file=None,
        composio_api_key="k",
        composio_user_id="u",
        whatsapp_sender="composio",
        whatsapp_phone_number_id="123",
    )
    assert composio_send.whatsapp_send_ready() is True
    assert composio_send.whatsapp_ingest_ready() is False
    meta_send = Settings(
        _env_file=None,
        whatsapp_access_token="t",
        whatsapp_phone_number_id="123",
    )
    assert meta_send.whatsapp_provider_label() == "meta"
    assert meta_send.whatsapp_send_ready() is True
    assert meta_send.whatsapp_connected_ready() is True


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
    assert body["composio_webhook"] is False
    assert body["postgres"] is False
    assert body["public_https"] is False
    assert body["whatsapp_ingest"] is False
    assert body["whatsapp_owner"] is False
    assert body["whatsapp_provider"] == "meta"
    assert body["whatsapp_connected"] is False
    assert body["whatsapp_send"] is False
    assert body["risk"]["R4_meta_writes"] == "approval"
    assert body["risk"]["R5_destructive"] == "deny"
    assert body["risk"]["kill_switch"] is False
    assert body["capabilities"]["http_api"] == "alive"
    assert body["capabilities"]["identity"] == "alive"
    assert body["capabilities"]["sales_state"] == "alive"
    assert body["capabilities"]["sales_reply"] == "alive"
    assert body["capabilities"]["risk_policy"] == "alive"
    assert body["capabilities"]["website"] == "alive"
    assert body["capabilities"]["langgraph"] == "alive"
    assert body["capabilities"]["whatsapp"] == "alive"
    assert body["capabilities"]["voice_stt"] == "alive"
    assert body["capabilities"]["instagram"] == "alive"
    assert "manychat" not in body["capabilities"]
    assert body["capabilities"]["gmail"] == "alive"
    assert body["capabilities"]["calendar"] == "alive"
    assert body["capabilities"]["sheets_mirror"] == "alive"
    assert body["capabilities"]["meta_ads"] == "alive"
    assert body["capabilities"]["content_performance"] == "alive"
    assert body["capabilities"]["campaign_analysis"] == "alive"
    assert body["capabilities"]["campaign_pacing"] == "alive"
    assert body["capabilities"]["research"] == "alive"
    assert body["capabilities"]["linkedin"] == "alive"
    assert body["capabilities"]["owner_learning"] == "alive"
    assert body["capabilities"]["graph_lab"] == "alive"
    assert body["capabilities"]["demo_mode"] == "alive"
    assert body["capabilities"]["follow_up"] == "alive"
    assert body["capabilities"]["due_scan"] == "alive"
    assert body["capabilities"]["conversation_kill"] == "alive"
    assert body["capabilities"]["meeting_brief"] == "alive"
    assert body["capabilities"]["meeting_debrief"] == "alive"
    assert body["capabilities"]["meetings"] == "alive"
    assert body["capabilities"]["ai_runs"] == "alive"
    assert body["capabilities"]["tool_runs"] == "alive"
    assert body["capabilities"]["canonical_events"] == "alive"
    assert body["capabilities"]["aws_runtime"] == "specified"
    assert body["capabilities"]["brain_memory"] == "alive"
    assert body["capabilities"]["brain_knowledge"] == "wired"
    assert body["capabilities"]["brain_retrieval"] == "wired"
    assert body["capabilities"]["embeddings"] == "wired"
    assert body["capabilities"]["owner_agent"] == "wired"
    assert "instagram" in body["capabilities"]
    integrations = body["owner_integrations"]
    assert integrations["gmail_send"] is False
    assert integrations["whatsapp_handoff_send"] is False
    assert integrations["research_apify"] is False
    assert integrations["composio"] is False
    assert "MIA_COMPOSIO_API_KEY" in integrations["missing"]
    assert integrations["linkedin_analytics"] is False
    assert "MIA_LINKEDIN_ACCESS_TOKEN" not in integrations["missing"]
    assert "MIA_GSC_SITE_URL" in integrations["missing"]
    assert "MIA_GA4_PROPERTY_ID" in integrations["missing"]
    assert "MIA_SHEETS_SPREADSHEET_ID" in integrations["missing"]
    assert "MIA_META_ADS_ACCOUNT_ID" in integrations["missing"]
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
        meta_ads_account_id="",
        sheets_spreadsheet_id="",
        linkedin_access_token="",
    )
    integrations = owner_integrations(settings)
    assert integrations["composio"] is True
    assert integrations["search_console"] is False
    assert integrations["ga4"] is False
    assert integrations["sheets_mirror"] is False
    assert integrations["linkedin_profile"] is True
    assert integrations["linkedin_analytics"] is False
    assert integrations["research_firecrawl"] is False
    assert integrations["missing"] == [
        "MIA_FIRECRAWL_API_KEY",
        "MIA_SHEETS_SPREADSHEET_ID",
        "MIA_GSC_SITE_URL",
        "MIA_GA4_PROPERTY_ID",
        "MIA_META_ADS_ACCOUNT_ID",
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
        meta_ads_account_id="",
        sheets_spreadsheet_id="",
        linkedin_access_token="",
    )
    integrations = owner_integrations(settings)
    assert integrations["search_console"] is True
    assert integrations["ga4"] is True
    assert integrations["sheets_mirror"] is False
    assert integrations["linkedin_analytics"] is False
    assert "MIA_GSC_SITE_URL" not in integrations["missing"]
    assert "MIA_GA4_PROPERTY_ID" not in integrations["missing"]
    assert "MIA_META_ADS_ACCOUNT_ID" not in integrations["missing"]
    assert integrations["missing"] == [
        "MIA_FIRECRAWL_API_KEY",
        "MIA_SHEETS_SPREADSHEET_ID",
    ]


def test_owner_integrations_linkedin_token_only_enables_analytics() -> None:
    settings = Settings(
        _env_file=None,
        composio_api_key="k",
        composio_user_id="u",
        composio_discovery=True,
        firecrawl_api_key="fc",
        sheets_spreadsheet_id="sheet",
        linkedin_access_token="li-token",
    )
    integrations = owner_integrations(settings)
    assert integrations["linkedin_profile"] is True
    assert integrations["linkedin_analytics"] is True
    assert integrations["missing"] == []


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
