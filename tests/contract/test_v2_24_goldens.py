"""The V2-24 golden fixtures (§10.4, §11.1, §22 E12/E14.3), pinned byte for byte.

``tests/contract/shapes/v2_24.py`` builds each body through the real ``owner_direct`` service on a
deterministic FakeLedger. This test renders it exactly as V2-20's export registry does (sorted keys,
2-space indent, UTF-8, LF, one trailing newline) and compares it with the committed file. Once V2-20
merges, its registry test covers the same files; this one stays as the chunk's own pin.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHAPES_FILE = ROOT / "tests" / "contract" / "shapes" / "v2_24.py"
GOLDEN_DIR = ROOT / "tests" / "contract" / "golden" / "v2-24"
GOLDEN_NAMES = sorted(path.stem for path in GOLDEN_DIR.glob("*.json"))

BINDING = {"api_version": "assaf-os.v2", "source": "postgresql", "workspace_id": "test",
           "authority_epoch": 1}
RECEIPT_KEYS = {*BINDING, "observed_version", "status", "request_id", "proposal_id", "kind",
                "actor_source", "commit_version", "committed_at", "server_now", "records",
                "undo", "undo_of", "reason_codes"}
DELETED_KEYS = {*BINDING, "observed_version", "entity", "records", "next_cursor"}
ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


def render(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


@pytest.fixture(scope="module")
def shapes():
    spec = importlib.util.spec_from_file_location("os_contract_shapes_v2_24", SHAPES_FILE)
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


def check_receipt(body: dict) -> None:
    """The §10.2 ``receipt_v2`` rules, with E12 (binding + observed_version) and E14.3."""
    assert set(body) == RECEIPT_KEYS
    assert {k: body[k] for k in BINDING} == BINDING
    assert type(body["observed_version"]) is int and body["observed_version"] >= 1
    assert body["observed_version"] >= body["commit_version"] >= 1
    assert body["status"] in {"committed", "target_changed", "abandoned"}
    assert body["kind"] in {"owner_direct", "owner_undo"}
    assert body["actor_source"] == "OS_DIRECT"
    assert UUID.fullmatch(body["request_id"]) and body["proposal_id"].startswith("PRO_")
    assert ISO.fullmatch(body["server_now"])
    assert (body["committed_at"] is not None) == (body["status"] == "committed")
    assert body["committed_at"] is None or ISO.fullmatch(body["committed_at"])
    if body["status"] == "committed":
        assert body["records"], "E14.3: a committed owner_direct/owner_undo receipt has records"
    if body["status"] == "abandoned":
        assert body["records"] == []
    for item in body["records"]:
        assert set(item) == {"entity", "record"} and type(item["record"]["revision"]) is int
    assert (body["undo"] is not None) == (body["kind"] == "owner_direct")
    assert (body["undo_of"] is not None) == (body["kind"] == "owner_undo")
    if body["undo"] is not None:
        assert set(body["undo"]) == {"state", "until", "undo_request_id"}
        if body["status"] != "committed":
            assert body["undo"] == {"state": "none", "until": None, "undo_request_id": None}
        else:
            assert body["undo"]["state"] in {"available", "undone", "expired"}
            assert ISO.fullmatch(body["undo"]["until"])
            assert (body["undo"]["undo_request_id"] is not None) == (
                body["undo"]["state"] == "undone")
    assert body["reason_codes"] == []


@pytest.mark.parametrize("name", GOLDEN_NAMES)
def test_each_golden_follows_the_v2_wire_rules(name):
    body = json.loads(golden(name))
    if name == "deleted_page":
        assert set(body) == DELETED_KEYS
        assert {k: body[k] for k in BINDING} == BINDING
        assert type(body["observed_version"]) is int and body["entity"] == "tasks"
        assert body["records"] and body["next_cursor"] is None
        return
    check_receipt(body)
    assert name.startswith(("commit_", "undo_", "abandon_"))
    if name.startswith("undo_"):
        assert body["kind"] == "owner_undo"
    if name.startswith("abandon_"):
        assert body["status"] == "abandoned"
    if name.endswith("target_changed"):
        assert body["status"] == "target_changed" and body["records"]
