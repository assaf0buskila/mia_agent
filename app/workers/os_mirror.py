"""Sheets mirror publisher (§14, V2-29): the ledger, projected to a read-only mirror workbook.

Default-off. ``start_if_enabled`` starts one daemon thread per process, and only when
``assaf_os_mirror_enabled`` is set and the target is the configured mirror (never the OS workbook
or a CRM workbook). Each tick (``run_once``):

1. pauses on the kill switch, the freeze unless core authority is active, a disabled mirror or
   a changed target, before any outbox, reader or Sheets call;
2. claims outbox rows under a lease (one short transaction);
3. loads one consistent snapshot through the reader (each read its own short transaction);
4. projects every tab (headers are the schema labels; the summary tab from
   ``os_summary_monthly``);
5. writes every tab RAW, then reads every tab back, with no transaction open (I13);
6. equal: rows ``sent`` and ``verified_version`` advances by the GREATEST rule; an error after the
   send: ``unknown``, read back before any rewrite; a mismatch: ``conflict``.

Anti-entropy runs every ``os_mirror_anti_entropy_seconds`` even with an empty queue, and
``RECOVERY_RETRY`` after an ``unknown`` or ``delayed`` tick. On a synced mirror it only reads:
drift sets ``conflict`` (``drift``) and is never imported, and a failed read leaves it synced.
``conflict`` clears only through a full rewrite whose readback is equal. Logs carry reason codes,
counts and timings only (I7).
"""

from __future__ import annotations

import importlib
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Any, NamedTuple

from sqlalchemy.exc import SQLAlchemyError

from app.integrations.sheets_mirror import (
    MirrorTransport,
    MirrorTransportError,
    target_refusal,
)
from app.services.os_ledger import api, outbox
from app.services.os_ledger import mirror_projection as mp
from app.services.os_ledger.outbox import (
    MirrorState,
    OutboxError,
    OutboxEvent,
    OutboxStore,
    PgOutbox,
    destination_binding,
)

_LOG = logging.getLogger("mia.os_mirror")
TICK_SECONDS = 5.0
CLAIM_LIMIT = 100
DEPS_RETRY = timedelta(seconds=60)
# An unknown or delayed mirror is retried after this floor, not left until anti-entropy.
RECOVERY_RETRY = timedelta(seconds=60)
_PUBLISHING = frozenset({"shadow", "active"})
_BODY_KEYS = frozenset({"authority_epoch", "refs", "full"})
_REF_KEYS = frozenset({"entity", "record_id", "revision"})
_STORE_ERRORS = (OutboxError, SQLAlchemyError, OSError)


class TickResult(NamedTuple):
    # paused | idle | verified | unknown | delayed | conflict | deferred | refused
    outcome: str
    reason: str
    events: int = 0


@dataclass(frozen=True)
class MirrorDeps:
    reader: Any  # api.LedgerReader: head, page, monthly_summary
    context: Callable[[], Any]  # a fresh WORKER context per tick (V2-19 for_worker("mirror"))
    outbox: OutboxStore
    transport: MirrorTransport
    destination_binding: str


def _utcnow() -> datetime:
    return datetime.now(UTC)


def pause_reason(
    settings: Any, binding: str | None = None, *,
    authority_reader: Callable[[Any], api.AuthorityState | None] | None = None,
) -> str | None:
    """Why to pause, before outbox, reader or Sheets calls; frozen reads fail closed (E35)."""
    if getattr(settings, "kill_switch", True):
        return "kill_switch"
    if getattr(settings, "assaf_os_mirror_enabled", False) is not True:
        return "disabled"
    target = str(getattr(settings, "os_mirror_spreadsheet_id", "") or "")
    if binding is not None and binding != destination_binding(target):
        return "mirror_target_changed"
    if getattr(settings, "assaf_os_freeze", True):
        try:
            if (authority_reader or api.core_authority_state)(settings) != "active":
                return "os_frozen"
        except Exception:  # fail closed; never log authority values or exception payloads (I7)
            return "os_frozen"
    return None


def _event_refusal(event: OutboxEvent, epoch: int) -> str | None:
    """``{authority_epoch, refs:[{entity, record_id, revision}]}`` or ``{.., refs:[], full:true}``.

    A ref to anything but the seven record entities (e.g. statement lines) is refused, so no
    personal line can ever be projected (I11). Another epoch's row is refused (I12)."""
    body = event.body
    if not isinstance(body, Mapping) or not {"authority_epoch", "refs"} <= set(body) <= _BODY_KEYS:
        return "outbox_body_invalid"
    body_epoch, refs, full = body["authority_epoch"], body["refs"], body.get("full", False)
    if (
        isinstance(body_epoch, bool)
        or not isinstance(body_epoch, int)
        or not isinstance(refs, list)
        or not isinstance(full, bool)
        or (full and refs)
    ):
        return "outbox_body_invalid"
    for ref in refs:
        if (
            not isinstance(ref, Mapping)
            or set(ref) != _REF_KEYS
            or ref["entity"] not in api.ENTITIES
            or not isinstance(ref["record_id"], str)
            or isinstance(ref["revision"], bool)
            or not isinstance(ref["revision"], int)
            or ref["revision"] < 1
        ):
            return "outbox_body_invalid"
    if body_epoch != epoch:
        return "authority_changed"
    return None


# ---------------------------------------------------------------------------------------------
# One tick


def _finish(deps: MirrorDeps, event: OutboxEvent, status: str, code: str | None, now: datetime):
    retry_at = now + outbox.retry_delay(status, event.attempts)
    if not deps.outbox.finish(event, event.lease_generation, status, code, retry_at):
        _LOG.info("os_mirror op=finish outcome=lost_lease reason=generation_changed")


def _finish_all(deps: MirrorDeps, events, status: str, code: str | None, now: datetime) -> None:
    for event in events:
        _finish(deps, event, status, code, now)


def _verified(deps: MirrorDeps, events, version: int, now: datetime, readback) -> TickResult:
    deps.outbox.record_result(
        status="synced", version=version, verified=True, error_code=None, now=now,
        coverage=mp.coverage_of(readback),
    )
    _finish_all(deps, events, "sent", None, now)
    return TickResult("verified", "readback_equal", len(events))


def _transport_failed(
    deps: MirrorDeps, events, state: MirrorState, version: int, now: datetime, code: str,
    *, sent: bool,
) -> TickResult:
    """A write that may have landed is ``unknown`` and is read back before any rewrite. A
    failure before the send is ``delayed``. A conflict is never erased by a transport error."""
    if state.status == "conflict":
        mirror_status = "conflict"
    elif sent or state.status == "unknown":
        mirror_status = "unknown"
    else:
        mirror_status = "delayed"
    deps.outbox.record_result(
        status=mirror_status, version=version, verified=False, error_code=code, now=now,
        coverage=None,
    )
    _finish_all(deps, events, "unknown" if sent else "pending", code, now)
    return TickResult("unknown" if sent else "delayed", code, len(events))


def _publish(
    deps: MirrorDeps, state: MirrorState, events: list[OutboxEvent], snapshot: mp.Snapshot,
    *, anti_entropy: bool, now: datetime,
) -> TickResult:
    try:
        projection = mp.project(snapshot)
    except mp.ProjectionError as exc:
        _finish_all(deps, events, "pending", exc.code, now)
        return TickResult("deferred", exc.code, len(events))
    version = projection.observed_version
    in_conflict = state.status == "conflict"
    # A synced mirror at this exact version, with nothing to deliver: read only, never write.
    drift_check = (
        anti_entropy and not events and state.status == "synced"
        and state.verified_version == version
    )
    # A write that may have landed (an unknown ack, or a lease taken over from a crashed worker)
    # is read back before any rewrite. Conflict never takes this shortcut: only a full rewrite
    # with an equal readback clears it.
    read_first = not in_conflict and not drift_check and (
        state.status == "unknown" or any(event.attempts > 1 for event in events)
    )
    coverage: Mapping[str, Any] = state.scan_coverage
    if drift_check or read_first:
        try:
            before = deps.transport.batch_get(mp.read_ranges())
        except MirrorTransportError as exc:
            if drift_check:
                # Nothing was written and nothing was learned: the mirror stays synced.
                return TickResult("delayed", exc.code, 0)
            return _transport_failed(deps, events, state, version, now, exc.code, sent=False)
        if mp.readback_equal(projection, before):
            return _verified(deps, events, version, now, before)
        if drift_check:
            deps.outbox.record_result(
                status="conflict", version=version, verified=False, error_code="drift", now=now,
                coverage=mp.coverage_of(before),
            )
            return TickResult("conflict", "drift", 0)
        coverage = mp.coverage_of(before)

    deps.outbox.record_attempt(version, now)
    try:
        deps.transport.batch_update(mp.write_ranges(projection, coverage))
    except MirrorTransportError as exc:
        return _transport_failed(deps, events, state, version, now, exc.code, sent=exc.sent)
    try:
        after = deps.transport.batch_get(mp.read_ranges())
    except MirrorTransportError:
        return _transport_failed(deps, events, state, version, now, "readback_failed", sent=True)
    if mp.readback_equal(projection, after):
        return _verified(deps, events, version, now, after)
    deps.outbox.record_result(
        status="conflict", version=version, verified=False, error_code="readback_mismatch",
        now=now, coverage=mp.coverage_of(after),
    )
    _finish_all(deps, events, "conflict", "readback_mismatch", now)
    return TickResult("conflict", "readback_mismatch", len(events))


def _tick(
    deps: MirrorDeps, settings: Any, *, now: datetime, lease_owner: str, anti_entropy: bool,
    claim_limit: int,
    authority_reader: Callable[[Any], api.AuthorityState | None] | None,
) -> TickResult:
    pause = pause_reason(settings, deps.destination_binding, authority_reader=authority_reader)
    if pause is not None:
        return TickResult("paused", pause)
    try:
        state = deps.outbox.load_state(deps.destination_binding, now)
        events = deps.outbox.claim(claim_limit, lease_owner, now)
    except OutboxError as exc:
        return TickResult("refused", exc.code)
    except _STORE_ERRORS:
        return TickResult("deferred", "outbox_unavailable")
    if not events and not anti_entropy:
        return TickResult("idle", "queue_empty")
    try:
        snapshot = mp.load_snapshot(deps.reader, deps.context())
    except (api.LedgerError, mp.ProjectionError) as exc:
        _finish_all(deps, events, "pending", exc.code, now)
        return TickResult("deferred", exc.code, len(events))
    except _STORE_ERRORS:
        _finish_all(deps, events, "pending", "reader_unavailable", now)
        return TickResult("deferred", "reader_unavailable", len(events))
    core = snapshot.states.get("core")
    publishing = {"active"} if getattr(settings, "assaf_os_freeze", True) else _PUBLISHING
    if core not in publishing:
        _finish_all(deps, events, "pending", "authority_not_publishing", now)
        if core == "fenced":
            deps.outbox.record_result(
                status="fenced", version=snapshot.observed_version, verified=False,
                error_code="authority_fenced", now=now, coverage=None,
            )
            return TickResult("paused", "authority_fenced", len(events))
        return TickResult("paused", "authority_not_publishing", len(events))
    deliver = []
    for event in events:
        refusal = _event_refusal(event, snapshot.authority_epoch)
        if refusal is None:
            deliver.append(event)
        else:
            _finish(deps, event, "conflict", refusal, now)
    if not deliver and not anti_entropy:
        return TickResult("idle", "events_refused", len(events))
    return _publish(deps, state, deliver, snapshot, anti_entropy=anti_entropy, now=now)


def run_once(
    deps: MirrorDeps,
    settings: Any,
    *,
    now: datetime,
    lease_owner: str,
    anti_entropy: bool,
    claim_limit: int = CLAIM_LIMIT,
    authority_reader: Callable[[Any], api.AuthorityState | None] | None = None,
) -> TickResult:
    """One publisher tick. Never raises a transport or store error; returns its outcome."""
    started = time.monotonic()
    try:
        result = _tick(
            deps, settings, now=now, lease_owner=lease_owner, anti_entropy=anti_entropy,
            claim_limit=claim_limit, authority_reader=authority_reader,
        )
    except _STORE_ERRORS:
        result = TickResult("deferred", "outbox_unavailable")
    if result.outcome not in {"paused", "idle"}:
        _LOG.info(
            "os_mirror op=verify outcome=%s reason=%s ms=%d events=%d anti_entropy=%s",
            result.outcome,
            result.reason,
            int((time.monotonic() - started) * 1000),
            result.events,
            anti_entropy,
        )
    return result


# ---------------------------------------------------------------------------------------------
# Wire state (§14 table; V2-20 renders it as ``mirror``)


def mirror_wire_state(
    state: MirrorState | None, *, enabled: bool, now: datetime, lag_warn_seconds: int
) -> dict[str, Any]:
    if not enabled or state is None or state.status == "fenced":
        return {
            "state": "disabled",
            "last_verified_at": (
                state.last_verified_at.isoformat() if state and state.last_verified_at else None
            ),
            "verified_version": state.verified_version if state else 0,
            "lag_s": None,
        }
    lag = None
    if state.last_verified_at is not None:
        lag = max(0, int((now - state.last_verified_at).total_seconds()))
    if state.status == "synced":
        wire, lag = "verified", 0
    elif state.status in {"pending", "delayed"}:
        wire = "lagging" if lag is None or lag > lag_warn_seconds else "verified"
    elif state.status == "conflict":
        wire = "conflict"
    else:
        wire = "unknown"
    return {
        "state": wire,
        "last_verified_at": state.last_verified_at.isoformat() if state.last_verified_at else None,
        "verified_version": state.verified_version,
        "lag_s": lag,
    }


# ---------------------------------------------------------------------------------------------
# Publisher thread


class MirrorPublisher:
    """Schedules ticks: deps are built lazily, pauses are logged once, anti-entropy is timed."""

    def __init__(
        self,
        *,
        settings_provider: Callable[[], Any],
        deps_factory: Callable[[Any], MirrorDeps],
        clock: Callable[[], datetime] | None = None,
        lease_owner: str | None = None,
        tick_seconds: float = TICK_SECONDS,
        authority_reader: Callable[[Any], api.AuthorityState | None] | None = None,
    ) -> None:
        self._settings_provider = settings_provider
        self._deps_factory = deps_factory
        self._authority_reader = authority_reader
        self._clock = clock or _utcnow
        self.lease_owner = lease_owner or f"mirror-{os.getpid()}-{uuid.uuid4().hex[:12]}"
        self._tick_seconds = tick_seconds
        self._deps: MirrorDeps | None = None
        self._deps_retry_at: datetime | None = None
        self._last_anti_entropy: datetime | None = None
        self._recover_at: datetime | None = None
        self._last_pause: str | None = None

    def __repr__(self) -> str:
        return "MirrorPublisher()"

    def tick(self) -> TickResult:
        settings = self._settings_provider()
        now = self._clock()
        # Both pause checks share one fresh, lazy read per tick, outside transport calls.
        authority_state = cache(
            lambda: (self._authority_reader or api.core_authority_state)(settings)
        )
        pause = pause_reason(
            settings, self._deps.destination_binding if self._deps is not None else None,
            authority_reader=lambda _: authority_state(),
        )
        if pause is not None:
            if pause == "mirror_target_changed":
                self._deps = None
            if pause != self._last_pause:
                _LOG.info("os_mirror op=tick outcome=paused reason=%s", pause)
                self._last_pause = pause
            return TickResult("paused", pause)
        self._last_pause = None
        if self._deps is None:
            if self._deps_retry_at is not None and now < self._deps_retry_at:
                return TickResult("deferred", "deps_backoff")
            try:
                self._deps = self._deps_factory(settings)
            except (api.LedgerError, MirrorTransportError, OutboxError) as exc:
                self._deps_retry_at = now + DEPS_RETRY
                _LOG.warning("os_mirror op=start outcome=deferred reason=%s", exc.code)
                return TickResult("deferred", exc.code)
            except Exception:  # noqa: BLE001 - e.g. a missing driver; a code, never the error
                self._deps_retry_at = now + DEPS_RETRY
                _LOG.warning("os_mirror op=start outcome=deferred reason=deps_failed")
                return TickResult("deferred", "deps_failed")
        interval = timedelta(seconds=int(getattr(settings, "os_mirror_anti_entropy_seconds", 900)))
        due = (
            self._last_anti_entropy is None
            or now - self._last_anti_entropy >= interval
            or (self._recover_at is not None and now >= self._recover_at)
        )
        result = run_once(self._deps, settings, now=now, lease_owner=self.lease_owner,
                          anti_entropy=due, authority_reader=lambda _: authority_state())
        if result.reason == "mirror_target_changed":
            self._deps = None  # rebuilt on the next tick; the pinned binding then refuses
        if due and result.outcome not in {"paused", "deferred", "refused"}:
            self._last_anti_entropy = now
        if result.outcome in {"unknown", "delayed"}:
            # Read back (or rewrite) on a full pass soon, even with an empty queue. The floor
            # keeps a 429 from becoming a write every tick.
            self._recover_at = now + RECOVERY_RETRY
        elif result.outcome in {"verified", "conflict"}:
            self._recover_at = None
        return result

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception:  # never let the thread die; the reason is a code, not the error
                _LOG.warning("os_mirror op=tick outcome=error reason=unexpected_error")
            stop.wait(self._tick_seconds)


def _worker_context(settings: Any) -> Any:
    """V2-19's ``authorize.for_worker("mirror", settings)``; refused until it exists."""
    try:
        for_worker = importlib.import_module("app.services.os_ledger.authorize").for_worker
    except (ImportError, AttributeError):
        raise api.LedgerError("ledger_unavailable", reason="authorize_missing") from None
    return for_worker("mirror", settings)


def build_deps(settings: Any) -> MirrorDeps:
    """The production wiring: Sheets transport, V2-16 reader, PG outbox on the ledger DSN.

    The outbox shares the reader's engine: ``guard.engine_for`` pins psycopg3 (provisioning
    writes a bare ``postgresql://`` DSN), sets the connect timeout, and refuses a bad DSN as
    ``ledger_unavailable``."""
    from app.integrations.sheets_mirror import SheetsMirrorTransport
    from app.services.os_ledger.guard import engine_for

    spreadsheet_id = str(settings.os_mirror_spreadsheet_id or "")
    transport = SheetsMirrorTransport(
        settings.os_mirror_service_account_json, spreadsheet_id, settings=settings
    )
    dsn = str(getattr(settings, "owner_os_ledger_database_url", "") or "")
    if not dsn:
        raise api.LedgerError("ledger_unavailable", reason="dsn_missing")
    reader = api.build_ledger(settings)
    engine = engine_for(dsn)
    return MirrorDeps(
        reader=reader,
        context=lambda: _worker_context(settings),
        outbox=PgOutbox(engine, settings.assaf_os_workspace_id),
        transport=transport,
        destination_binding=destination_binding(spreadsheet_id),
    )


# ---------------------------------------------------------------------------------------------
# Start (lifespan; one thread per process)

_STARTED = False
_START_LOCK = threading.Lock()


def _config_refusal(settings: Any) -> str | None:
    spreadsheet_id = str(getattr(settings, "os_mirror_spreadsheet_id", "") or "").strip()
    if not spreadsheet_id:
        return "mirror_spreadsheet_missing"
    target = target_refusal(spreadsheet_id, settings)
    if target is not None:
        return target
    if not str(getattr(settings, "os_mirror_service_account_json", "") or "").strip():
        return "mirror_credentials_missing"
    if not str(getattr(settings, "owner_os_ledger_database_url", "") or "").strip():
        return "ledger_dsn_missing"
    if not str(getattr(settings, "assaf_os_workspace_id", "") or "").strip():
        return "workspace_missing"
    return None


def _spawn(publisher: MirrorPublisher) -> None:
    stop = threading.Event()
    thread = threading.Thread(
        target=publisher.run_forever, args=(stop,), name="os-mirror", daemon=True
    )
    thread.start()


def start_if_enabled(settings: Any) -> None:
    """Start the publisher when the mirror is enabled and configured; otherwise do nothing."""
    global _STARTED
    if getattr(settings, "assaf_os_mirror_enabled", False) is not True:
        return None
    refusal = _config_refusal(settings)
    if refusal is not None:
        _LOG.warning("os_mirror op=start outcome=refused reason=%s", refusal)
        return None
    with _START_LOCK:
        if _STARTED:
            return None
        _STARTED = True
    from app.core.config import get_settings

    _spawn(MirrorPublisher(settings_provider=get_settings, deps_factory=build_deps))
    _LOG.info("os_mirror op=start outcome=started reason=none")
    return None
