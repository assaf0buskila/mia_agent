"""The statement domain is pure logic: no DB, routes, clock, randomness, files or network."""

from __future__ import annotations

import ast
from pathlib import Path

import app.domain.statements as statements

PACKAGE_DIR = Path(statements.__file__).resolve().parent

ALLOWED_STDLIB = {
    "__future__",
    "collections",
    "dataclasses",
    "datetime",
    "enum",
    "functools",
    "hashlib",
    "hmac",
    "itertools",
    "json",
    "re",
    "types",
    "typing",
    "unicodedata",
}
FORBIDDEN_CALLS = {
    "now",
    "today",
    "utcnow",
    "urandom",
    "uuid1",
    "uuid4",
    "open",
    "print",
    "eval",
    "exec",
}


def package_sources():
    return sorted(PACKAGE_DIR.glob("*.py"))


def test_imports_are_stdlib_allow_list_or_the_package_itself():
    assert package_sources()
    for path in package_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue  # relative import inside the package
                roots = [(node.module or "").split(".")[0]]
                modules = [node.module or ""]
            else:
                continue
            for root, module in zip(roots, modules, strict=True):
                if root == "app":
                    assert module.startswith("app.domain.statements"), (path.name, module)
                else:
                    assert root in ALLOWED_STDLIB, (path.name, module)


def test_no_clock_randomness_file_or_console_access():
    for path in package_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                assert name not in FORBIDDEN_CALLS, (path.name, name, node.lineno)


def test_public_names_resolve():
    assert statements.__all__
    assert len(set(statements.__all__)) == len(statements.__all__)
    for name in statements.__all__:
        assert hasattr(statements, name), name


def test_no_module_state_that_could_carry_text_between_calls():
    for path in package_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(
                node.value, (ast.List, ast.Dict, ast.Set)
            ):
                raise AssertionError(f"mutable module-level container in {path.name}:{node.lineno}")
