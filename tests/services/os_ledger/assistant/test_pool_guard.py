"""P1: three accounting slots reserve capacity for owner traffic in the shared pool."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager

import pytest
from app.services import assistant_keys as keys
from app.services.os_ledger.api import LedgerError


def test_fourth_bearer_is_bounded_retryable_and_never_enters_the_store(aw, caplog):
    callers = [
        aw.authenticate(aw.issue(label=str(i))["secret"], ip=f"203.0.113.{i}") for i in range(4)
    ]
    inside = threading.Barrier(4)
    release = threading.Event()
    errors = []

    def work(facts):
        inside.wait(5)
        assert release.wait(6)
        return {}

    def run(caller):
        try:
            aw.gateway.run(caller, "context", work)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(caller,)) for caller in callers[:3]]
    for thread in threads:
        thread.start()
    try:
        inside.wait(5)
        aw.store.calls.clear()
        started = time.monotonic()
        with pytest.raises(LedgerError) as caught:
            aw.gateway.run(callers[3], "context", lambda facts: {})
        assert 1.8 <= time.monotonic() - started < 3
        error = caught.value
        assert (error.code, error.status, error.retryable, error.write_outcome) == (
            "ledger_unavailable",
            503,
            True,
            "not_started",
        )
        assert error.reason == "bearer_busy" and "bearer_busy" in caplog.text
        assert not aw.store.calls and aw.audit() == []
        # Owner key-list never takes the guard, and a ledger read still has pool capacity.
        assert len(aw.listing()["credentials"]) == 4
        from unittest.mock import patch

        from app.api.os_v2 import owner_keys
        from app.services.os_ledger import api

        from tests.api.os_v2.v2_19_support import OwnerApp, owner_settings

        with OwnerApp(owner_settings(assaf_os_assistant_enabled=True)) as owner:
            headers = owner.login()
            with (
                patch.object(api, "build_ledger", lambda settings: aw.w.ledger),
                patch.object(owner_keys, "service_for", lambda settings: aw.service),
            ):
                for path in (
                    "/v1/owner/os/unified/pending",
                    "/v1/owner/os/unified/assistant-credentials",
                ):
                    response = owner.client.get(path, headers=headers)
                    assert response.status_code == 200, response.text
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
    assert errors == []


@pytest.mark.parametrize("exit_path", ["success", "work_error", "admit", "record", "commit"])
def test_guard_is_released_on_every_exit_path(fake_aw, monkeypatch, exit_path):
    aw = fake_aw
    caller = aw.authenticate(aw.issue()["secret"])
    semaphore = threading.BoundedSemaphore(1)
    monkeypatch.setattr(keys, "_bearer_slots", semaphore)

    @contextmanager
    def fault():
        if exit_path in {"admit", "record", "commit"}:
            with aw.accounting_fault(exit_path, caller.credential.credential_id):
                yield
        else:
            yield

    def work(facts):
        if exit_path == "work_error":
            raise LedgerError("validation_error")
        return {}

    with fault():
        if exit_path in {"work_error", "admit"}:
            with pytest.raises(LedgerError):
                aw.gateway.run(caller, "context", work)
        else:
            assert aw.gateway.run(caller, "context", work) == {}
    assert semaphore.acquire(blocking=False)
    semaphore.release()
