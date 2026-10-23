"""``mia os-authority``: the authority fence and cutover CLI (V2_PG_CONTRACTS.md section 16, V2-31).

    mia os-authority init|preflight|freeze-plan|activate|fence|rollback|status --approved-sha <sha>

It runs on the privileged migration DSN (the ``AuthorityAdmin`` of section 5.4), behind the same
exact-SHA go as ``mia os-ledger``. It never touches Secret Manager, a Script Property, a workbook or
a bank file: it machine-checks the preconditions around those owner steps and prints the steps
(``freeze-plan``, ``rollback``). Every refusal is ``os_authority_refused:<reason>[,<reason>...]`` on
stderr with exit 1, and a JSON document on stdout. Nothing printed or logged carries a DSN, a
secret, a record or a driver message; log lines are ``os_authority op=<command>
outcome=<ok|refused> reason=<code>``.

Evidence lives in one VM-owned directory (``MIA_OS_AUTHORITY_DIR``, default
``/var/lib/mia/os-authority``). This job writes ``drain.json`` (``preflight --drain``),
``preflight.json`` (``preflight``, and ``preflight --drain`` once it has seen the Script live: that
sighting ends the run of fenced probes) and ``activation.json`` (``activate``). ``freeze-plan
--phase drain`` starts a freeze and so sets an earlier ``drain.json`` aside: a drain belongs to
one freeze. The operator places
``backup.json``, ``restore-drill.json``, ``fe-v2.json``, ``journal.json`` and ``manifest.json``
there (formats: ops/assaf-os/CUTOVER_RUNBOOK.md). The activation preconditions are the fixed table
in ``PRECONDITIONS``; ``activate`` re-evaluates every one of them and moves ``core`` from
``shadow`` to ``active`` only when all pass. ``activate`` never bumps the epoch (section 5.4).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib
import json
import logging
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from app.domain.statements.vocabulary import MIN_PEPPER_BYTES
from app.services.os_ledger import api

_LOG = logging.getLogger("mia.os_authority")

COMMANDS = ("init", "preflight", "freeze-plan", "activate", "fence", "rollback", "status")

# Section 16: every activation precondition, in table order. Each is a refusal reason code.
PRECONDITIONS = (
    "release_sha_mismatch",
    "sheets_not_fenced",
    "freeze_off",
    "capture_before_fence",
    "main_role_unsafe",
    "ledger_role_unsafe",
    "sheets_pending_proposals",
    "sheets_unknown_receipts",
    "approval_cards_open",
    "backup_too_old",
    "restore_drill_too_old",
    "mirror_not_verified",
    "manifest_not_signed",
    "fe_v2_reader_not_live",
    "journal_capacity",
)

BACKUP_MAX_AGE = timedelta(hours=1)
# T3 follows T2 immediately: a drain taken more than this long before the fence probe was taken
# under a freeze that may have lapsed in between (an aborted window, then OS-screen proposals).
DRAIN_MAX_AGE = timedelta(hours=2)
DRILL_MAX_AGE = timedelta(days=7)
JOURNAL_MAX_AGE = timedelta(days=7)
CLOCK_SKEW = timedelta(seconds=60)
# A journal tab above this share of ASSAF_OS_MAX_ROWS leaves no room for the drain and the final
# reconcile, so it counts as at the cap.
JOURNAL_HEADROOM = 0.9
# The Script's own row cap (UnifiedRuntime.gs ASSAF_OS_MAX_ROWS) and its five journal tabs
# (ASSAF_OS_META). The measure must name exactly these tabs and carry exactly this cap: a missing
# tab or a larger cap would let a full journal pass. tests/workers/test_os_authority_probes.py reads
# both back from the Script's source, the way FENCED_REPLY is pinned.
JOURNAL_MAX_ROWS = 2000
JOURNAL_TABS = (
    "_assaf_os_accepted", "_assaf_os_proposals", "_assaf_os_receipts", "_assaf_os_replay",
    "_assaf_os_issues",
)

PRIVILEGED_DSN_ENV = "MIA_OS_LEDGER_MIGRATION_DATABASE_URL"
MAIN_DSN_ENV = "MIA_DATABASE_URL"
EVIDENCE_DIR_ENV = "MIA_OS_AUTHORITY_DIR"
DEFAULT_EVIDENCE_DIR = "/var/lib/mia/os-authority"
MAX_EVIDENCE_BYTES = 1_048_576
PEPPER_LABEL = b"assaf-os-statement-pepper-fingerprint.v1"

DRAIN = "drain.json"
PREFLIGHT = "preflight.json"
ACTIVATION = "activation.json"
BACKUP = "backup.json"
DRILL = "restore-drill.json"
FRONTEND = "fe-v2.json"
JOURNAL = "journal.json"
MANIFEST = "manifest.json"

_SHA = re.compile(r"[0-9a-f]{40}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_WORKSPACE = re.compile(r"[A-Za-z0-9_-]{3,128}")
_OWNER = re.compile(r"[1-9][0-9]{0,19}")
_STAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|[+-][0-9]{2}:[0-9]{2})"
)
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{3,127}")
_CARD_KEY = re.compile(r"(op_[A-Za-z0-9_-]+):(execute|reject)")
_SCRIPT_PATH = re.compile(r"/macros/s/[A-Za-z0-9_-]{20,256}/exec")
# What a Script with no readable config (ASSAF_OS_MODE=FENCED, or any property missing) answers:
# UnifiedRuntime.gs doPost catches assafOsConfig_'s "unavailable" with request still null, so the
# identity is the fixed safe identity and the response is unsigned. Pinned in
# tests/assaf-os/apps-script-fence.test.js against the real source.
FENCED_REPLY = {
    "api_version": "assaf-os.v1",
    "operation": "invalid",
    "actor_source": "OS",
    "owner_id": "0",
    "workspace_id": "unavailable",
    "workbook_id": "unavailable",
    "request_id": "invalid-request",
    "timestamp": 0,
    "mode": "UNKNOWN",
    "ok": False,
    "data": {"error": {"code": "unavailable"}},
}
FENCE_PROBE_BUDGET_SECONDS = 20.0

# The ``mia-env`` key flips of the cutover, in order. The freeze stays ON from ``drain`` through
# ``switch`` and ``mirror`` and is lifted only by ``open``: the first non-import write is its
# own go.
Phase = Literal["drain", "switch", "mirror", "open", "revert"]
PHASES = ("drain", "switch", "mirror", "open", "revert")


class Refusal(Exception):
    """A fixed refusal: the codes are printed, nothing else is."""

    def __init__(self, *codes: str) -> None:
        super().__init__(",".join(codes))
        self.codes = list(codes)


class EvidenceError(Exception):
    """An evidence file that exists but cannot be used; the check that needs it fails closed."""


@dataclass(frozen=True)
class Check:
    code: str
    status: Literal["pass", "fail", "pending"]
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        document = {"code": self.code, "status": self.status}
        if self.detail:
            document["detail"] = self.detail
        return document


@dataclass(frozen=True)
class SignedManifest:
    digest: str
    captured_at: datetime
    signed_at: datetime


@dataclass
class Result:
    ok: bool
    document: dict[str, Any]
    codes: list[str] = field(default_factory=list)
    text: str | None = None


# ---------------------------------------------------------------------------------------------
# Time and evidence files


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _time(value: object) -> datetime | None:
    """Strict ISO-8601 with an offset; None for anything else (a naive time proves nothing)."""
    if type(value) is not str or not _STAMP.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


class EvidenceStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def read(self, name: str) -> dict[str, Any] | None:
        """The parsed document, None when absent, EvidenceError when present but unusable."""
        path = self.directory / name
        try:
            if path.is_symlink():
                raise EvidenceError(name)
            if not path.exists():
                return None
            if not path.is_file() or path.stat().st_size > MAX_EVIDENCE_BYTES:
                raise EvidenceError(name)
            document = json.loads(path.read_bytes().decode("utf-8"))
        except EvidenceError:
            raise
        except (OSError, ValueError):
            raise EvidenceError(name) from None
        if not isinstance(document, dict):
            raise EvidenceError(name)
        return document

    def write(self, name: str, document: Mapping[str, Any]) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".evidence-", dir=self.directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                    json.dump(document, stream, sort_keys=True, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, self.directory / name)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        except OSError:
            raise Refusal("evidence_unwritable") from None

    @staticmethod
    def aside_stamp(moment: datetime) -> str:
        return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")

    def set_aside(self, name: str, moment: datetime) -> bool:
        """Rename an evidence file out of the way, ``<stem>-superseded-<UTC stamp><suffix>``; it is
        never deleted. False when there was none."""
        path = self.directory / name
        try:
            if not (path.is_symlink() or path.exists()):
                return False
            os.replace(path, path.with_name(
                f"{path.stem}-superseded-{self.aside_stamp(moment)}{path.suffix}"))
        except OSError:
            raise Refusal("evidence_unwritable") from None
        return True


# ---------------------------------------------------------------------------------------------
# Probes: everything that reads the outside world. Tests supply a fake; production uses LiveProbes.


class Probes(Protocol):
    def sheets_fence(self) -> tuple[str, str]:
        """``("unavailable"|"live"|"unknown", detail)`` from a read-only Script ``schema`` call."""

    def sheets_snapshot_pending(self) -> int:
        """Pending Sheets proposals from the T2 snapshot (a disclosed mutating call)."""

    def main_role_safe(self) -> bool: ...

    def ledger_role_safe(self) -> bool: ...

    def open_cards(self) -> int: ...

    def unknown_receipts(self) -> int: ...

    def mirror(self, workspace: str) -> str:
        """``empty``, ``verified`` or ``unverified``."""

    def non_import_receipts(self, workspace: str, epoch: int) -> int: ...


def fence_state(
    settings: Any,
    owner_id: str,
    *,
    post: Callable[[str, bytes, float], bytes] | None = None,
) -> tuple[str, str]:
    """Read-only fence probe: one signed ``schema`` call through the existing Script transport.

    ``unavailable`` only for the exact unsigned reply a config-less Script gives (FENCED_REPLY).
    A validly signed reply means the Script loaded its config, so it is live. Anything else,
    including a transport failure, is ``unknown``, which the caller treats as not fenced.
    """
    from app.services import assaf_os

    url = getattr(settings, "assaf_os_script_url", "")
    target = urlsplit(url)
    secret = getattr(settings, "assaf_os_hmac_secret", "")
    if (
        not getattr(settings, "assaf_os_enabled", False)
        or target.scheme != "https"
        or target.netloc != "script.google.com"
        or target.query
        or target.fragment
        or target.username
        or target.password
        or target.port not in {None, 443}
        or not _SCRIPT_PATH.fullmatch(target.path)
        or not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", getattr(settings, "assaf_os_workbook_id", ""))
        or not _WORKSPACE.fullmatch(getattr(settings, "assaf_os_workspace_id", ""))
        or len(secret) < 32
        or not _OWNER.fullmatch(owner_id)
    ):
        return "unknown", "not_configured"
    request = {
        "api_version": assaf_os.API_VERSION,
        "operation": "schema",
        "actor_source": "MIA",
        "owner_id": owner_id,
        "workspace_id": settings.assaf_os_workspace_id,
        "workbook_id": settings.assaf_os_workbook_id,
        "request_id": str(uuid4()),
        "timestamp": int(time.time()),
        "data": {},
    }
    payload = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    body = json.dumps({"payload": payload, "signature": signature}, separators=(",", ":")).encode()
    sender = post if post is not None else assaf_os._post
    try:
        raw = sender(url, body, FENCE_PROBE_BUDGET_SECONDS)
        wrapper = json.loads(raw)
        if not isinstance(wrapper, dict) or set(wrapper) != {"payload", "signature"}:
            return "unknown", "reply_shape"
        reply_payload, reply_signature = wrapper["payload"], wrapper["signature"]
        if not isinstance(reply_payload, str) or not isinstance(reply_signature, str):
            return "unknown", "reply_shape"
        reply = json.loads(reply_payload)
    except Exception:  # transport, size, JSON: all unknown, none of it is ever printed
        return "unknown", "transport"
    if reply_signature == "":
        return ("unavailable", "") if reply == FENCED_REPLY else ("unknown", "reply_unexpected")
    expected = hmac.new(secret.encode(), reply_payload.encode(), hashlib.sha256).hexdigest()
    if _HEX64.fullmatch(reply_signature) and hmac.compare_digest(reply_signature, expected):
        return "live", ""
    return "unknown", "reply_signature"


def open_assaf_os_cards(session: Any, now: datetime) -> int:
    """``assaf_os.approve`` cards that are not terminal. A card is terminal once it has an executed
    stamp or its expiry has passed. An unreadable card or expiry counts as open (fail closed)."""
    from sqlalchemy import select

    from app.db.models import ApprovalRow
    from app.services.owner_actions import ACTION_OWNER_EXTERNAL_WRITE, RESOURCE_OWNER_PROPOSAL

    open_count = 0
    rows = session.scalars(select(ApprovalRow).where(
        ApprovalRow.action == ACTION_OWNER_EXTERNAL_WRITE,
        ApprovalRow.resource_type == RESOURCE_OWNER_PROPOSAL,
        ApprovalRow.proposed_parameters.contains("assaf_os.approve"),
    )).all()
    for row in rows:
        try:
            envelope = json.loads(row.proposed_parameters)
            kind = envelope.get("kind") if isinstance(envelope, dict) else None
        except (TypeError, ValueError):
            kind = "assaf_os.approve"  # unreadable: cannot prove it is not one
        if kind != "assaf_os.approve":
            continue
        if str(row.executed_at or "").strip():
            continue
        expires = _time(row.expires_at)
        if expires is not None and now >= expires:
            continue
        open_count += 1
    return open_count


def unknown_assaf_os_receipts(session: Any) -> int:
    """Approval-scope provider writes of an ``assaf_os.approve`` card that never confirmed (the
    Sheets decide was sent, or claimed, and its receipt is unknown). A card that cannot be read
    counts (fail closed). Other approval kinds (Gmail, calendar) are not this check's business."""
    from sqlalchemy import select

    from app.db.models import ApprovalRow, IdempotencyRow
    from app.services.owner_actions import ACTION_OWNER_EXTERNAL_WRITE, RESOURCE_OWNER_PROPOSAL

    stuck = session.scalars(select(IdempotencyRow).where(
        IdempotencyRow.scope == "approval",
        IdempotencyRow.status.in_(("pending_review", "provider_claimed")),
    )).all()
    count = 0
    for record in stuck:
        match = _CARD_KEY.fullmatch(record.key or "")
        if match is None:
            continue
        card = session.scalars(select(ApprovalRow).where(
            ApprovalRow.action == ACTION_OWNER_EXTERNAL_WRITE,
            ApprovalRow.resource_type == RESOURCE_OWNER_PROPOSAL,
            ApprovalRow.resource_id == match.group(1),
        )).first()
        if card is None:
            count += 1
            continue
        try:
            envelope = json.loads(card.proposed_parameters)
            kind = envelope.get("kind") if isinstance(envelope, dict) else None
        except (TypeError, ValueError):
            kind = "assaf_os.approve"
        if kind == "assaf_os.approve":
            count += 1
    return count


class LiveProbes:
    """Production probes. Engines are created lazily from explicit DSNs; ``close`` disposes them."""

    def __init__(
        self,
        settings: Any,
        owner_id: str,
        *,
        main_url: str,
        ledger_url: str,
        privileged_url: str,
        now: Callable[[], datetime],
        post: Callable[[str, bytes, float], bytes] | None = None,
    ) -> None:
        self._settings = settings
        self._owner = owner_id
        self._urls = {"main": main_url, "ledger": ledger_url, "privileged": privileged_url}
        self._now = now
        self._post = post
        self._engines: dict[str, Any] = {}

    def _engine(self, name: str) -> Any:
        from sqlalchemy import create_engine

        from app.db.session import sqlalchemy_database_url

        if name not in self._engines:
            if not self._urls[name]:
                raise Refusal("dsn_missing")
            url = sqlalchemy_database_url(self._urls[name])
            options = {} if url.startswith("sqlite") else {"connect_args": {"connect_timeout": 10}}
            self._engines[name] = create_engine(url, **options)
        return self._engines[name]

    def close(self) -> None:
        for engine in self._engines.values():
            engine.dispose()
        self._engines.clear()

    def sheets_fence(self) -> tuple[str, str]:
        return fence_state(self._settings, self._owner, post=self._post)

    def sheets_snapshot_pending(self) -> int:
        from app.services.os_backend import get_os_backend

        backend = get_os_backend(self._settings, self._owner, actor="MIA")
        if getattr(backend, "kind", None) != "sheets":
            raise Refusal("backend_not_sheets")
        pending = backend.snapshot().get("pending_proposals")
        if not isinstance(pending, list):
            raise Refusal("snapshot_unreadable")
        return len(pending)

    def main_role_safe(self) -> bool:
        from app.db.app_role_posture import inspect_posture

        return inspect_posture(self._engine("main")) == "app_role_ok"

    def ledger_role_safe(self) -> bool:
        from sqlalchemy.orm import Session

        from app.services.os_ledger.role_policy import validate_ledger_role

        try:
            with Session(self._engine("ledger")) as db:
                validate_ledger_role(db, self._engine("main"))
        except ValueError:
            return False
        return True

    def open_cards(self) -> int:
        from sqlalchemy.orm import Session

        with Session(self._engine("main")) as session:
            return open_assaf_os_cards(session, self._now())

    def unknown_receipts(self) -> int:
        from sqlalchemy.orm import Session

        with Session(self._engine("main")) as session:
            return unknown_assaf_os_receipts(session)

    def mirror(self, workspace: str) -> str:
        from sqlalchemy import text

        with self._engine("privileged").connect() as connection:
            row = connection.execute(text(
                "SELECT status, projected_version, verified_version, last_error_code "
                "FROM assaf_os.os_mirror_state WHERE workspace_id = :workspace"
            ), {"workspace": workspace}).first()
        if row is None:
            return "empty"
        if row[0] == "synced" and row[1] == row[2] and row[3] is None:
            return "verified"
        return "unverified"

    def non_import_receipts(self, workspace: str, epoch: int) -> int:
        from sqlalchemy import text

        with self._engine("privileged").connect() as connection:
            return int(connection.execute(text(
                "SELECT count(*) FROM assaf_os.os_receipts WHERE workspace_id = :workspace "
                "AND epoch = :epoch AND kind <> 'import'"
            ), {"workspace": workspace, "epoch": epoch}).scalar_one())


# ---------------------------------------------------------------------------------------------
# Context and the precondition checks


@dataclass
class Deps:
    settings: Any
    admin: Any  # api.AuthorityAdmin
    probes: Probes
    evidence: EvidenceStore
    now: Callable[[], datetime]
    owner_id: str
    workspace: str
    build_sha: str


@dataclass
class Context:
    deps: Deps
    approved_sha: str
    mode: Literal["drain", "preflight", "activate"] = "activate"
    # The fence evidence in effect for this run (``preflight`` updates it before evaluating).
    fence_doc: dict[str, Any] | None = None
    _fence: tuple[str, str] | None = None
    _manifest: tuple[dict[str, Any] | None, str] | None = None
    now: datetime = field(init=False)

    def __post_init__(self) -> None:
        self.now = self.deps.now()

    def fence(self) -> tuple[str, str]:
        if self._fence is None:
            self._fence = self.deps.probes.sheets_fence()
        return self._fence

    def manifest(self) -> tuple[dict[str, Any] | None, str]:
        if self._manifest is None:
            self._manifest = read_manifest(self.deps.evidence)
        return self._manifest


def _strict_int(value: object) -> int | None:
    return value if type(value) is int else None


def read_manifest(evidence: EvidenceStore) -> tuple[dict[str, Any] | None, str]:
    """The operator's manifest evidence (``manifest.json``) with its exact shape checked.

    Exact keys: ``v``, ``manifest`` (V2-30's object), ``manifest_digest`` (= ``api.digest`` of it),
    ``xlsx_captured_at`` and ``signoff``. ``(None, "absent")`` when there is no file and
    ``(None, reason)`` when it is unusable.
    """
    try:
        document = evidence.read(MANIFEST)
    except EvidenceError:
        return None, "evidence_unreadable"
    if document is None:
        return None, "absent"
    signoff = document.get("signoff")
    if (
        set(document) != {"v", "manifest", "manifest_digest", "xlsx_captured_at", "signoff"}
        or document["v"] != 1
        or type(document["v"]) is not int
        or not isinstance(document["manifest"], dict)
        or not isinstance(signoff, dict)
        or set(signoff) != {
            "owner_id", "signed_at", "manifest_digest", "xlsx_captured_at", "drift_changed_cells",
        }
    ):
        return None, "shape"
    return document, ""


def verify_signoff(
    document: Mapping[str, Any], owner_id: str, now: datetime
) -> tuple[SignedManifest | None, str]:
    """Signed means every binding holds: the digest recomputes, the sign-off names the same digest
    and capture time, it is by the workspace owner after the capture and not in the future, and the
    drift proof (xlsx #1 against #2, ``scripts/os_workbook_diff.py``) changed zero cells."""
    signoff = document["signoff"]
    digest = document["manifest_digest"]
    try:
        recomputed = api.digest(document["manifest"])
    except (TypeError, ValueError):
        return None, "digest_mismatch"
    if not (type(digest) is str and _HEX64.fullmatch(digest) and digest == recomputed):
        return None, "digest_mismatch"
    captured, signed = _time(document["xlsx_captured_at"]), _time(signoff["signed_at"])
    if (
        signoff["manifest_digest"] != digest
        or signoff["xlsx_captured_at"] != document["xlsx_captured_at"]
        or signoff["owner_id"] != owner_id
        or captured is None
        or signed is None
    ):
        return None, "signoff_mismatch"
    if not captured <= signed <= now + CLOCK_SKEW:
        return None, "signoff_time"
    if _strict_int(signoff["drift_changed_cells"]) != 0:
        return None, "drift"
    return SignedManifest(digest, captured, signed), ""


def _fence_time(doc: Mapping[str, Any] | None, epoch: int, sha: str) -> datetime | None:
    """The earliest continuous time the fence was observed, for this epoch and release."""
    if (
        doc is None
        or doc.get("kind") != "preflight"
        or doc.get("sheets") != "unavailable"
        or doc.get("epoch") != epoch
        or doc.get("release_sha") != sha
    ):
        return None
    return _time(doc.get("fence_probe_at"))


def _fence_broken(doc: Mapping[str, Any] | None, epoch: int, sha: str) -> datetime | None:
    """The first time, for this epoch and release, the Script was seen live again after a fence had
    been established (the T3 rollback lifts it without touching the ledger, so the epoch and the
    SHA do not change). A drain taken before it was taken while the freeze may have been lifted."""
    if (
        doc is None
        or doc.get("kind") != "preflight"
        or doc.get("epoch") != epoch
        or doc.get("release_sha") != sha
    ):
        return None
    return _time(doc.get("fence_broken_at"))


def _fence_record(
    previous: Mapping[str, Any] | None, epoch: int, sha: str, state: str, now: datetime
) -> dict[str, Any]:
    """The fence evidence (``preflight.json``) after one observation of the Script.

    ``fence_probe_at`` is the earliest probe of an unbroken run of ``unavailable``: it is carried
    through further ``unavailable`` probes and dropped by any other observation. Seeing the Script
    live right after a fence had been established is the break: ``fence_broken_at`` records it, is
    kept by every later observation of this epoch and release, and is never moved earlier. An
    ``unknown`` probe ends the run but proves nothing about the Script, so it is no break.
    """
    carried = _fence_time(previous, epoch, sha)
    broken = _fence_broken(previous, epoch, sha)
    document: dict[str, Any] = {
        "v": 1, "kind": "preflight", "epoch": epoch, "release_sha": sha,
        "sheets": state, "last_probe_at": _iso(now),
    }
    if state == "unavailable":
        # A later capture is only provably after the fence if no probe in between saw the Script
        # live.
        document["fence_probe_at"] = _iso(carried if carried is not None else now)
    elif state == "live" and carried is not None:
        broken = now
    if broken is not None:
        document["fence_broken_at"] = _iso(broken)
    return document


def _read_fence_record(ctx: Context) -> dict[str, Any] | None:
    try:
        return ctx.deps.evidence.read(PREFLIGHT)
    except EvidenceError:
        return None


def _pass(code: str) -> Check:
    return Check(code, "pass")


def _fail(code: str, detail: str = "") -> Check:
    return Check(code, "fail", detail)


def _not_yet(code: str, detail: str) -> Check:
    """Operator evidence that is not there yet. ``preflight`` reports it without failing (the
    first probe runs right after the Script is fenced, long before the last backup); ``activate``
    still refuses on it, with the same reason code."""
    return Check(code, "pending", detail)


def _count_check(code: str, count: int) -> Check:
    return _pass(code) if count == 0 else _fail(code, "nonzero")


def _check_release(ctx: Context, snapshot: api.AuthoritySnapshot) -> Check:
    ok = (
        bool(_SHA.fullmatch(ctx.deps.build_sha))
        and bool(_SHA.fullmatch(ctx.approved_sha))
        and ctx.approved_sha == ctx.deps.build_sha
    )
    return _pass("release_sha_mismatch") if ok else _fail("release_sha_mismatch")


def _check_fenced(ctx: Context, snapshot: api.AuthoritySnapshot) -> Check:
    state, detail = ctx.fence()
    if state == "unavailable":
        return _pass("sheets_not_fenced")
    return _fail("sheets_not_fenced", state if state == "live" else detail or state)


def _check_freeze(ctx: Context, snapshot: api.AuthoritySnapshot) -> Check:
    # The freeze (the T1 drain) must still be on at activation; the switch step turns it off.
    return _pass("freeze_off") if ctx.deps.settings.assaf_os_freeze is True else _fail("freeze_off")


def _check_capture(ctx: Context, snapshot: api.AuthoritySnapshot) -> Check:
    code = "capture_before_fence"
    document, reason = ctx.manifest()
    if document is None and reason == "absent":
        return Check(code, "pending", "no_manifest")
    captured = _time(document.get("xlsx_captured_at")) if document is not None else None
    if captured is None:
        return _fail(code, "no_capture_time")
    fenced_at = _fence_time(ctx.fence_doc, snapshot.epoch, ctx.approved_sha)
    if fenced_at is None:
        return _fail(code, "no_fence_probe")
    if not fenced_at < captured:
        return _fail(code, "capture_not_after_fence")
    return _pass(code)


def _check_main_role(ctx: Context, snapshot: api.AuthoritySnapshot) -> Check:
    return (
        _pass("main_role_unsafe") if ctx.deps.probes.main_role_safe() else _fail("main_role_unsafe")
    )


def _check_ledger_role(ctx: Context, snapshot: api.AuthoritySnapshot) -> Check:
    safe = ctx.deps.probes.ledger_role_safe()
    return _pass("ledger_role_unsafe") if safe else _fail("ledger_role_unsafe")


def _check_pending(ctx: Context, snapshot: api.AuthoritySnapshot) -> Check:
    """Sheets proposals cannot be read once the Script is fenced, so this reads the drain evidence
    taken by ``preflight --drain`` while the Sheet was live and the freeze was on. The freeze
    refuses new proposals, so the count can only fall until the fence."""
    code = "sheets_pending_proposals"
    document = ctx.deps.evidence.read(DRAIN)
    if document is None:
        return _fail(code, "no_drain_evidence")
    recorded = _time(document.get("recorded_at"))
    if (
        document.get("kind") != "drain"
        or document.get("epoch") != snapshot.epoch
        or document.get("release_sha") != ctx.approved_sha
        or document.get("freeze") is not True
        or recorded is None
        or _strict_int(document.get("sheets_pending")) is None
    ):
        return _fail(code, "drain_evidence_invalid")
    if document["sheets_pending"] != 0:
        return _fail(code, "nonzero")
    broken_at = _fence_broken(ctx.fence_doc, snapshot.epoch, ctx.approved_sha)
    if broken_at is not None and recorded < broken_at:
        # The fence was lifted after this drain (the T3 rollback): proposals made while the freeze
        # was lifted are not in it. Redo T2 (and T3) for the retry.
        return _fail(code, "drain_before_fence_break")
    fenced_at = _fence_time(ctx.fence_doc, snapshot.epoch, ctx.approved_sha)
    if fenced_at is None:
        return Check(code, "pending", "fence_not_recorded")
    if not recorded < fenced_at:
        return _fail(code, "drain_after_fence")
    if fenced_at - recorded > DRAIN_MAX_AGE:
        # No fence had to be broken for this: an aborted window only unfreezes. Proposals made
        # over the days in between have no Telegram card, so no other check would see them.
        return _fail(code, "drain_stale")
    return _pass(code)


def _check_unknown(ctx: Context, snapshot: api.AuthoritySnapshot) -> Check:
    return _count_check("sheets_unknown_receipts", ctx.deps.probes.unknown_receipts())


def _check_cards(ctx: Context, snapshot: api.AuthoritySnapshot) -> Check:
    return _count_check("approval_cards_open", ctx.deps.probes.open_cards())


def _check_backup(ctx: Context, snapshot: api.AuthoritySnapshot) -> Check:
    code = "backup_too_old"
    document = ctx.deps.evidence.read(BACKUP)  # V2-10 ``backup_offsite.py --status`` output
    if document is None:
        return _not_yet(code, "missing")
    taken = _time(document.get("last_success_utc"))
    if taken is None:
        return _fail(code, "invalid")
    age = ctx.now - taken
    return _pass(code) if -CLOCK_SKEW <= age < BACKUP_MAX_AGE else _fail(code, "age")


def _check_drill(ctx: Context, snapshot: api.AuthoritySnapshot) -> Check:
    code = "restore_drill_too_old"
    document = ctx.deps.evidence.read(DRILL)  # V2-10 ``restore_drill.py --report`` output
    if document is None:
        return _not_yet(code, "missing")
    started = _time(document.get("started_utc"))
    if started is None:
        return _fail(code, "invalid")
    if document.get("passed") is not True:
        return _fail(code, "not_passed")
    age = ctx.now - started
    return _pass(code) if -CLOCK_SKEW <= age <= DRILL_MAX_AGE else _fail(code, "age")


def _check_mirror(ctx: Context, snapshot: api.AuthoritySnapshot) -> Check:
    state = ctx.deps.probes.mirror(ctx.deps.workspace)
    return _pass("mirror_not_verified") if state in {"empty", "verified"} else _fail(
        "mirror_not_verified"
    )


def _check_manifest(ctx: Context, snapshot: api.AuthoritySnapshot) -> Check:
    code = "manifest_not_signed"
    document, reason = ctx.manifest()
    if document is None:
        return Check(code, "pending" if reason == "absent" else "fail", reason)
    signed, reason = verify_signoff(document, ctx.deps.owner_id, ctx.now)
    return _pass(code) if signed is not None else _fail(code, reason)


def _check_frontend(ctx: Context, snapshot: api.AuthoritySnapshot) -> Check:
    code = "fe_v2_reader_not_live"
    document = ctx.deps.evidence.read(FRONTEND)  # operator evidence: Cloudflare version + bundle
    if document is None:
        return _not_yet(code, "missing")
    version, bundle, recorded = (
        document.get("cloudflare_version"), document.get("bundle_sha256"),
        _time(document.get("recorded_at")),
    )
    ok = (
        set(document) == {"v", "cloudflare_version", "bundle_sha256", "recorded_at"}
        and document.get("v") == 1
        and type(version) is str and bool(_VERSION.fullmatch(version))
        and type(bundle) is str and bool(_HEX64.fullmatch(bundle))
        and recorded is not None and recorded <= ctx.now + CLOCK_SKEW
    )
    return _pass(code) if ok else _fail(code, "invalid")


def _check_journal(ctx: Context, snapshot: api.AuthoritySnapshot) -> Check:
    code = "journal_capacity"
    document = ctx.deps.evidence.read(JOURNAL)  # V2-00 measure of the _assaf_os_* journal tabs
    if document is None:
        return _not_yet(code, "missing")
    measured, cap, tabs = (
        _time(document.get("measured_at")), _strict_int(document.get("max_rows")),
        document.get("tabs"),
    )
    if (
        set(document) != {"v", "measured_at", "max_rows", "tabs"}
        or document.get("v") != 1
        or measured is None or cap is None or cap <= 0
        or not isinstance(tabs, dict)
        or any(_strict_int(rows) is None or rows < 0 for rows in tabs.values())
    ):
        return _fail(code, "invalid")
    # Headroom is only proven against the cap the Script enforces, over every tab it writes: an
    # operator-supplied cap or a shorter tab list could pass a journal that is already full.
    if cap != JOURNAL_MAX_ROWS:
        return _fail(code, "max_rows")
    if set(tabs) != set(JOURNAL_TABS):
        return _fail(code, "tabs")
    if not -CLOCK_SKEW <= ctx.now - measured <= JOURNAL_MAX_AGE:
        return _fail(code, "stale")
    if max(tabs.values()) > JOURNAL_HEADROOM * JOURNAL_MAX_ROWS:
        return _fail(code, "at_cap")
    return _pass(code)


_CHECKS: dict[str, Callable[[Context, api.AuthoritySnapshot], Check]] = {
    "release_sha_mismatch": _check_release,
    "sheets_not_fenced": _check_fenced,
    "freeze_off": _check_freeze,
    "capture_before_fence": _check_capture,
    "main_role_unsafe": _check_main_role,
    "ledger_role_unsafe": _check_ledger_role,
    "sheets_pending_proposals": _check_pending,
    "sheets_unknown_receipts": _check_unknown,
    "approval_cards_open": _check_cards,
    "backup_too_old": _check_backup,
    "restore_drill_too_old": _check_drill,
    "mirror_not_verified": _check_mirror,
    "manifest_not_signed": _check_manifest,
    "fe_v2_reader_not_live": _check_frontend,
    "journal_capacity": _check_journal,
}
assert tuple(_CHECKS) == PRECONDITIONS


def run_checks(ctx: Context, snapshot: api.AuthoritySnapshot) -> list[Check]:
    """Every precondition, in table order. A probe or evidence failure is that check failing."""
    results: list[Check] = []
    for code in PRECONDITIONS:
        try:
            results.append(_CHECKS[code](ctx, snapshot))
        except EvidenceError:
            results.append(_fail(code, "evidence_unreadable"))
        except Exception:  # never print a driver or transport message
            results.append(_fail(code, "probe_error"))
    return results


def _blocking(checks: Sequence[Check], *, allow_pending: bool) -> list[str]:
    return [c.code for c in checks if c.status == "fail" or (
        c.status == "pending" and not allow_pending)]


# ---------------------------------------------------------------------------------------------
# Commands


def pepper_fingerprint(pepper: str) -> str:
    """HMAC-SHA256(pepper, label) as hex: what ``pin_statement_pepper`` stores (section 12.2)."""
    raw = pepper.encode("utf-8")
    if len(raw) < MIN_PEPPER_BYTES:
        raise Refusal("pepper_invalid")
    return hmac.new(raw, PEPPER_LABEL, hashlib.sha256).hexdigest()


def _ledger_refusal(error: api.LedgerError) -> Refusal:
    return Refusal(error.code + (f":{error.reason}" if error.reason else ""))


def _state(ctx: Context) -> api.AuthoritySnapshot:
    try:
        return ctx.deps.admin.state(ctx.deps.workspace)
    except api.LedgerError as error:
        if error.reason == "workspace_missing":
            raise Refusal("workspace_missing") from None
        raise _ledger_refusal(error) from None


def _snapshot_document(snapshot: api.AuthoritySnapshot) -> dict[str, Any]:
    return {
        "workspace": snapshot.workspace_id,
        "epoch": snapshot.epoch,
        "version": snapshot.version,
        "states": dict(snapshot.states),
    }


def cmd_init(ctx: Context, *, rearm: bool = False) -> Result:
    """Step 4 of FORK-PROD-ORDER: ``ensure_workspace`` at epoch 1 and the pepper pin.

    Idempotent: an existing workspace is re-verified (owner, epoch) and left alone. ``--rearm`` is
    the re-attempt after ``rollback``: ``core`` fenced to shadow with the one epoch bump.
    """
    deps = ctx.deps
    try:
        if rearm:
            snapshot = _state(ctx)
            if snapshot.states["core"] == "shadow":
                # A repeat of a re-arm that already ran (or a ledger that was never fenced).
                return Result(True, {"command": "init", "ok": True, "outcome": "already_shadow",
                                     **_snapshot_document(snapshot)})
            if snapshot.states["core"] != "fenced":
                raise Refusal("authority_state_invalid")
            if ctx.deps.probes.non_import_receipts(deps.workspace, snapshot.epoch):
                raise Refusal("rearm_after_first_write")
            after = deps.admin.transition(
                deps.workspace, "core", "shadow", expected_epoch=snapshot.epoch,
                actor_owner_id=deps.owner_id,
            )
            return Result(True, {"command": "init", "ok": True, "outcome": "rearmed",
                                 **_snapshot_document(after)})
        try:
            existing = deps.admin.state(deps.workspace)
        except api.LedgerError as error:
            if error.reason != "workspace_missing":
                raise
            snapshot = deps.admin.ensure_workspace(deps.workspace, deps.owner_id, 1)
            outcome = "created"
        else:
            # Re-verifies the owner and epoch of the existing row; never creates or bumps.
            snapshot = deps.admin.ensure_workspace(deps.workspace, deps.owner_id, existing.epoch)
            outcome = "existing"
        pepper = getattr(deps.settings, "os_statement_pepper", "")
        pinned = False
        if pepper:
            snapshot = deps.admin.pin_statement_pepper(deps.workspace, pepper_fingerprint(pepper))
            pinned = True
        return Result(True, {"command": "init", "ok": True, "outcome": outcome,
                             "pepper_pinned": pinned, **_snapshot_document(snapshot)})
    except api.LedgerError as error:
        raise _ledger_refusal(error) from None


def cmd_status(ctx: Context) -> Result:
    deps = ctx.deps
    snapshot = _state(ctx)
    settings = deps.settings
    configured = settings.assaf_os_authority_epoch
    evidence: dict[str, Any] = {}
    for name in (DRAIN, PREFLIGHT, ACTIVATION):
        try:
            document = deps.evidence.read(name)
        except EvidenceError:
            evidence[name] = "unreadable"
            continue
        evidence[name] = None if document is None else {
            "epoch": document.get("epoch"),
            "at": document.get("recorded_at") or document.get("last_probe_at")
            or document.get("activated_at"),
        }
    def observed(call: Callable[[], Any]) -> Any:
        try:
            return call()
        except Exception:  # an unreadable observation is null, never a driver message
            return None

    document = {
        "command": "status",
        "ok": True,
        **_snapshot_document(snapshot),
        "activated_at": {
            domain: None if moment is None else _iso(moment)
            for domain, moment in snapshot.activated_at.items()
        },
        "settings": {
            "backend": settings.assaf_os_backend,
            "ledger_enabled": settings.assaf_os_ledger_enabled,
            "authority_epoch": configured,
            "epoch_matches": configured == snapshot.epoch,
            "freeze": settings.assaf_os_freeze,
            "mirror_enabled": getattr(settings, "assaf_os_mirror_enabled", False),
        },
        # The two facts the cutover decisions turn on: whether any business write has been made at
        # this epoch (the point of no simple rollback), and whether the mirror is verified.
        "non_import_receipts": observed(
            lambda: deps.probes.non_import_receipts(deps.workspace, snapshot.epoch)),
        "mirror": observed(lambda: deps.probes.mirror(deps.workspace)) or "unknown",
        "evidence": evidence,
    }
    return Result(True, document)


def cmd_preflight(ctx: Context, *, drain: bool, final_accept: bool) -> Result:
    deps = ctx.deps
    snapshot = _state(ctx)
    if drain:
        return _drain(ctx, snapshot, final_accept=final_accept)
    ctx.mode = "preflight"
    state, _ = ctx.fence()
    document = _fence_record(
        _read_fence_record(ctx), snapshot.epoch, ctx.approved_sha, state, ctx.now
    )
    ctx.fence_doc = document
    checks = run_checks(ctx, snapshot)
    document["checks"] = [check.as_dict() for check in checks]
    deps.evidence.write(PREFLIGHT, document)
    failed = _blocking(checks, allow_pending=True)
    pending = [c.code for c in checks if c.status == "pending"]
    result = {
        "command": "preflight", "ok": not failed, "ready_to_activate": not failed and not pending,
        "checks": document["checks"], "sheets": state, **_snapshot_document(snapshot),
    }
    return Result(not failed, result, failed)


def _drain(ctx: Context, snapshot: api.AuthoritySnapshot, *, final_accept: bool) -> Result:
    """T2: the one approved signed snapshot (a disclosed mutating call that accepts pending native
    edits), taken while the Sheet is live and the freeze is on. Records what is still open."""
    deps = ctx.deps
    if not final_accept:
        raise Refusal("final_accept_flag_required")
    if deps.settings.assaf_os_freeze is not True:
        raise Refusal("freeze_off")
    ctx.mode = "drain"
    state, _ = ctx.fence()
    if state != "live":
        raise Refusal("drain_requires_live_sheet")
    # The probe saw the Script live: record it in the fence evidence BEFORE the mutating snapshot,
    # so a drain that dies half-way still ends the unbroken run of "unavailable" and, when a fence
    # had been established, voids the old drain.json. The drain itself is then the first thing
    # recorded after the break.
    deps.evidence.write(PREFLIGHT, _fence_record(
        _read_fence_record(ctx), snapshot.epoch, ctx.approved_sha, state, ctx.now))
    pending = deps.probes.sheets_snapshot_pending()
    unknown = deps.probes.unknown_receipts()
    cards = deps.probes.open_cards()
    document = {
        "v": 1, "kind": "drain", "recorded_at": _iso(deps.now()), "epoch": snapshot.epoch,
        "release_sha": ctx.approved_sha, "freeze": True, "sheets_pending": pending,
        "sheets_unknown_receipts": unknown, "cards_open": cards,
    }
    deps.evidence.write(DRAIN, document)
    failed = [
        code for code, count in (
            ("sheets_pending_proposals", pending), ("sheets_unknown_receipts", unknown),
            ("approval_cards_open", cards),
        ) if count
    ]
    return Result(not failed, {
        "command": "preflight", "mode": "drain", "ok": not failed, "sheets_pending": pending,
        "sheets_unknown_receipts": unknown, "cards_open": cards, **_snapshot_document(snapshot),
    }, failed)


def cmd_activate(ctx: Context) -> Result:
    """``shadow -> active``, only when every precondition holds. Never bumps the epoch."""
    deps = ctx.deps
    snapshot = _state(ctx)
    core = snapshot.states["core"]
    if core == "active":
        return Result(True, {"command": "activate", "ok": True, "outcome": "already_active",
                             **_snapshot_document(snapshot)})
    if core != "shadow":
        raise Refusal("authority_state_invalid")
    ctx.mode = "activate"
    try:
        ctx.fence_doc = deps.evidence.read(PREFLIGHT)
    except EvidenceError:
        ctx.fence_doc = None
    checks = run_checks(ctx, snapshot)
    blocking = _blocking(checks, allow_pending=False)
    if blocking:
        return Result(False, {"command": "activate", "ok": False, "outcome": "refused",
                              "checks": [check.as_dict() for check in checks],
                              **_snapshot_document(snapshot)}, blocking)
    try:
        after = deps.admin.transition(
            deps.workspace, "core", "active", expected_epoch=snapshot.epoch,
            actor_owner_id=deps.owner_id,
        )
    except api.LedgerError as error:
        raise _ledger_refusal(error) from None
    document = {"command": "activate", "ok": True, "outcome": "activated",
                "checks": [check.as_dict() for check in checks], **_snapshot_document(after)}
    try:
        deps.evidence.write(ACTIVATION, {
            "v": 1, "kind": "activation", "activated_at": _iso(deps.now()), "epoch": after.epoch,
            "release_sha": ctx.approved_sha,
        })
    except Refusal:
        document["evidence_written"] = False  # the ledger state is the authority, not this file
    return Result(True, document)


def cmd_fence(ctx: Context) -> Result:
    """Fence the ledger: ``core`` (and ``statements`` while active) to ``fenced``. Idempotent."""
    deps = ctx.deps
    snapshot = _state(ctx)
    changed: list[str] = []
    try:
        for domain in ("core", "statements"):
            state = snapshot.states[domain]
            if state == "restoring":
                raise Refusal("authority_state_invalid")
            wanted = state in ("shadow", "active") and (domain == "core" or state == "active")
            if wanted:
                snapshot = deps.admin.transition(
                    deps.workspace, domain, "fenced", expected_epoch=snapshot.epoch,
                    actor_owner_id=deps.owner_id,
                )
                changed.append(domain)
    except api.LedgerError as error:
        raise _ledger_refusal(error) from None
    return Result(True, {"command": "fence", "ok": True, "fenced": changed,
                         **_snapshot_document(snapshot)})


def rollback_steps(epoch: int) -> list[str]:
    return [
        "Core is fenced: the ledger refuses every write. The Sheet is exactly as captured.",
        "mia-env: " + ", ".join(plan_lines("revert", epoch))
        + " (keep MIA_ASSAF_OS_FREEZE=true), then: sudo mia settings; confirm /health "
        "deployment.commit_sha is the approved SHA.",
        "Apps Script: set Script Property ASSAF_OS_MODE back to LIVE (no republish). Confirm the "
        "Sheet answers again: the Mia OS page loads from Sheets.",
        "Remove the tab protection and rename the archive workbook back to its working name.",
        "mia-env: MIA_ASSAF_OS_FREEZE=false, then: sudo mia settings.",
        "Re-attempt later: mia os-authority init --rearm (fenced to shadow, epoch + 1), then "
        "repeat T1 to T7 from a fresh xlsx capture.",
    ]


def cmd_rollback(ctx: Context) -> Result:
    """Allowed only before the first non-import receipt at the current epoch."""
    deps = ctx.deps
    snapshot = _state(ctx)
    core = snapshot.states["core"]
    if core == "restoring":
        raise Refusal("authority_state_invalid")
    if deps.probes.non_import_receipts(deps.workspace, snapshot.epoch):
        raise Refusal("rollback_after_first_write")
    try:
        if core != "fenced":
            snapshot = deps.admin.transition(
                deps.workspace, "core", "fenced", expected_epoch=snapshot.epoch,
                actor_owner_id=deps.owner_id,
            )
    except api.LedgerError as error:
        raise _ledger_refusal(error) from None
    return Result(True, {"command": "rollback", "ok": True, "outcome": "fenced",
                         "next_steps": rollback_steps(snapshot.epoch),
                         **_snapshot_document(snapshot)})


def plan_lines(phase: str, epoch: int) -> list[str]:
    """The ``mia-env`` keys one phase flips. Each phase is its own owner go (section 16)."""
    if phase == "drain":  # T1: the freeze starts
        return ["MIA_ASSAF_OS_FREEZE=true"]
    if phase == "switch":  # T6: the backend moves; the freeze STAYS on
        return [
            "MIA_ASSAF_OS_BACKEND=postgres",
            "MIA_ASSAF_OS_LEDGER_ENABLED=true",
            f"MIA_ASSAF_OS_AUTHORITY_EPOCH={epoch}",
        ]
    if phase == "mirror":  # T7: the Sheets mirror starts (its workbook id and key are set already)
        return ["MIA_ASSAF_OS_MIRROR_ENABLED=true"]
    if phase == "open":  # the first non-import write is admitted
        return ["MIA_ASSAF_OS_FREEZE=false"]
    return [  # revert: the way back to Sheets (rollback), the freeze untouched
        "MIA_ASSAF_OS_BACKEND=sheets",
        "MIA_ASSAF_OS_LEDGER_ENABLED=false",
        "MIA_ASSAF_OS_AUTHORITY_EPOCH=0",
        "MIA_ASSAF_OS_MIRROR_ENABLED=false",
    ]


PLAN_NOTES = {
    "drain": [
        "the freeze refuses every new proposal; approve and reject still pass, so the drain can "
        "settle every pending item",
        "each freeze starts a new drain: an earlier drain.json is set aside (renamed "
        "drain-superseded-<UTC>.json), so run T2 (preflight --drain) after this freeze",
    ],
    "switch": [
        "MIA_ASSAF_OS_FREEZE stays true: no write is admitted until the 'open' phase",
        "then check /health deployment.commit_sha and: sudo mia os-authority status "
        "(settings.epoch_matches must be true)",
    ],
    "mirror": [
        "MIA_OS_MIRROR_SPREADSHEET_ID and MIA_OS_MIRROR_SERVICE_ACCOUNT_JSON must already be set "
        "to the NEW mirror workbook and its service account (never the archive workbook)",
        "then check: sudo mia os-authority status (mirror must become verified)",
    ],
    "open": [
        "this admits the first non-import write; after the first receipt there is no simple "
        "rollback to Sheets",
        "then check: sudo mia os-authority status (settings.freeze must be false)",
    ],
    "revert": [
        "the freeze is untouched: set MIA_ASSAF_OS_FREEZE=false only after the Script is LIVE "
        "again",
    ],
}


def cmd_freeze_plan(ctx: Context, *, phase: str) -> Result:
    """The exact ``mia-env`` keys to flip for a phase, then ``mia settings``. Prints, and never
    touches Secret Manager or the ledger. A phase that follows another refuses to print until the
    settings the job reads show that the earlier one was applied. The one thing it changes is the
    evidence of an earlier freeze: ``--phase drain`` starts a freeze, so it sets an existing
    ``drain.json`` aside (each freeze starts a new drain) and refuses if it cannot."""
    if phase not in PHASES:
        raise Refusal("phase_invalid")
    settings = ctx.deps.settings
    epoch = 0
    if phase == "drain":
        ctx.deps.evidence.set_aside(DRAIN, ctx.now)
    else:
        snapshot = _state(ctx)
        epoch = snapshot.epoch
        if phase in ("switch", "mirror", "open") and snapshot.states["core"] != "active":
            raise Refusal("authority_state_invalid")
        if phase == "revert" and snapshot.states["core"] not in ("fenced", "shadow"):
            # The way back to Sheets is printed once `rollback` has fenced the ledger. While core is
            # active the Sheets fence still refuses every write (authority_moved), and a ledger that
            # is being restored is nobody's way back; a shadow ledger (never activated, or re-armed)
            # owns no data, so the settings may return to Sheets.
            raise Refusal("authority_state_invalid")
        if phase in ("switch", "mirror") and settings.assaf_os_freeze is not True:
            raise Refusal("freeze_off")
        if phase in ("mirror", "open") and not (
            settings.assaf_os_backend == "postgres"
            and settings.assaf_os_ledger_enabled is True
            and settings.assaf_os_authority_epoch == epoch
        ):
            raise Refusal("switch_not_applied")
    lines = [
        f"# os-authority freeze-plan --phase {phase}: set exactly these keys in Secret Manager "
        "secret mia-env, then run: sudo mia settings",
        *plan_lines(phase, epoch),
        *(f"# {note}" for note in PLAN_NOTES[phase]),
    ]
    return Result(True, {"command": "freeze-plan", "ok": True, "phase": phase,
                         "keys": plan_lines(phase, epoch)}, text="\n".join(lines) + "\n")


# ---------------------------------------------------------------------------------------------
# Entry point


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mia os-authority")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--approved-sha", required=True)
    parser.add_argument("--drain", action="store_true", help="preflight: the T2 drain evidence")
    parser.add_argument(
        "--final-accept", action="store_true",
        help="preflight --drain: allow the one disclosed mutating snapshot (T2 final accept)",
    )
    parser.add_argument("--rearm", action="store_true", help="init: fenced to shadow, epoch + 1")
    parser.add_argument("--phase", choices=PHASES, default="drain", help="freeze-plan phase")
    return parser


def _loader(module: str, name: str) -> Callable[..., Any]:
    try:
        return getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError):
        raise Refusal("ledger_implementation_missing") from None


def live_deps(env: Mapping[str, str]) -> Deps:
    """Production wiring from explicit environment: no default database, no settings fallback."""
    from app.core.config import get_settings

    settings = get_settings()
    owners = settings.telegram_owner_user_id_set()
    owner_id = next(iter(owners)) if len(owners) == 1 else ""
    workspace = settings.assaf_os_workspace_id
    if not _OWNER.fullmatch(owner_id):
        raise Refusal("owner_ambiguous")
    if not _WORKSPACE.fullmatch(workspace):
        raise Refusal("workspace_invalid")
    privileged = env.get(PRIVILEGED_DSN_ENV, "")
    main = env.get(MAIN_DSN_ENV, "")
    if not privileged or not main:
        raise Refusal("dsn_missing")
    ledger = getattr(settings, "owner_os_ledger_database_url", "")

    def now() -> datetime:
        return datetime.now(UTC)

    admin = _loader("app.services.os_ledger.authority", "build_authority_admin")(privileged)
    probes = LiveProbes(
        settings, owner_id, main_url=main, ledger_url=ledger, privileged_url=privileged, now=now,
    )
    return Deps(
        settings=settings, admin=admin, probes=probes,
        evidence=EvidenceStore(Path(env.get(EVIDENCE_DIR_ENV, DEFAULT_EVIDENCE_DIR))),
        now=now, owner_id=owner_id, workspace=workspace, build_sha=env.get("MIA_BUILD_SHA", ""),
    )


def _dispatch(ctx: Context, args: argparse.Namespace) -> Result:
    command = args.command
    if command == "init":
        return cmd_init(ctx, rearm=args.rearm)
    if command == "status":
        return cmd_status(ctx)
    if command == "preflight":
        return cmd_preflight(ctx, drain=args.drain, final_accept=args.final_accept)
    if command == "freeze-plan":
        return cmd_freeze_plan(ctx, phase=args.phase)
    if command == "activate":
        return cmd_activate(ctx)
    if command == "fence":
        return cmd_fence(ctx)
    return cmd_rollback(ctx)


def run(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    deps_factory: Callable[[Mapping[str, str]], Deps] | None = None,
    stdout: Any = None,
    stderr: Any = None,
) -> int:
    env = os.environ if env is None else env
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    args = build_parser().parse_args(argv)

    def finish(result: Result) -> int:
        outcome = "ok" if result.ok else "refused"
        reason = "ok" if result.ok else (result.codes[0] if result.codes else "refused")
        _LOG.info("os_authority op=%s outcome=%s reason=%s", args.command, outcome, reason)
        if result.text is not None and result.ok:
            stdout.write(result.text)
        else:
            stdout.write(json.dumps(result.document, sort_keys=True, indent=2) + "\n")
        if not result.ok:
            stderr.write("os_authority_refused:" + ",".join(result.codes) + "\n")
            return 1
        return 0

    build_sha = env.get("MIA_BUILD_SHA", "")
    if not _SHA.fullmatch(args.approved_sha) or args.approved_sha != build_sha:
        return finish(
            Result(False, {"command": args.command, "ok": False}, ["release_sha_mismatch"])
        )
    deps: Deps | None = None
    try:
        deps = (deps_factory or live_deps)(env)
        return finish(_dispatch(Context(deps, args.approved_sha), args))
    except Refusal as refusal:
        return finish(Result(False, {"command": args.command, "ok": False}, refusal.codes))
    except api.LedgerError as error:
        return finish(Result(False, {"command": args.command, "ok": False},
                             _ledger_refusal(error).codes))
    except Exception:
        # Never print driver, provider or connection text from a privileged job.
        return finish(Result(False, {"command": args.command, "ok": False}, ["internal_error"]))
    finally:
        close = getattr(getattr(deps, "probes", None), "close", None)
        if callable(close):
            close()


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    return run(argv)


if __name__ == "__main__":
    sys.exit(main())
