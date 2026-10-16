"""V2-42: GET /v1/owner/os/unified/cached and the cache refresh on the snapshot route (section 2).

The cached route authenticates exactly like the snapshot route, re-proves the binding, and then
reads one validated row. It never reaches the Apps Script. A miss is 200 {"cached": false}.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import app.api.assaf_os as assaf_os_api
import app.api.owner_sessions as owner_sessions_api
import pytest
from app.api.owner_sessions import OwnerSessionPrincipal
from app.services import assaf_os as port
from app.services import assaf_os_snapshot_cache as cache
from fastapi.testclient import TestClient

from tests.api import test_owner_os_timing as timing
from tests.services import snapshot_cache_support as support
from tests.unit import test_assaf_os as assaf
from tests.unit import test_owner_sessions as auth

sessions = auth.sessions
OWNER = auth.OWNER_ID
OTHER = "87654321"
ORIGIN = timing.ORIGIN
SNAPSHOT = "/v1/owner/os/unified"
CACHED = "/v1/owner/os/unified/cached"
LOGGER = "mia.os_snapshot_cache"
HIT_KEYS = [
    "api_version",
    "source",
    "mode",
    "workbook_id",
    "workspace_id",
    "cached",
    "as_of",
    "age_seconds",
    "cache_version",
    "snapshot",
]


@pytest.fixture(autouse=True)
def clear_process_caches():
    port._clear_assaf_os_decision_binding_cache()
    port._clear_assaf_os_schema_cache()
    yield
    port._clear_assaf_os_decision_binding_cache()
    port._clear_assaf_os_schema_cache()


@pytest.fixture
def config():
    value = assaf.settings()
    value.telegram_owner_user_ids = f"{OWNER},{OTHER}"
    return value


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine, factory = support.sqlite_cache(tmp_path / "cache.db")
    monkeypatch.setattr(cache, "get_session_factory", lambda: factory)
    yield factory
    engine.dispose()


class Script:
    """A signed fake of the Apps Script. Each snapshot is tagged with the owner who asked."""

    def __init__(self, config, *, snapshot=None, fail=False):
        self.config, self.fixed, self.fail = config, snapshot, fail
        self.calls: list[str] = []

    def __call__(self, _url, body, _budget):
        request = json.loads(json.loads(body)["payload"])
        operation = request["operation"]
        self.calls.append(operation)
        if self.fail:
            raise RuntimeError("transport failed")
        if operation == "snapshot":
            result = self.fixed or support.snapshot_at(
                assaf._snapshot(self.config),
                datetime.now(UTC),
                marker=f"owner-{request['owner_id']}",
            )
        elif operation == "schema":
            result = assaf._schema(self.config)
        elif operation in {"propose", "proposal"}:
            result = assaf._pending(self.config, "OS")
        else:
            result = assaf._decision_result(self.config, request)
        return assaf.signed(body, result, self.config)


def _install(monkeypatch, config, **kwargs) -> Script:
    script = Script(config, **kwargs)
    monkeypatch.setattr(port, "_post", script)
    return script


def _login_as(browser, owner: str):
    return auth._login(browser, proof=auth._proof(owner_id=owner))


class _Spy:
    def __init__(self, real):
        self.real, self.calls = real, []

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        return self.real(*args, **kwargs)


# ----------------------------------------------------------------------- a cached hit


def test_an_open_fills_the_cache_and_the_cached_read_never_reaches_the_script(
    sessions, db, config, monkeypatch
):
    script = _install(monkeypatch, config)
    with timing._browser(sessions, config) as browser:
        auth._login(browser)
        opened = browser.get(SNAPSHOT, headers=ORIGIN)
        assert opened.status_code == 200 and script.calls == ["snapshot"]
        assert support.row_count(db) == 1, "the successful open refreshed the cache"

        def forbidden(*_args, **_kwargs):
            raise AssertionError("the cached read reached the Apps Script port")

        monkeypatch.setattr(port, "_post", forbidden)
        monkeypatch.setattr(port.AssafOsPort, "_call", forbidden)
        cached = browser.get(CACHED, headers=ORIGIN)

    assert cached.status_code == 200, cached.text
    body = cached.json()
    assert list(body) == HIT_KEYS
    assert body["cached"] is True and body["source"] == "google_sheets"
    assert body["api_version"] == "assaf-os.v1" and body["mode"] == config.assaf_os_mode
    assert body["workbook_id"] == config.assaf_os_workbook_id
    assert body["workspace_id"] == config.assaf_os_workspace_id
    assert body["snapshot"] == opened.json(), "exactly the validated snapshot the open returned"
    assert body["as_of"] == opened.json()["observed_at"]
    assert isinstance(body["age_seconds"], int) and 0 <= body["age_seconds"] < 30
    assert body["cache_version"] == 1
    assert body["snapshot"]["issues"] == [f"owner-{OWNER}"]
    assert script.calls == ["snapshot"], "no second Script call"
    assert port._SCHEMA_CACHE == {}, "the in-process schema cache was never touched"
    assert cached.headers["cache-control"] == "private, no-store"
    assert cached.headers["x-content-type-options"] == "nosniff"


def test_the_cached_read_server_timing_carries_auth_and_db_but_never_script(
    sessions, db, config, monkeypatch
):
    _install(monkeypatch, config)
    with timing._browser(sessions, config) as browser:
        auth._login(browser)
        assert browser.get(SNAPSHOT, headers=ORIGIN).status_code == 200
        hit = browser.get(CACHED, headers=ORIGIN)
    parsed = timing._timing(hit)
    assert {"auth", "db"} <= set(parsed)
    assert "script" not in parsed and "ledger" not in parsed
    assert hit.json()["cached"] is True


def test_a_miss_is_200_cached_false_and_never_204(sessions, db, config, monkeypatch):
    script = _install(monkeypatch, config)
    with timing._browser(sessions, config) as browser:
        auth._login(browser)
        miss = browser.get(CACHED, headers=ORIGIN)
    assert miss.status_code == 200
    assert miss.json() == {"cached": False}
    assert miss.content != b"" and "script" not in timing._timing(miss)
    assert script.calls == []


def test_the_cached_read_does_not_write_or_bump_the_row(sessions, db, config, monkeypatch):
    _install(monkeypatch, config)
    with timing._browser(sessions, config) as browser:
        auth._login(browser)
        browser.get(SNAPSHOT, headers=ORIGIN)
        before = support.read_row(db, OWNER, config.assaf_os_workspace_id)
        for _ in range(3):
            assert browser.get(CACHED, headers=ORIGIN).json()["cached"] is True
    assert support.read_row(db, OWNER, config.assaf_os_workspace_id) == before


@pytest.mark.parametrize("change", ["secret", "script_url", "workbook", "mode"])
def test_a_binding_change_after_the_open_turns_the_hit_into_a_miss(
    sessions, db, config, monkeypatch, change
):
    _install(monkeypatch, config)
    with timing._browser(sessions, config) as browser:
        auth._login(browser)
        browser.get(SNAPSHOT, headers=ORIGIN)
        assert browser.get(CACHED, headers=ORIGIN).json()["cached"] is True
        if change == "secret":
            config.assaf_os_hmac_secret = "rotated-test-secret-with-at-least-32-bytes"
        elif change == "script_url":
            config.assaf_os_script_url = "https://script.google.com/macros/s/" + "q" * 24 + "/exec"
        elif change == "workbook":
            config.assaf_os_workbook_id = "workbook_98765432109876543210"
        else:
            config.assaf_os_mode = "LIVE"
        after = browser.get(CACHED, headers=ORIGIN)
    assert after.status_code == 200 and after.json() == {"cached": False}


# ------------------------------------------------------- the snapshot route is unchanged


def test_the_snapshot_response_is_byte_identical_with_and_without_the_cache(
    sessions, db, config, monkeypatch
):
    fixed = support.snapshot_at(assaf._snapshot(config), datetime.now(UTC), marker="fixed")
    _install(monkeypatch, config, snapshot=fixed)
    bodies = []
    with timing._browser(sessions, config) as browser:
        auth._login(browser)
        for refresh in (lambda *_a, **_k: None, cache.remember_snapshot):
            monkeypatch.setattr(assaf_os_api, "remember_snapshot", refresh)
            response = browser.get(SNAPSHOT, headers=ORIGIN)
            assert response.status_code == 200
            bodies.append(
                (
                    response.content,
                    response.headers["content-type"],
                    response.headers["cache-control"],
                    sorted(set(timing._timing(response)) - {"script", "verify", "auth", "db"}),
                )
            )
    assert bodies[0] == bodies[1]
    assert json.loads(bodies[0][0]) == fixed
    assert support.row_count(db) == 1, "only the second run, with the real refresh, stored a row"


def test_a_broken_cache_never_changes_or_fails_the_snapshot_response(
    sessions, db, config, monkeypatch, caplog
):
    fixed = support.snapshot_at(assaf._snapshot(config), datetime.now(UTC), marker="fixed")
    _install(monkeypatch, config, snapshot=fixed)
    with timing._browser(sessions, config) as browser:
        auth._login(browser)
        healthy = browser.get(SNAPSHOT, headers=ORIGIN)

        def unreachable():
            raise RuntimeError("database unreachable")

        monkeypatch.setattr(cache, "get_session_factory", unreachable)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=LOGGER):
            broken = browser.get(SNAPSHOT, headers=ORIGIN)
            missed = browser.get(CACHED, headers=ORIGIN)
    assert broken.status_code == 200 and broken.content == healthy.content
    assert missed.status_code == 200 and missed.json() == {"cached": False}
    assert [r.getMessage() for r in caplog.records if r.name == LOGGER] == [
        "os_snapshot_cache op=write outcome=failed reason=db_unavailable",
        "os_snapshot_cache op=read outcome=failed reason=db_unavailable",
    ]


def test_only_a_successful_snapshot_get_fills_the_cache(sessions, db, config, monkeypatch):
    script = _install(monkeypatch, config)
    with timing._browser(sessions, config) as stranger:
        assert stranger.get(SNAPSHOT, headers=ORIGIN).status_code == 401
        assert stranger.get(CACHED, headers=ORIGIN).status_code == 401
    assert support.row_count(db) == 0

    with timing._browser(sessions, config) as browser:
        session = auth._login(browser)
        headers = {**ORIGIN, "X-CSRF-Token": session["csrf_token"]}

        script.fail = True
        assert browser.get(SNAPSHOT, headers=ORIGIN).status_code == 503
        script.fail = False
        monkeypatch.setattr(
            port,
            "_post",
            lambda _u, body, _b: assaf.signed(
                body, assaf._snapshot(config), config, valid_signature=False
            ),
        )
        assert browser.get(SNAPSHOT, headers=ORIGIN).status_code == 503, "bad signature"
        monkeypatch.setattr(port, "_post", script)

        # Every other owner OS call, successful or not, leaves the cache alone.
        assert browser.get(SNAPSHOT + "/schema", headers=ORIGIN).status_code == 200
        proposed = browser.post(
            SNAPSHOT + "/proposals", headers=headers, json=timing._propose_body()
        )
        assert proposed.status_code == 200
        approve = browser.post(
            f"{SNAPSHOT}/proposals/{assaf.PROPOSAL}/approve",
            headers=headers,
            json={"request_id": assaf._pending(config)["approval_request_id"]},
        )
        assert approve.status_code == 200
        reread = browser.get(f"{SNAPSHOT}/proposals/{assaf.PROPOSAL}", headers=ORIGIN)
        assert reread.status_code == 200
        assert support.row_count(db) == 0
        assert browser.get(CACHED, headers=ORIGIN).json() == {"cached": False}

        assert browser.get(SNAPSHOT, headers=ORIGIN).status_code == 200
    assert support.row_count(db) == 1


def test_an_oversized_snapshot_is_served_but_never_cached(sessions, db, config, monkeypatch):
    huge = support.padded_to(
        support.snapshot_at(assaf._snapshot(config), datetime.now(UTC)),
        support.MAX_CACHED_BYTES + 1,
    )
    _install(monkeypatch, config, snapshot=huge)
    with timing._browser(sessions, config) as browser:
        auth._login(browser)
        opened = browser.get(SNAPSHOT, headers=ORIGIN)
        assert opened.status_code == 200 and opened.json() == huge
        assert browser.get(CACHED, headers=ORIGIN).json() == {"cached": False}
    assert support.row_count(db) == 0


async def test_the_refresh_runs_after_the_response_has_been_sent(
    sessions, db, config, monkeypatch
):
    _install(monkeypatch, config)
    events: list[str] = []

    def slow_refresh(*_args, **_kwargs) -> None:
        events.append("refresh_start")
        time.sleep(0.15)
        events.append("refresh_end")

    monkeypatch.setattr(assaf_os_api, "remember_snapshot", slow_refresh)
    app = timing._app(sessions, config)
    with TestClient(app, base_url=auth.ORIGIN) as browser:
        auth._login(browser)
        cookie = "; ".join(f"{c.name}={c.value}" for c in browser.cookies.jar)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict] = []

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            events.append("response_complete")

    host = auth.ORIGIN.split("//")[1]
    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": SNAPSHOT,
            "raw_path": SNAPSHOT.encode(),
            "root_path": "",
            "query_string": b"",
            "headers": [
                (b"host", host.encode()),
                (b"origin", auth.ORIGIN.encode()),
                (b"cookie", cookie.encode()),
            ],
            "client": ("127.0.0.1", 50000),
            "server": (host, 443),
        },
        receive,
        send,
    )
    assert sent[0]["status"] == 200
    assert events == ["response_complete", "refresh_start", "refresh_end"]


# ------------------------------------------------------------------------- authorization


@pytest.mark.parametrize(
    "case",
    [
        "no_cookie",
        "unknown_cookie",
        "expired_session",
        "revoked_session",
        "removed_owner",
        "scope_removed",
        "feature_off",
        "wrong_origin",
    ],
)
def test_the_cached_route_has_the_snapshot_routes_authentication_and_scope(
    sessions, db, config, monkeypatch, case
):
    _install(monkeypatch, config)
    snapshot = support.snapshot_at(
        assaf._snapshot(config), datetime.now(UTC), marker="private to the owner"
    )
    cache.remember_snapshot(config, OWNER, snapshot)
    assert support.row_count(db) == 1
    reads = _Spy(cache.read_cached_snapshot)
    monkeypatch.setattr(assaf_os_api, "read_cached_snapshot", reads)

    with timing._browser(sessions, config) as browser:
        headers = dict(ORIGIN)
        if case not in {"no_cookie", "unknown_cookie"}:
            auth._login(browser)
            same_session = browser.get(CACHED, headers=ORIGIN)
            assert same_session.json()["cached"] is True, "valid until the case breaks it"
            reads.calls.clear()
        if case == "unknown_cookie":
            browser.cookies.set(
                owner_sessions_api.SESSION_COOKIE, "u" * 43, domain=auth.ORIGIN.split("//")[1]
            )
        if case == "expired_session":
            past = datetime.now(UTC) - timedelta(minutes=5)
            timing._expire(sessions, "UPDATE owner_os_sessions SET expires_at = :t", t=past)
        if case == "revoked_session":
            timing._expire(
                sessions, "UPDATE owner_os_sessions SET revoked_at = :t", t=datetime.now(UTC)
            )
        if case == "removed_owner":
            config.telegram_owner_user_ids = "99999999"
        if case == "scope_removed":
            config.owner_os_session_scopes = auth.READ_SCOPE
        if case == "feature_off":
            config.assaf_os_enabled = False
        if case == "wrong_origin":
            headers = {"Origin": "https://attacker.invalid"}
        response = browser.get(CACHED, headers=headers)

    assert response.status_code in {401, 403}, (case, response.status_code)
    assert "server-timing" not in response.headers, case
    assert response.json()["error"]["code"] in {
        "owner_auth_required",
        "owner_scope_denied",
        "origin_denied",
    }
    assert "private to the owner" not in response.text
    assert reads.calls == [], "a denied request never reads the cache"


def _principal_dependency():
    # Newer FastAPI wraps an included router lazily, so read the route from the router itself.
    route = next(r for r in assaf_os_api.router.routes if getattr(r, "path", "") == CACHED)
    return next(
        dependency.call
        for dependency in route.dependant.dependencies
        if dependency.call.__qualname__.startswith("owner_scope.")
    )


@pytest.mark.parametrize(
    ("case", "status", "code"),
    [
        ("feature_off", 404, "operation_unavailable"),
        ("owner_removed", 404, "operation_unavailable"),
        ("script_url_not_https", 503, "operation_unavailable"),
        ("secret_too_short", 503, "operation_unavailable"),
        ("workbook_malformed", 503, "operation_unavailable"),
    ],
)
def test_the_binding_is_reproved_with_the_snapshot_routes_404_and_503(
    sessions, db, config, monkeypatch, case, status, code
):
    cache.remember_snapshot(
        config, OWNER, support.snapshot_at(assaf._snapshot(config), datetime.now(UTC))
    )
    reads = _Spy(cache.read_cached_snapshot)
    app = timing._app(sessions, config)
    app.dependency_overrides[_principal_dependency()] = lambda: OwnerSessionPrincipal(
        owner_id="99999999" if case == "owner_removed" else OWNER,
        scopes=frozenset({"assaf-os:read"}),
        session_hash="h" * 64,
    )
    if case == "feature_off":
        config.assaf_os_enabled = False
    elif case == "script_url_not_https":
        config.assaf_os_script_url = "http://script.google.com/macros/s/" + "x" * 24 + "/exec"
    elif case == "secret_too_short":
        config.assaf_os_hmac_secret = "short"
    elif case == "workbook_malformed":
        config.assaf_os_workbook_id = "bad"
    monkeypatch.setattr(assaf_os_api, "read_cached_snapshot", reads)
    with TestClient(app, base_url=auth.ORIGIN) as browser:
        response = browser.get(CACHED, headers=ORIGIN)
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert reads.calls == []


# ------------------------------------------------------------------------ owner isolation


def test_owner_b_never_reads_owner_a_s_row(sessions, db, config, monkeypatch):
    _install(monkeypatch, config)
    with timing._browser(sessions, config) as alice:
        _login_as(alice, OWNER)
        assert alice.get(SNAPSHOT, headers=ORIGIN).status_code == 200
        assert alice.get(CACHED, headers=ORIGIN).json()["snapshot"]["issues"] == [f"owner-{OWNER}"]

        with timing._browser(sessions, config) as bob:
            _login_as(bob, OTHER)
            before = bob.get(CACHED, headers=ORIGIN)
            assert before.status_code == 200 and before.json() == {"cached": False}
            assert bob.get(SNAPSHOT, headers=ORIGIN).status_code == 200
            mine = bob.get(CACHED, headers=ORIGIN).json()
            assert mine["snapshot"]["issues"] == [f"owner-{OTHER}"]

        again = alice.get(CACHED, headers=ORIGIN).json()
        assert again["snapshot"]["issues"] == [f"owner-{OWNER}"]
    assert support.row_count(db) == 2


# ----------------------------------------------------------------------------- concurrency


def test_concurrent_writes_keep_the_newest_snapshot(sessions, db, config, monkeypatch):
    _install(monkeypatch, config)
    base = datetime.now(UTC) - timedelta(seconds=5)
    stamps = [base - timedelta(seconds=offset) for offset in range(8)]
    random.Random(7).shuffle(stamps)
    newest = max(stamps)
    barrier = threading.Barrier(len(stamps))

    def write(stamp: datetime) -> None:
        snapshot = support.snapshot_at(
            assaf._snapshot(config), stamp, marker=stamp.isoformat()
        )
        barrier.wait(timeout=10)
        cache.remember_snapshot(config, OWNER, snapshot)

    with ThreadPoolExecutor(max_workers=len(stamps)) as pool:
        list(pool.map(write, stamps))

    row = support.read_row(db, OWNER, config.assaf_os_workspace_id)
    assert support.row_count(db) == 1
    assert support.as_utc(row["as_of"]) == newest
    assert 1 <= row["version"] <= len(stamps)
    with timing._browser(sessions, config) as browser:
        auth._login(browser)
        hit = browser.get(CACHED, headers=ORIGIN).json()
    assert hit["snapshot"]["issues"] == [newest.isoformat()]
    assert hit["cache_version"] == row["version"]
