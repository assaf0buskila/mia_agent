"""Regression coverage for operator probes' origin-bound website writes."""

from __future__ import annotations

import runpy

import pytest


def test_local_website_probe_sends_origin_for_session_and_messages(monkeypatch) -> None:
    module = runpy.run_path("scripts/probe_website_flow.py")
    calls: list[dict[str, object]] = []

    class Response:
        def __init__(self, path: str):
            self.path = path

        def json(self):
            if self.path == "/v1/website/sessions":
                return {"session_id": "session"}
            return {"next_action": "continue", "message": "reply"}

    class Client:
        def __init__(self, _app):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, _path, **kwargs):
            calls.append(kwargs)
            return Response(_path)

    globals_ = module["main"].__globals__
    monkeypatch.setitem(globals_, "init_db", lambda: None)
    monkeypatch.setitem(globals_, "TestClient", Client)
    assert module["main"]() == 0
    assert calls
    assert all(call["headers"] == {"Origin": module["WEBSITE_ORIGIN"]} for call in calls)


def test_live_probe_post_includes_allowed_origin(monkeypatch) -> None:
    module = runpy.run_path("scripts/probe_live_website.py")
    request_headers: dict[str, str] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(request, timeout):
        request_headers.update(dict(request.header_items()))
        assert timeout == 45
        return Response()

    post = module["_post"]
    monkeypatch.setitem(post.__globals__["urllib"].request.__dict__, "urlopen", fake_urlopen)
    assert post("https://mia.example/sessions", None) == {}
    assert request_headers["Origin"] == module["WEBSITE_ORIGIN"]


def test_ecs_revision_script_rejects_plaintext_env_option(monkeypatch) -> None:
    module = runpy.run_path("scripts/deploy_ecs_revision.py")
    monkeypatch.setattr(
        "sys.argv", ["deploy_ecs_revision.py", "--tag", "21", "--env", "X=y"]
    )
    monkeypatch.setattr(
        module["subprocess"],
        "run",
        lambda *_args, **_kwargs: type("Result", (), {"returncode": 0})(),
    )
    with pytest.raises(SystemExit) as exc_info:
        module["main"]()
    assert exc_info.value.code == 2


def test_eval_diff_includes_calendar_and_routing() -> None:
    module = runpy.run_path("scripts/eval_diff.py")
    assert {"calendar", "routing"} <= set(module["RUNNERS"])


def test_ecs_migration_script_pins_override_to_mia_migrate(monkeypatch) -> None:
    module = runpy.run_path("scripts/run_ecs_migration.py")
    calls: list[tuple[str, ...]] = []

    def fake_aws(*args: str) -> dict:
        calls.append(args)
        if args[1] == "describe-services":
            return {"services": [{"networkConfiguration": {"awsvpcConfiguration": {}}}]}
        if args[1] == "run-task":
            return {"tasks": [{"taskArn": "task-arn"}]}
        return {"tasks": [{"lastStatus": "STOPPED", "containers": [{"exitCode": 0}]}]}

    monkeypatch.setattr(
        "sys.argv", ["run_ecs_migration.py", "--task-definition", "mia:21"]
    )
    monkeypatch.setitem(module["main"].__globals__, "_aws", fake_aws)
    module["main"]()

    run_task = next(args for args in calls if args[1] == "run-task")
    overrides = run_task[run_task.index("--overrides") + 1]
    assert module["json"].loads(overrides) == {
        "containerOverrides": [{"name": "mia", "command": ["mia-migrate"]}]
    }


def test_ecs_migration_script_rejects_command_override(monkeypatch) -> None:
    module = runpy.run_path("scripts/run_ecs_migration.py")
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_ecs_migration.py",
            "--task-definition",
            "mia:21",
            "--command",
            "python -m app.main",
        ],
    )
    with pytest.raises(SystemExit) as exc_info:
        module["main"]()
    assert exc_info.value.code == 2
