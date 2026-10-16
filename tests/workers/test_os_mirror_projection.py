"""V2-29 mirror layout: tabs, schema-label headers, calculated columns, RAW-safe cells, equality."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from app.services.os_ledger import api
from app.services.os_ledger import mirror_projection as mp

from tests.workers.mirror_fakes import FakeReader, TxCounter
from tests.workers.test_os_mirror import (
    CLIENT,
    EXPENSE,
    LEAD,
    MONTHLY,
    PAYMENT,
    TASK,
    TIME,
    WORK,
)

ROOT = Path(__file__).resolve().parents[2]


def _snapshot(**extra: Any) -> mp.Snapshot:
    reader = FakeReader(TxCounter())
    for entity, record in (
        ("leads", LEAD), ("clients", CLIENT), ("works", WORK), ("payments", PAYMENT),
        ("time", TIME), ("expenses", EXPENSE), ("tasks", TASK),
    ):
        reader.commit(entity, record)
    reader.monthly = [dict(MONTHLY)]
    for entity, record in extra.items():
        reader.commit(entity, record)
    return mp.load_snapshot(reader, object())


def _rows(projection: mp.Projection, entity: str) -> list[dict[str, Any]]:
    tab = mp.ENTITY_TABS[entity] if entity != "summary" else mp.SUMMARY_TAB
    header, *rows = projection.tabs[tab]
    return [dict(zip(header, row, strict=True)) for row in rows]


def test_tabs_are_the_workbook_layout_and_headers_are_the_schema_labels() -> None:
    schema = json.loads(
        (ROOT / "app/prototypes/assaf_os_export_schema.json").read_text(encoding="utf-8")
    )
    assert mp.MIRROR_TABS == (
        "סיכום", "לידים", "לקוחות", "עבודות", "תקבולים", "שעות עבודה", "הוצאות והשקעות",
        "משימות",
    )
    for entity in api.ENTITIES:
        spec = schema["entities"][entity]
        assert mp.ENTITY_TABS[entity] == spec["name"]
        assert mp.headers(entity) == tuple(field["label"] for field in spec["fields"])
    script = (ROOT / "ops/assaf-os/schema.js").read_text(encoding="utf-8")
    for label in mp.headers("summary"):
        assert f'"{label}"' in script
    assert 'name: "סיכום"' in script


def test_records_project_with_major_money_hours_and_calculated_columns() -> None:
    projection = _snapshot()
    assert projection.observed_version == 8
    rendered = mp.project(projection)
    client = _rows(rendered, "clients")[0]
    assert client[mp.label("clients", "person_id")] == "PER_alpha"
    assert client[mp.label("clients", "revision")] == 1
    assert client[mp.label("clients", "received_learning_ils")] == 0
    assert client[mp.label("clients", "received_automation_ils")] == 1500.5
    assert client[mp.label("clients", "total_received_ils")] == 1500.5
    assert client[mp.label("clients", "recorded_hours")] == 1.5
    assert client[mp.label("clients", "hourly_ils")] == 1000.33
    assert client[mp.label("clients", "foreign_currency_flag")] is False
    work = _rows(rendered, "works")[0]
    assert work[mp.label("works", "agreed_amount_ils")] == 5000
    assert work[mp.label("works", "received_ils")] == 1500.5
    assert work[mp.label("works", "recorded_hours")] == 1.5
    payment = _rows(rendered, "payments")[0]
    assert payment[mp.label("payments", "actual_amount_ils")] == 1500.5
    assert payment[mp.label("payments", "original_payment_id")] == ""
    summary = _rows(rendered, "summary")
    assert summary == [
        dict(zip(mp.headers("summary"), [
            "business", "2026-10", "", "תזרים עסקי", "", "ILS", 1500.5, 1.5, 1000.33, False,
            20, 1480.5, "complete", "",
        ], strict=True))
    ]


def test_unknown_record_keys_never_reach_a_cell() -> None:
    marker = "PERSONAL-MARKER-7731"
    projection = mp.project(_snapshot(expenses={**EXPENSE, "detail": marker, "merchant": marker}))
    cells = [cell for rows in projection.tabs.values() for row in rows for cell in row]
    assert marker not in cells
    assert set(projection.tabs) == set(mp.MIRROR_TABS)


def test_a_value_of_the_wrong_shape_fails_closed() -> None:
    with pytest.raises(mp.ProjectionError) as caught:
        mp.project(_snapshot(clients={**CLIENT, "notes": {"nested": "x"}}))
    assert caught.value.code == "record_shape_invalid"
    with pytest.raises(mp.ProjectionError):
        mp.project(_snapshot(payments={**PAYMENT, "actual_amount_ils": True}))


def test_readback_equality_normalizes_blanks_and_numbers_but_not_types() -> None:
    projection = mp.Projection(observed_version=3, tabs={tab: () for tab in mp.MIRROR_TABS})
    projection = mp.Projection(
        observed_version=3,
        tabs={**projection.tabs, "לידים": (("a", "b", ""), (1, 1.5, True), ("", "", ""))},
    )

    def readback(rows: list[list[Any]]) -> dict[str, list[list[Any]]]:
        out = {mp.tab_range(tab): [] for tab in mp.MIRROR_TABS}
        out[mp.tab_range("לידים")] = rows
        return out

    assert mp.readback_equal(projection, readback([["a", "b"], [1.0, 1.5000000001, True]]))
    assert not mp.readback_equal(projection, readback([["a", "b"], [1, 1.5, 1]]))
    assert not mp.readback_equal(projection, readback([["a", "b"], ["1", 1.5, True]]))
    assert not mp.readback_equal(projection, readback([["a", "b"], [1, 1.5, True], ["x"]]))
    assert not mp.readback_equal(projection, readback([["a", "b", "c"], [1, 1.5, True]]))
    missing = readback([["a", "b"], [1, 1.5, True]])
    missing.pop(mp.tab_range("משימות"))
    assert not mp.readback_equal(projection, missing)


def test_write_ranges_are_raw_anchors_padded_to_the_known_extent() -> None:
    rendered = mp.project(_snapshot())
    lead_tab = mp.ENTITY_TABS["leads"]
    ranges = dict(mp.write_ranges(rendered, {lead_tab: [5, 20]}))
    rows = ranges[mp.tab_anchor(lead_tab)]
    assert len(rows) == 5 and {len(row) for row in rows} == {20}
    assert rows[2:] == [[""] * 20] * 3
    assert all(a1.endswith("!A1") for a1 in ranges)
    assert mp.tab_of(mp.tab_anchor("it's")) == "it's"
    assert mp.tab_anchor("it's") == "'it''s'!A1"
    assert sorted(mp.read_ranges()) == sorted(mp.tab_range(tab) for tab in mp.MIRROR_TABS)
    assert mp.coverage_of({mp.tab_range(lead_tab): [["a"], ["b", "c"]]})[lead_tab] == [2, 2]


def test_snapshot_that_never_settles_is_refused() -> None:
    reader = FakeReader(TxCounter())
    reader.commit("clients", CLIENT)
    reader.on_page = lambda entity: reader.commit("tasks", {**TASK, "title": entity})
    with pytest.raises(mp.ProjectionError) as caught:
        mp.load_snapshot(reader, object())
    assert caught.value.code == "snapshot_unstable"


def test_a_snapshot_over_the_row_cap_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(mp, "MAX_ROWS", 3)
    reader = FakeReader(TxCounter())
    for index in range(4):
        reader.commit("tasks", {**TASK, "task_id": f"TAS_{index}"})
    with pytest.raises(mp.ProjectionError) as caught:
        mp.load_snapshot(reader, object())
    assert caught.value.code == "mirror_too_large"
