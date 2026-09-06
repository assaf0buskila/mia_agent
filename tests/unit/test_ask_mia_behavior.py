from __future__ import annotations

import subprocess
from pathlib import Path


def test_widget_contact_conversion_behavior() -> None:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["node", "tests/unit/widget_behavior.test.js"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "widget behavioral checks passed" in result.stdout
