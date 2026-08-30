import inspect
import json

import httpx
import pytest
from app.api.inbound import process_inbound_texts
from app.capabilities.types import Principal
from app.core.config import Settings
from app.db.models import CanonicalEventRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.owner_tasks import OwnerTaskType, ack_for_owner_task, classify_owner_task
from app.domain.tools import AdapterHttpError, AdapterResponseError, AdapterSchemaError
from app.integrations.base import RecordingMessagePort
from app.integrations.linkedin import (
    COMPOSIO_GET_MY_INFO_TOOL,
    COMPOSIO_LINKEDIN_VERSION,
    ComposioLinkedInPort,
    DisabledLinkedInPort,
    FakeLinkedInPort,
    LinkedInPort,
    LinkedInProfile,
    build_linkedin_port,
    enrich_linkedin_ack,
    format_profile_line,
)
from app.integrations.research import DisabledResearchPort
from app.integrations.sheets import FakeSheetsPort
from sqlalchemy import select

from tests.unit.sales_copy import assert_discovery_reply

OWNER_LINKEDIN_PHONE = "972509990009"
PROSPECT_LINKEDIN_PHONE = "972509990010"
_OWNER = Principal.owner(source="test")

SAMPLE_PROFILE = LinkedInProfile(
    name="Assaf Web",
    headline="Growth & Sales Operator at AssafWeb",
)


def test_fake_returns_snapshot_disabled_returns_none() -> None:
    fake = FakeLinkedInPort(SAMPLE_PROFILE)
    disabled = DisabledLinkedInPort()
    assert fake.get_my_profile() == SAMPLE_PROFILE
    assert disabled.get_my_profile() is None


def test_enrich_linkedin_ack_fake_appends_headline_and_keeps_not_executed() -> None:
    decision = classify_owner_task("how's my linkedin")
    assert decision.task_type == OwnerTaskType.LINKEDIN
    ack = ack_for_owner_task(decision)
    enriched, _outcome = enrich_linkedin_ack(
        ack, FakeLinkedInPort(SAMPLE_PROFILE), kill_switch=False, principal=_OWNER
    )
    assert "לא ביצעתי" in enriched
    assert "לא אפרסם, לא אגיב ולא אשלח הודעות בלינקדאין" in enriched
    assert "פרופיל: Assaf Web — Growth & Sales Operator at AssafWeb." in enriched


def test_enrich_linkedin_ack_disabled_leaves_ack_unchanged() -> None:
    decision = classify_owner_task("how's my linkedin")
    ack = ack_for_owner_task(decision)
    enriched, _outcome = enrich_linkedin_ack(
        ack, DisabledLinkedInPort(), kill_switch=False, principal=_OWNER
    )
    assert enriched == ack
    assert "פרופיל:" not in enriched


def test_enrich_linkedin_ack_kill_switch_skips_port_call() -> None:
    class RaisingLinkedInPort:
        def get_my_profile(self) -> LinkedInProfile | None:
            raise RuntimeError("must not call port when kill switch is on")

    decision = classify_owner_task("how's my linkedin")
    ack = ack_for_owner_task(decision)
    enriched, outcome = enrich_linkedin_ack(
        ack, RaisingLinkedInPort(), kill_switch=True, principal=_OWNER
    )
    assert enriched == ack
    assert outcome.status == "denied"


def test_format_profile_line_omits_missing_fields() -> None:
    assert (
        format_profile_line(LinkedInProfile(headline="Builder"))
        == "פרופיל: Builder."
    )
    assert format_profile_line(LinkedInProfile(name="Assaf")) == "פרופיל: Assaf."
    assert format_profile_line(LinkedInProfile()) == ""


@pytest.mark.asyncio
async def test_owner_linkedin_fake_headline_in_sent_text() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        sheets = FakeSheetsPort()
        port = RecordingMessagePort()

        class ExplodingResearchPort:
            def search(self, query: str) -> list:
                del query
                raise RuntimeError("research must not run on linkedin owner path")

        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.owner.linkedin.1",
                    "from": OWNER_LINKEDIN_PHONE,
                    "text": "how's my linkedin",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_LINKEDIN_PHONE},
            sheets=sheets,
            linkedin=FakeLinkedInPort(SAMPLE_PROFILE),
            research=ExplodingResearchPort(),
        )
        db.commit()
        task = store.get_owner_task(
            provider="whatsapp", provider_event_id="evt.owner.linkedin.1"
        )
        assert task is not None
        assert task.task_type == "linkedin"
        assert task.status == "logged"
        assert sheets.rows == {}
        sent = port.sent[0].text
        assert "Growth & Sales Operator at AssafWeb" in sent
        assert "לא ביצעתי" in sent
        assert "לא אפרסם, לא אגיב ולא אשלח הודעות בלינקדאין" in sent
        assert "how the business works" not in sent
        assert "יום רגיל בעסק" not in sent
        tool_rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.provider_event_id
                    == "evt.owner.linkedin.1:tool:linkedin_profile"
                )
            )
        )
        assert len(tool_rows) == 1
        payload = json.loads(tool_rows[0].payload_json)
        assert payload == {
            "tool": "linkedin_profile",
            "status": "ok",
            "result_count": 1,
        }
        assert "Assaf" not in json.dumps(payload)
        assert "Growth" not in json.dumps(payload)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_prospect_audio_does_not_call_linkedin() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()

        class ExplodingLinkedInPort:
            def get_my_profile(self) -> LinkedInProfile | None:
                raise RuntimeError("linkedin must not run on prospect path")

        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.prospect.linkedin.nopath.1",
                    "from": PROSPECT_LINKEDIN_PHONE,
                    "text": "hi there",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            linkedin=ExplodingLinkedInPort(),
            research=DisabledResearchPort(),
        )
        db.commit()
        assert store.get_owner_task(
            provider="whatsapp", provider_event_id="evt.prospect.linkedin.nopath.1"
        ) is None
        assert len(port.sent) == 1
        assert_discovery_reply(port.sent[0].text)
        assert "פרופיל:" not in port.sent[0].text
    finally:
        db.close()


def test_protocol_has_no_create_post_delete_comment_dm_upload_methods() -> None:
    forbidden = ("create", "post", "delete", "comment", "dm", "upload")
    protocol_methods = {
        name
        for name, _ in inspect.getmembers(LinkedInPort, predicate=inspect.isfunction)
    }
    for name in protocol_methods:
        lowered = name.lower()
        assert not any(token in lowered for token in forbidden)

    for impl in (
        DisabledLinkedInPort(),
        FakeLinkedInPort(SAMPLE_PROFILE),
        ComposioLinkedInPort(api_key="cmp-test", user_id="user-123"),
    ):
        for name in dir(impl):
            if name.startswith("_"):
                continue
            lowered = name.lower()
            assert not any(token in lowered for token in forbidden)


def test_build_linkedin_port_live_when_both_credentials_set() -> None:
    settings = Settings(
        composio_api_key="cmp-live",
        composio_user_id="user-123",
    )
    port = build_linkedin_port(settings)
    assert isinstance(port, ComposioLinkedInPort)
    assert not isinstance(port, DisabledLinkedInPort)


@pytest.mark.parametrize(
    "api_key,user_id",
    [
        ("", ""),
        ("cmp-live", ""),
        ("", "user-123"),
        ("   ", "user-123"),
        ("cmp-live", "   "),
    ],
)
def test_build_linkedin_port_disabled_when_any_credential_missing(
    api_key: str,
    user_id: str,
) -> None:
    settings = Settings(composio_api_key=api_key, composio_user_id=user_id)
    port = build_linkedin_port(settings)
    assert isinstance(port, DisabledLinkedInPort)


def test_enrich_linkedin_ack_http_401_unauthorized_ack_unchanged() -> None:
    class HttpErrorLinkedInPort:
        def get_my_profile(self) -> LinkedInProfile | None:
            raise AdapterHttpError(401)

    decision = classify_owner_task("how's my linkedin")
    ack = ack_for_owner_task(decision)
    enriched, outcome = enrich_linkedin_ack(
        ack, HttpErrorLinkedInPort(), kill_switch=False, principal=_OWNER
    )
    assert enriched == ack
    assert outcome.status == "unauthorized"
    assert outcome.result_count == 0
    assert "פרופיל:" not in enriched


def test_composio_linkedin_port_http_500_raises_adapter_error() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(500))
    client = httpx.Client(transport=transport)
    port = ComposioLinkedInPort(
        api_key="cmp-test",
        user_id="user-123",
        client=client,
    )
    with pytest.raises(AdapterHttpError) as exc_info:
        port.get_my_profile()
    assert exc_info.value.status_code == 500


class _RaisingHttpClient:
    def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
        raise httpx.HTTPError("network error")


def test_composio_linkedin_port_network_error_raises_adapter_error() -> None:
    port = ComposioLinkedInPort(
        api_key="cmp-test",
        user_id="user-123",
        client=_RaisingHttpClient(),  # type: ignore[arg-type]
    )
    with pytest.raises(AdapterHttpError) as exc_info:
        port.get_my_profile()
    assert exc_info.value.status_code is None


def test_composio_linkedin_port_unsuccessful_response_is_not_reported_as_empty() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"data": {}, "error": "tool failed", "successful": False},
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioLinkedInPort(
        api_key="cmp-test",
        user_id="user-123",
        client=client,
    )
    with pytest.raises(AdapterResponseError) as exc_info:
        port.get_my_profile()
    assert exc_info.value.tool_status() == "error"


def test_composio_linkedin_port_schema_mismatch_is_not_reported_as_empty() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"data": ["not a profile object"], "successful": True},
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioLinkedInPort(
        api_key="cmp-test",
        user_id="user-123",
        client=client,
    )
    with pytest.raises(AdapterSchemaError) as exc_info:
        port.get_my_profile()
    assert exc_info.value.tool_status() == "malformed"


def test_composio_linkedin_port_request_shape() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": {"localizedFirstName": "Assaf", "localizedHeadline": "Builder"},
                "error": None,
                "successful": True,
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    port = ComposioLinkedInPort(
        api_key="cmp-test",
        user_id="user-abc",
        client=client,
    )
    port.get_my_profile()

    assert str(captured["url"]).endswith(f"/{COMPOSIO_GET_MY_INFO_TOOL}")
    body = captured["json"]
    assert isinstance(body, dict)
    assert body["user_id"] == "user-abc"
    assert body["version"] == COMPOSIO_LINKEDIN_VERSION
    arguments = body["arguments"]
    assert isinstance(arguments, dict)
    assert arguments == {}
    assert "text" not in body
    assert "text" not in arguments
    serialized = json.dumps(body)
    for forbidden in (
        "CREATE",
        "DELETE",
        "COMMENT",
        "UPLOAD",
        "POST",
    ):
        assert forbidden not in serialized.upper()


def test_composio_linkedin_port_maps_localized_name_and_headline() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "data": {
                    "localizedFirstName": "Assaf",
                    "localizedLastName": "Web",
                    "localizedHeadline": "Growth & Sales Operator at AssafWeb",
                },
                "error": None,
                "successful": True,
            },
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioLinkedInPort(
        api_key="cmp-test",
        user_id="user-123",
        client=client,
    )
    profile = port.get_my_profile()
    assert profile == LinkedInProfile(
        name="Assaf Web",
        headline="Growth & Sales Operator at AssafWeb",
    )


def test_composio_linkedin_port_maps_name_and_headline_fields() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "data": {
                    "name": "Assaf Web",
                    "headline": "Growth & Sales Operator at AssafWeb",
                },
                "error": None,
                "successful": True,
            },
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioLinkedInPort(
        api_key="cmp-test",
        user_id="user-123",
        client=client,
    )
    profile = port.get_my_profile()
    assert profile == SAMPLE_PROFILE


def test_composio_linkedin_port_empty_name_and_headline_returns_none() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "data": {"localizedFirstName": "", "localizedHeadline": "   "},
                "error": None,
                "successful": True,
            },
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioLinkedInPort(
        api_key="cmp-test",
        user_id="user-123",
        client=client,
    )
    assert port.get_my_profile() is None


def test_composio_linkedin_port_maps_nested_localized_objects() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "data": {
                    "firstName": {"localized": {"en_US": "Assaf"}},
                    "lastName": {"localized": {"en_US": "Web"}},
                    "headline": {"localized": {"en_US": "Builder"}},
                },
                "error": None,
                "successful": True,
            },
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioLinkedInPort(
        api_key="cmp-test",
        user_id="user-123",
        client=client,
    )
    profile = port.get_my_profile()
    assert profile == LinkedInProfile(name="Assaf Web", headline="Builder")
