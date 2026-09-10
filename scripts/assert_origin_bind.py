"""AST gate for fail-closed public website POST routes."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEBSITE = ROOT / "app" / "api" / "website.py"
GUARD = ROOT / "app" / "core" / "public_website.py"
REQUIRED_GUARD_TOKENS = (
    "origin not allowed", "rate limited", "def origin_allowed",
    "def public_website_guard", 'origins.discard("*")', 'origins.discard("null")',
)


def _is_public_guard(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "Depends" and len(node.args) == 1
        and isinstance(node.args[0], ast.Call) and isinstance(node.args[0].func, ast.Name)
        and node.args[0].func.id == "public_website_guard" and len(node.args[0].args) == 1
        and isinstance(node.args[0].args[0], ast.Constant)
        and isinstance(node.args[0].args[0].value, str)
    )


def _post_guarded(decorator: ast.Call) -> bool:
    dependency = next((kw.value for kw in decorator.keywords if kw.arg == "dependencies"), None)
    return isinstance(dependency, ast.List) and any(
        _is_public_guard(item) for item in dependency.elts
    )


def assert_origin_bind(root: Path | None = None) -> None:
    base = root or ROOT
    website_path = base / "app" / "api" / "website.py"
    guard_path = base / "app" / "core" / "public_website.py"
    website = website_path.read_text(encoding="utf-8")
    guard = guard_path.read_text(encoding="utf-8")
    missing: list[str] = []
    for token in REQUIRED_GUARD_TOKENS:
        if token not in guard:
            missing.append(f"public_website.py missing {token!r}")
    if "if not origin_allowed(origin, settings):" not in guard:
        missing.append("origin check is not fail-closed")
    tree = ast.parse(website, filename=str(website_path))
    post_routes: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "post"
            ):
                route = ast.literal_eval(decorator.args[0]) if decorator.args else "<unknown>"
                post_routes.append((node.lineno, str(route)))
                if not _post_guarded(decorator):
                    missing.append(f"unguarded POST route {route!r} at line {node.lineno}")
    if not post_routes:
        missing.append("no router.post routes found")
    if missing:
        raise SystemExit("origin-bind gate failed:\n- " + "\n- ".join(missing))


def main() -> None:
    if not WEBSITE.is_file() or not GUARD.is_file():
        sys.exit("origin-bind files missing on this SHA")
    assert_origin_bind()
    print("origin-bind: ok")


if __name__ == "__main__":
    main()
