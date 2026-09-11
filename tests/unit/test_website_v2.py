import json

import pytest
from app.api.deps import get_transcription_port
from app.db.models import CrmContactRow, CrmOutboxRow
from app.db.session import get_session_factory
from app.db.site_v2 import SiteV2SessionRow
from app.db.store import LeadStore
from app.integrations.llm_client import LlmError, LlmResponse, ToolCall
from app.integrations.transcribe import FakeTranscriptionPort
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import select


class _SiteClient:
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[list[dict]] = []
        self.validation_calls = 0
        self.consent_prompts: list[str] = []
        # Verdicts are independent fixtures, not inferred from the implementation's
        # wording detector or defaulted to safe for arbitrary provider output.
        self.narrative_verdicts = {
            "אפשר להראות דוגמה קונקרטית שמתאימה למה שתיארת.": "safe",
        }

    def enabled(self) -> bool:
        return True

    def complete(self, **kwargs):  # noqa: ANN003
        tools = kwargs.get("tools") or []
        if _has_tool(kwargs, "validate_site_narrative"):
            self.validation_calls += 1
            proposed = json.loads(kwargs["messages"][-1]["content"])["proposed_reply"]
            return _validation_response(
                proposed, self.narrative_verdicts.get(proposed, "uncertain")
            )
        if any(
            tool.get("function", {}).get("name") == "classify_contact_consent"
            for tool in tools
        ):
            prompt = kwargs["messages"][0]["content"]
            self.consent_prompts.append(prompt)
            invitation = json.loads(prompt.split("PRIOR_INVITATION=", 1)[1].split("\n", 1)[0])
            current_raw = prompt.split("CURRENT_INPUT=", 1)[1].split("\n", 1)[0]
            contact_raw = prompt.split("SERVER_EXTRACTED_CONTACT=", 1)[1]
            current = json.loads(current_raw)
            contact = json.loads(contact_raw)
            lowered = current.casefold()
            decision = (
                "refused"
                if "withhold consent" in lowered
                else "quoted"
                if "customer wrote" in lowered
                else "ambiguous"
                if current.startswith("האם") or current.casefold().startswith("do you")
                else "ambiguous"
                if current.strip() == contact and not invitation
                else "affirmative"
            )
            arguments = {
                "decision": decision,
                "evidence": current,
                "contact_span": contact if decision == "affirmative" else "",
            }
            return LlmResponse(
                "",
                (ToolCall("consent-1", "classify_contact_consent", arguments, "{}"),),
                "stop",
                "",
                0,
                0,
                {"role": "assistant", "content": None},
            )
        self.calls += 1
        self.prompts.append(kwargs["messages"])
        return LlmResponse(
            text="אפשר להראות דוגמה קונקרטית שמתאימה למה שתיארת.",
            tool_calls=(),
            finish_reason="stop",
            refusal="",
            tokens_in=10,
            tokens_out=8,
            raw_message={"role": "assistant", "content": "תשובה"},
        )


def _has_tool(kwargs: dict, name: str) -> bool:
    return any(
        tool.get("function", {}).get("name") == name for tool in kwargs.get("tools") or []
    )


def _validation_response(proposed: str, decision: str) -> LlmResponse:
    return LlmResponse(
        "",
        (ToolCall("validation-1", "validate_site_narrative", {
            "decision": decision,
            "reviewed_text": proposed,
            "evidence": "" if decision == "safe" else proposed,
        }, "{}"),),
        "stop", "", 0, 0, {"role": "assistant", "content": None},
    )


def _new(client: TestClient) -> tuple[str, str]:
    created = client.post("/v1/website/sessions")
    assert created.status_code == 200, created.text
    body = created.json()
    from uuid import UUID

    assert str(UUID(body["session_id"])) == body["session_id"]
    return body["session_id"], body["session_credential"]


def _headers(credential: str) -> dict[str, str]:
    return {"X-Mia-Session-Credential": credential}


def test_v2_session_requires_credential_and_replays_stable_message(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MIA_WEBSITE_V2_ENABLED", "true")
    fake = _SiteClient()
    monkeypatch.setattr("app.surfaces.site_v2.build_site_client", lambda _settings: fake)
    with TestClient(app) as client:
        session_id, credential = _new(client)
        with get_session_factory()() as db:
            row = db.get(SiteV2SessionRow, session_id)
            assert row is not None
            assert row.credential_hash != credential
            assert credential not in row.state_json
        payload = {"text": "יש לי סטודיו לפילאטיס", "client_message_id": "m-1"}
        missing = client.post(f"/v1/website/sessions/{session_id}/messages", json=payload)
        assert missing.status_code == 401
        first = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json=payload,
            headers=_headers(credential),
        )
        replay = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json=payload,
            headers=_headers(credential),
        )
        assert first.status_code == replay.status_code == 200
        assert first.json() == replay.json()
        assert fake.calls == 1
        assert fake.validation_calls == 1


def test_v2_contact_capture_is_grounded_and_session_scoped(monkeypatch) -> None:
    monkeypatch.setenv("MIA_WEBSITE_V2_ENABLED", "true")
    monkeypatch.setenv("MIA_CRM_V2_ENABLED", "true")
    monkeypatch.setenv("MIA_TELEGRAM_OWNER_USER_IDS", "123")
    fake = _SiteClient()
    monkeypatch.setattr("app.surfaces.site_v2.build_site_client", lambda _settings: fake)
    with TestClient(app) as client:
        session_id, credential = _new(client)
        question = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={
                "text": "האם אתם תומכים במספרי טלפון כמו 050-123-4567?",
                "client_message_id": "same-id",
            },
            headers=_headers(credential),
        )
        assert question.status_code == 200
        with get_session_factory()() as db:
            assert (
                db.scalar(
                    select(CrmContactRow).where(
                        CrmContactRow.conversation_id == session_id
                    )
                )
                is None
            )

        captured = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={
                "text": "תחזרו אליי בבקשה",
                "phone": "+44 20 7946 0958",
                "name": "Dana",
                "client_message_id": "capture-1",
            },
            headers=_headers(credential),
        )
        assert captured.status_code == 200, captured.text
        assert captured.json()["next_action"] == "contact_saved"
        with get_session_factory()() as db:
            contact = db.scalar(
                select(CrmContactRow).where(
                    CrmContactRow.conversation_id == session_id
                )
            )
            assert contact is not None
            assert contact.conversation_id == session_id
            assert contact.source_ref == f"site:{session_id}:capture-1"
            state = db.get(SiteV2SessionRow, session_id)
            assert state is not None and json.loads(state.state_json)["captured"] is True
            jobs = list(db.scalars(select(CrmOutboxRow)).all())
            session_jobs = [job for job in jobs if job.aggregate_id == contact.id]
            assert any(job.destination == "telegram" for job in session_jobs)
            job_count = len(session_jobs)
        continued = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "ומה קורה אחרי זה?", "client_message_id": "continue-1"},
            headers=_headers(credential),
        )
        assert continued.status_code == 200
        assert continued.json()["next_action"] == "answer"
        with get_session_factory()() as db:
            assert (
                len(
                    list(
                        db.scalars(
                            select(CrmOutboxRow).where(CrmOutboxRow.aggregate_id == contact.id)
                        ).all()
                    )
                )
                == job_count
            )


@pytest.mark.parametrize(
    "contact_text,fields",
    (
        (
            'A customer wrote: "contact me at alice@example.com". How should I answer?',
            {},
        ),
        (
            "אני לא רוצה שתיצרו קשר. המייל שלי nofollow@example.com",
            {},
        ),
        (
            "Do not contact me at this address.",
            {"email": "nofollow@example.com"},
        ),
        (
            "My email is refusal2@example.com, but I refuse permission to contact me.",
            {},
        ),
        (
            "My email is refusal4@example.com, but I forbid you to contact me.",
            {},
        ),
        (
            "My email is refusal5@example.com, but I withhold consent to any follow-up.",
            {},
        ),
        (
            "המייל שלי refusal3@example.com, אבל אני מסרבת לתת אישור ליצור איתי קשר.",
            {},
        ),
    ),
)
def test_v2_contact_examples_and_negation_are_not_consent(
    monkeypatch, contact_text: str, fields: dict[str, str]
) -> None:
    monkeypatch.setenv("MIA_WEBSITE_V2_ENABLED", "true")
    monkeypatch.setenv("MIA_CRM_V2_ENABLED", "true")
    monkeypatch.setattr("app.surfaces.site_v2.build_site_client", lambda _settings: _SiteClient())
    with TestClient(app) as client:
        session_id, credential = _new(client)
        with get_session_factory()() as db:
            prior_job_ids = {row.id for row in db.scalars(select(CrmOutboxRow)).all()}
        response = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": contact_text, "client_message_id": "negative-1", **fields},
            headers=_headers(credential),
        )
        assert response.status_code == 200, response.text
        assert response.json()["next_action"] == "answer"
        with get_session_factory()() as db:
            assert (
                db.scalar(select(CrmContactRow).where(CrmContactRow.conversation_id == session_id))
                is None
            )
            assert {row.id for row in db.scalars(select(CrmOutboxRow)).all()} == prior_job_ids


@pytest.mark.parametrize(
    "arguments",
    (
        {
            "decision": "affirmative",
            "evidence": "invented permission",
            "contact_span": "grounding@example.com",
        },
        {"decision": "ambiguous", "evidence": "", "contact_span": ""},
    ),
)
def test_v2_contact_classifier_must_be_affirmative_and_exactly_grounded(
    monkeypatch, arguments: dict[str, str]
) -> None:
    class UngroundedClient(_SiteClient):
        def complete(self, **kwargs):  # noqa: ANN003
            tools = kwargs.get("tools") or []
            if any(
                tool.get("function", {}).get("name") == "classify_contact_consent"
                for tool in tools
            ):
                return LlmResponse(
                    "",
                    (ToolCall("consent-bad", "classify_contact_consent", arguments, "{}"),),
                    "stop",
                    "",
                    0,
                    0,
                    {"role": "assistant", "content": None},
                )
            return super().complete(**kwargs)

    monkeypatch.setattr(
        "app.surfaces.site_v2.build_site_client", lambda _settings: UngroundedClient()
    )
    with TestClient(app) as client:
        session_id, credential = _new(client)
        response = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={
                "text": "My email is grounding@example.com",
                "client_message_id": "grounding-1",
            },
            headers=_headers(credential),
        )
        assert response.status_code == 200, response.text
        assert response.json()["next_action"] == "answer"
        with get_session_factory()() as db:
            assert (
                db.scalar(select(CrmContactRow).where(CrmContactRow.conversation_id == session_id))
                is None
            )


@pytest.mark.parametrize(
    "invitation,contact_text",
    (
        ("What is your email so Assaf can follow up?", "My email is dana@example.com"),
        ("מה המייל שלך כדי שאסף יוכל לחזור אלייך?", "המייל שלי dana@example.com"),
        ("What is your email so Assaf can follow up?", "dana@example.com"),
        ("מה המייל שלך כדי שאסף יוכל לחזור אלייך?", "dana@example.com"),
    ),
)
def test_v2_contact_response_to_real_invitation_is_captured(
    monkeypatch, invitation: str, contact_text: str
) -> None:
    monkeypatch.setenv("MIA_WEBSITE_V2_ENABLED", "true")
    monkeypatch.setenv("MIA_CRM_V2_ENABLED", "true")

    class InvitationClient(_SiteClient):
        def __init__(self):
            super().__init__()
            self.narrative_verdicts[invitation] = "safe"

        def complete(self, **kwargs):  # noqa: ANN003
            response = super().complete(**kwargs)
            if self.calls == 1 and not response.tool_calls:
                return LlmResponse(
                    invitation,
                    (),
                    "stop",
                    "",
                    0,
                    0,
                    {"role": "assistant", "content": invitation},
                )
            return response

    fake = InvitationClient()
    monkeypatch.setattr("app.surfaces.site_v2.build_site_client", lambda _settings: fake)
    with TestClient(app) as client:
        session_id, credential = _new(client)
        first = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "I want help", "client_message_id": "invite-1"},
            headers=_headers(credential),
        )
        assert first.status_code == 200
        captured = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": contact_text, "client_message_id": "invite-2"},
            headers=_headers(credential),
        )
        assert captured.status_code == 200, captured.text
        assert captured.json()["next_action"] == "contact_saved"
        assert json.loads(
            fake.consent_prompts[-1].split("PRIOR_INVITATION=", 1)[1].split("\n", 1)[0]
        ) == invitation


@pytest.mark.parametrize(
    "claim",
    (
        "Your details were saved and delivered to Assaf.",
        "שמרתי את הפרטים והעברתי לאסף.",
        "שמרתי את המייל שלך.",
        "העברתי לו את המספר שלך.",
        "Your number has been saved.",
    ),
)
def test_v2_rejects_model_effect_claim_without_contact(monkeypatch, claim: str) -> None:
    class ClaimingClient(_SiteClient):
        def __init__(self):
            super().__init__()
            self.narrative_verdicts[claim] = "effect_claim"

        def complete(self, **kwargs):  # noqa: ANN003
            if _has_tool(kwargs, "validate_site_narrative"):
                return super().complete(**kwargs)
            self.calls += 1
            return LlmResponse(
                claim,
                (),
                "stop",
                "",
                0,
                0,
                {"role": "assistant", "content": "unsupported effect claim"},
            )

    fake = ClaimingClient()
    monkeypatch.setattr("app.surfaces.site_v2.build_site_client", lambda _settings: fake)
    with TestClient(app) as client:
        session_id, credential = _new(client)
        response = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "Hello", "client_message_id": "claim-1"},
            headers=_headers(credential),
        )
        assert response.status_code == 200, response.text
        assert response.json()["delivery_status"] == "none"
        assert response.json()["message"] == "How can I help?"
        assert fake.validation_calls == fake.calls == 2
        with get_session_factory()() as db:
            assert (
                db.scalar(select(CrmContactRow).where(CrmContactRow.conversation_id == session_id))
                is None
            )


def test_v2_regenerates_false_effect_claim_and_preserves_useful_answer(monkeypatch) -> None:
    class CorrectingClient(_SiteClient):
        def __init__(self):
            super().__init__()
            self.narrative_verdicts.update({
                "I saved them and sent them to Assaf.": "effect_claim",
                "We can automate email responses for your studio.": "safe",
            })

        def complete(self, **kwargs):  # noqa: ANN003
            if _has_tool(kwargs, "validate_site_narrative"):
                return super().complete(**kwargs)
            self.calls += 1
            if self.calls > 1:
                assert kwargs["tools"] == []
                assert kwargs["tool_choice"] == "none"
            text = (
                "I saved them and sent them to Assaf."
                if self.calls == 1
                else "We can automate email responses for your studio."
            )
            return LlmResponse(text, (), "stop", "", 0, 0, {"role": "assistant", "content": text})

    fake = CorrectingClient()
    monkeypatch.setattr("app.surfaces.site_v2.build_site_client", lambda _settings: fake)
    with TestClient(app) as client:
        session_id, credential = _new(client)
        response = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "What can you automate?", "client_message_id": "rewrite-1"},
            headers=_headers(credential),
        )
        assert response.status_code == 200, response.text
        assert response.json()["message"] == "We can automate email responses for your studio."
        assert response.json()["delivery_status"] == "none"
        assert fake.calls == 2
        assert fake.validation_calls == 2


def test_v2_delivery_questions_use_current_session_outbox_state(monkeypatch) -> None:
    monkeypatch.setenv("MIA_TELEGRAM_OWNER_USER_IDS", "123")
    monkeypatch.setattr("app.surfaces.site_v2.build_site_client", lambda _settings: _SiteClient())
    with TestClient(app) as client:
        session_id, credential = _new(client)
        captured = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={
                "text": "Please contact me at status@example.com",
                "client_message_id": "status-capture",
            },
            headers=_headers(credential),
        )
        assert captured.status_code == 200, captured.text
        assert captured.json()["delivery_status"] == "pending"
        assert "still pending" in captured.json()["message"]

        with get_session_factory()() as db:
            contact = db.scalar(
                select(CrmContactRow).where(CrmContactRow.conversation_id == session_id)
            )
            assert contact is not None
            contact_id = contact.id
            job = db.scalar(
                select(CrmOutboxRow).where(
                    CrmOutboxRow.aggregate_id == contact_id,
                    CrmOutboxRow.destination == "telegram",
                )
            )
            assert job is not None
            job.status = "confirmed"
            db.commit()

        confirmed = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "Were my details delivered?", "client_message_id": "status-confirmed"},
            headers=_headers(credential),
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["delivery_status"] == "confirmed"
        assert "delivery to Assaf is confirmed" in confirmed.json()["message"]

        with get_session_factory()() as db:
            job = db.scalar(
                select(CrmOutboxRow).where(
                    CrmOutboxRow.aggregate_id == contact_id,
                    CrmOutboxRow.destination == "telegram",
                )
            )
            assert job is not None
            job.status = "failed"
            db.commit()

        failed = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "What is my delivery status?", "client_message_id": "status-failed"},
            headers=_headers(credential),
        )
        assert failed.status_code == 200, failed.text
        assert failed.json()["delivery_status"] == "failed"
        assert "delivery to Assaf failed" in failed.json()["message"]


def test_v2_delivery_status_is_scoped_to_session_when_contact_is_shared(monkeypatch) -> None:
    monkeypatch.setenv("MIA_TELEGRAM_OWNER_USER_IDS", "123")
    monkeypatch.setattr("app.surfaces.site_v2.build_site_client", lambda _settings: _SiteClient())
    with TestClient(app) as client:
        session_a, credential_a = _new(client)
        session_b, credential_b = _new(client)
        for session_id, credential, message_id in (
            (session_a, credential_a, "shared-a"),
            (session_b, credential_b, "shared-b"),
        ):
            response = client.post(
                f"/v1/website/sessions/{session_id}/messages",
                json={
                    "text": "Contact me at shared-status@example.com",
                    "client_message_id": message_id,
                },
                headers=_headers(credential),
            )
            assert response.status_code == 200, response.text

        with get_session_factory()() as db:
            jobs = list(
                db.scalars(
                    select(CrmOutboxRow).where(CrmOutboxRow.destination == "telegram")
                ).all()
            )
            job_a = next(job for job in jobs if session_a in job.dedupe_key)
            job_b = next(job for job in jobs if session_b in job.dedupe_key)
            job_a.status = "pending"
            job_b.status = "confirmed"
            db.commit()

        status_b = client.post(
            f"/v1/website/sessions/{session_b}/messages",
            json={"text": "Were my details delivered?", "client_message_id": "shared-status-b"},
            headers=_headers(credential_b),
        )
        assert status_b.status_code == 200, status_b.text
        assert status_b.json()["delivery_status"] == "confirmed"
        assert "delivery to Assaf is confirmed" in status_b.json()["message"]


def test_v2_session_all_operations_authenticate_and_resume(
    monkeypatch,
) -> None:
    monkeypatch.setattr("app.surfaces.site_v2.build_site_client", lambda _settings: _SiteClient())
    with TestClient(app) as client:
        session_id, credential = _new(client)
        for suffix, kwargs in (
            ("events", {"json": {"kind": "page_viewed", "path": "/"}}),
            ("handoff", {}),
            ("end", {}),
        ):
            denied = client.post(f"/v1/website/sessions/{session_id}/{suffix}", **kwargs)
            assert denied.status_code == 401
        accepted = client.post(
            f"/v1/website/sessions/{session_id}/events",
            json={"kind": "page_viewed", "path": "/"},
            headers=_headers(credential),
        )
        assert accepted.status_code == 200
        ended = client.post(f"/v1/website/sessions/{session_id}/end", headers=_headers(credential))
        assert ended.status_code == 200
        resumed = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "נמשיך מאיפה שעצרנו", "client_message_id": "after-reload"},
            headers=_headers(credential),
        )
        assert resumed.status_code == 200


def test_legacy_session_id_requires_a_new_credentialed_session() -> None:
    with get_session_factory()() as db:
        LeadStore(db).open_website_session("web_legacy_without_credential")
        db.commit()
    with TestClient(app) as client:
        response = client.post(
            "/v1/website/sessions/web_legacy_without_credential/messages",
            json={"text": "continue", "client_message_id": "legacy-1"},
        )
        assert response.status_code == 401
        assert response.json()["detail"] == ("session credential required; start a new session")


def test_v2_voice_rejects_missing_credential_before_transcription(monkeypatch) -> None:
    monkeypatch.setenv("MIA_WEBSITE_V2_ENABLED", "true")
    transcriber = FakeTranscriptionPort("שלום")
    app.dependency_overrides[get_transcription_port] = lambda: transcriber
    try:
        with TestClient(app) as client:
            session_id, _credential = _new(client)
            denied = client.post(
                f"/v1/website/sessions/{session_id}/voice",
                data={"client_message_id": "voice-1"},
                files={"file": ("note.webm", b"voice", "audio/webm")},
            )
            assert denied.status_code == 401
            assert transcriber.call_count == 0
    finally:
        app.dependency_overrides.pop(get_transcription_port, None)


def test_rolled_back_claim_allows_retry_after_restart(monkeypatch) -> None:
    monkeypatch.setenv("MIA_WEBSITE_V2_ENABLED", "true")
    fake = _SiteClient()
    monkeypatch.setattr("app.surfaces.site_v2.build_site_client", lambda _settings: fake)
    with TestClient(app) as client:
        session_id, credential = _new(client)
        with get_session_factory()() as db:
            from app.surfaces.site_v2 import begin_site_message

            state, replay = begin_site_message(
                db,
                session_id=session_id,
                credential=credential,
                client_message_id="retryable",
                payload={"text": "hello"},
            )
            assert state is not None and replay is None
            db.rollback()
        retry = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "hello", "client_message_id": "retryable"},
            headers=_headers(credential),
        )
        assert retry.status_code == 200
        assert fake.calls == 1


@pytest.mark.parametrize("ending", ["", ".", ". Thanks!", ", please", "!"])
def test_email_contact_accepts_sentence_punctuation(monkeypatch, ending: str) -> None:
    fake = _SiteClient()
    monkeypatch.setattr("app.surfaces.site_v2.build_site_client", lambda _: fake)
    with TestClient(app) as client:
        session_id, credential = _new(client)
        response = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            headers=_headers(credential),
            json={"text": "Contact me at dana@example.com" + ending, "client_message_id": "email"},
        )
        assert response.status_code == 200
        assert response.json()["next_action"] == "contact_saved"
        with get_session_factory()() as db:
            contact = db.scalar(
                select(CrmContactRow).where(CrmContactRow.conversation_id == session_id)
            )
            assert contact is not None
            assert json.loads(contact.fields_json)["email"] == "dana@example.com"


@pytest.mark.parametrize("email", [
    "dana@example..com", "dana@-example.com", "dana@example-.com",
    "dana@example.com.invalid_", "dana@example.com..", "dana@example.com@other",
])
def test_email_extraction_does_not_capture_partial_invalid_domain(email: str) -> None:
    from app.surfaces.site_v2 import SiteV2State, _actual_contact

    fake = _SiteClient()
    result = _actual_contact(
        SiteV2State(), client=fake, text="Contact me at " + email,
        name="", phone="", email="", date="",
    )
    assert result == {}
    assert fake.consent_prompts == []


class _StagedConsent(_SiteClient):
    """Consent classifier whose verdict is scripted turn by turn."""

    def __init__(self, *decisions: str) -> None:
        super().__init__()
        self.decisions = list(decisions)

    def complete(self, **kwargs):  # noqa: ANN003
        if not _has_tool(kwargs, "classify_contact_consent"):
            return super().complete(**kwargs)
        prompt = kwargs["messages"][0]["content"]
        self.consent_prompts.append(prompt)
        current = json.loads(prompt.split("CURRENT_INPUT=", 1)[1].split("\n", 1)[0])
        contact = json.loads(prompt.split("SERVER_EXTRACTED_CONTACT=", 1)[1])
        decision = self.decisions.pop(0)
        return LlmResponse(
            "",
            (
                ToolCall(
                    "consent-1",
                    "classify_contact_consent",
                    {
                        "decision": decision,
                        "evidence": current,
                        "contact_span": contact if decision == "affirmative" else "",
                    },
                    "{}",
                ),
            ),
            "stop", "", 0, 0, {"role": "assistant", "content": None},
        )


def test_a_confirmation_carrying_no_number_still_captures_the_contact() -> None:
    """Mia reads the contact back and the visitor confirms.

    Reproduces a production dead end: capture only ever inspected the current message,
    so the confirmation Mia herself asked for could never complete it and the lead was
    lost however clearly the visitor consented.
    """
    from app.surfaces.site_v2 import SiteV2State, _actual_contact

    client = _StagedConsent("ambiguous", "affirmative")
    state = SiteV2State()
    assert _actual_contact(
        state, client=client, text="המספר שלי 0501234567",
        name="", phone="", email="", date="",
    ) == {}
    assert state.pending_contact == {"phone": "0501234567"}

    state.turns.append({"role": "mia", "text": "לאשר יצירת קשר בטלפון 0501234567?"})
    result = _actual_contact(
        state, client=client, text="מאשר שיתקשרו אליי",
        name="", phone="", email="", date="",
    )
    assert result.get("phone") == "0501234567"


def test_a_bare_confirmation_without_a_readback_never_captures() -> None:
    """Without Mia quoting the contact back there is nothing the visitor agreed to."""
    from app.surfaces.site_v2 import SiteV2State, _actual_contact

    client = _StagedConsent("affirmative")
    state = SiteV2State(pending_contact={"phone": "0501234567"})
    state.turns.append({"role": "mia", "text": "איך אפשר לעזור?"})
    assert _actual_contact(
        state, client=client, text="כן", name="", phone="", email="", date="",
    ) == {}
    assert client.consent_prompts == []


@pytest.mark.parametrize("failure", [
    "uncertain", "no_call", "wrong_tool", "wrong_text", "extra_field",
    "nonempty_safe_evidence", "invalid_decision", "two_calls", "refusal", "provider_error",
])
def test_narrative_validator_rejects_uncertain_or_unbound_output(failure: str) -> None:
    from app.surfaces.site_v2 import _narrative_is_safe

    proposed = "A useful sales explanation."

    class Validator:
        def enabled(self):
            return True

        def complete(self, **kwargs):
            assert [tool["function"]["name"] for tool in kwargs["tools"]] == [
                "validate_site_narrative"
            ]
            assert kwargs["tool_choice"] == "required"
            assert json.loads(kwargs["messages"][-1]["content"])["proposed_reply"] == proposed
            if failure == "provider_error":
                raise LlmError("synthetic failure")
            response = _validation_response(proposed, "safe")
            call = response.tool_calls[0]
            arguments = dict(call.arguments)
            if failure == "uncertain":
                arguments["decision"] = "uncertain"
            elif failure == "wrong_text":
                arguments["reviewed_text"] = "An unrelated safe sentence."
            elif failure == "extra_field":
                arguments["action"] = "submit_lead"
            elif failure == "nonempty_safe_evidence":
                arguments["evidence"] = proposed
            elif failure == "invalid_decision":
                arguments["decision"] = True
            call = call._replace(arguments=arguments)
            if failure == "wrong_tool":
                call = call._replace(name="submit_lead")
            calls = (
                () if failure == "no_call" else (call, call) if failure == "two_calls" else (call,)
            )
            return response._replace(
                tool_calls=calls, refusal="refused" if failure == "refusal" else ""
            )

    assert not _narrative_is_safe(Validator(), reply=proposed, visitor_text="Hi")


def test_regeneration_cannot_request_an_action() -> None:
    from app.surfaces.site_v2 import _validated_narrative

    class Validator:
        def enabled(self):
            return True

        def complete(self, **kwargs):
            if _has_tool(kwargs, "validate_site_narrative"):
                return _validation_response("Uncertain answer", "uncertain")
            assert kwargs["tools"] == [] and kwargs["tool_choice"] == "none"
            return LlmResponse(
                "Looks safe",
                (ToolCall("bad", "submit_lead", {"email": "wrong@example.com"}, "{}"),),
                "stop", "", 0, 0, {},
            )

    assert _validated_narrative(
        Validator(), reply="Uncertain answer", visitor_text="Hello", messages=[]
    ) == ""


def test_model_budget_is_shared_across_validation_and_regeneration(monkeypatch) -> None:
    from app.surfaces.site_v2 import _SiteTurnClient

    clock = [100.0]
    observed = []
    monkeypatch.setattr("app.surfaces.site_v2.monotonic", lambda: clock[0])

    class Provider:
        def complete(self, **kwargs):
            observed.append(kwargs["timeout"])
            return _validation_response("safe", "safe")

    client = _SiteTurnClient(Provider(), timeout=10)
    client.complete(messages=[])
    clock[0] = 107.0
    client.complete(messages=[])
    clock[0] = 110.0
    with pytest.raises(LlmError, match="deadline"):
        client.complete(messages=[])
    assert observed == [10.0, 3.0]
