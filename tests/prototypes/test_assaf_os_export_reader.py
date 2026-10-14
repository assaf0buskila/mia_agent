from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime

import app.prototypes.assaf_os_export_reader as reader_module
import pytest
from app.prototypes.assaf_os_export_reader import (
    ExportReaderError,
    LocalExportReader,
    ReaderBinding,
    ReadRequest,
    SyntheticGetTransport,
)

WORKBOOK = "synthetic_workbook_01"
DATA_RANGE = "Leads!A1:C3"
SECOND_RANGE = "'Source Meta'!A1:D2"
GUARD_RANGE = "'Export Guard'!A1:F2"
PARAMS_RAW = (
    ("majorDimension", "ROWS"),
    ("valueRenderOption", "UNFORMATTED_VALUE"),
)


def code(expected: str, call) -> None:
    with pytest.raises(ExportReaderError) as caught:
        call()
    assert caught.value.code == expected


def binding(*, ranges: tuple[str, ...] = (DATA_RANGE, SECOND_RANGE), **changes) -> ReaderBinding:
    values = {
        "workbook_id": WORKBOOK,
        "workspace_id": "synthetic_workspace",
        "authority_epoch": 3,
        "source_epoch": 7,
        "schema_version": "assaf-os.synthetic.v1",
        "allowed_ranges": ranges,
        "guard_range": GUARD_RANGE,
    }
    values.update(changes)
    return ReaderBinding(**values)


def envelope(a1_range: str, values: list[list[object]]) -> dict:
    return {"range": a1_range, "majorDimension": "ROWS", "values": values}


def guard(revision: int = 11, *, source_epoch: int = 7, token: str = "fixture-A") -> dict:
    return envelope(
        GUARD_RANGE,
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
                "synthetic_workspace",
                3,
                source_epoch,
                "assaf-os.synthetic.v1",
                revision,
                token,
            ],
        ],
    )


def fixture_responses() -> dict[str, list[dict]]:
    raw_one = envelope(
        DATA_RANGE,
        [
            ["lead_id", "name", "__native_acceptance_json"],
            ["lead_fixture_1", "Synthetic Ada", '{"accepted_revision":1}'],
        ],
    )
    formula_one = envelope(
        DATA_RANGE,
        [
            ["lead_id", "name", "__native_acceptance_json"],
            ["lead_fixture_1", "Synthetic Ada", '=JSON_METADATA("fixture")'],
        ],
    )
    raw_two = envelope(
        SECOND_RANGE,
        [["source", "schema", "epoch", "revision"], ["synthetic", "v1", 7, 11]],
    )
    formula_two = deepcopy(raw_two)
    return {
        DATA_RANGE: [
            deepcopy(raw_one),
            deepcopy(formula_one),
            deepcopy(raw_one),
            deepcopy(formula_one),
        ],
        SECOND_RANGE: [
            deepcopy(raw_two),
            deepcopy(formula_two),
            deepcopy(raw_two),
            deepcopy(formula_two),
        ],
        GUARD_RANGE: [guard(), guard(), guard(), guard()],
    }


def enabled_reader(
    responses: dict[str, list[dict]] | None = None,
    *,
    reader_binding: ReaderBinding | None = None,
    chunk_rows: int = 200,
) -> tuple[LocalExportReader, SyntheticGetTransport]:
    selected_binding = reader_binding or binding()
    transport = SyntheticGetTransport(WORKBOOK, responses or fixture_responses())
    return (
        LocalExportReader(
            selected_binding,
            transport,
            enabled=True,
            chunk_rows=chunk_rows,
        ),
        transport,
    )


def test_candidate_is_authentic_bounded_evidence_not_snapshot_authority():
    source = fixture_responses()
    unchanged = deepcopy(source)
    reader, transport = enabled_reader(source)

    result = reader.extract()

    assert result["status"] == "candidate"
    assert result["synthetic_only"] is True
    assert result["preparation_only"] is True
    assert result["issue_codes"] == []
    assert result["records"] == result["evidence"]["reads"]
    assert result["evidence"]["read_digest"] == result["evidence"]["passes"][0][
        "read_digest"
    ]
    assert result["evidence"]["read_digest"] == result["evidence"]["passes"][1][
        "read_digest"
    ]
    assert result["evidence"]["consistency_note"] == (
        "matching_synthetic_reads_require_approved_source_freeze"
    )
    assert result["source_binding"] == {
        "workbook_id": WORKBOOK,
        "workspace_id": "synthetic_workspace",
        "authority_epoch": 3,
        "source_epoch": 7,
        "schema_version": "assaf-os.synthetic.v1",
        "allowed_ranges": [DATA_RANGE, SECOND_RANGE],
        "guard_range": GUARD_RANGE,
    }
    assert datetime.fromisoformat(result["observed_at"]).utcoffset().total_seconds() == 0
    assert result["evidence"]["request_count"] == 12
    assert len(transport.readonly_calls) == 12
    assert {call.method for call in transport.readonly_calls} == {"GET"}
    assert {call.params for call in transport.readonly_calls} == {
        PARAMS_RAW,
        (
            ("majorDimension", "ROWS"),
            ("valueRenderOption", "FORMULA"),
        ),
    }
    assert transport.readonly_calls[0].path == (
        "/v4/spreadsheets/synthetic_workbook_01/values/"
        "%27Export%20Guard%27%21A1%3AF2"
    )
    assert source == unchanged
    assert not hasattr(reader, "snapshot")
    assert not hasattr(transport, "append")
    assert not hasattr(transport, "batch_update")


def test_default_off_and_binding_refusals_make_zero_calls():
    transport = SyntheticGetTransport(WORKBOOK, fixture_responses())
    reader = LocalExportReader(binding(), transport)
    code("export_reader_disabled", reader.extract)
    assert transport.readonly_calls == ()

    wrong_workbook = binding(workbook_id="synthetic_workbook_02")
    code(
        "workbook_not_allowed",
        lambda: LocalExportReader(wrong_workbook, transport, enabled=True),
    )
    wrong_range = binding(ranges=("Tasks!A1:C3", SECOND_RANGE))
    code(
        "transport_range_allowlist_mismatch",
        lambda: LocalExportReader(wrong_range, transport, enabled=True),
    )
    assert transport.readonly_calls == ()


def test_transport_rejects_verb_route_query_workbook_and_range_before_trace():
    transport = SyntheticGetTransport(WORKBOOK, fixture_responses())
    valid_path = "/v4/spreadsheets/synthetic_workbook_01/values/Leads%21A1%3AC3"
    cases = [
        (
            "read_method_refused",
            ReadRequest("POST", valid_path, PARAMS_RAW),
        ),
        (
            "read_route_refused",
            ReadRequest("GET", valid_path + ":append", PARAMS_RAW),
        ),
        (
            "read_route_refused",
            ReadRequest("GET", valid_path + "?valueInputOption=RAW", PARAMS_RAW),
        ),
        (
            "read_route_refused",
            ReadRequest("GET", "https://sheets.googleapis.com" + valid_path, PARAMS_RAW),
        ),
        (
            "workbook_not_allowed",
            ReadRequest(
                "GET",
                "/v4/spreadsheets/synthetic_workbook_02/values/Leads%21A1%3AC3",
                PARAMS_RAW,
            ),
        ),
        (
            "range_not_allowed",
            ReadRequest(
                "GET",
                "/v4/spreadsheets/synthetic_workbook_01/values/Unknown%21A1%3AC3",
                PARAMS_RAW,
            ),
        ),
        (
            "read_query_refused",
            ReadRequest(
                "GET",
                valid_path,
                (("majorDimension", "COLUMNS"), ("valueRenderOption", "FORMULA")),
            ),
        ),
    ]
    for expected, request in cases:
        code(expected, lambda selected=request: transport.read(selected))
    assert transport.readonly_calls == ()


def test_request_is_frozen_and_subclassed_or_arbitrary_transports_are_refused():
    request = ReadRequest("GET", "/fixed", PARAMS_RAW)
    with pytest.raises(FrozenInstanceError):
        request.method = "POST"

    class DerivedTransport(SyntheticGetTransport):
        pass

    derived = DerivedTransport(WORKBOOK, fixture_responses())
    code(
        "synthetic_transport_required",
        lambda: LocalExportReader(binding(), derived, enabled=True),
    )
    code(
        "synthetic_transport_required",
        lambda: LocalExportReader(binding(), object(), enabled=True),
    )
    assert derived.readonly_calls == ()


@pytest.mark.parametrize("render_index", [0, 1])
def test_raw_or_formula_drift_is_inconclusive_and_discards_records(render_index):
    responses = fixture_responses()
    changed = deepcopy(responses[DATA_RANGE][render_index])
    changed["values"][1][1] = "Drifted synthetic cell"
    responses[DATA_RANGE][render_index + 2] = changed
    reader, _transport = enabled_reader(responses)

    result = reader.extract()

    assert result["status"] == "inconclusive"
    assert result["records"] == {}
    assert result["evidence"]["reads"] == {}
    assert result["evidence"]["read_digest"] is None
    assert result["issue_codes"] == ["data_drift"]
    assert result["evidence"]["issues"] == [
        {
            "code": "data_drift",
            "range": DATA_RANGE,
            "render": "unformatted" if render_index == 0 else "formulas",
        }
    ]


def test_guard_revision_drift_and_aba_are_typed_and_inconclusive():
    responses = fixture_responses()
    responses[GUARD_RANGE] = [guard(11), guard(12), guard(11), guard(11)]
    reader, _transport = enabled_reader(responses)

    result = reader.extract()

    assert result["status"] == "inconclusive"
    assert result["records"] == {}
    assert {"guard_changed", "guard_revision_changed", "guard_aba_detected"} <= set(
        result["issue_codes"]
    )
    assert result["evidence"]["guard_before"] == guard(11)
    assert result["evidence"]["guard_after"] == guard(11)


def test_guard_source_epoch_mismatch_is_visible_even_when_stable():
    responses = fixture_responses()
    responses[GUARD_RANGE] = [guard(source_epoch=8) for _ in range(4)]
    reader, _transport = enabled_reader(responses)

    result = reader.extract()

    assert result["status"] == "inconclusive"
    assert result["records"] == {}
    assert result["issue_codes"] == ["guard_source_epoch_mismatch"]
    assert len(result["evidence"]["issues"]) == 4


def test_lost_read_is_bounded_inconclusive_and_drops_other_complete_reads():
    responses = fixture_responses()
    responses[DATA_RANGE] = responses[DATA_RANGE][:2]
    reader, transport = enabled_reader(responses)

    result = reader.extract()

    assert result["status"] == "inconclusive"
    assert result["records"] == {}
    assert result["evidence"]["reads"] == {}
    assert result["evidence"]["read_digest"] is None
    assert {"synthetic_response_missing", "data_read_incomplete"} <= set(
        result["issue_codes"]
    )
    assert len(transport.readonly_calls) == 12


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda item: item.update({"majorDimension": "COLUMNS"}), "response_binding_mismatch"),
        (lambda item: item.update({"range": "Tasks!A1:C3"}), "response_binding_mismatch"),
        (
            lambda item: item["values"].extend([["row", "three"], ["row", "four"]]),
            "response_matrix_bounds",
        ),
    ],
)
def test_invalid_response_envelopes_are_inconclusive_with_typed_context(mutate, expected):
    responses = fixture_responses()
    mutate(responses[DATA_RANGE][0])
    reader, _transport = enabled_reader(responses)

    result = reader.extract()

    assert result["status"] == "inconclusive"
    assert result["records"] == {}
    assert expected in result["issue_codes"]
    issue = next(item for item in result["evidence"]["issues"] if item["code"] == expected)
    assert issue["pass_number"] == 1
    assert issue["range"] == DATA_RANGE
    assert issue["render"] == "UNFORMATTED_VALUE"


def test_response_depth_and_byte_budgets_fail_closed(monkeypatch):
    responses = fixture_responses()
    nested: object = "synthetic"
    for _ in range(20):
        nested = [nested]
    responses[DATA_RANGE][0]["values"][1][1] = nested
    code(
        "response_json_too_complex",
        lambda: SyntheticGetTransport(WORKBOOK, responses),
    )

    monkeypatch.setattr(reader_module, "MAX_TOTAL_RESPONSE_BYTES", 100)
    reader, transport = enabled_reader()
    result = reader.extract()
    assert "response_byte_budget_exceeded" in result["issue_codes"]
    assert result["records"] == {}
    assert len(transport.readonly_calls) == 1
    assert result["evidence"]["response_bytes"] == 0
    assert result["evidence"]["attempted_response_bytes"] == 101
    assert result["evidence"]["response_byte_budget_exhausted"] is True


def test_oversized_malformed_envelope_stops_before_semantic_validation(monkeypatch):
    responses = fixture_responses()
    responses[GUARD_RANGE][0]["unexpected_padding"] = "x" * 1_000
    monkeypatch.setattr(reader_module, "MAX_TOTAL_RESPONSE_BYTES", 200)
    reader, transport = enabled_reader(responses)

    result = reader.extract()

    assert "response_byte_budget_exceeded" in result["issue_codes"]
    assert "invalid_response_envelope" not in result["issue_codes"]
    assert result["records"] == {}
    assert result["evidence"]["attempted_response_bytes"] == 201
    assert len(transport.readonly_calls) == 1


@pytest.mark.parametrize(
    ("cell", "expected"),
    [(None, "response_json_null"), (float("nan"), "response_json_invalid")],
)
def test_nonfinite_or_null_fixture_is_rejected_before_transport_exists(cell, expected):
    responses = fixture_responses()
    responses[DATA_RANGE][0]["values"][1].append(cell)
    code(expected, lambda: SyntheticGetTransport(WORKBOOK, responses))


def test_fixture_object_copy_hook_is_never_executed():
    copied = []

    class HostileCell:
        def __deepcopy__(self, _memo):
            copied.append(True)
            raise AssertionError("copy hook must not run")

    responses = fixture_responses()
    responses[DATA_RANGE][0]["values"][1][1] = HostileCell()
    code(
        "response_json_invalid",
        lambda: SyntheticGetTransport(WORKBOOK, responses),
    )
    assert copied == []


def test_bad_python_types_are_stable_refusals_not_raw_type_errors():
    transport = SyntheticGetTransport(WORKBOOK, fixture_responses())
    code(
        "invalid_workbook_id",
        lambda: LocalExportReader(binding(workbook_id=3), transport, enabled=True),
    )
    code(
        "invalid_workspace_id",
        lambda: LocalExportReader(binding(workspace_id=[]), transport, enabled=True),
    )
    code(
        "invalid_allowed_ranges",
        lambda: LocalExportReader(
            binding(ranges=(["Leads!A1:C3"],)), transport, enabled=True
        ),
    )
    malformed_params = ReadRequest(
        "GET",
        "/v4/spreadsheets/synthetic_workbook_01/values/Leads%21A1%3AC3",
        (("majorDimension", ["ROWS"]), ("valueRenderOption", "FORMULA")),
    )
    code("read_query_refused", lambda: transport.read(malformed_params))
    assert transport.readonly_calls == ()


def test_binding_requires_explicit_bounded_chunks_rows_columns_and_request_plan(monkeypatch):
    response = fixture_responses()
    transport = SyntheticGetTransport(WORKBOOK, response)
    code(
        "range_chunk_too_large",
        lambda: LocalExportReader(
            binding(ranges=("Leads!A1:C201", SECOND_RANGE)), transport, enabled=True
        ),
    )
    code(
        "invalid_allowed_range",
        lambda: LocalExportReader(
            binding(ranges=("Leads!A:C", SECOND_RANGE)), transport, enabled=True
        ),
    )
    code(
        "invalid_allowed_range",
        lambda: LocalExportReader(
            binding(ranges=("Leads!A1:BM2", SECOND_RANGE)), transport, enabled=True
        ),
    )
    code(
        "range_chunk_too_large",
        lambda: LocalExportReader(binding(), transport, enabled=True, chunk_rows=2),
    )

    ranges = tuple(f"Sheet{index}!A1:A200" for index in range(11))
    oversized_responses = {item: [] for item in ranges}
    oversized_responses[GUARD_RANGE] = []
    oversized_transport = SyntheticGetTransport(WORKBOOK, oversized_responses)
    code(
        "data_row_budget_exceeded",
        lambda: LocalExportReader(binding(ranges=ranges), oversized_transport, enabled=True),
    )

    monkeypatch.setattr(reader_module, "MAX_RANGES", 100)
    ranges = tuple(f"Sheet{index}!A1:A1" for index in range(50))
    request_responses = {item: [] for item in ranges}
    request_responses[GUARD_RANGE] = []
    request_transport = SyntheticGetTransport(WORKBOOK, request_responses)
    code(
        "request_budget_exceeded",
        lambda: LocalExportReader(binding(ranges=ranges), request_transport, enabled=True),
    )
    assert transport.readonly_calls == ()
    assert oversized_transport.readonly_calls == ()
    assert request_transport.readonly_calls == ()


def test_guard_range_must_be_separate_bounded_and_nonoverlapping():
    base = fixture_responses()
    same = binding(ranges=(DATA_RANGE,), guard_range=DATA_RANGE)
    code(
        "guard_range_is_data_range",
        lambda: LocalExportReader(same, SyntheticGetTransport(WORKBOOK, base), enabled=True),
    )
    overlap_binding = binding(ranges=("'Export Guard'!A2:C3", SECOND_RANGE))
    code(
        "guard_range_overlaps_data",
        lambda: LocalExportReader(
            overlap_binding, SyntheticGetTransport(WORKBOOK, base), enabled=True
        ),
    )


def test_transport_uses_private_copies_for_calls_and_fixture_responses():
    source = fixture_responses()
    transport = SyntheticGetTransport(WORKBOOK, source)
    with pytest.raises(AttributeError):
        transport.read = lambda _request: {}
    source[DATA_RANGE][0]["values"][1][1] = "caller mutation"
    request = ReadRequest(
        "GET",
        "/v4/spreadsheets/synthetic_workbook_01/values/Leads%21A1%3AC3",
        PARAMS_RAW,
    )
    response = transport.read(request)
    assert response["values"][1][1] == "Synthetic Ada"
    response["values"][1][1] = "returned mutation"
    second = transport.read(
        ReadRequest(
            "GET",
            "/v4/spreadsheets/synthetic_workbook_01/values/Leads%21A1%3AC3",
            (
                ("majorDimension", "ROWS"),
                ("valueRenderOption", "FORMULA"),
            ),
        )
    )
    assert second["values"][1][1] == "Synthetic Ada"
