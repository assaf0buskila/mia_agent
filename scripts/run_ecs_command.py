"""Run an arbitrary container command as a one-off Fargate task."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

CLUSTER = "mia"
SERVICE = "mia"
CONTAINER = "mia"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-definition", required=True)
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Container command argv after --, e.g. -- mia-wipe-data --confirm fresh-start",
    )
    args = parser.parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("container command is required after --")
    args.command = command
    return args


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


def main() -> None:
    args = _parse_args()

    service = _aws(
        "ecs", "describe-services", "--cluster", CLUSTER, "--services", SERVICE
    )["services"][0]
    network = json.dumps(service["networkConfiguration"])
    overrides = json.dumps(
        {"containerOverrides": [{"name": CONTAINER, "command": args.command}]}
    )

    started = _aws(
        "ecs",
        "run-task",
        "--cluster",
        CLUSTER,
        "--task-definition",
        args.task_definition,
        "--launch-type",
        "FARGATE",
        "--network-configuration",
        network,
        "--overrides",
        overrides,
    )
    failures = started.get("failures") or []
    if failures:
        sys.exit(f"run-task failures: {failures}")
    task_arn = started["tasks"][0]["taskArn"]
    print(f"task {task_arn}")

    for _ in range(60):
        time.sleep(10)
        described = _aws(
            "ecs", "describe-tasks", "--cluster", CLUSTER, "--tasks", task_arn
        )["tasks"][0]
        status = described["lastStatus"]
        print(f"status {status}")
        if status != "STOPPED":
            continue
        container = described["containers"][0]
        code = container.get("exitCode")
        print(f"exitCode {code} reason {described.get('stoppedReason')}")
        if code != 0:
            sys.exit(f"task {' '.join(args.command)} exited {code}")
        print(f"task {' '.join(args.command)} completed")
        return
    sys.exit("timed out waiting for task")


if __name__ == "__main__":
    main()
