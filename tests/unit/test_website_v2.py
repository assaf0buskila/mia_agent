import json

import pytest
from app.api.deps import get_transcription_port
from app.db.models import CrmActivityRow, CrmContactRow, CrmOutboxRow
from app.db.session import get_session_factory
from app.db.site_v2 import SiteV2SessionRow
from app.db.store import LeadStore
from app.integrations.llm_client import (
    OPENAI_RESPONSES_URL,
    LlmClient,
    LlmError,
    LlmResponse,
    ToolCall,
)
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
            # The instructions are a separate system message from the actual data; the
            # data (what a real classifier reads) is always the last message.
            prompt = kwargs["messages"][-1]["content"]
            assert kwargs["messages"][0]["role"] == "system"
            assert kwargs["messages"][-1]["role"] == "user", (
                "a system-only message list becomes an empty Responses `input` and 400s"
            )
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
        assert kwargs["messages"][-1]["role"] == "user"
        prompt = kwargs["messages"][-1]["content"]
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


@pytest.mark.parametrize("span", ["052-7654321", "0527654321", "052 765 4321", "ל-052-7654321"])
def test_a_reformatted_contact_span_still_counts_as_consent(span: str) -> None:
    """Models normalise phone formatting; byte-equality was losing real leads."""
    from app.surfaces.site_v2 import _span_covers_contact

    text = "אני רונית, הטלפון שלי 052-7654321"
    assert _span_covers_contact(span, "052-7654321", text=text) is True


@pytest.mark.parametrize("span", ["", "0509999999", "050-9999999", "someone@else.com"])
def test_a_span_pointing_at_another_contact_is_rejected(span: str) -> None:
    """Tolerating formatting must not tolerate the model substituting a different value."""
    from app.surfaces.site_v2 import _span_covers_contact

    text = "אני רונית, הטלפון שלי 052-7654321"
    assert _span_covers_contact(span, "052-7654321", text=text) is False


class _TruncatingConsent(_SiteClient):
    """Provider exhausted max_output_tokens while reasoning: no prose, no tool call.

    This is exactly what the Responses adapter returns for status=incomplete, and it
    is not an error, so the model chain does not fall back.
    """

    def __init__(self) -> None:
        super().__init__()
        self.requested_budget: int | None = None

    def complete(self, **kwargs):  # noqa: ANN003
        if not _has_tool(kwargs, "classify_contact_consent"):
            return super().complete(**kwargs)
        self.requested_budget = kwargs.get("max_completion_tokens")
        return LlmResponse("", (), "length", "", 120, 180, {"role": "assistant"})


def test_a_truncated_consent_verdict_fails_closed_and_is_visible() -> None:
    from app.surfaces.site_v2 import SiteV2State, _actual_contact

    client = _TruncatingConsent()
    result = _actual_contact(
        SiteV2State(), client=client, text="אני רונית, הטלפון שלי 052-7654321",
        name="", phone="", email="", date="",
    )
    assert result == {}, "a truncated classification must never be read as consent"


def test_the_consent_classifier_requests_headroom_for_reasoning_tokens() -> None:
    """Regression for the production dead end where every free-text lead was lost.

    On the Responses API, max_output_tokens bounds reasoning and visible output
    together. A budget of 180 was consumed entirely by reasoning, so the classifier
    never emitted its tool call and every verdict silently became "ambiguous". The
    guard is on what is actually requested at the call boundary, not on a constant.
    """
    from app.surfaces.site_v2 import SiteV2State, _actual_contact

    client = _TruncatingConsent()
    _actual_contact(
        SiteV2State(), client=client, text="אני רונית, הטלפון שלי 052-7654321",
        name="", phone="", email="", date="",
    )
    assert client.requested_budget is not None
    assert client.requested_budget >= 512


def test_a_dictated_phone_number_is_extracted_from_number_words() -> None:
    """Transcription renders spoken digits as words; capture must still find them.

    The transcript below is verbatim from a live voice test against production, where
    the number was never captured because no digits ever reached the phone regex.
    """
    from app.surfaces.site_v2 import SiteV2State, _actual_contact, _spoken_digits_to_numerals

    heard = (
        "Hi, I run a dental clinic in Tel Aviv. My phone number is zero five two, "
        "one one one, two two three three. Please call me back."
    )
    assert "0521112233" in _spoken_digits_to_numerals(heard)
    hebrew = "הטלפון שלי אפס חמש שתיים אחת אחת אחת שתיים שתיים שלוש שלוש"
    assert "0521112233" in _spoken_digits_to_numerals(hebrew)
    prose = "we offer two options and three plans, one of them free"
    assert _spoken_digits_to_numerals(prose) == prose

    client = _StagedConsent("affirmative")
    result = _actual_contact(
        SiteV2State(), client=client, text=heard, name="", phone="", email="", date="",
    )
    assert result.get("phone") == "0521112233"


def test_a_span_quoting_the_spoken_words_still_covers_the_extracted_number() -> None:
    from app.surfaces.site_v2 import _span_covers_contact

    heard = "My phone number is zero five two, one one one, two two three three."
    span = "zero five two, one one one, two two three three"
    assert _span_covers_contact(span, "0521112233", text=heard) is True


@pytest.mark.parametrize("invitation", [
    "השאירו טלפון או אימייל ונחזור אליכם",
    "אם תרצי, אפשר גם להשאיר טלפון או אימייל ואסף יחזור אלייך.",
    "אם תרצה, השאר טלפון או אימייל ואפשר יהיה להמשיך משם",
])
def test_the_invite_regex_matches_how_mia_actually_phrases_it(invitation: str) -> None:
    """Verbatim invitations from live production testing.

    The system prompt only tells the model to invite contact in one natural sentence;
    it does not fix the wording. The original three fixed phrases matched none of
    these, so prior_invitation was empty and a bare confirmation reply had to clear a
    higher bar than a visitor who had just been invited should have to.
    """
    from app.surfaces.site_v2 import _CONTACT_INVITE

    assert _CONTACT_INVITE.search(invitation)


def test_the_consent_call_never_becomes_an_empty_responses_input() -> None:
    """Root cause of every website lead being dropped in production.

    `_classified_consent` sent one system-role message and nothing else. The
    Responses adapter routes `role: "system"` content into `instructions` and never
    into `input` (`app/integrations/llm_client.py:_responses_payload`), so that
    request's `input` array was empty - which the Responses API rejects before any
    generation happens. This is why raising the token budget in a prior fix attempt
    changed nothing: the call never got far enough to run out of tokens. This test
    builds the real payload through the real adapter method, not a fake, because a
    hand-written fake client would happily accept a system-only message list and
    hide exactly this bug - which is what let it ship in the first place.
    """
    from app.surfaces.site_v2 import SiteV2State, _actual_contact

    captured: list[list[dict]] = []

    class RecordingClient(_SiteClient):
        def complete(self, **kwargs):  # noqa: ANN003
            if _has_tool(kwargs, "classify_contact_consent"):
                captured.append(kwargs["messages"])
            return super().complete(**kwargs)

    _actual_contact(
        SiteV2State(), client=RecordingClient(), text="אני רונית, הטלפון שלי 052-7654321",
        name="", phone="", email="", date="",
    )
    assert captured, "expected the consent classifier to be called"
    messages = captured[0]
    assert any(m.get("role") == "user" for m in messages), (
        "a system-only message list becomes an empty Responses `input`"
    )

    adapter = LlmClient(
        api_key="test-key", model="test-model", url=OPENAI_RESPONSES_URL,
        reasoning_effort="low",
    )
    payload = adapter._responses_payload(  # noqa: SLF001 - the real request shape is the point
        messages=messages, tools=None, tool_choice=None,
        parallel_tool_calls=None, max_completion_tokens=None, response_format=None,
    )
    assert payload["input"], "the Responses API rejects an empty input array"


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


def test_greeting_first_then_real_need_populates_business_and_need_separately(
    monkeypatch,
) -> None:
    """Chunk C3a defect 1+2: a bare greeting is never latched as the business, and the
    brief's business/need sections come from the visitor's real statements."""
    monkeypatch.setenv("MIA_WEBSITE_V2_ENABLED", "true")
    monkeypatch.setenv("MIA_CRM_V2_ENABLED", "true")
    monkeypatch.setenv("MIA_TELEGRAM_OWNER_USER_IDS", "123")
    fake = _SiteClient()
    monkeypatch.setattr("app.surfaces.site_v2.build_site_client", lambda _settings: fake)
    with TestClient(app) as client:
        session_id, credential = _new(client)
        greeting = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "היי מהקורה", "client_message_id": "greet-1"},
            headers=_headers(credential),
        )
        assert greeting.status_code == 200
        with get_session_factory()() as db:
            state = json.loads(db.get(SiteV2SessionRow, session_id).state_json)
            assert state["business_context"] == ""

        business = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "יש לי סטודיו פילאטיס בתל אביב", "client_message_id": "biz-1"},
            headers=_headers(credential),
        )
        assert business.status_code == 200
        with get_session_factory()() as db:
            state = json.loads(db.get(SiteV2SessionRow, session_id).state_json)
            assert state["business_context"] == "יש לי סטודיו פילאטיס בתל אביב"

        captured = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={
                "text": "אני צריך עזרה בניהול לוח זמנים ותורים, תחזרו אליי בבקשה",
                "phone": "052-1112222",
                "client_message_id": "need-1",
            },
            headers=_headers(credential),
        )
        assert captured.status_code == 200, captured.text
        assert captured.json()["next_action"] == "contact_saved"

        with get_session_factory()() as db:
            contact = db.scalar(
                select(CrmContactRow).where(CrmContactRow.conversation_id == session_id)
            )
            assert contact is not None
            job = db.scalar(
                select(CrmOutboxRow).where(
                    CrmOutboxRow.aggregate_id == contact.id,
                    CrmOutboxRow.destination == "telegram",
                )
            )
            assert job is not None
            summary_text = json.loads(job.payload_json)["text"]
            assert "העסק: יש לי סטודיו פילאטיס בתל אביב" in summary_text
            assert "הצורך:" in summary_text
            assert "לוח זמנים" in summary_text
            assert "היי מהקורה" not in summary_text
            assert summary_text.count("יש לי סטודיו פילאטיס בתל אביב") == 1


def test_lead_summary_never_repeats_a_sentence_and_omits_the_need_when_unknown(
    monkeypatch,
) -> None:
    """Chunk C3a defect 2: production showed the same sentence three times over."""
    monkeypatch.setenv("MIA_WEBSITE_V2_ENABLED", "true")
    monkeypatch.setenv("MIA_CRM_V2_ENABLED", "true")
    monkeypatch.setenv("MIA_TELEGRAM_OWNER_USER_IDS", "123")
    fake = _SiteClient()
    monkeypatch.setattr("app.surfaces.site_v2.build_site_client", lambda _settings: fake)
    text = "יש לי סטודיו פילאטיס בתל אביב"
    with TestClient(app) as client:
        session_id, credential = _new(client)
        first = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": text, "client_message_id": "first-1"},
            headers=_headers(credential),
        )
        assert first.status_code == 200

        captured = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": text, "phone": "052-3334444", "client_message_id": "second-1"},
            headers=_headers(credential),
        )
        assert captured.status_code == 200, captured.text
        assert captured.json()["next_action"] == "contact_saved"

        with get_session_factory()() as db:
            contact = db.scalar(
                select(CrmContactRow).where(CrmContactRow.conversation_id == session_id)
            )
            job = db.scalar(
                select(CrmOutboxRow).where(
                    CrmOutboxRow.aggregate_id == contact.id,
                    CrmOutboxRow.destination == "telegram",
                )
            )
            summary_text = json.loads(job.payload_json)["text"]
            assert summary_text.count("יש לי סטודיו פילאטיס בתל אביב") == 1
            assert "הצורך:" not in summary_text
            assert "השלב הבא המומלץ:" in summary_text


class _SubmitLeadClient(_SiteClient):
    """Returns a ``submit_lead`` tool call on the first reply-generation turn."""

    def __init__(self, *, next_step: str, name) -> None:
        super().__init__()
        self._next_step = next_step
        self._name = name
        self._returned = False

    def complete(self, **kwargs):  # noqa: ANN003
        if not self._returned and _has_tool(kwargs, "submit_lead"):
            self._returned = True
            self.calls += 1
            self.prompts.append(kwargs["messages"])
            arguments = {
                "name": self._name,
                "phone": None,
                "email": None,
                "next_step": self._next_step,
            }
            return LlmResponse(
                "",
                (ToolCall("lead-1", "submit_lead", arguments, "{}"),),
                "stop",
                "",
                0,
                0,
                {"role": "assistant", "content": None},
            )
        return super().complete(**kwargs)


def test_submit_lead_same_turn_reaches_the_single_pending_job(monkeypatch) -> None:
    """Chunk C3a defect 3: a same-turn submit_lead call must reach the built brief."""
    monkeypatch.setenv("MIA_WEBSITE_V2_ENABLED", "true")
    monkeypatch.setenv("MIA_CRM_V2_ENABLED", "true")
    monkeypatch.setenv("MIA_TELEGRAM_OWNER_USER_IDS", "123")
    fake = _SubmitLeadClient(next_step="לתאם שיחת ייעוץ", name="דנה")
    monkeypatch.setattr("app.surfaces.site_v2.build_site_client", lambda _settings: fake)
    with TestClient(app) as client:
        session_id, credential = _new(client)
        captured = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={
                "text": "קוראים לי דנה, תחזרו אליי בבקשה",
                "phone": "052-5556666",
                "client_message_id": "lead-1",
            },
            headers=_headers(credential),
        )
        assert captured.status_code == 200, captured.text
        assert captured.json()["next_action"] == "contact_saved"

        with get_session_factory()() as db:
            contact = db.scalar(
                select(CrmContactRow).where(CrmContactRow.conversation_id == session_id)
            )
            assert contact is not None
            jobs = list(
                db.scalars(
                    select(CrmOutboxRow).where(
                        CrmOutboxRow.aggregate_id == contact.id,
                        CrmOutboxRow.destination == "telegram",
                    )
                ).all()
            )
            assert len(jobs) == 1
            payload_text = json.loads(jobs[0].payload_json)["text"]
            assert "לתאם שיחת ייעוץ" in payload_text
            assert "דנה" in payload_text
            fields = json.loads(contact.fields_json)
            assert fields.get("next_step") == "לתאם שיחת ייעוץ"
            assert fields.get("name") == "דנה"
            activity = db.scalar(
                select(CrmActivityRow).where(CrmActivityRow.contact_id == contact.id)
            )
            assert activity is not None
            assert activity.result == payload_text
            # Review fix (P1): refreshing next_step/name bumps the contact's
            # revision, so a fresh Contacts Sheet sync job must exist at that new
            # revision, or the Sheet worker permanently conflicts the projection.
            assert contact.revision == 2
            contacts_job = db.scalar(
                select(CrmOutboxRow).where(
                    CrmOutboxRow.aggregate_id == contact.id,
                    CrmOutboxRow.destination == "contacts",
                    CrmOutboxRow.dedupe_key == f"contacts:{contact.id}:{contact.revision}",
                )
            )
            assert contacts_job is not None
            assert contacts_job.status == "pending"
            contacts_payload = json.loads(contacts_job.payload_json)
            assert contacts_payload["revision"] == contact.revision


def test_submit_lead_name_not_in_visitor_text_is_not_applied(monkeypatch) -> None:
    """A model-supplied name absent from the visitor's own words is never saved."""
    monkeypatch.setenv("MIA_WEBSITE_V2_ENABLED", "true")
    monkeypatch.setenv("MIA_CRM_V2_ENABLED", "true")
    monkeypatch.setenv("MIA_TELEGRAM_OWNER_USER_IDS", "123")
    fake = _SubmitLeadClient(next_step="לתאם שיחת ייעוץ", name="דנה")
    monkeypatch.setattr("app.surfaces.site_v2.build_site_client", lambda _settings: fake)
    with TestClient(app) as client:
        session_id, credential = _new(client)
        captured = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={
                "text": "תחזרו אליי בבקשה",
                "phone": "052-7778888",
                "client_message_id": "lead-1",
            },
            headers=_headers(credential),
        )
        assert captured.status_code == 200, captured.text
        with get_session_factory()() as db:
            contact = db.scalar(
                select(CrmContactRow).where(CrmContactRow.conversation_id == session_id)
            )
            fields = json.loads(contact.fields_json)
            assert fields.get("name") == ""
            assert fields.get("next_step") == "לתאם שיחת ייעוץ"
            job = db.scalar(
                select(CrmOutboxRow).where(
                    CrmOutboxRow.aggregate_id == contact.id,
                    CrmOutboxRow.destination == "telegram",
                )
            )
            payload_text = json.loads(job.payload_json)["text"]
            assert "דנה" not in payload_text
            assert "לתאם שיחת ייעוץ" in payload_text


def test_model_error_after_capture_leaves_original_job_and_contact_unchanged(
    monkeypatch,
) -> None:
    """A model failure after capture must not disturb the already-committed brief."""
    monkeypatch.setenv("MIA_WEBSITE_V2_ENABLED", "true")
    monkeypatch.setenv("MIA_CRM_V2_ENABLED", "true")
    monkeypatch.setenv("MIA_TELEGRAM_OWNER_USER_IDS", "123")

    class RaisingClient(_SiteClient):
        def complete(self, **kwargs):  # noqa: ANN003
            if _has_tool(kwargs, "submit_lead"):
                raise LlmError("synthetic failure after capture")
            return super().complete(**kwargs)

    fake = RaisingClient()
    monkeypatch.setattr("app.surfaces.site_v2.build_site_client", lambda _settings: fake)
    with TestClient(app) as client:
        session_id, credential = _new(client)
        captured = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={
                "text": "תחזרו אליי בבקשה",
                "phone": "052-9990000",
                "client_message_id": "lead-1",
            },
            headers=_headers(credential),
        )
        assert captured.status_code == 200, captured.text
        assert captured.json()["next_action"] == "contact_saved"
        with get_session_factory()() as db:
            contact = db.scalar(
                select(CrmContactRow).where(CrmContactRow.conversation_id == session_id)
            )
            assert contact is not None
            fields = json.loads(contact.fields_json)
            assert fields.get("next_step") == "לחזור לפונה"
            job = db.scalar(
                select(CrmOutboxRow).where(
                    CrmOutboxRow.aggregate_id == contact.id,
                    CrmOutboxRow.destination == "telegram",
                )
            )
            assert job is not None
            assert job.status == "pending"
            payload_text = json.loads(job.payload_json)["text"]
            assert "השלב הבא המומלץ: לחזור לפונה" in payload_text


def test_greeting_regex_worst_case_input_stays_fast() -> None:
    """Chunk C3a review P0 round 2: the 60-char length cap alone still left input
    just under it exponential (measured ~0.5s at 60 chars on the prior commit)
    because the repeated greeting-token group could still backtrack across a
    near-match run. The token group and the separator run are now atomic/
    possessive, so even the adversarial worst case returns immediately; the cap
    is kept only as a cheap second guard, not the actual fix."""
    from time import perf_counter

    from app.surfaces.site_v2 import _GREETING_ONLY, _is_uninformative_business_text

    worst_59 = ("היי" * 20)[:59] + "ה"
    worst_59_x = ("היי" * 20)[:59] + "X"
    for text in (worst_59, worst_59_x):
        start = perf_counter()
        assert _is_uninformative_business_text(text) is False
        assert perf_counter() - start < 0.05

    # 4000 chars is well past the length cap, so call the compiled regex
    # directly -- this exercises the atomic/possessive fix itself, not the cap.
    long_text = "היי" * 1333 + "X"
    start = perf_counter()
    assert _GREETING_ONLY.fullmatch(long_text) is None
    assert perf_counter() - start < 0.05


@pytest.mark.parametrize(
    "text,expected",
    (
        ("היי מהקורה", True),
        ("מה נשמע?", True),
        ("שלום!", True),
        ("בוקר טוב!", True),
        ("מה", True),
        ("היי", True),
        ("ה", True),
        ("hello!", True),
        ("good morning", True),
        ("SEO", False),
        ("אתר", False),
        ("CRM", False),
        ("יש לי סטודיו פילאטיס בתל אביב", False),
    ),
)
def test_greeting_classification_is_unaffected_by_the_atomic_group_fix(
    text: str, expected: bool
) -> None:
    """The atomic/possessive rewrite must not change any classification outcome."""
    from app.surfaces.site_v2 import _is_uninformative_business_text

    assert _is_uninformative_business_text(text) is expected


@pytest.mark.parametrize("text", ("SEO", "אתר"))
def test_short_business_words_are_not_treated_as_uninformative(text: str) -> None:
    """Chunk C3a review P3: a real 3-letter business word is not a greeting."""
    from app.surfaces.site_v2 import _is_uninformative_business_text

    assert _is_uninformative_business_text(text) is False


@pytest.mark.parametrize("text", ("מה", "היי", "הי"))
def test_bare_short_greetings_are_still_caught_by_the_greeting_regex(text: str) -> None:
    """Lowering the fallback length threshold must not stop the regex from catching
    the short greeting words it is built for."""
    from app.surfaces.site_v2 import _is_uninformative_business_text

    assert _is_uninformative_business_text(text) is True


def test_absorb_submit_lead_rejects_single_letter_and_fragment_names() -> None:
    """Chunk C3a review P2: a name must be a whole word, at least two characters,
    verbatim in the visitor's own text -- not a single letter or a word fragment."""
    from app.surfaces.site_v2 import SiteV2State, _absorb_submit_lead

    class _Call:
        def __init__(self, arguments):
            self.arguments = arguments

    visitor_text = "קוראים לי דנה"

    state = SiteV2State()
    _absorb_submit_lead(
        state, _Call({"name": "ד", "next_step": None}), visitor_text=visitor_text
    )
    assert state.pending_name == ""

    state = SiteV2State()
    _absorb_submit_lead(
        state, _Call({"name": "נה", "next_step": None}), visitor_text=visitor_text
    )
    assert state.pending_name == ""

    state = SiteV2State()
    _absorb_submit_lead(
        state, _Call({"name": "דנה", "next_step": None}), visitor_text=visitor_text
    )
    assert state.pending_name == "דנה"

# --------------------------------------------------------------------------------------
# Regression: the pre-classifier contact veto silently lost leads in production.
#
# The guard that ran here matched bare "quote", "sample", "dont", "never" and "no need to"
# anywhere in the message, ahead of the consent classifier and ahead of the parking that
# makes a next-turn confirmation possible. Measured against realistic lead text, 8 of 11
# messages carrying a real phone or email were discarded with no record and no recovery.
# Neither detector had a single test.
# --------------------------------------------------------------------------------------

_ORDINARY_LEADS = (
    "Can I get a quote for a landing page? email me at dana@example.com",
    "I dont have a landline, my mobile is 052-7654321",
    "never mind the email, call me at 052-7654321",
    "I have never used a service like this. my email dana@example.com",
    "no need to rush, email me at dana@example.com",
    "send me a sample, my email is dana@example.com",
    "how much do you quote for a shop? 052-7654321",
    "we dont need a rewrite, just a quote - dana@example.com",
    "אפשר לקבל הצעת מחיר? המייל שלי dana@example.com",
)


@pytest.mark.parametrize("text", _ORDINARY_LEADS)
def test_an_ordinary_lead_reaches_the_consent_classifier(text: str) -> None:
    """No phrase detector may decide a contact-bearing message before the classifier.

    The assertion that matters is ``consent_prompts``: a lead that never reaches the
    classifier was thrown away by a regex, which is the defect this test exists for.
    """
    from app.surfaces.site_v2 import SiteV2State, _actual_contact

    fake = _StagedConsent("affirmative")
    result = _actual_contact(
        SiteV2State(), client=fake, text=text, name="", phone="", email="", date="",
    )
    # The load-bearing assertion: a lead that never reaches the classifier was thrown
    # away by a regex. The capture below uses a scripted affirmative rather than
    # _SiteClient's default so it asserts the server's handling, not the double's.
    assert fake.consent_prompts, "the classifier was never consulted for: " + text
    assert result.get("phone") or result.get("email"), text


@pytest.mark.parametrize("text", (
    "אני לא רוצה שתיצרו קשר. המייל שלי nofollow@example.com",
    "Do not contact me at this address. nofollow@example.com",
    "My email is refusal2@example.com, but I refuse permission to contact me.",
    "My email is refusal4@example.com, but I forbid you to contact me.",
    "never call me, email only: refusal6@example.com",
    "please don't email me, refusal7@example.com",
    "המייל שלי refusal3@example.com, אבל אני מסרבת לתת אישור ליצור איתי קשר.",
))
def test_an_explicit_refusal_blocks_capture_without_the_classifier(text: str) -> None:
    """Explicit refusal is decided deterministically, so it still holds with no model.

    This is the one thing the old veto got right and the reason it is not simply deleted:
    the classifier is instructed that refusal overrides everything, but it can be
    disabled, time out or truncate, and a refusal must survive all three.
    """
    from app.surfaces.site_v2 import SiteV2State, _actual_contact

    fake = _SiteClient()
    state = SiteV2State()
    assert _actual_contact(
        state, client=fake, text=text, name="", phone="", email="", date="",
    ) == {}
    assert fake.consent_prompts == []
    assert state.pending_contact == {}


@pytest.mark.parametrize("decision", ("refused", "quoted"))
def test_a_decided_non_consent_verdict_is_not_parked_for_a_later_turn(decision: str) -> None:
    """A refusal and a third-party contact are answers, not uncertainty.

    Parking either would let the readback path capture, one turn later, a contact the
    visitor declined or never owned.
    """
    from app.surfaces.site_v2 import SiteV2State, _actual_contact

    state = SiteV2State()
    assert _actual_contact(
        state, client=_StagedConsent(decision), text="reach me at parked@example.com",
        name="", phone="", email="", date="",
    ) == {}
    assert state.pending_contact == {}


def test_a_model_ambiguous_verdict_is_still_parked_for_a_later_turn() -> None:
    """The case parking exists for: the model looked and was genuinely unsure."""
    from app.surfaces.site_v2 import SiteV2State, _actual_contact

    state = SiteV2State()
    assert _actual_contact(
        state, client=_StagedConsent("ambiguous"), text="reach me at maybe@example.com",
        name="", phone="", email="", date="",
    ) == {}
    assert state.pending_contact == {"email": "maybe@example.com"}


def test_an_unresolved_classifier_does_not_park_the_contact() -> None:
    """A disabled client yields no verdict, and no verdict must not look like uncertainty.

    ``_classified_consent`` reports both "the model said ambiguous" and "there was no
    model" as the decision ``ambiguous``; only ``resolved`` separates them. Parking on
    the second would let a contact be captured later once the classifier recovered.
    """
    from app.surfaces.site_v2 import SiteV2State, _actual_contact

    class _Disabled(_SiteClient):
        def enabled(self) -> bool:
            return False

    state = SiteV2State()
    assert _actual_contact(
        state, client=_Disabled(), text="reach me at down@example.com",
        name="", phone="", email="", date="",
    ) == {}
    assert state.pending_contact == {}

# Added after independent review of the fix above, which found each of these.

@pytest.mark.parametrize("text", (
    "no need to call, just email me at dana@example.com",
    "there's no need to email, WhatsApp me 0501234567",
    "don't mail me the brochure, just call 0501234567",
    "you guys never called me back. my number is 0501234567",
    "I never text, email me at dana@x.com",
    "don't phone the office, phone me 0501234567",
    "is it prohibited to send a quote to dana@x.com?",
    "we work without permission slips, email dana@x.com",
))
def test_a_negated_verb_without_the_visitor_as_object_is_not_a_refusal(text: str) -> None:
    """Channel preferences and past-tense complaints are leads, not refusals.

    Every one of these was blocked by the first version of the refusal guard: it matched
    a negated contact verb with no object, so "don't phone the office" and "you guys
    never called me back" read as refusals.
    """
    from app.surfaces.site_v2 import SiteV2State, _actual_contact

    fake = _StagedConsent("affirmative")
    result = _actual_contact(
        SiteV2State(), client=fake, text=text, name="", phone="", email="", date="",
    )
    assert fake.consent_prompts, "the classifier was never consulted for: " + text
    assert result.get("phone") or result.get("email"), text


@pytest.mark.parametrize("text", (
    "I don't consent.", "you may not contact me", "I do not want to be contacted.",
    "don't get in touch with me again", "please don't follow up with me",
    "don't text or call me", "don’t call me", "without my permission",
    "בלי מייל בבקשה, 0501234567",
    "אל תתקשר אליי", "אל תחזור אליי", "אל תשלח לי",
    "אני לא מאשר שתתקשרו",
))
def test_refusal_forms_beyond_the_original_guard_are_caught(text: str) -> None:
    """Refusals the original guard missed, including every Hebrew imperative form.

    The original guard carried plural imperatives only, so "אל תתקשר" (masculine
    singular) passed; and "לא מעוניין" ends in a final nun, which a medial-nun pattern
    cannot match. Both were live gaps.
    """
    from app.surfaces.site_v2 import SiteV2State, _actual_contact

    fake = _SiteClient()
    state = SiteV2State()
    state.pending_contact = {"phone": "0501234567"}
    assert _actual_contact(
        state, client=fake, text=text, name="", phone="", email="", date="",
    ) == {}
    assert fake.consent_prompts == []
    assert state.pending_contact == {}


def test_a_refusal_carrying_no_contact_still_clears_the_parked_value() -> None:
    """The refusal that matters carries no contact of its own.

    The normal shape is: the visitor gives a number, the classifier is unsure so it is
    parked, Mia reads it back, and the refusal arrives on the next turn carrying no value.
    Clearing the park only when the refusal repeated the value left that number alive, and
    a bland acknowledgement two turns later captured it through the readback path -- a
    contact taken from a visitor who had just refused.
    """
    from app.surfaces.site_v2 import SiteV2State, _actual_contact

    state = SiteV2State()
    staged = _StagedConsent("ambiguous")
    _actual_contact(
        state, client=staged, text="my number is 0501234567",
        name="", phone="", email="", date="",
    )
    assert state.pending_contact == {"phone": "0501234567"}

    state.turns.append({"role": "mia", "text": "confirm 0501234567?"})
    assert _actual_contact(
        state, client=staged, text="no, please don't contact me",
        name="", phone="", email="", date="",
    ) == {}
    assert state.pending_contact == {}

    state.turns.append({"role": "mia", "text": "understood, I will not use 0501234567"})
    assert _actual_contact(
        state, client=_SiteClient(), text="ok thanks",
        name="", phone="", email="", date="",
    ) == {}

# Added after the second review round, which found that the fix had reintroduced the
# defect it existed to fix and had left the same parked-value hole one branch over.

@pytest.mark.parametrize("text", (
    "how do I unsubscribe from your newsletter? my email is dana@x.com",
    "no contact form on your site? email me dana@x.com",
    "there is no contact info on the site, my number is 0501234567",
    "no calls after 6pm please, but email me at dana@x.com",
    "stop calling the office line, call my mobile 0501234567",
    "take me off the waitlist and call me at 0501234567",
    "no follow-ups needed on the old ticket, but email me dana@x.com",
    "is consent required for a quote? dana@x.com",
))
def test_soft_refusal_wording_is_left_to_the_classifier(text: str) -> None:
    """Wording that only sometimes means refusal must not be decided deterministically.

    "unsubscribe", "no calls", "take me off" and "stop calling" were added to the guard
    after the first review and every one of them turned out to appear in ordinary
    messages from buying visitors -- a visitor asking how to unsubscribe while
    volunteering their email is a lead. Matching them here discarded the lead before the
    classifier was asked, which is the defect this whole change exists to fix. They are
    retired from the deterministic guard and left to the classifier, which is instructed
    that refusal overrides affirmative wording anywhere.
    """
    from app.surfaces.site_v2 import SiteV2State, _actual_contact

    fake = _StagedConsent("affirmative")
    result = _actual_contact(
        SiteV2State(), client=fake, text=text, name="", phone="", email="", date="",
    )
    assert fake.consent_prompts, "the classifier was never consulted for: " + text
    assert result.get("phone") or result.get("email"), text


def test_a_classifier_refusal_on_the_readback_turn_clears_the_parked_value() -> None:
    """The readback branch must forget a refused value, not only the other branch.

    The first fix cleared the park in the contact-bearing branch alone. A refusal the
    deterministic guard does not catch -- "no thanks, I'm not interested" -- reaches the
    classifier through the readback branch instead, is correctly classified ``refused``,
    and left the parked number alive, so the next bland turn captured it. Same defect,
    one branch over; both now settle through _settle_pending_contact.
    """
    from app.surfaces.site_v2 import SiteV2State, _actual_contact

    state = SiteV2State()
    staged = _StagedConsent("ambiguous", "refused", "affirmative")
    _actual_contact(
        state, client=staged, text="my number is 0501234567",
        name="", phone="", email="", date="",
    )
    assert state.pending_contact == {"phone": "0501234567"}

    state.turns.append({"role": "mia", "text": "confirm 0501234567?"})
    assert _actual_contact(
        state, client=staged, text="no thanks, I'm not interested",
        name="", phone="", email="", date="",
    ) == {}
    assert state.pending_contact == {}

    state.turns.append({"role": "mia", "text": "understood, I will not use 0501234567"})
    assert _actual_contact(
        state, client=staged, text="ok thanks", name="", phone="", email="", date="",
    ) == {}


def test_an_unresolved_verdict_on_the_readback_turn_clears_the_parked_value() -> None:
    """A classifier that never ran is not uncertainty, on either branch."""
    from app.surfaces.site_v2 import SiteV2State, _actual_contact

    class _Disabled(_SiteClient):
        def enabled(self) -> bool:
            return False

    state = SiteV2State()
    state.pending_contact = {"phone": "0501234567"}
    state.turns.append({"role": "mia", "text": "confirm 0501234567?"})
    assert _actual_contact(
        state, client=_Disabled(), text="yes please", name="", phone="", email="", date="",
    ) == {}
    assert state.pending_contact == {}


def test_the_consent_refusal_alternative_uses_a_real_word_boundary() -> None:
    """Guards against a corrupted escape, which is how this line shipped once.

    ``consent`` was written through a non-raw string and became a literal backspace,
    so the whole alternative silently matched nothing while the pattern still compiled
    and every other alternative kept working. Reading the regex could not show it.
    """
    from app.surfaces.site_v2 import _CONTACT_REFUSAL

    assert "" not in _CONTACT_REFUSAL.pattern
    assert _CONTACT_REFUSAL.search("I don't consent.")
    assert _CONTACT_REFUSAL.search("I do not consent.")
    assert not _CONTACT_REFUSAL.search("is consent required for a quote? dana@x.com")
