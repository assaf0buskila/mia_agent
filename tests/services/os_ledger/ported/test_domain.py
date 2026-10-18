"""Ported from tests/prototypes/test_assaf_os_domain.py (V2-16, unchanged cases).

The prototype's own domain tests, run against ``app.services.os_ledger.domain``, the production copy
of ``app/prototypes/assaf_os_domain.py``. Only the imports and the repository root differ.
"""

import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from app.services.os_ledger.domain import (
    ENTITIES,
    ID_KEYS,
    SCHEMA,
    canonical_input,
    lead_contact_dependencies,
    lead_phone_identity,
    validate_state,
    validate_transition,
)
from app.services.os_ledger.domain import DomainError as LocalPrimaryError

ROOT = Path(__file__).resolve().parents[4]


def assert_code(code, operation):
    with pytest.raises(LocalPrimaryError) as exc:
        operation()
    assert exc.value.code == code


def inputs():
    return {
        "leads": {
            "name": "ליד",
            "phone": "+972502222222",
            "email": "lead@example.com",
            "path": "לימוד",
            "status": "חדש",
            "source_note": None,
        },
        "clients": {
            "name": "לקוח",
            "phone": None,
            "email": "client@example.com",
            "status": "פעיל",
            "notes": None,
        },
        "works": {
            "person_id": "PER_1",
            "title": "עבודה",
            "path": "לימוד",
            "start_date": "2026-10-13",
            "status": "פעילה",
            "agreed_amount_ils": 12_345,
            "currency": "ILS",
        },
        "payments": {
            "person_id": "PER_1",
            "work_id": "WOR_1",
            "path": "לימוד",
            "date": "2026-10-13",
            "actual_amount_ils": 10_000,
            "currency": "ILS",
            "kind": "התקבל",
            "original_payment_id": None,
            "notes": None,
        },
        "time": {
            "person_id": "PER_1",
            "work_id": "WOR_1",
            "path": "לימוד",
            "date": "2026-10-13",
            "minutes": 60,
            "description": None,
            "status": "פעיל",
        },
        "expenses": {
            "date": "2026-10-13",
            "category": "פרסום",
            "platform": "Meta",
            "path": "משותף",
            "vendor": None,
            "amount_ils": 2_500,
            "currency": "ILS",
            "payment_status": "שולם",
            "source_type": "תשלום בפועל",
            "source_key": "invoice-1",
            "notes": None,
        },
        "tasks": {
            "title": "משימה",
            "person_id": "PER_1",
            "work_id": "WOR_1",
            "path": "לימוד",
            "status": "לביצוע",
            "due_date": "2026-10-14",
            "notes": None,
        },
    }


def state():
    value = {entity: [] for entity in ENTITIES}
    source = inputs()
    value["leads"].append(
        {"lead_id": "LEA_1", "person_id": "PER_2", "revision": 1, **source["leads"]}
    )
    for entity in ENTITIES[1:]:
        value[entity].append(
            {
                ID_KEYS[entity]: {
                    "clients": "PER_1",
                    "works": "WOR_1",
                    "payments": "PAY_1",
                    "time": "TIM_1",
                    "expenses": "EXP_1",
                    "tasks": "TAS_1",
                }[entity],
                "revision": 1,
                **source[entity],
            }
        )
    return value


def node_canonical(cases):
    script = r"""
const Runtime = require('./ops/assaf-os/runtime.js');
const cases = JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(cases.map(item => {
  try { return {ok: true, value: Runtime.canonicalInput(item.entity, item.raw, item.creating)}; }
  catch (error) { return {ok: false, code: error.code}; }
})));
"""
    result = subprocess.run(
        ["node", "-e", script, json.dumps(cases, ensure_ascii=False)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


def test_exported_input_schema_and_enums_have_no_drift():
    script = r"""
const Schema = require('./ops/assaf-os/schema.js');
const entities = ['leads','clients','works','payments','time','expenses','tasks'];
const keys = [
  'key','type','role','required','enum','values','minimum','immutableAfterCreate','ref'
];
const projected = {
  source:'ops/assaf-os/schema.js', version:Schema.version, enums:Schema.enums, entities:{}
};
for (const entity of entities) projected.entities[entity] = Schema.tab(entity).fields
  .filter(field => field.role === 'input')
  .map(field => Object.fromEntries(
    keys.filter(key => Object.hasOwn(field, key)).map(key => [key, field[key]])
  ));
process.stdout.write(JSON.stringify(projected));
"""
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert json.loads(result.stdout) == SCHEMA


def test_canonical_input_matches_javascript_runtime_for_synthetic_matrix():
    base = inputs()
    cases = [{"entity": entity, "raw": raw, "creating": True} for entity, raw in base.items()]
    cases += [
        {
            "entity": "tasks",
            "raw": {"title": "  keep trim  ", "status": "לביצוע"},
            "creating": True,
        },
        {
            "entity": "tasks",
            "raw": {"title": "\ufefftrim me\ufeff", "status": "לביצוע"},
            "creating": True,
        },
        {
            "entity": "tasks",
            "raw": {"title": "😀" * 1_001, "status": "לביצוע"},
            "creating": True,
        },
        {"entity": "tasks", "raw": {"title": "x", "status": "unknown"}, "creating": True},
        {
            "entity": "tasks",
            "raw": {"title": "x", "status": "לביצוע", "revision": 1},
            "creating": True,
        },
        {"entity": "time", "raw": {**base["time"], "minutes": True}, "creating": True},
        {
            "entity": "time",
            "raw": {**base["time"], "minutes": 9_007_199_254_740_992},
            "creating": True,
        },
        {
            "entity": "payments",
            "raw": {**base["payments"], "actual_amount_ils": 1.5},
            "creating": True,
        },
        {"entity": "expenses", "raw": {**base["expenses"], "amount_ils": 0}, "creating": True},
        {"entity": "works", "raw": {**base["works"], "start_date": "2026-02-29"}, "creating": True},
        {"entity": "works", "raw": {"title": "partial"}, "creating": False},
        {"entity": "payments", "raw": {"kind": "החזר"}, "creating": False},
    ]
    expected = node_canonical(cases)
    actual = []
    for case in cases:
        try:
            actual.append(
                {
                    "ok": True,
                    "value": canonical_input(case["entity"], case["raw"], case["creating"]),
                }
            )
        except LocalPrimaryError as exc:
            actual.append({"ok": False, "code": exc.code})
    assert actual == expected


@pytest.mark.parametrize(
    ("value", "identity"),
    [
        ("050-222-2222", "+972502222222"),
        ("+972 50 222 2222", "+972502222222"),
        ("+9720502222222", ""),
        ("050-22", ""),
        ("(050 222-2222", ""),
        ("1234567", "1234567"),
        (None, ""),
    ],
)
def test_lead_phone_identity_matches_apps_script_rules(value, identity):
    assert lead_phone_identity(value) == identity


def test_lead_contact_policy_is_phone_or_casefolded_email_and_create_only():
    records = state()
    assert (
        lead_contact_dependencies({"phone": "050-222-2222"}, records["leads"]) == records["leads"]
    )
    assert (
        lead_contact_dependencies({"email": " LEAD@EXAMPLE.COM "}, records["leads"])
        == records["leads"]
    )
    duplicate = {**records["leads"][0], "lead_id": "LEA_2", "person_id": "PER_3"}
    assert_code(
        "lead_already_exists",
        lambda: validate_transition("leads", None, duplicate, records),
    )
    # Existing runtime permits contact edits; record_id exclusion also prevents self-matches.
    validate_transition("leads", records["leads"][0], records["leads"][0], records)


def test_complete_canonical_state_is_valid():
    validate_state(state())


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda s: s["time"][0].update(minutes=-1), "invalid_minutes"),
        (lambda s: s["time"][0].update(minutes=True), "invalid_minutes"),
        (lambda s: s["expenses"][0].update(date="2026-13-01"), "invalid_date"),
        (lambda s: s["payments"][0].update(person_id="PER_missing"), "client_not_found"),
        (lambda s: s["tasks"][0].update(work_id="WOR_missing"), "work_not_found"),
        (lambda s: s["time"][0].update(person_id="PER_2"), "client_not_found"),
        (lambda s: s["time"][0].update(path="אוטומציה"), "work_path_mismatch"),
        (lambda s: s["payments"][0].update(currency="EUR"), "work_currency_mismatch"),
        (lambda s: s["tasks"][0].update(created_at="forbidden"), "non_input_field"),
        (lambda s: s["works"].append(deepcopy(s["works"][0])), "invalid_or_duplicate_record_id"),
    ],
)
def test_state_rejects_bad_types_dates_references_and_duplicates(mutation, code):
    records = state()
    mutation(records)
    assert_code(code, lambda: validate_state(records))


def test_expense_source_pair_is_workspace_unique():
    records = state()
    duplicate = {**records["expenses"][0], "expense_id": "EXP_2"}
    records["expenses"].append(duplicate)
    assert_code("duplicate_expense_source", lambda: validate_state(records))
    duplicate["source_type"] = "דיווח שימוש בפלטפורמה"
    validate_state(records)


def test_full_refund_graph_checks_create_and_original_edits():
    records = state()
    refund = {
        **records["payments"][0],
        "payment_id": "PAY_2",
        "revision": 1,
        "kind": "החזר",
        "original_payment_id": "PAY_1",
        "actual_amount_ils": 6_000,
        "date": "2026-10-14",
    }
    records["payments"].append(refund)
    validate_state(records)
    second = {**refund, "payment_id": "PAY_3", "actual_amount_ils": 5_000}
    records["payments"].append(second)
    assert_code("refund_exceeds_original", lambda: validate_state(records))
    records["payments"].pop()
    records["payments"][0]["actual_amount_ils"] = 2_000
    assert_code("refund_exceeds_original", lambda: validate_state(records))
    records["payments"][0]["actual_amount_ils"] = 10_000
    records["payments"][0]["person_id"] = "PER_2"
    assert_code("client_not_found", lambda: validate_state(records))


def test_refund_shape_and_graph_reason_codes_match_runtime():
    raw = inputs()["payments"]
    assert_code(
        "refund_original_required",
        lambda: canonical_input("payments", {**raw, "kind": "החזר"}),
    )
    assert_code(
        "original_payment_only_for_refund",
        lambda: canonical_input("payments", {**raw, "original_payment_id": "PAY_2"}),
    )
    records = state()
    refund = {
        **records["payments"][0],
        "payment_id": "PAY_2",
        "kind": "החזר",
        "original_payment_id": "PAY_missing",
    }
    records["payments"].append(refund)
    assert_code("refund_original_not_found", lambda: validate_state(records))
    refund["original_payment_id"] = "PAY_1"
    refund["date"] = "2026-10-12"
    assert_code("refund_before_original", lambda: validate_state(records))


def test_immutable_path_and_work_edits_revalidate_all_dependents():
    records = state()
    before = records["works"][0]
    assert_code(
        "immutable_field_changed",
        lambda: validate_transition("works", before, {**before, "path": "אוטומציה"}, records),
    )
    assert_code(
        "client_not_found",
        lambda: validate_transition(
            "works", before, {**before, "person_id": "PER_missing"}, records
        ),
    )
    assert_code(
        "work_person_mismatch",
        lambda: validate_transition(
            "works",
            before,
            {**before, "person_id": "PER_2"},
            {
                **records,
                "clients": records["clients"]
                + [{**records["clients"][0], "person_id": "PER_2", "revision": 1}],
            },
        ),
    )
    assert_code(
        "work_currency_mismatch",
        lambda: validate_transition("works", before, {**before, "currency": "EUR"}, records),
    )
    payment = records["payments"][0]
    assert_code(
        "immutable_field_changed",
        lambda: validate_transition("payments", payment, {**payment, "path": "אוטומציה"}, records),
    )


def test_transition_rejects_missing_or_conflicting_target():
    records = state()
    task = records["tasks"][0]
    assert_code(
        "target_changed",
        lambda: validate_transition("tasks", None, task, records),
    )
    assert_code(
        "target_changed",
        lambda: validate_transition(
            "tasks", {**task, "task_id": "TAS_2"}, {**task, "task_id": "TAS_2"}, records
        ),
    )
