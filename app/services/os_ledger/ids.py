"""Identifiers, request digests and the dependency/envelope helpers of the PG ledger (V2-16).

``PREFIXES``, ``DATE_KEYS``, ``dependencies`` and ``envelope`` are verbatim copies of the
prototype ledger's ``PREFIXES``, ``DATE_KEYS``, ``_dependencies``/``_lookup`` and ``_envelope``
(``app/prototypes/assaf_os_ledger.py``, V2-13's oracle, never edited). A ported test pins each copy
to the prototype. Nothing here touches a database.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, replace
from datetime import UTC
from typing import Any
from uuid import uuid4 as _uuid4
from uuid import uuid5

from app.services.os_ledger import api
from app.services.os_ledger.domain import ENTITIES, ID_KEYS, lead_contact_dependencies

PREFIXES = dict(zip(ENTITIES, ("LEA", "PER", "WOR", "PAY", "TIM", "EXP", "TAS")))
DATE_KEYS = {
    "works": "start_date",
    "payments": "date",
    "time": "date",
    "expenses": "date",
    "tasks": "due_date",
}
FUSED_KINDS = frozenset({"owner_direct", "owner_undo", "import"})
TERMINAL = ("committed", "rejected", "expired", "target_changed", "superseded", "abandoned")
# The canonical lowercase hyphenated UUID; every uuid column is compared only in this form.
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def default_uuid4() -> str:
    return str(_uuid4())


def is_uuid(value: Any) -> bool:
    return isinstance(value, str) and UUID_RE.fullmatch(value) is not None


def hex_id(prefix: str, uuid4: Callable[[], str]) -> str:
    """``<PREFIX>_<32 hex>`` from a server-minted UUIDv4 (records, persons, proposals)."""
    return prefix + "_" + uuid4().replace("-", "")


def fused_rejection_id(request_id: str) -> str:
    """§5.3: a fused kind's rejection id, which can never be used."""
    return str(uuid5(api.REQUEST_NS, request_id + ":reject"))


def proposal_request_digest(ctx: api.TrustedOwnerContext, entity: str, record_id: str | None,
                            revision: int, values: Mapping[str, Any],
                            outer_expiry: str | None) -> str:
    """§5.3 ``proposal`` digest: the prototype field set, bound to the channel as source."""
    return api.digest({
        "owner": str(ctx.numeric_owner), "workspace": ctx.workspace,
        "epoch": ctx.authority_epoch, "source": ctx.channel, "entity": entity,
        "record_id": record_id, "revision": revision, "input": dict(values),
        "outer_expiry": outer_expiry,
    })


def mutation_json(mutation: api.Mutation) -> dict[str, Any]:
    """One stored ``os_proposals.mutations`` element (the dataclass fields, as plain JSON)."""
    return {
        "entity": mutation.entity, "record_id": mutation.record_id, "op": mutation.op,
        "before": None if mutation.before is None else dict(mutation.before),
        "after": dict(mutation.after),
    }


# ---- verbatim prototype copies (app/prototypes/assaf_os_ledger.py) ---------------------------


# The prototype's ``_envelope``: the exact proposal field set, ``expires_at`` in ISO form.
def envelope(row: Any) -> dict[str, Any]:
    return {
        key: getattr(row, key)
        for key in (
            "workspace_id",
            "proposal_id",
            "request_id",
            "request_digest",
            "owner_id",
            "epoch",
            "approval_id",
            "rejection_id",
            "entity",
            "record_id",
            "expected_revision",
            "mutations",
            "dependency_digest",
            "source",
        )
    } | {"expires_at": row.expires_at.isoformat()}


def envelope_digest(row: api.ProposalRow) -> str:
    """The FakeLedger's envelope digest: the prototype field set with plain-JSON mutations.

    ``expires_at`` is rendered in UTC, so the digest of a row read back from PostgreSQL (in any
    session time zone) equals the digest computed at insert.
    """
    value = envelope(replace(row, expires_at=row.expires_at.astimezone(UTC)))
    value["mutations"] = [asdict(m) for m in row.mutations]
    return api.digest(value)


def _lookup(records, entity, record_id):
    return next((r for r in records[entity] if r[ID_KEYS[entity]] == record_id), None)


def dependencies(records: Mapping[str, Sequence[Mapping[str, Any]]],
                 mutations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Capture target and exact source/dependency records, including absent matches."""
    result = []
    for change in mutations:
        entity, key, candidate = change["entity"], change["record_id"], change["after"]
        part = {"entity": entity, "target": _lookup(records, entity, key)}
        if candidate.get("person_id"):
            part["client"] = _lookup(records, "clients", candidate["person_id"])
        if candidate.get("work_id") and entity != "works":
            part["work"] = _lookup(records, "works", candidate["work_id"])
        if entity == "leads":
            part["contacts"] = lead_contact_dependencies(candidate, records["leads"], key)
        if entity == "payments":
            original = candidate.get("original_payment_id")
            part["payments"] = [
                r
                for r in records["payments"]
                if r["payment_id"] in {key, original}
                or r.get("original_payment_id") in {key, original} - {None}
            ]
        if entity == "expenses":
            part["sources"] = [
                r
                for r in records["expenses"]
                if (r["source_type"], r["source_key"])
                == (candidate["source_type"], candidate["source_key"])
            ]
        result.append(part)
    return result


def dependency_digest(records: Mapping[str, Sequence[Mapping[str, Any]]],
                      mutations: Sequence[api.Mutation]) -> str:
    return api.digest(dependencies(records, [asdict(m) for m in mutations]))
