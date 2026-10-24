"""The V2-26 golden fixtures (§12.4, §10.1, §10.2, §22 E12), pinned byte for byte.

``tests/contract/shapes/v2_26.py`` builds each body through the real ``LedgerStatementService`` on
a deterministic FakeLedger. This test renders it exactly as V2-20's export registry does (sorted
keys, 2-space indent, UTF-8, LF, one trailing newline) and compares it with the committed file.
Once V2-20 merges, its registry test covers the same files; this one stays as the chunk's own pin.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHAPES_FILE = ROOT / "tests" / "contract" / "shapes" / "v2_26.py"
GOLDEN_DIR = ROOT / "tests" / "contract" / "golden" / "v2-26"
GOLDEN_NAMES = sorted(path.stem for path in GOLDEN_DIR.glob("*.json"))

BINDING = {"api_version": "assaf-os.v2", "source": "postgresql", "workspace_id": "test",
           "authority_epoch": 1}
WITH_VERSION = {*BINDING, "observed_version"}
RECEIPT_KEYS = {*WITH_VERSION, "status", "request_id", "proposal_id", "kind", "actor_source",
                "commit_version", "committed_at", "server_now", "records", "undo", "undo_of",
                "reason_codes"}
REVIEW_KEYS = {*WITH_VERSION, "proposal_id", "statement_id", "cycle", "account_label",
               "charge_date", "actor_source", "credential_label", "submitted_at", "expires_at",
               "status", "approval_request_id", "rejection_request_id", "conflict_with",
               "draft_revision", "review_digest", "counts", "merges", "blocking", "totals",
               "line_count"}
LINE_KEYS = {"line_key", "txn_date", "merchant_display", "amount_minor", "kind", "currency",
             "original_amount_minor", "original_currency", "installment", "suggested_tag", "tag",
             "category", "path", "is_subscription", "rule_id", "disagreement", "confidence",
             "action", "flags", "candidate_expense_ids"}
CYCLE_KEYS = {"cycle", "account_label", "charge_date", "statement_id", "status",
              "business_total_minor", "business_line_count", "pending_proposal_id"}
TOTAL_KEYS = {"business_charge_minor", "business_credit_minor", "business_total_minor",
              "business_line_count", "personal_count", "unassigned_count"}
SHAPE_KEYS = {
    "batch_created": {*WITH_VERSION, "proposal_id", "statement_id", "status", "conflict",
                      "superseded_proposal_id", "summary"},
    "draft_saved": {*WITH_VERSION, "draft_revision", "review_digest", "blocking"},
    "batch_lines": {*WITH_VERSION, "lines", "next_cursor"},
    "statement_list": {*WITH_VERSION, "cycles"},
    "statement_view": {*WITH_VERSION, "statement_id", "cycle", "account_label", "charge_date",
                       "cycle_from", "cycle_to", "totals", "by_category", "subscriptions",
                       "previous"},
    "merchant_rules": {*WITH_VERSION, "rules"},
}
ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
# Text that exists only in a personal line of the synthetic worlds.
PERSONAL_TEXT = ("PERSONAL CAFE", "personal cafe", "MY LUNCH", "421731")


def render(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


@pytest.fixture(scope="module")
def shapes():
    spec = importlib.util.spec_from_file_location("os_contract_shapes_v2_26", SHAPES_FILE)
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


@pytest.mark.parametrize("name", GOLDEN_NAMES)
def test_each_golden_is_the_service_output_byte_for_byte(shapes, name):
    first, second = shapes[name](), shapes[name]()
    assert render(first) == render(second), "shape is not deterministic"
    assert render(first) == golden(name)


def kind_of(name: str) -> str:
    for prefix, kind in (("receipt_", "receipt"), ("batch_review_", "batch_review"),
                         ("batch_created_", "batch_created"), ("batch_lines_", "batch_lines")):
        if name.startswith(prefix):
            return kind
    return name


@pytest.mark.parametrize("name", GOLDEN_NAMES)
def test_each_golden_follows_the_v2_wire_rules(name):
    body = json.loads(golden(name))
    kind = kind_of(name)
    assert {k: body[k] for k in BINDING} == BINDING  # E12 a: the binding is on every body
    assert type(body["observed_version"]) is int and body["observed_version"] >= 0  # E12 b
    if kind == "receipt":
        assert set(body) == RECEIPT_KEYS
        assert body["kind"] == "statement_batch" and body["undo"] is None
        assert body["undo_of"] is None and ISO.fullmatch(body["server_now"])
        assert body["observed_version"] >= body["commit_version"] >= 1
        assert (body["committed_at"] is not None) == (body["status"] == "committed")
        assert (len(body["records"]) > 0) == (body["status"] == "committed")
        assert body["status"] in {"committed", "rejected", "expired"}
        assert len(body["reason_codes"]) <= 20
    elif kind == "batch_review":
        assert set(body) == REVIEW_KEYS and set(body["totals"]) == TOTAL_KEYS
        assert ISO.fullmatch(body["submitted_at"]) and ISO.fullmatch(body["expires_at"])
        assert re.fullmatch(r"[0-9a-f]{64}", body["review_digest"])
    else:
        assert set(body) == SHAPE_KEYS[kind]
    if kind == "batch_created":
        assert body["status"] == "pending"
        assert set(body["summary"]) == {"line_count", "business_count", "personal_count",
                                        "unassigned_count", "disagreement_count"}
    if kind == "batch_lines":
        assert body["lines"] and all(set(item) == LINE_KEYS for item in body["lines"])
        assert len(body["lines"]) <= 100
    if kind == "statement_list":
        assert len(body["cycles"]) == 36
        assert all(set(entry) == CYCLE_KEYS for entry in body["cycles"])
        missing = [e for e in body["cycles"] if e["status"] == "not_imported"]
        assert missing and all(e["business_total_minor"] is None for e in missing)  # null, not 0
    if kind == "statement_view":
        assert set(body["totals"]) == TOTAL_KEYS


@pytest.mark.parametrize("name", GOLDEN_NAMES)
def test_no_golden_carries_personal_statement_text(name):
    text = golden(name).decode("utf-8")
    assert not [secret for secret in PERSONAL_TEXT if secret in text]


def test_the_cycle_blocked_golden_carries_the_e31_code():
    body = json.loads(golden("batch_review_cycle_blocked"))
    assert body["blocking"] == ["cycle_has_statement"] and body["status"] == "pending"
    assert body["merges"] == [] and body["totals"]["unassigned_count"] == 0
