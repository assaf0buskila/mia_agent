"""V2-31: the live probes of ``mia os-authority`` (V2_PG_CONTRACTS.md section 16).

The Script fence probe runs against a fake transport, the approval-card and unknown-receipt checks
against an in-memory SQLite copy of the real tables, and ``LiveProbes`` against fakes for its
engines. Nothing here opens a socket or touches a secret store.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.db.models import ApprovalRow, Base, IdempotencyRow
from app.services import assaf_os
from app.services.owner_actions import ACTION_OWNER_EXTERNAL_WRITE, RESOURCE_OWNER_PROPOSAL
from app.workers import os_authority as cli
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
SECRET = "S" * 40
URL = "https://script.google.com/macros/s/" + "A" * 40 + "/exec"
BOOK = "B" * 44
OWNER = "123456789"
ROOT = Path(__file__).resolve().parents[2]


def settings(**over):
    values = {
        "assaf_os_enabled": True, "assaf_os_script_url": URL, "assaf_os_workbook_id": BOOK,
        "assaf_os_workspace_id": "assaf-os-main", "assaf_os_hmac_secret": SECRET,
    }
    values.update(over)
    return SimpleNamespace(**values)


def wrap(payload: str, signature: str) -> bytes:
    return json.dumps({"payload": payload, "signature": signature}).encode()


def sign(payload: str, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


class Transport:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def __call__(self, url, body, budget):
        self.calls.append((url, body, budget))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply(body) if callable(self.reply) else self.reply


def signed_live_reply(body: bytes) -> bytes:
    request = json.loads(json.loads(body)["payload"])
    inner = json.dumps({
        "api_version": request["api_version"], "operation": "schema", "actor_source": "MIA",
        "owner_id": request["owner_id"], "workspace_id": request["workspace_id"],
        "workbook_id": request["workbook_id"], "request_id": request["request_id"],
        "timestamp": request["timestamp"], "mode": "LIVE", "ok": True, "data": {},
    })
    return wrap(inner, sign(inner))


# --------------------------------------------------------------------------- the Script fence probe


def test_the_fenced_reply_is_the_one_the_script_source_gives():
    # tests/assaf-os/apps-script-fence.test.js runs the real Script under FENCED and pins this exact
    # object between two markers. The two files cannot drift apart without a test failing.
    source = (ROOT / "tests" / "assaf-os" / "apps-script-fence.test.js").read_text(encoding="utf-8")
    match = re.search(r"// FENCED_REPLY:begin\n(.*?)\n// FENCED_REPLY:end", source, re.S)
    assert match, "the fence harness must pin FENCED_REPLY between its two markers"
    literal = re.sub(r"^\s*(?:const|let)\s+\w+\s*=\s*", "", match.group(1)).rstrip(";\n ")
    assert json.loads(literal) == cli.FENCED_REPLY


def test_the_journal_tabs_and_cap_are_the_ones_the_script_source_gives():
    # `journal_capacity` measures the archive workbook's journal tabs against the Script's own cap.
    # The Script's source is the only authority for both, so the CLI's constants are read back from
    # it here, the way FENCED_REPLY is, and cannot drift from it without a test failing.
    source = (ROOT / "ops" / "assaf-os" / "UnifiedRuntime.gs").read_text(encoding="utf-8")
    cap = re.findall(r"^var ASSAF_OS_MAX_ROWS = (\d+);$", source, re.M)
    assert len(cap) == 1, "UnifiedRuntime.gs must declare ASSAF_OS_MAX_ROWS exactly once"
    assert int(cap[0]) == cli.JOURNAL_MAX_ROWS
    block = re.search(r"^var ASSAF_OS_META = \{\n(.*?)^\};$", source, re.S | re.M)
    assert block, "UnifiedRuntime.gs must declare ASSAF_OS_META as a block"
    names = re.findall(r'^\s+\w+: \{ name: "(_assaf_os_[a-z]+)"', block.group(1), re.M)
    assert len(names) == 5, names
    assert sorted(names) == sorted(cli.JOURNAL_TABS) and len(set(cli.JOURNAL_TABS)) == 5


def test_an_unsigned_fenced_reply_is_unavailable():
    post = Transport(wrap(json.dumps(cli.FENCED_REPLY), ""))
    assert cli.fence_state(settings(), OWNER, post=post) == ("unavailable", "")


def test_the_probe_is_one_signed_read_only_schema_call_with_a_fixed_budget():
    post = Transport(wrap(json.dumps(cli.FENCED_REPLY), ""))
    cli.fence_state(settings(), OWNER, post=post)
    assert len(post.calls) == 1
    url, body, budget = post.calls[0]
    assert url == URL and budget == cli.FENCE_PROBE_BUDGET_SECONDS
    outer = json.loads(body)
    assert set(outer) == {"payload", "signature"}
    assert outer["signature"] == sign(outer["payload"])
    request = json.loads(outer["payload"])
    assert request["operation"] == "schema" and request["actor_source"] == "MIA"
    assert request["owner_id"] == OWNER and request["data"] == {}
    assert request["workbook_id"] == BOOK and request["workspace_id"] == "assaf-os-main"
    assert request["api_version"] == assaf_os.API_VERSION
    assert request["operation"] not in {"propose", "approve", "reject"}


def test_a_validly_signed_reply_means_the_script_is_live():
    assert cli.fence_state(settings(), OWNER, post=Transport(signed_live_reply)) == ("live", "")


@pytest.mark.parametrize("reply,detail", [
    (b"not json", "transport"),
    (json.dumps(["x"]).encode(), "reply_shape"),
    (json.dumps({"payload": "{}"}).encode(), "reply_shape"),
    (json.dumps({"payload": "{}", "signature": "", "extra": 1}).encode(), "reply_shape"),
    (json.dumps({"payload": 1, "signature": ""}).encode(), "reply_shape"),
    (wrap("{}", ""), "reply_unexpected"),
    (wrap(json.dumps({**cli.FENCED_REPLY, "ok": True}), ""), "reply_unexpected"),
    (wrap(json.dumps({**cli.FENCED_REPLY, "workspace_id": "x"}), ""), "reply_unexpected"),
    (wrap("{}", "0" * 64), "reply_signature"),
    (wrap("{}", sign("{}", "T" * 40)), "reply_signature"),
    (wrap("{}", "short"), "reply_signature"),
])
def test_anything_else_is_unknown_never_unavailable(reply, detail):
    assert cli.fence_state(settings(), OWNER, post=Transport(reply)) == ("unknown", detail)


@pytest.mark.parametrize("error", [TimeoutError("x"), OSError("net"), ValueError(SECRET)])
def test_a_transport_failure_is_unknown_and_prints_nothing(error):
    state = cli.fence_state(settings(), OWNER, post=Transport(error))
    assert state == ("unknown", "transport")
    assert SECRET not in repr(state)


@pytest.mark.parametrize("over", [
    {"assaf_os_enabled": False},
    {"assaf_os_script_url": ""},
    {"assaf_os_script_url": URL.replace("script.google.com", "evil.example")},
    {"assaf_os_script_url": URL + "?x=1"},
    {"assaf_os_script_url": URL.replace("https", "http")},
    {"assaf_os_script_url": URL.replace("/exec", "/dev")},
    {"assaf_os_workbook_id": "short"},
    {"assaf_os_workspace_id": "!"},
    {"assaf_os_hmac_secret": "x" * 31},
])
def test_an_unconfigured_probe_sends_nothing(over):
    post = Transport(wrap(json.dumps(cli.FENCED_REPLY), ""))
    assert cli.fence_state(settings(**over), OWNER, post=post) == ("unknown", "not_configured")
    assert post.calls == []


@pytest.mark.parametrize("owner", ["", "0", "abc", "-1", "0123"])
def test_the_probe_needs_a_numeric_owner(owner):
    post = Transport(wrap(json.dumps(cli.FENCED_REPLY), ""))
    assert cli.fence_state(settings(), owner, post=post) == ("unknown", "not_configured")
    assert post.calls == []


def test_the_default_transport_is_the_existing_script_transport(monkeypatch):
    seen = []
    monkeypatch.setattr(
        assaf_os, "_post", lambda url, body, budget: seen.append((url, budget)) or wrap(
            json.dumps(cli.FENCED_REPLY), ""),
    )
    assert cli.fence_state(settings(), OWNER) == ("unavailable", "")
    assert seen == [(URL, cli.FENCE_PROBE_BUDGET_SECONDS)]


# --------------------------------------------------------------------------- approval cards


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


def card(db, n, *, kind="assaf_os.approve", executed="", expires=None, decision="pending",
         action=ACTION_OWNER_EXTERNAL_WRITE, resource=RESOURCE_OWNER_PROPOSAL, params=None):
    when = (NOW + timedelta(minutes=10)).isoformat() if expires is None else expires
    db.add(ApprovalRow(
        channel="telegram", action=action, risk="high", payload_hash="h" * 64, decision=decision,
        resource_type=resource, resource_id=f"op_{n}", expires_at=when, approval_id=f"ap{n}",
        actor_id=OWNER, executed_at=executed,
        proposed_parameters=(
            json.dumps({"kind": kind, "parameters": {}}) if params is None else params
        ),
    ))
    db.commit()


def test_a_pending_card_is_open_until_it_is_executed_or_expires(session):
    card(session, 1)
    assert cli.open_assaf_os_cards(session, NOW) == 1
    card(session, 2, executed=NOW.isoformat())
    card(session, 3, expires=(NOW - timedelta(seconds=1)).isoformat())
    card(session, 4, expires=NOW.isoformat())  # expired at exactly now
    assert cli.open_assaf_os_cards(session, NOW) == 1
    card(session, 5, decision="rejected")  # rejected but its reject was never executed
    assert cli.open_assaf_os_cards(session, NOW) == 2


def test_other_approval_kinds_and_actions_are_not_this_checks_business(session):
    card(session, 1, kind="gmail.send")
    card(session, 2, action="other_action")
    card(session, 3, resource="lead")
    assert cli.open_assaf_os_cards(session, NOW) == 0


def test_an_unreadable_card_or_expiry_counts_as_open(session):
    card(session, 1, params="assaf_os.approve but not json")
    card(session, 2, expires="not a time")
    card(session, 3, expires="")
    card(session, 4, expires="2026-10-20T11:00:00")  # naive: proves nothing
    assert cli.open_assaf_os_cards(session, NOW) == 4


def test_a_card_that_only_mentions_the_kind_in_text_is_not_one(session):
    card(session, 1, kind="gmail.send", params=json.dumps(
        {"kind": "gmail.send", "parameters": {"note": "assaf_os.approve"}}))
    assert cli.open_assaf_os_cards(session, NOW) == 0


def stuck(db, key, status="provider_claimed", scope="approval"):
    db.add(IdempotencyRow(
        scope=scope, key=key, status=status, created_at=NOW.isoformat(), expires_at="",
        result_json="{}",
    ))
    db.commit()


def test_an_assaf_os_decision_that_never_confirmed_is_an_unknown_receipt(session):
    card(session, 1)
    card(session, 2)
    card(session, 3, kind="gmail.send")
    stuck(session, "op_1:execute")
    stuck(session, "op_2:reject", status="pending_review")
    stuck(session, "op_3:execute")  # a Gmail card: not an Assaf OS write
    assert cli.unknown_assaf_os_receipts(session) == 2


def test_settled_or_foreign_idempotency_rows_do_not_count(session):
    card(session, 1)
    stuck(session, "op_1:execute", status="completed")
    stuck(session, "op_1:reject", status="failed")
    stuck(session, "op_1:execute-x", scope="approval")  # not a card key
    stuck(session, "op_1:other", scope="approval")
    stuck(session, "op_1:execute", scope="webhook")
    assert cli.unknown_assaf_os_receipts(session) == 0


def test_a_claim_whose_card_is_gone_counts_because_it_cannot_be_ruled_out(session):
    stuck(session, "op_404:execute")
    assert cli.unknown_assaf_os_receipts(session) == 1


def test_a_claim_whose_card_cannot_be_read_counts(session):
    card(session, 1, params="{broken")
    stuck(session, "op_1:execute")
    assert cli.unknown_assaf_os_receipts(session) == 1


# --------------------------------------------------------------------------- LiveProbes


def probes(**over):
    values = {
        "main_url": "postgresql://app@127.0.0.1:1/mia", "ledger_url": "postgresql://l@127.0.0.1:1/mia",
        "privileged_url": "postgresql://p@127.0.0.1:1/mia", "now": lambda: NOW,
    }
    values.update(over)
    return cli.LiveProbes(settings(), OWNER, **values)


@pytest.mark.parametrize("row,version,expected", [
    (None, 3, "empty"),
    (("synced", 3, 3, None), 2, "verified"),
    (("synced", 3, 3, None), 3, "verified"),
    (("synced", 3, 3, None), 4, "unverified"),
    (("synced", 3, 2, None), 2, "unverified"),
    (("synced", 3, 3, "readback_failed"), 3, "unverified"),
    (("delayed", 3, 3, None), 3, "unverified"),
    (("unknown", 3, 3, None), 3, "unverified"),
    (("conflict", 3, 3, None), 3, "unverified"),
    (("pending", 0, 0, None), 0, "unverified"),
    (("synced", 0, 0, None), 0, "verified"),
    (None, None, "empty"),
    (("synced", 3, 3, None), None, "verified"),
    (("synced", 3, 2, None), None, "unverified"),
    (("synced", 3, 3, "readback_failed"), None, "unverified"),
    (("unknown", 3, 3, None), None, "unverified"),
    (("conflict", 3, 3, None), None, "unverified"),
    (("pending", 3, 3, None), None, "unverified"),
    (("delayed", 3, 3, None), None, "unverified"),
    (("fenced", 3, 3, None), None, "unverified"),
])
def test_mirror_probe_binds_verification_to_the_requested_version(row, version, expected):
    engine = create_engine("sqlite://")
    live = probes()
    live._engines["privileged"] = engine
    try:
        with engine.begin() as connection:
            connection.execute(text("ATTACH DATABASE ':memory:' AS assaf_os"))
            connection.execute(text(
                "CREATE TABLE assaf_os.os_mirror_state (workspace_id TEXT, status TEXT, "
                "projected_version INTEGER, verified_version INTEGER, last_error_code TEXT)"
            ))
            if row is not None:
                connection.execute(text(
                    "INSERT INTO assaf_os.os_mirror_state VALUES "
                    "(:workspace, :status, :projected, :verified, :error)"
                ), dict(zip(("workspace", "status", "projected", "verified", "error"),
                            ("ws-1", *row), strict=True)))
        assert live.mirror("ws-1", version) == expected
        assert live.mirror("another-workspace", version) == "empty"
    finally:
        live.close()


def test_a_missing_dsn_is_a_fixed_refusal_not_a_default_database():
    live = probes(ledger_url="")
    with pytest.raises(cli.Refusal) as refused:
        live.ledger_role_safe()
    assert refused.value.codes == ["dsn_missing"]


def test_engines_are_built_lazily_cached_and_disposed(monkeypatch):
    built = []

    class Engine:
        def __init__(self, url, **options):
            self.url, self.options, self.disposed = url, options, False
            built.append(self)

        def dispose(self):
            self.disposed = True

    import sqlalchemy

    monkeypatch.setattr(sqlalchemy, "create_engine", Engine)
    live = probes()
    assert built == []
    first = live._engine("main")
    assert live._engine("main") is first and len(built) == 1
    assert first.options == {"connect_args": {"connect_timeout": 10}}
    assert first.url.startswith("postgresql+psycopg://")
    live._engine("privileged")
    live.close()
    assert all(engine.disposed for engine in built) and live._engines == {}


def test_the_snapshot_drain_probe_uses_the_sheets_backend_only(monkeypatch):
    from app.services import os_backend

    class Sheets:
        kind = "sheets"
        calls = 0

        def snapshot(self):
            Sheets.calls += 1
            return {"pending_proposals": [{"id": 1}, {"id": 2}]}

    seen = {}

    def factory(config, owner, *, actor, principal=None):
        seen.update(owner=owner, actor=actor)
        return Sheets()

    monkeypatch.setattr(os_backend, "get_os_backend", factory)
    assert probes().sheets_snapshot_pending() == 2
    assert seen == {"owner": OWNER, "actor": "MIA"} and Sheets.calls == 1


@pytest.mark.parametrize("backend,reason", [
    (SimpleNamespace(kind="postgres"), "backend_not_sheets"),
    (SimpleNamespace(kind="sheets", snapshot=lambda: {"pending_proposals": None}),
     "snapshot_unreadable"),
    (SimpleNamespace(kind="sheets", snapshot=lambda: {}), "snapshot_unreadable"),
])
def test_the_snapshot_drain_probe_refuses_a_wrong_backend_or_an_unreadable_snapshot(
    monkeypatch, backend, reason
):
    from app.services import os_backend

    monkeypatch.setattr(os_backend, "get_os_backend", lambda *a, **k: backend)
    with pytest.raises(cli.Refusal) as refused:
        probes().sheets_snapshot_pending()
    assert refused.value.codes == [reason]


def test_the_card_probes_read_the_main_database(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    live = probes()
    live._engines["main"] = engine
    with Session(engine) as db:
        card(db, 1)
        stuck(db, "op_1:execute")
    assert live.open_cards() == 1 and live.unknown_receipts() == 1
    engine.dispose()
