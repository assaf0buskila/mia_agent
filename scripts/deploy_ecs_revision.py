"""Register a v2 ECS task revision from the task production currently serves.

The helper reads ECS/ECR and registers a revision. It never updates the service.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.request import urlopen

FAMILY = "mia"
CLUSTER = "mia"
MUTABLE_KEYS = (
    "family", "taskRoleArn", "executionRoleArn", "networkMode",
    "containerDefinitions", "volumes", "placementConstraints",
    "requiresCompatibilities", "cpu", "memory", "runtimePlatform",
    "ephemeralStorage",
)
RETIRED_NAMES = frozenset(
    {
        "MIA_WEBSITE_V2_ENABLED",
        "MIA_CRM_V2_ENABLED",
        "MIA_OWNER_V2_ENABLED",
        "MIA_COMPOSIO_WEBHOOK_SECRET",
        "MIA_EXTRACTION_MODEL",
        "MIA_WEBSITE_INACTIVITY_MINUTES",
        "MIA_WHATSAPP_ACCESS_TOKEN",
        "MIA_WHATSAPP_APP_SECRET",
        "MIA_WHATSAPP_VERIFY_TOKEN",
        "MIA_WHATSAPP_PHONE_NUMBER_ID",
        "MIA_WHATSAPP_BAILEYS_URL",
        "MIA_WHATSAPP_BAILEYS_TOKEN",
        "MIA_WHATSAPP_HANDOFF_SEND",
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
)
ENABLED_V2_SETTINGS = {
    "MIA_CRM_DELIVERY_ENABLED": "true",
    "MIA_GMAIL_SEND": "true",
    "MIA_CALENDAR_WRITE": "true",
    "MIA_COMPOSIO_DISCOVERY": "true",
}
PURPOSE_MODEL_SETTINGS = (
    "MIA_OWNER_AGENT_GEMINI_MODEL",
    "MIA_GEMINI_TRANSCRIBE_MODEL",
)
_DIGEST_IMAGE = re.compile(
    r"^(?P<account>\d{12})\.dkr\.ecr\.(?P<region>[a-z0-9-]+)\.amazonaws\.com/"
    r"(?P<repository>[^@\s]+)@(?P<digest>sha256:[0-9a-f]{64})$"
)
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def _aws(*args: str) -> dict:
    proc = subprocess.run(
        ["aws", *args, "--output", "json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        # ECS echoes service-event text back verbatim, and some of it is not valid UTF-8:
        # describe-services on the mia service currently returns sixteen raw 0xaa bytes
        # inside historical event messages quoting a provider error. Strict decoding raised
        # UnicodeDecodeError inside subprocess's reader thread, stdout came back None, and
        # the deploy died on a TypeError from json.loads with no usable message -- which
        # reads like a broken deploy rather than an undecodable event string. Every field
        # this script actually consumes (ARNs, image URIs, env names, SHAs) is ASCII, so
        # replacing the undecodable bytes loses nothing and keeps the JSON parseable.
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        operation = " ".join(args[:2])
        sys.exit(f"aws {operation} failed; provider details withheld")
    return json.loads(proc.stdout)


def _resolved_sha(requested: str) -> str:
    requested = requested.strip()
    if not requested:
        sys.exit("--sha is required: pass the tested commit")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    if head.returncode != 0:
        sys.exit("cannot determine HEAD; refusing to register a revision")
    head_sha = head.stdout.strip()
    if not head_sha.startswith(requested) and not requested.startswith(head_sha):
        sys.exit(f"--sha {requested} does not match HEAD {head_sha[:12]}")
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True, check=False
    )
    if dirty.returncode != 0:
        sys.exit("cannot determine tree state; refusing to register a revision")
    if dirty.stdout.strip():
        sys.exit("working tree is dirty; commit the tested tree before registering")
    return head_sha


def _running_task_definition() -> str:
    service = _aws("ecs", "describe-services", "--cluster", CLUSTER, "--services", FAMILY)
    services = service.get("services") or []
    if not services:
        sys.exit(f"no ECS service {FAMILY} in cluster {CLUSTER}; refusing to guess a base")
    current = services[0]
    desired = int(current.get("desiredCount") or 0)
    running = int(current.get("runningCount") or 0)
    pending = int(current.get("pendingCount") or 0)
    deployments = [
        item
        for item in current.get("deployments") or []
        if item.get("status") == "PRIMARY"
        or any(int(item.get(key) or 0) for key in ("desiredCount", "runningCount", "pendingCount"))
    ]
    if desired < 1 or running != desired or pending or len(deployments) != 1:
        sys.exit("ECS service is not on one stable serving deployment; refusing a mixed base")
    deployment = deployments[0]
    arn = str(deployment.get("taskDefinition") or "")
    if (
        deployment.get("rolloutState") != "COMPLETED"
        or int(deployment.get("pendingCount") or 0)
        or int(deployment.get("runningCount") or 0) != desired
        or arn != str(current.get("taskDefinition") or "")
    ):
        sys.exit("ECS primary deployment is not completed and serving consistently")
    listed = _aws(
        "ecs", "list-tasks", "--cluster", CLUSTER, "--service-name", FAMILY,
        "--desired-status", "RUNNING",
    )
    task_arns = [str(item) for item in listed.get("taskArns") or [] if item]
    if len(task_arns) != desired:
        sys.exit("running ECS task count does not match the stable service")
    described = _aws("ecs", "describe-tasks", "--cluster", CLUSTER, "--tasks", *task_arns)
    if described.get("failures") or len(described.get("tasks") or []) != desired:
        sys.exit("ECS did not return every running service task")
    for task in described["tasks"]:
        containers = [item for item in task.get("containers") or [] if item.get("name") == "mia"]
        if (
            task.get("taskDefinitionArn") != arn
            or task.get("lastStatus") != "RUNNING"
            or task.get("healthStatus") != "HEALTHY"
            or len(containers) != 1
            or containers[0].get("lastStatus") != "RUNNING"
            or containers[0].get("healthStatus") != "HEALTHY"
        ):
            sys.exit("running ECS tasks are unhealthy or use a different task definition")
    return arn


def _download_json(url: str) -> dict:
    try:
        with urlopen(url, timeout=15) as response:  # noqa: S310 - URL is issued by ECR.
            value = json.loads(response.read())
    except Exception:
        sys.exit("could not inspect the ECR image configuration")
    if not isinstance(value, dict):
        sys.exit("ECR image configuration is invalid")
    return value


def _verified_digest_image(image_uri: str, expected_sha: str) -> str:
    image_uri = image_uri.strip()
    match = _DIGEST_IMAGE.fullmatch(image_uri)
    if not match:
        sys.exit("--image-uri must be an ECR URI pinned with @sha256:<64 lowercase hex>")
    if not _FULL_SHA.fullmatch(expected_sha):
        sys.exit("image provenance requires the exact full 40-character commit SHA")
    identity = _aws("sts", "get-caller-identity")
    if str(identity.get("Account") or "") != match.group("account"):
        sys.exit("ECR image registry does not belong to the authenticated AWS account")
    common = ("--repository-name", match.group("repository"), "--region", match.group("region"))
    result = _aws(
        "ecr",
        "describe-images",
        *common,
        "--image-ids",
        f"imageDigest={match.group('digest')}",
    )
    details = result.get("imageDetails") or []
    if not any(item.get("imageDigest") == match.group("digest") for item in details):
        sys.exit("ECR did not verify the requested image digest")
    image = _aws(
        "ecr", "batch-get-image", *common, "--image-ids",
        f"imageDigest={match.group('digest')}",
        "--accepted-media-types",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
    images = image.get("images") or []
    if len(images) != 1 or images[0].get("imageId", {}).get("imageDigest") != match.group("digest"):
        sys.exit("ECR did not return the requested immutable image manifest")
    try:
        manifest = json.loads(images[0]["imageManifest"])
        config_digest = str(manifest["config"]["digest"])
    except (KeyError, TypeError, json.JSONDecodeError):
        sys.exit("ECR image manifest has no readable configuration digest")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", config_digest):
        sys.exit("ECR image manifest has an invalid configuration digest")
    layer = _aws(
        "ecr", "get-download-url-for-layer", *common, "--layer-digest", config_digest
    )
    config = _download_json(str(layer.get("downloadUrl") or ""))
    image_config = config.get("config") or {}
    labels = image_config.get("Labels") or {}
    env = image_config.get("Env") or []
    if (
        labels.get("org.opencontainers.image.revision") != expected_sha
        or f"MIA_BUILD_SHA={expected_sha}" not in env
    ):
        sys.exit("ECR image provenance does not match the tested commit SHA")
    return image_uri


def _entries_by_name(entries: list[dict]) -> dict[str, str]:
    return {
        str(entry.get("name") or ""): str(entry.get("value") or "")
        for entry in entries
        if entry.get("name")
    }


def _normalize_v2_container(container: dict, *, image_uri: str, sha: str) -> None:
    environment = [
        dict(entry)
        for entry in container.get("environment", [])
        if entry.get("name") not in RETIRED_NAMES
    ]
    values = _entries_by_name(environment)
    sales_gemini = values.get("MIA_SALES_GEMINI_MODEL", "").strip()
    for name in PURPOSE_MODEL_SETTINGS:
        if not values.get(name, "").strip():
            if not sales_gemini:
                sys.exit(
                    f"{name} is absent and MIA_SALES_GEMINI_MODEL provides no configured fallback"
                )
            values[name] = sales_gemini
    values.update(ENABLED_V2_SETTINGS)
    values["MIA_BUILD_SHA"] = sha
    by_name = {entry["name"]: entry for entry in environment if entry.get("name")}
    for name, value in values.items():
        by_name[name] = {"name": name, "value": value}
    container["environment"] = list(by_name.values())
    container["secrets"] = [
        dict(entry)
        for entry in container.get("secrets", [])
        if entry.get("name") not in RETIRED_NAMES
    ]
    container["image"] = image_uri


def _v2_payload(task_def: dict, *, image_uri: str, sha: str) -> dict:
    payload = {key: task_def[key] for key in MUTABLE_KEYS if key in task_def}
    mia = [item for item in payload.get("containerDefinitions", []) if item.get("name") == "mia"]
    if len(mia) != 1:
        sys.exit("task definition must contain exactly one container named mia")
    _normalize_v2_container(mia[0], image_uri=image_uri, sha=sha)
    return payload


def main() -> None:
    gate = Path(__file__).resolve().parent / "assert_origin_bind.py"
    if subprocess.run([sys.executable, str(gate)], check=False).returncode != 0:
        sys.exit("origin-bind gate failed; refusing to register a revision")
    parser = argparse.ArgumentParser()
    parser.add_argument("--v2-release", action="store_true", required=True)
    parser.add_argument("--image-uri", required=True)
    parser.add_argument("--sha", required=True)
    args = parser.parse_args()
    sha = _resolved_sha(args.sha)
    image_uri = _verified_digest_image(args.image_uri, sha)
    base = _running_task_definition()
    current = _aws("ecs", "describe-task-definition", "--task-definition", base)
    task_def = current["taskDefinition"]
    payload = _v2_payload(task_def, image_uri=image_uri, sha=sha)
    print(
        f"base revision {task_def['revision']}; "
        "mia image pinned by digest; v2 settings normalized"
    )
    registered = _aws(
        "ecs", "register-task-definition", "--cli-input-json", json.dumps(payload)
    )
    new = registered["taskDefinition"]
    print(f"registered {new['family']}:{new['revision']}")


if __name__ == "__main__":
    main()
