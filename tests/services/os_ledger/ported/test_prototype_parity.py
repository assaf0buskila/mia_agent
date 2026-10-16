"""The V2-16 copies are the prototypes (V2_PG_CONTRACTS.md §3.1: copied, never moved or edited).

``app/prototypes/assaf_os_{domain,summaries,ledger}.py`` stay as V2-13's oracle. These cases pin
every copied function to the prototype's source and behaviour, so a later edit to either side is a
visible, deliberate change.
"""

import ast
import inspect
import json
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.prototypes import assaf_os_domain as proto_domain
from app.prototypes import assaf_os_ledger as proto_ledger
from app.prototypes import assaf_os_summaries as proto_summaries
from app.prototypes.assaf_os_primary import LocalPrimaryError
from app.services.os_ledger import api, domain, ids, summaries

ROOT = Path(__file__).resolve().parents[4]


def _functions(module):
    return {name: value for name, value in vars(module).items()
            if inspect.isfunction(value) and value.__module__ == module.__name__}


def test_the_embedded_schema_is_the_prototype_schema():
    path = ROOT / "app" / "prototypes" / "assaf_os_local_schema.json"
    assert domain.SCHEMA == json.loads(path.read_text(encoding="utf-8"))
    assert domain.ENUMS == proto_domain.ENUMS
    assert dict(domain.ID_KEYS) == dict(proto_domain.ID_KEYS) == dict(api.ID_KEYS)
    assert domain.ENTITIES == proto_domain.ENTITIES == api.ENTITIES


def test_every_domain_function_is_a_verbatim_copy():
    copied, original = _functions(domain), _functions(proto_domain)
    assert set(copied) == set(original)
    for name in original:
        source = inspect.getsource(original[name])
        if name == "_error":  # the one substitution: DomainError for LocalPrimaryError
            source = source.replace("LocalPrimaryError", "DomainError")
        assert inspect.getsource(copied[name]) == source, name


def test_every_summary_function_is_a_verbatim_copy():
    copied, original = _functions(summaries), _functions(proto_summaries)
    assert set(original) <= set(copied)
    assert set(copied) - set(original) == {"_month_start", "_json", "monthly_rows"}
    for name in original:
        assert inspect.getsource(copied[name]) == inspect.getsource(original[name]), name
    assert summaries.MAX_SAFE_INTEGER == proto_summaries.MAX_SAFE_INTEGER


def test_the_ledger_helpers_are_verbatim_copies():
    assert ids.PREFIXES == proto_ledger.PREFIXES
    assert ids.DATE_KEYS == proto_ledger.DATE_KEYS

    def body(function):  # the statements, whatever the signature's name and annotations
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        return [ast.dump(statement) for statement in tree.body[0].body]

    assert body(ids._lookup) == body(proto_ledger._lookup)
    assert body(ids.envelope) == body(proto_ledger._envelope)
    assert body(ids.dependencies) == body(proto_ledger._dependencies)


@pytest.mark.parametrize("raw,creating", [
    ({"title": "  T  ", "status": "לביצוע"}, True),
    ({"title": "T", "status": "nope"}, True),
    ({"status": "לביצוע"}, False),
    ({"title": "T", "status": "לביצוע", "bogus": 1}, True),
])
def test_canonical_input_behaves_like_the_prototype(raw, creating):
    def run(module, error):
        try:
            return "ok", module.canonical_input("tasks", dict(raw), creating=creating)
        except error as exc:
            return "error", exc.code

    assert run(domain, domain.DomainError) == run(proto_domain, LocalPrimaryError)


def _state():
    return {
        "leads": [{"lead_id": "LEA_a1", "person_id": "PER_l1", "revision": 1, "name": "L",
                   "phone": "0501234567", "path": "לימוד", "status": "חדש"}],
        "clients": [{"person_id": "PER_c1", "revision": 1, "name": "C", "status": "פעיל"}],
        "works": [{"work_id": "WOR_w1", "revision": 1, "person_id": "PER_c1", "title": "W",
                   "path": "לימוד", "status": "פעילה", "currency": "ILS"}],
        "payments": [{"payment_id": "PAY_p1", "revision": 1, "person_id": "PER_c1",
                      "work_id": "WOR_w1", "path": "לימוד", "date": "2026-10-01",
                      "actual_amount_ils": 1000, "currency": "ILS", "kind": "התקבל"}],
        "time": [], "tasks": [],
        "expenses": [{"expense_id": "EXP_e1", "revision": 1, "date": "2026-10-01",
                      "category": "Ads", "platform": "Meta", "path": "לימוד",
                      "amount_ils": 5, "currency": "ILS", "payment_status": "שולם",
                      "source_type": "תשלום בפועל", "source_key": "k"}],
    }


def test_dependencies_capture_the_same_records_as_the_prototype():
    state = _state()
    mutations = [
        {"entity": "payments", "record_id": "PAY_r1", "after": {
            "payment_id": "PAY_r1", "person_id": "PER_c1", "work_id": "WOR_w1",
            "original_payment_id": "PAY_p1", "kind": "החזר"}},
        {"entity": "expenses", "record_id": "EXP_e2", "after": {
            "expense_id": "EXP_e2", "source_type": "תשלום בפועל", "source_key": "k"}},
        {"entity": "leads", "record_id": "LEA_b2", "after": {
            "lead_id": "LEA_b2", "phone": "+972501234567"}},
        {"entity": "tasks", "record_id": "TAS_x", "after": {
            "task_id": "TAS_x", "person_id": "PER_missing", "work_id": "WOR_w1"}},
    ]
    assert ids.dependencies(state, mutations) == proto_ledger._dependencies(state, mutations)


def test_the_envelope_is_the_prototype_field_set():
    expires = datetime(2026, 10, 16, 12, 10, tzinfo=UTC)
    row = api.ProposalRow(
        "ws", "PRO_" + "0" * 32, "00000000-0000-4000-8000-000000000001", "d" * 64, "",
        "123", 1, "00000000-0000-4000-8000-000000000002",
        "00000000-0000-4000-8000-000000000003", "tasks", "TAS_x", 0, (), "e" * 64, "OS", None,
        "proposal", "pending", expires - timedelta(minutes=10), expires)
    assert ids.envelope(row) == proto_ledger._envelope(row)
