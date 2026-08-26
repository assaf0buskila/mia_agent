"""Preloaded Composio/direct tool pin registry — catalog only, no discovery."""

from __future__ import annotations

import ast
from pathlib import Path

from app.core.capabilities import CapabilityId, require_alive
from app.core.risk import RiskLevel
from app.core.write_flags import named_write_may_auto
from app.integrations.calendar import COMPOSIO_GOOGLECALENDAR_VERSION
from app.tools.registries.mia_preloaded_tools import (
    PRELOADED_TOOL_NAMES,
    PRELOADED_TOOLS,
    preloaded_tool,
)

_MODULE = Path("app/tools/registries/mia_preloaded_tools.py")
_READ_NAME_MARKERS = (
    "FETCH",
    "LIST",
    "GET",
    "INSIGHTS",
    "MY_INFO",
    "FIND_FREE_SLOTS",
    "QUERY",
    "PIVOT",
    "INSPECT",
    "CONVERSION",
)


def test_preloaded_tool_names_unique() -> None:
    names = [t.name for t in PRELOADED_TOOLS]
    assert len(names) == len(set(names))


def test_read_pins_are_not_writes() -> None:
    for tool in PRELOADED_TOOLS:
        upper = tool.name.upper()
        if any(marker in upper for marker in _READ_NAME_MARKERS):
            assert tool.write is False, tool.name


def test_calendar_create_and_patch_are_writes() -> None:
    create = preloaded_tool("GOOGLECALENDAR_CREATE_EVENT")
    patch = preloaded_tool("GOOGLECALENDAR_PATCH_EVENT")
    assert create is not None and create.write is True
    assert patch is not None and patch.write is True


def test_no_send_delete_pause_in_pin_names() -> None:
    for name in PRELOADED_TOOL_NAMES:
        upper = name.upper()
        # ADR-016: WhatsApp outbound has one owner selected by MIA_WHATSAPP_SENDER, and
        # production runs `composio`, so this send pin is expected. It is invoked by the
        # adapter, never offered to the owner model. Exemption matches production.
        if name == "INSTAGRAM_SEND_TEXT_MESSAGE" or name == "WHATSAPP_SEND_MESSAGE":
            continue
        assert "SEND" not in upper
        assert "DELETE" not in upper
        assert "PAUSE" not in upper


def test_instagram_send_pin_is_write_not_publish() -> None:
    send = preloaded_tool("INSTAGRAM_SEND_TEXT_MESSAGE")
    assert send is not None
    assert send.write is True
    assert send.risk == "R2"
    assert preloaded_tool("INSTAGRAM_CREATE_POST") is None
    assert preloaded_tool("INSTAGRAM_CREATE_MEDIA_CONTAINER") is None


def test_unknown_preloaded_tool_is_none() -> None:
    assert preloaded_tool("GMAIL_SEND") is None


def test_create_event_version_matches_calendar_module() -> None:
    tool = preloaded_tool("GOOGLECALENDAR_CREATE_EVENT")
    assert tool is not None
    assert tool.version == COMPOSIO_GOOGLECALENDAR_VERSION


def test_module_has_no_discovery_catalog() -> None:
    source = _MODULE.read_text(encoding="utf-8")
    lowered = source.lower()
    assert "tools/list" not in lowered
    assert "backend.composio.dev" not in lowered
    assert "httpx" not in lowered
    assert "requests." not in lowered
    assert "from app.api" not in source


def test_require_alive_preloaded_tools() -> None:
    require_alive(CapabilityId.PRELOADED_TOOLS)


def test_named_write_may_auto_r4_still_false() -> None:
    assert named_write_may_auto(enabled=True, risk=RiskLevel.R4_FINANCIAL_MARKETING) is False


def test_registry_module_has_no_dynamic_discovery_ast() -> None:
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and "discover" in node.name.lower():
            raise AssertionError(f"discovery function {node.name} must not exist")
