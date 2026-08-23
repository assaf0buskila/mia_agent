import inspect

import pytest
from app.api.inbound import process_inbound_texts
from app.core.capabilities import CapabilityId, require_alive
from app.core.config import Settings
from app.db.models import CampaignPrelaunchRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.prelaunch import (
    apply_prelaunch_policy,
    evaluate_prelaunch,
    format_prelaunch_line,
    should_run_prelaunch,
)
from app.integrations.base import RecordingMessagePort
from app.integrations.meta_ads import CampaignInsights, FakeMetaAdsPort
from app.integrations.sheets import FakeSheetsPort
from sqlalchemy import select

OWNER_PRELAUNCH_PHONE = "972509990311"
EVENT_PRELAUNCH = "evt.owner.prelaunch.1"


def _full_settings(**overrides: str) -> Settings:
    base = {
        "campaign_name": "Yuma",
        "campaign_launch_date": "2026-09-01",
        "campaign_objective": "leads",
        "campaign_lead_path": "website",
        "website_url": "https://www.assafweb.com",
        "sheets_spreadsheet_id": "sheet1",
        "campaign_monthly_budget": "5000",
        "campaign_e2e_tested": "true",
    }
    base.update(overrides)
    return Settings(**base)


def test_should_run_prelaunch_false_when_name_empty_or_invalid() -> None:
    assert should_run_prelaunch(Settings(campaign_name="")) is False
    assert should_run_prelaunch(Settings(campaign_name="Campaign Yuma")) is False
    assert should_run_prelaunch(Settings(campaign_name="bad name!")) is False


def test_should_run_prelaunch_true_when_name_yuma() -> None:
    assert should_run_prelaunch(Settings(campaign_name="Yuma")) is True


def test_evaluate_prelaunch_all_checks_fail_name_only() -> None:
    snap = evaluate_prelaunch(Settings(campaign_name="Yuma"))
    assert snap.ready is False
    assert snap.skipped is False
    assert snap.failed_checks == (
        "alert_thresholds,campaign_config,e2e_test,lead_capture,"
        "sheet_tabs,source_attribution,tracking_utms"
    )


def test_evaluate_prelaunch_full_pass() -> None:
    snap = evaluate_prelaunch(_full_settings())
    assert snap.ready is True
    assert snap.failed_checks == ""
    assert snap.launch_date == "2026-09-01"
    assert snap.objective == "leads"
    assert snap.lead_path == "website"
    assert snap.campaign == "Yuma"


def test_evaluate_prelaunch_whatsapp_lead_capture_tokens() -> None:
    without = evaluate_prelaunch(
        _full_settings(campaign_lead_path="whatsapp", campaign_e2e_tested="true")
    )
    assert without.ready is False
    assert "lead_capture" in without.failed_checks.split(",")
    assert "tracking_utms" in without.failed_checks.split(",")
    with_tokens = evaluate_prelaunch(
        _full_settings(
            campaign_lead_path="whatsapp",
            whatsapp_verify_token="v",
            whatsapp_access_token="a",
            whatsapp_phone_number_id="p",
        )
    )
    assert with_tokens.ready is False
    assert "lead_capture" not in with_tokens.failed_checks.split(",")
    assert "tracking_utms" in with_tokens.failed_checks.split(",")


def test_format_prelaunch_line_hebrew_no_budget_digits() -> None:
    ready = evaluate_prelaunch(_full_settings())
    assert format_prelaunch_line(ready) == "שער טרום-השקה: מוכן"
    not_ready = evaluate_prelaunch(Settings(campaign_name="Yuma"))
    line = format_prelaunch_line(not_ready)
    assert line.startswith("שער טרום-השקה: לא מוכן")
    assert "5000" not in line
    assert "sheet1" not in line
    assert "חסר:" in line


def test_apply_prelaunch_policy_persist_kill_switch_demo_skip() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        snap = evaluate_prelaunch(_full_settings())
        apply_prelaunch_policy(
            store, snapshot=snap, kill_switch=False, demo_active=False
        )
        db.commit()
        row = store.get_campaign_prelaunch()
        assert row is not None
        assert row.campaign == "Yuma"
        assert row.ready is True
        apply_prelaunch_policy(
            store,
            snapshot=evaluate_prelaunch(_full_settings(campaign_e2e_tested="")),
            kill_switch=True,
            demo_active=False,
        )
        db.commit()
        assert store.get_campaign_prelaunch().ready is True
        apply_prelaunch_policy(
            store,
            snapshot=evaluate_prelaunch(_full_settings(campaign_e2e_tested="")),
            kill_switch=False,
            demo_active=True,
        )
        db.commit()
        assert store.get_campaign_prelaunch().ready is True
    finally:
        row = store.get_campaign_prelaunch()
        if row is not None:
            db.delete(row)
            db.commit()
        db.close()


@pytest.mark.asyncio
async def test_owner_analytics_prelaunch_ack_not_ready(monkeypatch) -> None:
    monkeypatch.setenv("MIA_CAMPAIGN_NAME", "Yuma")
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": EVENT_PRELAUNCH,
                    "from": OWNER_PRELAUNCH_PHONE,
                    "text": "how's the campaign spend",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_PRELAUNCH_PHONE},
            sheets=FakeSheetsPort(),
            meta_ads=FakeMetaAdsPort(
                CampaignInsights(spend="100", clicks="5", impressions="1000", ctr="1")
            ),
        )
        db.commit()
        task = store.get_owner_task(
            provider="whatsapp", provider_event_id=EVENT_PRELAUNCH
        )
        assert task is not None
        assert task.status == "logged"
        ack = port.sent[0].text
        assert "לא מוכן" in ack
        assert "שער טרום-השקה" in ack
        assert "5000" not in ack
        row = store.get_campaign_prelaunch()
        assert row is not None
        assert row.campaign == "Yuma"
        assert row.ready is False
    finally:
        prelaunch = db.scalars(select(CampaignPrelaunchRow)).one_or_none()
        if prelaunch is not None:
            db.delete(prelaunch)
            db.commit()
        db.close()


def test_prelaunch_module_no_message_port_or_meta_writes() -> None:
    import app.domain.prelaunch as prelaunch_mod

    source = inspect.getsource(prelaunch_mod)
    assert "MessagePort" not in source
    lowered = source.lower()
    assert "meta_ads" not in lowered
    assert "metaads" not in lowered


def test_require_alive_campaign_prelaunch() -> None:
    require_alive(CapabilityId.CAMPAIGN_PRELAUNCH)
