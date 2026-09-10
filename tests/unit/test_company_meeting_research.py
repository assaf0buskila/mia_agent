import json

import pytest
from app.db.models import CanonicalEventRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.company import extract_explicit_company_domain, sanitize_company_domain
from app.domain.events import Channel, EventType, build_qualification_updated_event
from app.domain.meetings.briefs import apply_meeting_brief_policy
from app.domain.sales import (
    FitLevel,
    NextAction,
    SalesState,
)
from app.integrations.research import FakeResearchPort, ResearchSnippet
from sqlalchemy import select

PROSPECT_PHONE = "972509994021"
PROSPECT_PHONE_2 = "972509994022"

_MEETING_BRIEF_PAYLOAD_KEYS = frozenset(
    {
        "channel",
        "fit",
        "pain_level",
        "workflow_known",
        "impact_confirmed",
        "reflected",
        "hypothesis_offered",
        "buying_reality_known",
        "authority_known",
        "timeline_known",
        "metric_known",
        "willingness_to_meet",
        "owner_required",
        "active_objection",
        "missing_fields",
        "owner_questions",
        "next_action",
    }
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://www.Example.co.il/path?q=1", "example.co.il"),
        ("example.co.il", "example.co.il"),
        ("www.shop.example.com.", "shop.example.com"),
        ("HTTPS://BUSINESS.CO.IL", "business.co.il"),
    ],
)
def test_sanitize_company_domain_accepts_valid(value: str, expected: str) -> None:
    assert sanitize_company_domain(value) == expected


def test_sanitize_company_domain_idna_when_supported() -> None:
    result = sanitize_company_domain("https://xn--4dbrk0ce.co.il")
    assert result == "xn--4dbrk0ce.co.il"


@pytest.mark.parametrize(
    "value",
    [
        "localhost",
        "local",
        "127.0.0.1",
        "::1",
        "192.168.1.1",
        "user:pass@example.com",
        "http://user@example.com",
        "ftp://example.com",
        "javascript:alert(1)",
        "mailto:user@example.com",
        "user@example.com",
        "example..com",
        "-bad.com",
        "bad-.com",
        "has secret.example.com",
        "token.example.com",
        "password.example.com",
        "example.com:8080",
        "http://example.com:443",
        "http://example.com:notaport",
        "singlelabel",
        "",
        "   ",
        "a" * 64 + ".com",
    ],
)
def test_sanitize_company_domain_rejects_invalid(value: str) -> None:
    assert sanitize_company_domain(value) is None


def test_extract_whole_domain_reply() -> None:
    assert extract_explicit_company_domain("example.co.il") == "example.co.il"
    assert extract_explicit_company_domain("https://www.shop.co.il/") == "shop.co.il"
    assert extract_explicit_company_domain("example.co.il.") == "example.co.il"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("our website is https://clinic.co.il", "clinic.co.il"),
        ("Website is shop.example.com thanks", "shop.example.com"),
        ("Our site is business.co.il", "business.co.il"),
        ("company website demo.co.il please", "demo.co.il"),
        ("business website https://a.co.il/x", "a.co.il"),
        ("האתר שלנו clinic.co.il", "clinic.co.il"),
        ("אתר העסק https://biz.co.il", "biz.co.il"),
        ("הדומיין שלנו shop.co.il", "shop.co.il"),
        ("האתר הוא example.co.il", "example.co.il"),
    ],
)
def test_extract_explicit_markers(message: str, expected: str) -> None:
    assert extract_explicit_company_domain(message) == expected


def test_extract_ignores_embedded_url_without_marker() -> None:
    assert extract_explicit_company_domain("see https://hidden.co.il for info") is None
    assert extract_explicit_company_domain("notwebsite is hidden.co.il") is None


def test_sales_state_company_domain_db_roundtrip() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_domain_roundtrip_1"
        )
        sales = store.get_sales(lead_id)
        sales.company_domain = "clinic.co.il"
        store.save_sales(sales)
        db.commit()
        reloaded = store.get_sales(lead_id)
        assert reloaded.company_domain == "clinic.co.il"
    finally:
        db.close()


def _ready_sales(lead_id: str, *, domain: str = "") -> SalesState:
    return SalesState(
        lead_id=lead_id,
        fit=FitLevel.GOOD,
        workflow_known=True,
        impact_confirmed=True,
        reflected=True,
        hypothesis_offered=True,
        buying_reality_known=True,
        willingness_to_meet=True,
        company_domain=domain,
    )


def test_policy_no_domain_no_research_call_or_outcome() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_no_domain_policy_1"
        )
        sales = _ready_sales(lead_id)
        store.save_sales(sales)
        port = FakeResearchPort([ResearchSnippet(title="T", url="https://x.com", excerpt="e")])
        outcome = apply_meeting_brief_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.OFFER_MEETING.value,
            sales=sales,
            kill_switch=False,
            research_port=port,
        )
        db.commit()
        assert outcome is None
        assert port.last_query is None
        row = store.get_meeting_brief(lead_id)
        assert row is not None
        payload = json.loads(row.payload_json)
        assert set(payload.keys()) == _MEETING_BRIEF_PAYLOAD_KEYS
    finally:
        db.close()


def test_policy_revalidates_direct_sales_domain_before_search() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_invalid_direct_domain_1"
        )
        sales = _ready_sales(lead_id, domain="unsafe value")
        port = FakeResearchPort([ResearchSnippet(title="T", url="https://safe.co.il", excerpt="e")])
        outcome = apply_meeting_brief_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.OFFER_MEETING.value,
            sales=sales,
            kill_switch=False,
            research_port=port,
        )
        db.commit()
        assert outcome is None
        assert port.last_query is None
        payload = json.loads(store.get_meeting_brief(lead_id).payload_json)
        assert "company_domain" not in payload
    finally:
        db.close()


def test_policy_domain_and_fake_port_researches_and_partitions_event() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_domain_research_1"
        )
        sales = _ready_sales(lead_id, domain="clinic.co.il")
        store.save_sales(sales)
        port = FakeResearchPort(
            [
                ResearchSnippet(
                    title="Clinic Home",
                    url="https://www.clinic.co.il/about",
                    excerpt="secret path",
                ),
                ResearchSnippet(
                    title="Clinic Services",
                    url="https://services.clinic.co.il/page",
                    excerpt="more",
                ),
            ]
        )
        outcome = apply_meeting_brief_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.OFFER_MEETING.value,
            sales=sales,
            kill_switch=False,
            research_port=port,
        )
        db.commit()
        assert outcome is not None
        assert outcome.tool == "meeting_research"
        assert outcome.status == "ok"
        assert outcome.result_count == 2
        assert port.last_query == "clinic.co.il"
        row = store.get_meeting_brief(lead_id)
        payload = json.loads(row.payload_json)
        assert payload["company_domain"] == "clinic.co.il"
        assert payload["research_attempted"] is True
        assert len(payload["research_sources"]) == 2
        assert payload["research_sources"][0] == {
            "title": "Clinic Home",
            "host": "clinic.co.il",
        }
        serialized = row.payload_json.lower()
        assert "http" not in serialized
        assert "excerpt" not in serialized
        assert "secret" not in serialized
        events = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.lead_id == lead_id,
                    CanonicalEventRow.event_type == EventType.MEETING_BRIEF.value,
                )
            )
        )
        assert len(events) == 1
        event_payload = json.loads(events[0].payload_json)
        assert set(event_payload.keys()) <= _MEETING_BRIEF_PAYLOAD_KEYS
        for forbidden in ("company_domain", "research_attempted", "research_sources"):
            assert forbidden not in event_payload
    finally:
        db.close()


def test_second_offer_preserves_sources_without_research_recall() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_research_cache_1"
        )
        sales = _ready_sales(lead_id, domain="clinic.co.il")
        store.save_sales(sales)
        port = FakeResearchPort([ResearchSnippet(title="One", url="https://a.co.il", excerpt="x")])
        apply_meeting_brief_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.OFFER_MEETING.value,
            sales=sales,
            kill_switch=False,
            research_port=port,
        )
        db.commit()
        first_query = port.last_query
        apply_meeting_brief_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.OFFER_MEETING.value,
            sales=sales,
            kill_switch=False,
            research_port=port,
        )
        db.commit()
        assert port.last_query == first_query
        payload = json.loads(store.get_meeting_brief(lead_id).payload_json)
        assert payload["research_attempted"] is True
        assert payload["research_sources"][0]["title"] == "One"
    finally:
        db.close()


def test_second_offer_revalidates_cached_research_sources() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_research_cache_sanitize_1"
        )
        sales = _ready_sales(lead_id, domain="clinic.co.il")
        store.save_sales(sales)
        port = FakeResearchPort([])
        apply_meeting_brief_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.OFFER_MEETING.value,
            sales=sales,
            kill_switch=False,
            research_port=port,
        )
        row = store.get_meeting_brief(lead_id)
        row.payload_json = json.dumps(
            {
                **json.loads(row.payload_json),
                "research_sources": [
                    {"title": " Good\nTitle ", "host": "WWW.Good.co.il"},
                    {"title": "Bad", "host": "bad.example.com:443"},
                ],
            }
        )
        db.commit()
        apply_meeting_brief_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.OFFER_MEETING.value,
            sales=sales,
            kill_switch=False,
            research_port=port,
        )
        db.commit()
        payload = json.loads(store.get_meeting_brief(lead_id).payload_json)
        assert payload["research_sources"] == [{"title": "Good Title", "host": "good.co.il"}]
    finally:
        db.close()


def test_policy_empty_research_still_marks_attempted() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_research_empty_1"
        )
        sales = _ready_sales(lead_id, domain="empty.co.il")
        store.save_sales(sales)
        port = FakeResearchPort([])
        outcome = apply_meeting_brief_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.OFFER_MEETING.value,
            sales=sales,
            kill_switch=False,
            research_port=port,
        )
        db.commit()
        assert outcome is not None
        assert outcome.status == "empty"
        payload = json.loads(store.get_meeting_brief(lead_id).payload_json)
        assert payload["research_attempted"] is True
        assert payload["research_sources"] == []
    finally:
        db.close()


def test_policy_error_still_persists_base_brief(monkeypatch) -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_research_error_1"
        )
        sales = _ready_sales(lead_id, domain="err.co.il")
        store.save_sales(sales)

        class ExplodingPort(FakeResearchPort):
            def search(self, query: str) -> list[ResearchSnippet]:
                raise RuntimeError("boom")

        outcome = apply_meeting_brief_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.OFFER_MEETING.value,
            sales=sales,
            kill_switch=False,
            research_port=ExplodingPort(),
        )
        db.commit()
        assert outcome is not None
        assert outcome.status == "error"
        assert store.get_meeting_brief(lead_id) is not None
    finally:
        db.close()


def test_kill_switch_skips_brief_and_denies_research(monkeypatch) -> None:
    monkeypatch.setenv("MIA_KILL_SWITCH", "true")
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_kill_research_1"
        )
        sales = _ready_sales(lead_id, domain="kill.co.il")
        store.save_sales(sales)
        port = FakeResearchPort([ResearchSnippet(title="T", url="https://kill.co.il", excerpt="e")])
        outcome = apply_meeting_brief_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.OFFER_MEETING.value,
            sales=sales,
            kill_switch=True,
            research_port=port,
        )
        db.commit()
        assert outcome is not None
        assert outcome.status == "denied"
        assert store.get_meeting_brief(lead_id) is None
        assert port.last_query is None
    finally:
        db.close()


def test_qualification_event_excludes_company_domain() -> None:
    event = build_qualification_updated_event(
        provider="website",
        channel=Channel.WEBSITE,
        run_id="run_1",
        lead_id="lead_1",
        conversation_id="sess",
        payload={
            "fit": "good",
            "pain_level": 3,
            "workflow_known": True,
            "impact_confirmed": True,
            "reflected": True,
            "hypothesis_offered": True,
            "buying_reality_known": True,
            "authority_known": True,
            "timeline_known": True,
            "metric_known": True,
            "missing_fields": [],
            "willingness_to_meet": True,
            "owner_required": False,
            "active_objection": None,
            "company_domain": "evil.co.il",
        },
    )
    assert "company_domain" not in event.payload
