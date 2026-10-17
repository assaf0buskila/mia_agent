"""V2-20 gate: the golden fixtures (section 10.4) validate against the Python port of the readers.

Three things are pinned here:
  * the export registry: every ``tests/contract/shapes/*.py`` chunk exports deterministic bodies,
    the committed ``tests/contract/golden`` tree equals the export, and nothing stale remains;
  * the V2-20 goldens are exactly what the strict readers (``contract.read_*``, the port of the
    frontend's exact-key readers) accept, key for key;
  * those readers fail closed: one extra key, one missing key, a wrong binding, an unknown actor or
    a mixed v1/v2 body is refused.
"""

from __future__ import annotations

import contextlib
import copy
import json
import shutil
from pathlib import Path

import pytest
from app.services.os_ledger import contract
from scripts import export_os_contract_fixtures as exporter

from tests.contract.shapes import v2_20

GOLDEN = Path(__file__).parent / "golden"
CHUNK = "v2-20"
NAMES = sorted(v2_20.SHAPES)
# The schema and a lone proposal carry the binding without observed_version (section 10.1, 10.2).
NO_OBSERVED = {"schema", "proposal_create", "proposal_update", "proposal_promote_lead"}


def golden(name: str) -> dict:
    return json.loads((GOLDEN / CHUNK / f"{name}.json").read_text(encoding="utf-8"))


def reader(name: str):
    return contract.READERS[v2_20.READER_OF[name]]


# ---- the export registry -----------------------------------------------------------------------


def test_the_registry_imports_every_shapes_module_and_names_its_chunk():
    registry = exporter.load_registry()
    assert CHUNK in registry
    assert set(registry[CHUNK]) == set(v2_20.SHAPES)
    assert all(not chunk.startswith("_") for chunk in registry)


def test_the_registry_skips_private_modules(tmp_path):
    shapes = tmp_path / "shapes"
    shapes.mkdir()
    (shapes / "__init__.py").write_text("raise RuntimeError('must not load')\n")
    (shapes / "_helper.py").write_text("raise RuntimeError('must not load')\n")
    (shapes / "v9_99.py").write_text("SHAPES = {'only': lambda: {'a': 1}}\n")
    assert list(exporter.load_registry(shapes)) == ["v9-99"]


def test_the_registry_refuses_a_module_without_shapes(tmp_path):
    shapes = tmp_path / "shapes"
    shapes.mkdir()
    (shapes / "v9_98.py").write_text("OTHER = 1\n")
    with pytest.raises(exporter.ExportError, match="^shapes_missing$"):
        exporter.load_registry(shapes)


@pytest.mark.parametrize("bad", ["Upper", "has space", "../escape", "a/b", ""])
def test_the_registry_refuses_an_unsafe_shape_name(tmp_path, bad):
    shapes = tmp_path / "shapes"
    shapes.mkdir()
    (shapes / "v9_97.py").write_text(f"SHAPES = {{{bad!r}: lambda: {{}}}}\n")
    with pytest.raises(exporter.ExportError, match="^shape_name_invalid$"):
        exporter.load_registry(shapes)


@pytest.mark.parametrize("name", NAMES)
def test_a_shape_is_deterministic(name):
    assert exporter.render(v2_20.SHAPES[name]()) == exporter.render(v2_20.SHAPES[name]())


@pytest.mark.parametrize("name", NAMES)
def test_the_export_equals_the_committed_golden(name):
    committed = exporter.read_golden(GOLDEN / CHUNK / f"{name}.json")
    assert exporter.render(v2_20.SHAPES[name]()) == committed


def test_no_stale_or_missing_golden_file_exists_for_any_chunk():
    registry = exporter.load_registry()
    expected = {f"{chunk}/{name}.json" for chunk, shapes in registry.items() for name in shapes}
    found = {
        path.relative_to(GOLDEN).as_posix()
        for path in GOLDEN.rglob("*")
        if path.is_file() and path.name != "fixtures.sha256"
    }
    assert found == expected


def test_check_mode_passes_on_the_committed_tree():
    assert exporter.check(exporter.load_registry(), GOLDEN) == []


def test_check_mode_reports_a_changed_a_missing_and_a_stale_file(tmp_path):
    tree = tmp_path / "golden"
    shutil.copytree(GOLDEN, tree)
    (tree / CHUNK / "schema.json").write_text("{}\n", encoding="utf-8")
    (tree / CHUNK / "receipt_unknown.json").unlink()
    (tree / CHUNK / "left_over.json").write_text("{}\n", encoding="utf-8")
    problems = exporter.check(exporter.load_registry(), tree)
    assert problems == [
        f"{CHUNK}/left_over.json: stale",
        f"{CHUNK}/receipt_unknown.json: missing",
        f"{CHUNK}/schema.json: changed",
    ]


def test_check_mode_exit_code_and_write_mode_round_trip(tmp_path, capsys):
    out = tmp_path / "golden"
    assert exporter.main(["--out", str(out)]) == 0
    assert exporter.main(["--check", "--out", str(out)]) == 0
    (out / CHUNK / "schema.json").write_text("{}\n", encoding="utf-8")
    assert exporter.main(["--check", "--out", str(out)]) == 1
    assert "schema.json: changed" in capsys.readouterr().out


def test_a_shape_that_changes_between_runs_is_refused(tmp_path):
    shapes = tmp_path / "shapes"
    shapes.mkdir()
    (shapes / "v9_96.py").write_text(
        "import itertools\n_n = itertools.count()\nSHAPES = {'moving': lambda: {'n': next(_n)}}\n"
    )
    with pytest.raises(exporter.ExportError, match="^shape_not_deterministic$"):
        exporter.export(exporter.load_registry(shapes))


@pytest.mark.parametrize("name", NAMES)
def test_golden_files_are_lf_utf8_sorted_with_one_trailing_newline(name):
    raw = exporter.read_golden(GOLDEN / CHUNK / f"{name}.json")
    assert raw == exporter.render(v2_20.SHAPES[name]())
    text = raw.decode("utf-8")
    assert b"\r" not in raw and text.endswith("}\n") and not text.endswith("\n\n")
    assert text == json.dumps(json.loads(text), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def test_every_shape_names_a_reader_and_every_reader_is_covered():
    assert set(v2_20.READER_OF) == set(v2_20.SHAPES)
    assert set(v2_20.READER_OF.values()) == set(contract.READERS)


# ---- the goldens against the strict readers ----------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_the_golden_passes_its_strict_reader(name):
    body = golden(name)
    assert reader(name)(body) == body


@pytest.mark.parametrize("name", NAMES)
def test_every_body_carries_the_v2_binding_and_never_a_v1_key(name):
    body = golden(name)
    assert body["api_version"] == "assaf-os.v2" and body["source"] == "postgresql"
    assert isinstance(body["workspace_id"], str) and body["authority_epoch"] >= 1
    text = json.dumps(body, ensure_ascii=False)
    for forbidden in ("google_sheets", "workbook_id", '"mode"', "assaf-os.v1"):
        assert forbidden not in text


@pytest.mark.parametrize("name", NAMES)
def test_observed_version_is_on_every_read_body_except_the_schema_and_a_proposal(name):
    body = golden(name)
    if name in NO_OBSERVED:
        assert "observed_version" not in body
    else:
        assert type(body["observed_version"]) is int and body["observed_version"] >= 0


def test_the_unchanged_body_is_under_400_bytes():
    body = golden("snapshot_unchanged")
    assert set(body) == {
        "api_version", "source", "workspace_id", "authority_epoch", "observed_version",
        "unchanged", "since", "mirror",
    }
    assert body["unchanged"] is True
    assert len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) < 400


def test_the_page_body_has_exactly_the_documented_keys():
    assert set(golden("snapshot_tasks_page")) == {
        "api_version", "source", "workspace_id", "authority_epoch", "observed_version",
        "observed_at", "unchanged", "since", "entity", "records", "next_cursor", "total",
        "coverage", "summary", "pending_count", "labels", "mirror",
    }


def test_a_page_with_more_rows_carries_a_cursor_and_the_last_page_none():
    first, second = golden("snapshot_tasks_page"), golden("snapshot_tasks_second_page")
    assert first["next_cursor"] and second["next_cursor"] is None
    assert first["total"] == second["total"] == 3
    assert [r["task_id"] for r in first["records"]] == ["TAS_t1", "TAS_t2"]
    assert [r["task_id"] for r in second["records"]] == ["TAS_t3"]


def test_labels_cover_only_the_references_on_the_page():
    labels = golden("snapshot_tasks_page")["labels"]
    assert labels == {
        "clients.person_id": {"PER_c1": "דנה כהן"},
        "works.work_id": {"WOR_w1": "סדנת אוטומציה"},
    }
    assert golden("snapshot_expenses_page")["labels"] == {
        "clients.person_id": {}, "works.work_id": {},
    }


def test_the_receipt_goldens_cover_every_status_and_undo_state():
    statuses = {golden(n)["status"] for n in NAMES if n.startswith("receipt_")}
    assert statuses == {
        "committed", "rejected", "expired", "target_changed", "unknown", "abandoned", "superseded",
    }
    undo = {
        golden(n)["undo"]["state"] for n in NAMES
        if n.startswith("receipt_direct") and golden(n)["undo"]
    }
    assert undo == {"available", "undone", "expired", "none"}


def test_a_committed_receipt_carries_server_now_and_the_current_records():
    body = golden("receipt_committed")
    assert body["committed_at"] and body["server_now"] and body["undo"] is None
    assert [r["entity"] for r in body["records"]] == ["tasks"]
    assert body["records"][0]["record"]["task_id"].startswith("TAS_")


def test_the_unknown_receipt_shape_is_all_null():
    body = golden("receipt_unknown")
    assert body["status"] == "unknown" and body["records"] == []
    assert body["reason_codes"] == ["receipt_not_found"] and body["server_now"]
    for key in ("proposal_id", "kind", "actor_source", "commit_version", "committed_at", "undo",
                "undo_of"):
        assert body[key] is None, key


def test_the_proposal_request_goldens_carry_every_new_status():
    seen = {golden(n)["proposal"]["status"] for n in NAMES
            if n.startswith("proposal_request") and golden(n)["proposal"]}
    assert seen == {"pending", "committed", "abandoned", "superseded"}
    assert golden("proposal_request_unknown")["reason_codes"] == ["proposal_request_unknown"]


def test_a_promotion_proposal_carries_lead_and_client_states():
    body = golden("proposal_promote_lead")
    assert body["entity"] == "promote_lead" and body["input"] == {}
    assert set(body["before"]) == set(body["after"]) == {"lead", "client"}
    assert body["before"]["client"] is None and body["after"]["client"]["person_id"]


# ---- the readers fail closed -------------------------------------------------------------------


def refused(read, body):
    with pytest.raises(contract.ContractError):
        read(body)


@pytest.mark.parametrize("name", NAMES)
def test_one_extra_top_level_key_is_refused(name):
    body = golden(name)
    body["extra"] = 1
    refused(reader(name), body)


@pytest.mark.parametrize("name", NAMES)
def test_every_missing_top_level_key_is_refused(name):
    for key in golden(name):
        body = golden(name)
        del body[key]
        refused(reader(name), body)


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize(
    "changes",
    [
        {"api_version": "assaf-os.v1"},
        {"api_version": "assaf-os.v3"},
        {"source": "google_sheets"},
        {"source": "sqlite"},
        {"workspace_id": ""},
        {"workspace_id": "has space"},
        {"authority_epoch": 0},
        {"authority_epoch": True},
        {"authority_epoch": "1"},
        {"authority_epoch": 1.0},
    ],
)
def test_a_wrong_binding_is_refused(name, changes):
    body = golden(name)
    body.update(changes)
    refused(reader(name), body)


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("key", ["mode", "workbook_id", "pending_proposals", "entity_coverage"])
def test_a_v1_key_on_a_v2_body_is_refused(name, key):
    body = golden(name)
    body[key] = "TEST"
    refused(reader(name), body)


@pytest.mark.parametrize("name", [n for n in NAMES if n not in NO_OBSERVED])
@pytest.mark.parametrize("value", [-1, True, "3", 1.5, None])
def test_a_bad_observed_version_is_refused(name, value):
    body = golden(name)
    body["observed_version"] = value
    refused(reader(name), body)


def test_the_readers_are_not_fooled_by_a_non_object():
    for read in contract.READERS.values():
        for value in (None, [], "x", 1, True):
            refused(read, value)


# schema

def test_a_schema_with_a_changed_version_money_unit_or_too_few_tabs_is_refused():
    for key, value in (("schema_version", "assaf-os-unified.v2"), ("money_unit", "major"),
                       ("tabs", []), ("enums", [])):
        body = golden("schema")
        body[key] = value
        refused(contract.READERS["schema"], body)
    body = golden("schema")
    body["tabs"][1]["fields"][0]["role"] = "owner"
    refused(contract.READERS["schema"], body)
    body = golden("schema")
    body["tabs"][1]["extra"] = 1
    refused(contract.READERS["schema"], body)
    body = golden("schema")
    body["tabs"] = [t for t in body["tabs"] if t["key"] != "summary"]
    refused(contract.READERS["schema"], body)


# snapshot

def test_a_page_refuses_bad_members():
    read = contract.READERS["snapshot"]
    cases = {
        "unchanged": True,
        "coverage": "partial",
        "entity": "promote_lead",
        "since": "1.2",
        "next_cursor": "has space",
        "total": -1,
        "pending_count": "1",
        "labels": {"clients.person_id": {}},
        "mirror": {"state": "fine", "last_verified_at": None, "verified_version": 0,
                   "lag_s": None},
        "summary": {"coverage": "complete"},
        "records": "all",
        "observed_at": "yesterday",
    }
    for key, value in cases.items():
        body = golden("snapshot_tasks_page")
        body[key] = value
        refused(read, body)


def test_a_page_refuses_more_records_than_total_and_a_bad_record():
    read = contract.READERS["snapshot"]
    body = golden("snapshot_tasks_page")
    body["total"] = 1
    refused(read, body)
    body = golden("snapshot_tasks_page")
    body["records"][0]["nested"] = {"a": 1}
    refused(read, body)
    body = golden("snapshot_tasks_page")
    body["records"][0]["bad key!"] = 1
    refused(read, body)
    body = golden("snapshot_tasks_page")
    body["records"][0]["title"] = "x" * 2001
    refused(read, body)


def test_a_monthly_row_must_have_exactly_the_v1_keys():
    read = contract.READERS["snapshot"]
    body = golden("snapshot_tasks_page")
    assert body["summary"]["monthly"], "the golden world has a month"
    body["summary"]["monthly"][0]["extra"] = 1
    refused(read, body)
    body = golden("snapshot_tasks_page")
    body["summary"]["monthly"][0]["paid_income_ils"] = 1.5
    refused(read, body)


def test_a_mirror_view_refuses_bad_members():
    read = contract.READERS["snapshot"]
    for change in ({"state": "synced"}, {"verified_version": -1}, {"lag_s": -1},
                   {"last_verified_at": "soon"}, {"extra": 1}):
        body = golden("snapshot_unchanged")
        body["mirror"].update(change)
        refused(read, body)


def test_the_unchanged_body_refuses_page_keys_and_a_false_flag():
    read = contract.READERS["snapshot"]
    body = golden("snapshot_unchanged")
    body["unchanged"] = False
    refused(read, body)
    body = golden("snapshot_unchanged")
    body["records"] = []
    refused(read, body)


# pending list and proposals

def test_a_pending_list_holds_only_pending_proposal_kind_items_of_one_binding():
    read = contract.READERS["pending"]
    for change in ({"status": "committed"}, {"kind": "owner_direct"}, {"authority_epoch": 2},
                   {"workspace_id": "other"}):
        body = golden("pending_list")
        body["items"][0].update(change)
        refused(read, body)
    body = golden("pending_list")
    body["items"][0]["observed_version"] = 1
    refused(read, body)


def test_proposal_statuses_outside_the_request_recovery_envelope_are_refused():
    for status in ("superseded", "abandoned", "undone", "unknown"):
        body = golden("proposal_create")
        body["status"] = status
        refused(contract.READERS["proposal"], body)
    body = golden("proposal_create")
    body["status"] = "abandoned"
    assert contract.read_proposal_v2(body, statuses=contract.PROPOSAL_REQUEST_STATUSES)


@pytest.mark.parametrize(
    "changes",
    [
        {"actor_source": "ASSISTANT_OTHER"},
        {"actor_source": "WORKER"},
        {"actor_source": None},
        {"kind": "bulk"},
        {"approval_guard_version": 2},
        {"approval_request_id": "not-a-uuid"},
        {"expires_at": "tomorrow"},
        {"entity": "unknown_entity"},
        {"expected_revision": -1},
        {"expected_revision": True},
        {"input": {"nested": {"a": 1}}},
        {"input": []},
        {"after": None},
        {"before": []},
        {"record_id": ""},
    ],
)
def test_a_proposal_member_is_checked(changes):
    body = golden("proposal_update")
    body.update(changes)
    refused(contract.READERS["proposal"], body)


def test_a_proposal_whose_two_decision_ids_are_equal_is_refused():
    body = golden("proposal_update")
    body["rejection_request_id"] = body["approval_request_id"]
    refused(contract.READERS["proposal"], body)


def test_the_promotion_state_must_be_a_lead_client_pair():
    body = golden("proposal_promote_lead")
    body["after"] = {"lead": body["after"]["lead"]}
    refused(contract.READERS["proposal"], body)
    body = golden("proposal_promote_lead")
    body["after"]["client"] = None
    refused(contract.READERS["proposal"], body)
    body = golden("proposal_promote_lead")
    body["before"] = {"lead": None, "client": None}
    refused(contract.READERS["proposal"], body)


# receipts

@pytest.mark.parametrize(
    "changes",
    [
        {"status": "undone"},
        {"status": "undo_expired"},
        {"status": "undo_conflict"},
        {"status": "pending"},
        {"actor_source": "NOBODY"},
        {"kind": "mystery"},
        {"commit_version": 0},
        {"commit_version": "1"},
        {"committed_at": "later"},
        {"server_now": None},
        {"server_now": "now"},
        {"records": [{"entity": "tasks"}]},
        {"records": [{"entity": "nope", "record": {}}]},
        {"records": {"entity": "tasks"}},
        {"reason_codes": ["x"] * 21},
        {"reason_codes": [""]},
        {"undo": {"state": "available", "until": None, "undo_request_id": None}},
        {"undo_of": "not-a-uuid"},
    ],
)
def test_a_receipt_member_is_checked(changes):
    body = golden("receipt_committed")
    body.update(changes)
    refused(contract.READERS["receipt"], body)


def test_committed_at_is_non_null_exactly_when_committed():
    body = golden("receipt_rejected")
    body["committed_at"] = golden("receipt_committed")["committed_at"]
    refused(contract.READERS["receipt"], body)
    body = golden("receipt_committed")
    body["committed_at"] = None
    refused(contract.READERS["receipt"], body)


def test_records_are_non_empty_only_for_committed_and_target_changed():
    for name in ("receipt_rejected", "receipt_expired", "receipt_direct_abandoned"):
        body = golden(name)
        body["records"] = golden("receipt_committed")["records"]
        refused(contract.READERS["receipt"], body)
    assert golden("receipt_target_changed")["records"]


def test_undo_is_present_exactly_for_owner_direct_receipts():
    body = golden("receipt_direct_committed")
    body["undo"] = None
    refused(contract.READERS["receipt"], body)
    body = golden("receipt_committed")
    body["undo"] = {"state": "none", "until": None, "undo_request_id": None}
    refused(contract.READERS["receipt"], body)
    body = golden("receipt_direct_abandoned")
    assert body["undo"] == {"state": "none", "until": None, "undo_request_id": None}


@pytest.mark.parametrize(
    "undo",
    [
        {"state": "available", "until": None, "undo_request_id": None},
        {"state": "undone", "until": "2026-10-16T12:00:30.000Z", "undo_request_id": None},
        {"state": "expired", "until": None, "undo_request_id": None},
        {"state": "none", "until": "2026-10-16T12:00:30.000Z", "undo_request_id": None},
        {"state": "maybe", "until": None, "undo_request_id": None},
        {"state": "available", "until": "2026-10-16T12:00:30.000Z"},
        {"state": "available", "until": "2026-10-16T12:00:30.000Z", "undo_request_id": None,
         "x": 1},
    ],
)
def test_an_undo_view_is_checked(undo):
    body = golden("receipt_direct_committed")
    body["undo"] = undo
    refused(contract.READERS["receipt"], body)


def test_an_unknown_receipt_must_be_all_null_with_the_one_reason():
    for change in ({"proposal_id": "PRO_x"}, {"records": [{"entity": "tasks", "record": {}}]},
                   {"reason_codes": []}, {"reason_codes": ["receipt_not_found", "x"]},
                   {"committed_at": "2026-10-16T12:00:00.000Z"}, {"undo": {"state": "none",
                    "until": None, "undo_request_id": None}}):
        body = golden("receipt_unknown")
        body.update(change)
        refused(contract.READERS["receipt"], body)


def test_undo_of_is_present_exactly_for_an_undo_receipt():
    body = golden("receipt_direct_undone")
    assert body["undo_of"] is None and body["undo"]["undo_request_id"]
    body["undo_of"] = body["request_id"]
    refused(contract.READERS["receipt"], body)


# proposal-request recovery

def test_a_found_request_needs_its_proposal_and_an_empty_reason_list():
    read = contract.READERS["proposal_request"]
    body = golden("proposal_request_pending")
    body["reason_codes"] = ["proposal_request_unknown"]
    refused(read, body)
    body = golden("proposal_request_pending")
    body["proposal"] = None
    refused(read, body)
    body = golden("proposal_request_pending")
    body["status"] = "maybe"
    refused(read, body)


def test_an_unknown_request_is_all_null_with_its_one_reason():
    read = contract.READERS["proposal_request"]
    for change in ({"proposal": golden("proposal_request_pending")["proposal"]},
                   {"receipt": golden("proposal_request_committed")["receipt"]},
                   {"reason_codes": []}, {"reason_codes": ["receipt_not_found"]}):
        body = golden("proposal_request_unknown")
        body.update(change)
        refused(read, body)


def test_the_proposal_must_belong_to_the_requested_id_and_binding():
    read = contract.READERS["proposal_request"]
    body = golden("proposal_request_pending")
    body["request_id"] = "00000000-0000-4000-8000-000000000042"
    refused(read, body)
    body = golden("proposal_request_pending")
    body["proposal"]["authority_epoch"] = 2
    refused(read, body)
    body = golden("proposal_request_committed")
    body["receipt"]["workspace_id"] = "other"
    refused(read, body)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b["receipt"].update(status="rejected"),
        lambda b: b["receipt"].update(proposal_id="PRO_other"),
        lambda b: b["receipt"].update(actor_source="TELEGRAM_APPROVAL"),
        lambda b: b["receipt"].update(request_id=b["proposal"]["rejection_request_id"]),
        lambda b: b["receipt"].update(status="unknown"),
        lambda b: b["proposal"].update(status="rejected"),
    ],
)
def test_the_receipt_cross_check_follows_the_receipt_key_table(mutate):
    body = golden("proposal_request_committed")
    mutate(body)
    refused(contract.READERS["proposal_request"], body)


def test_only_a_pending_proposal_may_have_no_receipt():
    read = contract.READERS["proposal_request"]
    body = golden("proposal_request_committed")
    body["receipt"] = None
    refused(read, body)
    body = golden("proposal_request_pending")
    body["proposal"]["status"] = "expired"
    refused(read, body)


def test_a_pending_proposal_must_not_have_a_receipt():
    body = golden("proposal_request_pending")
    body["receipt"] = golden("proposal_request_committed")["receipt"]
    refused(contract.READERS["proposal_request"], body)


@pytest.mark.parametrize("name,key", [
    ("proposal_request_committed", "approval_request_id"),
    ("proposal_request_abandoned", "approval_request_id"),
    ("proposal_request_superseded", "rejection_request_id"),
])
def test_the_receipt_key_is_the_decision_id_the_status_demands(name, key):
    body = golden(name)
    assert body["receipt"]["request_id"] == body["proposal"][key]
    other = ("rejection_request_id" if key == "approval_request_id" else "approval_request_id")
    body["receipt"]["request_id"] = body["proposal"][other]
    refused(contract.READERS["proposal_request"], body)


# deleted

def test_a_deleted_page_refuses_a_bad_entity_and_cursor():
    read = contract.READERS["deleted"]
    body = golden("deleted_page")
    assert body["records"], "the golden world has a tombstoned task"
    for change in ({"entity": "promote_lead"}, {"next_cursor": 5}, {"records": [1]}):
        broken = copy.deepcopy(body)
        broken.update(change)
        refused(read, broken)


# shape classification (v1, v2, mixed)

def test_body_shape_tells_v1_v2_and_mixed_apart():
    page = golden("snapshot_tasks_page")
    assert contract.body_shape(page) == "v2"
    assert contract.body_shape(golden("receipt_committed")) == "v2"
    v1 = {"api_version": "assaf-os.v1", "source": "google_sheets", "mode": "TEST",
          "workbook_id": "w", "workspace_id": "ws", "records": {}, "entity_coverage": {}}
    assert contract.body_shape(v1) == "v1"
    assert contract.body_shape({"source": "google_sheets", "mode": "TEST"}) == "v1"
    assert contract.body_shape({**page, "mode": "TEST"}) == "mixed"
    assert contract.body_shape({**page, "source": "google_sheets"}) == "mixed"
    assert contract.body_shape({**v1, "source": "postgresql"}) == "mixed"
    assert contract.body_shape({**v1, "authority_epoch": 1}) == "mixed"
    assert contract.body_shape({**v1, "api_version": "assaf-os.v2"}) == "mixed"
    assert contract.body_shape({}) == "unknown"
    assert contract.body_shape([]) == "unknown"
    assert contract.body_shape({"api_version": "assaf-os.v9"}) == "unknown"


# ---- CI ----------------------------------------------------------------------------------------


def test_ci_uploads_the_golden_tree_as_an_artifact_pinned_to_a_commit():
    import re

    import yaml

    workflow = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
    steps = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]["checks"]["steps"]
    [upload] = [
        step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@")
    ]
    assert re.fullmatch(r"actions/upload-artifact@[0-9a-f]{40}", upload["uses"])
    assert upload["with"]["path"] == "tests/contract/golden"
    assert upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["name"].startswith("os-contract-golden-")
    # On a pull request ``github.sha`` is the merge commit; the frontend records the head SHA.
    assert upload["with"]["name"] == (
        "os-contract-golden-${{ github.event.pull_request.head.sha || github.sha }}")
    # It runs after the suite that compares the export with the committed copy.
    suite = [
        i for i, step in enumerate(steps)
        if "pytest --basetemp=.cache/pytest-ci" in step.get("run", "")
    ]
    assert suite and steps.index(upload) > max(suite)


# ---- lengths follow the frontend: JavaScript counts UTF-16 units ---------------------------------

ASTRAL = chr(0x1F600)  # one code point, two UTF-16 units


def test_a_record_text_is_capped_at_2000_utf16_units_like_the_frontend():
    read = contract.READERS["snapshot"]
    body = golden("snapshot_tasks_page")
    body["records"][0]["title"] = ASTRAL * 1000
    assert read(body)
    body["records"][0]["title"] = ASTRAL * 1001
    refused(read, body)


def test_a_label_is_cut_to_120_utf16_units_so_the_frontend_reader_accepts_it():
    labels = contract.labels_view({"clients.person_id": {"PER_x": ASTRAL * 100 + "tail"}})
    cut = labels["clients.person_id"]["PER_x"]
    assert cut == ASTRAL * 60 and len(cut.encode("utf-16-le")) // 2 == 120
    body = golden("snapshot_tasks_page")
    body["labels"] = labels
    assert contract.READERS["snapshot"](body)
    body["labels"]["clients.person_id"]["PER_x"] = ASTRAL * 61
    refused(contract.READERS["snapshot"], body)


def test_a_label_of_exactly_the_cap_is_kept_and_a_surrogate_is_never_split():
    keep = contract.labels_view({"works.work_id": {"W_1": "x" * 120, "W_2": "x" * 119 + ASTRAL}})
    assert keep["works.work_id"]["W_1"] == "x" * 120
    assert keep["works.work_id"]["W_2"] == "x" * 119


# ---- review round 1 -----------------------------------------------------------------------------
# Each test below failed on the first V2-20 head (5e84fa3) and pins one reviewer finding.


def test_a_committed_batch_with_no_expense_op_has_a_receipt_without_records():
    """Section 10.2 caps ``records`` (non-empty only for committed and target_changed); it does not
    demand them. A batch of personal-only lines commits with no record mutation (section 12.2)."""
    body = golden("receipt_batch_committed")
    assert body["status"] == "committed" and body["kind"] == "statement_batch"
    assert body["records"] == [] and body["undo"] is None and body["committed_at"]
    assert contract.READERS["receipt"](body) == body


def test_the_recovery_envelope_accepts_a_committed_batch_whose_receipt_lists_no_record():
    body = golden("proposal_request_batch_committed")
    assert body["proposal"]["kind"] == "statement_batch"
    assert body["proposal"]["status"] == body["receipt"]["status"] == "committed"
    assert body["receipt"]["records"] == []
    assert contract.READERS["proposal_request"](body) == body


@pytest.mark.parametrize("kind", ["statement_batch", "import"])
def test_the_kinds_that_may_commit_without_a_record_accept_both_shapes(kind):
    body = golden("receipt_committed")
    body["kind"] = kind
    assert contract.READERS["receipt"](body)
    body["records"] = []
    assert contract.READERS["receipt"](body)


@pytest.mark.parametrize("kind", ["proposal", "owner_direct", "owner_undo"])
def test_a_committed_receipt_of_a_record_kind_still_lists_its_records(kind):
    body = golden("receipt_committed")
    body["kind"] = kind
    if kind == "owner_direct":
        body["undo"] = {"state": "none", "until": None, "undo_request_id": None}
    if kind == "owner_undo":
        body["undo_of"] = "00000000-0000-4000-8000-000000000077"
    assert contract.READERS["receipt"](body)
    body["records"] = []
    refused(contract.READERS["receipt"], body)


def test_a_lone_proposal_is_pending_and_of_kind_proposal_like_the_frontend_reader():
    """V2-21 ``readProposalV2`` accepts statuses ['pending'] and kinds ['proposal'] only."""
    read = contract.READERS["proposal"]
    assert read(golden("proposal_update"))
    for status in ("committed", "rejected", "expired", "target_changed"):
        body = golden("proposal_update")
        body["status"] = status
        refused(read, body)
    for kind in ("owner_direct", "owner_undo", "statement_batch", "import"):
        body = golden("proposal_update")
        body["kind"] = kind
        refused(read, body)


def test_the_status_and_kind_sets_are_arguments_of_the_reader_not_defaults():
    body = golden("proposal_update")
    body["status"], body["kind"] = "committed", "statement_batch"
    refused(contract.read_proposal_v2, body)
    refused(lambda v: contract.read_proposal_v2(v, statuses=contract.PROPOSAL_STATUSES), body)
    assert contract.read_proposal_v2(
        body, statuses=contract.PROPOSAL_STATUSES, kinds=contract.PROPOSAL_KINDS)


def test_a_decided_proposal_is_still_read_inside_the_recovery_envelope():
    for name in ("proposal_request_committed", "proposal_request_abandoned",
                 "proposal_request_superseded", "proposal_request_batch_committed"):
        body = golden(name)
        assert body["proposal"]["status"] != "pending"
        assert contract.READERS["proposal_request"](body)


def test_a_boolean_is_not_the_approval_guard_version_anywhere_it_is_read():
    """In Python ``True == 1``; JavaScript's ``=== 1`` is false for ``true``."""
    body = golden("proposal_update")
    body["approval_guard_version"] = True
    refused(contract.READERS["proposal"], body)
    body = golden("pending_list")
    body["items"][0]["approval_guard_version"] = True
    refused(contract.READERS["pending"], body)
    body = golden("proposal_request_pending")
    body["proposal"]["approval_guard_version"] = True
    refused(contract.READERS["proposal_request"], body)
    body = golden("proposal_request_committed")
    body["proposal"]["approval_guard_version"] = True
    refused(contract.READERS["proposal_request"], body)


# ---- a hostile value of the wrong type is a contract error, never a crash ------------------------


def node_paths(value, prefix=()):
    """Every member path below ``value`` (lists by index, objects by key), parents first."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield (*prefix, key)
            yield from node_paths(item, (*prefix, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield (*prefix, index)
            yield from node_paths(item, (*prefix, index))


def replaced(body, path, junk):
    broken = copy.deepcopy(body)
    holder = broken
    for step in path[:-1]:
        holder = holder[step]
    holder[path[-1]] = junk
    return broken


@pytest.mark.parametrize("name", NAMES)
def test_an_unhashable_value_in_any_member_is_a_contract_error_never_a_type_error(name):
    base, read = golden(name), reader(name)
    for path in node_paths(base):
        for junk in ([], {}):
            with contextlib.suppress(contract.ContractError):
                read(replaced(base, path, junk))


@pytest.mark.parametrize("junk", [[], {}, [[]], [{}]])
def test_the_request_status_and_the_schema_enums_refuse_unhashable_values(junk):
    body = golden("proposal_request_unknown")
    body["status"] = junk
    refused(contract.READERS["proposal_request"], body)
    schema = golden("schema")
    schema["tabs"][1]["fields"][0]["type"] = junk
    refused(contract.READERS["schema"], schema)
    schema = golden("schema")
    schema["tabs"][1]["fields"][0]["role"] = junk
    refused(contract.READERS["schema"], schema)
    schema = golden("schema")
    schema["tabs"][1]["kind"] = junk
    refused(contract.READERS["schema"], schema)


# ---- the mirror agrees with the token that rides beside it ---------------------------------------


@pytest.mark.parametrize(
    "name", ["snapshot_tasks_page", "snapshot_tasks_second_page", "snapshot_expenses_page",
             "snapshot_unchanged"])
def test_a_golden_mirror_is_the_version_the_since_token_names(name):
    body = golden(name)
    version, _pending_seq, mirror_part = body["since"].split(".")
    assert int(mirror_part) == body["mirror"]["verified_version"] == int(version)
    assert body["mirror"]["state"] == "verified"
