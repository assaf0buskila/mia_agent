"""Origin-bind must be on the SHA before CI cuts an image or ECS registers a revision."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_origin_bind_gate_passes_this_sha() -> None:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "assert_origin_bind.py")],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "origin-bind: ok" in proc.stdout


def test_ci_and_deploy_script_refuse_without_origin_bind() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    deploy = (ROOT / "scripts/deploy_ecs_revision.py").read_text(encoding="utf-8")
    assert "scripts/assert_origin_bind.py" in ci
    assert ci.count("assert_origin_bind.py") >= 3
    assert "assert_origin_bind.py" in deploy
    website = (ROOT / "app/api/website.py").read_text(encoding="utf-8")
    for bucket in ("session", "message", "voice", "handoff", "end"):
        assert f'public_website_guard("{bucket}")' in website
