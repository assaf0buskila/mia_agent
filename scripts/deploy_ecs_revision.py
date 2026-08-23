"""Register a new `mia` ECS task revision pointing at a given image tag.

Read-mostly: describes the active task definition, swaps only the image tag,
and registers a new revision. Never updates the service (that stays explicit).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

FAMILY = "mia"
MUTABLE_KEYS = (
    "family",
    "taskRoleArn",
    "executionRoleArn",
    "networkMode",
    "containerDefinitions",
    "volumes",
    "placementConstraints",
    "requiresCompatibilities",
    "cpu",
    "memory",
    "runtimePlatform",
    "ephemeralStorage",
)


def _aws(*args: str) -> dict:
    proc = subprocess.run(
        ["aws", *args, "--output", "json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        sys.exit(f"aws {' '.join(args)} failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def _normalize_tag(value: str) -> str:
    tag = value.strip().rsplit(":", 1)[-1]
    if not tag or "/" in tag:
        sys.exit("pass an image tag such as 14")
    return tag


def _image_with_tag(image: str, tag: str) -> str:
    if "/" not in image:
        sys.exit(f"unexpected image URI {image}")
    registry, _, name_and_tag = image.rpartition("/")
    name = name_and_tag.split(":", 1)[0]
    if not name:
        sys.exit(f"unexpected image URI {image}")
    return f"{registry}/{name}:{tag}"


def _upsert_env(container: dict, pairs: list[tuple[str, str]]) -> None:
    env = list(container.get("environment") or [])
    by_name = {row["name"]: row for row in env if "name" in row}
    for name, value in pairs:
        by_name[name] = {"name": name, "value": value}
    container["environment"] = list(by_name.values())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="Upsert a plaintext container env var. Repeatable.",
    )
    args = parser.parse_args()
    tag = _normalize_tag(args.tag)
    extra: list[tuple[str, str]] = []
    for item in args.env:
        if "=" not in item:
            sys.exit(f"expected NAME=VALUE, got {item}")
        name, value = item.split("=", 1)
        extra.append((name.strip(), value))

    current = _aws("ecs", "describe-task-definition", "--task-definition", FAMILY)
    task_def = current["taskDefinition"]
    print(f"current revision {task_def['revision']}")

    payload = {key: task_def[key] for key in MUTABLE_KEYS if key in task_def}
    for container in payload["containerDefinitions"]:
        container["image"] = _image_with_tag(container["image"], tag)
        if extra:
            _upsert_env(container, extra)
        print(f"{container['name']} -> {container['image']}")

    registered = _aws(
        "ecs",
        "register-task-definition",
        "--cli-input-json",
        json.dumps(payload),
    )
    new = registered["taskDefinition"]
    print(f"registered {new['family']}:{new['revision']}")


if __name__ == "__main__":
    main()
