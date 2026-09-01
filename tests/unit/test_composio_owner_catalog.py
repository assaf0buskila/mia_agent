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
from app.domain.events import Channel
from app.domain.owner_callbacks import resolve_owner_callback_result
from app.domain.owner_linkedin_writes import (
    MAX_LINKEDIN_APPROVAL_PARAMETERS_BYTES,
    propose_linkedin_write,
)
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
        assert request.url.params.get("limit") == "25"
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
    assert [tool.slug for tool in catalog.search("my info", "LINKEDIN")] == ["LINKEDIN_GET_MY_INFO"]
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
        {"successful": True, "data": {"rows": ["x" * 10_000], "next_cursor": "page-2"}}
    )
    parsed = json.loads(text)
    assert len(text) <= 8_000
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
    assert validate_arguments(schema, {"filters": {"status": "OPEN", "labels": ["ready"]}}) == ""
    assert "allowed value" in validate_arguments(
        schema, {"filters": {"status": "UNKNOWN", "labels": ["ready"]}}
    )
    assert "shorter" in validate_arguments(schema, {"filters": {"status": "OPEN", "labels": ["x"]}})


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
        ("GMAIL_SEND_DRAFT", "R3"),
        ("GMAIL_REPLY_TO_THREAD", "R3"),
        ("GMAIL_FORWARD_MESSAGE", "R3"),
        ("LINKEDIN_POST_UPDATE", "R4"),
        ("GMAIL_DELETE_THREAD", "R5"),
        ("GMAIL_DELETE_MESSAGE", "R5"),
        ("GMAIL_BATCH_DELETE_MESSAGES", "R5"),
        ("GMAIL_DELETE_DRAFT", "R5"),
        ("GMAIL_DELETE_FILTER", "R5"),
        ("GMAIL_DELETE_LABEL", "R5"),
        ("GOOGLE_SEARCH_CONSOLE_DELETE_SITE", "R5"),
        ("GOOGLE_SEARCH_CONSOLE_ADD_SITE", "R3"),
        ("GOOGLE_ANALYTICS_SEND_EVENTS", "R3"),
        ("GOOGLE_ANALYTICS_ARCHIVE_CUSTOM_DIMENSION", "R3"),
        ("GMAIL_MOVE_TO_TRASH", "R3"),
        ("GOOGLESHEETS_DELETE_DIMENSION", "R5"),
        ("GOOGLESHEETS_CLEAR_VALUES", "R5"),
        ("GOOGLESHEETS_EXECUTE_SQL", "R5"),
        ("GOOGLESHEETS_VALUES_UPDATE", "R1"),
        ("GOOGLESHEETS_UPSERT_ROWS", "R1"),
        ("GOOGLESHEETS_SPREADSHEETS_VALUES_APPEND", "R1"),
        ("GOOGLESHEETS_VALUES_GET", "R0"),
        ("INSTAGRAM_DELETE_COMMENT", "R5"),
        ("INSTAGRAM_DELETE_MESSAGGER_PROFILE", "R5"),
        ("LINKEDIN_DELETE_POST", "R5"),
        ("LINKEDIN_DELETE_UGC_POST", "R5"),
        ("LINKEDIN_DELETE_LINKED_IN_POST", "R5"),
        ("OBSCURE_PROVIDER_DO_THING", "R3"),
        ("CRM_TARGET_AUDIENCE", "R3"),
        ("SLACK_LIST_AND_JOIN_CHANNEL", "R3"),
    ],
)
def test_slug_risk_is_conservative(slug: str, expected: str) -> None:
    assert risk_for_slug(slug).value == expected


def test_slug_risk_ignores_read_words_in_toolkit_prefix() -> None:
    assert risk_for_slug("GOOGLE_SEARCH_CONSOLE_LIST_SITES", "GOOGLE_SEARCH_CONSOLE").value == "R0"
    assert (
        risk_for_slug("GOOGLE_SEARCH_CONSOLE_SUBMIT_SITEMAP", "GOOGLE_SEARCH_CONSOLE").value == "R3"
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
        if slug in {
            "GMAIL_DELETE_THREAD",
            "GMAIL_DELETE_MESSAGE",
            "GMAIL_BATCH_DELETE_MESSAGES",
            "GMAIL_DELETE_DRAFT",
            "GMAIL_SEND_EMAIL",
            "GMAIL_SEND_DRAFT",
            "GMAIL_REPLY_TO_THREAD",
            "GMAIL_FORWARD_MESSAGE",
            "GMAIL_MOVE_TO_TRASH",
            "GOOGLE_SEARCH_CONSOLE_DELETE_SITE",
            "GOOGLE_SEARCH_CONSOLE_ADD_SITE",
            "GOOGLE_SEARCH_CONSOLE_SUBMIT_SITEMAP",
            "GOOGLE_ANALYTICS_SEND_EVENTS",
            "GOOGLE_ANALYTICS_ARCHIVE_CUSTOM_DIMENSION",
            "GOOGLESHEETS_DELETE_DIMENSION",
            "GOOGLESHEETS_CLEAR_VALUES",
            "GOOGLESHEETS_VALUES_UPDATE",
            "GOOGLESHEETS_UPSERT_ROWS",
            "GOOGLESHEETS_SPREADSHEETS_VALUES_APPEND",
            "GOOGLESHEETS_VALUES_GET",
            "INSTAGRAM_GET_IG_USER_MEDIA",
            "INSTAGRAM_DELETE_COMMENT",
            "INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH",
            "LINKEDIN_DELETE_POST",
            "LINKEDIN_POST_UPDATE",
        }:
            toolkit = slug.split("_", 1)[0]
            if slug.startswith("GOOGLESHEETS_"):
                toolkit = "GOOGLESHEETS"
            elif slug.startswith("INSTAGRAM_"):
                toolkit = "INSTAGRAM"
            elif slug.startswith("LINKEDIN_"):
                toolkit = "LINKEDIN"
            elif slug.startswith("GMAIL_"):
                toolkit = "GMAIL"
            elif slug.startswith("GOOGLE_SEARCH_CONSOLE_"):
                toolkit = "GOOGLE_SEARCH_CONSOLE"
            elif slug.startswith("GOOGLE_ANALYTICS_"):
                toolkit = "GOOGLE_ANALYTICS"
            return CatalogTool(slug, toolkit, slug.lower(), self.tool.input_schema)
        return None

    def execute_read(self, tool, arguments):
        del arguments
        return {"successful": True, "data": {"slug": tool.slug, "name": "Assaf"}}


class _ComposioStore:
    def __init__(self) -> None:
        self.saved: list[dict] = []
        self.row = None

    def upsert_composio_approval(self, **kwargs):
        self.saved.append(kwargs)
        self.row = SimpleNamespace(
            **kwargs,
            resource_type="composio_tool",
            approval_id="apr_composio_test",
        )

    def get_approval_by_resource(self, *_args):
        return self.row

    def claim_operation(self, **_kwargs):
        return True

    def save_canonical_event(self, **_kwargs):
        pass

    def complete_operation(self, **_kwargs):
        pass


def _context(*, kill_switch: bool = False) -> ToolContext:
    return ToolContext(
        store=_ComposioStore(),
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
    assert denied.ok and "destructive action is ready" in denied.text
    killed = execute_tool(
        "composio_execute_tool",
        {"tool_slug": "LINKEDIN_GET_MY_INFO", "arguments_json": "{}"},
        _context(kill_switch=True),
    )
    assert not killed.ok and "denied" in killed.error


def test_official_sheet_ig_linkedin_policy_keeps_deletes_off_and_does_not_autopublish(
    monkeypatch,
) -> None:
    catalog = _Catalog()
    monkeypatch.setattr(
        ComposioCatalog, "from_settings", classmethod(lambda cls, settings: catalog)
    )
    ctx = _context()
    delete_denied = execute_tool(
        "composio_execute_tool",
        {"tool_slug": "GOOGLESHEETS_DELETE_DIMENSION", "arguments_json": "{}"},
        ctx,
    )
    assert delete_denied.ok and "destructive action is ready" in delete_denied.text
    clear_denied = execute_tool(
        "composio_execute_tool",
        {"tool_slug": "GOOGLESHEETS_CLEAR_VALUES", "arguments_json": "{}"},
        ctx,
    )
    assert clear_denied.ok and "destructive action is ready" in clear_denied.text
    ig_delete = execute_tool(
        "composio_execute_tool",
        {"tool_slug": "INSTAGRAM_DELETE_COMMENT", "arguments_json": "{}"},
        ctx,
    )
    assert ig_delete.ok and "destructive action is ready" in ig_delete.text
    li_delete = execute_tool(
        "composio_execute_tool",
        {"tool_slug": "LINKEDIN_DELETE_POST", "arguments_json": "{}"},
        ctx,
    )
    assert li_delete.ok and "destructive action is ready" in li_delete.text

    sheet_read = execute_tool(
        "composio_execute_tool",
        {"tool_slug": "GOOGLESHEETS_VALUES_GET", "arguments_json": "{}"},
        ctx,
    )
    assert sheet_read.ok and "GOOGLESHEETS_VALUES_GET" in sheet_read.text
    ig_read = execute_tool(
        "composio_execute_tool",
        {"tool_slug": "INSTAGRAM_GET_IG_USER_MEDIA", "arguments_json": "{}"},
        ctx,
    )
    assert ig_read.ok and "INSTAGRAM_GET_IG_USER_MEDIA" in ig_read.text
    li_read = execute_tool(
        "composio_execute_tool",
        {"tool_slug": "LINKEDIN_GET_MY_INFO", "arguments_json": "{}"},
        ctx,
    )
    assert li_read.ok

    for slug in (
        "GOOGLESHEETS_VALUES_UPDATE",
        "GOOGLESHEETS_UPSERT_ROWS",
        "GOOGLESHEETS_SPREADSHEETS_VALUES_APPEND",
    ):
        result = execute_tool(
            "composio_execute_tool",
            {"tool_slug": slug, "arguments_json": "{}"},
            ctx,
        )
        assert not result.ok
        assert "destructive" not in (result.error or "")
        assert "sheets_update" in (result.error or "")

    publish = execute_tool(
        "composio_execute_tool",
        {"tool_slug": "INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH", "arguments_json": "{}"},
        ctx,
    )
    assert publish.ok and "Composio action is ready" in publish.text
    li_post = execute_tool(
        "composio_execute_tool",
        {"tool_slug": "LINKEDIN_POST_UPDATE", "arguments_json": "{}"},
        ctx,
    )
    assert li_post.ok and "Composio action is ready" in li_post.text


def test_gmail_send_is_never_auto_executed_and_delete_requires_approval(
    monkeypatch,
) -> None:
    catalog = _Catalog()
    monkeypatch.setattr(
        ComposioCatalog, "from_settings", classmethod(lambda cls, settings: catalog)
    )
    ctx = _context()
    for slug in (
        "GMAIL_SEND_EMAIL",
        "GMAIL_SEND_DRAFT",
        "GMAIL_REPLY_TO_THREAD",
        "GMAIL_FORWARD_MESSAGE",
        "GOOGLE_ANALYTICS_SEND_EVENTS",
    ):
        result = execute_tool(
            "composio_execute_tool",
            {"tool_slug": slug, "arguments_json": "{}"},
            ctx,
        )
        assert not result.ok
        assert "never auto-executed" in result.error
        assert "destructive" not in result.error
    for slug in (
        "GMAIL_DELETE_THREAD",
        "GMAIL_DELETE_MESSAGE",
        "GMAIL_BATCH_DELETE_MESSAGES",
        "GMAIL_DELETE_DRAFT",
        "GOOGLE_SEARCH_CONSOLE_DELETE_SITE",
    ):
        denied = execute_tool(
            "composio_execute_tool",
            {"tool_slug": slug, "arguments_json": "{}"},
            ctx,
        )
        assert denied.ok and "destructive action is ready" in denied.text
    trash = execute_tool(
        "composio_execute_tool",
        {"tool_slug": "GMAIL_MOVE_TO_TRASH", "arguments_json": "{}"},
        ctx,
    )
    assert trash.ok and "Composio action is ready" in trash.text
    for slug in (
        "GOOGLE_SEARCH_CONSOLE_ADD_SITE",
        "GOOGLE_SEARCH_CONSOLE_SUBMIT_SITEMAP",
        "GOOGLE_ANALYTICS_ARCHIVE_CUSTOM_DIMENSION",
    ):
        write = execute_tool(
            "composio_execute_tool",
            {"tool_slug": slug, "arguments_json": "{}"},
            ctx,
        )
        assert write.ok and "Composio action is ready" in write.text


def test_linkedin_side_effect_is_bound_for_approval_and_never_executes_at_proposal_time() -> None:
    class Store:
        def __init__(self):
            self.saved = []
            self.row = None

        def upsert_linkedin_approval(self, **kwargs):
            self.saved.append(kwargs)
            self.row = SimpleNamespace(
                **kwargs,
                resource_type="linkedin_tool",
                approval_id="apr_linkedin_test",
            )

        def get_approval_by_resource(self, *_args):
            return self.row

        def get_approval_by_approval_id(self, _approval_id):
            return self.row

        def decide_linkedin_approval(self, *, resource_id, decision):
            assert resource_id == self.row.resource_id
            self.row.decision = decision
            return True

        def claim_operation(self, **_kwargs):
            return True

        def save_canonical_event(self, **_kwargs):
            pass

        def complete_operation(self, **_kwargs):
            pass

    class Catalog:
        def detail(self, slug):
            return CatalogTool(
                slug,
                "LINKEDIN",
                "post",
                {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
            )

    store = Store()
    result = propose_linkedin_write(
        store=store,
        channel=Channel.TELEGRAM,
        catalog=Catalog(),
        slug="LINKEDIN_POST_UPDATE",
        arguments={"text": "hello"},
        kill_switch=False,
    )
    assert result.startswith("LinkedIn action is ready")
    assert len(store.saved) == 1
    assert store.saved[0]["risk"] == "R4"
    approved = resolve_owner_callback_result(
        store, decision="approve", token="apr_linkedin_test"
    )
    assert approved.linkedin_resource_id_to_execute == store.row.resource_id
    replay = resolve_owner_callback_result(
        store, decision="approve", token="apr_linkedin_test"
    )
    assert replay.linkedin_resource_id_to_execute == store.row.resource_id


def test_linkedin_destructive_and_direct_message_tools_remain_denied() -> None:
    class Catalog:
        def detail(self, slug):
            return CatalogTool(
                slug,
                "LINKEDIN",
                "action",
                {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            )

    class Store:
        pass

    assert "Destructive" in propose_linkedin_write(
        store=Store(),
        channel=Channel.TELEGRAM,
        catalog=Catalog(),
        slug="LINKEDIN_DELETE_POST",
        arguments={},
        kill_switch=False,
    )
    assert "direct messages" in propose_linkedin_write(
        store=Store(),
        channel=Channel.TELEGRAM,
        catalog=Catalog(),
        slug="LINKEDIN_SEND_MESSAGE",
        arguments={},
        kill_switch=False,
    )


def test_linkedin_approval_accepts_a_practical_exact_post_payload_over_255_chars() -> None:
    class Store:
        def __init__(self):
            self.saved = []
            self.row = None

        def upsert_linkedin_approval(self, **kwargs):
            self.saved.append(kwargs)
            self.row = SimpleNamespace(
                **kwargs, resource_type="linkedin_tool", approval_id="apr_linkedin_long"
            )

        def get_approval_by_resource(self, *_args):
            return self.row

        def claim_operation(self, **_kwargs):
            return True

        def save_canonical_event(self, **_kwargs):
            pass

        def complete_operation(self, **_kwargs):
            pass

    class Catalog:
        def detail(self, slug):
            return CatalogTool(
                slug,
                "LINKEDIN",
                "post",
                {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
            )

    store = Store()
    result = propose_linkedin_write(
        store=store,
        channel=Channel.TELEGRAM,
        catalog=Catalog(),
        slug="LINKEDIN_POST_UPDATE",
        arguments={"text": "x" * 1_000},
        kill_switch=False,
    )
    assert result.startswith("LinkedIn action is ready")
    assert len(store.saved[0]["proposed_parameters"]) > 255
    assert json.loads(store.saved[0]["proposed_parameters"]) == {
        "arguments": {"text": "x" * 1_000},
        "slug": "LINKEDIN_POST_UPDATE",
    }


def test_linkedin_approval_rejects_payload_beyond_the_explicit_bound() -> None:
    class Catalog:
        def detail(self, slug):
            return CatalogTool(
                slug,
                "LINKEDIN",
                "post",
                {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
            )

    assert "too large" in propose_linkedin_write(
        store=SimpleNamespace(),
        channel=Channel.TELEGRAM,
        catalog=Catalog(),
        slug="LINKEDIN_POST_UPDATE",
        arguments={"text": "x" * MAX_LINKEDIN_APPROVAL_PARAMETERS_BYTES},
        kill_switch=False,
    )


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
