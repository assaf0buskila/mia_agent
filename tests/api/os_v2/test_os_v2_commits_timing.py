"""OS-RUNTIME F5 on the V2-24 owner-direct routes: Server-Timing ``ledger`` and named summary ops.

The commit, undo, abandon and deleted routes call the ledger directly (not through
``PostgresOsBackend``), so they time the ledger call themselves, and the boundary's summary line
names them instead of logging ``other``.
"""

from __future__ import annotations

import logging
import re

import pytest

from tests.api.os_v2 import test_os_v2_commits as commits_tests
from tests.api.os_v2.test_os_v2_commits import ROOT, create_body, post

# The V2-24 route harness, reused as fixtures.
sessions = commits_tests.sessions
world = commits_tests.world

_PART = re.compile(r"(auth|db|script|ledger|verify|total);dur=(\d+)")


def _metrics(response) -> list[str]:
    (value,) = response.headers.get_list("server-timing")
    names = []
    for part in value.split(", "):
        match = _PART.fullmatch(part)
        assert match is not None, value
        names.append(match.group(1))
    return names


def _lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "mia.owner_timing"]


def test_the_owner_direct_routes_time_the_ledger_and_name_their_op(world, caplog):
    with caplog.at_level(logging.INFO, logger="mia.owner_timing"):
        created = post(world, "/commits", create_body())
        deleted = world.browser.get(ROOT + "/deleted", headers=world.headers)
    for response in (created, deleted):
        assert response.status_code == 200, response.text
        metrics = _metrics(response)
        assert "ledger" in metrics and metrics[-1] == "total", metrics
    lines = _lines(caplog)
    assert any("stage=os_commit metric=ledger" in line for line in lines), lines
    assert any("stage=os_read metric=ledger" in line for line in lines), lines
    ops = [line.split("op=")[1].split()[0] for line in lines if line.startswith("owner_os_request")]
    assert ops == ["commit", "deleted"], ops


@pytest.mark.parametrize(
    ("method", "path", "op"),
    [
        ("POST", ROOT + "/commits", "commit"),
        ("POST", ROOT + "/commits/12345678-1234-4234-8234-123456789012/undo", "undo"),
        ("POST", ROOT + "/commits/12345678-1234-4234-8234-123456789012/abandon", "abandon"),
        ("GET", ROOT + "/deleted", "deleted"),
        ("GET", ROOT + "/commits", "other"),
        ("POST", ROOT + "/commits/a/b/undo", "other"),
    ],
)
def test_the_owner_direct_ops_are_fixed_names(method, path, op):
    from app.api.owner_os_boundary import request_op

    assert request_op(method, path) == op
