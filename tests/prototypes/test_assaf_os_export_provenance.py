"""Fixture-only tests for the pure export provenance mapper."""

from __future__ import annotations

import json
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from app.prototypes.assaf_os_domain import ENTITIES, ID_KEYS, SCHEMA
from app.prototypes.assaf_os_export_provenance import (
    ACCEPTED_HEADERS,
    COVERAGE_HEADERS,
    ENTITY_HEADERS,
    ENTITY_ROLES,
    HEADER_HISTORY_HEADERS,
    NATIVE_ENTITY_HEADERS,
    NATIVE_ENTITY_ROLES,
    NATIVE_FIELDS,
    NORMALISED_SOURCE_FORMAT,
    PHYSICAL_SOURCE_FORMAT,
    PROPOSAL_HEADERS,
    RECEIPT_HEADERS,
    SOURCE_METADATA_HEADERS,
    SourceBinding,
    canonical_digest,
    map_export,
    native_raw_owned_digest,
    record_input_digest,
)

ROOT = Path(__file__).resolve().parents[2]
ORACLE = Path(__file__).with_name("assaf_os_summary_oracle.cjs")
NATIVE_ORACLE = Path(__file__).with_name("assaf_os_native_export_oracle.cjs")
NOW = "2026-10-14T12:34:56+00:00"


def _row(entity: str, record_id: str, **values):
    return {ID_KEYS[entity]: record_id, "revision": 1, **values}


def fixture_records():
    enum = SCHEMA["enums"]
    path = enum["path"][0]
    client = "PER_1"
    work = "WOR_1"
    return {
        "leads": [
            _row(
                "leads",
                "LEA_1",
                person_id="PER_LEAD_1",
                name="Invented lead",
                phone="0501234567",
                email="lead@example.test",
                path=path,
                status=enum["lead_status"][0],
                source_note=None,
            )
        ],
        "clients": [
            _row(
                "clients",
                client,
                name="Invented client",
                phone=None,
                email="client@example.test",
                status=enum["client_status"][0],
                notes=None,
            )
        ],
        "works": [
            _row(
                "works",
                work,
                person_id=client,
                title="Invented work",
                path=path,
                start_date="2026-10-01",
                status=enum["work_status"][0],
                agreed_amount_ils=25_000,
                currency="ILS",
            )
        ],
        "payments": [
            _row(
                "payments",
                "PAY_1",
                person_id=client,
                work_id=work,
                path=path,
                date="2026-10-02",
                actual_amount_ils=12_345,
                currency="ILS",
                kind=enum["payment_kind"][0],
                original_payment_id=None,
                notes=None,
            ),
            _row(
                "payments",
                "PAY_REF_1",
                person_id=client,
                work_id=work,
                path=path,
                date="2026-10-03",
                actual_amount_ils=2_345,
                currency="ILS",
                kind=enum["payment_kind"][1],
                original_payment_id="PAY_1",
                notes=None,
            ),
        ],
        "time": [
            _row(
                "time",
                "TIM_1",
                person_id=client,
                work_id=work,
                path=path,
                date="2026-10-02",
                minutes=90,
                description="Invented session",
                status=enum["time_status"][0],
            )
        ],
        "expenses": [
            _row(
                "expenses",
                "EXP_1",
                date="2026-10-02",
                category="Invented ads",
                platform="Google Ads",
                path=path,
                vendor="Example vendor",
                amount_ils=1_500,
                currency="ILS",
                payment_status=enum["payment_status"][0],
                source_type=enum["expense_source"][0],
                source_key="invented-source-1",
                notes=None,
            )
        ],
        "tasks": [
            _row(
                "tasks",
                "TAS_1",
                title="Invented follow-up",
                person_id=client,
                work_id=work,
                path=path,
                status=enum["task_status"][0],
                due_date="2026-10-20",
                notes=None,
            )
        ],
    }


def _matrix_row(entity, record):
    return ["" if record.get(key) is None else record.get(key) for key in ENTITY_HEADERS[entity]]


def _column_name(number):
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def _native_matrix_row(entity, record):
    result = []
    identity = record[ID_KEYS[entity]]
    for field in NATIVE_FIELDS[entity]:
        key = field["key"]
        value = record.get(key)
        if field["role"] == "calculated":
            value = ""
        elif value is None and key in {"created_at", "updated_at"}:
            value = NOW
        elif value is None and key == "write_source":
            value = "OS"
        elif value is None and key == "request_id":
            value = "REQ_" + identity
        elif field["type"] == "money_ils" and value is not None:
            value = value // 100 if value % 100 == 0 else value / 100
        result.append("" if value is None else value)
    return result


def _envelope(source_range, values):
    return {"range": source_range, "majorDimension": "ROWS", "values": values}


def _pair(source_range, values):
    raw = _envelope(source_range, deepcopy(values))
    formulas = _envelope(source_range, deepcopy(values))
    return {"unformatted": raw, "formulas": formulas}


def make_candidate(records=None, *, coverage_value="complete", physical=False, range_starts=None):
    records = deepcopy(records or fixture_records())
    range_starts = range_starts or {}
    if physical:
        ranges = {}
        for entity in ENTITIES:
            start = range_starts.get(entity, 1)
            end = start + 19
            sheet = "'" + entity.replace("'", "''") + " physical fixture'"
            ranges[entity] = f"{sheet}!A{start}:{_column_name(len(NATIVE_FIELDS[entity]))}{end}"
    else:
        ranges = {
            entity: (
                f"Fixture_{entity}!A{range_starts.get(entity, 1)}:AZ"
                f"{range_starts.get(entity, 1) + 19}"
            )
            for entity in ENTITIES
        }
    accepted_range = "'_assaf_os_accepted'!A1:I20"
    proposal_range = "'_assaf_os_proposals'!A1:M20"
    receipt_range = "'_assaf_os_receipts'!A1:F20"
    source_range = "Fixture_Source_Evidence!A1:N20"
    coverage_range = "Fixture_Coverage!A1:F20"
    header_range = "Fixture_Header_History!A1:E20"
    allowed = tuple(
        [ranges[entity] for entity in ENTITIES]
        + [
            accepted_range,
            proposal_range,
            receipt_range,
            source_range,
            coverage_range,
            header_range,
        ]
    )
    binding = SourceBinding(
        workbook_id="fixture-workbook-not-real",
        workspace_id="fixture-workspace",
        authority_epoch=7,
        source_epoch=11,
        schema_version=SCHEMA["version"],
        allowed_ranges=allowed,
        guard_range="Fixture_Guard!A1:F2",
    )
    reads = {}
    accepted = [list(ACCEPTED_HEADERS)]
    reference_ids = {
        entity: {row.get(ID_KEYS[entity]) for row in records[entity] if row.get(ID_KEYS[entity])}
        for entity in ENTITIES
    }
    for entity in ENTITIES:
        values = [list(NATIVE_ENTITY_HEADERS[entity] if physical else ENTITY_HEADERS[entity])]
        for record in records[entity]:
            matrix_row = (
                _native_matrix_row(entity, record) if physical else _matrix_row(entity, record)
            )
            values.append(matrix_row)
            inputs = {
                field["key"]: record.get(field["key"]) for field in SCHEMA["entities"][entity]
            }
            identity = record.get(ID_KEYS[entity], "")
            snapshot = {
                "entity": entity,
                "record_id": identity,
                "revision": record.get("revision"),
                "input": inputs,
            }
            accepted.append(
                [
                    entity,
                    identity,
                    record.get("revision"),
                    record_input_digest(entity, record),
                    (
                        native_raw_owned_digest(entity, matrix_row, reference_ids)
                        if physical
                        else canonical_digest(matrix_row)
                    ),
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    NOW,
                    "OS",
                    "REQ_" + str(identity),
                ]
            )
        reads[ranges[entity]] = _pair(ranges[entity], values)
    reads[accepted_range] = _pair(accepted_range, accepted)
    reads[proposal_range] = _pair(
        proposal_range,
        [
            list(PROPOSAL_HEADERS),
            [
                "PRO_1",
                "REQ_PAY_1",
                "12345",
                "OS",
                "committed",
                "payments",
                "PAY_1",
                0,
                "f" * 64,
                "{}",
                NOW,
                "2026-10-15T12:34:56+00:00",
                NOW,
            ],
        ],
    )
    reads[receipt_range] = _pair(
        receipt_range,
        [
            list(RECEIPT_HEADERS),
            ["REQ_PAY_1", "12345", "PRO_1", "committed", "{}", NOW],
        ],
    )
    coverage = [list(COVERAGE_HEADERS)] + [
        [entity] + [coverage_value] * (len(COVERAGE_HEADERS) - 1) for entity in ENTITIES
    ]
    reads[coverage_range] = _pair(coverage_range, coverage)
    headers = [list(HEADER_HISTORY_HEADERS)] + [
        [
            entity,
            SCHEMA["version"],
            json.dumps(
                list(NATIVE_ENTITY_HEADERS[entity] if physical else ENTITY_HEADERS[entity]),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            json.dumps(
                list(NATIVE_ENTITY_ROLES[entity] if physical else ENTITY_ROLES[entity]),
                separators=(",", ":"),
            ),
            NOW,
        ]
        for entity in ENTITIES
    ]
    reads[header_range] = _pair(header_range, headers)
    source_rows = [list(SOURCE_METADATA_HEADERS)]
    for entity in ENTITIES:
        entity_range = ranges[entity]
        source_rows.append(
            [
                entity_range,
                entity,
                "fixture:invented-real-format",
                binding.workbook_id,
                binding.workspace_id,
                binding.authority_epoch,
                binding.source_epoch,
                binding.schema_version,
                "native_major_decimal" if physical else "canonical_minor",
                "apps_script_raw_owned_sha256" if physical else "normalized_row_sha256",
                canonical_digest(reads[entity_range]["unformatted"]),
                canonical_digest(reads[entity_range]["formulas"]),
                NOW,
                PHYSICAL_SOURCE_FORMAT if physical else NORMALISED_SOURCE_FORMAT,
            ]
        )
    reads[source_range] = _pair(source_range, source_rows)
    # Preserve binding order, independent of the order ranges were assembled above.
    reads = {source_range_: reads[source_range_] for source_range_ in allowed}
    guard = _envelope(
        binding.guard_range,
        [
            [
                "workspace_id",
                "authority_epoch",
                "source_epoch",
                "schema_version",
                "revision",
                "export_token",
            ],
            [
                binding.workspace_id,
                binding.authority_epoch,
                binding.source_epoch,
                binding.schema_version,
                3,
                "invented-freeze-token",
            ],
        ],
    )
    read_digest = canonical_digest(reads)
    passes = [
        {
            "pass_number": number,
            "read_digest": read_digest,
            "guard_before": deepcopy(guard),
            "guard_after": deepcopy(guard),
        }
        for number in (1, 2)
    ]
    response_bytes = sum(
        len(
            json.dumps(item[key], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        for item in passes
        for key in ("guard_before", "guard_after")
    ) + 2 * sum(
        len(
            json.dumps(
                pair[render], ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        )
        for pair in reads.values()
        for render in ("unformatted", "formulas")
    )
    evidence = {
        "reads": deepcopy(reads),
        "read_digest": read_digest,
        "guard_before": deepcopy(guard),
        "guard_after": deepcopy(guard),
        "passes": passes,
        "issues": [],
        "request_count": 2 * (2 + 2 * len(allowed)),
        "response_bytes": response_bytes,
        "attempted_response_bytes": response_bytes,
        "response_byte_budget_exhausted": False,
        "consistency_note": "matching_synthetic_reads_require_approved_source_freeze",
    }
    binding_dict = {
        **binding.__dict__,
        "allowed_ranges": list(binding.allowed_ranges),
    }
    extract = {
        "status": "candidate",
        "records": deepcopy(reads),
        "evidence": deepcopy(evidence),
        "issue_codes": [],
        "source_binding": binding_dict,
        "observed_at": NOW,
        "synthetic_only": True,
        "preparation_only": True,
    }
    return extract, deepcopy(evidence), binding, records, ranges


def _refresh_reader_evidence(extract):
    evidence = extract["evidence"]
    evidence["reads"] = deepcopy(extract["records"])
    evidence["read_digest"] = canonical_digest(extract["records"])
    for item in evidence["passes"]:
        item["read_digest"] = evidence["read_digest"]
    response_bytes = sum(
        len(
            json.dumps(item[key], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        for item in evidence["passes"]
        for key in ("guard_before", "guard_after")
    ) + 2 * sum(
        len(
            json.dumps(
                pair[render], ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        )
        for pair in extract["records"].values()
        for render in ("unformatted", "formulas")
    )
    evidence["response_bytes"] = response_bytes
    evidence["attempted_response_bytes"] = response_bytes
    extract["evidence"] = deepcopy(evidence)
    return deepcopy(evidence)


def test_valid_seven_entity_candidate_preserves_native_acceptance_and_financial_parity():
    extract, evidence, binding, records, ranges = make_candidate()
    manifest = map_export(extract, evidence, expected_binding=binding)

    assert manifest["status"] == "validated_candidate"
    assert manifest["counts"] == {entity: len(records[entity]) for entity in ENTITIES}
    assert manifest["ids"]["payments"] == ["PAY_1", "PAY_REF_1"]
    assert manifest["revisions"]["payments"] == {"PAY_1": 1, "PAY_REF_1": 1}
    assert manifest["money_by_currency"]["ILS"] == {
        "received_minor": 12_345,
        "refund_minor": 2_345,
        "net_payments_minor": 10_000,
        "paid_expenses_minor": 1_500,
        "net_cash_minor": 8_500,
    }
    assert manifest["refunds"] == [
        {
            "payment_id": "PAY_REF_1",
            "original_payment_id": "PAY_1",
            "amount_minor": 2_345,
            "currency": "ILS",
        }
    ]
    payment_provenance = manifest["provenance"]["payments"]["PAY_1"]
    assert payment_provenance["source_range"] == ranges["payments"]
    assert payment_provenance["source_native_acceptance"][
        "input_fingerprint"
    ] == record_input_digest("payments", records["payments"][0])
    assert payment_provenance["native_acceptance_is_human_approval"] is False
    assert manifest["native_acceptance_is_human_approval"] is False
    assert manifest["source_authorization_proof"] is False
    assert manifest["cutover_proof"] is False
    assert manifest["writes_performed"] == 0
    assert manifest["physical_schema_compatibility"].endswith("requires_live_header_inspection")

    assert shutil.which("node"), "Node is required for the independent Apps Script oracle"
    completed = subprocess.run(
        ["node", str(ORACLE)],
        cwd=ROOT,
        input=json.dumps(
            {
                "records": records,
                "coverage": dict.fromkeys(ENTITIES, "complete"),
                "aggregate_coverage": "complete",
            },
            ensure_ascii=False,
        ),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    oracle = json.loads(completed.stdout)
    assert {
        "coverage": manifest["summaries"]["coverage"],
        "monthly": manifest["summaries"]["monthly"],
    } == oracle


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda records: records["tasks"].append(deepcopy(records["tasks"][0])),
            "duplicate_record_id",
        ),
        (
            lambda records: records["tasks"][0].pop("task_id"),
            "missing_or_invalid_record_id",
        ),
        (
            lambda records: records["payments"][1].update(actual_amount_ils=99_999),
            "state_validation_failed",
        ),
        (
            lambda records: records["payments"][0].update(date="2026-02-30"),
            "record_invalid",
        ),
        (
            lambda records: records["tasks"][0].update(work_id="WOR_MISSING"),
            "unresolved_reference",
        ),
    ],
)
def test_invalid_identity_refund_date_and_reference_are_quarantined(mutate, code):
    records = fixture_records()
    mutate(records)
    extract, evidence, binding, *_ = make_candidate(records)
    manifest = map_export(extract, evidence, expected_binding=binding)
    assert manifest["status"] == "quarantined"
    assert code in manifest["issue_codes"]


def test_duplicate_contacts_and_promoted_lead_reference_are_quarantined():
    records = fixture_records()
    duplicate = deepcopy(records["leads"][0])
    duplicate["lead_id"] = "LEA_2"
    duplicate["person_id"] = "PER_LEAD_2"
    records["leads"].append(duplicate)
    records["leads"][0]["status"] = SCHEMA["enums"]["lead_status"][-1]
    extract, evidence, binding, *_ = make_candidate(records)
    manifest = map_export(extract, evidence, expected_binding=binding)
    assert manifest["status"] == "quarantined"
    assert {"duplicate_lead_contact", "unresolved_reference"} <= set(manifest["issue_codes"])


def test_missing_and_changed_native_acceptance_quarantine_without_inventing_approval():
    extract, _evidence, binding, *_ = make_candidate()
    accepted_range = next(key for key in extract["records"] if "_assaf_os_accepted" in key)
    for mode in ("missing", "changed"):
        changed = deepcopy(extract)
        rows = changed["records"][accepted_range]["unformatted"]["values"]
        if mode == "missing":
            rows[:] = [rows[0]] + [row for row in rows[1:] if row[1] != "PAY_1"]
        else:
            target = next(row for row in rows[1:] if row[1] == "PAY_1")
            target[3] = "0" * 64
        changed["records"][accepted_range]["formulas"] = deepcopy(
            changed["records"][accepted_range]["unformatted"]
        )
        evidence = _refresh_reader_evidence(changed)
        manifest = map_export(changed, evidence, expected_binding=binding)
        assert manifest["status"] == "quarantined"
        assert (
            "unaccepted_record" if mode == "missing" else "accepted_snapshot_mismatch"
        ) in manifest["issue_codes"]
        assert manifest["native_acceptance_is_human_approval"] is False


def test_aba_guard_and_unrelated_evidence_injection_are_inconclusive():
    extract, evidence, binding, *_ = make_candidate()
    aba = deepcopy(extract)
    aba["evidence"]["guard_after"]["values"][1][-1] = "token-returned-after-aba"
    aba_evidence = deepcopy(aba["evidence"])
    manifest = map_export(aba, aba_evidence, expected_binding=binding)
    assert manifest["status"] == "inconclusive"
    assert "reader_guard_changed" in manifest["issue_codes"]

    unrelated, unrelated_evidence, _other_binding, *_ = make_candidate()
    unrelated_evidence["consistency_note"] = "valid-looking evidence from another call"
    manifest = map_export(extract, unrelated_evidence, expected_binding=binding)
    assert manifest["status"] == "inconclusive"
    assert "evidence_not_reader_bound" in manifest["issue_codes"]
    assert unrelated["status"] == "candidate"


def test_source_hash_formula_and_coverage_changes_block_validation():
    extract, _evidence, binding, _records, ranges = make_candidate()
    source_range = next(key for key in extract["records"] if key.startswith("Fixture_Source"))
    source_values = extract["records"][source_range]["unformatted"]["values"]
    row = next(row for row in source_values[1:] if row[0] == ranges["payments"])
    row[9] = "1" * 64
    extract["records"][source_range]["formulas"] = deepcopy(
        extract["records"][source_range]["unformatted"]
    )
    evidence = _refresh_reader_evidence(extract)
    manifest = map_export(extract, evidence, expected_binding=binding)
    assert manifest["status"] == "quarantined"
    assert "source_metadata_changed" in manifest["issue_codes"]

    extract, evidence, binding, *_ = make_candidate(coverage_value="partial")
    manifest = map_export(extract, evidence, expected_binding=binding)
    assert manifest["status"] == "quarantined"
    assert "coverage_incomplete" in manifest["issue_codes"]


def test_native_major_decimal_is_exact_and_float_precision_is_rejected():
    extract, _evidence, binding, _records, ranges = make_candidate()
    source_range = next(key for key in extract["records"] if key.startswith("Fixture_Source"))
    source_values = extract["records"][source_range]["unformatted"]["values"]
    source_header = source_values[0]
    unit_index = source_header.index("money_unit")
    payments_meta = next(row for row in source_values[1:] if row[0] == ranges["payments"])
    payments_meta[unit_index] = "native_major_decimal"
    payment_values = extract["records"][ranges["payments"]]["unformatted"]["values"]
    amount_index = payment_values[0].index("actual_amount_ils")
    payment_values[1][amount_index] = "123.45"
    payment_values[2][amount_index] = "23.45"
    accepted_range = next(key for key in extract["records"] if "_assaf_os_accepted" in key)
    accepted_values = extract["records"][accepted_range]["unformatted"]["values"]
    for payment_row in payment_values[1:]:
        identity = payment_row[payment_values[0].index("payment_id")]
        accepted_row = next(row for row in accepted_values[1:] if row[1] == identity)
        accepted_row[4] = canonical_digest(payment_row)
    extract["records"][accepted_range]["formulas"] = deepcopy(
        extract["records"][accepted_range]["unformatted"]
    )
    extract["records"][ranges["payments"]]["formulas"] = deepcopy(
        extract["records"][ranges["payments"]]["unformatted"]
    )
    payments_meta[source_header.index("unformatted_hash")] = canonical_digest(
        extract["records"][ranges["payments"]]["unformatted"]
    )
    payments_meta[source_header.index("formula_hash")] = canonical_digest(
        extract["records"][ranges["payments"]]["formulas"]
    )
    extract["records"][source_range]["formulas"] = deepcopy(
        extract["records"][source_range]["unformatted"]
    )
    evidence = _refresh_reader_evidence(extract)
    manifest = map_export(extract, evidence, expected_binding=binding)
    assert manifest["status"] == "validated_candidate"
    assert manifest["money_by_currency"]["ILS"]["net_payments_minor"] == 10_000

    bad = deepcopy(extract)
    bad_values = bad["records"][ranges["payments"]]["unformatted"]["values"]
    bad_values[1][amount_index] = 123.456
    bad["records"][ranges["payments"]]["formulas"] = deepcopy(
        bad["records"][ranges["payments"]]["unformatted"]
    )
    bad_source = bad["records"][source_range]["unformatted"]["values"]
    bad_meta = next(row for row in bad_source[1:] if row[0] == ranges["payments"])
    bad_meta[source_header.index("unformatted_hash")] = canonical_digest(
        bad["records"][ranges["payments"]]["unformatted"]
    )
    bad_meta[source_header.index("formula_hash")] = canonical_digest(
        bad["records"][ranges["payments"]]["formulas"]
    )
    bad["records"][source_range]["formulas"] = deepcopy(bad["records"][source_range]["unformatted"])
    bad_evidence = _refresh_reader_evidence(bad)
    manifest = map_export(bad, bad_evidence, expected_binding=binding)
    assert manifest["status"] == "quarantined"
    assert "record_invalid" in manifest["issue_codes"]


def test_formula_in_authoritative_cell_is_preserved_and_quarantined():
    extract, _evidence, binding, _records, ranges = make_candidate()
    formulas = extract["records"][ranges["tasks"]]["formulas"]["values"]
    formulas[1][formulas[0].index("title")] = '=IMPORTDATA("https://example.invalid")'
    source_range = next(key for key in extract["records"] if key.startswith("Fixture_Source"))
    source_values = extract["records"][source_range]["unformatted"]["values"]
    header = source_values[0]
    meta = next(row for row in source_values[1:] if row[0] == ranges["tasks"])
    meta[header.index("formula_hash")] = canonical_digest(
        extract["records"][ranges["tasks"]]["formulas"]
    )
    extract["records"][source_range]["formulas"] = deepcopy(
        extract["records"][source_range]["unformatted"]
    )
    evidence = _refresh_reader_evidence(extract)
    manifest = map_export(extract, evidence, expected_binding=binding)
    assert manifest["status"] == "quarantined"
    assert "formula_in_authoritative_field" in manifest["issue_codes"]
    assert (
        manifest["provenance"]["tasks"]["TAS_1"]["formula_cells"]["title"]
        == '=IMPORTDATA("https://example.invalid")'
    )


def test_physical_schema_rows_map_native_money_refs_and_raw_owned_fingerprints():
    extract, evidence, binding, records, ranges = make_candidate(physical=True)
    manifest = map_export(extract, evidence, expected_binding=binding)

    assert manifest["status"] == "validated_candidate"
    assert manifest["source_format"] == PHYSICAL_SOURCE_FORMAT
    assert manifest["money_by_currency"]["ILS"]["net_cash_minor"] == 8_500
    provenance = manifest["provenance"]["payments"]["PAY_1"]
    assert provenance["source_range"] == ranges["payments"]
    assert provenance["money_unit"] == "native_major_decimal"
    assert provenance["raw_fingerprint_contract"] == "apps_script_raw_owned_sha256"
    assert provenance["source_input_digest"] == record_input_digest(
        "payments", records["payments"][0]
    )


def test_physical_mapping_matches_actual_apps_script_native_oracle_with_display_refs():
    extract, _evidence, binding, records, ranges = make_candidate(physical=True)
    native_matrices = {
        entity: {
            "headers": deepcopy(extract["records"][ranges[entity]]["unformatted"]["values"][0]),
            "rows": deepcopy(extract["records"][ranges[entity]]["unformatted"]["values"][1:]),
        }
        for entity in ENTITIES
    }
    completed = subprocess.run(
        ["node", str(NATIVE_ORACLE)],
        cwd=ROOT,
        input=json.dumps(
            {
                "synthetic_only": True,
                "records": records,
                "native_matrices": native_matrices,
                "display_refs": [
                    {"entity": "payments", "row_index": 0, "field": "person_id"},
                    {"entity": "payments", "row_index": 0, "field": "work_id"},
                ],
            },
            ensure_ascii=False,
        ),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    oracle = json.loads(completed.stdout)
    accepted_range = next(key for key in extract["records"] if "_assaf_os_accepted" in key)
    accepted_rows = extract["records"][accepted_range]["unformatted"]["values"]
    source_range = next(key for key in extract["records"] if key.startswith("Fixture_Source"))
    source_values = extract["records"][source_range]["unformatted"]["values"]
    source_header = source_values[0]
    for entity in ENTITIES:
        entity_oracle = oracle["entities"][entity]
        values = [entity_oracle["headers"]] + [item["native_row"] for item in entity_oracle["rows"]]
        for render in ("unformatted", "formulas"):
            extract["records"][ranges[entity]][render]["values"] = deepcopy(values)
        metadata = next(row for row in source_values[1:] if row[0] == ranges[entity])
        metadata[source_header.index("unformatted_hash")] = canonical_digest(
            extract["records"][ranges[entity]]["unformatted"]
        )
        metadata[source_header.index("formula_hash")] = canonical_digest(
            extract["records"][ranges[entity]]["formulas"]
        )
        for item in entity_oracle["rows"]:
            accepted = next(
                row for row in accepted_rows[1:] if row[0] == entity and row[1] == item["record_id"]
            )
            accepted[3] = item["input_fingerprint"]
            accepted[4] = item["raw_fingerprint"]
    extract["records"][accepted_range]["formulas"] = deepcopy(
        extract["records"][accepted_range]["unformatted"]
    )
    extract["records"][source_range]["formulas"] = deepcopy(
        extract["records"][source_range]["unformatted"]
    )
    evidence = _refresh_reader_evidence(extract)
    manifest = map_export(extract, evidence, expected_binding=binding)
    assert manifest["status"] == "validated_candidate"
    oracle_payment = oracle["entities"]["payments"]["rows"][0]
    provenance = manifest["provenance"]["payments"]["PAY_1"]
    assert provenance["source_raw_digest"] == oracle_payment["raw_fingerprint"]
    assert provenance["source_input_digest"] == oracle_payment["input_fingerprint"]


def test_source_metadata_identity_time_and_plain_formula_disagreement_are_blocked():
    extract, _evidence, binding, _records, ranges = make_candidate()
    source_range = next(key for key in extract["records"] if key.startswith("Fixture_Source"))
    source_values = extract["records"][source_range]["unformatted"]["values"]
    source_header = source_values[0]
    source_values[1][source_header.index("source_id")] = ""
    source_values[2][source_header.index("observed_at")] = "2001-01-01T00:00:00+00:00"
    extract["records"][source_range]["formulas"] = deepcopy(
        extract["records"][source_range]["unformatted"]
    )
    evidence = _refresh_reader_evidence(extract)
    manifest = map_export(extract, evidence, expected_binding=binding)
    assert manifest["status"] == "quarantined"
    assert {"source_id_missing_or_changed", "invalid_source_observed_at"} <= set(
        manifest["issue_codes"]
    )

    extract, _evidence, binding, _records, ranges = make_candidate()
    formulas = extract["records"][ranges["tasks"]]["formulas"]
    formulas["values"][1][0] = "TAS_PLAIN_CHANGED"
    source_range = next(key for key in extract["records"] if key.startswith("Fixture_Source"))
    source_values = extract["records"][source_range]["unformatted"]["values"]
    source_header = source_values[0]
    metadata = next(row for row in source_values[1:] if row[0] == ranges["tasks"])
    metadata[source_header.index("formula_hash")] = canonical_digest(formulas)
    extract["records"][source_range]["formulas"] = deepcopy(
        extract["records"][source_range]["unformatted"]
    )
    evidence = _refresh_reader_evidence(extract)
    manifest = map_export(extract, evidence, expected_binding=binding)
    assert manifest["status"] == "inconclusive"
    assert "formula_nonformula_value_changed" in manifest["issue_codes"]


def test_forged_acceptance_formula_raw_digest_and_overwide_rows_cannot_validate():
    for mutation, expected_status, expected_code in (
        ("formula", "quarantined", "formula_in_evidence_metadata"),
        ("raw_digest", "quarantined", "accepted_raw_changed"),
        ("overwide", "inconclusive", "range_envelope_invalid"),
    ):
        extract, _evidence, binding, *_ = make_candidate()
        accepted_range = next(key for key in extract["records"] if "_assaf_os_accepted" in key)
        raw = extract["records"][accepted_range]["unformatted"]["values"]
        formulas = extract["records"][accepted_range]["formulas"]["values"]
        if mutation == "formula":
            formulas[1][2] = "=1"
        elif mutation == "raw_digest":
            for matrix in (raw, formulas):
                matrix[1][4] = "0" * 64
        else:
            raw[1].append("extra")
            formulas[1].append("extra")
        evidence = _refresh_reader_evidence(extract)
        manifest = map_export(extract, evidence, expected_binding=binding)
        assert manifest["status"] == expected_status
        assert expected_code in manifest["issue_codes"]


def test_malformed_reader_envelopes_return_structured_inconclusive_results():
    extract, _evidence, binding, _records, ranges = make_candidate()
    for render in ("unformatted", "formulas"):
        extract["records"][ranges["tasks"]][render]["values"][0][0] = ["nested"]
    evidence = _refresh_reader_evidence(extract)
    manifest = map_export(extract, evidence, expected_binding=binding)
    assert manifest["status"] == "inconclusive"
    assert "range_envelope_invalid" in manifest["issue_codes"]

    extract, _evidence, binding, _records, ranges = make_candidate()
    del extract["records"][ranges["tasks"]]["unformatted"]
    evidence = deepcopy(extract["evidence"])
    evidence["reads"] = deepcopy(extract["records"])
    evidence["read_digest"] = canonical_digest(extract["records"])
    for item in evidence["passes"]:
        item["read_digest"] = evidence["read_digest"]
    extract["evidence"] = deepcopy(evidence)
    manifest = map_export(extract, evidence, expected_binding=binding)
    assert manifest["status"] == "inconclusive"
    assert "reader_response_budget_invalid" in manifest["issue_codes"]

    extract, _evidence, binding, *_ = make_candidate()
    evidence = extract["evidence"]
    guards = [evidence["guard_before"], evidence["guard_after"]]
    for item in evidence["passes"]:
        guards.extend((item["guard_before"], item["guard_after"]))
    for guard in guards:
        guard["values"][1][1] = True
    evidence["passes"][0]["pass_number"] = True
    evidence = _refresh_reader_evidence(extract)
    manifest = map_export(extract, evidence, expected_binding=binding)
    assert manifest["status"] == "inconclusive"
    assert {"reader_guard_invalid", "reader_passes_changed"} <= set(manifest["issue_codes"])


def test_all_accepted_revisions_are_unique_contiguous_and_magnitude_bounded():
    records = fixture_records()
    records["tasks"][0]["revision"] = 2
    extract, _evidence, binding, *_ = make_candidate(records)
    accepted_range = next(key for key in extract["records"] if "_assaf_os_accepted" in key)
    accepted = extract["records"][accepted_range]["unformatted"]["values"]
    latest = next(row for row in accepted[1:] if row[1] == "TAS_1")
    older = deepcopy(latest)
    older[2] = 1
    snapshot = json.loads(older[5])
    snapshot["revision"] = 1
    older[5] = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    conflict = deepcopy(older)
    older[6] = "2026-10-13T12:34:56+00:00"
    conflict[6] = "2026-10-12T12:34:56+00:00"
    accepted.extend([older, conflict])
    extract["records"][accepted_range]["formulas"] = deepcopy(
        extract["records"][accepted_range]["unformatted"]
    )
    evidence = _refresh_reader_evidence(extract)
    manifest = map_export(extract, evidence, expected_binding=binding)
    assert manifest["status"] == "quarantined"
    assert "accepted_revision_ambiguous" in manifest["issue_codes"]

    extract, _evidence, binding, *_ = make_candidate(records)
    accepted_range = next(key for key in extract["records"] if "_assaf_os_accepted" in key)
    accepted = extract["records"][accepted_range]["unformatted"]["values"]
    latest = next(row for row in accepted[1:] if row[1] == "TAS_1")
    invalid_old = deepcopy(latest)
    invalid_old[2] = 1
    invalid_snapshot = {
        "entity": "tasks",
        "record_id": "TAS_1",
        "revision": 1,
        "input": {"bogus": "x"},
    }
    invalid_old[3] = canonical_digest(invalid_snapshot["input"])
    invalid_old[5] = json.dumps(invalid_snapshot, separators=(",", ":"))
    invalid_old[6] = "2026-10-13T12:34:56+00:00"
    accepted.append(invalid_old)
    extract["records"][accepted_range]["formulas"] = deepcopy(
        extract["records"][accepted_range]["unformatted"]
    )
    evidence = _refresh_reader_evidence(extract)
    manifest = map_export(extract, evidence, expected_binding=binding)
    assert manifest["status"] == "quarantined"
    assert "accepted_snapshot_mismatch" in manifest["issue_codes"]

    huge = fixture_records()
    huge["tasks"][0]["revision"] = 9_007_199_254_740_991
    extract, evidence, binding, *_ = make_candidate(huge)
    manifest = map_export(extract, evidence, expected_binding=binding)
    assert manifest["status"] == "quarantined"
    assert "accepted_history_gap" in manifest["issue_codes"]


def test_chunk_row_number_uses_absolute_a1_start_and_audit_json_is_validated():
    extract, evidence, binding, *_ = make_candidate(range_starts={"tasks": 201})
    manifest = map_export(extract, evidence, expected_binding=binding)
    assert manifest["status"] == "validated_candidate"
    assert manifest["provenance"]["tasks"]["TAS_1"]["source_row_number"] == 202

    audit_cases = (
        ("proposal", "proposal_history_invalid"),
        ("receipt", "receipt_history_invalid"),
    )
    for kind, code in audit_cases:
        extract, _evidence, binding, *_ = make_candidate()
        target = next(key for key in extract["records"] if f"_assaf_os_{kind}" in key)
        index = 9 if kind == "proposal" else 4
        for render in ("unformatted", "formulas"):
            extract["records"][target][render]["values"][1][index] = "broken JSON"
        evidence = _refresh_reader_evidence(extract)
        manifest = map_export(extract, evidence, expected_binding=binding)
        assert manifest["status"] == "quarantined"
        assert code in manifest["issue_codes"]

    extract, _evidence, binding, *_ = make_candidate()
    accepted_range = next(key for key in extract["records"] if "_assaf_os_accepted" in key)
    for render in ("unformatted", "formulas"):
        rows = extract["records"][accepted_range][render]["values"]
        for row in rows[1:]:
            row[6] = "2099-01-01T00:00:00+00:00"
            row[7] = ""
            row[8] = ""
    evidence = _refresh_reader_evidence(extract)
    manifest = map_export(extract, evidence, expected_binding=binding)
    assert manifest["status"] == "quarantined"
    assert "accepted_audit_invalid" in manifest["issue_codes"]

    extract, _evidence, binding, *_ = make_candidate()
    proposal_range = next(key for key in extract["records"] if "_assaf_os_proposals" in key)
    for render in ("unformatted", "formulas"):
        proposal = extract["records"][proposal_range][render]["values"][1]
        proposal[5] = "promote_lead"
        proposal[6] = "LEA_1"
        proposal[9] = json.dumps(
            {
                "after": {
                    "lead": {"lead_id": "LEA_1"},
                    "client": {"person_id": "PER_1"},
                }
            },
            separators=(",", ":"),
        )
    evidence = _refresh_reader_evidence(extract)
    manifest = map_export(extract, evidence, expected_binding=binding)
    assert manifest["status"] == "quarantined"
    assert "accepted_proposal_target_mismatch" in manifest["issue_codes"]
