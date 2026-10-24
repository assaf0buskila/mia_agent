"""V2-37, the three bearer routes under ``/v1/assistant/os`` (V2_PG_CONTRACTS.md §17.2).

The real ``app.main.app`` runs, with the real ``assistant_keys`` gateway over the FakeLedger world
of ``tests/services/os_ledger/assistant`` and V2-19's real ``authorize``. Only the factory functions
``gateway_for`` / ``service_for`` are replaced, exactly as V2-26's route tests replace
``service_for``. The PostgreSQL store is proved by the service suites; this file proves the HTTP
contract: shapes, status codes, headers, the body cap and the cross-refusals.
"""

from __future__ import annotations

import json

import pytest
from app.api.assistant import os as assistant_os
from app.core.config import get_settings
from app.main import app
from app.services import assistant_keys as ac
from fastapi.testclient import TestClient

from tests.services.os_ledger.assistant.support import IP_A, IP_B, ORIGIN, AWorld
from tests.services.os_ledger.statements.support import batch, encode, line, three_lines

ROOT = "/v1/assistant/os"
BATCH_KEYS = {"api_version", "proposal_id", "status", "review_url", "summary"}
SUMMARY_KEYS = {"line_count", "business_count", "personal_count", "unassigned_count"}
ENVELOPE = {"api_version", "request_id", "error"}


class Env:
    def __init__(self, aw: AWorld) -> None:
        self.aw = aw

    def client(self, ip: str = IP_A) -> TestClient:
        return TestClient(app, base_url="https://api.assafweb.test", client=(ip, 50000))

    def headers(self, secret: str, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {secret}", **extra}

    def post_batch(self, secret: str, body: bytes | None = None, *, ip: str = IP_A,
                   content_type: str = "application/json", number: int = 1):
        raw = encode(batch(three_lines(), number=number)) if body is None else body
        return self.client(ip).post(
            ROOT + "/statement-batches", content=raw,
            headers=self.headers(secret, **{"Content-Type": content_type}))


@pytest.fixture
def env(monkeypatch):
    aw = AWorld("fake")
    monkeypatch.setattr(assistant_os, "gateway_for", lambda settings: aw.gateway)
    monkeypatch.setattr(assistant_os, "service_for", lambda settings: aw.statements)
    app.dependency_overrides[get_settings] = lambda: aw.config
    yield Env(aw)
    app.dependency_overrides.pop(get_settings, None)


def mounted() -> set[tuple[str, str]]:
    return {(method.upper(), path) for path, operations in app.openapi()["paths"].items()
            for method in operations}


def error_of(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == ENVELOPE and body["error"]["code"] == code, body
    assert response.headers["cache-control"] == "private, no-store"
    return body["error"]


# ---------------------------------------------------------------------------------------------
# the route table


def test_exactly_the_three_bearer_routes_and_methods_exist():
    found = {(m, p) for m, p in mounted() if p.startswith("/v1/assistant")}
    assert found == {
        ("GET", ROOT + "/context"),
        ("POST", ROOT + "/statement-batches"),
        ("GET", ROOT + "/statement-batches/{proposal_id}"),
    }


def test_no_bearer_route_can_reach_approve_reject_draft_commit_or_key_management(env):
    paths = [p for _, p in mounted() if p.startswith("/v1/assistant")]
    assert not [p for p in paths if any(
        word in p for word in ("approve", "reject", "draft", "commit", "undo", "abandon",
                               "decide", "credential", "revoke", "step-up", "unified"))]
    issued = env.aw.issue()
    submitted = env.post_batch(issued["secret"]).json()
    client = env.client()
    for method, suffix in (("POST", "/approve"), ("POST", "/reject"), ("POST", "/draft"),
                           ("GET", "/lines")):
        response = client.request(method, f"{ROOT}/statement-batches/{submitted['proposal_id']}"
                                  + suffix, headers=env.headers(issued["secret"]))
        assert response.status_code in {404, 405}, (method, suffix)
    for path in ("/v1/assistant/os/assistant-credentials", "/v1/assistant/os/commits",
                 "/v1/assistant/os/step-up"):
        assert client.post(path, headers=env.headers(issued["secret"])).status_code in {404, 405}
    assert [p["status"] for p in env.aw.proposals()] == ["pending"]


# ---------------------------------------------------------------------------------------------
# cookie and bearer cross-refusals, and the uniform 401


def test_a_cookie_is_a_401_even_with_a_valid_key(env):
    issued = env.aw.issue()
    client = env.client()
    client.cookies.set("__Host-mia-owner", "x" * 40)
    for response in (client.get(ROOT + "/context", headers=env.headers(issued["secret"])),
                     client.get(ROOT + "/context", headers={
                         **env.headers(issued["secret"]), "Cookie": "a=b"})):
        error = error_of(response, 401, "assistant_auth_required")
        assert error == {"code": "assistant_auth_required", "retryable": False}
        assert response.headers["www-authenticate"] == "Bearer"
    assert env.aw.audit() == []


def test_a_post_401_carries_write_outcome_not_started_and_a_get_does_not(env):
    get = error_of(env.client().get(ROOT + "/context"), 401, "assistant_auth_required")
    post = error_of(env.client().post(ROOT + "/statement-batches", content=b"{}"), 401,
                    "assistant_auth_required")
    assert "write_outcome" not in get and post["write_outcome"] == "not_started"


@pytest.mark.parametrize("header", [
    None, "Bearer", "Bearer x", "Basic eDp5", "bearer aosp_" + "A" * 43, "Bearer aosp_" + "A" * 42])
def test_a_missing_or_malformed_authorization_is_the_uniform_401(env, header):
    headers = {} if header is None else {"Authorization": header}
    response = env.client().get(ROOT + "/context", headers=headers)
    error_of(response, 401, "assistant_auth_required")
    assert env.aw.audit() == []


def test_the_401_is_byte_identical_for_every_kind_of_invalid_key(env):
    aw = env.aw
    bodies = {}
    unknown = ac.generate_secret()  # the right shape, never issued

    def attempt(name: str, secret: str) -> None:
        response = env.client().get(ROOT + "/context", headers=env.headers(secret))
        assert response.status_code == 401, (name, response.text)
        body = response.json()
        body.pop("request_id")
        bodies[name] = (body, response.headers["www-authenticate"],
                        response.headers["cache-control"])

    attempt("unknown", unknown)
    revoked = aw.issue(label="revoked")
    aw.revoke(revoked["credential_id"])
    attempt("revoked", revoked["secret"])
    expired = aw.issue(label="expired", days=1)
    aw.clock.advance(2 * 86_400)
    attempt("expired", expired["secret"])
    epoch = aw.issue(label="epoch")
    aw.w.bump_epoch()
    attempt("epoch", epoch["secret"])
    assert len({json.dumps(v, sort_keys=True) for v in bodies.values()}) == 1, bodies


def test_the_kill_switch_and_a_disabled_feature_are_the_uniform_401(env):
    aw = env.aw
    issued = aw.issue()
    assert env.client().get(ROOT + "/context", headers=env.headers(issued["secret"])
                            ).status_code == 200
    aw.config.kill_switch = True
    error_of(env.client().get(ROOT + "/context", headers=env.headers(issued["secret"])), 401,
             "assistant_auth_required")
    aw.config.kill_switch = False
    aw.config.assaf_os_assistant_enabled = False
    error_of(env.client().get(ROOT + "/context", headers=env.headers(issued["secret"])), 401,
             "assistant_auth_required")


def test_a_disabled_feature_never_builds_a_gateway_or_touches_a_ledger(env, monkeypatch):
    env.aw.config.assaf_os_assistant_enabled = False

    def forbidden(settings):
        raise AssertionError("the gateway was built while the feature is off")

    monkeypatch.setattr(assistant_os, "gateway_for", forbidden)
    for method, path in (("GET", "/context"), ("POST", "/statement-batches"),
                         ("GET", "/statement-batches/PRO_" + "0" * 32)):
        response = env.client().request(method, ROOT + path, headers=env.headers(
            "aosp_" + "A" * 43))
        error_of(response, 401, "assistant_auth_required")


# ---------------------------------------------------------------------------------------------
# GET /context


def test_context_has_the_rules_the_statements_and_the_format(env):
    issued = env.aw.issue()
    response = env.client().get(ROOT + "/context", headers=env.headers(issued["secret"]))
    assert response.status_code == 200 and response.headers["cache-control"] == (
        "private, no-store")
    body = response.json()
    assert set(body) == {"api_version", "rules", "statements", "format"}
    assert body["api_version"] == "assaf-os-assistant.v1"
    assert body["rules"] == [] and body["statements"] == []
    assert body["format"]["schema"] == "statement-batch.v1"
    assert body["format"]["max_bytes"] == 131072
    assert "set-cookie" not in response.headers


def test_context_lists_the_pending_statement_without_personal_lines(env):
    issued = env.aw.issue()
    env.post_batch(issued["secret"])
    body = env.client().get(ROOT + "/context", headers=env.headers(issued["secret"])).json()
    [statement] = body["statements"]
    assert set(statement) == {"account_label", "charge_date", "status", "line_count"}
    assert statement["status"] == "pending" and statement["line_count"] == 3
    assert "PERSONAL CAFE" not in json.dumps(body)


# ---------------------------------------------------------------------------------------------
# POST /statement-batches


def test_a_submit_lands_a_pending_batch_and_returns_the_exact_shape(env):
    aw = env.aw
    issued = aw.issue()
    response = env.post_batch(issued["secret"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == BATCH_KEYS and set(body["summary"]) == SUMMARY_KEYS
    assert body["api_version"] == "assaf-os-assistant.v1" and body["status"] == "pending"
    assert body["review_url"] == f"{ORIGIN}/#statements/{body['proposal_id']}"
    assert body["summary"] == {"line_count": 3, "business_count": 2, "personal_count": 1,
                               "unassigned_count": 0}
    [proposal] = aw.proposals()
    assert (proposal["status"], proposal["kind"], proposal["source"]) == (
        "pending", "statement_batch", "ASSISTANT_CLAUDE")
    assert proposal["credential_id"] == issued["credential_id"]
    assert aw.receipts() == [] and aw.w.expenses() == []  # nothing executed


def test_a_gpt_key_submits_as_assistant_gpt(env):
    issued = env.aw.issue(kind="gpt")
    env.post_batch(issued["secret"])
    assert env.aw.proposals()[0]["source"] == "ASSISTANT_GPT"


def test_no_decision_id_appears_in_any_bearer_body(env):
    aw = env.aw
    issued = aw.issue()
    submitted = env.post_batch(issued["secret"]).json()
    texts = [json.dumps(submitted),
             env.client().get(ROOT + "/context", headers=env.headers(issued["secret"])).text,
             env.client().get(f"{ROOT}/statement-batches/{submitted['proposal_id']}",
                              headers=env.headers(issued["secret"])).text]
    [proposal] = aw.proposals()
    for text in texts:
        assert proposal["approval_id"] not in text and proposal["rejection_id"] not in text
        assert proposal["request_id"] not in text
        lowered = text.lower()
        assert not any(word in lowered for word in ("approval", "rejection", "decision",
                                                    "review_digest", "request_id"))


def test_resubmitting_the_same_batch_is_idempotent(env):
    issued = env.aw.issue()
    first, second = env.post_batch(issued["secret"]), env.post_batch(issued["secret"])
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() and len(env.aw.proposals()) == 1


def test_an_invalid_batch_is_a_422_envelope_with_issues_and_is_audited(env):
    issued = env.aw.issue()
    response = env.post_batch(issued["secret"], body=b'{"schema":"statement-batch.v1"}')
    error = error_of(response, 422, "invalid_statement_batch")
    assert error["write_outcome"] == "not_started" and error["details"]["issues"]
    [row] = env.aw.audit()
    assert (row["operation"], row["ok"], row["reason"]) == ("submit", False, row["reason"])
    assert env.aw.proposals() == []


def test_a_body_over_128_kib_is_a_422_validation_error_counted_and_audited(env):
    issued = env.aw.issue()
    over = b"x" * 131_073
    error = error_of(env.post_batch(issued["secret"], body=over), 422, "validation_error")
    assert error["details"] == {"reason": "body_too_large"} and error["write_outcome"] == (
        "not_started")
    [row] = env.aw.audit()
    assert (row["operation"], row["ok"], row["reason"], row["bytes"]) == (
        "submit", False, "body_too_large", 131_073)
    assert env.aw.proposals() == []


def test_a_body_of_exactly_128_kib_is_read_and_judged_as_a_batch_not_as_too_large(env):
    issued = env.aw.issue()
    exactly = b" " * 131_072
    error = error_of(env.post_batch(issued["secret"], body=exactly), 422,
                     "invalid_statement_batch")
    assert "details" in error
    assert env.aw.audit()[0]["bytes"] == 131_072


def test_a_non_json_content_type_is_a_422_validation_error(env):
    issued = env.aw.issue()
    error = error_of(env.post_batch(issued["secret"], content_type="text/plain"), 422,
                     "validation_error")
    assert error["details"] == {"reason": "content_type"}
    assert env.post_batch(issued["secret"], content_type="application/json; charset=utf-8"
                          ).status_code == 200


def test_an_unset_owner_origin_refuses_before_the_batch_is_submitted(env):
    issued = env.aw.issue()
    env.aw.config.owner_os_origin = ""
    error_of(env.post_batch(issued["secret"]), 503, "ledger_unavailable")
    assert env.aw.proposals() == []
    [row] = env.aw.audit()
    assert row["ok"] is False and row["reason"] == "owner_origin_unset"


def test_a_frozen_ledger_refuses_the_submit_with_os_frozen_and_writes_nothing(env):
    issued = env.aw.issue()
    env.aw.config.assaf_os_freeze = True
    error_of(env.post_batch(issued["secret"]), 503, "os_frozen")
    assert env.aw.proposals() == []


# ---------------------------------------------------------------------------------------------
# GET /statement-batches/{id}


def test_status_of_the_keys_own_batch_and_nothing_else(env):
    aw = env.aw
    mine, other = aw.issue(label="mine"), aw.issue(label="other", kind="gpt")
    own = env.post_batch(mine["secret"], number=1).json()
    foreign = env.post_batch(other["secret"], number=2,
                             body=encode(batch(three_lines(), number=2, label="Card Two"))).json()
    ok = env.client().get(f"{ROOT}/statement-batches/{own['proposal_id']}",
                          headers=env.headers(mine["secret"]))
    assert ok.status_code == 200
    body = ok.json()
    assert set(body) == {"api_version", "proposal_id", "status", "updated_at"}
    assert (body["proposal_id"], body["status"]) == (own["proposal_id"], "pending")
    for target in (foreign["proposal_id"], "PRO_" + "0" * 32, "nope", "PRO_" + "g" * 32):
        response = env.client().get(f"{ROOT}/statement-batches/{target}",
                                    headers=env.headers(mine["secret"]))
        assert error_of(response, 404, "batch_not_found") == {
            "code": "batch_not_found", "retryable": False}


def test_status_follows_the_owner_decision_without_exposing_it(env):
    aw = env.aw
    issued = aw.issue()
    submitted = env.post_batch(issued["secret"]).json()
    aw.w.reject(submitted["proposal_id"])
    body = env.client().get(f"{ROOT}/statement-batches/{submitted['proposal_id']}",
                            headers=env.headers(issued["secret"])).json()
    assert body["status"] == "rejected" and set(body) == {
        "api_version", "proposal_id", "status", "updated_at"}


# ---------------------------------------------------------------------------------------------
# limits over HTTP


def test_the_per_address_limit_answers_429_with_retry_after_before_the_lookup(env):
    aw = env.aw
    issued = aw.issue()
    aw.seed_audit(60, ip_hmac=aw.ip_hmac(IP_A), operation="context", ok=True, reason="ok")
    response = env.client().get(ROOT + "/context", headers=env.headers(issued["secret"]))
    error = error_of(response, 429, "rate_limited")
    assert error["retryable"] is True and response.headers["retry-after"] == "600"
    assert env.client(IP_B).get(ROOT + "/context", headers=env.headers(issued["secret"])
                                ).status_code == 200


def test_the_per_key_hourly_and_daily_limits_answer_429(env):
    aw = env.aw
    issued = aw.issue()
    for _ in range(30):
        assert env.client().get(ROOT + "/context", headers=env.headers(issued["secret"])
                                ).status_code == 200
    hourly = env.client().get(ROOT + "/context", headers=env.headers(issued["secret"]))
    error_of(hourly, 429, "rate_limited")
    assert hourly.headers["retry-after"] == "3600"
    other = aw.issue(label="other", kind="gpt")
    for number in range(5):
        assert env.post_batch(other["secret"], number=number + 1,
                              body=encode(batch(three_lines(), number=number + 1,
                                                label=f"Card {number}"))).status_code == 200
    daily = env.post_batch(other["secret"], number=9)
    error_of(daily, 429, "rate_limited")
    assert daily.headers["retry-after"] == "86400"


def test_the_invalid_lookup_budget_answers_429_and_a_valid_key_still_works(env):
    issued = env.aw.issue()
    wrong = "aosp_" + "B" * 43
    for _ in range(20):
        error_of(env.client().get(ROOT + "/context", headers=env.headers(wrong)), 401,
                 "assistant_auth_required")
    blocked = env.client().get(ROOT + "/context", headers=env.headers(wrong))
    error_of(blocked, 429, "rate_limited")
    assert env.client().get(ROOT + "/context", headers=env.headers(issued["secret"])
                            ).status_code == 200


# ---------------------------------------------------------------------------------------------
# an unexpected failure is still an envelope


def test_an_unexpected_failure_is_a_ledger_envelope_never_a_stack_trace(env, monkeypatch):
    issued = env.aw.issue()

    def broken(caller, operation, work):
        raise RuntimeError("COFFEE SHOP 4271")

    monkeypatch.setattr(env.aw.gateway, "run", broken)
    post = env.post_batch(issued["secret"])
    error = error_of(post, 503, "unknown")
    assert error["write_outcome"] == "unknown" and "COFFEE" not in post.text
    get = env.client().get(ROOT + "/context", headers=env.headers(issued["secret"]))
    assert error_of(get, 503, "ledger_unavailable") == {
        "code": "ledger_unavailable", "retryable": True}


def test_a_batch_with_other_lines_is_just_as_pending(env):
    issued = env.aw.issue()
    raw = encode(batch([line("NEW SHOP", 5000), line("OTHER", 700, tag=None)]))
    response = env.post_batch(issued["secret"], body=raw)
    assert response.status_code == 200
    assert response.json()["summary"]["unassigned_count"] == 1


# ---------------------------------------------------------------------------------------------
# privacy and the address bucket


def test_the_logs_carry_reason_codes_and_nothing_a_caller_sent(env, caplog):
    caplog.set_level("DEBUG")
    aw = env.aw
    issued = aw.issue(label="MY PRIVATE LABEL")
    wrong = ac.generate_secret()
    env.client("198.51.100.77").get(ROOT + "/context", headers=env.headers(wrong))
    env.client("198.51.100.77").get(ROOT + "/context", headers=env.headers(issued["secret"]))
    env.post_batch(issued["secret"], ip="198.51.100.77")
    env.client("198.51.100.77").get(ROOT + "/context", headers={"Cookie": "a=b"})
    text = caplog.text
    assert "reason=key_unknown" in text and "reason=cookie_present" in text
    for forbidden in (issued["secret"], wrong, ac.secret_hash(issued["secret"]),
                      ac.secret_hash(wrong), "198.51.100.77", "MY PRIVATE LABEL", "Bearer",
                      aw.ip_hmac("198.51.100.77"), "DEMO SHOP", "PERSONAL CAFE"):
        assert forbidden not in text, forbidden


def test_the_address_the_limiter_counts_is_the_slash_64_of_the_peer(env):
    aw = env.aw
    issued = aw.issue()
    env.client("2001:db8:0:1::7").get(ROOT + "/context", headers=env.headers(issued["secret"]))
    [row] = aw.audit()
    assert row["client_ip_hmac"] == aw.ip_hmac("2001:db8:0:1:ffff::99")
    assert row["client_ip_hmac"] != aw.ip_hmac("2001:db8:0:2::7")
    env.client("203.0.113.5").get(ROOT + "/context", headers=env.headers(issued["secret"]))
    assert aw.audit()[1]["client_ip_hmac"] == aw.ip_hmac("203.0.113.5")


def test_a_forwarded_for_header_does_not_choose_the_bucket_outside_production(env):
    aw = env.aw
    issued = aw.issue()
    env.client(IP_A).get(ROOT + "/context", headers={
        **env.headers(issued["secret"]), "X-Forwarded-For": "198.51.100.200"})
    [row] = aw.audit()
    assert row["client_ip_hmac"] == aw.ip_hmac(IP_A)
