import json

import httpx
from app.domain.owner.composio_effects import (
    COMPOSIO_EFFECT_COVERAGE,
    EffectRoute,
    composio_effect,
    snapshot_effect_target,
)
from app.integrations.composio_catalog import CatalogTool, ComposioCatalog


def test_coverage_manifest_routes_only_verified_write_shapes() -> None:
    by_slug = {item.slug: item for item in COMPOSIO_EFFECT_COVERAGE}

    assert by_slug["GMAIL_SEND_DRAFT"].route is EffectRoute.SNAPSHOT_WRITE
    assert by_slug["GMAIL_SEND_DRAFT"].reader_slug == "GMAIL_GET_DRAFT"
    assert by_slug["GMAIL_SEND_EMAIL"].route is EffectRoute.UNAVAILABLE
    assert by_slug["GMAIL_REPLY_TO_THREAD"].route is EffectRoute.UNAVAILABLE
    assert by_slug["GOOGLECALENDAR_PATCH_EVENT"].workflow == "calendar_reschedule"
    assert by_slug["GOOGLESHEETS_VALUES_UPDATE"].workflow == "sheets_update"
    assert by_slug["GOOGLESHEETS_UPSERT_ROWS"].workflow == "crm_upsert"
    assert by_slug["WHATSAPP_SEND_MESSAGE"].route is EffectRoute.DENIED
    assert composio_effect("SLACK_CREATE_CHANNEL", "SLACK").route is EffectRoute.GENERIC_CREATE
    assert composio_effect("SLACK_ARCHIVE_CHANNEL", "SLACK").route is EffectRoute.UNAVAILABLE


def test_unbound_linkedin_profile_editor_is_unavailable() -> None:
    assert composio_effect("LINKEDIN_UPDATE_PROFILE", "LINKEDIN").route is EffectRoute.UNAVAILABLE
    assert composio_effect("LINKEDIN_POST_UPDATE", "LINKEDIN").route is EffectRoute.UNAVAILABLE
    assert composio_effect("LINKEDIN_SEND_MESSAGE", "LINKEDIN").route is EffectRoute.DENIED


def test_snapshot_reader_is_pinned_and_target_hash_changes_with_provider_state() -> None:
    class Catalog:
        state = {"id": "draft_1", "message": {"to": "first@example.com"}}

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict, str | None]] = []

        def detail(self, slug: str) -> CatalogTool | None:
            if slug != "GMAIL_GET_DRAFT":
                return None
            return CatalogTool(slug, "GMAIL", "Get Draft", {"type": "object"})

        def execute_read(
            self,
            tool: CatalogTool,
            arguments: dict,
            *,
            connected_account_id: str | None = None,
        ) -> dict:
            self.calls.append((tool.slug, arguments, connected_account_id))
            return {"successful": True, "data": self.state}

    catalog = Catalog()
    effect = composio_effect("GMAIL_SEND_DRAFT", "GMAIL")
    first = snapshot_effect_target(
        catalog, effect, {"draft_id": "draft_1"}, connected_account_id="ca_owner"
    )
    catalog.state = {"id": "draft_1", "message": {"to": "changed@example.com"}}
    second = snapshot_effect_target(
        catalog, effect, {"draft_id": "draft_1"}, connected_account_id="ca_owner"
    )

    assert first is not None and second is not None
    assert first["state_hash"] != second["state_hash"]
    assert first["identity"] == {"draft_id": "draft_1"}
    assert catalog.calls == [
        ("GMAIL_GET_DRAFT", {"draft_id": "draft_1"}, "ca_owner"),
        ("GMAIL_GET_DRAFT", {"draft_id": "draft_1"}, "ca_owner"),
    ]


def test_snapshot_reader_rejects_empty_and_mismatched_draft_targets() -> None:
    class Catalog:
        data: object = None

        def detail(self, slug: str) -> CatalogTool:
            return CatalogTool(slug, "GMAIL", "Get Draft", {"type": "object"})

        def execute_read(self, *_args, **_kwargs) -> dict:
            return {"successful": True, "data": self.data}

    catalog = Catalog()
    effect = composio_effect("GMAIL_SEND_DRAFT", "GMAIL")

    assert (
        snapshot_effect_target(
            catalog, effect, {"draft_id": "draft_1"}, connected_account_id="ca_owner"
        )
        is None
    )
    catalog.data = {}
    assert (
        snapshot_effect_target(
            catalog, effect, {"draft_id": "draft_1"}, connected_account_id="ca_owner"
        )
        is None
    )
    catalog.data = {"id": "somebody_elses_draft"}
    assert (
        snapshot_effect_target(
            catalog, effect, {"draft_id": "draft_1"}, connected_account_id="ca_owner"
        )
        is None
    )


def test_catalog_read_can_pin_the_connected_account() -> None:
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"successful": True, "data": {"id": "draft_1"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    catalog = ComposioCatalog(api_key="key", user_id="owner", client=client)
    tool = CatalogTool("GMAIL_GET_DRAFT", "GMAIL", "Get Draft", {"type": "object"})

    assert (
        catalog.execute_read(
            tool,
            {"draft_id": "draft_1"},
            connected_account_id="ca_owner",
        )
        is not None
    )
    assert payloads == [
        {
            "user_id": "owner",
            "arguments": {"draft_id": "draft_1"},
            "connected_account_id": "ca_owner",
        }
    ]
    client.close()
