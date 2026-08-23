import inspect
import json
from datetime import UTC, date, datetime

import httpx
import pytest
from app.api.inbound import process_inbound_texts
from app.core.config import Settings
from app.db.models import CanonicalEventRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.owner_tasks import OwnerTaskType, ack_for_owner_task, classify_owner_task
from app.domain.tools import AdapterHttpError
from app.integrations.base import RecordingMessagePort
from app.integrations.linkedin import FakeLinkedInPort, LinkedInProfile
from app.integrations.linkedin_analytics import (
    _MEMBER_CREATOR_POST_ANALYTICS_URL,
    LINKEDIN_API_VERSION,
    DirectLinkedInAnalyticsPort,
    DisabledLinkedInAnalyticsPort,
    FakeLinkedInAnalyticsPort,
    LinkedInAnalyticsPort,
    LinkedInAnalyticsSnapshot,
    build_linkedin_analytics_port,
    enrich_linkedin_analytics_ack,
    format_analytics_line,
    linkedin_analytics_date_range,
)
from app.integrations.research import DisabledResearchPort
from app.integrations.sheets import FakeSheetsPort
from sqlalchemy import select

OWNER_ANALYTICS_PHONE = "972509990011"
PROSPECT_ANALYTICS_PHONE = "972509990012"

SAMPLE_PROFILE = LinkedInProfile(
    name="Assaf Web",
    headline="Growth & Sales Operator at AssafWeb",
)

SAMPLE_SNAPSHOT = LinkedInAnalyticsSnapshot(
    impressions=100,
    members_reached=80,
    reactions=12,
    comments=3,
    reshares=2,
    link_clicks=5,
)

FRIDAY_JERUSALEM = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def test_linkedin_analytics_date_range_friday_jerusalem() -> None:
    result = linkedin_analytics_date_range(now=FRIDAY_JERUSALEM, timezone="Asia/Jerusalem")
    assert result == (date(2026, 7, 22), date(2026, 8, 21))


def test_linkedin_analytics_date_range_invalid_timezone() -> None:
    assert linkedin_analytics_date_range(now=FRIDAY_JERUSALEM, timezone="Not/AZone") is None


def test_linkedin_analytics_date_range_naive_now_uses_utc() -> None:
    naive = datetime(2026, 8, 21, 9, 0)
    result = linkedin_analytics_date_range(now=naive, timezone="Asia/Jerusalem")
    assert result == (date(2026, 7, 22), date(2026, 8, 21))


def test_fake_records_calls_disabled_returns_none() -> None:
    fake = FakeLinkedInAnalyticsPort(SAMPLE_SNAPSHOT)
    disabled = DisabledLinkedInAnalyticsPort()
    start = date(2026, 7, 22)
    end = date(2026, 8, 21)
    assert fake.get_member_analytics(start=start, end=end) == SAMPLE_SNAPSHOT
    assert fake.calls == [(start, end)]
    assert disabled.get_member_analytics(start=start, end=end) is None


def test_build_linkedin_analytics_port_disabled_even_when_token_set() -> None:
    settings = Settings(linkedin_access_token="li-member-token")
    port = build_linkedin_analytics_port(settings)
    assert isinstance(port, DisabledLinkedInAnalyticsPort)


@pytest.mark.parametrize("token", ["", "   "])
def test_build_linkedin_analytics_port_disabled_when_token_missing(token: str) -> None:
    settings = Settings(linkedin_access_token=token)
    port = build_linkedin_analytics_port(settings)
    assert isinstance(port, DisabledLinkedInAnalyticsPort)


def test_direct_port_request_shape_for_all_metrics() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            {
                "method": request.method,
                "url": str(request.url),
                "params": dict(request.url.params),
                "headers": dict(request.headers),
            }
        )
        return httpx.Response(
            200,
            json={"elements": [{"count": 1, "metricType": "REACTION"}]},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    port = DirectLinkedInAnalyticsPort(access_token="secret-token", client=client)
    start = date(2026, 7, 22)
    end = date(2026, 8, 21)
    port.get_member_analytics(start=start, end=end)

    assert len(captured) == 6
    query_types = [entry["params"]["queryType"] for entry in captured]
    assert query_types == [
        "IMPRESSION",
        "MEMBERS_REACHED",
        "REACTION",
        "COMMENT",
        "RESHARE",
        "LINK_CLICKS",
    ]
    for entry in captured:
        assert entry["method"] == "GET"
        url = str(entry["url"])
        assert url.startswith(_MEMBER_CREATOR_POST_ANALYTICS_URL)
        params = entry["params"]
        assert params["q"] == "me"
        assert params["aggregation"] == "TOTAL"
        assert params["dateRange"] == (
            "(start:(day:22,month:7,year:2026),end:(day:21,month:8,year:2026))"
        )
        headers = entry["headers"]
        assert headers["x-restli-protocol-version"] == "2.0.0"
        assert headers["linkedin-version"] == LINKEDIN_API_VERSION
        assert headers["content-type"] == "application/json"
        auth = headers["authorization"]
        assert auth.startswith("Bearer ")
        assert auth.endswith("secret-token")
        assert "secret-token" not in json.dumps(params)


def test_direct_port_maps_response_and_sums_multiple_elements() -> None:
    responses = {
        "IMPRESSION": {
            "elements": [
                {"count": 4, "metricType": "IMPRESSION"},
                {"count": 6, "metricType": "IMPRESSION"},
            ]
        },
        "MEMBERS_REACHED": {
            "elements": [{"count": 7, "metricType": "MEMBERS_REACHED"}]
        },
        "REACTION": {"elements": [{"count": 2, "metricType": "REACTION"}]},
        "COMMENT": {"elements": [{"count": 1, "metricType": "COMMENT"}]},
        "RESHARE": {"elements": [{"count": 0, "metricType": "RESHARE"}]},
        "LINK_CLICKS": {"elements": [{"count": 3, "metricType": "LINK_CLICKS"}]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        query_type = request.url.params["queryType"]
        return httpx.Response(200, json=responses[query_type])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    port = DirectLinkedInAnalyticsPort(access_token="token", client=client)
    snapshot = port.get_member_analytics(start=date(2026, 7, 22), end=date(2026, 8, 21))
    assert snapshot == LinkedInAnalyticsSnapshot(
        impressions=10,
        members_reached=7,
        reactions=2,
        comments=1,
        reshares=0,
        link_clicks=3,
    )


def test_direct_port_partial_metric_failure_leaves_field_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query_type = request.url.params["queryType"]
        if query_type == "IMPRESSION":
            return httpx.Response(500)
        return httpx.Response(
            200,
            json={"elements": [{"count": 5, "metricType": query_type}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    port = DirectLinkedInAnalyticsPort(access_token="token", client=client)
    snapshot = port.get_member_analytics(start=date(2026, 7, 22), end=date(2026, 8, 21))
    assert snapshot is not None
    assert snapshot.impressions is None
    assert snapshot.reactions == 5


def test_direct_port_malformed_json_and_schema_ignored() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query_type = request.url.params["queryType"]
        if query_type == "IMPRESSION":
            return httpx.Response(200, content=b"not-json")
        if query_type == "REACTION":
            return httpx.Response(
                200,
                json={"elements": [{"count": True, "metricType": query_type}]},
            )
        if query_type == "COMMENT":
            return httpx.Response(
                200,
                json={"elements": [{"count": -1, "metricType": query_type}]},
            )
        return httpx.Response(
            200,
            json={"elements": [{"count": 2, "metricType": query_type}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    port = DirectLinkedInAnalyticsPort(access_token="token", client=client)
    snapshot = port.get_member_analytics(start=date(2026, 7, 22), end=date(2026, 8, 21))
    assert snapshot == LinkedInAnalyticsSnapshot(
        members_reached=2,
        reshares=2,
        link_clicks=2,
    )


def test_direct_port_all_missing_raises_adapter_error() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(500))
    client = httpx.Client(transport=transport)
    port = DirectLinkedInAnalyticsPort(access_token="token", client=client)
    with pytest.raises(AdapterHttpError) as exc_info:
        port.get_member_analytics(start=date(2026, 7, 22), end=date(2026, 8, 21))
    assert exc_info.value.status_code == 500


def test_direct_port_mismatched_metric_type_is_ignored() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"elements": [{"count": 10, "metricType": "OTHER"}]},
        )
    )
    client = httpx.Client(transport=transport)
    port = DirectLinkedInAnalyticsPort(access_token="token", client=client)
    assert port.get_member_analytics(start=date(2026, 7, 22), end=date(2026, 8, 21)) is None


def test_direct_port_invalid_date_range_makes_no_request() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid range must not call LinkedIn")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    port = DirectLinkedInAnalyticsPort(access_token="token", client=client)
    assert port.get_member_analytics(start=date(2026, 8, 21), end=date(2026, 8, 21)) is None


@pytest.mark.parametrize("value", [-1, True])
def test_snapshot_rejects_invalid_metric_counts(value: object) -> None:
    with pytest.raises(ValueError):
        LinkedInAnalyticsSnapshot(impressions=value)


def test_direct_port_401_stops_and_raises_adapter_error() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if request.url.params["queryType"] == "MEMBERS_REACHED":
            return httpx.Response(403)
        return httpx.Response(200, json={"elements": [{"count": 1}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    port = DirectLinkedInAnalyticsPort(access_token="token", client=client)
    with pytest.raises(AdapterHttpError) as exc_info:
        port.get_member_analytics(start=date(2026, 7, 22), end=date(2026, 8, 21))
    assert exc_info.value.status_code == 403
    assert calls["count"] == 2


def test_enrich_http_403_unauthorized_ack_unchanged() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if request.url.params["queryType"] == "MEMBERS_REACHED":
            return httpx.Response(403)
        return httpx.Response(200, json={"elements": [{"count": 1}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    port = DirectLinkedInAnalyticsPort(access_token="token", client=client)
    decision = classify_owner_task("how's my linkedin")
    ack = ack_for_owner_task(decision)
    enriched, outcome = enrich_linkedin_analytics_ack(
        ack,
        port,
        kill_switch=False,
        now=FRIDAY_JERUSALEM,
        timezone="Asia/Jerusalem",
    )
    assert enriched == ack
    assert outcome.status == "unauthorized"
    assert outcome.result_count == 0
    assert calls["count"] == 2


def test_format_analytics_line_omits_missing_and_all_missing_empty() -> None:
    assert (
        format_analytics_line(
            LinkedInAnalyticsSnapshot(impressions=10, reactions=2)
        )
        == "ביצועי תוכן בשלושים הימים המלאים האחרונים: חשיפות 10, ריאקציות 2."
    )
    assert format_analytics_line(LinkedInAnalyticsSnapshot()) == ""


def test_format_analytics_line_has_no_url_id_or_token() -> None:
    line = format_analytics_line(SAMPLE_SNAPSHOT)
    assert "linkedin.com" not in line
    assert "assaf" not in line.lower()
    assert "token" not in line.lower()


def test_enrich_kill_switch_skips_port_call() -> None:
    class RaisingPort:
        def get_member_analytics(
            self, *, start: date, end: date
        ) -> LinkedInAnalyticsSnapshot | None:
            del start, end
            raise RuntimeError("must not call")

    decision = classify_owner_task("how's my linkedin")
    ack = ack_for_owner_task(decision)
    enriched, outcome = enrich_linkedin_analytics_ack(
        ack,
        RaisingPort(),
        kill_switch=True,
        now=FRIDAY_JERUSALEM,
        timezone="Asia/Jerusalem",
    )
    assert enriched == ack
    assert outcome.status == "denied"


def test_enrich_invalid_timezone_skips_port_call() -> None:
    class RaisingPort:
        def get_member_analytics(
            self, *, start: date, end: date
        ) -> LinkedInAnalyticsSnapshot | None:
            del start, end
            raise RuntimeError("must not call")

    decision = classify_owner_task("how's my linkedin")
    ack = ack_for_owner_task(decision)
    enriched, outcome = enrich_linkedin_analytics_ack(
        ack,
        RaisingPort(),
        kill_switch=False,
        now=FRIDAY_JERUSALEM,
        timezone="Bad/Zone",
    )
    assert enriched == ack
    assert outcome.status == "empty"


def test_enrich_fake_appends_stats_line() -> None:
    decision = classify_owner_task("how's my linkedin")
    ack = ack_for_owner_task(decision)
    enriched, outcome = enrich_linkedin_analytics_ack(
        ack,
        FakeLinkedInAnalyticsPort(SAMPLE_SNAPSHOT),
        kill_switch=False,
        now=FRIDAY_JERUSALEM,
        timezone="Asia/Jerusalem",
    )
    assert "ביצועי תוכן בשלושים הימים המלאים האחרונים" in enriched
    assert "חשיפות 100" in enriched
    assert outcome.status == "ok"
    assert outcome.result_count == 6


def test_enrich_partial_metrics_stamps_partial_status() -> None:
    decision = classify_owner_task("how's my linkedin")
    ack = ack_for_owner_task(decision)
    partial = LinkedInAnalyticsSnapshot(impressions=100, reactions=12)
    enriched, outcome = enrich_linkedin_analytics_ack(
        ack,
        FakeLinkedInAnalyticsPort(partial),
        kill_switch=False,
        now=FRIDAY_JERUSALEM,
        timezone="Asia/Jerusalem",
    )
    assert "חשיפות 100" in enriched
    assert outcome.status == "partial"
    assert outcome.result_count == 2


@pytest.mark.asyncio
async def test_owner_linkedin_persists_profile_and_analytics_tool_outcomes() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        sheets = FakeSheetsPort()
        port = RecordingMessagePort()
        analytics = FakeLinkedInAnalyticsPort(SAMPLE_SNAPSHOT)

        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.owner.linkedin.analytics.1",
                    "from": OWNER_ANALYTICS_PHONE,
                    "text": "how's my linkedin",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_ANALYTICS_PHONE},
            sheets=sheets,
            linkedin=FakeLinkedInPort(SAMPLE_PROFILE),
            linkedin_analytics=analytics,
            research=DisabledResearchPort(),
        )
        db.commit()

        task = store.get_owner_task(
            provider="whatsapp", provider_event_id="evt.owner.linkedin.analytics.1"
        )
        assert task is not None
        assert task.task_type == OwnerTaskType.LINKEDIN.value
        assert task.status == "logged"
        assert sheets.rows == {}
        assert len(analytics.calls) == 1
        start, end = analytics.calls[0]
        assert (end - start).days == 30

        sent = port.sent[0].text
        assert "Growth & Sales Operator at AssafWeb" in sent
        assert "חשיפות 100" in sent
        assert "linkedin.com" not in sent

        profile_row = db.scalars(
            select(CanonicalEventRow).where(
                CanonicalEventRow.provider_event_id
                == "evt.owner.linkedin.analytics.1:tool:linkedin_profile"
            )
        ).one()
        analytics_row = db.scalars(
            select(CanonicalEventRow).where(
                CanonicalEventRow.provider_event_id
                == "evt.owner.linkedin.analytics.1:tool:linkedin_analytics"
            )
        ).one()
        profile_payload = json.loads(profile_row.payload_json)
        analytics_payload = json.loads(analytics_row.payload_json)
        assert profile_payload == {
            "tool": "linkedin_profile",
            "status": "ok",
            "result_count": 1,
        }
        assert analytics_payload == {
            "tool": "linkedin_analytics",
            "status": "ok",
            "result_count": 6,
        }
        assert "100" not in json.dumps(analytics_payload)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_prospect_path_does_not_call_analytics() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()

        class ExplodingAnalyticsPort:
            def get_member_analytics(
                self, *, start: date, end: date
            ) -> LinkedInAnalyticsSnapshot | None:
                del start, end
                raise RuntimeError("analytics must not run on prospect path")

        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.prospect.linkedin.analytics.nopath.1",
                    "from": PROSPECT_ANALYTICS_PHONE,
                    "text": "hi there",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            linkedin_analytics=ExplodingAnalyticsPort(),
            research=DisabledResearchPort(),
        )
        db.commit()
        assert "ביצועי תוכן" not in port.sent[0].text
    finally:
        db.close()


def test_protocol_has_no_create_post_delete_comment_dm_upload_methods() -> None:
    forbidden = ("create", "post", "delete", "comment", "dm", "upload")
    protocol_methods = {
        name
        for name, _ in inspect.getmembers(LinkedInAnalyticsPort, predicate=inspect.isfunction)
    }
    for name in protocol_methods:
        lowered = name.lower()
        assert not any(token in lowered for token in forbidden)

    for impl in (
        DisabledLinkedInAnalyticsPort(),
        FakeLinkedInAnalyticsPort(SAMPLE_SNAPSHOT),
        DirectLinkedInAnalyticsPort(access_token="token"),
    ):
        for name in dir(impl):
            if name.startswith("_"):
                continue
            lowered = name.lower()
            assert not any(token in lowered for token in forbidden)
