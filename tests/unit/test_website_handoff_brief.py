"""Website→WhatsApp click briefs Assaf on Telegram; Mia stays silent on WhatsApp."""

from __future__ import annotations

import inspect

from app.core.config import Settings
from app.domain import website_handoff_brief as brief_mod
from app.domain.memory import ConversationTurn
from app.domain.sales import FitLevel, PainLevel, SalesState
from app.domain.website_handoff_brief import (
    KIND_WEBSITE_WHATSAPP,
    NOTIFICATION_DELIVERED,
    NOTIFICATION_DUPLICATE_OR_AMBIGUOUS,
    NOTIFICATION_FAILED,
    apply_website_whatsapp_handoff_brief,
    format_website_whatsapp_brief,
)
from app.integrations.owner_reply import OWNER_CAPABILITIES, SYSTEM_PROMPT
from app.services.notifications import OwnerTelegramDelivery


def _paste_line(brief: str) -> str:
    _, rest = brief.split("השורה שלך:", 1)
    return rest.strip().split("\n", 1)[0].strip()


def _inventory_sales() -> SalesState:
    return SalesState(
        lead_id="lead_abc123def456",
        pain_level=PainLevel.P2,
        fit=FitLevel.UNKNOWN,
        workflow_known=True,
        manual_step_known=True,
        impact_confirmed=True,
    )


def _inventory_turns() -> list[ConversationTurn]:
    return [
        ConversationTurn(role="prospect", text="אני מוכר נעליים ומזין מלאי לשיטס"),
        ConversationTurn(role="mia", text="כמה זמן זה לוקח לך בפועל?"),
        ConversationTurn(role="prospect", text="שעתיים כל פעם"),
    ]


class _FakeStore:
    def __init__(self, *, already: bool = False) -> None:
        self.already = already
        self.upserts: list[dict[str, str]] = []
        self.legacy_claims: set[tuple[str, str, str]] = set()
        self.recipient_claims: set[tuple[str, str, str, str]] = set()
        self.accepted_recipient_claims: set[tuple[str, str, str, str]] = set()
        self.delivery_state_commits = 0
        self.sales = _inventory_sales()
        self.turns = _inventory_turns()

    def try_insert_owner_notification(
        self, *, kind: str, lead_id: str, scheduled_at: str, conversation_id: str = ""
    ) -> bool:
        """Model the DB's atomic claim: the first caller wins, duplicates lose.

        This double stands in for the database, not for the code under test. What is
        being tested is whether the caller gates its Telegram send on this result --
        a read-then-send let two concurrent /handoff clicks both push the brief.
        """
        if self.already:
            return False
        key = (kind, lead_id, conversation_id)
        if key in self.legacy_claims:
            return False
        self.legacy_claims.add(key)
        self.upserts.append(
            {"kind": kind, "lead_id": lead_id, "scheduled_at": scheduled_at}
        )
        return True

    def has_owner_notification(self, *, kind: str, lead_id: str) -> bool:
        if self.already:
            return True
        return any(
            row["kind"] == kind and row["lead_id"] == lead_id for row in self.upserts
        )

    def get_sales(self, lead_id: str) -> SalesState:
        return self.sales

    def list_conversation_turns(self, session_id: str) -> list[ConversationTurn]:
        return self.turns

    def upsert_owner_notification(
        self, *, kind: str, lead_id: str, scheduled_at: str
    ) -> None:
        if self.has_owner_notification(kind=kind, lead_id=lead_id):
            return
        self.upserts.append(
            {"kind": kind, "lead_id": lead_id, "scheduled_at": scheduled_at}
        )

    def try_claim_owner_notification_recipient(
        self,
        *,
        kind: str,
        lead_id: str,
        notification_key: str,
        recipient_id: str,
        claimed_at: str,
    ) -> bool:
        _ = claimed_at
        key = (kind, lead_id, notification_key, recipient_id)
        if key in self.recipient_claims:
            return False
        self.recipient_claims.add(key)
        return True

    def try_claim_owner_notification_recipient_compatible(
        self,
        *,
        kind: str,
        compatible_kinds: tuple[str, ...],
        lead_id: str,
        notification_key: str,
        recipient_id: str,
        claimed_at: str,
    ) -> bool:
        if any(
            claim[0] in compatible_kinds
            and claim[1:] == (lead_id, notification_key, recipient_id)
            for claim in self.recipient_claims
        ):
            return False
        return self.try_claim_owner_notification_recipient(
            kind=kind,
            lead_id=lead_id,
            notification_key=notification_key,
            recipient_id=recipient_id,
            claimed_at=claimed_at,
        )

    def release_owner_notification_recipient_claim(
        self,
        *,
        kind: str,
        lead_id: str,
        notification_key: str,
        recipient_id: str,
    ) -> None:
        self.recipient_claims.discard(
            (kind, lead_id, notification_key, recipient_id)
        )

    def has_owner_notification_delivery_claim(
        self, *, kind: str, lead_id: str, notification_key: str = ""
    ) -> bool:
        return (kind, lead_id, notification_key) in self.legacy_claims or any(
            claim[:3] == (kind, lead_id, notification_key)
            for claim in self.recipient_claims
        )

    def has_owner_notification_claim(
        self, *, kind: str, lead_id: str, conversation_id: str = ""
    ) -> bool:
        return (kind, lead_id, conversation_id) in self.legacy_claims

    def commit_owner_notification_delivery_state(self) -> bool:
        self.delivery_state_commits += 1
        return True

    def release_owner_notification_recipient_claims_durably(
        self,
        *,
        kind: str,
        lead_id: str,
        recipient_ids: tuple[str, ...],
        notification_key: str = "",
    ) -> bool:
        for recipient_id in recipient_ids:
            self.release_owner_notification_recipient_claim(
                kind=kind,
                lead_id=lead_id,
                notification_key=notification_key,
                recipient_id=recipient_id,
            )
        return self.commit_owner_notification_delivery_state()

    def confirmed_owner_notification_recipients(
        self, *, kind: str, lead_id: str, notification_key: str = ""
    ) -> tuple[str, ...]:
        return tuple(
            sorted(
                recipient_id
                for (
                    claim_kind,
                    claim_lead_id,
                    claim_key,
                    recipient_id,
                ) in self.accepted_recipient_claims
                if (claim_kind, claim_lead_id, claim_key)
                == (kind, lead_id, notification_key)
            )
        )

    def record_owner_notification_recipient_delivery_outcomes_durably(
        self,
        *,
        kind: str,
        lead_id: str,
        delivered_recipient_ids: tuple[str, ...],
        rejected_recipient_ids: tuple[str, ...],
        notification_key: str = "",
    ) -> bool:
        for recipient_id in delivered_recipient_ids:
            self.accepted_recipient_claims.add(
                (kind, lead_id, notification_key, recipient_id)
            )
        for recipient_id in rejected_recipient_ids:
            self.release_owner_notification_recipient_claim(
                kind=kind,
                lead_id=lead_id,
                notification_key=notification_key,
                recipient_id=recipient_id,
            )
        return self.commit_owner_notification_delivery_state()


def test_handoff_brief_names_the_lead_and_tells_assaf_to_take_over() -> None:
    brief = format_website_whatsapp_brief(
        lead_id="lead_abc123def456",
        sales=_inventory_sales(),
        turns=_inventory_turns(),
    )
    assert "lead_abc123def456" in brief
    assert "מיה לא תענה שם" in brief
    assert "תטפל אתה" in brief
    assert "שעתיים כל פעם" in brief
    assert "נעליים" in brief
    assert "השורה שלך:" in brief
    paste = _paste_line(brief)
    assert "מלאי" in paste
    assert "שיטס" in paste
    assert "מיה תענה" not in paste
    assert "mia1_" not in paste
    assert "mia1_" not in brief.split("השורה שלך:", 1)[1].split("השיחה:", 1)[0]


def test_human_handoff_brief_includes_the_conversation_and_is_not_a_whatsapp_claim() -> None:
    from app.domain.website_handoff_brief import format_website_human_handoff_brief

    brief = format_website_human_handoff_brief(
        lead_id="lead_abc123def456",
        sales=_inventory_sales(),
        turns=_inventory_turns(),
    )
    assert "צריך אותך" in brief
    assert "שעתיים כל פעם" in brief
    assert "וואטסאפ" not in brief
    assert "מיה לא תענה שם" not in brief


def test_recommended_line_is_generic_when_nothing_concrete_is_known() -> None:
    sales = SalesState(lead_id="lead_early1234567", fit=FitLevel.UNKNOWN)
    turns = [
        ConversationTurn(role="prospect", text="היי"),
        ConversationTurn(role="mia", text="ספרי לי על היום בעסק?"),
    ]
    brief = format_website_whatsapp_brief(
        lead_id="lead_early1234567", sales=sales, turns=turns
    )
    paste = _paste_line(brief)
    assert "השורה שלך:" in brief
    assert "מלאי" not in paste
    assert "ידנית" not in paste
    assert "אתר" in paste or "מהאתר" in paste
    assert "מיה תענה" not in paste


def test_recommended_line_continues_explicit_buying_intent() -> None:
    sales = SalesState(
        lead_id="lead_intent123456",
        explicit_buying_intent=True,
    )
    turns = [
        ConversationTurn(role="prospect", text="אני רוצה לבנות אתר לעסק"),
    ]
    paste = _paste_line(
        format_website_whatsapp_brief(
            lead_id="lead_intent123456", sales=sales, turns=turns
        )
    )
    assert "אתר" in paste
    assert "מלאי" not in paste


def test_recommended_line_never_copies_token_phone_price_or_roi() -> None:
    sales = SalesState(lead_id="lead_unsafe123456", fit=FitLevel.UNKNOWN)
    turns = [
        ConversationTurn(
            role="prospect",
            text="תתקשרי ל-0501234567 mia1_secretTOKEN זה 5000 שח וROI של 40%",
        )
    ]
    paste = _paste_line(
        format_website_whatsapp_brief(
            lead_id="lead_unsafe123456", sales=sales, turns=turns
        )
    )
    assert "mia1_" not in paste
    assert "0501234567" not in paste
    assert "5000" not in paste
    assert "roi" not in paste.lower()
    assert "%" not in paste
    assert "מיה תענה" not in paste


def test_recommended_line_does_not_invent_a_manual_step_from_pain_alone() -> None:
    sales = SalesState(
        lead_id="lead_pain12345678",
        pain_level=PainLevel.P3,
        impact_confirmed=True,
    )
    turns = [
        ConversationTurn(role="prospect", text="השיחות נעלמות לי כל היום"),
    ]
    paste = _paste_line(
        format_website_whatsapp_brief(
            lead_id="lead_pain12345678", sales=sales, turns=turns
        )
    )
    assert "מלאי" not in paste
    assert "שיטס" not in paste
    assert paste


def test_handoff_brief_module_has_no_llm() -> None:
    source = inspect.getsource(brief_mod)
    lowered = source.lower()
    assert "openai" not in lowered
    assert "anthropic" not in lowered
    assert "chat.completions" not in lowered
    assert "paraphrase" not in lowered


def test_apply_skips_kill_switch_and_demo(monkeypatch) -> None:
    monkeypatch.setattr(
        brief_mod,
        "_deliver_owner_brief",
        lambda **kwargs: OwnerTelegramDelivery(delivered=("111",)),
    )
    store = _FakeStore()
    killed = apply_website_whatsapp_handoff_brief(
        store,
        lead_id="lead_abc123def456",
        session_id="sess_1",
        settings=Settings(kill_switch=True),
    )
    demo = apply_website_whatsapp_handoff_brief(
        store,
        lead_id="lead_abc123def456",
        session_id="sess_1",
        settings=Settings(demo_mode=True),
    )
    assert killed.brief is None and killed.notification_status == NOTIFICATION_FAILED
    assert demo.brief is None and demo.notification_status == NOTIFICATION_FAILED
    assert store.upserts == []


def test_apply_persists_once_per_lead(monkeypatch) -> None:
    sent: list[tuple[str, ...]] = []

    def deliver(**kwargs):
        sent.append(kwargs["recipient_ids"])
        return OwnerTelegramDelivery(delivered=kwargs["recipient_ids"])

    monkeypatch.setattr(
        brief_mod,
        "_deliver_owner_brief",
        deliver,
    )
    store = _FakeStore()
    settings = Settings(telegram_bot_token="tok", telegram_owner_user_ids="111")
    first = apply_website_whatsapp_handoff_brief(
        store,
        lead_id="lead_abc123def456",
        session_id="sess_1",
        settings=settings,
    )
    second = apply_website_whatsapp_handoff_brief(
        store,
        lead_id="lead_abc123def456",
        session_id="sess_1",
        settings=settings,
    )
    assert first.notification_status == NOTIFICATION_DELIVERED
    assert second.notification_status == NOTIFICATION_DELIVERED
    assert first.brief is not None and "השורה שלך:" in first.brief
    assert len(store.upserts) == 1
    assert store.upserts[0]["kind"] == KIND_WEBSITE_WHATSAPP
    assert len(sent) == 1


def test_missing_configuration_does_not_consume_click_brief_retry(monkeypatch) -> None:
    sent: list[tuple[str, ...]] = []

    def deliver(**kwargs):
        sent.append(kwargs["recipient_ids"])
        return OwnerTelegramDelivery(delivered=kwargs["recipient_ids"])

    monkeypatch.setattr(brief_mod, "_deliver_owner_brief", deliver)
    store = _FakeStore()
    first = apply_website_whatsapp_handoff_brief(
        store,
        lead_id="lead_abc123def456",
        session_id="sess_1",
        settings=Settings(),
    )
    assert first.notification_status == NOTIFICATION_FAILED
    assert store.recipient_claims == set()
    assert store.delivery_state_commits == 0
    second = apply_website_whatsapp_handoff_brief(
        store,
        lead_id="lead_abc123def456",
        session_id="sess_1",
        settings=Settings(telegram_bot_token="tok", telegram_owner_user_ids="111"),
    )
    assert second.notification_status == NOTIFICATION_DELIVERED
    assert sent == [("111",)]


def test_explicit_rejection_retries_only_rejected_owner(monkeypatch) -> None:
    sends: list[tuple[str, ...]] = []

    def deliver(**kwargs):
        recipients = kwargs["recipient_ids"]
        sends.append(recipients)
        if len(sends) == 1:
            return OwnerTelegramDelivery(delivered=("111",), rejected=("222",))
        return OwnerTelegramDelivery(delivered=recipients)

    monkeypatch.setattr(brief_mod, "_deliver_owner_brief", deliver)
    store = _FakeStore()
    settings = Settings(
        telegram_bot_token="tok", telegram_owner_user_ids="111,222"
    )
    first = apply_website_whatsapp_handoff_brief(
        store,
        lead_id="lead_abc123def456",
        session_id="sess_1",
        settings=settings,
    )
    second = apply_website_whatsapp_handoff_brief(
        store,
        lead_id="lead_abc123def456",
        session_id="sess_1",
        settings=settings,
    )
    assert first.notification_status == NOTIFICATION_DELIVERED
    assert second.notification_status == NOTIFICATION_DELIVERED
    assert sends == [("111", "222"), ("222",)]
    assert store.delivery_state_commits == 4


def test_ambiguous_click_brief_is_not_resent(monkeypatch) -> None:
    sends: list[tuple[str, ...]] = []

    def deliver(**kwargs):
        sends.append(kwargs["recipient_ids"])
        return OwnerTelegramDelivery(ambiguous=kwargs["recipient_ids"])

    monkeypatch.setattr(brief_mod, "_deliver_owner_brief", deliver)
    store = _FakeStore()
    settings = Settings(telegram_bot_token="tok", telegram_owner_user_ids="111")
    first = apply_website_whatsapp_handoff_brief(
        store,
        lead_id="lead_abc123def456",
        session_id="sess_1",
        settings=settings,
    )
    second = apply_website_whatsapp_handoff_brief(
        store,
        lead_id="lead_abc123def456",
        session_id="sess_1",
        settings=settings,
    )
    assert first.notification_status == NOTIFICATION_DUPLICATE_OR_AMBIGUOUS
    assert second.notification_status == NOTIFICATION_DUPLICATE_OR_AMBIGUOUS
    assert sends == [("111",)]


def test_handoff_compose_text_is_human_and_hides_the_token() -> None:
    from app.domain.handoff import compose_handoff_text, generate_handoff_token

    token = generate_handoff_token()
    text = compose_handoff_text(token)
    assert "mia1_" not in text
    assert "אסף" in text
    assert "מיה" in text


def test_whatsapp_stays_gated_in_auto_approved_without_the_flag() -> None:
    from app.core.config import AutomationMode
    from app.domain.events import Channel
    from app.domain.shadow import should_skip_prospect_send

    assert (
        should_skip_prospect_send(
            AutomationMode.AUTO_APPROVED,
            "prospect",
            channel=Channel.WHATSAPP.value,
            whatsapp_handoff_send=False,
            whatsapp_require_business_scope=True,
        )
        is True
    )
    assert (
        should_skip_prospect_send(
            AutomationMode.AUTO_APPROVED,
            "prospect",
            channel=Channel.WHATSAPP.value,
            whatsapp_handoff_send=True,
            whatsapp_require_business_scope=True,
        )
        is False
    )
    assert should_skip_prospect_send(AutomationMode.AUTO_APPROVED, "owner") is False


def test_owner_prompt_does_not_dump_a_composio_catalog() -> None:
    assert "WHATSAPP_SEND_MESSAGE" not in SYSTEM_PROMPT
    assert "catalog" not in SYSTEM_PROMPT.lower()
    assert "gmail_summary" in OWNER_CAPABILITIES
    assert "daily_brief" in OWNER_CAPABILITIES
    assert "Python owns" in SYSTEM_PROMPT
    assert "Composio tools" in SYSTEM_PROMPT
