"""Small read projections; source evidence never becomes authority to act."""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from app.services.assaf_os import ID_PATTERN, RECORD_ENTITIES

FOLLOW_UP_LIMIT_PER_ENTITY = 5
FOLLOW_UP_MAX_CHARS = 16_000
_STATUS = {
    "leads": {"חדש", "רלוונטי", "לא רלוונטי", "לא ענה", "הפך ללקוח"},
    "tasks": {"לביצוע", "בתהליך", "ממתין", "בוצע", "בוטל"},
}
_OPEN = {"leads": {"חדש"}, "tasks": {"לביצוע", "בתהליך", "ממתין"}}


def unknown_check(scope: str, reason: str) -> dict[str, Any]:
    return {
        "scope": scope,
        "state": "unknown",
        "reason_codes": [reason],
        "next_action": "resolve_source_evidence_before_any_new_mutation",
    }


def follow_up_check(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Only new Sheet leads and open tasks, in source order; no priority inference."""
    result: dict[str, Any] = {
        "scope": "shared_sheet_new_leads_and_open_tasks",
        "source": {
            key: snapshot[key]
            for key in ("source", "mode", "workspace_id", "workbook_id", "observed_at")
        },
        "coverage": snapshot["coverage"],
        "entities": {},
        "items": [],
        "recommendations_are_actions_taken": False,
    }
    unknown = False
    for entity, id_key, label_key in (
        ("tasks", "task_id", "title"),
        ("leads", "lead_id", "name"),
    ):
        rows = snapshot["records"][entity]
        coverage = snapshot["entity_coverage"][entity]
        ids = Counter(row.get(id_key) for row in rows if isinstance(row.get(id_key), str))
        invalid = any(
            not isinstance(row.get(id_key), str)
            or not re.fullmatch(ID_PATTERN, row[id_key])
            or ids[row[id_key]] != 1
            or type(row.get("revision")) is not int
            or not 1 <= row["revision"] <= 1_000_000
            or not isinstance(row.get("status"), str)
            or row["status"] not in _STATUS[entity]
            or not isinstance(row.get(label_key), str)
            or not row[label_key].strip()
            for row in rows
        )
        issue = any(
            not isinstance(item, dict)
            or item.get("entity") not in RECORD_ENTITIES
            or item["entity"] == entity
            for item in snapshot["issues"]
        )
        reliable = coverage == "complete" and not invalid and not issue
        candidates = [row for row in rows if row.get("status") in _OPEN[entity]] if reliable else []
        shown = candidates[:FOLLOW_UP_LIMIT_PER_ENTITY]
        result["entities"][entity] = {
            "coverage": coverage,
            "state": "checked" if reliable else "unknown",
            "observed_row_count": len(rows),
            "follow_up_count": len(candidates) if reliable else None,
            "omitted_count": max(0, len(candidates) - len(shown)) if reliable else None,
            "reason_codes": [] if reliable else ["source_incomplete_or_invalid"],
        }
        unknown |= not reliable
        for row in shown:
            waiting = entity == "tasks" and row["status"] == "ממתין"
            result["items"].append(
                {
                    "entity": entity,
                    "record_id": row[id_key],
                    "revision": row["revision"],
                    "label": row[label_key][:120],
                    "recorded_status": row["status"],
                    "state": "blocked" if waiting else "open",
                    "recommendation": (
                        "ask_owner_what_is_needed_to_unblock_task"
                        if waiting
                        else "review_new_lead_with_owner"
                        if entity == "leads"
                        else "confirm_task_progress_with_owner"
                    ),
                }
            )
    result["state"] = (
        "unknown" if unknown else "open" if result["items"] else "no_recorded_follow_up"
    )
    result["next_action"] = (
        "resolve_source_evidence"
        if unknown
        else "review_a_listed_item_with_owner"
        if result["items"]
        else "ask_owner_about_unrecorded_work_if_needed"
    )
    return result


def receipt_check(
    receipt: dict[str, Any], request_id: str, *, now: datetime | None = None
) -> dict[str, Any]:
    """A committed receipt proves that historical write, never the business goal."""
    result = unknown_check("exact_historical_write_only", "receipt_not_found")
    result["source"] = {"kind": "assaf_os_signed_receipt", "request_id": request_id}
    binding_keys = ("source", "mode", "workspace_id", "workbook_id", "actor_source")
    binding_valid = all(
        isinstance(receipt.get(key), str) and 0 < len(receipt[key]) <= 256 for key in binding_keys
    )
    if binding_valid:
        result["source"].update({key: receipt[key] for key in binding_keys})
    status = receipt.get("status")
    result["next_action"] = "query_original_receipt_without_replaying_mutation"
    if receipt.get("request_id") != request_id:
        result["reason_codes"] = ["receipt_binding_mismatch"]
        return result
    if (
        not binding_valid
        or receipt["source"] != "google_sheets"
        or receipt["mode"] not in {"TEST", "LIVE"}
        or receipt["actor_source"] not in {"OS", "MIA"}
    ):
        result["reason_codes"] = ["receipt_binding_incomplete"]
        return result
    if not isinstance(status, str):
        result["reason_codes"] = ["receipt_evidence_incomplete"]
        return result
    if status in {"rejected", "expired", "target_changed"}:
        result.update(
            state="blocked",
            reason_codes=[status],
            next_action="review_with_owner_before_preparing_a_new_exact_proposal",
        )
    elif status == "committed":
        try:
            if (
                not isinstance(receipt.get("committed_at"), str)
                or len(receipt["committed_at"]) > 64
            ):
                raise ValueError
            committed_at = datetime.fromisoformat(receipt["committed_at"].replace("Z", "+00:00"))
            valid = (
                committed_at.tzinfo is not None
                and committed_at.timestamp() <= (now or datetime.now(UTC)).timestamp() + 300
                and receipt.get("entity") in {*RECORD_ENTITIES, "promote_lead"}
                and all(
                    isinstance(receipt.get(key), str) and re.fullmatch(ID_PATTERN, receipt[key])
                    for key in ("proposal_id", "record_id")
                )
                and type(receipt.get("revision")) is int
                and 1 <= receipt["revision"] <= 1_000_000
            )
        except (KeyError, TypeError, AttributeError, ValueError):
            valid = False
        if not valid:
            result["reason_codes"] = ["receipt_evidence_incomplete"]
            return result
        result.update(
            state="completed",
            reason_codes=["exact_write_committed"],
            next_action="read_current_record_if_checking_business_completion",
        )
        result["source"].update(
            {
                key: receipt[key]
                for key in ("proposal_id", "entity", "record_id", "revision", "committed_at")
            }
        )
    elif status != "unknown":
        result["reason_codes"] = ["receipt_evidence_incomplete"]
    return result
