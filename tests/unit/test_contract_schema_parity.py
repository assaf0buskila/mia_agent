"""V2-20: the v2 schema body is the v1 schema, generated, and kept equal to its sources.

``schema_v2.json`` is generated from ``ops/assaf-os/schema.js`` (the Apps Script schema the v1
``GET /schema`` serves), so the tab, field and enum rules are identical to v1 (section 10.2). These
tests pin it to schema.js (run by node), to the frozen ``assaf_os_local_schema.json`` input rules,
and to the ``INPUT_FIELDS`` the proposal validators use. A drift in any of them fails here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import get_args, get_type_hints

import pytest
from app.prototypes.assaf_os_domain import ENUMS, SCHEMA
from app.services.assaf_os import INPUT_FIELDS, READ_FIELDS
from app.services.os_ledger import api, contract

ROOT = Path(__file__).resolve().parents[2]


def schema_js() -> dict:
    node = shutil.which("node")
    assert node, "node is required: the Apps Script schema is the source of the v2 schema"
    script = (
        "const s = require('./ops/assaf-os/schema.js');"
        "process.stdout.write(JSON.stringify({version: s.version, enums: s.enums, tabs: s.tabs}));"
    )
    done = subprocess.run(
        [node, "-e", script], cwd=ROOT, capture_output=True, check=True, timeout=60
    )
    return json.loads(done.stdout.decode("utf-8"))


def test_the_schema_file_equals_what_schema_js_defines():
    source = schema_js()
    body = contract.schema_body("workspace", 1)
    assert body["tabs"] == source["tabs"] and body["enums"] == source["enums"]
    assert body["schema_version"] == source["version"] == contract.SCHEMA_VERSION


def test_the_schema_body_has_the_v1_tab_order_and_a_summary_tab():
    keys = [tab["key"] for tab in contract.schema_body("workspace", 1)["tabs"]]
    assert keys == ["summary", "leads", "clients", "works", "payments", "time", "expenses",
                    "tasks", "settings"]


def test_the_enums_equal_the_frozen_local_schema():
    assert contract.schema_enums() == {key: list(values) for key, values in ENUMS.items()}


@pytest.mark.parametrize("entity", api.ENTITIES)
def test_input_fields_equal_the_local_schema_and_the_proposal_validator(entity):
    local = {field["key"] for field in SCHEMA["entities"][entity]}
    assert contract.input_fields()[entity] == local == INPUT_FIELDS[entity]


@pytest.mark.parametrize("entity", api.ENTITIES)
def test_every_wire_field_of_a_record_is_a_schema_field(entity):
    tabs = {tab["key"]: tab for tab in contract.schema_tabs()}
    declared = {field["key"] for field in tabs[entity]["fields"]}
    assert READ_FIELDS[entity] <= declared
    assert api.ID_KEYS[entity] in declared and "revision" in declared


def test_free_text_and_reference_fields_come_from_the_schema():
    assert contract.text_fields() == {
        "leads": {"name", "phone", "email", "source_note"},
        "clients": {"name", "phone", "email", "notes"},
        "works": {"title"},
        "payments": {"notes"},
        "time": {"description"},
        "expenses": {"category", "vendor", "source_key", "notes"},
        "tasks": {"title", "notes"},
    }
    assert contract.reference_fields() == {
        "leads": (),
        "clients": (),
        "works": (("person_id", "clients.person_id"),),
        "payments": (("person_id", "clients.person_id"), ("work_id", "works.work_id")),
        "time": (("person_id", "clients.person_id"), ("work_id", "works.work_id")),
        "expenses": (),
        "tasks": (("person_id", "clients.person_id"), ("work_id", "works.work_id")),
    }


def test_the_schema_is_served_from_a_packaged_resource():
    resource = ROOT / "app" / "services" / "os_ledger" / "schema_v2.json"
    assert resource.is_file() and b"\r" not in resource.read_bytes()
    assert json.loads(resource.read_text(encoding="utf-8"))["source"] == "ops/assaf-os/schema.js"


def test_the_dto_constants_match_the_ledger_api():
    assert contract.ENTITIES == api.ENTITIES
    assert set(contract.ACTOR_SOURCES) == set(get_args(api.Source))
    assert set(contract.PROPOSAL_KINDS) == set(get_args(api.ProposalKind))
    assert set(contract.PROPOSAL_REQUEST_STATUSES) == set(get_args(api.ProposalStatus))
    assert set(contract.RECEIPT_STATUSES) == set(get_args(api.ReceiptStatus))
    assert set(contract.UNDO_STATES) == set(get_args(get_type_hints(api.UndoView)["state"]))
