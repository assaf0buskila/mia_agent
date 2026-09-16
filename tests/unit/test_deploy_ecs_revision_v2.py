from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "deploy_ecs_revision_v2", ROOT / "scripts" / "deploy_ecs_revision.py"
)
assert SPEC and SPEC.loader
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)

DIGEST = "sha256:" + "a" * 64
IMAGE = f"123456789012.dkr.ecr.eu-north-1.amazonaws.com/mia@{DIGEST}"
SHA = "b" * 40
CONFIG_DIGEST = "sha256:" + "c" * 64


def _task() -> dict:
    return {
        "family": "mia",
        "containerDefinitions": [
            {
                "name": "mia",
                "image": "old/mia:1",
                "environment": [
                    {"name": "MIA_SALES_GEMINI_MODEL", "value": "gemini-safe"},
                    {"name": "MIA_WEBSITE_V2_ENABLED", "value": "false"},
                    {"name": "UNCHANGED", "value": "kept"},
                ],
                "secrets": [
                    {"name": "MIA_WHATSAPP_ACCESS_TOKEN", "valueFrom": "old-ref"},
                    {"name": "MIA_GEMINI_API_KEY", "valueFrom": "gemini-ref"},
                ],
            },
            {"name": "sidecar", "image": "sidecar:unchanged", "environment": []},
        ],
    }


def test_v2_payload_changes_only_mia_and_pins_digest() -> None:
    payload = deploy._v2_payload(_task(), image_uri=IMAGE, sha="commit-sha")
    mia, sidecar = payload["containerDefinitions"]
    assert mia["image"] == IMAGE
    assert sidecar["image"] == "sidecar:unchanged"
    env = {item["name"]: item["value"] for item in mia["environment"]}
    assert "MIA_WEBSITE_V2_ENABLED" not in env
    assert env["UNCHANGED"] == "kept"
    assert env["MIA_BUILD_SHA"] == "commit-sha"
    assert env["MIA_OWNER_AGENT_GEMINI_MODEL"] == "gemini-safe"
    assert env["MIA_GEMINI_TRANSCRIBE_MODEL"] == "gemini-safe"
    assert all(env[name] == "true" for name in deploy.ENABLED_V2_SETTINGS)
    assert mia["secrets"] == [{"name": "MIA_GEMINI_API_KEY", "valueFrom": "gemini-ref"}]


def test_v2_payload_removes_every_retired_reference_and_keeps_live_configuration() -> None:
    retired = {
        "MIA_OWNER_V2_ENABLED",
        "MIA_COMPOSIO_WEBHOOK_SECRET",
        "MIA_EXTRACTION_MODEL",
        "MIA_WEBSITE_INACTIVITY_MINUTES",
        "MIA_WHATSAPP_GRAPH_VERSION",
        "MIA_WHATSAPP_OWNER_PHONES",
        "MIA_WHATSAPP_REQUIRE_BUSINESS_SCOPE",
        "MIA_WHATSAPP_SENDER",
        "MIA_MANYCHAT_INGEST_TOKEN",
        "MIA_META_ADS_ACCOUNT_ID",
        "MIA_CAMPAIGN_MONTHLY_BUDGET",
        "MIA_CAMPAIGN_NAME",
        "MIA_CAMPAIGN_LAUNCH_DATE",
        "MIA_CAMPAIGN_OBJECTIVE",
        "MIA_CAMPAIGN_LEAD_PATH",
        "MIA_CAMPAIGN_E2E_TESTED",
        "MIA_DYNAMIC_TOOL_DISCOVERY",
        "MIA_WEBSITE_MEETING_FIRST",
        "MIA_AUTO_REPLY_INSTAGRAM",
        "MIA_AUTO_FOLLOWUP",
    }
    task = _task()
    mia = task["containerDefinitions"][0]
    mia["environment"].extend(
        [{"name": name, "value": "obsolete"} for name in sorted(retired)]
    )
    mia["environment"].append(
        {"name": "MIA_WHATSAPP_CLICK_TO_CHAT", "value": "https://wa.me/current"}
    )
    mia["secrets"].extend(
        [
            {"name": name, "valueFrom": f"retired-ref-{index}"}
            for index, name in enumerate(sorted(retired))
        ]
    )
    mia["secrets"].append(
        {"name": "MIA_COMPOSIO_API_KEY", "valueFrom": "current-composio-ref"}
    )

    payload = deploy._v2_payload(task, image_uri=IMAGE, sha="commit-sha")
    normalized = payload["containerDefinitions"][0]
    env = {item["name"]: item["value"] for item in normalized["environment"]}
    secrets = {item["name"]: item["valueFrom"] for item in normalized["secrets"]}

    assert retired <= deploy.RETIRED_NAMES
    assert retired.isdisjoint(env)
    assert retired.isdisjoint(secrets)
    assert env["MIA_WHATSAPP_CLICK_TO_CHAT"] == "https://wa.me/current"
    assert secrets["MIA_GEMINI_API_KEY"] == "gemini-ref"
    assert secrets["MIA_COMPOSIO_API_KEY"] == "current-composio-ref"


def test_existing_purpose_models_are_preserved_without_sales_fallback() -> None:
    task = _task()
    mia = task["containerDefinitions"][0]
    mia["environment"] = [
        {"name": "MIA_OWNER_AGENT_GEMINI_MODEL", "value": "owner-purpose"},
        {"name": "MIA_GEMINI_TRANSCRIBE_MODEL", "value": "audio-purpose"},
    ]
    payload = deploy._v2_payload(task, image_uri=IMAGE, sha="sha")
    env = {
        item["name"]: item["value"] for item in payload["containerDefinitions"][0]["environment"]
    }
    assert env["MIA_OWNER_AGENT_GEMINI_MODEL"] == "owner-purpose"
    assert env["MIA_GEMINI_TRANSCRIBE_MODEL"] == "audio-purpose"


def test_missing_purpose_model_and_fallback_refuses_revision() -> None:
    task = _task()
    task["containerDefinitions"][0]["environment"] = []
    with pytest.raises(SystemExit, match="provides no configured fallback"):
        deploy._v2_payload(task, image_uri=IMAGE, sha="sha")


def test_digest_uri_is_verified_in_ecr(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_aws(*args: str) -> dict:
        calls.append(args)
        if args[:2] == ("sts", "get-caller-identity"):
            return {"Account": "123456789012"}
        if args[:2] == ("ecr", "describe-images"):
            return {"imageDetails": [{"imageDigest": DIGEST}]}
        if args[:2] == ("ecr", "batch-get-image"):
            return {
                "images": [{
                    "imageId": {"imageDigest": DIGEST},
                    "imageManifest": '{"config":{"digest":"' + CONFIG_DIGEST + '"}}',
                }]
            }
        return {"downloadUrl": "https://ecr.invalid/config"}

    monkeypatch.setattr(deploy, "_aws", fake_aws)
    monkeypatch.setattr(
        deploy,
        "_download_json",
        lambda _url: {
            "config": {
                "Labels": {"org.opencontainers.image.revision": SHA},
                "Env": [f"MIA_BUILD_SHA={SHA}"],
            }
        },
    )
    assert deploy._verified_digest_image(IMAGE, SHA) == IMAGE
    assert calls[0][:2] == ("sts", "get-caller-identity")
    ecr_calls = [call for call in calls if call[0] == "ecr"]
    assert all(
        ("--region", "eu-north-1") == call[call.index("--region") :][:2]
        for call in ecr_calls
    )
    with pytest.raises(SystemExit, match="pinned"):
        deploy._verified_digest_image("repo:latest", SHA)


def test_digest_image_refuses_wrong_account_or_provenance(monkeypatch) -> None:
    monkeypatch.setattr(deploy, "_aws", lambda *_args: {"Account": "999999999999"})
    with pytest.raises(SystemExit, match="authenticated AWS account"):
        deploy._verified_digest_image(IMAGE, SHA)

    def fake_aws(*args: str) -> dict:
        if args[0] == "sts":
            return {"Account": "123456789012"}
        if args[:2] == ("ecr", "describe-images"):
            return {"imageDetails": [{"imageDigest": DIGEST}]}
        if args[:2] == ("ecr", "batch-get-image"):
            return {
                "images": [{
                    "imageId": {"imageDigest": DIGEST},
                    "imageManifest": '{"config":{"digest":"' + CONFIG_DIGEST + '"}}',
                }]
            }
        return {"downloadUrl": "https://ecr.invalid/config"}

    monkeypatch.setattr(deploy, "_aws", fake_aws)
    monkeypatch.setattr(
        deploy,
        "_download_json",
        lambda _url: {
            "config": {
                "Labels": {"org.opencontainers.image.revision": "d" * 40},
                "Env": [f"MIA_BUILD_SHA={SHA}"],
            }
        },
    )
    with pytest.raises(SystemExit, match="provenance"):
        deploy._verified_digest_image(IMAGE, SHA)


def _stable_service() -> dict:
    task_definition = "arn:aws:ecs:eu-north-1:123456789012:task-definition/mia:52"
    return {
        "services": [{
            "taskDefinition": task_definition,
            "desiredCount": 1,
            "runningCount": 1,
            "pendingCount": 0,
            "deployments": [{
                "status": "PRIMARY",
                "rolloutState": "COMPLETED",
                "taskDefinition": task_definition,
                "desiredCount": 1,
                "runningCount": 1,
                "pendingCount": 0,
            }],
        }]
    }


def test_running_base_is_the_single_healthy_serving_revision(monkeypatch) -> None:
    task_definition = _stable_service()["services"][0]["taskDefinition"]

    def fake_aws(*args: str) -> dict:
        if args[:2] == ("ecs", "describe-services"):
            return _stable_service()
        if args[:2] == ("ecs", "list-tasks"):
            return {"taskArns": ["task-1"]}
        return {
            "failures": [],
            "tasks": [{
                "taskDefinitionArn": task_definition,
                "lastStatus": "RUNNING",
                "healthStatus": "HEALTHY",
                "containers": [{
                    "name": "mia", "lastStatus": "RUNNING", "healthStatus": "HEALTHY"
                }],
            }],
        }

    monkeypatch.setattr(deploy, "_aws", fake_aws)
    assert deploy._running_task_definition() == task_definition


@pytest.mark.parametrize("unstable", ("pending", "mixed", "unhealthy"))
def test_running_base_refuses_unstable_or_mixed_service(monkeypatch, unstable: str) -> None:
    service = _stable_service()
    if unstable == "pending":
        service["services"][0]["pendingCount"] = 1
    elif unstable == "mixed":
        old = dict(service["services"][0]["deployments"][0])
        old.update(status="ACTIVE", taskDefinition="old", desiredCount=1, runningCount=1)
        service["services"][0]["deployments"].append(old)

    def fake_aws(*args: str) -> dict:
        if args[:2] == ("ecs", "describe-services"):
            return service
        if args[:2] == ("ecs", "list-tasks"):
            return {"taskArns": ["task-1"]}
        task_definition = service["services"][0]["taskDefinition"]
        return {
            "failures": [],
            "tasks": [{
                "taskDefinitionArn": task_definition,
                "lastStatus": "RUNNING",
                "healthStatus": "UNHEALTHY" if unstable == "unhealthy" else "HEALTHY",
                "containers": [{
                    "name": "mia", "lastStatus": "RUNNING", "healthStatus": "HEALTHY"
                }],
            }],
        }

    monkeypatch.setattr(deploy, "_aws", fake_aws)
    with pytest.raises(SystemExit, match="stable|unhealthy"):
        deploy._running_task_definition()


def test_aws_failure_does_not_echo_task_payload_or_stderr(monkeypatch) -> None:
    result = type(
        "Result",
        (),
        {"returncode": 1, "stdout": "", "stderr": "secret task payload"},
    )()
    monkeypatch.setattr(deploy.subprocess, "run", lambda *args, **kwargs: result)
    with pytest.raises(SystemExit) as exc:
        deploy._aws("ecs", "register-task-definition", "--cli-input-json", "secret")
    assert "secret" not in str(exc.value)
    assert "register-task-definition" in str(exc.value)
