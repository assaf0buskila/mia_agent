"""V2 policy regressions for prohibited Meta Ads mutations."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from app.capabilities.types import Principal
from app.core.config import Settings
from app.domain.approvals import ACTION_COMPOSIO_WRITE, DECISION_APPROVED, approval_expires_at
from app.domain.events import Channel
from app.domain.owner.composio_effects import (
    EffectRoute,
    composio_effect,
    is_prohibited_meta_ads_mutation,
)
from app.domain.owner.composio_writes import (
    _digest,
    _parameters,
    composio_approval_resource_id,
    execute_approved_composio_write,
)
from app.integrations.composio_catalog import CatalogTool, ComposioCatalog
from app.tools.owner.types import ToolContext
from app.tools.registries.owner_tools import execute_tool


@pytest.mark.parametrize(
    ("slug", "toolkit"),
    [
        ("FACEBOOKADS_CREATE_CAMPAIGN", "FACEBOOKADS"),
        ("METAADS_CREATE_CAMPAIGN", "METAADS"),
        ("METAADS_SET_BID", "META_ADS"),
        ("FACEBOOKADS_PAUSE_CAMPAIGN", "FACEBOOK_ADS"),
        ("META_ADS_CREATE_CAMPAIGN", ""),
        ("LAUNCH_CAMPAIGN", "METAADS"),
    ],
)
def test_meta_ads_mutations_are_denied_by_shared_effect_registry(slug: str, toolkit: str) -> None:
    assert is_prohibited_meta_ads_mutation(slug, toolkit)
    assert composio_effect(slug, toolkit).route is EffectRoute.DENIED


def test_meta_ads_read_alias_does_not_get_denied_as_a_mutation() -> None:
    assert not is_prohibited_meta_ads_mutation("METAADS_GET_CAMPAIGNS", "METAADS")
    assert composio_effect("METAADS_GET_CAMPAIGNS", "METAADS").route is EffectRoute.UNAVAILABLE


class _Catalog:
    def __init__(self, slug: str, toolkit: str) -> None:
        self.tool = CatalogTool(
            slug,
            toolkit,
            "Meta Ads mutation",
            {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        )
        self.execute_calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def detail(self, slug: str) -> CatalogTool | None:
        return self.tool if slug == self.tool.slug else None

    def execute(self, tool: CatalogTool, arguments: dict, **_kwargs):
        self.execute_calls.append({"slug": tool.slug, "arguments": arguments})
        return {"successful": True, "data": {}}


class _Store:
    def __init__(self, row=None) -> None:
        self.row = row
        self.saved: list[dict] = []
        self.claim_calls = 0

    def upsert_composio_approval(self, **kwargs):
        self.saved.append(kwargs)

    def get_approval_by_resource(self, *_args):
        return self.row

    def claim_operation(self, **_kwargs):
        return True

    def save_canonical_event(self, **_kwargs):
        pass

    def complete_operation(self, **_kwargs):
        pass

    def claim_provider_write(self, **_kwargs):
        self.claim_calls += 1
        return True


def _context(store: _Store) -> ToolContext:
    return ToolContext(
        store=store,
        brain=None,
        settings=SimpleNamespace(composio_user_id="owner", memory_write_enabled=True),
        principal=Principal.owner(source="telegram", actor_id="1"),
        embedding_port=None,
        kill_switch=False,
    )


def test_v2_proposal_denies_both_meta_aliases_before_any_provider_write(monkeypatch) -> None:
    for slug, toolkit in (
        ("FACEBOOKADS_CREATE_CAMPAIGN", "FACEBOOKADS"),
        ("METAADS_CREATE_CAMPAIGN", "METAADS"),
    ):
        catalog = _Catalog(slug, toolkit)
        store = _Store()
        monkeypatch.setattr(
            ComposioCatalog,
            "from_settings",
            classmethod(lambda _cls, _settings, catalog=catalog: catalog),
        )
        result = execute_tool(
            "composio_propose_action",
            {"tool_slug": slug, "arguments_json": json.dumps({})},
            _context(store),
        )
        assert not result.ok
        assert "Meta Ads" in result.error
        assert store.saved == []
        assert catalog.execute_calls == []


def test_previously_approved_meta_alias_is_rejected_before_provider_mutation(monkeypatch) -> None:
    slug = "METAADS_CREATE_CAMPAIGN"
    arguments: dict = {}
    parameters = _parameters(slug, arguments)
    resource_id = composio_approval_resource_id(slug, arguments)
    row = SimpleNamespace(
        channel=Channel.TELEGRAM.value,
        action=ACTION_COMPOSIO_WRITE,
        risk="R4",
        payload_hash=_digest(
            channel=Channel.TELEGRAM.value,
            resource_id=resource_id,
            risk="R4",
            parameters=parameters,
        ),
        decision=DECISION_APPROVED,
        resource_id=resource_id,
        expires_at=approval_expires_at(now=datetime.now(UTC)),
        proposed_parameters=parameters,
    )
    store = _Store(row)
    catalog = _Catalog(slug, "METAADS")
    monkeypatch.setattr(
        ComposioCatalog,
        "from_settings",
        classmethod(lambda _cls, _settings: catalog),
    )

    result = execute_approved_composio_write(
        store=store,
        settings=Settings(_env_file=None),
        resource_id=resource_id,
        kill_switch=False,
    )

    assert "nothing was executed" in result.lower()
    assert catalog.execute_calls == []
    assert store.claim_calls == 0
