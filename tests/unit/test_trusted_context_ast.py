"""V2-19 (V2_PG_CONTRACTS.md §9): only the allow-listed modules can mint or use a trusted context.

The scanner resolves symbols through the import table of each module: it never matches substrings
(``_issue(`` already appears in unrelated app modules). Outside the §9 allow-list
(``app/services/os_ledger/{authorize,owner_direct,api}.py`` and ``app/api/os_v2/commits.py``;
``tests/`` is never scanned) it flags:
  * any import of ``_issue``, ``_CAPABILITY`` or ``TrustedOwnerContext`` from the api module;
  * any attribute access ``api._issue`` / ``api._CAPABILITY`` on that module, however imported;
  * any import from, or call into, ``app.services.os_ledger.owner_direct``.
Two stricter pins sit on top: ``api._issue`` is referenced by ``authorize.py`` alone, and the
browser mutation proof ``owner_sessions._MUTATION_PROOF`` by ``owner_sessions.py`` and
``authorize.py`` alone. Planted violations are scratch files under ``tmp_path``, never ``app/``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
APP = REPO / "app"
API = "app.services.os_ledger.api"
DIRECT = "app.services.os_ledger.owner_direct"
SESSIONS = "app.api.owner_sessions"
CONTEXT_NAMES = frozenset({"_issue", "_CAPABILITY", "TrustedOwnerContext"})
CONTEXT_ATTRS = frozenset({"_issue", "_CAPABILITY"})
ALLOW_LIST = frozenset({
    "app/services/os_ledger/authorize.py",
    "app/services/os_ledger/owner_direct.py",
    "app/services/os_ledger/api.py",
    "app/api/os_v2/commits.py",
})
ISSUE_ALLOW_LIST = frozenset({"app/services/os_ledger/authorize.py",
                              "app/services/os_ledger/api.py"})
PROOF_ALLOW_LIST = frozenset({"app/api/owner_sessions.py", "app/services/os_ledger/authorize.py"})


def module_of(relative: str) -> tuple[str, str]:
    """``(module, package)`` for a repo-relative path such as ``app/api/os_v2/reads.py``."""
    parts = relative[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
        return ".".join(parts), ".".join(parts)
    return ".".join(parts), ".".join(parts[:-1])


class Scanner(ast.NodeVisitor):
    """Resolves every import, alias and attribute chain of one module to a dotted name."""

    def __init__(self, relative: str) -> None:
        self.module, self.package = module_of(relative)
        self.bound: dict[str, str] = {}  # local name -> the dotted name it refers to
        self.findings: set[tuple[int, str, str]] = set()  # (line, rule, resolved name)

    # -- resolution ------------------------------------------------------------------------

    def _absolute(self, module: str | None, level: int) -> str:
        if not level:
            return module or ""
        base = self.package.split(".")
        if level > 1:
            base = base[: len(base) - (level - 1)]
        return ".".join([*base, *([module] if module else [])])

    def resolve(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return self.bound.get(node.id)
        if isinstance(node, ast.Attribute):
            root = self.resolve(node.value)
            return f"{root}.{node.attr}" if root else None
        if isinstance(node, ast.Call) and self._import_call(node):
            return self._import_call(node)
        return None

    @staticmethod
    def _import_call(node: ast.Call) -> str | None:
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name not in {"import_module", "__import__"} or not node.args:
            return None
        first = node.args[0]
        return first.value if isinstance(first, ast.Constant) and isinstance(
            first.value, str) else None

    # -- rules -----------------------------------------------------------------------------

    def _check(self, line: int, name: str | None) -> None:
        if not name:
            return
        if name == DIRECT or name.startswith(DIRECT + "."):
            self.findings.add((line, "owner_direct", name))
        if name in {f"{API}.{attr}" for attr in CONTEXT_ATTRS}:
            self.findings.add((line, "context_attribute", name))
        if name == f"{SESSIONS}._MUTATION_PROOF":
            self.findings.add((line, "mutation_proof", name))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._check(node.lineno, alias.name)
            if alias.asname:
                self.bound[alias.asname] = alias.name
            else:
                root = alias.name.split(".")[0]
                self.bound[root] = root

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        source = self._absolute(node.module, node.level)
        for alias in node.names:
            if alias.name == "*":
                if source in {API, DIRECT, SESSIONS}:
                    self.findings.add((node.lineno, "star_import", source))
                continue
            full = f"{source}.{alias.name}"
            if source == API and alias.name in CONTEXT_NAMES:
                self.findings.add((node.lineno, "context_import", full))
            self._check(node.lineno, source)
            self._check(node.lineno, full)
            self.bound[alias.asname or alias.name] = full

    def visit_Assign(self, node: ast.Assign) -> None:
        resolved = self.resolve(node.value)
        if resolved:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.bound[target.id] = resolved
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._check(node.lineno, self.resolve(node))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self._check(node.lineno, self._import_call(node))
        func = node.func
        if isinstance(func, ast.Name) and func.id == "getattr" and len(node.args) >= 2:
            target, attr = self.resolve(node.args[0]), node.args[1]
            if target and isinstance(attr, ast.Constant) and isinstance(attr.value, str):
                self._check(node.lineno, f"{target}.{attr.value}")
        self.generic_visit(node)


def scan(source: str, relative: str) -> list[tuple[int, str, str]]:
    scanner = Scanner(relative)
    scanner.visit(ast.parse(source))
    return sorted(scanner.findings)


def app_modules() -> list[tuple[str, str]]:
    found = []
    for path in sorted(APP.rglob("*.py")):
        relative = path.relative_to(REPO).as_posix()
        found.append((relative, path.read_text(encoding="utf-8")))
    return found


# ---------------------------------------------------------------------------------------------
# The real tree


def test_no_app_module_outside_the_allow_list_mints_or_reaches_a_trusted_context():
    violations = {
        relative: findings
        for relative, source in app_modules()
        if relative not in ALLOW_LIST and (findings := scan(source, relative))
    }
    assert violations == {}


def test_only_authorize_references_the_issuer():
    issuers = {
        relative
        for relative, source in app_modules()
        if any(rule == "context_attribute" and name.endswith("._issue")
               for _, rule, name in scan(source, relative))
        or any(rule == "context_import" and name.endswith("._issue")
               for _, rule, name in scan(source, relative))
    }
    assert issuers == {"app/services/os_ledger/authorize.py"}
    assert issuers <= ISSUE_ALLOW_LIST


def test_only_the_session_layer_and_authorize_touch_the_mutation_proof():
    holders = {
        relative
        for relative, source in app_modules()
        if any(rule == "mutation_proof" for _, rule, _ in scan(source, relative))
    }
    assert holders == {"app/services/os_ledger/authorize.py"}  # owner_sessions defines it
    assert holders <= PROOF_ALLOW_LIST


def test_the_allow_listed_modules_are_the_ones_the_contract_names():
    assert (APP / "services" / "os_ledger" / "authorize.py").is_file()
    assert (APP / "services" / "os_ledger" / "api.py").is_file()
    assert (APP / "api" / "os_v2" / "commits.py").is_file()
    # authorize.py really is caught by the scanner when it is not allow-listed.
    source = (APP / "services" / "os_ledger" / "authorize.py").read_text(encoding="utf-8")
    assert {rule for _, rule, _ in scan(source, "app/services/os_ledger/evil.py")} >= {
        "context_attribute", "mutation_proof"}


# ---------------------------------------------------------------------------------------------
# Planted violations: each must be flagged (scratch files in tmp_path, never in app/)

PLANTED = {
    "from_import_issue": ("app/x/evil.py",
                          "from app.services.os_ledger.api import _issue\n_issue()\n"),
    "from_import_capability": ("app/x/evil.py",
                               "from app.services.os_ledger.api import _CAPABILITY as c\n"),
    "from_import_context_class": (
        "app/x/evil.py",
        "from app.services.os_ledger.api import TrustedOwnerContext as T\nT(1)\n"),
    "module_alias_attribute": (
        "app/x/evil.py",
        "from app.services.os_ledger import api as ledger\nledger._issue(channel='OS')\n"),
    "import_as_attribute": ("app/x/evil.py",
                            "import app.services.os_ledger.api as a\nx = a._CAPABILITY\n"),
    "full_dotted_chain": ("app/x/evil.py",
                          "import app.services.os_ledger.api\n"
                          "app.services.os_ledger.api._issue()\n"),
    "reassigned_alias": ("app/x/evil.py",
                         "from app.services.os_ledger import api\nb = api\nb._issue()\n"),
    "getattr_constant": ("app/x/evil.py",
                         "from app.services.os_ledger import api\ngetattr(api, '_issue')()\n"),
    "dynamic_import_api": (
        "app/x/evil.py",
        "import importlib\nm = importlib.import_module('app.services.os_ledger.api')\n"
        "m._CAPABILITY\n"),
    "star_import_api": ("app/x/evil.py", "from app.services.os_ledger.api import *\n"),
    "relative_import_issue": ("app/services/os_ledger/evil.py", "from .api import _issue\n"),
    "relative_module_attribute": ("app/services/os_ledger/evil.py",
                                  "from . import api\napi._CAPABILITY\n"),
    "parent_relative_import": ("app/services/os_ledger/sub/evil.py",
                               "from ..api import TrustedOwnerContext\n"),
    "import_owner_direct_module": ("app/tools/owner/evil.py",
                                   "from app.services.os_ledger import owner_direct\n"),
    "import_from_owner_direct": ("app/api/evil.py",
                                 "from app.services.os_ledger.owner_direct import commit\n"),
    "import_owner_direct_as": ("app/workers/evil.py",
                               "import app.services.os_ledger.owner_direct as od\nod.undo()\n"),
    "call_owner_direct_by_chain": ("app/api/evil.py",
                                   "import app.services.os_ledger\n"
                                   "app.services.os_ledger.owner_direct.abandon()\n"),
    "dynamic_import_owner_direct": (
        "app/api/telegram_evil.py",
        "import importlib\nimportlib.import_module('app.services.os_ledger.owner_direct')\n"),
    "relative_owner_direct": ("app/services/os_ledger/evil.py",
                              "from .owner_direct import commit\n"),
    "mutation_proof_import": ("app/api/evil.py",
                              "from app.api.owner_sessions import _MUTATION_PROOF\n"),
    "mutation_proof_attribute": (
        "app/api/evil.py",
        "from app.api import owner_sessions\nproof = owner_sessions._MUTATION_PROOF\n"),
}


@pytest.mark.parametrize("case", sorted(PLANTED))
def test_a_planted_violation_is_flagged(case, tmp_path):
    relative, source = PLANTED[case]
    scratch = tmp_path / relative
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_text(source, encoding="utf-8")
    assert scan(scratch.read_text(encoding="utf-8"), relative), case


def test_substrings_and_unrelated_symbols_are_never_flagged(tmp_path):
    source = (
        "from app.services.os_ledger import api\n"
        "from app.services.os_ledger.api import LedgerError\n"
        "from app.api import owner_sessions\n"
        "def _issue():\n    return 1\n"
        "_issue()\n"
        "class X:\n    def _issue(self):\n        return self._issue\n"
        "owner_sessions._issue_session_view\n"
        "isinstance(None, api.TrustedOwnerContext)\n"
        "text = 'api._issue( and owner_direct.commit('\n"
        "# api._CAPABILITY\n"
        "api.LedgerError('kill_switch')\n"
    )
    scratch = tmp_path / "app" / "x" / "fine.py"
    scratch.parent.mkdir(parents=True)
    scratch.write_text(source, encoding="utf-8")
    assert scan(scratch.read_text(encoding="utf-8"), "app/x/fine.py") == []
