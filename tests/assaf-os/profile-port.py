"""Local call-count/CPU profile for the actual AssafOsPort adapter.

The transport is a deterministic signed in-process responder. It adds no delay and
does not make network calls; results therefore prove adapter call shape, validation,
and local CPU only.
"""

# ruff: noqa: E402, I001

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("MIA_ENV", "test")

from app.services import assaf_os as port  # noqa: E402


SAMPLES = 12
BASE_GIT_HEAD = "bda48fa697a79b0df6b41f6ef8165f13b4deaec0"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PORT_SOURCE = REPOSITORY_ROOT / "app" / "services" / "assaf_os.py"
OWNER_ID = "123456"
PROPOSAL_ID = "PR_12345678901234567890"
APPROVAL_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
REJECTION_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class Config(SimpleNamespace):
    def telegram_owner_user_id_set(self) -> set[str]:
        return {OWNER_ID}


def config() -> Config:
    return Config(
        assaf_os_enabled=True,
        assaf_os_script_url="https://script.google.com/macros/s/" + "x" * 24 + "/exec",
        assaf_os_workbook_id="workbook_12345678901234567890",
        assaf_os_workspace_id="workspace-main",
        assaf_os_hmac_secret="synthetic-profile-secret-at-least-32-bytes",
        assaf_os_mode="TEST",
        kill_switch=False,
    )


def binding(value: Config) -> dict[str, object]:
    return {
        "api_version": port.API_VERSION,
        "source": "google_sheets",
        "mode": value.assaf_os_mode,
        "workbook_id": value.assaf_os_workbook_id,
        "workspace_id": value.assaf_os_workspace_id,
    }


def pending(value: Config, source: str) -> dict[str, object]:
    return {
        **binding(value),
        "actor_source": source,
        "approval_guard_version": 1,
        "status": "pending",
        "proposal_id": PROPOSAL_ID,
        "request_id": "12345678-1234-4234-8234-123456789012",
        "approval_request_id": APPROVAL_ID,
        "rejection_request_id": REJECTION_ID,
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        "entity": "tasks",
        "record_id": None,
        "expected_revision": 0,
        "input": {"title": "Synthetic profile", "status": "לביצוע"},
        "before": None,
        "after": {"task_id": "TAS_1", "title": "Synthetic profile", "status": "לביצוע"},
    }


def receipt(
    value: Config, request_id: str, source: str, *, status: str = "committed"
) -> dict[str, object]:
    return {
        **binding(value),
        "actor_source": source,
        "status": status,
        "request_id": request_id,
        "proposal_id": PROPOSAL_ID if status != "unknown" else None,
        "entity": "tasks" if status != "unknown" else None,
        "record_id": "TAS_1" if status != "unknown" else None,
        "revision": 1 if status != "unknown" else None,
        "committed_at": datetime.now(UTC).isoformat() if status != "unknown" else None,
        "reason_codes": [] if status != "unknown" else ["receipt_not_found"],
    }


def snapshot(value: Config) -> dict[str, object]:
    entities = port.RECORD_ENTITIES
    return {
        **binding(value),
        "observed_at": datetime.now(UTC).isoformat(),
        "coverage": "unknown",
        "entity_coverage": {entity: "unknown" for entity in entities},
        "records": {entity: [] for entity in entities},
        "summary": {"coverage": "unknown", "monthly": []},
        "issues": [],
        "pending_proposals": [],
    }


def schema(value: Config) -> dict[str, object]:
    return {
        **binding(value),
        "schema_version": "assaf-os-unified.v1",
        "money_unit": "minor",
        "tabs": [],
        "enums": {},
    }


@dataclass
class Responder:
    value: Config
    calls: int = 0
    operations: tuple[str, ...] = ()

    def __call__(self, _url: str, body: bytes) -> bytes:
        request = json.loads(json.loads(body)["payload"])
        operation = request["operation"]
        self.calls += 1
        self.operations += (operation,)
        if operation == "schema":
            result = schema(self.value)
        elif operation == "snapshot":
            result = snapshot(self.value)
        elif operation in {"propose", "proposal"}:
            result = pending(self.value, request["actor_source"])
            if operation == "propose":
                result["request_id"] = request["request_id"]
        elif operation in {"approve", "reject"}:
            source = "MIA" if request["actor_source"] == "TELEGRAM_APPROVAL" else "OS"
            result = receipt(
                self.value,
                request["request_id"],
                source,
                status="committed" if operation == "approve" else "rejected",
            )
        elif operation == "receipt":
            result = receipt(self.value, request["data"]["request_id"], "MIA", status="unknown")
        else:  # pragma: no cover - profile definitions are fixed
            raise AssertionError(operation)
        identity = {key: value for key, value in request.items() if key != "data"}
        identity.update(mode=self.value.assaf_os_mode, ok=True, data=result)
        payload = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
        signature = hmac.new(
            self.value.assaf_os_hmac_secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        return json.dumps(
            {"payload": payload, "signature": signature}, separators=(",", ":")
        ).encode()


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * fraction + 0.999999) - 1))
    return ordered[index]


def distribution(values: list[float]) -> dict[str, float]:
    return {"p50": percentile(values, 0.5), "p95": percentile(values, 0.95), "max": max(values)}


def current_git_head() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPOSITORY_ROOT / ".cache" / "port-profile-raw.json",
        help="JSON output path (default: repository .cache/port-profile-raw.json)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    original_post = port._post
    current: Responder | None = None

    def dispatch(url: str, body: bytes) -> bytes:
        assert current is not None
        return current(url, body)

    port._post = dispatch
    families: list[dict[str, object]] = []

    def profile(name: str, actor_source: str, invoke, *, setup=None) -> None:
        nonlocal current
        samples: list[dict[str, object]] = []
        for sample in range(1, SAMPLES + 1):
            port._clear_assaf_os_decision_binding_cache()
            value = config()
            current = Responder(value)
            adapter = port.AssafOsPort(value, OWNER_ID, actor_source=actor_source)
            if setup is not None:
                setup(adapter, sample)
                current = Responder(value)
            started_cpu = time.process_time_ns()
            started_wall = time.perf_counter_ns()
            result = invoke(adapter, sample)
            samples.append(
                {
                    "sample": sample,
                    "cpu_ms": (time.process_time_ns() - started_cpu) / 1_000_000,
                    "wall_ms": (time.perf_counter_ns() - started_wall) / 1_000_000,
                    "signed_transport_calls": current.calls,
                    "operations": list(current.operations),
                    "result_status": result.get("status") if isinstance(result, dict) else None,
                }
            )
        calls = [int(sample["signed_transport_calls"]) for sample in samples]
        families.append(
            {
                "name": name,
                "actor_source": actor_source,
                "independent_samples": SAMPLES,
                "cpu_ms": distribution([float(sample["cpu_ms"]) for sample in samples]),
                "wall_ms": distribution([float(sample["wall_ms"]) for sample in samples]),
                "signed_transport_calls": calls[0]
                if len(set(calls)) == 1
                else distribution([float(value) for value in calls]),
                "operation_sequence": samples[0]["operations"],
                "samples": samples,
            }
        )

    try:
        profile("schema", "OS", lambda adapter, _sample: adapter.schema())
        profile("snapshot", "OS", lambda adapter, _sample: adapter.snapshot())
        profile(
            "propose_task",
            "OS",
            lambda adapter, _sample: adapter.propose(
                port.ProposeRequest(
                    request_id=str(uuid4()),
                    entity="tasks",
                    record_id=None,
                    expected_revision=0,
                    input={"title": "Synthetic profile", "status": "לביצוע"},
                )
            ),
        )
        profile(
            "browser_approve_cold",
            "OS",
            lambda adapter, _sample: adapter.decide(
                "approve", PROPOSAL_ID, port.DecisionRequest(request_id=APPROVAL_ID)
            ),
        )
        profile(
            "browser_approve_warm",
            "OS",
            lambda adapter, _sample: adapter.decide(
                "approve", PROPOSAL_ID, port.DecisionRequest(request_id=APPROVAL_ID)
            ),
            setup=lambda adapter, sample: adapter.propose(
                port.ProposeRequest(
                    request_id=f"12345678-1234-4234-8234-{sample:012d}",
                    entity="tasks",
                    record_id=None,
                    expected_revision=0,
                    input={"title": "Synthetic profile", "status": "לביצוע"},
                )
            ),
        )
        profile(
            "telegram_approve",
            "TELEGRAM_APPROVAL",
            lambda adapter, _sample: adapter.decide(
                "approve", PROPOSAL_ID, port.DecisionRequest(request_id=APPROVAL_ID)
            ),
        )
        profile(
            "receipt_unknown",
            "MIA",
            lambda adapter, sample: adapter.receipt(f"12345678-1234-4234-8234-{sample:012d}"),
        )
    finally:
        port._clear_assaf_os_decision_binding_cache()
        port._post = original_post

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": datetime.now(UTC).isoformat(),
                "base_git_head": BASE_GIT_HEAD,
                "git_head": current_git_head(),
                "measured_source": {
                    "path": PORT_SOURCE.relative_to(REPOSITORY_ROOT).as_posix(),
                    "sha256": hashlib.sha256(PORT_SOURCE.read_bytes()).hexdigest(),
                },
                "evidence_scope": (
                    "LOCAL synthetic actual AssafOsPort with deterministic signed in-process "
                    "responses; no delay, network, credentials, or live calls"
                ),
                "families": families,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    for family in families:
        print(
            f"{family['name']}: n={family['independent_samples']} "
            f"calls={family['signed_transport_calls']} cpu={family['cpu_ms']}"
        )
    print(f"wrote {output}")


if __name__ == "__main__":
    main()
