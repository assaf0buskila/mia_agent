"""Fixtures for the Assaf OS v2 route tests. V2-16s creates the directory; chunks extend it."""

from __future__ import annotations

import importlib

import pytest

# Every v2 router V2-16s mounts, as (module, mount prefix). Each chunk fills its own module.
V2_ROUTER_MODULES = (
    ("app.api.os_v2.reads", ""),
    ("app.api.os_v2.commits", ""),
    ("app.api.os_v2.statements", ""),
    ("app.api.os_v2.credentials", ""),
    ("app.api.owner_step_up", "/v1/owner/auth"),
    ("app.api.assistant.os", ""),
    ("app.api.assistant.mcp", ""),
)


@pytest.fixture
def v2_routers():
    return [(importlib.import_module(name).router, prefix) for name, prefix in V2_ROUTER_MODULES]
