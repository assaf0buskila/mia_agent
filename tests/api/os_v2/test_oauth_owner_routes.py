"""E38b owner consent uses real cookie/Origin/CSRF and the existing Telegram worker."""

import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest
from app.api.os_v2 import owner_keys
from app.services import assistant_oauth, owner_actions

from tests.api.os_v2.test_owner_key_routes import world as owner_world  # noqa: F401

ROOT = "/v1/owner/os/unified/oauth-requests/"


@pytest.fixture
def flow(owner_world, monkeypatch):  # noqa: F811 - imported pytest fixture
    world = owner_world
    world.owner.settings.public_base_url = "https://api.assafweb.test"
    service = assistant_oauth.AssistantOAuthService(
        world.aw.w.ledger, world.owner.settings, revoke_watermark=lambda _: None)
    service._public_tx = lambda: service._tx(world.aw.w.owner)
    monkeypatch.setattr(owner_keys, "oauth_service_for", lambda _: service, raising=False)
    monkeypatch.setattr(owner_actions, "assistant_oauth_service", lambda _: service, raising=False)
    verifier = "Z" * 43
    row = service.authorize({
        "response_type": "code", "client_id": assistant_oauth.CLIENT_ID,
        "redirect_uri": assistant_oauth.REDIRECT_URI,
        "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode().rstrip("="), "code_challenge_method": "S256", "state": "private-state",
        "resource": assistant_oauth.resource(world.owner.settings)})
    world.oauth_id = row.oauth_request_id
    world.oauth_service = service
    return world


def test_owner_get_details_never_returns_state_or_code(flow):
    response = flow.call("GET", ROOT + flow.oauth_id)
    assert response.status_code == 200, response.text
    assert response.json()["client"] == "ChatGPT"
    assert response.json()["status"] == "pending_owner"
    assert "private-state" not in response.text and "code_challenge" not in response.text


@pytest.mark.parametrize("route", ["request-approval", "complete"])
@pytest.mark.parametrize("guard", ["cookie", "origin", "csrf", "bearer"])
def test_owner_oauth_posts_require_cookie_origin_csrf(flow, route, guard):
    headers = dict(flow.headers)
    if guard == "cookie":
        headers.pop("Cookie", None)
    elif guard == "origin":
        headers["Origin"] = "https://evil.test"
    elif guard == "csrf":
        headers.pop("X-CSRF-Token", None)
    else:
        headers["Authorization"] = "Bearer aosp_" + "A" * 43
    response = flow.call("POST", ROOT + flow.oauth_id + "/" + route, headers=headers, body={})
    assert response.status_code in {401, 403}, response.text


def test_owner_request_commits_then_sends_card_and_two_factors_complete(flow):
    response = flow.call("POST", ROOT + flow.oauth_id + "/request-approval", body={})
    assert response.status_code == 202, response.text
    assert flow.oauth_service.status(flow.aw.w.reader, flow.oauth_id,
                                    chain_id=_chain(flow))["status"] == "approval_requested"
    assert len(flow.port.sent) == 1
    text = flow.port.sent[0].text
    assert "ChatGPT" in text and "30" in text and "אישור" in text
    assert "private-state" not in text
    response = flow.call("POST", ROOT + flow.oauth_id + "/complete", body={})
    assert response.status_code == 409 and response.json()["error"]["code"] == "oauth_not_approved"
    outcome = flow.approve({})
    assert outcome.status == "approved", outcome
    response = flow.call("POST", ROOT + flow.oauth_id + "/complete", body={})
    assert response.status_code == 200, response.text
    query = parse_qs(urlsplit(response.json()["redirect_url"]).query)
    assert query["state"] == ["private-state"] and query["code"]
    assert flow.aw.credential_rows() == []


def _chain(flow):
    from sqlalchemy import text

    with flow.owner.factory() as db:
        return db.execute(text("SELECT chain_id FROM owner_os_sessions LIMIT 1")).scalar_one()


def test_other_google_login_chain_cannot_read_or_complete_approved_grant(flow):
    assert flow.call("POST", ROOT + flow.oauth_id + "/request-approval", body={}).status_code == 202
    flow.approve({})
    other = flow.fresh_headers()
    for method, suffix in (("GET", ""), ("POST", "/complete"), ("POST", "/request-approval")):
        response = flow.call(method, ROOT + flow.oauth_id + suffix, headers=other, body={})
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "oauth_request_unknown"


def test_rejected_and_cancelled_cards_have_terminal_text(flow):
    assert flow.call("POST", ROOT + flow.oauth_id + "/request-approval", body={}).status_code == 202
    flow.aw.revoke_all()
    outcome = flow.approve({})
    assert outcome.status == "cancelled" and "בוטלה" in outcome.text
    assert "לנסות שוב" not in outcome.text


@pytest.mark.parametrize("blocker", ["kill_switch", "assaf_os_assistant_enabled"])
def test_committed_card_permanent_refusal_never_invites_retry(flow, blocker):
    assert flow.call("POST", ROOT + flow.oauth_id + "/request-approval", body={}).status_code == 202
    setattr(flow.owner.settings, blocker, blocker == "kill_switch")
    outcome = flow.approve({})
    assert outcome.status in {"policy_denied", "unbound"}
    assert outcome.status != "not_started"
