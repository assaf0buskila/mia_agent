import json
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

import pytest
from app.api.inbound import process_inbound_texts
from app.db.models import ChannelIdentityRow, HandoffTokenRow, IdentityLinkRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.handoff import (
    HANDOFF_COMPOSE_HINT_HE,
    click_to_chat_digits,
    click_to_chat_url,
    compose_handoff_text,
    extract_handoff_token,
    generate_handoff_token,
    hash_handoff_token,
    inbound_text_without_token,
)
from app.domain.identity import REASON_HANDOFF_TOKEN, persist_verified_identity_link
from app.domain.sales import SalesState
from app.integrations.base import RecordingMessagePort
from app.integrations.sheets import FakeSheetsPort
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import select

WEB_PHONE = "972509993001"
OTHER_PHONE = "972509993002"
OWNER_PHONE = "972509993003"
STATE_PHONE = "972509993011"
EXPIRED_PHONE = "972509993012"
REUSE_PHONE = "972509993013"
CLICK_CHAT = "972500000001"
LINK_PHONE = "972509996001"
LINK_NOP_PHONE = "972509996002"
CONFLICT_LINK_PHONE = "972509996003"


def test_generate_token_prefix_and_hash_is_sha256_hex() -> None:
    raw = generate_handoff_token()
    assert raw.startswith("mia1_")
    assert len(raw) >= 21
    digest = hash_handoff_token(raw)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_extract_token_exact_and_with_rest() -> None:
    raw = generate_handoff_token()
    exact = extract_handoff_token(raw)
    assert exact == (raw, "")
    with_rest = extract_handoff_token(f"  {raw}  hello there  ")
    assert with_rest == (raw, "hello there")
    assert extract_handoff_token("not a token") is None
    assert extract_handoff_token("mia1_short") is None
    assert inbound_text_without_token(raw) == "[website handoff]"
    assert inbound_text_without_token(f"{raw} hello") == "hello"
    assert inbound_text_without_token("plain") == "plain"


def test_extract_rejoins_whatsapp_wrapped_token() -> None:
    raw = "mia1_qkc4VDRLioNkR_3Wbx8kSKoe"
    wrapped = "mia1_qkc4VDRLioNkR_3Wbx8kS\nKoe"
    assert extract_handoff_token(wrapped) == (raw, "")
    assert inbound_text_without_token(wrapped) == "[website handoff]"
    with_hint = f"{wrapped}\n{HANDOFF_COMPOSE_HINT_HE}"
    assert extract_handoff_token(with_hint) == (raw, HANDOFF_COMPOSE_HINT_HE)
    assert extract_handoff_token(f"{raw} Hi") == (raw, "Hi")
    assert extract_handoff_token(f"{raw}\n{HANDOFF_COMPOSE_HINT_HE}") == (
        raw,
        HANDOFF_COMPOSE_HINT_HE,
    )


def test_compose_handoff_text_is_human_and_omits_the_token() -> None:
    raw = generate_handoff_token()
    composed = compose_handoff_text(raw)
    assert composed == HANDOFF_COMPOSE_HINT_HE
    assert raw not in composed
    assert extract_handoff_token(composed) is None


def test_click_to_chat_digits_reject_non_phone() -> None:
    assert click_to_chat_digits("972500000001") == "972500000001"
    assert click_to_chat_digits("+972 500000001") == "972500000001"
    assert click_to_chat_digits("https://evil.example") == ""
    assert click_to_chat_digits("97250abc") == ""


def test_click_to_chat_url_is_https_wa_me_or_empty() -> None:
    url = click_to_chat_url(CLICK_CHAT)
    assert url.startswith("https://wa.me/")
    parsed_host = url.split("/")[2]
    assert parsed_host == "wa.me"
    assert click_to_chat_url("") == ""
    assert click_to_chat_url("https://evil.example") == ""
    assert click_to_chat_url("javascript:alert(1)") == ""


@pytest.mark.asyncio
async def test_handoff_issue_consume_same_lead_redacts_token() -> None:
    init_db()
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        session_id = created.json()["session_id"]
        website_lead_id = created.json()["lead_id"]
        handoff = client.post(f"/v1/website/sessions/{session_id}/handoff")
        assert handoff.status_code == 200
        token = handoff.json()["token"]
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        event_id = "evt.handoff.consume.1"
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{"id": event_id, "from": WEB_PHONE, "text": token}],
            store=store,
            port=port,
            kill_switch=False,
            sheets=FakeSheetsPort(),
        )
        db.commit()
        in_row = store.get_canonical_event(provider="whatsapp", provider_event_id=event_id)
        assert in_row is not None
        assert in_row.lead_id == website_lead_id
        payload = json.loads(in_row.payload_json)
        assert payload["text"] == "[website handoff]"
        assert token not in in_row.payload_json
        assert token not in in_row.source_json
        hash_row = db.scalars(
            select(HandoffTokenRow).where(HandoffTokenRow.lead_id == website_lead_id)
        ).first()
        assert hash_row is not None
        assert hash_row.consumed_at is not None
        assert hash_row.token_hash == hash_handoff_token(token)
        assert hash_row.token_hash != token
        wa_identity = db.scalars(
            select(ChannelIdentityRow).where(ChannelIdentityRow.external_id == WEB_PHONE)
        ).one()
        web_identity = db.scalars(
            select(ChannelIdentityRow).where(ChannelIdentityRow.external_id == session_id)
        ).one()
        assert wa_identity.customer_id == web_identity.customer_id
    finally:
        db.close()


@pytest.mark.asyncio
async def test_handoff_preserves_website_sales_state() -> None:
    init_db()
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        session_id = created.json()["session_id"]
        website_lead_id = created.json()["lead_id"]
        handoff = client.post(f"/v1/website/sessions/{session_id}/handoff")
        token = handoff.json()["token"]
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        store.save_sales(SalesState(lead_id=website_lead_id, workflow_known=True))
        db.commit()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.handoff.state.1",
                    "from": STATE_PHONE,
                    "text": f"{token} ok",
                }
            ],
            store=store,
            port=RecordingMessagePort(),
            kill_switch=False,
            sheets=FakeSheetsPort(),
        )
        db.commit()
        sales = store.get_sales(website_lead_id)
        assert sales.workflow_known is True
        in_row = store.get_canonical_event(
            provider="whatsapp", provider_event_id="evt.handoff.state.1"
        )
        assert in_row is not None
        assert in_row.lead_id == website_lead_id
        assert json.loads(in_row.payload_json)["text"] == "ok"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_expired_token_creates_different_lead() -> None:
    init_db()
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        session_id = created.json()["session_id"]
        website_lead_id = created.json()["lead_id"]
        handoff = client.post(f"/v1/website/sessions/{session_id}/handoff")
        token = handoff.json()["token"]
    db = get_session_factory()()
    try:
        row = db.scalars(
            select(HandoffTokenRow).where(HandoffTokenRow.lead_id == website_lead_id)
        ).one()
        row.expires_at = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        db.commit()
        store = LeadStore(db)
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{"id": "evt.handoff.expired.1", "from": EXPIRED_PHONE, "text": token}],
            store=store,
            port=RecordingMessagePort(),
            kill_switch=False,
            sheets=FakeSheetsPort(),
        )
        db.commit()
        in_row = store.get_canonical_event(
            provider="whatsapp", provider_event_id="evt.handoff.expired.1"
        )
        assert in_row is not None
        assert in_row.lead_id != website_lead_id
        assert json.loads(in_row.payload_json)["text"] == "[website handoff]"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_consumed_token_second_inbound_does_not_reuse() -> None:
    init_db()
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        session_id = created.json()["session_id"]
        website_lead_id = created.json()["lead_id"]
        handoff = client.post(f"/v1/website/sessions/{session_id}/handoff")
        token = handoff.json()["token"]
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{"id": "evt.handoff.first.1", "from": REUSE_PHONE, "text": token}],
            store=store,
            port=port,
            kill_switch=False,
            sheets=FakeSheetsPort(),
        )
        db.commit()
        first_lead = store.get_canonical_event(
            provider="whatsapp", provider_event_id="evt.handoff.first.1"
        )
        assert first_lead is not None
        assert first_lead.lead_id == website_lead_id
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{"id": "evt.handoff.second.1", "from": REUSE_PHONE, "text": token}],
            store=store,
            port=port,
            kill_switch=False,
            sheets=FakeSheetsPort(),
        )
        db.commit()
        second_lead = store.get_canonical_event(
            provider="whatsapp", provider_event_id="evt.handoff.second.1"
        )
        assert second_lead is not None
        assert second_lead.lead_id == website_lead_id
        hash_row = db.scalars(
            select(HandoffTokenRow).where(HandoffTokenRow.token_hash == hash_handoff_token(token))
        ).one()
        assert hash_row.consumed_at is not None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_phone_bound_to_other_customer_consume_fails_no_merge() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        other_cust, _other_lead = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id=OTHER_PHONE
        )
        db.commit()
    finally:
        db.close()
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        session_id = created.json()["session_id"]
        website_lead_id = created.json()["lead_id"]
        handoff = client.post(f"/v1/website/sessions/{session_id}/handoff")
        token = handoff.json()["token"]
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{"id": "evt.handoff.conflict.1", "from": OTHER_PHONE, "text": token}],
            store=store,
            port=RecordingMessagePort(),
            kill_switch=False,
            sheets=FakeSheetsPort(),
        )
        db.commit()
        in_row = store.get_canonical_event(
            provider="whatsapp", provider_event_id="evt.handoff.conflict.1"
        )
        assert in_row is not None
        assert in_row.lead_id != website_lead_id
        hash_row = db.scalars(
            select(HandoffTokenRow).where(HandoffTokenRow.token_hash == hash_handoff_token(token))
        ).one()
        assert hash_row.consumed_at is None
        wa_customer = db.scalars(
            select(ChannelIdentityRow.customer_id).where(
                ChannelIdentityRow.external_id == OTHER_PHONE
            )
        ).one()
        web_customer = db.scalars(
            select(ChannelIdentityRow.customer_id).where(
                ChannelIdentityRow.external_id == session_id
            )
        ).one()
        assert wa_customer == other_cust
        assert web_customer != wa_customer
        web_link = db.scalars(
            select(IdentityLinkRow).where(IdentityLinkRow.customer_id == web_customer)
        ).first()
        assert web_link is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_successful_handoff_persists_identity_link() -> None:
    init_db()
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        session_id = created.json()["session_id"]
        website_lead_id = created.json()["lead_id"]
        handoff = client.post(f"/v1/website/sessions/{session_id}/handoff")
        token = handoff.json()["token"]
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{"id": "evt.link.persist.1", "from": LINK_PHONE, "text": token}],
            store=store,
            port=RecordingMessagePort(),
            kill_switch=False,
            sheets=FakeSheetsPort(),
        )
        db.commit()
        web_identity = db.scalars(
            select(ChannelIdentityRow).where(ChannelIdentityRow.external_id == session_id)
        ).one()
        wa_identity = db.scalars(
            select(ChannelIdentityRow).where(ChannelIdentityRow.external_id == LINK_PHONE)
        ).one()
        link = store.get_identity_link(wa_identity.id)
        assert link is not None
        assert link.reason == REASON_HANDOFF_TOKEN
        assert link.customer_id == web_identity.customer_id
        assert link.identity_id == wa_identity.id
        assert link.reversed_at is None
        in_row = store.get_canonical_event(
            provider="whatsapp", provider_event_id="evt.link.persist.1"
        )
        assert in_row is not None
        assert in_row.lead_id == website_lead_id
    finally:
        db.close()


@pytest.mark.asyncio
async def test_second_identity_link_persist_is_noop() -> None:
    init_db()
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        session_id = created.json()["session_id"]
        handoff = client.post(f"/v1/website/sessions/{session_id}/handoff")
        token = handoff.json()["token"]
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{"id": "evt.link.noop.1", "from": LINK_NOP_PHONE, "text": token}],
            store=store,
            port=RecordingMessagePort(),
            kill_switch=False,
            sheets=FakeSheetsPort(),
        )
        db.commit()
        wa_identity = db.scalars(
            select(ChannelIdentityRow).where(ChannelIdentityRow.external_id == LINK_NOP_PHONE)
        ).one()
        web_identity = db.scalars(
            select(ChannelIdentityRow).where(ChannelIdentityRow.external_id == session_id)
        ).one()
        first_link = store.get_identity_link(wa_identity.id)
        assert first_link is not None
        again = persist_verified_identity_link(
            store,
            customer_id=web_identity.customer_id,
            channel=Channel.WHATSAPP,
            external_id=LINK_NOP_PHONE,
            reason=REASON_HANDOFF_TOKEN,
        )
        db.commit()
        assert again is False
        link_after = store.get_identity_link(wa_identity.id)
        assert link_after is not None
        assert link_after.id == first_link.id
    finally:
        db.close()


@pytest.mark.asyncio
async def test_phone_bound_to_other_customer_no_identity_link_for_website() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        other_cust, _other_lead = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id=CONFLICT_LINK_PHONE
        )
        db.commit()
    finally:
        db.close()
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        session_id = created.json()["session_id"]
        website_lead_id = created.json()["lead_id"]
        handoff = client.post(f"/v1/website/sessions/{session_id}/handoff")
        token = handoff.json()["token"]
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.link.conflict.1",
                    "from": CONFLICT_LINK_PHONE,
                    "text": token,
                }
            ],
            store=store,
            port=RecordingMessagePort(),
            kill_switch=False,
            sheets=FakeSheetsPort(),
        )
        db.commit()
        web_customer = db.scalars(
            select(ChannelIdentityRow.customer_id).where(
                ChannelIdentityRow.external_id == session_id
            )
        ).one()
        wa_customer = db.scalars(
            select(ChannelIdentityRow.customer_id).where(
                ChannelIdentityRow.external_id == CONFLICT_LINK_PHONE
            )
        ).one()
        assert wa_customer == other_cust
        assert web_customer != wa_customer
        web_link = db.scalars(
            select(IdentityLinkRow).where(IdentityLinkRow.customer_id == web_customer)
        ).first()
        assert web_link is None
        in_row = store.get_canonical_event(
            provider="whatsapp", provider_event_id="evt.link.conflict.1"
        )
        assert in_row is not None
        assert in_row.lead_id != website_lead_id
    finally:
        db.close()


def test_handoff_unknown_session_returns_404() -> None:
    init_db()
    with TestClient(app) as client:
        response = client.post("/v1/website/sessions/web_unknown_session/handoff")
        assert response.status_code == 404


def test_handoff_whatsapp_url_with_click_to_chat(monkeypatch) -> None:
    monkeypatch.setenv("MIA_WHATSAPP_CLICK_TO_CHAT", CLICK_CHAT)
    init_db()
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        session_id = created.json()["session_id"]
        handoff = client.post(f"/v1/website/sessions/{session_id}/handoff")
        assert handoff.status_code == 200
        body = handoff.json()
        url = body["whatsapp_url"]
        assert url is not None
        assert CLICK_CHAT in url
        assert "wa.me/" in url
        assert unquote(url.split("text=", 1)[1]) == compose_handoff_text(body["token"])
        assert "mia1_" not in url
        assert "lead_" not in url
        assert "web_" not in url
        assert "@" not in url


def test_handoff_empty_click_to_chat_null_url() -> None:
    init_db()
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        session_id = created.json()["session_id"]
        handoff = client.post(f"/v1/website/sessions/{session_id}/handoff")
        assert handoff.status_code == 200
        body = handoff.json()
        assert body["whatsapp_url"] is None
        assert body["token"].startswith("mia1_")
        assert body["expires_at"]


@pytest.mark.asyncio
async def test_owner_inbound_with_token_does_not_attach_lead() -> None:
    init_db()
    token = generate_handoff_token()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{"id": "evt.handoff.owner.1", "from": OWNER_PHONE, "text": token}],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_PHONE},
            sheets=FakeSheetsPort(),
        )
        db.commit()
        in_row = store.get_canonical_event(
            provider="whatsapp", provider_event_id="evt.handoff.owner.1"
        )
        assert in_row is not None
        assert in_row.lead_id is None
        assert in_row.actor_role == "owner"
        payload = json.loads(in_row.payload_json)
        assert payload["text"] == "[website handoff]"
        assert token not in in_row.payload_json
        identity = db.scalars(
            select(ChannelIdentityRow).where(ChannelIdentityRow.external_id == OWNER_PHONE)
        ).first()
        assert identity is None
    finally:
        db.close()
