"""QA.1: static bind()-env completeness check (kills the latent-NameError class).

harness_lib modules receive shared helpers via `bind(env)` at import time; a
name the module references but no binder supplies only explodes when that code
path finally runs (bit 3x during MF.1 round 2 alone). This checker resolves,
purely from AST:
- every `X.bind(ARG)` call site in the binders (direct or loop form), with ARG
  as a dict literal, a Name whose top-level assignment is a dict literal
  (plus the EXPORTED_FUNCTIONS union when that dict is mutated afterwards),
  or `globals()` (= the binder's own defined names);
- every bound module's free names (Name loads minus everything the module
  defines anywhere, over-approximating locals on purpose);
and fails naming `module: name (binder)` for each unsatisfied free name.
"""
from __future__ import annotations

import ast
import builtins
import sys
from pathlib import Path

BINDER_RELS = ("scripts/harness.py", "scripts/spec_test_gate.py")
_GLOBALS = "<globals>"
_IMPLICIT = {"__name__", "__file__", "__doc__", "__spec__", "__package__"}
_BUILTINS = set(dir(builtins))


def _defined_names(tree: ast.AST) -> set[str]:
    """Every name the file binds anywhere: stores, defs, imports, args, handlers."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
    return names


def _free_names(tree: ast.AST) -> set[str]:
    loads = {n.id for n in ast.walk(tree)
             if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return loads - _defined_names(tree) - _BUILTINS - _IMPLICIT


def _dict_literal_keys(node: ast.expr) -> set[str] | None:
    if isinstance(node, ast.Dict):
        return {k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    return None


def _lib_aliases(tree: ast.AST) -> dict[str, str]:
    """import alias -> harness_lib module name."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "harness_lib":
            for alias in node.names:
                out[alias.asname or alias.name] = alias.name
    return out


def _exported_function_keys(lib_dir: Path) -> set[str]:
    """Union of literal EXPORTED_FUNCTIONS dict keys across harness_lib."""
    keys: set[str] = set()
    for path in sorted(lib_dir.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "EXPORTED_FUNCTIONS"
                            for t in node.targets)):
                keys |= _dict_literal_keys(node.value) or set()
                if isinstance(node.value, (ast.List, ast.Tuple)):  # list form is the common one
                    keys |= {e.value for e in node.value.elts
                             if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return keys


def _binder_bind_map(tree: ast.AST, exported: set[str]) -> dict[str, set[str] | str]:
    """alias of bound module -> env name set (or _GLOBALS sentinel)."""
    dict_assigns: dict[str, set[str]] = {}
    mutated: set[str] = set()
    explicit_adds: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            keys = _dict_literal_keys(node.value)
            if keys is not None:
                dict_assigns[node.targets[0].id] = keys
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("setdefault", "update") \
                and isinstance(node.func.value, ast.Name):
            owner = node.func.value.id
            if (node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                explicit_adds.setdefault(owner, set()).add(node.args[0].value)
            else:
                mutated.add(owner)
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            mutated.add(node.value.id)

    def resolve_env(arg: ast.expr) -> set[str] | str | None:
        keys = _dict_literal_keys(arg)
        if keys is not None:
            return keys
        if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) \
                and arg.func.id == "globals":
            return _GLOBALS
        if isinstance(arg, ast.Name):
            keys = dict_assigns.get(arg.id)
            if keys is None:
                return None
            keys = keys | explicit_adds.get(arg.id, set())
            if arg.id in mutated:
                keys = keys | exported  # runtime re-injection, over-approximated
            return keys
        return None

    out: dict[str, set[str] | str] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "bind" and node.args):
            continue
        env = resolve_env(node.args[0])
        if env is None or not isinstance(node.func.value, ast.Name):
            continue
        owner = node.func.value.id
        out[owner] = env
    # loop form: for _m in (mod_a, mod_b): _m.bind(ENV) — spread env to elements
    for node in ast.walk(tree):
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name) \
                and node.target.id in out \
                and isinstance(node.iter, (ast.Tuple, ast.List)):
            env = out.pop(node.target.id)
            for element in node.iter.elts:
                if isinstance(element, ast.Name):
                    out[element.id] = env
    return out


def check_bind_completeness(root: Path) -> list[str]:
    """Failure details `module: name (binder)`; empty list = complete."""
    lib_dir = root / "scripts" / "harness_lib"
    exported = _exported_function_keys(lib_dir)
    details: list[str] = []
    bound: dict[str, list[tuple[str, set[str]]]] = {}  # module name -> [(binder, env names)]
    for binder_rel in BINDER_RELS:
        binder_path = root / binder_rel
        if not binder_path.exists():
            continue
        tree = ast.parse(binder_path.read_text(encoding="utf-8"))
        aliases = _lib_aliases(tree)
        binder_names = _defined_names(tree) | exported
        for owner, env in _binder_bind_map(tree, exported).items():
            module = aliases.get(owner)
            if not module:
                continue
            names = binder_names if env == _GLOBALS else set(env)  # type: ignore[arg-type]
            bound.setdefault(module, []).append((binder_rel, names))
    for module, binders in sorted(bound.items()):
        module_path = lib_dir / f"{module}.py"
        if not module_path.exists():
            details.append(f"{module}: module file missing")
            continue
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        if not any(isinstance(n, (ast.FunctionDef,)) and n.name == "bind" for n in tree.body):
            continue  # bound-but-bindless would fail at runtime immediately; not our class
        free = _free_names(tree)
        for binder_rel, env_names in binders:
            for name in sorted(free - env_names):
                details.append(f"{module}: {name} ({binder_rel})")
    return details


def _self_check() -> None:
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    lib = tmp / "scripts" / "harness_lib"
    lib.mkdir(parents=True)
    (lib / "modx.py").write_text(
        "def bind(env):\n    globals().update(env)\n\n"
        "def work():\n    return helper(OTHER_CONST) + local_fn()\n\n"
        "def local_fn():\n    return 1\n", encoding="utf-8")
    (lib / "donor.py").write_text(
        "EXPORTED_FUNCTIONS = {'injected_fn': None}\n", encoding="utf-8")
    binder = (tmp / "scripts" / "harness.py")
    binder.write_text(
        "from harness_lib import modx\n"
        "_ENV = {'helper': 1}\n"
        "modx.bind(_ENV)\n", encoding="utf-8")
    (tmp / "scripts" / "spec_test_gate.py").write_text("", encoding="utf-8")
    # OTHER_CONST unsatisfied -> flagged; helper satisfied; local_fn own def
    details = check_bind_completeness(tmp)
    assert details == ["modx: OTHER_CONST (scripts/harness.py)"], details
    # mutated env unions EXPORTED_FUNCTIONS; globals() env uses binder names
    binder.write_text(
        "from harness_lib import modx\n"
        "OTHER_CONST = 2\n"
        "_ENV = {'helper': 1}\n"
        "for _n in ('a',):\n    _ENV.setdefault(_n, 1)\n"
        "_ENV.setdefault('extra_name', OTHER_CONST)\n"
        "modx.bind(_ENV)\n", encoding="utf-8")
    assert check_bind_completeness(tmp) == [f"modx: OTHER_CONST (scripts/harness.py)"]
    (lib / "modx.py").write_text(
        "def bind(env):\n    globals().update(env)\n\n"
        "def work():\n    return injected_fn(helper)\n", encoding="utf-8")
    assert check_bind_completeness(tmp) == [], check_bind_completeness(tmp)  # setdefault -> exported ok
    binder.write_text(
        "from harness_lib import modx\nOTHER_CONST = 2\nmodx.bind(globals())\n",
        encoding="utf-8")
    (lib / "modx.py").write_text(
        "def bind(env):\n    globals().update(env)\n\n"
        "def work():\n    return injected_fn() + OTHER_CONST\n", encoding="utf-8")
    # globals() env = binder names ∪ EXPORTED_FUNCTIONS (runtime re-export loops)
    assert check_bind_completeness(tmp) == [], check_bind_completeness(tmp)
    (lib / "modx.py").write_text(
        "def bind(env):\n    globals().update(env)\n\n"
        "def work():\n    return never_defined_anywhere()\n", encoding="utf-8")
    details = check_bind_completeness(tmp)
    assert details == ["modx: never_defined_anywhere (scripts/harness.py)"], details
    print("bind_completeness self-check ok")


if __name__ == "__main__":
    _self_check()
