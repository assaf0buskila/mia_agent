"""Composio resource discovery: parsing, ambiguity, caching, and the env-var override.

The inner shape of Composio's `data` payload is not published, so the parsers accept the
documented provider field names AND fall back to a recursive value scan. Both paths are
covered here, plus the refusals that matter: never guess between candidates, never let a
failure break port construction, never override an explicit setting.
"""

from __future__ import annotations

import httpx
import pytest
from app.core.config import get_settings
from app.integrations.composio_discovery import (
    ComposioDiscovery,
    build_discovery,
    cached_resolve,
    choose_site,
    extract_ga4_properties,
    extract_sites,
    reset_cache,
)
from app.integrations.ga4 import DisabledGa4Port, build_ga4_port
from app.integrations.search_console import resolve_gsc_site_url


def _ok(data: object) -> dict:
    return {"successful": True, "error": None, "data": data}


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _discovery(payload: object, *, status: int = 200, website_url: str = "") -> ComposioDiscovery:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return ComposioDiscovery(
        api_key="k", user_id="u", website_url=website_url, client=_client(handler)
    )


# ------------------------------------------------------------------- parsing


def test_sites_parse_from_the_documented_shape() -> None:
    payload = {
        "siteEntry": [{"siteUrl": "https://www.assafweb.com/", "permissionLevel": "owner"}]
    }
    assert extract_sites(payload) == ("https://www.assafweb.com/",)


def test_sites_parse_from_an_undocumented_shape_by_scanning() -> None:
    """The inner payload is not published, so an unexpected wrapper must still work."""
    payload = {"result": {"properties": [{"resource": "sc-domain:assafweb.com"}]}}
    assert extract_sites(payload) == ("sc-domain:assafweb.com",)


def test_sites_ignore_values_that_are_not_properties() -> None:
    payload = {"siteEntry": [{"siteUrl": "https://a.com"}], "note": "owner", "count": "3"}
    assert extract_sites(payload) == ("https://a.com",)


def test_ga4_properties_strip_the_resource_prefix() -> None:
    payload = {"accountSummaries": [{"propertySummaries": [{"property": "properties/123456789"}]}]}
    assert extract_ga4_properties(payload) == ("123456789",)


def test_ga4_accepts_a_bare_numeric_id() -> None:
    assert extract_ga4_properties({"propertyId": "987654321"}) == ("987654321",)


@pytest.mark.parametrize("payload", [None, {}, {"data": []}, "not json", 42])
def test_parsers_never_raise_on_junk(payload: object) -> None:
    assert extract_sites(payload) == ()
    assert extract_ga4_properties(payload) == ()


# ----------------------------------------------------------------- ambiguity


def test_one_candidate_resolves() -> None:
    discovery = _discovery(_ok({"siteEntry": [{"siteUrl": "https://www.assafweb.com/"}]}))
    assert discovery.search_console_site().value == "https://www.assafweb.com/"


def test_two_unrelated_sites_stay_ambiguous_rather_than_guessing() -> None:
    payload = _ok({"siteEntry": [{"siteUrl": "https://a.com/"}, {"siteUrl": "https://b.com/"}]})
    result = _discovery(payload, website_url="https://www.assafweb.com").search_console_site()
    assert result.value == ""
    assert result.ambiguous is True


def test_variants_of_the_configured_site_resolve_to_the_domain_property() -> None:
    """One site commonly has http/https/www/domain properties. That is not ambiguity."""
    candidates = (
        "http://assafweb.com/",
        "https://www.assafweb.com/",
        "sc-domain:assafweb.com",
    )
    chosen = choose_site(candidates, website_url="https://www.assafweb.com")
    assert chosen == "sc-domain:assafweb.com"


def test_a_different_site_is_not_chosen_for_the_configured_host() -> None:
    assert choose_site(("https://someoneelse.com/",), website_url="https://www.assafweb.com") == (
        "https://someoneelse.com/"
    )
    # ...but with a real alternative present it must refuse.
    assert (
        choose_site(
            ("https://someoneelse.com/", "https://other.com/"),
            website_url="https://www.assafweb.com",
        )
        == ""
    )


def test_multiple_ga4_properties_stay_ambiguous() -> None:
    payload = _ok({"a": "properties/111111111", "b": "properties/222222222"})
    result = _discovery(payload).ga4_property()
    assert result.value == ""
    assert len(result.candidates) == 2


# -------------------------------------------------------------------- errors


def test_a_missing_connection_does_not_raise() -> None:
    result = _discovery({"error": {"message": "No connected account"}}, status=400).ga4_property()
    assert result.value == ""
    assert result.error.startswith("http_")


def test_successful_false_is_treated_as_a_failure_despite_http_200() -> None:
    """Composio returns provider failures as 200 with successful:false."""
    payload = {
        "successful": False,
        "error": "upstream",
        "data": {"siteEntry": [{"siteUrl": "https://a.com"}]},
    }
    result = _discovery(payload).search_console_site()
    assert result.value == ""
    assert result.error == "unsuccessful"


def test_data_delivered_as_a_json_string_is_still_parsed() -> None:
    payload = {"successful": True, "data": '{"siteEntry": [{"siteUrl": "https://a.com/"}]}'}
    assert _discovery(payload).search_console_site().value == "https://a.com/"


def test_connected_toolkits_reads_active_slugs_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "connected_accounts" in str(request.url)
        return httpx.Response(
            200,
            json={
                "items": [
                    {"toolkit": {"slug": "GOOGLE_SEARCH_CONSOLE"}},
                    {"toolkit": {"slug": "gmail"}},
                ]
            },
        )

    discovery = ComposioDiscovery(api_key="k", user_id="u", client=_client(handler))
    assert discovery.connected_toolkits() == ("google_search_console", "gmail")


# ------------------------------------------------------- override and caching


def test_an_explicit_env_var_always_wins_and_makes_no_call() -> None:
    called: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(str(request.url))
        return httpx.Response(200, json=_ok({"siteEntry": [{"siteUrl": "https://discovered.com/"}]}))

    settings = get_settings()
    settings.composio_api_key = "k"
    settings.composio_user_id = "u"
    settings.composio_discovery = True
    settings.gsc_site_url = "https://explicit.example/"
    reset_cache()
    assert resolve_gsc_site_url(settings) == "https://explicit.example/"
    assert called == []


def test_discovery_is_skipped_entirely_without_composio_credentials() -> None:
    settings = get_settings()
    settings.composio_api_key = ""
    settings.composio_user_id = ""
    settings.composio_discovery = True
    settings.gsc_site_url = ""
    reset_cache()
    assert build_discovery(settings) is None
    assert resolve_gsc_site_url(settings) == ""


def test_resolution_is_cached_per_process() -> None:
    calls = {"n": 0}

    def resolver():
        calls["n"] += 1
        from app.integrations.composio_discovery import DiscoveryResult

        return DiscoveryResult(value="resolved")

    reset_cache()
    assert cached_resolve("k1", resolver) == "resolved"
    assert cached_resolve("k1", resolver) == "resolved"
    assert calls["n"] == 1


def test_a_raising_resolver_is_cached_as_empty_and_never_propagates() -> None:
    def resolver():
        raise RuntimeError("composio exploded")

    reset_cache()
    assert cached_resolve("boom", resolver) == ""
    assert cached_resolve("boom", resolver) == ""


def test_ga4_port_still_builds_disabled_without_credentials() -> None:
    settings = get_settings()
    settings.composio_api_key = ""
    settings.composio_user_id = ""
    settings.composio_discovery = True
    settings.ga4_property_id = ""
    reset_cache()
    assert isinstance(build_ga4_port(settings), DisabledGa4Port)


def test_an_invalid_explicit_ga4_property_is_not_replaced_by_discovery() -> None:
    """A wrong value is a mistake to surface, not something to silently paper over."""
    settings = get_settings()
    settings.composio_api_key = "k"
    settings.composio_user_id = "u"
    settings.composio_discovery = True
    settings.ga4_property_id = "not-a-property"
    reset_cache()
    assert isinstance(build_ga4_port(settings), DisabledGa4Port)


def test_discovery_is_off_unless_explicitly_enabled() -> None:
    """The default must add no network call to per-request port construction."""
    settings = get_settings()
    settings.composio_api_key = "k"
    settings.composio_user_id = "u"
    settings.gsc_site_url = ""
    assert settings.composio_discovery is False
    assert build_discovery(settings) is None
    reset_cache()
    assert resolve_gsc_site_url(settings) == ""


# ------------------------------------------------ version recovery on 404


def _seq_client(responses: list) -> httpx.Client:
    """Replays (status, json) in order and records the paths/bodies it saw."""
    calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = {}
        if request.content:
            import json as _json

            try:
                body = _json.loads(request.content.decode("utf-8"))
            except ValueError:
                body = {}
        calls.append((str(request.url), body))
        status, payload = responses[min(len(calls) - 1, len(responses) - 1)]
        return httpx.Response(status, json=payload)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    client.recorded = calls  # type: ignore[attr-defined]
    return client


def test_discovery_sends_no_version_so_composio_resolves_latest() -> None:
    """v3.1 documents omission as "latest". Pinning a TOOLKIT version into this
    TOOL-scoped field is what produced the live 404s."""
    client = _seq_client(
        [(200, _ok({"siteEntry": [{"siteUrl": "https://www.assafweb.com/"}]}))]
    )
    discovery = ComposioDiscovery(api_key="k", user_id="u", client=client)
    assert discovery.search_console_site().value == "https://www.assafweb.com/"
    assert len(client.recorded) == 1
    assert "version" not in client.recorded[0][1]


def test_a_404_recovers_by_resolving_this_tool_own_version() -> None:
    client = _seq_client(
        [
            (404, {"error": {"message": "not found"}}),   # execute, no version
            (200, {"tool": {"version": "20260901_00"}}),   # GET /tools/{slug}
            (200, _ok({"siteEntry": [{"siteUrl": "https://a.com/"}]})),
        ]
    )
    discovery = ComposioDiscovery(api_key="k", user_id="u", client=client)
    assert discovery.search_console_site().value == "https://a.com/"
    assert "/tools/GOOGLE_SEARCH_CONSOLE_LIST_SITES" in client.recorded[1][0]
    assert client.recorded[2][1].get("version") == "20260901_00"


def test_a_404_with_no_resolvable_version_gives_up_cleanly() -> None:
    client = _seq_client([(404, {"error": {}}), (404, {"error": {}})])
    discovery = ComposioDiscovery(api_key="k", user_id="u", client=client)
    result = discovery.search_console_site()
    assert result.value == ""
    assert result.error.startswith("http_")


def test_a_non_404_failure_is_not_retried() -> None:
    """A 400 or 500 is not a version problem; retrying twice would just be noise."""
    client = _seq_client([(500, {"error": {}})])
    discovery = ComposioDiscovery(api_key="k", user_id="u", client=client)
    result = discovery.search_console_site()
    assert result.value == ""
    assert len(client.recorded) == 1
