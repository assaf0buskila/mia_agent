"""Refuse to cut an image unless public Ask Mia origin-bind is on this SHA.

Stdlib only. Exit 1 if the fail-closed origin allowlist or the public POST
guards are missing. CI and `scripts/deploy_ecs_revision.py` both call this.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEBSITE = ROOT / "app" / "api" / "website.py"
GUARD = ROOT / "app" / "core" / "public_website.py"

REQUIRED_BUCKETS = ("session", "message", "voice", "handoff", "end")
REQUIRED_GUARD_TOKENS = (
    "origin not allowed",
    "rate limited",
    "def origin_allowed",
    "def public_website_guard",
    "origins.discard(\"*\")",
    'origins.discard("null")',
)


def assert_origin_bind(root: Path | None = None) -> None:
    base = root or ROOT
    website = (base / "app" / "api" / "website.py").read_text(encoding="utf-8")
    guard = (base / "app" / "core" / "public_website.py").read_text(encoding="utf-8")
    missing: list[str] = []
    for token in REQUIRED_GUARD_TOKENS:
        if token not in guard:
            missing.append(f"public_website.py missing {token!r}")
    if "if not origin_allowed(origin, settings):" not in guard:
        missing.append("origin check is not fail-closed")
    for bucket in REQUIRED_BUCKETS:
        needle = f'public_website_guard("{bucket}")'
        if needle not in website:
            missing.append(f"website.py missing {needle}")
    if missing:
        raise SystemExit("origin-bind gate failed:\n- " + "\n- ".join(missing))


def main() -> None:
    if not WEBSITE.is_file() or not GUARD.is_file():
        sys.exit("origin-bind files missing on this SHA")
    assert_origin_bind()
    print("origin-bind: ok")


if __name__ == "__main__":
    main()
