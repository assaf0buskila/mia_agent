"""V2-37 boundaries that need no database: what the assistant code can call, what the limiter may
write, and that the Caddyfile is untouched (§17.2: the per-IP limit lives in MIA).
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest
from app.services.os_ledger import authorize, role_policy

ROOT = Path(__file__).resolve().parents[2]
MODULES = (
    "app/services/assistant_keys.py", "app/services/assistant_issue.py",
    "app/api/assistant/os.py", "app/api/os_v2/owner_keys.py", "app/api/owner_step_up.py",
)
# Every ledger operation that decides, approves, edits or commits. None of the assistant code may
# call one: an assistant proposes (``submit``) and reads; the owner's key routes revoke.
FORBIDDEN_CALLS = frozenset({
    "approve", "reject", "decide", "propose", "commit", "undo", "abandon", "save_draft",
    "expire_due", "review", "lines", "cycles", "statement_view", "merchant_rules", "apply",
    "finish", "insert_proposal", "pin_statement_pepper", "transition", "bump_epoch",
})
ALLOWED_STATEMENT_CALLS = frozenset({
    "submit", "context_for_assistant", "status_for_credential", "revoke_pending_for_credential"})
# The Caddyfile at the V2-37 base (identical on master): everything is proxied to MIA unchanged,
# and the stock caddy image has no rate-limit directive, so V2-37 limits per address in MIA.
CADDYFILE_SHA256 = "b8edabca7783fc259a61333f426897b7296b6ca931f6b56cffbc6b9f67267992"


def attribute_calls(path: str) -> list[tuple[str, str, int]]:
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = ast.unparse(node.func.value)
            found.append((owner, node.func.attr, node.lineno))
    return found


@pytest.mark.parametrize("path", MODULES)
def test_no_assistant_module_calls_a_deciding_or_committing_operation(path):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    db_commits = {node.lineno for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and ast.unparse(node.func.value) == "db" and node.func.attr == "commit"
                  and not node.args and not node.keywords}
    if path == "app/api/os_v2/owner_keys.py":
        # E39 adds the OAuth card: each card commits, then closes its rendering read.
        assert len(db_commits) == 4
    else:
        assert db_commits == set()
    bad = [(owner, name, line) for owner, name, line in attribute_calls(path)
           if name in FORBIDDEN_CALLS and not (path == "app/api/os_v2/owner_keys.py"
                                              and line in db_commits)]
    assert bad == [], bad


@pytest.mark.parametrize("path", MODULES)
def test_the_only_statement_service_calls_are_the_four_propose_read_and_revoke_ones(path):
    used = {name for owner, name, _ in attribute_calls(path)
            if "statements" in owner.lower() and not name.startswith("_")}
    assert used <= ALLOWED_STATEMENT_CALLS, used


def test_the_census_sees_exactly_one_submit_call_in_the_assistant_modules():
    sites = [(path, name) for path in MODULES for _owner, name, _ in attribute_calls(path)
             if name in {"submit", "undo", "abandon"}]
    assert sites == [("app/api/assistant/os.py", "submit")]


def test_the_assistant_and_step_up_modules_never_touch_the_trusted_context_internals():
    for path in MODULES:
        source = (ROOT / path).read_text(encoding="utf-8")
        # Exact identifiers: E37's module/table names contain the ordinary word "issue".
        tree = ast.parse(source)
        identifiers = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        identifiers |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        identifiers |= {node.name for node in ast.walk(tree) if isinstance(node, ast.alias)}
        assert not identifiers & {"_issue", "_CAPABILITY", "_MINTED", "_register"}
        for name in ("import TrustedOwnerContext",
                     "owner_direct", "_MutationProof", "_attach_mutation_proof"):
            assert name not in source, (path, name)


def test_issue_census_mints_only_inside_collect_and_routes_only_request_or_collect():
    mint_sites = [(path, owner, name) for path in MODULES
                  for owner, name, _ in attribute_calls(path) if name == "_new_secret"]
    assert mint_sites == [("app/services/assistant_issue.py", "self", "_new_secret")]
    tree = ast.parse((ROOT / "app/services/assistant_issue.py").read_text(encoding="utf-8"))
    sites = [(fn.name, node.func.attr) for fn in ast.walk(tree)
             if isinstance(fn, ast.FunctionDef) for node in ast.walk(fn)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr == "_new_secret"]
    assert sites == [("collect", "_new_secret")]


def test_the_runtime_role_may_only_select_and_insert_audit_rows_and_update_three_key_columns():
    assert role_policy.TABLE_PRIVILEGES["os_assistant_audit"] == frozenset({"SELECT", "INSERT"})
    assert role_policy.TABLE_PRIVILEGES["os_assistant_credentials"] == frozenset(
        {"SELECT", "INSERT"})
    assert "os_assistant_audit" not in role_policy.ADDENDUM_COLUMN_UPDATES  # never updatable
    assert role_policy.ADDENDUM_COLUMN_UPDATES["os_assistant_credentials"] == (
        "revoked_at", "last_used_at", "use_count")
    assert "DELETE" not in role_policy.TABLE_PRIVILEGES["os_assistant_audit"]
    assert role_policy.RETENTION_TABLE_PRIVILEGES["os_assistant_audit"] == frozenset(
        {"SELECT", "DELETE"})


def test_the_scopes_of_an_assistant_key_are_exactly_the_three_the_row_check_allows():
    assert authorize.ASSISTANT_SCOPES == frozenset(
        {"merchant_rules:read", "own_proposals:read", "statement:propose"})
    sql = (ROOT / "migrations_assaf_os" / "20261016_assaf_os_assistant.sql").read_text(
        encoding="utf-8")
    assert "scopes = 'merchant_rules:read own_proposals:read statement:propose'" in sql


def test_the_caddyfile_is_unchanged_and_has_no_rate_limit_directive():
    raw = (ROOT / "ops" / "gcp" / "vm" / "Caddyfile").read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(raw).hexdigest() == CADDYFILE_SHA256
    text = raw.decode("utf-8")
    assert "rate_limit" not in text and "/v1/assistant" not in text
    assert "reverse_proxy 127.0.0.1:8000" in text


def test_the_default_revoke_all_watermark_is_the_one_v2_26_reads():
    from app.services import assistant_keys
    from app.services.os_ledger import statements

    gateway = assistant_keys.AssistantGateway(object(), store=object(), statements=object())
    assert gateway._watermark is statements.db_revoke_watermark  # D4: no second implementation
