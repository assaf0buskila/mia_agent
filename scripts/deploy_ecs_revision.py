"""Register a new `mia` ECS task revision pointing at a given image tag.

Read-mostly: describes the active task definition, swaps only the image tag and the
build sha, and registers a new revision. Never updates the service (that stays
explicit).

The sha is required and must match a clean checkout of HEAD. The image is built from
the working tree, so without this a dirty tree could ship silently and production
would report a commit it is not running.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

FAMILY = "mia"
CLUSTER = "mia"
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


def _resolved_sha(requested: str) -> str:
    """The tested commit. Refuses a dirty tree or a mismatched HEAD."""
    requested = requested.strip()
    if not requested:
        sys.exit("--sha is required: pass the tested commit")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    if head.returncode != 0:
        sys.exit("cannot determine HEAD; refusing to register a revision")
    head_sha = head.stdout.strip()
    if not head_sha.startswith(requested) and not requested.startswith(head_sha):
        sys.exit(
            f"--sha {requested} does not match HEAD {head_sha[:12]}; "
            "check out the tested commit before building"
        )
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, check=False,
    )
    if dirty.returncode != 0:
        sys.exit("cannot determine tree state; refusing to register a revision")
    if dirty.stdout.strip():
        sys.exit(
            "working tree is dirty; the image is built from the tree, so commit or "
            "stash before registering a revision"
        )
    return head_sha


def _stamp_build_sha(container: dict, sha: str) -> None:
    """Report the running commit on /health. Plain env, never a secret."""
    environment = [
        entry
        for entry in container.get("environment", [])
        if entry.get("name") != "MIA_BUILD_SHA"
    ]
    environment.append({"name": "MIA_BUILD_SHA", "value": sha})
    container["environment"] = environment


def _image_with_tag(image: str, tag: str) -> str:
    if "/" not in image:
        sys.exit(f"unexpected image URI {image}")
    registry, _, name_and_tag = image.rpartition("/")
    name = name_and_tag.split(":", 1)[0]
    if not name:
        sys.exit(f"unexpected image URI {image}")
    return f"{registry}/{name}:{tag}"


def _running_task_definition() -> str:
    """The exact revision the service is serving right now.

    Not the family name. `describe-task-definition --task-definition mia` returns the
    latest ACTIVE revision, which is not necessarily the one production runs: a release
    that registered mia:30 and then failed its migration leaves the service on mia:29
    while mia:30 stays registered forever. Basing the next revision on the family would
    silently inherit that abandoned config -- environment, secrets, cpu, memory -- from
    a revision a human deliberately never cut over to.
    """
    service = _aws("ecs", "describe-services", "--cluster", CLUSTER, "--services", FAMILY)
    services = service.get("services") or []
    if not services:
        sys.exit(f"no ECS service {FAMILY} in cluster {CLUSTER}; refusing to guess a base")
    arn = str(services[0].get("taskDefinition") or "")
    if not arn:
        sys.exit("service reports no task definition; refusing to guess a base")
    return arn


def main() -> None:
    # Gate F: do not register a revision unless this SHA still has origin-bind.
    gate = Path(__file__).resolve().parent / "assert_origin_bind.py"
    checked = subprocess.run([sys.executable, str(gate)], check=False)
    if checked.returncode != 0:
        sys.exit("origin-bind gate failed; refusing to register a revision")
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument(
        "--sha", required=True, help="the tested commit this image was built from"
    )
    args = parser.parse_args()
    tag = _normalize_tag(args.tag)
    sha = _resolved_sha(args.sha)
    print(f"build sha {sha[:12]}")

    base = _running_task_definition()
    current = _aws("ecs", "describe-task-definition", "--task-definition", base)
    task_def = current["taskDefinition"]
    print(f"basing on the revision production is serving: {task_def['revision']}")

    payload = {key: task_def[key] for key in MUTABLE_KEYS if key in task_def}
    for container in payload["containerDefinitions"]:
        container["image"] = _image_with_tag(container["image"], tag)
        _stamp_build_sha(container, sha)
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
