import httpx
import pytest
from app.core.config import Settings
from app.domain.tools import AdapterHttpError
from app.integrations.seo_audit import (
    DEFAULT_HOMEPAGE_URL,
    DisabledSeoAuditPort,
    FakeSeoAuditPort,
    FirecrawlSeoAuditPort,
    SeoAuditSnapshot,
    _is_allowlisted_https_url,
    build_seo_audit_port,
)


def test_allowlist_rejects_evil_url() -> None:
    assert _is_allowlisted_https_url("https://evil.example.com/") is False
    assert _is_allowlisted_https_url("http://www.assafweb.com/") is False
    assert _is_allowlisted_https_url("https://127.0.0.1/") is False
    assert _is_allowlisted_https_url(DEFAULT_HOMEPAGE_URL) is True


def test_fake_snapshot_missing_title_not_invented() -> None:
    snap = SeoAuditSnapshot(url=DEFAULT_HOMEPAGE_URL, title="", h1_count=1)
    fake = FakeSeoAuditPort(snap)
    result = fake.audit_homepage()
    assert result is not None
    assert result.title == ""
    assert result.description == ""


def test_build_seo_audit_port_disabled_without_key() -> None:
    assert isinstance(build_seo_audit_port(Settings()), DisabledSeoAuditPort)


def test_firecrawl_http_error_raises_adapter_error() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(503))
    client = httpx.Client(transport=transport)
    port = FirecrawlSeoAuditPort(api_key="fc-test", client=client)
    with pytest.raises(AdapterHttpError) as exc_info:
        port.audit_homepage()
    assert exc_info.value.status_code == 503


def test_firecrawl_maps_metadata_fields() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "metadata": {
                        "title": "AssafWeb",
                        "description": "Growth operator",
                        "canonical": "https://www.assafweb.com/",
                    },
                    "markdown": "# AssafWeb\n\nBody",
                },
            },
        )
    )
    client = httpx.Client(transport=transport)
    port = FirecrawlSeoAuditPort(api_key="fc-test", client=client)
    snap = port.audit_homepage()
    assert snap is not None
    assert snap.title == "AssafWeb"
    assert snap.description == "Growth operator"
    assert snap.canonical == "https://www.assafweb.com/"
    assert snap.h1_count == 1
    assert snap.has_json_ld is False
