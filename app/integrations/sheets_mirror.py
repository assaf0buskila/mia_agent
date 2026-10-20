"""Google Sheets API v4 transport for the OS mirror workbook (§14, FORK-MIRROR-TRANSPORT).

A dedicated service account signs an RS256 JWT-bearer grant (``pyjwt[crypto]``) and every call
goes through stdlib ``urllib`` with a 15 s timeout. Writes use ``valueInputOption=RAW``, so a
cell that starts with ``=``, ``+``, ``-`` or ``@`` is stored as text and never evaluated. Reads use
``UNFORMATTED_VALUE``. The only hosts are the Google token endpoint and the Sheets API.

The target must equal ``os_mirror_spreadsheet_id`` and must never be the OS workbook or a CRM
workbook. Cell values, tokens, the key and provider responses are never logged or put in an
error: errors carry a reason code and whether the request may have landed (``sent``).
"""

from __future__ import annotations

import http.client
import json
import logging
import math
import re
import socket
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request
from urllib.request import urlopen as _urlopen

import jwt

SCOPE = "https://www.googleapis.com/auth/spreadsheets"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API_ROOT = "https://sheets.googleapis.com/v4/spreadsheets/"
TIMEOUT_S = 15
VALUE_INPUT_OPTION = "RAW"
VALUE_RENDER_OPTION = "UNFORMATTED_VALUE"
JWT_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
TOKEN_LIFETIME_S = 3600
TOKEN_SKEW = timedelta(seconds=60)
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_CELL_CHARS = 50_000

_SHEET_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_LOG = logging.getLogger("mia.os_mirror")

Cell = str | int | float | bool
Rows = Sequence[Sequence[Cell]]


class MirrorTransportError(Exception):
    """A refused or failed mirror call. ``sent`` is True when the write may have landed."""

    def __init__(self, code: str, *, sent: bool) -> None:
        super().__init__(code)
        self.code = code
        self.sent = sent


class MirrorTransport(Protocol):
    def batch_update(self, ranges: Sequence[tuple[str, Rows]]) -> None: ...
    def batch_get(self, ranges: Sequence[str]) -> dict[str, list[list[Any]]]: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


def target_refusal(spreadsheet_id: str, settings: Any) -> str | None:
    """None when ``spreadsheet_id`` is the configured mirror and no other known workbook."""
    from app.surfaces.crm import LOCKED_SPREADSHEET_ID

    target = (spreadsheet_id or "").strip()
    configured = str(getattr(settings, "os_mirror_spreadsheet_id", "") or "").strip()
    if not target or not _SHEET_ID.fullmatch(target) or target != configured:
        return "mirror_target_mismatch"
    # Any workbook Mia may write, or the frozen OS workbook, is never a mirror target.
    listed = str(getattr(settings, "sheets_allowed_spreadsheet_ids", "") or "")
    others = {item.strip() for item in listed.split(",")} | {
        str(getattr(settings, "assaf_os_workbook_id", "") or "").strip(),
        str(getattr(settings, "sheets_spreadsheet_id", "") or "").strip(),
        LOCKED_SPREADSHEET_ID,
    }
    if target in others:
        return "mirror_target_unsafe"
    return None


def _service_account(raw: str) -> tuple[str, str]:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        raise MirrorTransportError("mirror_credentials_invalid", sent=False) from None
    if not isinstance(data, dict) or data.get("type") != "service_account":
        raise MirrorTransportError("mirror_credentials_invalid", sent=False)
    email, key = data.get("client_email"), data.get("private_key")
    token_uri = data.get("token_uri", TOKEN_URL)
    if (
        not isinstance(email, str)
        or not email
        or not isinstance(key, str)
        or "-----BEGIN" not in key
        or token_uri != TOKEN_URL
    ):
        raise MirrorTransportError("mirror_credentials_invalid", sent=False)
    return email, key


def _check_cell(value: Any) -> None:
    if isinstance(value, bool | int):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if isinstance(value, str) and len(value) <= MAX_CELL_CHARS:
        return
    raise MirrorTransportError("mirror_value_invalid", sent=False)


class SheetsMirrorTransport:
    """``batch_update(ranges)`` and ``batch_get(ranges)`` on the one mirror spreadsheet."""

    def __init__(
        self,
        service_account_json: str,
        spreadsheet_id: str,
        *,
        settings: Any = None,
        urlopen: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if settings is None:
            from app.core.config import get_settings

            settings = get_settings()
        refusal = target_refusal(spreadsheet_id, settings)
        if refusal is not None:
            raise MirrorTransportError(refusal, sent=False)
        self._client_email, self._private_key = _service_account(service_account_json)
        self._spreadsheet_id = spreadsheet_id.strip()
        self._urlopen = urlopen or _urlopen
        self._clock = clock or _utcnow
        self._token: str | None = None
        self._token_expires: datetime | None = None

    def __repr__(self) -> str:
        return "SheetsMirrorTransport(<redacted>)"

    # -- auth ----------------------------------------------------------------------------------
    def _bearer(self) -> str:
        now = self._clock()
        if self._token and self._token_expires and now < self._token_expires - TOKEN_SKEW:
            return self._token
        self._token = self._token_expires = None
        issued = int(now.timestamp())
        claims = {
            "iss": self._client_email,
            "scope": SCOPE,
            "aud": TOKEN_URL,
            "iat": issued,
            "exp": issued + TOKEN_LIFETIME_S,
        }
        try:
            assertion = jwt.encode(claims, self._private_key, algorithm="RS256")
        except Exception:  # an unusable key; never echo it
            raise MirrorTransportError("mirror_credentials_invalid", sent=False) from None
        form = urlencode({"grant_type": JWT_GRANT, "assertion": assertion}).encode("ascii")
        request = Request(
            TOKEN_URL,
            data=form,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            payload = self._send(request)
        except MirrorTransportError:
            raise MirrorTransportError("mirror_auth_failed", sent=False) from None
        token = payload.get("access_token") if isinstance(payload, Mapping) else None
        lifetime = payload.get("expires_in") if isinstance(payload, Mapping) else None
        if (
            not isinstance(token, str)
            or not token
            or isinstance(lifetime, bool)
            or not isinstance(lifetime, int)
            or lifetime <= 0
        ):
            raise MirrorTransportError("mirror_auth_failed", sent=False)
        self._token = token
        self._token_expires = now + timedelta(seconds=lifetime)
        return token

    # -- HTTP ----------------------------------------------------------------------------------
    def _send(self, request: Request, *, write: bool = False) -> Any:
        """One HTTP exchange. Errors carry a reason code and whether a write may have landed."""
        try:
            with self._urlopen(request, timeout=TIMEOUT_S) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            exc.close()  # the provider body is never read
            if exc.code == 401:
                self._token = self._token_expires = None
            # Google refuses a 4xx before applying it; a 5xx or a 408 may have landed.
            landed = exc.code >= 500 or exc.code == 408
            raise MirrorTransportError(f"mirror_http_{exc.code}", sent=write and landed) from None
        except URLError as exc:
            reason = exc.reason
            if isinstance(reason, TimeoutError):
                raise MirrorTransportError("mirror_timeout", sent=write) from None
            if isinstance(reason, ConnectionRefusedError | socket.gaierror):
                raise MirrorTransportError("mirror_connect_failed", sent=False) from None
            raise MirrorTransportError("mirror_io_failed", sent=write) from None
        except TimeoutError:
            raise MirrorTransportError("mirror_timeout", sent=write) from None
        except (OSError, http.client.HTTPException):  # e.g. IncompleteRead, which is no OSError
            raise MirrorTransportError("mirror_io_failed", sent=write) from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise MirrorTransportError("mirror_response_invalid", sent=write)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise MirrorTransportError("mirror_response_invalid", sent=write) from None

    def _api_request(self, path: str, *, body: Any = None, query: str = "") -> Request:
        url = f"{API_ROOT}{quote(self._spreadsheet_id, safe='')}/{path}"
        if query:
            url = f"{url}?{query}"
        headers = {"Authorization": f"Bearer {self._bearer()}"}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        method = "POST" if body is not None else "GET"
        return Request(url, data=data, method=method, headers=headers)

    # -- the two calls -------------------------------------------------------------------------
    def batch_update(self, ranges: Sequence[tuple[str, Rows]]) -> None:
        """Write every range in one ``values:batchUpdate`` with ``valueInputOption=RAW``."""
        data = []
        for a1, rows in ranges:
            values = [list(row) for row in rows]
            for row in values:
                for value in row:
                    _check_cell(value)
            data.append({"range": a1, "majorDimension": "ROWS", "values": values})
        body = {
            "valueInputOption": VALUE_INPUT_OPTION,
            "includeValuesInResponse": False,
            "data": data,
        }
        started = time.monotonic()
        payload = self._send(self._api_request("values:batchUpdate", body=body), write=True)
        if not isinstance(payload, Mapping):
            raise MirrorTransportError("mirror_response_invalid", sent=True)
        _LOG.info(
            "os_mirror op=sheets_write outcome=ok reason=none ms=%d ranges=%d",
            int((time.monotonic() - started) * 1000),
            len(data),
        )

    def batch_get(self, ranges: Sequence[str]) -> dict[str, list[list[Any]]]:
        """Read every range in one ``values:batchGet`` with ``UNFORMATTED_VALUE``."""
        query = urlencode(
            [("ranges", a1) for a1 in ranges]
            + [("majorDimension", "ROWS"), ("valueRenderOption", VALUE_RENDER_OPTION)]
        )
        try:
            payload = self._send(self._api_request("values:batchGet", query=query))
        except MirrorTransportError as exc:
            if exc.code in {"mirror_auth_failed", "mirror_credentials_invalid"}:
                raise
            raise MirrorTransportError("mirror_read_failed", sent=False) from None
        value_ranges = payload.get("valueRanges") if isinstance(payload, Mapping) else None
        if not isinstance(value_ranges, list) or len(value_ranges) != len(ranges):
            raise MirrorTransportError("mirror_read_failed", sent=False)
        result: dict[str, list[list[Any]]] = {}
        for a1, item in zip(ranges, value_ranges, strict=True):
            values = item.get("values", []) if isinstance(item, Mapping) else None
            if not isinstance(values, list) or not all(isinstance(row, list) for row in values):
                raise MirrorTransportError("mirror_read_failed", sent=False)
            result[a1] = values
        return result
