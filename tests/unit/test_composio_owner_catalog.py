"""ADR-043: dynamic owner Composio reads remain narrow, cached and policy-gated."""

import json
from types import SimpleNamespace

import httpx
import pytest
from app.capabilities.policy import execute_capability
from app.capabilities.registry import (
    COMPOSIO_CATALOG_SEARCH,
    COMPOSIO_EXECUTE_READ,
    COMPOSIO_TOOL_SCHEMA,
)
from app.capabilities.types import Principal
from app.core.errors import PermissionDenied
from app.integrations.composio_catalog import (
    CatalogTool,
    ComposioCatalog,
    bounded_result_text,
    risk_for_slug,
    schema_text,
    validate_arguments,
)
from app.tools.registries.owner_tools import ToolContext, execute_tool, tool_definitions


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_active_toolkits_and_tool_list_cache_per_owner_and_never_use_unconnected_toolkit() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "connected_accounts" in str(request.url):
            assert request.url.params.get("user_ids") == "assaf"
            assert request.url.params.get("statuses") == "ACTIVE"
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "toolkit": {"slug": "linkedin"},
                            "user_id": "assaf",
                            "status": "ACTIVE",
                            "is_disabled": False,
                        },
                        {
                            "toolkit": {"slug": "gmail"},
                            "user_id": "somebody-else",
                            "status": "ACTIVE",
                            "is_disabled": False,
                        },
                        {
                            "toolkit": {"slug": "slack"},
                            "user_id": "assaf",
                            "status": "EXPIRED",
                            "is_disabled": False,
                        },
                        {
                            "toolkit": {"slug": "notion"},
                            "user_id": "assaf",
                            "status": "ACTIVE",
                        },
                        {
                            "toolkit": {"slug": "trello"},
                            "user_id": "assaf",
                            "status": "active",
                            "is_disabled": False,
                        },
                        {
                            "toolkit": {"slug": "github"},
                            "user_id": 123,
                            "status": "ACTIVE",
                            "is_disabled": False,
                        },
                    ]
                },
            )
        assert request.url.params.get("query") == "my info"
        assert request.url.params.get("limit") == "12"
        assert request.url.params.get("cursor") is None
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "slug": "LINKEDIN_GET_MY_INFO",
                        "toolkit": {"slug": "linkedin"},
                        "input_schema": {
                            "type": "object",
                            "properties": {},
                            "required": [],
                            "additionalProperties": False,
                        },
                    }
                ]
            },
        )

    ComposioCatalog.reset_cache()
    catalog = ComposioCatalog(api_key="test", user_id="assaf", client=_client(handler))
    assert catalog.active_toolkits() == ("LINKEDIN",)
    assert catalog.search("my info")[0].slug == "LINKEDIN_GET_MY_INFO"
    assert catalog.search("my info")[0].slug == "LINKEDIN_GET_MY_INFO"
    assert catalog.search("mail", "GMAIL") == ()
    assert len([call for call in calls if "/tools" in call]) == 1


def test_catalog_separates_project_caches_for_the_same_user_id() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        project = request.headers["x-api-key"]
        calls.append((project, str(request.url)))
        slug = "linkedin" if project == "project-a" else "gmail"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "toolkit": {"slug": slug},
                        "user_id": "same-user",
                        "status": "ACTIVE",
                        "is_disabled": False,
                    }
                ]
            },
        )

    ComposioCatalog.reset_cache()
    first = ComposioCatalog(api_key="project-a", user_id="same-user", client=_client(handler))
    second = ComposioCatalog(api_key="project-b", user_id="same-user", client=_client(handler))
    assert first.active_toolkits() == ("LINKEDIN",)
    assert second.active_toolkits() == ("GMAIL",)
    assert first.active_toolkits() == ("LINKEDIN",)
    assert second.active_toolkits() == ("GMAIL",)
    assert sum(project == "project-a" for project, _url in calls) == 1
    assert sum(project == "project-b" for project, _url in calls) == 1


def test_search_accepts_provider_soft_match_for_a_natural_language_paraphrase() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "connected_accounts" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "toolkit": {"slug": "gmail"},
                            "user_id": "owner",
                            "status": "ACTIVE",
                            "is_disabled": False,
                        }
                    ]
                },
            )
        assert request.url.params.get("query") == "show my latest emails"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "slug": "GMAIL_SEARCH_EMAILS",
                        "toolkit": {"slug": "gmail"},
                        "description": "Search mailbox messages",
                        "input_parameters": {},
                    }
                ]
            },
        )

    ComposioCatalog.reset_cache()
    catalog = ComposioCatalog(api_key="project", user_id="owner", client=_client(handler))
    assert catalog.search("show my latest emails")[0].slug == "GMAIL_SEARCH_EMAILS"


def test_catalog_does_not_cache_a_transient_provider_failure() -> None:
    tool_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal tool_attempts
        if "connected_accounts" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "toolkit": {"slug": "linkedin"},
                            "user_id": "owner",
                            "status": "ACTIVE",
                            "is_disabled": False,
                        }
                    ]
                },
            )
        tool_attempts += 1
        if tool_attempts == 1:
            return httpx.Response(503, json={"error": "temporary"})
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "slug": "LINKEDIN_GET_MY_INFO",
                        "toolkit": {"slug": "linkedin"},
                        "input_parameters": {},
                    }
                ]
            },
        )

    ComposioCatalog.reset_cache()
    catalog = ComposioCatalog(api_key="project", user_id="owner", client=_client(handler))
    assert catalog.search("my info", "LINKEDIN") == ()
    assert [tool.slug for tool in catalog.search("my info", "LINKEDIN")] == [
        "LINKEDIN_GET_MY_INFO"
    ]
    assert tool_attempts == 2


def test_transient_active_account_failure_does_not_poison_search_or_detail() -> None:
    connected_attempts: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        project = request.headers["x-api-key"]
        if "connected_accounts" in str(request.url):
            connected_attempts[project] = connected_attempts.get(project, 0) + 1
            if connected_attempts[project] == 1:
                return httpx.Response(503, json={"error": "temporary"})
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "toolkit": {"slug": "linkedin"},
                            "user_id": "owner",
                            "status": "ACTIVE",
                            "is_disabled": False,
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "slug": "LINKEDIN_GET_MY_INFO",
                "toolkit": {"slug": "linkedin"},
                "input_parameters": {},
            }
            if str(request.url).endswith("/LINKEDIN_GET_MY_INFO")
            else {
                "items": [
                    {
                        "slug": "LINKEDIN_GET_MY_INFO",
                        "toolkit": {"slug": "linkedin"},
                        "input_parameters": {},
                    }
                ]
            },
        )

    ComposioCatalog.reset_cache()
    search_catalog = ComposioCatalog(
        api_key="search-project", user_id="owner", client=_client(handler)
    )
    assert search_catalog.search("my info", "LINKEDIN") == ()
    assert search_catalog.search("my info", "LINKEDIN")[0].slug == "LINKEDIN_GET_MY_INFO"

    detail_catalog = ComposioCatalog(
        api_key="detail-project", user_id="owner", client=_client(handler)
    )
    assert detail_catalog.detail("LINKEDIN_GET_MY_INFO") is None
    assert detail_catalog.detail("LINKEDIN_GET_MY_INFO").slug == "LINKEDIN_GET_MY_INFO"


def test_detail_refuses_a_provider_response_for_a_different_slug() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "connected_accounts" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "toolkit": {"slug": "gmail"},
                            "user_id": "owner",
                            "status": "ACTIVE",
                            "is_disabled": False,
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "slug": "GMAIL_SEND_EMAIL",
                "toolkit": {"slug": "gmail"},
                "input_parameters": {},
            },
        )

    ComposioCatalog.reset_cache()
    catalog = ComposioCatalog(api_key="project", user_id="owner", client=_client(handler))
    assert catalog.detail("GMAIL_GET_THREADS") is None


@pytest.mark.parametrize(
    "body",
    [
        {"successful": False, "error": "provider rejected"},
        {"successful": None, "error": "provider rejected"},
        {"successful": "false", "error": "provider rejected"},
        {"error": "provider rejected"},
    ],
)
def test_execute_read_treats_provider_level_failure_as_failure(body: dict) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    catalog = ComposioCatalog(api_key="project", user_id="owner", client=_client(handler))
    tool = CatalogTool("LINKEDIN_GET_MY_INFO", "LINKEDIN", "profile", {})
    assert catalog.execute_read(tool, {}) is None


def test_bounded_result_is_valid_json_and_preserves_nested_cursor() -> None:
    text = bounded_result_text(
        {"successful": True, "data": {"rows": ["x" * 5_000], "next_cursor": "page-2"}}
    )
    parsed = json.loads(text)
    assert len(text) <= 3_000
    assert parsed["truncated"] is True
    assert parsed["continuation"]["data.next_cursor"] == "page-2"


def test_oversized_schema_is_refused_instead_of_returned_as_malformed_json() -> None:
    tool = CatalogTool(
        "NOTION_FETCH_DATA",
        "NOTION",
        "fetch",
        {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "x" * 20_000}},
        },
    )
    assert schema_text(tool) is None


def test_schema_validation_rejects_missing_wrong_and_extra_fields() -> None:
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    }
    assert validate_arguments(schema, {}) == "arguments.query is required"
    assert "does not match" in validate_arguments(schema, {"query": 4})
    assert "unknown field" in validate_arguments(schema, {"query": "x", "extra": "x"})
    assert validate_arguments(schema, {"query": "x"}) == ""


def test_schema_preflight_checks_nested_items_enums_and_bounds() -> None:
    schema = {
        "type": "object",
        "properties": {
            "filters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["OPEN", "CLOSED"]},
                    "labels": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 2},
                    },
                },
                "required": ["status", "labels"],
                "additionalProperties": False,
            }
        },
        "required": ["filters"],
        "additionalProperties": False,
    }
    assert validate_arguments(
        schema, {"filters": {"status": "OPEN", "labels": ["ready"]}}
    ) == ""
    assert "allowed value" in validate_arguments(
        schema, {"filters": {"status": "UNKNOWN", "labels": ["ready"]}}
    )
    assert "shorter" in validate_arguments(
        schema, {"filters": {"status": "OPEN", "labels": ["x"]}}
    )


def test_documented_composio_input_parameter_map_is_normalized() -> None:
    tool = ComposioCatalog._tool(
        {
            "slug": "GMAIL_SEARCH_EMAILS",
            "toolkit": {"slug": "gmail"},
            "input_parameters": {
                "query": {
                    "type": "string",
                    "description": "Gmail search query",
                    "required": True,
                },
                "page_size": {"type": "integer", "required": False},
            },
        },
        "GMAIL",
    )
    assert tool is not None
    assert tool.input_schema["required"] == ["query"]
    assert tool.input_schema["properties"]["query"] == {
        "type": "string",
        "description": "Gmail search query",
    }
    assert validate_arguments(tool.input_schema, {"query": "from:daniel"}) == ""


@pytest.mark.parametrize(
    ("slug", "expected"),
    [
        ("LINKEDIN_GET_MY_INFO", "R0"),
        ("GMAIL_SEND_EMAIL", "R3"),
        ("LINKEDIN_POST_UPDATE", "R4"),
        ("GMAIL_DELETE_THREAD", "R5"),
        ("OBSCURE_PROVIDER_DO_THING", "R3"),
        ("CRM_TARGET_AUDIENCE", "R3"),
        ("SLACK_LIST_AND_JOIN_CHANNEL", "R3"),
    ],
)
def test_slug_risk_is_conservative(slug: str, expected: str) -> None:
    assert risk_for_slug(slug).value == expected


def test_slug_risk_ignores_read_words_in_toolkit_prefix() -> None:
    assert (
        risk_for_slug(
            "GOOGLE_SEARCH_CONSOLE_LIST_SITES", "GOOGLE_SEARCH_CONSOLE"
        ).value
        == "R0"
    )
    assert (
        risk_for_slug(
            "GOOGLE_SEARCH_CONSOLE_SUBMIT_SITEMAP", "GOOGLE_SEARCH_CONSOLE"
        ).value
        == "R3"
    )
    assert risk_for_slug("NOTION_FETCH_DATA", "NOTION").value == "R0"


def test_client_cannot_access_any_dynamic_composio_capability() -> None:
    for name in (COMPOSIO_CATALOG_SEARCH, COMPOSIO_TOOL_SCHEMA, COMPOSIO_EXECUTE_READ):
        with pytest.raises(PermissionDenied):
            execute_capability(
                name, principal=Principal.client(source="website"), handlers={}, args={}
            )


class _Catalog:
    tool = CatalogTool(
        "LINKEDIN_GET_MY_INFO",
        "LINKEDIN",
        "profile",
        {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    )

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def detail(self, slug: str):
        if slug == self.tool.slug:
            return self.tool
        if slug == "GMAIL_DELETE_THREAD":
            return CatalogTool(slug, "GMAIL", "delete", self.tool.input_schema)
        return None

    def execute_read(self, tool, arguments):
        assert tool == self.tool
        assert arguments == {}
        return {"successful": True, "data": {"name": "Assaf"}}


def _context(*, kill_switch: bool = False) -> ToolContext:
    return ToolContext(
        store=None,
        brain=None,
        settings=SimpleNamespace(),
        principal=Principal.owner(source="telegram", actor_id="1"),
        embedding_port=None,
        kill_switch=kill_switch,
    )


def test_dynamic_execute_is_read_only_schema_checked_and_kill_switch_checked(monkeypatch) -> None:
    catalog = _Catalog()
    monkeypatch.setattr(
        ComposioCatalog, "from_settings", classmethod(lambda cls, settings: catalog)
    )
    ok = execute_tool(
        "composio_execute_tool",
        {"tool_slug": "LINKEDIN_GET_MY_INFO", "arguments_json": "{}"},
        _context(),
    )
    assert ok.ok and "Assaf" in ok.text
    denied = execute_tool(
        "composio_execute_tool",
        {"tool_slug": "GMAIL_DELETE_THREAD", "arguments_json": "{}"},
        _context(),
    )
    assert not denied.ok and "destructive" in denied.error
    killed = execute_tool(
        "composio_execute_tool",
        {"tool_slug": "LINKEDIN_GET_MY_INFO", "arguments_json": "{}"},
        _context(kill_switch=True),
    )
    assert not killed.ok and "denied" in killed.error


@pytest.mark.parametrize(
    ("arguments_json", "error"),
    [
        ("{", "valid JSON"),
        ("[]", "decode to a JSON object"),
        ("null", "decode to a JSON object"),
    ],
)
def test_dynamic_execute_rejects_malformed_or_non_object_json_before_catalog_lookup(
    monkeypatch, arguments_json: str, error: str
) -> None:
    def forbidden_catalog(_cls, _settings):
        raise AssertionError("invalid JSON must not reach catalog lookup")

    monkeypatch.setattr(
        ComposioCatalog,
        "from_settings",
        classmethod(forbidden_catalog),
    )
    result = execute_tool(
        "composio_execute_tool",
        {"tool_slug": "LINKEDIN_GET_MY_INFO", "arguments_json": arguments_json},
        _context(),
    )
    assert not result.ok
    assert error in result.error


def test_all_meta_tools_deny_before_config_or_catalog_lookup(monkeypatch) -> None:
    def forbidden_catalog(_cls, _settings):
        raise AssertionError("policy must run before configuration or catalog lookup")

    monkeypatch.setattr(
        ComposioCatalog,
        "from_settings",
        classmethod(forbidden_catalog),
    )
    client_context = _context()
    client_context.principal = Principal.client(source="website")
    calls = (
        ("composio_search_tools", {"query": "mail", "toolkit": None}),
        ("composio_get_tool_schema", {"tool_slug": "GMAIL_SEARCH_EMAILS"}),
        (
            "composio_execute_tool",
            {"tool_slug": "GMAIL_SEARCH_EMAILS", "arguments_json": "{}"},
        ),
    )
    for name, arguments in calls:
        killed = execute_tool(name, arguments, _context(kill_switch=True))
        assert not killed.ok and "denied" in killed.error
        denied = execute_tool(name, arguments, client_context)
        assert not denied.ok and "denied" in denied.error


def test_owner_prompt_has_only_three_small_meta_tools_not_catalog_payload() -> None:
    names = {definition["function"]["name"] for definition in tool_definitions()}
    assert {"composio_search_tools", "composio_get_tool_schema", "composio_execute_tool"}.issubset(
        names
    )
    assert len(names) < 40


def test_all_advertised_object_schemas_are_closed_recursively() -> None:
    def assert_closed(schema: object) -> None:
        if isinstance(schema, dict):
            if schema.get("type") == "object":
                assert schema.get("additionalProperties") is False
            for value in schema.values():
                assert_closed(value)
        elif isinstance(schema, list):
            for value in schema:
                assert_closed(value)

    for definition in tool_definitions():
        assert definition["function"]["strict"] is True
        assert_closed(definition["function"]["parameters"])
