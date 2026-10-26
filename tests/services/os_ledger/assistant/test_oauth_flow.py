"""E39: both factors, atomic exchange/replay, audience and cancellation, on both ledgers."""

from __future__ import annotations

import base64
import hashlib
import threading
from urllib.parse import parse_qs, urlsplit

import pytest
from app.services import assistant_keys as keys
from app.services.os_ledger import api

from tests.services.os_ledger.assistant.support import IP_A
from tests.services.os_ledger.statements.support import pg_url

CHAIN = "oauth-login-chain"
VERIFIER = "V" * 43
STATE = "opaque-state +/%?=&"


class Flow:
    def __init__(self, aw):
        from app.services import assistant_oauth as oauth

        self.oauth, self.aw = oauth, aw
        aw.config.public_base_url = "https://api.assafweb.test"
        self.service = oauth.AssistantOAuthService(
            aw.w.ledger, aw.config, revoke_watermark=lambda _: aw.w.watermark["value"])
        if aw.kind == "fake":
            self.service._public_tx = lambda **kw: self.service._tx(aw.w.owner)
        self.params = {
            "response_type": "code", "client_id": oauth.CLIENT_ID,
            "redirect_uri": oauth.REDIRECT_URI, "scope": oauth.SCOPE,
            "resource": oauth.resource(aw.config), "state": STATE,
            "code_challenge_method": "S256",
            "code_challenge": base64.urlsafe_b64encode(
                hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("="),
        }
        self.cards = []

    def start(self, *, ip=IP_A, **changes):
        return self.service.authorize({**self.params, **changes}, ip=ip).oauth_request_id

    def request(self, id, *, chain=CHAIN):
        return self.service.request_approval(self.aw.w.owner, id, chain_id=chain)

    def decide(self, id, *, decision="approve"):
        from app.capabilities.types import Principal
        from app.db.models import ApprovalRow
        from app.db.store import LeadStore
        from app.services.owner_actions import decide_owner_action, propose_owner_action
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        envelope = self.service.proposal(self.aw.w.owner, id, chain_id=CHAIN)
        engine = create_engine("sqlite:///:memory:")
        ApprovalRow.__table__.create(engine)
        db = Session(engine, expire_on_commit=False)
        store = LeadStore(db)
        actor = Principal.owner(source="telegram", actor_id="123")
        action = propose_owner_action(
            store, principal=actor, source_ref=id, kind=envelope["kind"],
            parameters=envelope["parameters"], target=envelope["target"], ttl_seconds=600)
        db.commit()
        row = store.get_approval_by_approval_id(action.approval_id)
        decide_owner_action(store, principal=actor, approval_id=action.approval_id,
                            decision="approved" if decision == "approve" else "rejected")
        db.commit()
        self.cards.append((db, row))
        return self.service.decide_grant(row, decision=decision)

    def complete(self, id, *, chain=CHAIN, ctx=None):
        body = self.service.complete(ctx or self.aw.w.owner, id, chain_id=chain)
        url = urlsplit(body["redirect_url"])
        query = parse_qs(url.query)
        assert query["state"] == [STATE]
        assert query["iss"] == [self.oauth.issuer(self.aw.config)]
        return query["code"][0]

    def code(self):
        id = self.start()
        self.request(id)
        self.decide(id)
        return id, self.complete(id)

    def exchange(self, code, **changes):
        return self.service.token({
            "grant_type": "authorization_code", "code": code,
            "client_id": self.oauth.CLIENT_ID, "redirect_uri": self.oauth.REDIRECT_URI,
            "code_verifier": VERIFIER, "resource": self.oauth.resource(self.aw.config),
            **changes})

    def status(self, id, chain=CHAIN):
        return self.service.status(self.aw.w.reader, id, chain_id=chain)["status"]


@pytest.fixture
def flow(aw):
    world = Flow(aw)
    yield world
    for db, _ in world.cards:
        db.close()


def refused(call, code):
    with pytest.raises(api.LedgerError) as caught:
        call()
    assert caught.value.code == code


def test_chatgpt_authorize_alone_cannot_mint(flow):
    id = flow.start()
    assert flow.status(id) == "pending_owner"
    refused(lambda: flow.complete(id), "oauth_not_approved")
    assert flow.aw.credential_rows() == []


def test_owner_session_alone_cannot_complete(flow):
    id = flow.start()
    flow.request(id)
    refused(lambda: flow.complete(id), "oauth_not_approved")
    assert flow.aw.credential_rows() == []


@pytest.mark.parametrize("context", ["reader", "telegram"])
def test_telegram_approval_alone_cannot_complete(flow, context):
    id = flow.start()
    flow.request(id)
    flow.decide(id)
    ctx = flow.aw.w.reader if context == "reader" else flow.aw.w.ctx("TELEGRAM_APPROVAL")
    with pytest.raises(api.LedgerError):
        flow.complete(id, ctx=ctx)
    assert flow.aw.credential_rows() == []


@pytest.mark.parametrize("stage", ["pending", "approved", "code"])
def test_other_login_chain_gets_404_after_binding(flow, stage):
    id = flow.start()
    flow.request(id)
    if stage != "pending":
        flow.decide(id)
    if stage == "code":
        flow.complete(id)
    for call in (lambda: flow.status(id, "another"), lambda: flow.request(id, chain="another"),
                 lambda: flow.complete(id, chain="another")):
        refused(call, "oauth_request_unknown")
    assert flow.aw.credential_rows() == []


def test_complete_and_exchange_hash_only_once_and_30_days(flow, caplog):
    caplog.set_level("DEBUG")
    id, code = flow.code()
    token = flow.exchange(code)
    assert set(token) == {"access_token", "token_type", "expires_in", "scope"}
    assert token["token_type"] == "Bearer" and token["expires_in"] == 30 * 86400
    assert token["scope"] == flow.oauth.SCOPE
    assert flow.status(id) == "exchanged"
    rows = flow.aw.credential_rows()
    assert len(rows) == 1 and rows[0]["secret_hash"] == keys.secret_hash(token["access_token"])
    assert rows[0]["kind"] == "gpt" and rows[0]["label"] == "ChatGPT connector"
    assert rows[0]["expires_at"] - rows[0]["created_at"] == flow.oauth.TOKEN_TTL
    refused(lambda: flow.complete(id), "oauth_completed")
    with flow.service._public_tx() as tx:
        row = flow.oauth.OAuthTables(tx).get(id)
    assert row.code_hash == flow.oauth.digest(code) and row.state == STATE
    assert not hasattr(row, "state_hash")
    surfaces = caplog.text + repr(flow.aw.audit())
    for value in (code, token["access_token"], VERIFIER, STATE):
        assert value not in surfaces
    assert code not in repr(row) and STATE not in repr(row)
    assert token["access_token"] not in repr(rows)


@pytest.mark.parametrize("change,error", [
    ({"code_verifier": "X" * 43}, "invalid_grant"),
    ({"code_verifier": "V" * 42}, "invalid_grant"),
    ({"code_verifier": "V" * 129}, "invalid_grant"),
    ({"code_verifier": "!" * 43}, "invalid_grant"),
    ({"client_id": "https://evil.test"}, "invalid_client"),
    ({"redirect_uri": "https://evil.test"}, "invalid_grant"),
    ({"resource": "https://api.assafweb.test/v1/assistant/mcp/"}, "invalid_grant"),
    ({"resource": ""}, "invalid_request"),
    ({"scope": "openid"}, "invalid_scope"),
    ({"grant_type": "refresh_token"}, "unsupported_grant_type"),
])
def test_exchange_rejects_mismatches_without_consuming_code(flow, change, error):
    id, code = flow.code()
    with pytest.raises(flow.oauth.OAuthError) as caught:
        flow.exchange(code, **change)
    assert caught.value.code == error and code not in str(caught.value)
    assert flow.status(id) == "code_issued"
    assert flow.aw.credential_rows() == []
    assert flow.exchange(code)["access_token"]


def test_code_replay_revokes_derived_key_atomically(flow):
    _, code = flow.code()
    token = flow.exchange(code)
    with pytest.raises(flow.oauth.OAuthError, match="invalid_grant"):
        flow.exchange(code)
    assert flow.aw.credential_rows()[0]["revoked_at"] is not None
    refused(lambda: flow.aw.gateway.authenticate(
        cookie=False, authorization=["Bearer " + token["access_token"]], ip=IP_A,
        resource=flow.oauth.resource(flow.aw.config)), "assistant_auth_required")


def test_code_replay_applies_existing_pending_batch_revocation(flow):
    from tests.services.os_ledger.statements.support import batch, three_lines

    _, code = flow.code()
    flow.exchange(code)
    issued = flow.aw.credential_rows()[0]
    pending = flow.aw.paste_as(issued, batch(three_lines()), kind="gpt")
    assert flow.aw.proposals()[0]["status"] == "pending"
    with pytest.raises(flow.oauth.OAuthError, match="invalid_grant"):
        flow.exchange(code)
    assert flow.aw.proposals()[0]["status"] == "rejected"
    lines = flow.aw.w.tables()["batch_lines"]
    assert all(row.detail is None for (pid, _), row in lines.items()
               if pid == pending.proposal_id)


def test_oauth_audience_is_mcp_only_static_keys_unchanged(flow):
    _, code = flow.code()
    token = flow.exchange(code)["access_token"]
    for audience in (None, "https://evil.test/mcp"):
        refused(lambda: flow.aw.gateway.authenticate(
            cookie=False, authorization=["Bearer " + token], ip=IP_A, resource=audience),
            "assistant_auth_required")
    caller = flow.aw.gateway.authenticate(cookie=False, authorization=["Bearer " + token], ip=IP_A,
                                         resource=flow.oauth.resource(flow.aw.config))
    assert caller.credential.audience == flow.oauth.resource(flow.aw.config)
    static = flow.aw.issue()["secret"]
    assert flow.aw.authenticate(static)
    assert flow.aw.gateway.authenticate(cookie=False, authorization=["Bearer " + static],
                                       ip=IP_A, resource=flow.oauth.resource(flow.aw.config))


@pytest.mark.parametrize("stage", [
    "pending_owner", "approval_requested", "approved", "code_issued"])
def test_revoke_all_cancels_every_nonterminal_request(flow, stage):
    id = flow.start()
    if stage != "pending_owner":
        flow.request(id)
    if stage in {"approved", "code_issued"}:
        flow.decide(id)
    code = flow.complete(id) if stage == "code_issued" else None
    flow.aw.revoke_all()
    assert flow.status(id) == "cancelled"
    refused(lambda: flow.complete(id), "oauth_cancelled")
    if code:
        with pytest.raises(flow.oauth.OAuthError, match="invalid_grant"):
            flow.exchange(code)
    assert flow.aw.credential_rows() == []


@pytest.mark.parametrize("block", ["revoke", "revoke_all", "epoch", "kill"])
def test_existing_revocation_paths_kill_oauth_tokens(flow, block):
    _, code = flow.code()
    token = flow.exchange(code)["access_token"]
    if block == "revoke":
        flow.aw.revoke(flow.aw.credential_rows()[0]["credential_id"])
    elif block == "revoke_all":
        flow.aw.revoke_all()
    elif block == "epoch":
        flow.aw.w.bump_epoch()
    else:
        flow.aw.config.kill_switch = True
    refused(lambda: flow.aw.gateway.authenticate(
        cookie=False, authorization=["Bearer " + token], ip=IP_A,
        resource=flow.oauth.resource(flow.aw.config)), "assistant_auth_required")


@pytest.mark.parametrize("block", ["epoch", "owner_watermark"])
def test_revoked_login_or_epoch_refuses_code_as_invalid_grant(flow, block):
    _, code = flow.code()
    if block == "epoch":
        flow.aw.w.bump_epoch()
        if flow.aw.kind == "fake":
            flow.service._public_tx = lambda: flow.service._tx(flow.aw.w.current_reader())
    else:
        flow.aw.w.watermark["value"] = flow.aw.w.now()
    with pytest.raises(flow.oauth.OAuthError, match="invalid_grant"):
        flow.exchange(code)
    assert flow.aw.credential_rows() == []


@pytest.mark.parametrize("stage", ["approval_requested", "approved", "code_issued"])
def test_bound_request_cap_is_five_and_scope_default_resource_case(flow, stage):
    for i in range(5):
        p = dict(flow.params)
        p.pop("scope")
        p["resource"] = "HTTPS://API.ASSAFWEB.TEST/v1/assistant/mcp"
        row = flow.service.authorize(p, ip=f"203.0.113.{i}")
        flow.request(row.oauth_request_id)
        if stage != "approval_requested":
            flow.decide(row.oauth_request_id)
        if stage == "code_issued":
            flow.complete(row.oauth_request_id)
    id = flow.start()
    refused(lambda: flow.request(id), "oauth_not_approved")
    assert flow.status(id) == "pending_owner"


def test_two_parallel_exchanges_mint_once_and_revoke_on_replay(flow):
    _, code = flow.code()
    outcomes = []
    barrier = threading.Barrier(2)

    def work():
        barrier.wait()
        try:
            outcomes.append(flow.exchange(code))
        except flow.oauth.OAuthError as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=work) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    assert len(outcomes) == 2 and outcomes.count("invalid_grant") == 1
    assert len(flow.aw.credential_rows()) == 1
    assert flow.aw.credential_rows()[0]["revoked_at"] is not None


def test_exchange_failure_rolls_back_both_mint_and_code_consumption(flow, monkeypatch):
    id, code = flow.code()
    table = keys.PgCredentialTables if flow.aw.kind == "pg" else keys.MemoryCredentialTables
    original = table.insert

    def fail(self, row):
        original(self, row)
        raise api.LedgerError("ledger_unavailable")

    monkeypatch.setattr(table, "insert", fail)
    refused(lambda: flow.exchange(code), "ledger_unavailable")
    assert flow.aw.credential_rows() == [] and flow.status(id) == "code_issued"
    monkeypatch.setattr(table, "insert", original)
    assert flow.exchange(code)["access_token"]


def test_code_expires_at_exactly_sixty_seconds(flow):
    _, code = flow.code()
    flow.aw.w.advance(60)
    with pytest.raises(flow.oauth.OAuthError, match="invalid_grant"):
        flow.exchange(code)
    assert flow.aw.credential_rows() == []


@pytest.mark.parametrize("stage", ["pending", "approval", "approved"])
def test_request_expires_at_exactly_ten_minutes(flow, stage):
    id = flow.start()
    if stage != "pending":
        flow.request(id)
    if stage == "approved":
        flow.decide(id)
    flow.aw.w.advance(600)
    refused(lambda: flow.complete(id), "oauth_expired")
    assert flow.status(id) == "expired"
    assert flow.aw.credential_rows() == []


def test_pending_limit_expires_old_status_rows_before_admitting_more(flow):
    for i in range(5):
        flow.start(ip=f"203.0.113.{i}")
    flow.aw.w.advance(600)
    flow.start()
    with flow.service._public_tx() as tx:
        table = flow.oauth.OAuthTables(tx)
        if tx.connection is None:
            rows = list(table.memory().values())
            assert sum(r.status in {"pending_owner", "approval_requested", "approved"}
                       for r in rows) == 1
        else:
            from sqlalchemy import text

            assert tx.connection.execute(text(
                "SELECT count(*) FROM assaf_os.os_assistant_oauth_requests "
                "WHERE status IN ('pending_owner','approval_requested','approved')"
            )).scalar_one() == 1


def test_unexpected_stored_audience_is_uniform_401(flow):
    from dataclasses import replace

    from sqlalchemy import text

    _, code = flow.code()
    token = flow.exchange(code)["access_token"]
    with flow.service._public_tx() as tx:
        table = keys.credential_tables(tx)
        row = table.list()[0]
        if tx.connection is None:
            table._table()[row.credential_id] = replace(row, audience="https://other.test/mcp")
        else:
            with flow.aw.w._admin_engine.begin() as conn:
                conn.execute(text("UPDATE assaf_os.os_assistant_credentials "
                                  "SET audience = 'https://other.test/mcp'"))
    refused(lambda: flow.aw.gateway.authenticate(
        cookie=False, authorization=["Bearer " + token], ip=IP_A,
        resource=flow.oauth.resource(flow.aw.config)), "assistant_auth_required")


@pytest.mark.parametrize("resource", [
    "\nhttps://api.assafweb.test/v1/assistant/mcp",
    "https://api.assafweb.test/v1/assistant/\nmcp",
    "https://api.assafweb.test/v1/assistant/mcp\t",
])
def test_resource_uri_is_never_normalized_beyond_scheme_and_host_case(flow, resource):
    with pytest.raises(flow.oauth.OAuthError):
        flow.start(resource=resource)


def test_kill_switch_at_telegram_approval_is_permanent(flow):
    id = flow.start()
    flow.request(id)
    flow.aw.config.kill_switch = True
    refused(lambda: flow.decide(id), "kill_switch")


if pg_url():
    def test_public_oauth_and_bearer_share_exact_ip_window_under_concurrency(pg_aw):
        flow = Flow(pg_aw)
        pg_aw.seed_audit(59, ip_hmac=pg_aw.ip_hmac(IP_A), operation="mcp_control")
        barrier = threading.Barrier(2)
        outcomes = []

        def account(operation):
            barrier.wait()
            try:
                flow.service.account(IP_A, operation)
                outcomes.append("ok")
            except api.LedgerError as exc:
                outcomes.append(exc.code)

        threads = [threading.Thread(target=account, args=(operation,))
                   for operation in ("oauth_authorize", "oauth_token")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            assert not thread.is_alive()
        assert sorted(outcomes) == ["ok", "rate_limited"]
        assert len(pg_aw.audit()) == 60
        assert len([r for r in pg_aw.audit() if r["operation"].startswith("oauth_")]) == 1


def test_unbound_per_ip_cap_is_two_and_hmac_is_private(flow, caplog):
    ids = [flow.start(), flow.start()]
    with pytest.raises(flow.oauth.OAuthError, match="oauth_pending_limit"):
        flow.start()
    with flow.service._public_tx() as tx:
        assert len(flow.oauth.OAuthTables(tx).live()) == 2
        rows = [flow.oauth.OAuthTables(tx).get(id) for id in ids]
    assert all(row.client_ip_hmac == flow.aw.ip_hmac(IP_A) for row in rows)
    surfaces = caplog.text + repr([flow.service.status(flow.aw.w.reader, id, chain_id=CHAIN)
                                  for id in ids])
    assert flow.aw.ip_hmac(IP_A) not in surfaces and IP_A not in surfaces
    assert flow.start(ip="203.0.113.99")


def eviction_audit(flow):
    with flow.service._public_tx() as tx:
        if tx.connection is None:
            return tx.scratch.get("assistant_oauth_audit", [])
        from sqlalchemy import text

        return list(tx.connection.execute(text(
            "SELECT reason FROM assaf_os.os_assistant_audit WHERE reason='evicted_unbound'"
        )).scalars())


@pytest.mark.parametrize("stage", ["approval_requested", "approved", "code_issued"])
def test_oldest_unbound_is_cancelled_with_atomic_audit_and_bound_never_evicted(flow, stage):
    bound = flow.start()
    flow.request(bound)
    if stage != "approval_requested":
        flow.decide(bound)
    if stage == "code_issued":
        flow.complete(bound)
    ids = []
    for i in range(5):
        ids.append(flow.start(ip=f"203.0.113.{i}"))
        flow.aw.w.advance(1)
    newest = flow.start(ip="203.0.113.99")
    assert flow.status(ids[0]) == "cancelled"
    assert all(flow.status(id) == "pending_owner" for id in [*ids[1:], newest])
    assert flow.status(bound) == stage
    assert eviction_audit(flow) == ["evicted_unbound"]


def test_unknown_values_never_reach_storage_audit_or_logs(flow, caplog):
    marker = "UNRECOGNIZED_PRIVATE_VALUE_214"
    id = flow.start(extra=marker)
    flow.request(id)
    flow.decide(id)
    assert flow.exchange(flow.complete(id), extra=marker)["access_token"]
    with flow.service._public_tx() as tx:
        row = flow.oauth.OAuthTables(tx).get(id)
    assert marker not in repr(vars(row)) + repr(flow.aw.audit()) + caplog.text


def test_eviction_and_its_audit_roll_back_when_new_request_insert_fails(flow, monkeypatch):
    ids = [flow.start(ip=f"203.0.113.{i}") for i in range(5)]

    def fail(self, row):
        raise api.LedgerError("ledger_unavailable")

    monkeypatch.setattr(flow.oauth.OAuthTables, "insert", fail)
    refused(lambda: flow.start(ip="203.0.113.99"), "ledger_unavailable")
    assert all(flow.status(id) == "pending_owner" for id in ids)
    assert eviction_audit(flow) == []


if pg_url():
    @pytest.mark.parametrize("case", ["ip", "eviction", "binding"])
    @pytest.mark.parametrize("stage", ["approval_requested", "approved", "code_issued"])
    def test_oauth_admission_caps_and_eviction_are_serialized_on_pg(pg_aw, case, stage):
        from concurrent.futures import ThreadPoolExecutor

        flow = Flow(pg_aw)
        bound = flow.start()
        flow.request(bound)
        if stage != "approval_requested":
            flow.decide(bound)
        if stage == "code_issued":
            flow.complete(bound)
        if case == "eviction":
            ids = [flow.start(ip=f"203.0.113.{i}") for i in range(5)]
            # Initial rows precede the race; only initial rows tie on creation time.
            flow.aw.w.advance(1)
        elif case == "binding":
            for i in range(3):
                flow.request(flow.start(ip=f"203.0.113.{i}"))
            ids = [flow.start(ip=f"203.0.113.{i + 10}") for i in range(2)]
        barrier = threading.Barrier(2)

        def work(i):
            barrier.wait()
            try:
                if case == "binding":
                    flow.request(ids[i])
                    return "bound"
                return flow.start(ip=IP_A if case == "ip" else f"203.0.113.{i + 100}")
            except (flow.oauth.OAuthError, api.LedgerError) as exc:
                return exc.code

        if case == "ip":
            flow.start()
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(work, range(2)))
        if case == "ip":
            assert outcomes.count("oauth_pending_limit") == 1
            assert sum(value.startswith("OR_") for value in outcomes) == 1
        elif case == "binding":
            assert sorted(outcomes) == ["bound", "oauth_not_approved"]
        else:
            assert all(value.startswith("OR_") for value in outcomes)
            oldest = sorted(ids)[:2]
            assert {id for id in ids if flow.status(id) == "cancelled"} == set(oldest)
            survivors = set(ids) - set(oldest)
            assert all(flow.status(id) == "pending_owner" for id in [*survivors, *outcomes])
            assert eviction_audit(flow) == ["evicted_unbound"] * 2
        assert flow.status(bound) == stage
