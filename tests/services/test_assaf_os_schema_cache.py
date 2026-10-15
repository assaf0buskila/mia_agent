"""V2-04: the process-local cache of the validated Assaf OS schema (contract section 3).

The schema is the same for every launch until the Apps Script is republished, and a republish is
always followed by a MIA restart. After the first validated reply the port serves a deep copy and
makes no Script POST. The cache is process-local, bounded (8 entries, LRU), expires after 24 h on
the monotonic clock, is never persisted, and never holds an error.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor

import pytest
from app.core.owner_timing import RequestTiming
from app.services import assaf_os as port

from tests.unit import test_assaf_os as assaf
from tests.unit import test_owner_sessions as auth

sessions = auth.sessions
DAY = 24 * 60 * 60


@pytest.fixture(autouse=True)
def clear_caches():
    port._clear_assaf_os_decision_binding_cache()
    port._clear_assaf_os_schema_cache()
    yield
    port._clear_assaf_os_decision_binding_cache()
    port._clear_assaf_os_schema_cache()


class Script:
    """A signed fake of the Apps Script that counts every POST that reaches it."""

    def __init__(self, config):
        self.config = config
        self.calls: list[str] = []

    def __call__(self, _url, body, _budget):
        request = json.loads(json.loads(body)["payload"])
        self.calls.append(request["operation"])
        if request["operation"] == "snapshot":
            result = assaf._snapshot(self.config)
        else:
            result = self.schema(request)
        return assaf.signed(body, result, self.config)

    def schema(self, request):
        return {
            **assaf._schema(self.config),
            "api_version": port.API_VERSION,
            "schema_version": port.SCHEMA_VERSION,
            "mode": self.config.assaf_os_mode,
            "workbook_id": request["workbook_id"],
            "workspace_id": request["workspace_id"],
            "tabs": [{"key": "tasks", "fields": [{"key": "title"}, {"key": "status"}]}],
            "enums": {"status": ["open", "done"]},
        }


def _port(config, owner=auth.OWNER_ID, source="OS"):
    return port.AssafOsPort(config, owner, actor_source=source)


def _install(monkeypatch, config):
    script = Script(config)
    monkeypatch.setattr(port, "_post", script)
    return script


class Clock:
    def __init__(self, monkeypatch, start=500.0):
        self.now = start
        monkeypatch.setattr(port.time, "monotonic", lambda: self.now)


# ------------------------------------------------------------------------------ the hit path


def test_constants_follow_the_contract():
    assert port._SCHEMA_CACHE_MAX == 8
    assert port._SCHEMA_CACHE_TTL_SECONDS == DAY


def test_second_schema_call_in_the_process_makes_zero_script_posts(monkeypatch):
    config = assaf.settings()
    script = _install(monkeypatch, config)
    first = _port(config).schema()
    assert script.calls == ["schema"]
    assert first["schema_version"] == port.SCHEMA_VERSION and first["tabs"]

    assert _port(config).schema() == first
    assert script.calls == ["schema"], "a hit never reaches the Script"
    # Any actor source with the same settings shares the entry: the schema is not per actor.
    assert _port(config, source="MIA").schema() == first
    assert _port(config, source="TELEGRAM_APPROVAL").schema() == first
    assert script.calls == ["schema"]


def test_only_the_schema_is_cached(monkeypatch):
    config = assaf.settings()
    script = _install(monkeypatch, config)
    adapter = _port(config)
    adapter.schema()
    adapter.snapshot()
    adapter.snapshot()
    assert script.calls == ["schema", "snapshot", "snapshot"]
    assert len(port._SCHEMA_CACHE) == 1


def test_hit_and_miss_both_return_copies_the_caller_can_mutate(monkeypatch):
    config = assaf.settings()
    script = _install(monkeypatch, config)
    adapter = _port(config)

    miss = adapter.schema()
    pristine = json.loads(json.dumps(miss))
    miss["tabs"][0]["fields"].append({"key": "NOT_IN_THE_SCHEMA"})
    miss["enums"]["status"].append("tampered")
    miss["mode"] = "LIVE"

    hit = adapter.schema()
    assert hit == pristine and script.calls == ["schema"]
    hit["tabs"].clear()
    hit["enums"].clear()
    assert adapter.schema() == pristine, "mutating a hit never reaches the cache"


def test_the_cache_never_bypasses_the_port_authorization(monkeypatch):
    config = assaf.settings()
    script = _install(monkeypatch, config)
    _port(config).schema()
    assert script.calls == ["schema"]

    with pytest.raises(port.AssafOsError) as stranger:
        _port(config, owner="87654321")
    assert stranger.value.code == "operation_unavailable" and stranger.value.status == 404

    disabled = config.model_copy(update={"assaf_os_enabled": False})
    with pytest.raises(port.AssafOsError) as off:
        _port(disabled)
    assert off.value.code == "operation_unavailable"

    short_secret = config.model_copy(update={"assaf_os_hmac_secret": "too-short"})
    with pytest.raises(port.AssafOsError) as weak:
        _port(short_secret)
    assert weak.value.code == "operation_unavailable"
    assert script.calls == ["schema"]


# --------------------------------------------------------------------------- the key parts


KEY_PARTS = (
    "owner_id",
    "workspace_id",
    "workbook_id",
    "mode",
    "script_url",
    "hmac_secret",
    "api_version",
    "schema_version",
)


@pytest.mark.parametrize("part", KEY_PARTS)
def test_changing_any_one_key_part_misses_and_leaves_the_other_entry_alone(part, monkeypatch):
    base = assaf.settings()
    script = _install(monkeypatch, base)
    assert _port(base).schema() and script.calls == ["schema"]

    changed, owner = base.model_copy(), auth.OWNER_ID
    if part == "owner_id":
        changed.telegram_owner_user_ids = f"{auth.OWNER_ID},87654321"
        owner = "87654321"
    elif part == "workspace_id":
        changed.assaf_os_workspace_id = "another-workspace"
    elif part == "workbook_id":
        changed.assaf_os_workbook_id = "workbook_other_1234567890123"
    elif part == "mode":
        changed.assaf_os_mode = "LIVE"
    elif part == "script_url":
        changed.assaf_os_script_url = "https://script.google.com/macros/s/" + "y" * 24 + "/exec"
    elif part == "hmac_secret":
        changed.assaf_os_hmac_secret = "another-secret-that-is-at-least-32-bytes"

    with monkeypatch.context() as patched:
        if part == "api_version":
            patched.setattr(port, "API_VERSION", "assaf-os.test-other")
        if part == "schema_version":
            patched.setattr(port, "SCHEMA_VERSION", "assaf-os-unified.test-other")
        script.config = changed
        assert _port(changed, owner).schema()
        assert script.calls == ["schema", "schema"], f"{part} must be part of the cache key"
        assert _port(changed, owner).schema()
        assert script.calls == ["schema", "schema"], f"{part} variant is itself cached"
    script.config = base
    assert _port(base).schema()
    assert script.calls == ["schema", "schema"], "the original entry survived the variant"
    assert len(port._SCHEMA_CACHE) == 2


def test_the_key_is_exactly_the_contract_tuple(monkeypatch):
    config = assaf.settings()
    _install(monkeypatch, config)
    _port(config).schema()
    (key,) = port._SCHEMA_CACHE
    assert key == (
        auth.OWNER_ID,
        config.assaf_os_workspace_id,
        config.assaf_os_workbook_id,
        config.assaf_os_mode,
        config.assaf_os_script_url,
        hashlib.sha256(config.assaf_os_hmac_secret.encode()).digest(),
        port.API_VERSION,
        port.SCHEMA_VERSION,
    )
    assert config.assaf_os_hmac_secret.encode() not in repr(key).encode(), "never the secret"


# -------------------------------------------------------------------- what is never stored


def test_errors_and_invalid_replies_are_never_cached(monkeypatch):
    config = assaf.settings()
    script = _install(monkeypatch, config)
    good = script

    def unavailable(_url, _body, _budget):
        script.calls.append("schema")
        raise port.AssafOsError("source_unavailable")

    monkeypatch.setattr(port, "_post", unavailable)
    with pytest.raises(port.AssafOsError):
        _port(config).schema()
    assert script.calls == ["schema"] and not port._SCHEMA_CACHE

    def wrong_version(_url, body, _budget):
        script.calls.append("schema")
        reply = good.schema(json.loads(json.loads(body)["payload"]))
        reply["schema_version"] = "assaf-os-unified.v0"
        return assaf.signed(body, reply, config)

    monkeypatch.setattr(port, "_post", wrong_version)
    with pytest.raises(port.AssafOsError) as invalid:
        _port(config).schema()
    assert invalid.value.code == "source_unavailable" and not port._SCHEMA_CACHE
    assert script.calls == ["schema", "schema"]

    def unsigned(_url, body, _budget):
        script.calls.append("schema")
        reply = good.schema(json.loads(json.loads(body)["payload"]))
        return assaf.signed(body, reply, config, valid_signature=False)

    monkeypatch.setattr(port, "_post", unsigned)
    with pytest.raises(port.AssafOsError):
        _port(config).schema()
    assert not port._SCHEMA_CACHE

    monkeypatch.setattr(port, "_post", good)
    assert _port(config).schema()
    assert script.calls == ["schema", "schema", "schema", "schema"]
    assert len(port._SCHEMA_CACHE) == 1


def test_test_hook_clears_the_cache(monkeypatch):
    config = assaf.settings()
    script = _install(monkeypatch, config)
    _port(config).schema()
    _port(config).schema()
    assert script.calls == ["schema"]
    port._clear_assaf_os_schema_cache()
    assert not port._SCHEMA_CACHE
    _port(config).schema()
    assert script.calls == ["schema", "schema"]


# ------------------------------------------------------------------------ TTL and clock


def test_ttl_is_24_hours_on_the_monotonic_clock(monkeypatch):
    config = assaf.settings()
    script = _install(monkeypatch, config)
    clock = Clock(monkeypatch)
    adapter = _port(config)
    adapter.schema()
    clock.now = 500.0 + DAY - 0.001
    adapter.schema()
    assert script.calls == ["schema"], "still warm just before 24 h"
    clock.now = 500.0 + DAY
    adapter.schema()
    assert script.calls == ["schema", "schema"], "expired exactly at 24 h"
    clock.now = 500.0 + DAY + 100.0
    adapter.schema()
    assert script.calls == ["schema", "schema"], "the refreshed entry has its own 24 h"
    clock.now = 500.0 + 2 * DAY + 0.5
    adapter.schema()
    assert script.calls == ["schema", "schema", "schema"]


def test_the_wall_clock_does_not_expire_the_entry(monkeypatch):
    config = assaf.settings()
    script = _install(monkeypatch, config)
    Clock(monkeypatch)
    adapter = _port(config)
    adapter.schema()
    monkeypatch.setattr(port.time, "time", lambda: 9_999_999_999.0)
    adapter.schema()
    assert script.calls == ["schema"]


def test_a_clock_that_goes_backwards_or_is_not_finite_never_serves_a_stale_entry(monkeypatch):
    config = assaf.settings()
    script = _install(monkeypatch, config)
    clock = Clock(monkeypatch)
    adapter = _port(config)
    adapter.schema()
    clock.now = 499.0
    adapter.schema()
    assert script.calls == ["schema", "schema"], "monotonic time went backwards: drop the entry"

    for bad in (float("nan"), float("inf")):
        port._clear_assaf_os_schema_cache()
        clock.now = 500.0
        adapter.schema()
        before = len(script.calls)
        clock.now = bad
        adapter.schema()
        assert len(script.calls) == before + 1, "a non-finite clock is never a hit"

    clock.now = float("inf")
    port._clear_assaf_os_schema_cache()
    adapter.schema()
    assert not port._SCHEMA_CACHE, "an entry that could never expire is not stored"


# ------------------------------------------------------------------------ LRU and threads


def _workspace(config, index):
    return config.model_copy(update={"assaf_os_workspace_id": f"workspace-{index:03d}"})


def test_cache_holds_at_most_eight_entries_and_evicts_the_least_recently_used(monkeypatch):
    base = assaf.settings()
    script = _install(monkeypatch, base)
    for index in range(8):
        _port(_workspace(base, index)).schema()
    assert len(port._SCHEMA_CACHE) == 8 and len(script.calls) == 8

    _port(_workspace(base, 0)).schema()  # a hit refreshes recency of the oldest entry
    assert len(script.calls) == 8
    _port(_workspace(base, 8)).schema()  # the ninth key evicts workspace-001, not workspace-000
    assert len(port._SCHEMA_CACHE) == 8 and len(script.calls) == 9

    _port(_workspace(base, 0)).schema()
    assert len(script.calls) == 9, "workspace-000 survived because the hit made it recent"
    _port(_workspace(base, 1)).schema()
    assert len(script.calls) == 10, "workspace-001 was the least recently used"
    assert len(port._SCHEMA_CACHE) == 8


def test_cache_lock_is_not_held_while_the_script_is_called(monkeypatch):
    config = assaf.settings()
    script = Script(config)

    def probe(url, body, budget):
        acquired = port._SCHEMA_CACHE_LOCK.acquire(blocking=False)
        assert acquired, "the transport must run without the cache lock"
        port._SCHEMA_CACHE_LOCK.release()
        return script(url, body, budget)

    monkeypatch.setattr(port, "_post", probe)
    _port(config).schema()
    assert script.calls == ["schema"]


def test_concurrent_callers_never_exceed_the_bound_or_return_a_foreign_schema(monkeypatch):
    base = assaf.settings()
    script = _install(monkeypatch, base)

    def exercise(index):
        workspace = index % 20
        result = _port(_workspace(base, workspace)).schema()
        assert result["workspace_id"] == f"workspace-{workspace:03d}"
        assert result["workbook_id"] == base.assaf_os_workbook_id
        return result["workspace_id"]

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(exercise, range(400)))
    assert len(results) == 400
    assert len(port._SCHEMA_CACHE) <= port._SCHEMA_CACHE_MAX
    assert 20 <= len(script.calls) < 400, "the cache absorbed most of the traffic"


# --------------------------------------------------------------------- the owner OS route


def test_a_cached_schema_route_has_no_script_phase_in_server_timing(sessions, monkeypatch, caplog):
    config = assaf.settings()
    script = _install(monkeypatch, config)
    with assaf.client(sessions, config) as browser:
        auth._login(browser)
        with caplog.at_level(logging.INFO, logger="mia.owner_timing"):
            cold = browser.get("/v1/owner/os/unified/schema", headers={"Origin": auth.ORIGIN})
            caplog.clear()
            warm = browser.get("/v1/owner/os/unified/schema", headers={"Origin": auth.ORIGIN})
    assert cold.status_code == warm.status_code == 200
    assert warm.json() == cold.json() and script.calls == ["schema"]

    def phases(response):
        return set(re.findall(r"([a-z]+);dur=\d+", response.headers.get("server-timing", "")))

    assert {"auth", "script", "verify"} <= phases(cold)
    assert "script" not in phases(warm) and "auth" in phases(warm)
    lines = [r.getMessage() for r in caplog.records if r.name == "mia.owner_timing"]
    assert lines and not any("metric=script" in line for line in lines), lines


def test_a_cache_hit_adds_no_script_time_to_the_active_request(monkeypatch):
    config = assaf.settings()
    _install(monkeypatch, config)
    _port(config).schema()
    timing = RequestTiming()
    timing.authenticated = True
    with timing.activate():
        _port(config).schema()
    assert timing.header(200) == ""
