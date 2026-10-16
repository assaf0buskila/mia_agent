"""Export the Assaf OS v2 golden fixtures (V2_PG_CONTRACTS.md section 10.4).

A registry, not a list of shapes. Every ``tests/contract/shapes/<chunk>.py`` module exports
``SHAPES: dict[str, Callable[[], dict]]``; this script imports each one and writes
``tests/contract/golden/<chunk>/<shape>.json`` (``v2_20`` becomes the directory ``v2-20``). A later
chunk adds its shapes by adding its own module, never by editing this file.

    python scripts/export_os_contract_fixtures.py            # write the golden tree
    python scripts/export_os_contract_fixtures.py --check    # exit 1 when the tree differs

CI runs ``--check``: the export must equal the committed copy. The frontend vendors the files by
hash. ``fixtures.sha256`` is the integrator's, regenerated on every rebase; this script never writes
it and ``--check`` ignores it.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHAPES_DIR = ROOT / "tests" / "contract" / "shapes"
GOLDEN_DIR = ROOT / "tests" / "contract" / "golden"
INTEGRATOR_FILE = "fixtures.sha256"

_SHAPE_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
Registry = dict[str, dict[str, Callable[[], dict]]]


class ExportError(Exception):
    """A registry or shape problem. ``args[0]`` is a fixed reason code."""


def render(value: dict) -> bytes:
    """One canonical byte form: sorted keys, 2-space indent, UTF-8, LF, one trailing newline."""
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def read_golden(path: Path) -> bytes:
    """A committed golden's bytes. Git for Windows may check a text file out with CRLF; the
    committed blob and the CI artifact are LF, so line endings are normalised on read."""
    return path.read_bytes().replace(b"\r\n", b"\n")


def _chunk_of(path: Path) -> str:
    return path.stem.replace("_", "-")


def load_registry(shapes_dir: Path = SHAPES_DIR) -> Registry:
    """Import every public shapes module (``_*.py`` is private) and collect its ``SHAPES``."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))  # ``app`` and ``tests`` import from the repository root
    registry: Registry = {}
    for path in sorted(shapes_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(f"os_contract_shapes_{path.stem}", path)
        if spec is None or spec.loader is None:
            raise ExportError("shapes_unloadable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module  # dataclasses resolve string annotations through it
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(spec.name, None)
            raise
        shapes = getattr(module, "SHAPES", None)
        if not isinstance(shapes, dict):
            raise ExportError("shapes_missing")
        for name, build in shapes.items():
            if not isinstance(name, str) or _SHAPE_NAME.fullmatch(name) is None:
                raise ExportError("shape_name_invalid")
            if not callable(build):
                raise ExportError("shape_not_callable")
        registry[_chunk_of(path)] = dict(shapes)
    return registry


def export(registry: Registry) -> dict[str, bytes]:
    """Relative path (``<chunk>/<shape>.json``) to bytes. A shape is built twice and must match."""
    files: dict[str, bytes] = {}
    for chunk, shapes in registry.items():
        for name, build in shapes.items():
            first, second = build(), build()
            if not isinstance(first, dict):
                raise ExportError("shape_not_an_object")
            rendered = render(first)
            if rendered != render(second):
                raise ExportError("shape_not_deterministic")
            files[f"{chunk}/{name}.json"] = rendered
    return files


def check(registry: Registry, golden_dir: Path = GOLDEN_DIR) -> list[str]:
    """Differences between the export and the tree on disk, as sorted ``<path>: <problem>``."""
    wanted = export(registry)
    problems: list[str] = []
    for relative, expected in wanted.items():
        path = golden_dir / relative
        if not path.is_file():
            problems.append(f"{relative}: missing")
        elif read_golden(path) != expected:
            problems.append(f"{relative}: changed")
    if golden_dir.is_dir():
        for path in golden_dir.rglob("*"):
            if not path.is_file() or path.name == INTEGRATOR_FILE:
                continue
            relative = path.relative_to(golden_dir).as_posix()
            if relative not in wanted:
                problems.append(f"{relative}: stale")
    return sorted(problems)


def write(registry: Registry, golden_dir: Path = GOLDEN_DIR) -> list[str]:
    """Write the export and remove files it no longer produces. Returns the written paths."""
    wanted = export(registry)
    for relative, data in wanted.items():
        target = golden_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    if golden_dir.is_dir():
        for path in golden_dir.rglob("*"):
            if path.is_file() and path.name != INTEGRATOR_FILE and (
                path.relative_to(golden_dir).as_posix() not in wanted
            ):
                path.unlink()
    return sorted(wanted)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 when the tree differs")
    parser.add_argument("--out", type=Path, default=GOLDEN_DIR, help="the golden tree to use")
    args = parser.parse_args(argv)
    try:
        registry = load_registry()
        if args.check:
            problems = check(registry, args.out)
            for problem in problems:
                print(problem)
            print(f"contract fixtures: {'DIFFER' if problems else 'match'}")
            return 1 if problems else 0
        written = write(registry, args.out)
    except ExportError as exc:
        print(f"contract fixtures: refused reason={exc.args[0]}")
        return 2
    print(f"contract fixtures: wrote {len(written)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
