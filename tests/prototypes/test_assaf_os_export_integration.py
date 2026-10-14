"""Exercise the real fixture reader boundary before dry provenance mapping."""

import json
import shutil
import socket
import subprocess
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

import app.prototypes.assaf_os_export_reader as reader_module
import httpx
import pytest
import sqlalchemy
from app.prototypes.assaf_os_export_provenance import map_export
from app.prototypes.assaf_os_export_reader import (
    ExportReaderError,
    LocalExportReader,
    ReaderBinding,
    SyntheticGetTransport,
)
from app.prototypes.assaf_os_ledger import LocalLedgerPrimary
from app.prototypes.assaf_os_shadow_import import LocalShadowImporter
from tests.prototypes.test_assaf_os_export_provenance import NOW, make_candidate


@pytest.fixture(params=[False, True], ids=["normalized", "physical"])
def isolated_export(monkeypatch, request):
    def forbidden(*args, **kwargs):
        pytest.fail("export preparation attempted provider or database side effects")

    class FixtureClock:
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(NOW)

    monkeypatch.setattr(reader_module, "datetime", FixtureClock)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(httpx.Client, "request", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    monkeypatch.setattr(sqlalchemy, "create_engine", forbidden)
    for kind, names in (
        (LocalLedgerPrimary, ("propose", "decide", "snapshot")),
        (LocalShadowImporter, ("stage", "reconcile", "read_shadow")),
    ):
        for name in names:
            monkeypatch.setattr(kind, name, forbidden)

    draft, _, source_binding, records, ranges = make_candidate(physical=request.param)
    binding = ReaderBinding(**asdict(source_binding))
    responses = {
        source_range: [
            deepcopy(pair["unformatted"]),
            deepcopy(pair["formulas"]),
            deepcopy(pair["unformatted"]),
            deepcopy(pair["formulas"]),
        ]
        for source_range, pair in draft["records"].items()
    }
    responses[binding.guard_range] = [deepcopy(draft["evidence"]["guard_before"]) for _ in range(4)]
    return binding, responses, records, ranges


def extract_and_map(binding, responses):
    transport = SyntheticGetTransport(binding.workbook_id, responses)
    extract = LocalExportReader(binding, transport, enabled=True).extract()
    manifest = map_export(extract, extract["evidence"], expected_binding=binding)
    return extract, manifest, transport


def test_seven_families_flow_through_get_reader_without_external_side_effects(isolated_export):
    binding, responses, records, _ = isolated_export
    before = deepcopy(responses)
    extract, manifest, transport = extract_and_map(binding, responses)

    assert extract["status"] == "candidate"
    assert manifest["status"] == "validated_candidate"
    assert manifest["counts"] == {entity: len(rows) for entity, rows in records.items()}
    assert manifest["money_by_currency"]["ILS"]["net_payments_minor"] == 10_000
    assert manifest["money_by_currency"]["ILS"]["net_cash_minor"] == 8_500
    assert responses == before
    assert transport.readonly_calls
    assert {call.method for call in transport.readonly_calls} == {"GET"}
    assert manifest["synthetic_only"] is True
    assert manifest["cutover_proof"] is False
    assert manifest["source_authorization_proof"] is False
    assert manifest["writes_performed"] == 0
    assert manifest["importer_invoked"] is False
    assert manifest["business_ledger_invoked"] is False


@pytest.mark.parametrize("render_offset", [2, 3])
def test_source_drift_cannot_become_validated_mapping(isolated_export, render_offset):
    binding, responses, _, ranges = isolated_export
    responses[ranges["tasks"]][render_offset]["values"][1][1] = "changed fixture"
    extract, manifest, _ = extract_and_map(binding, responses)

    assert extract["status"] == "inconclusive"
    assert "data_drift" in extract["issue_codes"]
    assert extract["records"] == {}
    assert manifest["status"] == "inconclusive"


def test_guard_aba_change_cannot_become_validated_mapping(isolated_export):
    binding, responses, _, _ = isolated_export
    responses[binding.guard_range][1]["values"][1][4] += 1
    extract, manifest, _ = extract_and_map(binding, responses)

    assert extract["status"] == "inconclusive"
    assert "guard_aba_detected" in extract["issue_codes"]
    assert manifest["status"] == "inconclusive"


def test_lost_read_cannot_become_validated_mapping(isolated_export):
    binding, responses, _, ranges = isolated_export
    responses[ranges["expenses"]].pop()
    extract, manifest, _ = extract_and_map(binding, responses)

    assert extract["status"] == "inconclusive"
    assert "synthetic_response_missing" in extract["issue_codes"]
    assert manifest["status"] == "inconclusive"


def test_mapper_rejects_other_authority_binding_after_matching_reads(isolated_export):
    binding, responses, _, _ = isolated_export
    extract, _, _ = extract_and_map(binding, responses)
    changed = replace(binding, authority_epoch=binding.authority_epoch + 1)
    manifest = map_export(extract, extract["evidence"], expected_binding=changed)

    assert manifest["status"] == "inconclusive"
    assert "source_binding_changed" in manifest["issue_codes"]


def test_disabled_reader_has_no_fixture_reads(isolated_export):
    binding, responses, _, _ = isolated_export
    transport = SyntheticGetTransport(binding.workbook_id, responses)
    with pytest.raises(ExportReaderError) as caught:
        LocalExportReader(binding, transport).extract()
    assert caught.value.code == "export_reader_disabled"
    assert transport.readonly_calls == ()


def test_frozen_physical_descriptor_matches_local_native_schema():
    node = shutil.which("node")
    if node is None:
        pytest.skip("local Node runtime required for schema parity oracle")
    root = Path(__file__).resolve().parents[2]
    script = (
        "const s=require('./ops/assaf-os/schema.js');"
        "const e=['leads','clients','works','payments','time','expenses','tasks'];"
        "console.log(JSON.stringify({version:s.version,source:'ops/assaf-os/schema.js',"
        "entities:Object.fromEntries(s.tabs.filter(t=>e.includes(t.key))"
        ".map(t=>[t.key,{name:t.name,fields:t.fields}]))}));"
    )
    result = subprocess.run(
        [node, "-e", script], cwd=root, capture_output=True, check=True, text=True,
        encoding="utf-8", timeout=10,
    )
    frozen = json.loads(
        (root / "app/prototypes/assaf_os_export_schema.json").read_text(encoding="utf-8")
    )
    assert frozen == json.loads(result.stdout)
