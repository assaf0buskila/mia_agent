"""V2-20 gate: Mia's v1 (Sheets) consumers stay byte-identical (V2_PG_CONTRACTS.md section 20).

``follow_up_check`` and ``receipt_check`` learned the v2 shape in V2-20. For every v1 input they
must return exactly the bytes they returned before. The digests below were taken from the code at
master 1eb231e, before any V2-20 change, over the canonical JSON the owner tools hand to the model.
A digest that moves means a v1 reader changed, which V2-20 is not allowed to do.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest
from app.domain.owner.assaf_os_follow_up import follow_up_check, receipt_check
from app.services.assaf_os import RECORD_ENTITIES

NOW = datetime(2026, 10, 13, 10, tzinfo=UTC)
REQUEST = "12345678-1234-4234-8234-123456789012"


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_of(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def snapshot():
    return {
        "source": "google_sheets",
        "mode": "TEST",
        "workspace_id": "workspace-test",
        "workbook_id": "workbook_12345678901234567890",
        "observed_at": "2026-10-13T10:00:00Z",
        "coverage": "complete",
        "entity_coverage": dict.fromkeys(RECORD_ENTITIES, "complete"),
        "records": {entity: [] for entity in RECORD_ENTITIES},
        "issues": [],
    }


def task(status="לביצוע", number=123, **changes):
    row = {"task_id": f"T_{number}", "title": f"Task {number}", "status": status, "revision": 2}
    row.update(changes)
    return row


def lead(number=0, **changes):
    row = {
        "lead_id": f"L_{number:03}",
        "name": f"Lead {number}",
        "status": "חדש",
        "revision": 1,
        "phone": "PRIVATE_PHONE",
        "source_note": "UNTRUSTED_NOTE",
    }
    row.update(changes)
    return row


def v1_receipt(status="committed", **changes):
    value = {
        "source": "google_sheets",
        "mode": "TEST",
        "workspace_id": "workspace-test",
        "workbook_id": "workbook_12345678901234567890",
        "actor_source": "MIA",
        "request_id": REQUEST,
        "status": status,
        "proposal_id": "PR_123",
        "entity": "tasks",
        "record_id": "T_123",
        "revision": 3,
        "committed_at": "2026-10-13T09:59:00Z",
    }
    value.update(changes)
    return value


def _empty():
    return snapshot()


def _seven_leads():
    view = snapshot()
    view["records"]["leads"] = [lead(n) for n in range(7)]
    return view


def _task_states():
    view = snapshot()
    view["records"]["tasks"] = [
        task("לביצוע", 1), task("בתהליך", 2), task("ממתין", 3), task("בוצע", 4), task("בוטל", 5),
    ]
    view["records"]["leads"] = [lead(0), lead(1, status="לא ענה")]
    return view


def _invalid_task():
    view = snapshot()
    view["records"]["tasks"] = [task(task_id="bad id")]
    return view


def _partial_tasks():
    view = snapshot()
    view["entity_coverage"]["tasks"] = "partial"
    view["records"]["leads"] = [lead(0)]
    return view


def _issue_on_tasks():
    view = snapshot()
    view["records"]["tasks"] = [task()]
    view["issues"] = [{"entity": "tasks", "code": "native_conflict"}]
    return view


def _unrelated_partial_finance():
    view = snapshot()
    view["coverage"] = "partial"
    view["entity_coverage"]["payments"] = "unknown"
    view["issues"] = [{"entity": "payments", "code": "missing_date"}]
    view["records"]["tasks"] = [task()]
    return view


def _duplicate_ids():
    view = snapshot()
    view["records"]["tasks"] = [task(), task("בוצע")]
    return view


FOLLOW_UP_CASES = {
    "empty": _empty,
    "seven_leads": _seven_leads,
    "task_states": _task_states,
    "invalid_task": _invalid_task,
    "partial_tasks": _partial_tasks,
    "issue_on_tasks": _issue_on_tasks,
    "unrelated_partial_finance": _unrelated_partial_finance,
    "duplicate_ids": _duplicate_ids,
}

RECEIPT_CASES = {
    "committed": lambda: v1_receipt(),
    "rejected": lambda: v1_receipt("rejected"),
    "expired": lambda: v1_receipt("expired"),
    "target_changed": lambda: v1_receipt("target_changed"),
    "unknown": lambda: v1_receipt("unknown"),
    "other_request": lambda: v1_receipt(request_id="different-original-request"),
    "no_binding": lambda: v1_receipt(mode=None),
    "bad_revision": lambda: v1_receipt(revision=0),
    "future_commit": lambda: v1_receipt(committed_at="2099-01-01T00:00:00Z"),
    "live_mode_os_actor": lambda: v1_receipt(mode="LIVE", actor_source="OS"),
    "promote_lead": lambda: v1_receipt(entity="promote_lead", record_id="L_000"),
}

# sha256 of the canonical JSON, produced by the pre-V2-20 code.
FOLLOW_UP_DIGESTS: dict[str, str] = {
    "duplicate_ids": "490379af5a2ca49c0ae83d15a60ed0afb9ef8047d450e8086bc7c4cc4e0dbbc5",
    "empty": "bcac4479ff4973bffa849a1109e94646610aae32dd63cea78fb82c6088f645c8",
    "invalid_task": "dbefb821a5332a1c8ee582f6c6a55e0239434b8af7d2a52c365cbd23624edebd",
    "issue_on_tasks": "dbefb821a5332a1c8ee582f6c6a55e0239434b8af7d2a52c365cbd23624edebd",
    "partial_tasks": "978b25bfd7ab270c694aaf4f8d56910afe067dab0885594bebab2830e2d9bcac",
    "seven_leads": "a0efe8c76689153b351d98fa525a8f2ac53e2286c17294c8c54ac3b11770f8fa",
    "task_states": "0b1b5ea9b829ab2af4cda6b8603866df5bad9d475dc85795de803658d8590110",
    "unrelated_partial_finance": "1472c5690f8620bc5b1c8de6f6f78e512916b03115e75f6ed934fd641d880e6b",
}
RECEIPT_DIGESTS: dict[str, str] = {
    "bad_revision": "283f1edca2e66807f18be0bb209df2c80ac546dd0ddd95e393817e5548320248",
    "committed": "fb31f93adbc6380ed12c21a0501b12f69c40fe3305e456fee6cb6f328787869b",
    "expired": "51a37144be1f9a2116257f93f457d372990700bec5fdae6d74c7e35353a872a8",
    "future_commit": "283f1edca2e66807f18be0bb209df2c80ac546dd0ddd95e393817e5548320248",
    "live_mode_os_actor": "c23e1fb9c17bd10a7e4154069930973024dd66e727125ba3d979eea51c681469",
    "no_binding": "2ac53bb2e0da8f5961daaa630dacba07dedf871f91627e41faf9acdd5887e3b8",
    "other_request": "625d92f0b4047e0a517ed996d625d1cab05b9e84c8eb0e13bfb8e78dc4dbc9ee",
    "promote_lead": "e0ae28dc7b7a200e930b0c6cc348fe3b86fed4501de788026f3f34f303a39d74",
    "rejected": "fd0fe201ea2c211db8eb0d4b3e5f9cb4a5694e9a5b42d7c3dc3fee1ade2df610",
    "target_changed": "b23db76034e1629e05d1d872c544297c773d13e657346f2df87362c201f30540",
    "unknown": "d48323145c3896af7f2a69d488b8da5fcd323845be3910db54ad0c4e4127113e",
}


@pytest.mark.parametrize("name", sorted(FOLLOW_UP_CASES))
def test_follow_up_check_of_a_v1_snapshot_is_byte_identical(name):
    assert digest_of(follow_up_check(FOLLOW_UP_CASES[name]())) == FOLLOW_UP_DIGESTS[name]


@pytest.mark.parametrize("name", sorted(RECEIPT_CASES))
def test_receipt_check_of_a_v1_receipt_is_byte_identical(name):
    check = receipt_check(RECEIPT_CASES[name](), REQUEST, now=NOW)
    assert digest_of(check) == RECEIPT_DIGESTS[name]


def test_every_case_has_a_pinned_digest():
    assert set(FOLLOW_UP_DIGESTS) == set(FOLLOW_UP_CASES)
    assert set(RECEIPT_DIGESTS) == set(RECEIPT_CASES)
