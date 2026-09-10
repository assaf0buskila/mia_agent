from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from app.core.risk import RiskLevel


class _Session:
    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def rollback(self) -> None:
        return None


class _Catalog:
    def __enter__(self) -> _Catalog:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def active_toolkits(self) -> list[str]:
        return ["LINKEDIN"]

    def search(self, _query: str, toolkit: str, *, limit: int) -> list[SimpleNamespace]:
        assert toolkit == "LINKEDIN"
        assert limit == 50
        return [
            SimpleNamespace(
                slug="LINKEDIN_GET_MY_INFO",
                toolkit="LINKEDIN",
                input_schema={},
            )
        ]

    def detail(self, slug: str) -> SimpleNamespace:
        assert slug == "LINKEDIN_GET_MY_INFO"
        return SimpleNamespace(
            slug=slug,
            toolkit="LINKEDIN",
            input_schema={},
        )

    def execute_read(
        self, _tool: SimpleNamespace, _arguments: dict[str, object]
    ) -> dict[str, object]:
        return {
            "successful": True,
            "data": {
                "name": "Assaf Web",
                "headline": "Builder",
                "about": "Builds practical systems",
            },
        }


def _load_probe() -> ModuleType:
    path = Path("scripts/probe_owner_tools.py")
    spec = importlib.util.spec_from_file_location("test_probe_owner_tools", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wire_probe(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    *,
    result_by_tool: dict[str, SimpleNamespace] | None = None,
    catalog: _Catalog | None = None,
) -> None:
    settings = SimpleNamespace(
        kill_switch=False,
        resolved_sheets_spreadsheet_id=lambda: "sheet-id",
    )
    success = SimpleNamespace(
        ok=True,
        text="Fresh provider result",
        error="",
        outcome_label=lambda: "success",
    )
    results = result_by_tool or {}
    monkeypatch.setattr(module.logging, "disable", lambda _level: None)
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(module, "bind_owner_house_ports", lambda _settings: {})
    monkeypatch.setattr(module, "get_session_factory", lambda: _Session)
    monkeypatch.setattr(module, "LeadStore", lambda _db: object())
    monkeypatch.setattr(module, "BrainStore", lambda _db: object())
    monkeypatch.setattr(module, "build_embedding_port", lambda _settings: object())
    monkeypatch.setattr(module, "ToolContext", lambda **_kwargs: object())
    monkeypatch.setattr(module, "tool_names", lambda: ["linkedin_snapshot"])
    monkeypatch.setattr(
        module,
        "execute_tool",
        lambda name, _args, _ctx: results.get(name, success),
    )
    monkeypatch.setattr(
        module.ComposioCatalog,
        "from_settings",
        classmethod(lambda _cls, _settings: catalog),
    )
    monkeypatch.setattr(module, "schema_text", lambda _tool: "{}")
    monkeypatch.setattr(module, "risk_for_slug", lambda _slug, _toolkit: RiskLevel.R0_READ)
    monkeypatch.setattr(module, "validate_arguments", lambda _schema, _args: [])


@pytest.mark.parametrize(
    "failed_result",
    [
        SimpleNamespace(
            ok=False,
            text="",
            error="provider failed",
            outcome_label=lambda: "failure",
        ),
        SimpleNamespace(
            ok=True,
            text="Not connected yet.",
            error="",
            outcome_label=lambda: "success",
        ),
        SimpleNamespace(
            ok=True,
            text="Partial provider result",
            error="",
            outcome_label=lambda: "partial",
        ),
    ],
    ids=["failed-read", "unavailable-read", "partial-read"],
)
def test_probe_returns_one_when_any_read_fails_or_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failed_result: SimpleNamespace,
) -> None:
    module = _load_probe()
    _wire_probe(
        monkeypatch,
        module,
        result_by_tool={"gmail_inbox": failed_result},
        catalog=_Catalog(),
    )

    assert module.main() == 1
    assert '"result": "FAIL"' in capsys.readouterr().out


def test_probe_returns_one_when_catalog_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_probe()
    _wire_probe(monkeypatch, module, catalog=None)

    assert module.main() == 1
    output = capsys.readouterr().out
    assert '"kind": "catalog", "ok": false' in output
    assert '"result": "FAIL"' in output


def test_probe_returns_zero_for_successful_reads_catalog_and_profile(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _load_probe()
    _wire_probe(monkeypatch, module, catalog=_Catalog())

    assert module.main() == 0
    output = capsys.readouterr().out
    assert '"kind": "linkedin_fields", "ok": true' in output
    assert '"failures": 0, "result": "PASS"' in output
