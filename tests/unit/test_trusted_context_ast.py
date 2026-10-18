"""V2-19 (V2_PG_CONTRACTS.md §9): only the allow-listed modules can mint or use a trusted context.

The scanner resolves symbols through the import table of each module: it never matches substrings
(``_issue(`` already appears in unrelated app modules). Outside the §9 allow-list
(``app/services/os_ledger/{authorize,owner_direct,api}.py`` and ``app/api/os_v2/commits.py``;
``tests/`` is never scanned) it flags:
  * any import of ``_issue``, ``_CAPABILITY`` or ``TrustedOwnerContext`` from the api module;
  * any attribute access ``api._issue`` / ``api._CAPABILITY`` on that module, however imported;
  * any import from, or call into, ``app.services.os_ledger.owner_direct``.
Stricter pins sit on top:
  * ``api._issue`` is referenced by ``authorize.py`` alone;
  * the browser mutation proof (``owner_sessions._mutation_proven``, ``_attach_mutation_proof``,
    ``_MutationProof``) is referenced outside ``owner_sessions.py`` only by ``authorize.py``, and
    there only through ``_mutation_proven``;
  * review r1: the private fields themselves, ``._capability`` (outside ``api.py`` and
    ``authorize.py``) and ``._mutation_proof`` (outside ``owner_sessions.py``), as an attribute,
    a constant ``getattr``/``setattr``/``hasattr`` or a keyword argument
    (``dataclasses.replace(p, _mutation_proof=...)``).
  * review r2: any private name of ``authorize`` (``_register``, ``_mint``, ``_MINTED`` and every
    other ``authorize._*``) outside ``authorize.py``, and the private field names as exact string
    constants (``vars(p)['_mutation_proof']``, ``replace(p, **{'_mutation_proof': x})``).
Aliases are followed through plain, annotated, tuple and walrus assignments, ``sys.modules[...]``,
``vars(...)`` and ``__dict__``. Planted violations are scratch files under ``tmp_path``.
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
AUTHORIZE = "app.services.os_ledger.authorize"
AUTHORIZE_FILE = "app/services/os_ledger/authorize.py"
CONTEXT_NAMES = frozenset({"_issue", "_CAPABILITY", "TrustedOwnerContext"})
CONTEXT_ATTRS = frozenset({"_issue", "_CAPABILITY"})
PROOF_NAMES = frozenset({"_mutation_proven", "_attach_mutation_proof", "_MutationProof"})
PRIVATE_FIELDS = frozenset({"_capability", "_mutation_proof"})
GUARDED_MODULES = frozenset({API, DIRECT, SESSIONS})
ALLOW_LIST = frozenset({
    "app/services/os_ledger/authorize.py",
    "app/services/os_ledger/owner_direct.py",
    "app/services/os_ledger/api.py",
    "app/api/os_v2/commits.py",
})
ISSUE_ALLOW_LIST = frozenset({"app/services/os_ledger/authorize.py",
                              "app/services/os_ledger/api.py"})
PROOF_ALLOW_LIST = frozenset({"app/api/owner_sessions.py", "app/services/os_ledger/authorize.py"})
FIELD_ALLOW_LIST = {
    "_capability": frozenset({"app/services/os_ledger/api.py",
                              "app/services/os_ledger/authorize.py"}),
    "_mutation_proof": frozenset({"app/api/owner_sessions.py"}),
}
CONTEXT_RULES = frozenset({"context_import", "context_attribute", "owner_direct", "star_import",
                           "module_table"})


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
        if isinstance(node, ast.Subscript) and self._modules_table(node):
            return self._modules_table(node)
        if isinstance(node, ast.NamedExpr):
            return self.resolve(node.value)
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

    def _modules_table(self, node: ast.Subscript) -> str | None:
        """``sys.modules['app.services.os_ledger.api']`` resolves to that module."""
        if self.resolve(node.value) != "sys.modules":
            return None
        key = node.slice
        return key.value if isinstance(key, ast.Constant) and isinstance(key.value, str) else None

    def _bind(self, target: ast.AST, value: ast.AST | None) -> None:
        if value is None:
            return
        if isinstance(target, ast.Name):
            resolved = self.resolve(value)
            if resolved:
                self.bound[target.id] = resolved
        elif isinstance(target, ast.Tuple | ast.List) and isinstance(value, ast.Tuple | ast.List):
            for element, item in zip(target.elts, value.elts, strict=False):
                self._bind(element, item)

    # -- rules -----------------------------------------------------------------------------

    def _check(self, line: int, name: str | None) -> None:
        if not name:
            return
        if name == DIRECT or name.startswith(DIRECT + "."):
            self.findings.add((line, "owner_direct", name))
        if name in {f"{API}.{attr}" for attr in CONTEXT_ATTRS}:
            self.findings.add((line, "context_attribute", name))
        if name in {f"{SESSIONS}.{proof}" for proof in PROOF_NAMES}:
            self.findings.add((line, "mutation_proof", name))
        if name in {f"{module}.__dict__" for module in GUARDED_MODULES}:
            self.findings.add((line, "module_table", name))
        if name.startswith(AUTHORIZE + "._"):
            self.findings.add((line, "authorize_private", name))

    def _field(self, line: int, attr: str) -> None:
        if attr in PRIVATE_FIELDS:
            self.findings.add((line, "private_field", attr))

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
                if source in GUARDED_MODULES:
                    self.findings.add((node.lineno, "star_import", source))
                continue
            full = f"{source}.{alias.name}"
            if source == API and alias.name in CONTEXT_NAMES:
                self.findings.add((node.lineno, "context_import", full))
            self._check(node.lineno, source)
            self._check(node.lineno, full)
            self.bound[alias.asname or alias.name] = full

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._bind(target, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._bind(node.target, node.value)
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._bind(node.target, node.value)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._check(node.lineno, self.resolve(node))
        self._field(node.lineno, node.attr)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        table = self._modules_table(node)
        if table in GUARDED_MODULES:
            self.findings.add((node.lineno, "module_table", table))
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self._field(getattr(node, "lineno", 0), node.value)

    def visit_keyword(self, node: ast.keyword) -> None:
        if node.arg:
            self._field(getattr(node, "lineno", 0), node.arg)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        self._check(node.lineno, self._import_call(node))
        func = node.func
        if isinstance(func, ast.Name) and func.id in {"getattr", "setattr", "hasattr"} and len(
                node.args) >= 2:
            target, attr = self.resolve(node.args[0]), node.args[1]
            if isinstance(attr, ast.Constant) and isinstance(attr.value, str):
                self._field(node.lineno, attr.value)
                if target:
                    self._check(node.lineno, f"{target}.{attr.value}")
        if isinstance(func, ast.Name) and func.id == "vars" and node.args:
            target = self.resolve(node.args[0])
            if target in GUARDED_MODULES:
                self.findings.add((node.lineno, "module_table", target))
        self.generic_visit(node)


def scan(source: str, relative: str) -> list[tuple[int, str, str]]:
    scanner = Scanner(relative)
    scanner.visit(ast.parse(source))
    return sorted(scanner.findings)


def disallowed(findings: list[tuple[int, str, str]], relative: str) -> list[tuple[int, str, str]]:
    """The findings ``relative`` is not allowed to have, rule by rule."""
    out = []
    for line, rule, name in findings:
        if rule in CONTEXT_RULES and relative in ALLOW_LIST:
            continue
        if rule == "mutation_proof" and relative == "app/api/owner_sessions.py":
            continue
        if rule == "mutation_proof" and relative == "app/services/os_ledger/authorize.py" and (
                name == f"{SESSIONS}._mutation_proven"):
            continue
        if rule == "private_field" and relative in FIELD_ALLOW_LIST.get(name, frozenset()):
            continue
        if rule == "authorize_private" and relative == AUTHORIZE_FILE:
            continue
        out.append((line, rule, name))
    return out


def app_modules() -> list[tuple[str, str]]:
    found = []
    for path in sorted(APP.rglob("*.py")):
        relative = path.relative_to(REPO).as_posix()
        found.append((relative, path.read_text(encoding="utf-8")))
    return found


# ---------------------------------------------------------------------------------------------
# The real tree


def test_no_app_module_mints_reaches_or_forges_a_trusted_context_outside_its_allow_list():
    violations = {
        relative: bad
        for relative, source in app_modules()
        if (bad := disallowed(scan(source, relative), relative))
    }
    assert violations == {}


def test_only_authorize_references_the_issuer():
    issuers = {
        relative
        for relative, source in app_modules()
        if any(rule in {"context_attribute", "context_import"} and name.endswith("._issue")
               for _, rule, name in scan(source, relative))
    }
    assert issuers == {"app/services/os_ledger/authorize.py"}
    assert issuers <= ISSUE_ALLOW_LIST


def test_only_authorize_checks_the_mutation_proof_and_only_through_mutation_proven():
    holders = {
        relative: {name for _, rule, name in scan(source, relative) if rule == "mutation_proof"}
        for relative, source in app_modules()
    }
    holders = {relative: names for relative, names in holders.items() if names}
    assert holders == {"app/services/os_ledger/authorize.py": {f"{SESSIONS}._mutation_proven"}}
    assert set(holders) <= PROOF_ALLOW_LIST


def test_the_private_fields_are_touched_only_where_they_live():
    touching = {
        relative: {name for _, rule, name in scan(source, relative) if rule == "private_field"}
        for relative, source in app_modules()
    }
    touching = {relative: names for relative, names in touching.items() if names}
    assert touching == {
        "app/services/os_ledger/api.py": {"_capability"},
        "app/services/os_ledger/authorize.py": {"_capability"},
        "app/api/owner_sessions.py": {"_mutation_proof"},
    }


def test_the_allow_listed_modules_are_the_ones_the_contract_names():
    assert (APP / "services" / "os_ledger" / "authorize.py").is_file()
    assert (APP / "services" / "os_ledger" / "api.py").is_file()
    assert (APP / "api" / "os_v2" / "commits.py").is_file()
    # authorize.py really is caught by the scanner when it is not allow-listed.
    source = (APP / "services" / "os_ledger" / "authorize.py").read_text(encoding="utf-8")
    flagged = {rule for _, rule, _ in disallowed(
        scan(source, "app/services/os_ledger/evil.py"), "app/services/os_ledger/evil.py")}
    assert flagged >= {"context_attribute", "mutation_proof", "private_field"}


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
    "annotated_alias": ("app/x/evil.py",
                        "from typing import Any\nfrom app.services.os_ledger import api\n"
                        "m: Any = api\nm._issue()\n"),
    "tuple_alias": ("app/x/evil.py",
                    "from app.services.os_ledger import api\nx, m = 1, api\nm._CAPABILITY\n"),
    "walrus_alias": ("app/x/evil.py",
                     "from app.services.os_ledger import api\n(m := api)\nm._issue()\n"),
    "sys_modules_table": ("app/x/evil.py",
                          "import sys\nm = sys.modules['app.services.os_ledger.api']\n"),
    "vars_of_api": ("app/x/evil.py",
                    "from app.services.os_ledger import api\nvars(api)['_issue']\n"),
    "dict_of_api": ("app/x/evil.py",
                    "from app.services.os_ledger import api\napi.__dict__['_CAPABILITY']\n"),
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
    "proof_check_import": ("app/api/evil.py",
                           "from app.api.owner_sessions import _mutation_proven\n"),
    "proof_attach_attribute": (
        "app/api/evil.py",
        "from app.api import owner_sessions\np = owner_sessions._attach_mutation_proof(x)\n"),
    "proof_class": ("app/api/evil.py",
                    "from app.api import owner_sessions\nowner_sessions._MutationProof()\n"),
    # review r1: the private fields themselves
    "capability_attribute": ("app/x/evil.py", "def f(ctx):\n    return ctx._capability\n"),
    "capability_getattr": ("app/x/evil.py",
                           "def f(ctx):\n    return getattr(ctx, '_capability')\n"),
    "capability_keyword": ("app/x/evil.py",
                           "def f(T, fields, cap):\n"
                           "    return T(**fields, _capability=cap)\n"),
    "proof_attribute": ("app/x/evil.py", "def f(p):\n    return p._mutation_proof\n"),
    "proof_borrowed_by_replace": (
        "app/x/evil.py",
        "import dataclasses\n"
        "def f(read, proven):\n"
        "    return dataclasses.replace(read, _mutation_proof=proven)\n"),
    "proof_setattr": ("app/x/evil.py",
                      "def f(q):\n    setattr(q, '_mutation_proof', 1)\n"),
    # review r2: authorize's own private entry points, and the field names as plain strings
    "authorize_register": ("app/x/evil.py",
                           "from app.services.os_ledger import authorize\n"
                           "def f(c):\n    return authorize._register(c)\n"),
    "authorize_mint": ("app/x/evil.py",
                       "from app.services.os_ledger import authorize\n"
                       "authorize._mint('OS', 1, None, op='x', scopes=frozenset(),\n"
                       "                expires=None)\n"),
    "authorize_registry_write": ("app/x/evil.py",
                                 "from app.services.os_ledger import authorize as a\n"
                                 "a._MINTED[1] = None\n"),
    "authorize_private_import": ("app/x/evil.py",
                                 "from app.services.os_ledger.authorize import _register\n"),
    "proof_by_vars": ("app/x/evil.py", "def f(p):\n    return vars(p)['_mutation_proof']\n"),
    "proof_by_dict_unpack": ("app/x/evil.py",
                             "import dataclasses\n"
                             "def f(p, x):\n"
                             "    return dataclasses.replace(p, **{'_mutation_proof': x})\n"),
    "capability_by_dict": ("app/x/evil.py", "def f(c):\n    return c.__dict__['_capability']\n"),
}


@pytest.mark.parametrize("case", sorted(PLANTED))
def test_a_planted_violation_is_flagged(case, tmp_path):
    relative, source = PLANTED[case]
    scratch = tmp_path / relative
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_text(source, encoding="utf-8")
    assert disallowed(scan(scratch.read_text(encoding="utf-8"), relative), relative), case


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
        "text = 'api._issue( and owner_direct.commit( and ctx._capability'\n"
        "# api._CAPABILITY and p._mutation_proof\n"
        "api.LedgerError('kill_switch')\n"
        "def g(spec):\n    return spec.read_capability, spec._capabilities, spec.capability\n"
        "execute_capability = 1\n"
        "from app.services.os_ledger import authorize\n"
        "authorize.from_owner_read\n"
        "authorize.require\n"
        "label = 'the _capability field'\n"
    )
    scratch = tmp_path / "app" / "x" / "fine.py"
    scratch.parent.mkdir(parents=True)
    scratch.write_text(source, encoding="utf-8")
    assert disallowed(scan(scratch.read_text(encoding="utf-8"), "app/x/fine.py"),
                      "app/x/fine.py") == []
