"""Fixed synthetic fixtures; no file loader, source export or provider access."""

from __future__ import annotations

from uuid import uuid4

from app.prototypes.assaf_os_domain import ENTITIES, ENUMS, ID_KEYS, SCHEMA, canonical_input
from app.prototypes.assaf_os_primary import _digest


def synthetic_records() -> dict[str, list[dict]]:
    """Twelve invented rows, every entity, two currencies, one bounded refund."""
    path = ENUMS["path"][0]
    client_a, client_b, work_a, work_b = (
        "legacy_person_A",
        "legacy_person_B",
        "legacy_work_A",
        "legacy_work_B",
    )
    refs = {"person_id": client_a, "work_id": work_a, "path": path}
    return {
        "leads": [
            {
                "lead_id": "legacy_lead_A",
                "person_id": client_a,
                "revision": 3,
                "name": "Synthetic client A",
                "phone": "0520000001",
                "email": None,
                "path": path,
                "status": ENUMS["lead_status"][-1],
                "source_note": "Synthetic fixture",
            }
        ],
        "clients": [
            {
                "person_id": client_a,
                "revision": 4,
                "name": "Synthetic client A",
                "status": ENUMS["client_status"][0],
                "phone": "0520000001",
                "email": None,
            },
            {
                "person_id": client_b,
                "revision": 2,
                "name": "Synthetic client B",
                "status": ENUMS["client_status"][0],
            },
        ],
        "works": [
            {
                "work_id": work_a,
                "revision": 5,
                "person_id": client_a,
                "title": "Synthetic ILS work",
                "path": path,
                "status": ENUMS["work_status"][1],
                "currency": "ILS",
                "agreed_amount_ils": 99999,
                "start_date": "2026-10-01",
            },
            {
                "work_id": work_b,
                "revision": 1,
                "person_id": client_b,
                "title": "Synthetic EUR work",
                "path": path,
                "status": ENUMS["work_status"][1],
                "currency": "EUR",
            },
        ],
        "payments": [
            refs
            | {
                "payment_id": "legacy_payment_A",
                "revision": 2,
                "date": "2026-10-02",
                "actual_amount_ils": 12345,
                "currency": "ILS",
                "kind": ENUMS["payment_kind"][0],
            },
            refs
            | {
                "payment_id": "legacy_refund_A",
                "revision": 1,
                "date": "2026-10-03",
                "actual_amount_ils": 2345,
                "currency": "ILS",
                "kind": ENUMS["payment_kind"][1],
                "original_payment_id": "legacy_payment_A",
            },
            {
                "payment_id": "legacy_payment_B",
                "revision": 1,
                "person_id": client_b,
                "work_id": work_b,
                "path": path,
                "date": "2026-10-02",
                "actual_amount_ils": 5000,
                "currency": "EUR",
                "kind": ENUMS["payment_kind"][0],
            },
        ],
        "time": [
            refs
            | {
                "time_id": "legacy_time_A",
                "revision": 2,
                "date": "2026-10-02",
                "minutes": 90,
                "status": ENUMS["time_status"][0],
                "description": "Synthetic time",
            }
        ],
        "expenses": [
            {
                "expense_id": "legacy_expense_A",
                "revision": 2,
                "date": "2026-10-02",
                "category": "Synthetic expense",
                "platform": "Meta",
                "path": path,
                "amount_ils": 1500,
                "currency": "ILS",
                "payment_status": ENUMS["payment_status"][0],
                "source_type": ENUMS["expense_source"][0],
                "source_key": "synthetic:invoice-A",
            },
            {
                "expense_id": "legacy_estimate_A",
                "revision": 1,
                "date": "2026-10-02",
                "category": "Synthetic estimate",
                "platform": "Google Ads",
                "path": path,
                "amount_ils": 99900,
                "currency": "ILS",
                "payment_status": ENUMS["payment_status"][0],
                "source_type": ENUMS["expense_source"][1],
                "source_key": "synthetic:estimate-A",
            },
        ],
        "tasks": [
            refs
            | {
                "task_id": "legacy_task_A",
                "revision": 6,
                "title": "Synthetic follow-up",
                "status": ENUMS["task_status"][0],
                "due_date": "2026-10-14",
                "notes": None,
            }
        ],
    }


def record_inputs(entity: str, record: dict) -> dict:
    return canonical_input(
        entity,
        {
            key: value
            for key, value in record.items()
            if key not in {ID_KEYS[entity], "revision"}
            and not (entity == "leads" and key == "person_id")
        },
    )


def make_synthetic_batch(
    workspace_id: str,
    authority_epoch: int = 1,
    *,
    batch_id: str | None = None,
) -> dict:
    records = synthetic_records()
    provenance = {
        entity: {
            row[ID_KEYS[entity]]: {
                "source_input_digest": _digest(record_inputs(entity, row)),
                "accepted_input_digest": _digest(record_inputs(entity, row)),
                "accepted_revision": row["revision"],
                "acceptance_kind": "synthetic_native_acceptance",
                "source_snapshot_version": 1,
            }
            for row in records[entity]
        }
        for entity in ENTITIES
    }
    return {
        "schema_version": SCHEMA.get("version", "assaf-os.schema.v1"),
        "source_id": "synthetic:seven-entity-v1",
        "source_epoch": 1,
        "workspace_id": workspace_id,
        "authority_epoch": authority_epoch,
        "batch_id": batch_id or str(uuid4()),
        "observed_at": "2026-10-14T01:00:00+00:00",
        "coverage": dict.fromkeys(ENTITIES, "complete"),
        "records": records,
        "provenance": provenance,
    }
