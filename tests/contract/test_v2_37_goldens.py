"""The V2-37 golden fixtures (§17, §10.1, §22 E12, A4), pinned byte for byte.

``tests/contract/shapes/v2_37.py`` builds each body through the real services and the routes' own
body builders on a deterministic FakeLedger. This test renders it exactly as V2-20's export registry
does (sorted keys, 2-space indent, UTF-8, LF, one trailing newline) and compares it with the
committed file. Once V2-20 merges, its registry test covers the same files; this one stays as the
chunk's own pin.

The rules they prove:

* the owner's key bodies carry the §10.1 binding and NO ``observed_version`` (E12 a, b);
* the step-up body is exactly ``{step_up_token, expires_at}`` (A4);
* the bearer bodies are ``assaf-os-assistant.v1`` and carry no decision id, no request id, no review
  digest and no binding;
* the secret appears in the issue body and nowhere else, and no body carries a hash.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHAPES_FILE = ROOT / "tests" / "contract" / "shapes" / "v2_37.py"
GOLDEN_DIR = ROOT / "tests" / "contract" / "golden" / "v2-37"
GOLDEN_NAMES = sorted(path.stem for path in GOLDEN_DIR.glob("*.json"))

BINDING = {"api_version": "assaf-os.v2", "source": "postgresql", "workspace_id": "test",
           "authority_epoch": 1}
ITEM_KEYS = {"credential_id", "label", "kind", "created_at", "expires_at", "last_used_at",
             "use_count", "revoked_at"}
OWNER_SHAPES = {"keys_list_empty", "keys_list", "key_issued", "key_revoked", "keys_revoked_all"}
BEARER_SHAPES = {"assistant_context", "assistant_batch_submitted", "assistant_batch_status",
                 "assistant_batch_status_rejected"}
ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
FORBIDDEN = ("approval", "rejection", "decision", "request_id", "review_digest", "secret_hash",
             "observed_version")
SECRET = re.compile(r"aosp_[A-Za-z0-9_-]{43}")


def render(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


@pytest.fixture(scope="module")
def shapes():
    spec = importlib.util.spec_from_file_location("os_contract_shapes_v2_37", SHAPES_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module.SHAPES


def golden(name: str) -> bytes:
    return (GOLDEN_DIR / f"{name}.json").read_bytes().replace(b"\r\n", b"\n")


def test_the_golden_tree_is_exactly_the_shapes(shapes):
    assert GOLDEN_NAMES == sorted(shapes)
    assert sorted(p.name for p in GOLDEN_DIR.iterdir()) == [f"{n}.json" for n in GOLDEN_NAMES]
    assert set(GOLDEN_NAMES) == OWNER_SHAPES | BEARER_SHAPES | {"step_up_issued"}


@pytest.mark.parametrize("name", GOLDEN_NAMES)
def test_each_golden_is_the_service_output_byte_for_byte(shapes, name):
    first, second = shapes[name](), shapes[name]()
    assert render(first) == render(second), "shape is not deterministic"
    assert render(first) == golden(name)


@pytest.mark.parametrize("name", sorted(OWNER_SHAPES))
def test_each_owner_key_body_has_the_binding_and_no_observed_version(name):
    body = json.loads(golden(name))
    assert {k: body[k] for k in BINDING} == BINDING  # E12 a
    assert "observed_version" not in body  # E12 b
    if name in {"keys_list_empty", "keys_list", "key_revoked", "keys_revoked_all"}:
        assert set(body) == BINDING.keys() | {"credentials"}
        assert all(set(item) == ITEM_KEYS for item in body["credentials"])
    else:
        assert set(body) == BINDING.keys() | ITEM_KEYS | {"secret"}
    for stamp in ("created_at", "expires_at", "last_used_at", "revoked_at"):
        for item in body.get("credentials", [body]):
            assert item[stamp] is None or ISO.fullmatch(item[stamp])


def test_the_secret_is_in_the_issue_body_and_nowhere_else():
    issued = json.loads(golden("key_issued"))
    assert SECRET.fullmatch(issued["secret"])
    for name in GOLDEN_NAMES:
        if name != "key_issued":
            assert "aosp_" not in golden(name).decode("utf-8"), name
    assert issued["secret"] not in golden("keys_list").decode("utf-8")


def test_the_list_shows_a_used_key_an_unused_key_and_a_revoked_key():
    items = json.loads(golden("keys_list"))["credentials"]
    assert any(i["use_count"] > 0 and i["last_used_at"] for i in items)
    assert any(i["use_count"] == 0 and i["last_used_at"] is None and i["revoked_at"] is None
               for i in items)
    assert any(i["revoked_at"] for i in items)
    assert {i["kind"] for i in items} == {"claude", "gpt"}


def test_the_step_up_body_is_exactly_the_token_and_its_expiry():
    body = json.loads(golden("step_up_issued"))
    assert set(body) == {"step_up_token", "expires_at"}  # A4: no binding, no version
    assert re.fullmatch(r"su1\.[0-9]+\.[0-9]+\.[0-9a-f]{64}", body["step_up_token"])
    assert ISO.fullmatch(body["expires_at"])


@pytest.mark.parametrize("name", sorted(BEARER_SHAPES))
def test_each_bearer_body_is_assistant_v1_with_no_decision_id_and_no_binding(name):
    body = json.loads(golden(name))
    assert body["api_version"] == "assaf-os-assistant.v1"
    assert not {"source", "workspace_id", "authority_epoch", "observed_version"} & set(body)
    lowered = golden(name).decode("utf-8").lower()
    assert not [word for word in FORBIDDEN if word in lowered]
    assert not re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", lowered), (
        "a UUID is a decision or request id")


def test_the_submit_body_is_exactly_the_pending_proposal_and_its_review_url():
    body = json.loads(golden("assistant_batch_submitted"))
    assert set(body) == {"api_version", "proposal_id", "status", "review_url", "summary"}
    assert body["status"] == "pending"
    assert body["review_url"] == f"https://owner.assafweb.test/#statements/{body['proposal_id']}"
    assert set(body["summary"]) == {"line_count", "business_count", "personal_count",
                                    "unassigned_count"}


def test_the_status_bodies_and_the_context_have_exactly_their_keys():
    pending = json.loads(golden("assistant_batch_status"))
    rejected = json.loads(golden("assistant_batch_status_rejected"))
    for body in (pending, rejected):
        assert set(body) == {"api_version", "proposal_id", "status", "updated_at"}
        assert ISO.fullmatch(body["updated_at"])
    assert (pending["status"], rejected["status"]) == ("pending", "rejected")
    context = json.loads(golden("assistant_context"))
    assert set(context) == {"api_version", "rules", "statements", "format"}
    assert context["rules"] and len(context["statements"]) <= 12
    assert all(set(r) == {"merchant_key", "match", "category", "path", "platform",
                          "is_subscription", "label"} for r in context["rules"])
    assert all(set(s) == {"account_label", "charge_date", "status", "line_count"}
               for s in context["statements"])


@pytest.mark.parametrize("name", GOLDEN_NAMES)
def test_no_golden_carries_personal_statement_text(name):
    text = golden(name).decode("utf-8")
    assert not [t for t in ("PERSONAL CAFE", "personal cafe", "MY LUNCH", "421731") if t in text]
