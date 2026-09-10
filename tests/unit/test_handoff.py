"""Manual WhatsApp handoff primitives retained by the v2 website."""

from app.db.session import init_db
from app.domain.handoff.tokens import (
    HANDOFF_COMPOSE_HINT_HE,
    click_to_chat_digits,
    click_to_chat_url,
    compose_handoff_text,
    extract_handoff_token,
    generate_handoff_token,
    hash_handoff_token,
    inbound_text_without_token,
)
from app.main import app
from fastapi.testclient import TestClient

CLICK_CHAT = "972500000001"


def test_generate_token_prefix_and_hash_is_sha256_hex() -> None:
    raw = generate_handoff_token()
    assert raw.startswith("mia1_")
    assert len(raw) >= 21
    digest = hash_handoff_token(raw)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_extract_token_exact_wrapped_and_with_rest() -> None:
    raw = "mia1_qkc4VDRLioNkR_3Wbx8kSKoe"
    wrapped = "mia1_qkc4VDRLioNkR_3Wbx8kS\nKoe"
    assert extract_handoff_token(raw) == (raw, "")
    assert extract_handoff_token(f"  {raw}  hello there  ") == (raw, "hello there")
    assert extract_handoff_token(wrapped) == (raw, "")
    assert inbound_text_without_token(wrapped) == "[website handoff]"
    assert extract_handoff_token(f"{wrapped}\n{HANDOFF_COMPOSE_HINT_HE}") == (
        raw,
        HANDOFF_COMPOSE_HINT_HE,
    )
    assert extract_handoff_token("not a token") is None


def test_compose_handoff_text_is_human_and_omits_token() -> None:
    raw = generate_handoff_token()
    composed = compose_handoff_text(raw)
    assert composed == HANDOFF_COMPOSE_HINT_HE
    assert raw not in composed
    assert extract_handoff_token(composed) is None


def test_click_to_chat_rejects_non_phone_and_uses_https() -> None:
    assert click_to_chat_digits("+972 500000001") == CLICK_CHAT
    assert click_to_chat_digits("https://evil.example") == ""
    url = click_to_chat_url(CLICK_CHAT)
    assert url.startswith("https://wa.me/")
    assert click_to_chat_url("javascript:alert(1)") == ""


def test_handoff_unknown_session_returns_404() -> None:
    init_db()
    with TestClient(app) as client:
        response = client.post("/v1/website/sessions/web_unknown_session/handoff")
        assert response.status_code == 404
