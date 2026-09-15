"""Exact, durable approval envelopes for v2 owner external writes.

Provider-specific code supplies validation and execution callbacks. This module owns the
invariants common to every write: authenticated owner, immutable parameters and target
snapshot, one proposal id, 24-hour expiry, exact callback binding, and an uncertain-outcome
claim that cannot be blindly retried.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.capabilities.policy import authorize
from app.capabilities.types import Principal
from app.core.errors import PermissionDenied
from app.db.models import ApprovalRow
from app.domain.approvals import (
    APPROVAL_TTL,
    DECISION_APPROVED,
    DECISION_PENDING,
    DECISION_REJECTED,
    new_approval_id,
)

ACTION_OWNER_EXTERNAL_WRITE = "owner_external_write"
RESOURCE_OWNER_PROPOSAL = "owner_proposal"
MAX_OWNER_ACTION_BYTES = 16 * 1024


@dataclass(frozen=True)
class OwnerActionProposal:
    approval_id: str
    proposal_id: str
    expires_at: str
    created: bool


@dataclass(frozen=True)
class OwnerActionDecision:
    status: str
    proposal_id: str = ""


@dataclass(frozen=True)
class OwnerActionExecution:
    status: str
    text: str = ""


class OwnerActionTargetChanged(RuntimeError):
    """The approved target changed after the final snapshot check."""


def typed_composio_binding(settings, toolkit: str, *, port: object | None = None) -> dict[str, Any]:
    """Return the exact account and active connection bound to a typed write."""
    toolkit = toolkit.strip().upper()
    account = settings.composio_user_id.strip()
    fake_reader = getattr(port, "approval_connected_account_id", None)
    if callable(fake_reader):
        connection_id = str(fake_reader() or "").strip()
        if not connection_id:
            raise RuntimeError("typed provider connection is unavailable")
        return {
            "toolkit": toolkit,
            "account_hash": hashlib.sha256(account.encode()).hexdigest(),
            "connection": {"connected_account_id": connection_id, "toolkit": toolkit},
        }
    from app.integrations.composio_catalog import ComposioCatalog

    catalog = ComposioCatalog.from_settings(settings)
    if catalog is None:
        raise RuntimeError("typed provider connection is unavailable")
    with catalog:
        connection = catalog.active_connection_snapshot(toolkit)
    if connection is None:
        raise RuntimeError("typed provider connection is unavailable")
    return {
        "toolkit": toolkit,
        "account_hash": hashlib.sha256(account.encode()).hexdigest(),
        "connection": connection.__dict__,
    }


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(envelope_json: str) -> str:
    return hashlib.sha256(envelope_json.encode("utf-8")).hexdigest()


def _calendar_booking_key(proposal_id: str) -> str:
    """Map an immutable owner proposal id to the booking adapter's idempotency key."""
    return "mia_" + hashlib.sha256(proposal_id.encode("utf-8")).hexdigest()


def _aware(moment: datetime | None) -> datetime:
    value = moment or datetime.now(UTC)
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _valid_owner(principal: Principal) -> bool:
    if principal.source != "telegram" or not principal.actor_id.strip().isdigit():
        return False
    try:
        authorize("leads.get_recent", principal=principal)
    except PermissionDenied:
        return False
    return True


def propose_owner_action(
    store,
    *,
    principal: Principal,
    source_ref: str,
    kind: str,
    parameters: Mapping[str, Any],
    target: Mapping[str, Any],
    risk: str = "R3",
    now: datetime | None = None,
) -> OwnerActionProposal:
    """Persist one exact proposal. Replaying the same owner event is idempotent."""
    if not _valid_owner(principal):
        raise PermissionError("current numeric Telegram owner identity is required")
    if not source_ref.strip() or not kind.strip() or risk not in {"R3", "R4"}:
        raise ValueError("source_ref, kind and an allowed risk are required")
    envelope = {
        "kind": kind.strip(),
        "parameters": dict(parameters),
        "target": dict(target),
        "version": 1,
    }
    encoded = _canonical(envelope)
    if len(encoded.encode("utf-8")) > MAX_OWNER_ACTION_BYTES:
        raise ValueError("proposal is too large to bind safely")
    # source_ref makes two concurrent turns distinct while keeping a retried webhook
    # bound to the original row.
    proposal_id = (
        "op_"
        + hashlib.sha256(f"{principal.actor_id}\n{source_ref}\n{encoded}".encode()).hexdigest()[:40]
    )
    existing = store.get_approval_by_resource(
        RESOURCE_OWNER_PROPOSAL, proposal_id, ACTION_OWNER_EXTERNAL_WRITE
    )
    if existing is not None:
        return OwnerActionProposal(
            approval_id=existing.approval_id,
            proposal_id=proposal_id,
            expires_at=existing.expires_at,
            created=False,
        )
    moment = _aware(now)
    expires_at = (moment + APPROVAL_TTL).isoformat()
    row = ApprovalRow(
        lead_id=None,
        channel="telegram",
        action=ACTION_OWNER_EXTERNAL_WRITE,
        risk=risk,
        payload_hash=_digest(encoded),
        decision=DECISION_PENDING,
        approver="",
        resource_type=RESOURCE_OWNER_PROPOSAL,
        resource_id=proposal_id,
        expires_at=expires_at,
        approval_id=new_approval_id(),
        proposed_parameters=encoded,
        actor_id=principal.actor_id[:32],
    )
    try:
        with store.session.begin_nested():
            store.session.add(row)
            store.session.flush()
    except IntegrityError:
        existing = store.get_approval_by_resource(
            RESOURCE_OWNER_PROPOSAL, proposal_id, ACTION_OWNER_EXTERNAL_WRITE
        )
        if existing is None:
            raise
        return OwnerActionProposal(existing.approval_id, proposal_id, existing.expires_at, False)
    return OwnerActionProposal(row.approval_id, proposal_id, expires_at, True)


def read_owner_action(row) -> dict[str, Any] | None:
    if (
        row is None
        or row.action != ACTION_OWNER_EXTERNAL_WRITE
        or row.resource_type != RESOURCE_OWNER_PROPOSAL
        or not row.resource_id.startswith("op_")
    ):
        return None
    try:
        envelope = json.loads(row.proposed_parameters)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(envelope, dict) or set(envelope) != {
        "kind",
        "parameters",
        "target",
        "version",
    }:
        return None
    if (
        envelope.get("version") != 1
        or not isinstance(envelope.get("kind"), str)
        or not isinstance(envelope.get("parameters"), dict)
        or not isinstance(envelope.get("target"), dict)
        or len(row.proposed_parameters.encode("utf-8")) > MAX_OWNER_ACTION_BYTES
        or row.payload_hash != _digest(_canonical(envelope))
    ):
        return None
    return envelope


def decide_owner_action(
    store,
    *,
    principal: Principal,
    approval_id: str,
    decision: str,
    now: datetime | None = None,
) -> OwnerActionDecision:
    if not _valid_owner(principal):
        return OwnerActionDecision("unauthorized")
    row = store.get_approval_by_approval_id(approval_id)
    envelope = read_owner_action(row)
    if row is None or envelope is None or row.actor_id != principal.actor_id:
        return OwnerActionDecision("unbound")
    if decision not in {DECISION_APPROVED, DECISION_REJECTED}:
        return OwnerActionDecision("invalid")
    if row.decision != DECISION_PENDING:
        return OwnerActionDecision("already_decided", row.resource_id)
    moment = _aware(now)
    try:
        expiry = datetime.fromisoformat(row.expires_at.replace("Z", "+00:00"))
    except ValueError:
        return OwnerActionDecision("expired", row.resource_id)
    if moment >= _aware(expiry):
        return OwnerActionDecision("expired", row.resource_id)
    values = {"decision": decision, "approver": principal.actor_id}
    if decision == DECISION_APPROVED:
        values["approved_at"] = moment.isoformat()
    changed = store.session.execute(
        update(ApprovalRow)
        .where(
            ApprovalRow.id == row.id,
            ApprovalRow.decision == DECISION_PENDING,
            ApprovalRow.payload_hash == row.payload_hash,
        )
        .values(**values)
        .returning(ApprovalRow.id)
    )
    if changed.scalar_one_or_none() is None:
        store.session.expire(row)
        return OwnerActionDecision("already_decided", row.resource_id)
    store.session.flush()
    return OwnerActionDecision("decided", row.resource_id)


def execute_owner_action(
    store,
    *,
    principal: Principal,
    proposal_id: str,
    validate_schema: Callable[[str, Mapping[str, Any]], bool],
    validate_policy: Callable[[str, Mapping[str, Any]], bool],
    connection_ready: Callable[[str], bool],
    current_target: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
    execute: Callable[[str, Mapping[str, Any]], str],
    now: datetime | None = None,
) -> OwnerActionExecution:
    """Revalidate an approved envelope, then execute at most once.

    Once execution begins, exceptions are treated as uncertain and the claim remains
    pending review. A retry therefore cannot repeat an effect that may have landed.
    """
    if not _valid_owner(principal):
        return OwnerActionExecution("unauthorized")
    row = store.get_approval_by_resource(
        RESOURCE_OWNER_PROPOSAL, proposal_id, ACTION_OWNER_EXTERNAL_WRITE
    )
    envelope = read_owner_action(row)
    if row is None or envelope is None or row.actor_id != principal.actor_id:
        return OwnerActionExecution("unbound")
    if row.decision != DECISION_APPROVED:
        return OwnerActionExecution("not_approved")
    if str(row.executed_at or "").strip():
        return OwnerActionExecution("already_handled")
    moment = _aware(now)
    try:
        expiry = datetime.fromisoformat(row.expires_at.replace("Z", "+00:00"))
    except ValueError:
        return OwnerActionExecution("expired")
    if moment >= _aware(expiry):
        return OwnerActionExecution("expired")
    kind = envelope["kind"]
    parameters = envelope["parameters"]
    target = envelope["target"]
    if not validate_schema(kind, parameters):
        return OwnerActionExecution("schema_changed")
    if not validate_policy(kind, parameters):
        return OwnerActionExecution("policy_denied")
    if not connection_ready(kind):
        return OwnerActionExecution("not_connected")
    if _canonical(current_target(kind, parameters)) != _canonical(target):
        return OwnerActionExecution("target_changed")
    operation_key = f"{proposal_id}:execute"
    if not store.claim_provider_write(scope="approval", key=operation_key):
        return OwnerActionExecution("already_handled")
    try:
        text = execute(kind, parameters)
    except OwnerActionTargetChanged:
        store.fail_operation(scope="approval", key=operation_key)
        return OwnerActionExecution("target_changed")
    except Exception:  # provider may have applied the write before transport failed
        store.mark_provider_write_pending_review(scope="approval", key=operation_key)
        return OwnerActionExecution("unknown")
    if not store.complete_provider_write(
        scope="approval", key=operation_key, result_json='{"ok":true}'
    ):
        return OwnerActionExecution("unknown")
    row.executed_at = moment.isoformat()
    row.execution_operation_id = operation_key[:64]
    row.result = str(text)[:255]
    store.session.flush()
    return OwnerActionExecution("executed", str(text))


def execute_approved_owner_action_with_adapters(
    store,
    *,
    settings,
    principal: Principal,
    proposal_id: str,
    crm_pre_synced: bool = False,
) -> OwnerActionExecution:
    """Execute one built-in v2 owner action through its typed adapter."""
    if principal.actor_id not in settings.telegram_owner_user_id_set():
        return OwnerActionExecution("unauthorized")
    from app.capabilities.policy import authorize, execute_capability
    from app.capabilities.sheets import sheets_handlers, validate_sheets_write_args
    from app.core.errors import InvalidArguments, PermissionDenied
    from app.core.risk import RiskAction, RiskLevel, assert_allowed
    from app.core.write_flags import write_flag_enabled
    from app.domain.owner.composio_effects import (
        EffectRoute,
        composio_effect,
        snapshot_effect_target,
    )
    from app.domain.owner.composio_writes import (
        _generic_write_denial,
        generic_composio_effect_supported,
    )
    from app.integrations.calendar import (
        ComposioCalendarPort,
        DisabledCalendarPort,
        build_calendar_agenda_port,
        window_free_excluding_self,
    )
    from app.integrations.calendar_booking import (
        ComposioCalendarBookingPort,
        DisabledCalendarBookingPort,
    )
    from app.integrations.composio_catalog import (
        ComposioCatalog,
        risk_for_slug,
        validate_arguments,
    )
    from app.integrations.gmail import ComposioGmailPort, DisabledGmailPort
    from app.integrations.sheets import ComposioSheetsPort, DisabledSheetsPort, build_sheets_port
    from app.services.crm_v2 import CrmError, CrmRevisionConflict, CrmService

    row = store.get_approval_by_resource(
        RESOURCE_OWNER_PROPOSAL, proposal_id, ACTION_OWNER_EXTERNAL_WRITE
    )
    envelope = read_owner_action(row)
    if envelope is None:
        return OwnerActionExecution("unbound")

    prepared: dict[str, Any] = {}

    def validate_schema(kind: str, parameters: Mapping[str, Any]) -> bool:
        if kind in {"sheets.update", "sheets.append"}:
            try:
                spreadsheet_id, a1_range, values = validate_sheets_write_args(
                    dict(parameters),
                    allowed_spreadsheet_ids=settings.allowed_sheets_spreadsheet_ids(),
                )
            except (InvalidArguments, TypeError, ValueError):
                return False
            prepared.update(spreadsheet_id=spreadsheet_id, a1_range=a1_range, values=values)
            return True
        if kind == "gmail.create_draft":
            to = parameters.get("to")
            subject = parameters.get("subject")
            body = parameters.get("body")
            return (
                isinstance(to, str)
                and bool(to.strip())
                and isinstance(subject, str)
                and isinstance(body, str)
                and bool(subject.strip() or body.strip())
            )
        if kind == "crm.upsert":
            fields = parameters.get("fields")
            contact_id = parameters.get("contact_id")
            expected_revision = parameters.get("expected_revision")
            return (
                isinstance(fields, dict)
                and isinstance(contact_id, str)
                and isinstance(expected_revision, int)
                and expected_revision >= 0
            )
        if kind == "crm.activity":
            return all(
                isinstance(parameters.get(key), str) and bool(parameters[key].strip())
                for key in ("contact_id", "kind", "summary")
            )
        if kind == "crm.resolve_conflict":
            return (
                all(
                    isinstance(parameters.get(key), str) and bool(str(parameters[key]).strip())
                    for key in ("conflict_id", "resolution", "contact_id")
                )
                and parameters.get("resolution") in {"database", "sheet", "value"}
                and isinstance(parameters.get("expected_revision"), int)
                and (
                    parameters.get("resolution") != "value"
                    or isinstance(parameters.get("value"), str)
                )
            )
        if kind in {"calendar.create", "calendar.reschedule"}:
            try:
                start = datetime.fromisoformat(str(parameters.get("start") or ""))
                end = datetime.fromisoformat(str(parameters.get("end") or ""))
            except ValueError:
                return False
            valid = (
                bool(str(parameters.get("title") or "").strip())
                if kind == "calendar.create"
                else bool(str(parameters.get("event_id") or "").strip())
            )
            return valid and start.tzinfo is not None and end.tzinfo is not None and start < end
        if kind == "composio.write":
            return (
                isinstance(parameters.get("slug"), str)
                and isinstance(parameters.get("toolkit"), str)
                and isinstance(parameters.get("arguments"), dict)
            )
        return False

    def validate_policy(kind: str, _parameters: Mapping[str, Any]) -> bool:
        try:
            if kind in {"sheets.update", "sheets.append"}:
                authorize(kind, principal=principal, kill_switch=settings.kill_switch)
            elif kind == "gmail.create_draft":
                authorize(
                    "mail.create_draft",
                    principal=principal,
                    kill_switch=settings.kill_switch,
                )
            elif kind in {"crm.upsert", "crm.activity", "crm.resolve_conflict"}:
                if settings.kill_switch:
                    return False
            elif kind in {"calendar.create", "calendar.reschedule"}:
                assert_allowed(
                    RiskAction(name="calendar_create", risk=RiskLevel.R3_COMMERCIAL),
                    kill_switch=settings.kill_switch,
                )
                if not write_flag_enabled(settings, "calendar_write"):
                    return False
            elif kind == "composio.write":
                if not generic_composio_effect_supported(str(_parameters.get("slug") or "")):
                    return False
                assert_allowed(
                    RiskAction(name="composio_write", risk=RiskLevel(row.risk)),
                    kill_switch=settings.kill_switch,
                )
            else:
                return False
        except (PermissionDenied, KeyError):
            return False
        return True

    ports: dict[str, Any] = {}

    def approved_typed_binding(kind: str) -> tuple[str, str] | None:
        toolkit = {
            "gmail.create_draft": "GMAIL",
            "sheets.update": "GOOGLESHEETS",
            "sheets.append": "GOOGLESHEETS",
            "calendar.create": "GOOGLECALENDAR",
            "calendar.reschedule": "GOOGLECALENDAR",
        }.get(kind)
        approved = envelope["target"].get("provider_binding")
        if toolkit is None or not isinstance(approved, dict):
            return None
        try:
            fresh = typed_composio_binding(settings, toolkit)
        except RuntimeError:
            return None
        if _canonical(fresh) != _canonical(approved):
            return None
        connection = approved.get("connection")
        if not isinstance(connection, dict):
            return None
        connection_id = str(connection.get("connected_account_id") or "").strip()
        return (toolkit, connection_id) if connection_id else None

    def connection_ready(kind: str) -> bool:
        if kind in {"sheets.update", "sheets.append"}:
            binding = approved_typed_binding(kind)
            if binding is None:
                return False
            port = ComposioSheetsPort(
                api_key=settings.composio_api_key.strip(),
                user_id=settings.composio_user_id.strip(),
                spreadsheet_id=settings.resolved_sheets_spreadsheet_id(),
                allowed_spreadsheet_ids=settings.allowed_sheets_spreadsheet_ids(),
                connected_account_id=binding[1],
            )
            ports["sheets"] = port
            return not isinstance(port, DisabledSheetsPort)
        if kind == "gmail.create_draft":
            binding = approved_typed_binding(kind)
            if binding is None:
                return False
            port = ComposioGmailPort(
                api_key=settings.composio_api_key.strip(),
                user_id=settings.composio_user_id.strip(),
                connected_account_id=binding[1],
            )
            ports["gmail"] = port
            return not isinstance(port, DisabledGmailPort)
        if kind in {"crm.upsert", "crm.activity", "crm.resolve_conflict"}:
            sheets = build_sheets_port(settings)
            if not all(
                callable(getattr(sheets, name, None))
                for name in ("ensure_crm_workspace", "read_crm_contacts_chunk")
            ):
                return False
            if not crm_pre_synced:
                try:
                    sync_owner_crm_sheet_in_session(store, sheets=sheets)
                except Exception:
                    return False
            ports["crm"] = CrmService(store.session)
            return True
        if kind in {"calendar.create", "calendar.reschedule"}:
            binding = approved_typed_binding(kind)
            if binding is None:
                return False
            calendar = ComposioCalendarPort(
                api_key=settings.composio_api_key.strip(),
                user_id=settings.composio_user_id.strip(),
                connected_account_id=binding[1],
            )
            booking = ComposioCalendarBookingPort(
                api_key=settings.composio_api_key.strip(),
                user_id=settings.composio_user_id.strip(),
                connected_account_id=binding[1],
            )
            ports["calendar"] = calendar
            ports["booking"] = booking
            # Only used by the calendar.reschedule self-conflict re-check below;
            # harmless (and unused) for calendar.create. Bound to the same
            # approved connection as calendar/booking above -- otherwise the
            # re-check could read a different (e.g. switched) calendar account
            # than the one the owner actually approved.
            ports["agenda"] = build_calendar_agenda_port(settings, connected_account_id=binding[1])
            return not isinstance(calendar, DisabledCalendarPort) and not isinstance(
                booking, DisabledCalendarBookingPort
            )
        if kind == "composio.write":
            catalog = ComposioCatalog.from_settings(settings)
            if catalog is None:
                return False
            ports["composio"] = catalog.__enter__()
            return True
        return False

    def current_target(kind: str, parameters: Mapping[str, Any]) -> Mapping[str, Any]:
        if kind in {"sheets.update", "sheets.append"}:
            port = ports["sheets"]
            return {
                "spreadsheet_id": prepared["spreadsheet_id"],
                "range": prepared["a1_range"],
                "values": port.read_values(
                    spreadsheet_id=prepared["spreadsheet_id"],
                    a1_range=prepared["a1_range"],
                ),
                "provider_binding": envelope["target"]["provider_binding"],
            }
        if kind == "gmail.create_draft":
            return {
                "recipient": str(parameters["to"]).strip(),
                "provider_binding": envelope["target"]["provider_binding"],
            }
        if kind == "crm.upsert":
            snapshot = ports["crm"].snapshot_identity(parameters["fields"])
            return {
                "contact_id": snapshot.contact_id,
                "revision": snapshot.revision,
                "snapshot_hash": snapshot.snapshot_hash,
                "fields": snapshot.fields,
            }
        if kind == "crm.activity":
            snapshot = ports["crm"].snapshot_target(str(parameters["contact_id"]))
            return {
                "contact_id": snapshot.contact_id,
                "revision": snapshot.revision,
                "snapshot_hash": snapshot.snapshot_hash,
                "fields": snapshot.fields,
            }
        if kind == "crm.resolve_conflict":
            conflict_id = str(parameters["conflict_id"])
            conflict = next(
                (item for item in ports["crm"].list_conflicts() if item.id == conflict_id),
                None,
            )
            if conflict is None:
                return {}
            snapshot = ports["crm"].snapshot_target(str(parameters["contact_id"]))
            return {"conflict": conflict.__dict__, "contact": snapshot.__dict__}
        if kind == "calendar.create":
            start = datetime.fromisoformat(str(parameters["start"]))
            end = datetime.fromisoformat(str(parameters["end"]))
            duration = max(1, int((end - start).total_seconds() // 60))
            slots = ports["calendar"].find_free_slots(
                time_min=start,
                time_max=end,
                duration_minutes=duration,
                timezone=str(parameters["timezone"]),
            )
            free = any(slot.start <= start and slot.end >= end for slot in slots)
            return {
                "free": free,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "provider_binding": envelope["target"]["provider_binding"],
            }
        if kind == "calendar.reschedule":
            current = ports["booking"].get_event(
                event_id=str(parameters["event_id"]),
                timezone=str(parameters["timezone"]),
            )
            event = current.event
            if event is None:
                return {}
            start = datetime.fromisoformat(str(parameters["start"]))
            end = datetime.fromisoformat(str(parameters["end"]))
            destination_free = window_free_excluding_self(
                ports["calendar"],
                window_start=start,
                window_end=end,
                self_start=event.start,
                self_end=event.end,
                self_event_id=event.event_id,
                agenda=ports.get("agenda"),
                timezone=str(parameters["timezone"]),
            )
            return {
                "event_id": event.event_id,
                "start": event.start.isoformat() if event.start else "",
                "end": event.end.isoformat() if event.end else "",
                "destination_free": destination_free,
                "provider_binding": envelope["target"]["provider_binding"],
            }
        if kind == "composio.write":
            catalog = ports["composio"]
            tool = catalog.detail(str(parameters["slug"]))
            connection = catalog.active_connection_snapshot(str(parameters["toolkit"]))
            if tool is None or connection is None:
                return {}
            risk = risk_for_slug(tool.slug, tool.toolkit)
            denial = _generic_write_denial(tool.slug, tool.toolkit, risk)
            if tool.toolkit == "LINKEDIN" and risk is not RiskLevel.R5_DESTRUCTIVE:
                words = frozenset(tool.slug.split("_"))
                denial = "" if not words & {"MESSAGE", "DM", "INMAIL"} else denial
            if denial or validate_arguments(tool.input_schema, parameters["arguments"]):
                return {}
            effect = composio_effect(tool.slug, tool.toolkit)
            if effect.route not in {
                EffectRoute.GENERIC_CREATE,
                EffectRoute.SNAPSHOT_WRITE,
            }:
                return {}
            target = {
                "slug": tool.slug,
                "toolkit": tool.toolkit,
                "input_schema": tool.input_schema,
                "risk": risk.value,
                "account_hash": hashlib.sha256(settings.composio_user_id.encode()).hexdigest(),
                "connection": connection.__dict__,
            }
            if "effect_route" in envelope["target"]:
                target["effect_route"] = effect.route.value
            if effect.route is EffectRoute.SNAPSHOT_WRITE:
                resource = snapshot_effect_target(
                    catalog,
                    effect,
                    parameters["arguments"],
                    connected_account_id=connection.connected_account_id,
                )
                if resource is None:
                    return {}
                target["resource"] = resource
            return target
        return {}

    def run(kind: str, parameters: Mapping[str, Any]) -> str:
        if kind in {"sheets.update", "sheets.append"}:
            out = execute_capability(
                kind,
                principal=principal,
                args={
                    "spreadsheet_id": prepared["spreadsheet_id"],
                    "range": prepared["a1_range"],
                    "values": prepared["values"],
                },
                handlers=sheets_handlers(
                    ports["sheets"],
                    allowed_spreadsheet_ids=settings.allowed_sheets_spreadsheet_ids(),
                ),
                kill_switch=settings.kill_switch,
            )
            count = int(out.get("updated" if kind == "sheets.update" else "appended") or 0)
            return f"{count} Sheet row(s) written."
        if kind == "gmail.create_draft":
            draft = ports["gmail"].create_draft(
                to=str(parameters["to"]),
                subject=str(parameters["subject"]),
                body=str(parameters["body"]),
            )
            if draft is None or not draft.draft_id.strip():
                raise RuntimeError("Gmail did not confirm the draft")
            return f"Gmail draft created: {draft.draft_id}"
        if kind == "crm.upsert":
            try:
                result = ports["crm"].capture(
                    parameters["fields"],
                    source_ref=f"approval:{proposal_id}",
                    contact_id=parameters["contact_id"] or None,
                    expected_revision=parameters["expected_revision"],
                )
            except CrmRevisionConflict as exc:
                raise OwnerActionTargetChanged(str(exc)) from exc
            except CrmError as exc:
                raise RuntimeError(str(exc)) from exc
            if result.contact is None or result.status == "conflict":
                raise RuntimeError("CRM did not confirm the write")
            return f"CRM contact {result.status}: {result.contact.id}"
        if kind == "crm.activity":
            activity = ports["crm"].record_activity(
                str(parameters["contact_id"]),
                source_ref=f"approval:{proposal_id}",
                kind=str(parameters["kind"]),
                summary=str(parameters["summary"]),
            )
            return f"CRM activity recorded: {activity.id}"
        if kind == "crm.resolve_conflict":
            try:
                contact = ports["crm"].resolve_conflict(
                    str(parameters["conflict_id"]),
                    resolution=str(parameters["resolution"]),
                    value=(
                        str(parameters["value"])
                        if isinstance(parameters.get("value"), str)
                        else None
                    ),
                    expected_contact_revision=int(parameters["expected_revision"]),
                )
            except CrmRevisionConflict as exc:
                raise OwnerActionTargetChanged(str(exc)) from exc
            except CrmError as exc:
                raise RuntimeError(str(exc)) from exc
            return f"CRM conflict resolved for contact: {contact.id}"
        if kind == "calendar.create":
            event = ports["booking"].create_event(
                booking_key=_calendar_booking_key(proposal_id),
                start=datetime.fromisoformat(str(parameters["start"])),
                end=datetime.fromisoformat(str(parameters["end"])),
                timezone=str(parameters["timezone"]),
                summary=str(parameters["title"]),
                location=str(parameters.get("location") or ""),
                allow_nonstandard_duration=True,
            )
            if event is None:
                raise RuntimeError("calendar did not confirm event creation")
            return f"Calendar event created: {event.event_id}"
        if kind == "calendar.reschedule":
            event = ports["booking"].patch_event(
                event_id=str(parameters["event_id"]),
                start=datetime.fromisoformat(str(parameters["start"])),
                end=datetime.fromisoformat(str(parameters["end"])),
                timezone=str(parameters["timezone"]),
                allow_nonstandard_duration=True,
            )
            if event is None:
                raise RuntimeError("calendar did not confirm event reschedule")
            return f"Calendar event rescheduled: {event.event_id}"
        if kind == "composio.write":
            catalog = ports["composio"]
            tool = catalog.detail(str(parameters["slug"]))
            if tool is None:
                raise RuntimeError("Composio tool is no longer active")
            connection = catalog.active_connection_snapshot(tool.toolkit)
            approved_connection = envelope["target"].get("connection")
            if connection is None or not isinstance(approved_connection, dict):
                raise OwnerActionTargetChanged("Composio connection changed")
            if _canonical(connection.__dict__) != _canonical(approved_connection):
                raise OwnerActionTargetChanged("Composio connection changed")
            approved_connection_id = str(
                approved_connection.get("connected_account_id") or ""
            ).strip()
            if not approved_connection_id:
                raise OwnerActionTargetChanged("Composio connection changed")
            response = catalog.execute(
                tool,
                parameters["arguments"],
                connected_account_id=approved_connection_id,
            )
            if response is None:
                raise RuntimeError("Composio did not confirm the action")
            return "Composio action completed."
        raise PermissionError("unsupported owner action")

    try:
        return execute_owner_action(
            store,
            principal=principal,
            proposal_id=proposal_id,
            validate_schema=validate_schema,
            validate_policy=validate_policy,
            connection_ready=connection_ready,
            current_target=current_target,
            execute=run,
        )
    finally:
        catalog = ports.get("composio")
        if catalog is not None:
            catalog.__exit__(None, None, None)


def sync_owner_crm_sheet_in_session(store, *, sheets: object) -> int:
    """Merge current Contacts rows through the caller's open transaction.

    Approval callbacks must not open a second writer while the callback session holds
    an uncommitted decision.  Keeping the import, decision, and execution claim in one
    session also makes the target recheck observe exactly the rows that were imported.
    """
    required = ("ensure_crm_workspace", "read_crm_contacts_chunk")
    if not all(callable(getattr(sheets, name, None)) for name in required):
        raise RuntimeError("CRM sheet import is unavailable")

    from app.services.crm_v2 import CrmService

    sheets.ensure_crm_workspace()
    service = CrmService(store.session)
    seen_ids: set[str] = set()
    row_count = 0
    start_row = 2
    while start_row <= 10_001:
        chunk = sheets.read_crm_contacts_chunk(start_row=start_row, limit=100)
        if not isinstance(chunk, list):
            raise RuntimeError("CRM sheet import returned an invalid chunk")
        for offset, cells in enumerate(chunk):
            row_count += 1
            if any(str(value or "").strip() for value in cells):
                imported = service.import_sheet_contact(cells, row_number=start_row + offset)
                if imported.contact is not None:
                    seen_ids.add(imported.contact.id)
        if len(chunk) < 100:
            break
        start_row += 100
    service.note_missing_sheet_contacts(seen_ids)
    store.session.flush()
    return row_count
