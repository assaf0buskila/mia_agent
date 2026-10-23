"""V2-29 Sheets API v4 transport, against a recorded fake ``urlopen`` only. Never live."""

from __future__ import annotations

import http.client
import io
import json
import logging
import socket
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from app.core.config import Settings
from app.integrations import sheets_mirror
from app.integrations.sheets_mirror import (
    SCOPE,
    TOKEN_URL,
    MirrorTransportError,
    SheetsMirrorTransport,
)
from app.surfaces.crm import LOCKED_SPREADSHEET_ID
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from tests.workers.mirror_fakes import Clock

SHEET_ID = "mirror-sheet-1"
CLIENT_EMAIL = "os-mirror@example-project.iam.gserviceaccount.com"
CELL_SECRET = "CELL-VALUE-NEVER-LOGGED"


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def account(key: rsa.RSAPrivateKey) -> str:
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    return json.dumps({
        "type": "service_account",
        "client_email": CLIENT_EMAIL,
        "private_key": pem,
        "token_uri": TOKEN_URL,
    })


class Response(io.BytesIO):
    def __init__(self, payload: Any, status: int = 200) -> None:
        super().__init__(json.dumps(payload).encode("utf-8"))
        self.status = status

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class FakeUrlopen:
    """Records every request; answers from a script. No socket is ever opened."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.token_count = 0
        self.script: list[Any] = []

    def __call__(self, request: Any, timeout: float | None = None) -> Response:
        body = request.data.decode("utf-8") if request.data else ""
        self.requests.append({
            "url": request.full_url,
            "method": request.get_method(),
            "headers": {k.lower(): v for k, v in request.header_items()},
            "body": body,
            "timeout": timeout,
        })
        if request.full_url == TOKEN_URL:
            self.token_count += 1
            return Response({
                "access_token": f"tok-{self.token_count}",
                "expires_in": 3600,
                "token_type": "Bearer",
            })
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return Response(item)
        if ":batchGet" in request.full_url:
            ranges = parse_qs(urlsplit(request.full_url).query)["ranges"]
            return Response({
                "spreadsheetId": SHEET_ID,
                "valueRanges": [{"range": r, "values": [["a", 1]]} for r in ranges],
            })
        return Response({"spreadsheetId": SHEET_ID, "totalUpdatedCells": 2})


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"os_mirror_spreadsheet_id": SHEET_ID}
    values.update(overrides)
    return Settings(_env_file=None, **values)


def transport(account: str, opener: FakeUrlopen, clock: Clock | None = None, **kw: Any):
    return SheetsMirrorTransport(
        account,
        kw.pop("spreadsheet_id", SHEET_ID),
        settings=kw.pop("settings", settings()),
        urlopen=opener,
        clock=clock or Clock(),
    )


def test_writes_are_raw_reads_are_unformatted_and_every_call_has_a_15s_timeout(account):
    opener = FakeUrlopen()
    mirror = transport(account, opener)
    mirror.batch_update([("'לידים'!A1", [["=1+1", 5, True, ""]])])
    got = mirror.batch_get(["'לידים'", "'סיכום'"])
    assert got == {"'לידים'": [["a", 1]], "'סיכום'": [["a", 1]]}

    token, write, read = opener.requests
    assert token["url"] == TOKEN_URL and token["method"] == "POST"
    assert write["url"] == (
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values:batchUpdate"
    )
    body = json.loads(write["body"])
    assert body["valueInputOption"] == "RAW"
    assert body["data"] == [
        {"range": "'לידים'!A1", "majorDimension": "ROWS", "values": [["=1+1", 5, True, ""]]}
    ]
    query = parse_qs(urlsplit(read["url"]).query)
    assert urlsplit(read["url"]).path == f"/v4/spreadsheets/{SHEET_ID}/values:batchGet"
    assert query["valueRenderOption"] == ["UNFORMATTED_VALUE"]
    assert query["ranges"] == ["'לידים'", "'סיכום'"]
    assert {r["timeout"] for r in opener.requests} == {15}
    assert write["headers"]["authorization"] == "Bearer tok-1"
    assert {urlsplit(r["url"]).hostname for r in opener.requests} == {
        "oauth2.googleapis.com", "sheets.googleapis.com",
    }
    source = Path(sheets_mirror.__file__).read_text(encoding="utf-8")
    assert "USER_ENTERED" not in source


def test_the_jwt_bearer_grant_is_rs256_scoped_and_cached(account, key) -> None:
    opener = FakeUrlopen()
    clock = Clock()
    mirror = transport(account, opener, clock)
    mirror.batch_update([("'לידים'!A1", [["x"]])])
    mirror.batch_get(["'לידים'"])
    assert opener.token_count == 1, "the token is cached in memory"

    form = parse_qs(opener.requests[0]["body"])
    assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    assertion = form["assertion"][0]
    assert jwt.get_unverified_header(assertion)["alg"] == "RS256"
    claims = jwt.decode(
        assertion, key.public_key(), algorithms=["RS256"], audience=TOKEN_URL,
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims["iss"] == CLIENT_EMAIL
    assert claims["scope"] == SCOPE == "https://www.googleapis.com/auth/spreadsheets"
    assert claims["exp"] - claims["iat"] == 3600

    clock.advance(3600)
    mirror.batch_get(["'לידים'"])
    assert opener.token_count == 2, "an expiring token is refreshed"


@pytest.mark.parametrize(
    ("spreadsheet_id", "overrides", "code"),
    [
        ("another-sheet", {}, "mirror_target_mismatch"),
        ("", {"os_mirror_spreadsheet_id": ""}, "mirror_target_mismatch"),
        ("os-book", {"os_mirror_spreadsheet_id": "os-book", "assaf_os_workbook_id": "os-book"},
         "mirror_target_unsafe"),
        (LOCKED_SPREADSHEET_ID, {"os_mirror_spreadsheet_id": LOCKED_SPREADSHEET_ID},
         "mirror_target_unsafe"),
        ("crm-book", {"os_mirror_spreadsheet_id": "crm-book", "sheets_spreadsheet_id": "crm-book"},
         "mirror_target_unsafe"),
    ],
)
def test_the_target_must_be_the_configured_mirror_and_never_another_workbook(
    account, spreadsheet_id, overrides, code
) -> None:
    opener = FakeUrlopen()
    with pytest.raises(MirrorTransportError) as caught:
        transport(account, opener, spreadsheet_id=spreadsheet_id, settings=settings(**overrides))
    assert caught.value.code == code and caught.value.sent is False
    assert opener.requests == []


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json",
        '{"type":"authorized_user","client_email":"a@b","private_key":"-----BEGIN"}',
        '{"type":"service_account","client_email":"a@b"}',
        '{"type":"service_account","client_email":"a@b","private_key":"-----BEGIN x",'
        '"token_uri":"https://evil.example/token"}',
    ],
)
def test_bad_credentials_are_refused_without_echoing_them(raw: str) -> None:
    with pytest.raises(MirrorTransportError) as caught:
        transport(raw, FakeUrlopen())
    assert caught.value.code == "mirror_credentials_invalid"
    assert "BEGIN" not in str(caught.value) and "evil" not in str(caught.value)


def test_the_secret_and_the_token_never_appear_in_repr(account) -> None:
    opener = FakeUrlopen()
    mirror = transport(account, opener)
    mirror.batch_get(["'לידים'"])
    text = repr(mirror) + str(vars(mirror).keys())
    assert "PRIVATE" not in repr(mirror) and "tok-1" not in repr(mirror)
    assert "BEGIN" not in text


def _http_error(code: int) -> HTTPError:
    return HTTPError(
        "https://sheets.googleapis.com/x", code, "x", {}, io.BytesIO(CELL_SECRET.encode())
    )


@pytest.mark.parametrize(
    ("error", "code", "sent"),
    [
        (_http_error(400), "mirror_http_400", False),
        (_http_error(403), "mirror_http_403", False),
        (_http_error(429), "mirror_http_429", False),
        (_http_error(500), "mirror_http_500", True),
        (_http_error(503), "mirror_http_503", True),
        (URLError(ConnectionRefusedError()), "mirror_connect_failed", False),
        (URLError(socket.gaierror()), "mirror_connect_failed", False),
        (URLError(TimeoutError()), "mirror_timeout", True),
        (TimeoutError(), "mirror_timeout", True),
        (ConnectionResetError(), "mirror_io_failed", True),
        # http.client errors are not OSError: a truncated body or a bad status line.
        (http.client.IncompleteRead(b"partial"), "mirror_io_failed", True),
        (http.client.BadStatusLine("x"), "mirror_io_failed", True),
    ],
)
def test_write_errors_say_whether_the_request_may_have_landed(
    account, caplog, error, code, sent
) -> None:
    caplog.set_level(logging.DEBUG)
    opener = FakeUrlopen()
    opener.script = [error]
    mirror = transport(account, opener)
    with pytest.raises(MirrorTransportError) as caught:
        mirror.batch_update([("'לידים'!A1", [[CELL_SECRET]])])
    assert (caught.value.code, caught.value.sent) == (code, sent)
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert CELL_SECRET not in logged and "tok-" not in logged
    assert CELL_SECRET not in str(caught.value)


def test_a_malformed_write_response_is_unknown_and_a_read_error_is_a_read_failure(account):
    opener = FakeUrlopen()
    opener.script = [["not", "an", "object"], _http_error(500)]
    mirror = transport(account, opener)
    with pytest.raises(MirrorTransportError) as write:
        mirror.batch_update([("'לידים'!A1", [["x"]])])
    assert (write.value.code, write.value.sent) == ("mirror_response_invalid", True)
    with pytest.raises(MirrorTransportError) as read:
        mirror.batch_get(["'לידים'"])
    assert read.value.code == "mirror_read_failed"


def test_a_read_with_the_wrong_number_of_ranges_is_refused(account) -> None:
    opener = FakeUrlopen()
    opener.script = [{"valueRanges": []}]
    with pytest.raises(MirrorTransportError) as caught:
        transport(account, opener).batch_get(["'לידים'"])
    assert caught.value.code == "mirror_read_failed"


def test_a_token_failure_is_not_sent_and_drops_the_cache(account) -> None:
    opener = FakeUrlopen()

    def refuse(request: Any, timeout: float | None = None) -> Response:
        if request.full_url == TOKEN_URL:
            raise _http_error(401)
        return opener(request, timeout)

    mirror = transport(account, refuse)  # type: ignore[arg-type]
    with pytest.raises(MirrorTransportError) as caught:
        mirror.batch_update([("'לידים'!A1", [["x"]])])
    assert (caught.value.code, caught.value.sent) == ("mirror_auth_failed", False)
    assert opener.requests == []


def test_non_json_safe_values_are_refused_before_anything_is_sent(account) -> None:
    opener = FakeUrlopen()
    mirror = transport(account, opener)
    for bad in (float("nan"), {"x": 1}, b"bytes", None):
        with pytest.raises(MirrorTransportError) as caught:
            mirror.batch_update([("'לידים'!A1", [[bad]])])
        assert (caught.value.code, caught.value.sent) == ("mirror_value_invalid", False)
    assert opener.requests == []
