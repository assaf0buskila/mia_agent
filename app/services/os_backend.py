"""V2-17: the OS backend factory (V2_PG_CONTRACTS.md section 7).

Every code path that talks to Assaf OS asks ``get_os_backend`` for a backend and never builds an
``AssafOsPort`` itself. ``sheets`` wraps the signed Apps Script port with unchanged behaviour.
``postgres`` returns the ledger backend of V2-20 (``app.services.os_backend_pg``), imported lazily,
and fails closed with 503 ``ledger_unavailable`` when the ledger is not ready. The factory is one
half of the authority fence (I3); the other half is ``AssafOsPort._call`` (the Sheets side).

Log lines carry reason codes and booleans only: never a body, a record, an id or an error text.
"""

from __future__ import annotations

import importlib
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import parse_qsl

from app.services import assaf_os
from app.services.assaf_os import ActorSource, AssafOsError
from app.services.os_ledger.api import ENTITIES, Entity, LedgerError

_LOG = logging.getLogger("mia.os_backend")
_PG_MODULE = "app.services.os_backend_pg"

# The section 10.2 allow-list for the postgres snapshot route.
QUERY_KEYS = frozenset({"entity", "after", "limit", "since"})
MAX_AFTER_CHARS = 512
MAX_SINCE_CHARS = 64
MIN_LIMIT, MAX_LIMIT, DEFAULT_LIMIT = 1, 200, 50
_MAX_QUERY_FIELDS = 16
_LIMIT = re.compile(r"[1-9][0-9]{0,2}")
# Cursors and since tokens are server-issued and opaque: visible ASCII only, never whitespace.
_TOKEN = re.compile(r"[\x21-\x7e]+")


def _invalid_query() -> LedgerError:
    return LedgerError("invalid_query")


@dataclass(frozen=True)
class SnapshotQuery:
    """The validated section 10.2 query. Any value outside the allow-list is ``invalid_query``."""

    entity: Entity = "tasks"
    after: str | None = None
    limit: int = DEFAULT_LIMIT
    since: str | None = None

    def __post_init__(self) -> None:
        if type(self.entity) is not str or self.entity not in ENTITIES:
            raise _invalid_query()
        if type(self.limit) is not int or not MIN_LIMIT <= self.limit <= MAX_LIMIT:
            raise _invalid_query()
        for value, cap in ((self.after, MAX_AFTER_CHARS), (self.since, MAX_SINCE_CHARS)):
            if value is not None and (
                type(value) is not str or len(value) > cap or not _TOKEN.fullmatch(value)
            ):
                raise _invalid_query()
        if self.after is not None and self.since is not None:
            raise _invalid_query()


def parse_snapshot_query(query_string: str | bytes) -> SnapshotQuery:
    """Parse a raw query string against the section 10.2 allow-list.

    An unknown key, a duplicate key, a malformed value or ``after`` together with ``since`` raises
    ``LedgerError("invalid_query")``. No query gives the default query.
    """
    try:
        raw = query_string.decode("ascii") if isinstance(query_string, bytes) else query_string
        pairs = parse_qsl(
            raw,
            keep_blank_values=True,
            strict_parsing=bool(raw),
            encoding="utf-8",
            errors="strict",
            max_num_fields=_MAX_QUERY_FIELDS,
        )
    except (ValueError, UnicodeError):
        raise _invalid_query() from None
    values: dict[str, str] = {}
    for key, value in pairs:
        if key not in QUERY_KEYS or key in values:
            raise _invalid_query()
        values[key] = value
    fields: dict[str, Any] = {}
    if "entity" in values:
        fields["entity"] = values["entity"]
    if "after" in values:
        fields["after"] = values["after"]
    if "since" in values:
        fields["since"] = values["since"]
    if "limit" in values:
        if not _LIMIT.fullmatch(values["limit"]):
            raise _invalid_query()
        fields["limit"] = int(values["limit"])
    return SnapshotQuery(**fields)


class OsBackend(Protocol):
    kind: Literal["sheets", "postgres"]

    def schema(self) -> dict: ...

    def snapshot(self, query: SnapshotQuery | None = None) -> dict: ...

    def propose(self, request: Any) -> dict: ...

    def proposal(self, proposal_id: str) -> dict: ...

    def decide(
        self, operation: Literal["approve", "reject"], proposal_id: str, request: Any
    ) -> dict: ...

    def receipt(self, request_id: str) -> dict: ...

    def proposal_request(self, request_id: str) -> dict: ...


class SheetsOsBackend:
    """The Apps Script port behind the ``OsBackend`` protocol, with unchanged behaviour.

    The seven protocol methods are explicit and pass every argument through, so a call such as
    ``proposal(id, budget=10)`` still reaches the port. Any other attribute is the port's own, so a
    legacy call line that uses a port-only member keeps working.
    """

    kind: Literal["sheets"] = "sheets"

    def __init__(self, port: Any) -> None:
        self._port = port

    def __getattr__(self, name: str) -> Any:
        # Only reached when normal lookup fails. Dunder names and ``_port`` itself are never
        # delegated, so a copy or pickle probe cannot recurse before ``_port`` exists.
        if name == "_port" or (name.startswith("__") and name.endswith("__")):
            raise AttributeError(name)
        return getattr(self._port, name)

    def schema(self, *args: Any, **kwargs: Any) -> dict:
        return self._port.schema(*args, **kwargs)

    def snapshot(self, query: SnapshotQuery | None = None) -> dict:
        # v1 is byte-identical under sheets: the query is ignored.
        del query
        return self._port.snapshot()

    def propose(self, *args: Any, **kwargs: Any) -> dict:
        return self._port.propose(*args, **kwargs)

    def proposal(self, *args: Any, **kwargs: Any) -> dict:
        return self._port.proposal(*args, **kwargs)

    def decide(self, *args: Any, **kwargs: Any) -> dict:
        return self._port.decide(*args, **kwargs)

    def receipt(self, *args: Any, **kwargs: Any) -> dict:
        return self._port.receipt(*args, **kwargs)

    def proposal_request(self, *args: Any, **kwargs: Any) -> dict:
        del args, kwargs
        raise AssafOsError("operation_unavailable", 404)


def _log(outcome: str, reason: str) -> None:
    # The routine sheets/ok selection runs on every OS route and Mia tool call: DEBUG, not INFO.
    level = logging.DEBUG if (outcome, reason) == ("sheets", "ok") else logging.INFO
    _LOG.log(level, "os_backend op=select outcome=%s reason=%s", outcome, reason)


def _refuse_ledger(reason: str) -> AssafOsError:
    _log("refused", reason)
    return AssafOsError("ledger_unavailable", 503)


def _postgres_backend(settings: Any, owner_id: str, actor: ActorSource, principal: Any) -> Any:
    epoch = getattr(settings, "assaf_os_authority_epoch", 0)
    if not getattr(settings, "assaf_os_ledger_enabled", False):
        raise _refuse_ledger("ledger_disabled")
    if type(epoch) is not int or epoch < 1:
        raise _refuse_ledger("epoch_zero")
    try:
        module = importlib.import_module(_PG_MODULE)
        backend_class = module.PostgresOsBackend
    except (ImportError, AttributeError):
        raise _refuse_ledger("module_missing") from None
    except Exception:  # a broken module still fails closed, never as an unclassified 500
        raise _refuse_ledger("module_import_failed") from None
    try:
        backend = backend_class(settings, owner_id, actor=actor, principal=principal)
    except (AssafOsError, LedgerError) as exc:
        _log("refused", exc.code)
        raise
    except Exception:  # any other construction failure still fails closed, never a raw 500
        raise _refuse_ledger("backend_init_failed") from None
    _log("postgres", "ok")
    return backend


def get_os_backend(
    settings: Any, owner_id: str, *, actor: ActorSource, principal: Any = None
) -> OsBackend:
    """The only constructor of an Assaf OS backend.

    ``settings.assaf_os_backend`` is ``sheets`` unless it is exactly ``postgres``. The settings
    loader coerces any other value to ``sheets``, and the Sheets fence still refuses writes while
    the ledger holds the authority. The ``AssafOsPort`` name is read from its module at call time.
    """
    if getattr(settings, "assaf_os_backend", "sheets") == "postgres":
        return _postgres_backend(settings, owner_id, actor, principal)
    try:
        port = assaf_os.AssafOsPort(settings, owner_id, actor_source=actor)
    except AssafOsError as exc:
        _log("refused", exc.code)
        raise
    _log("sheets", "ok")
    return SheetsOsBackend(port)


__all__ = [
    "OsBackend",
    "SheetsOsBackend",
    "SnapshotQuery",
    "get_os_backend",
    "parse_snapshot_query",
]
