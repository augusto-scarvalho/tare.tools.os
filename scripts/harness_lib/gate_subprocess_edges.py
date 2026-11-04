#!/usr/bin/env python3
"""wf-subprocess-edges (SPEC-159 v3, SHADOW) — static subprocess/source-read edges.

The SPEC-159 import-closure affected-set has a blind spot: a scenario drives the
CLI through a SUBPROCESS (`harness.py ...`), not an import edge, so a change to
`harness.py`/`common.py`/... cannot be traced and is forced to a GLOBAL full-run
(the FB-2 floor). This module recovers those edges STATICALLY, pure-stdlib `ast`,
so a future affected-map (`affectedV2`) can narrow the floor without losing safety.

For each `.py` graph node under `scripts/` + `testing/scenarios/` it extracts, by
ONE collection rule, an edge to every OTHER graph node whose repo-relative path it
resolves at rest:

  - module-level literal path resolution: `ROOT / "a" / "b"` chains, `str(...)` /
    `Path(...)` wraps, and Name-assignment following (incl. a `HARNESS = [...]` list
    and its starred `[*HARNESS, ...]` use);
  - the SAME rule covers subprocess argv literals, `read_text()` targets, and the
    `bash py-run.sh scripts/harness.py ...` string-literal invocation.

Two special cases:

  - _lib.py WRAPPER EXCEPTION: no edges attach to `testing/scenarios/_lib.py`
    itself; instead a `run(...)`/`scrub(...)`/`_lib.run(...)` call INSIDE a scenario
    adds that scenario -> `scripts/harness.py`. (ponytail: `_lib` is imported by
    every scenario but only *run-callers* actually reach the CLI; attaching the
    edge at the call site keeps non-callers narrow. Upgrade path: a real call graph
    that follows `_lib.run` into `subprocess` instead of this name heuristic.)
  - unresolved: a subprocess call whose PROGRAM element is opaque (an f-string or a
    variable we cannot follow) makes the whole file `unresolved: True` — its blast
    radius is unknown, so the merged affected-map treats it as always-affected.
    `-c`/`-m` inline runs and literal non-node paths are *resolved, no edge*.

Fail-open by construction: an `ast.parse` failure or an unreadable file yields
`unresolved: True` and NEVER raises; a broken extractor degrades to all-unresolved,
which the affected-map reads as all-affected (conservative). Output shape:
`{path: {"edges": set[str], "unresolved": bool}}`. Functions take `root` + an
injected graph — NO module-level ROOT in the compute paths (the acceptance tests
fabricate synthetic roots).
"""
from __future__ import annotations

import ast
from pathlib import Path

SCEN_REL = "testing/scenarios"
WRAPPER = "testing/scenarios/_lib.py"
WRAPPER_EDGE_TARGET = "scripts/harness.py"
WRAPPER_CALLS = {"run", "scrub"}                       # _lib helpers a scenario invokes
SUBPROCESS_FUNCS = {"run", "Popen", "call", "check_call", "check_output"}
_INTERPRETERS = {"python", "python3", "bash", "sh"}
_INLINE = object()                                      # sentinel: -c/-m inline run (resolved, no program path)


def _node_paths(graph: dict | None) -> set[str]:
    out: set[str] = set()
    for n in (graph or {}).get("nodes", []):
        if isinstance(n, dict):
            p = n.get("path") or n.get("id")
            if p:
                out.add(str(p).replace("\\", "/"))
    return out


# --- resolution --------------------------------------------------------------

def _collect_names(tree: ast.AST) -> dict[str, ast.AST]:
    """Flat name -> assigned-value AST over the whole module.
    ponytail: last-wins, no scope tracking — a wrong shadow of a rare rebound name
    only ever adds an edge (conservative); real scope needs a resolver we do not."""
    names: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            names[node.targets[0].id] = node.value
    return names


def _resolve(node: ast.AST, names: dict[str, ast.AST], depth: int = 0) -> str | None:
    """Resolve an expression to a repo-relative POSIX path, or None. Handles the
    `ROOT` convention (-> repo root ""), `/`-join chains, `str()`/`Path()` wraps,
    string literals, and one hop of Name-assignment following. Depth-bounded."""
    if depth > 25:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.replace("\\", "/")
    if isinstance(node, ast.Name):
        if node.id == "ROOT":                           # the repo-wide ROOT convention
            return ""
        val = names.get(node.id)
        return _resolve(val, names, depth + 1) if val is not None else None
    if isinstance(node, ast.Call):
        fn = node.func
        if isinstance(fn, ast.Name) and fn.id in ("str", "Path") and node.args:
            return _resolve(node.args[0], names, depth + 1)
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _resolve(node.left, names, depth + 1)
        right = _resolve(node.right, names, depth + 1)
        if left is None or right is None:
            return None
        return f"{left}/{right}".strip("/")
    return None


# --- subprocess argv / program-element analysis ------------------------------

def _argv_elements(argv: ast.AST, names: dict[str, ast.AST]) -> tuple[list[ast.AST], bool]:
    """The element nodes of a subprocess argv, expanding a top-level Name->list and
    `*list` splats. Returns ([], False) for an opaque argv (a Name we cannot expand,
    or an f-string shell command) so the caller marks the file unresolved."""
    node = argv
    if isinstance(node, ast.Name):
        resolved = names.get(node.id)
        if not isinstance(resolved, (ast.List, ast.Tuple)):
            return [], False
        node = resolved
    if not isinstance(node, (ast.List, ast.Tuple)):
        return [], False
    out: list[ast.AST] = []
    for el in node.elts:
        if isinstance(el, ast.Starred):
            inner = el.value
            lst = names.get(inner.id) if isinstance(inner, ast.Name) else None
            if isinstance(lst, (ast.List, ast.Tuple)):
                out.extend(lst.elts)
            # else opaque *args (function varargs) -> variadic DATA, not the program: skip
            continue
        out.append(el)
    return out, True


def _is_interpreter(el: ast.AST, names: dict[str, ast.AST]) -> bool:
    if isinstance(el, ast.Attribute) and el.attr == "executable":   # sys.executable
        return True
    r = _resolve(el, names)
    if r is None:
        return False
    return r in _INTERPRETERS or r.rsplit("/", 1)[-1] in _INTERPRETERS


def _program_element(elements: list[ast.AST], names: dict[str, ast.AST]):
    """The AST node of the executed PROGRAM; None (empty argv); or _INLINE (`-c`/`-m`).
    When argv[0] is an interpreter, the program is the first non-flag arg after it."""
    if not elements:
        return None
    i = 0
    if _is_interpreter(elements[0], names):
        i = 1
        while i < len(elements):
            el = elements[i]
            if isinstance(el, ast.Constant) and isinstance(el.value, str) and el.value.startswith("-"):
                if el.value in ("-c", "-m"):
                    return _INLINE
                i += 1                                    # a plain flag (-u/-B/...); the script follows
                continue
            break
        if i >= len(elements):
            return _INLINE                                # bare `python`/`python -u` — nothing dynamic to run
    return elements[i]


def _subprocess_unresolved(call: ast.Call, names: dict[str, ast.AST]) -> bool:
    """True iff this subprocess call's PROGRAM element cannot be statically pinned."""
    if not call.args:
        return False
    elements, expandable = _argv_elements(call.args[0], names)
    if not expandable:
        return True
    prog = _program_element(elements, names)
    if prog is None:
        return True
    if prog is _INLINE:
        return False
    if isinstance(prog, ast.JoinedStr):
        return True
    if _is_interpreter(prog, names):
        return False
    return _resolve(prog, names) is None


def _is_subprocess_call(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in SUBPROCESS_FUNCS
            and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess")


def _is_wrapper_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    fn = node.func
    if isinstance(fn, ast.Name) and fn.id in WRAPPER_CALLS:                  # run(...) / scrub(...)
        return True
    return (isinstance(fn, ast.Attribute) and fn.attr in WRAPPER_CALLS       # _lib.run(...) / _lib.scrub(...)
            and isinstance(fn.value, ast.Name) and fn.value.id == "_lib")


# --- fb3-verb-level: verb -> module map + per-file called verbs --------------
# The wrapper edge scenario->harness.py pulls the WHOLE CLI import universe into
# every run()-caller's reach. Verb-level edges recover the real granularity: the
# registry says which module each verb's handler traverses, the call site says
# which verbs a scenario actually invokes. Consumers (the FB-3 docs refinement)
# fall back to the full harness closure on ANY doubt: unknown verb, opaque call,
# unparseable registry.

def _harness_cmd_refs(src: str) -> dict[str, set[str]]:
    """Function name -> harness_lib MODULE NAMES its body references, fixpoint
    over local helper calls. Counts top-level `from harness_lib import x [as y]`
    aliases, in-function lazy imports (the harness.py idiom), and `harness_lib.x`
    attribute chains. Never raises; a parse error yields {}."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}
    aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "harness_lib":
            for a in node.names:
                aliases[a.asname or a.name] = a.name
    funcs = {n.name: n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    direct: dict[str, set[str]] = {}
    calls: dict[str, set[str]] = {}
    for name, fn in funcs.items():
        mods: set[str] = set()
        called: set[str] = set()
        for node in ast.walk(fn):
            if isinstance(node, ast.ImportFrom) and node.module == "harness_lib":
                mods.update(a.name for a in node.names)
            elif isinstance(node, ast.Name) and node.id in aliases:
                mods.add(aliases[node.id])
            elif (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                  and node.value.id == "harness_lib"):
                mods.add(node.attr)
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                  and node.func.id in funcs):
                called.add(node.func.id)
        direct[name] = mods
        calls[name] = called
    # fixpoint over the local call graph (cycles converge: sets only grow).
    changed = True
    while changed:
        changed = False
        for name in funcs:
            before = len(direct[name])
            for c in calls[name]:
                direct[name] |= direct.get(c, set())
            changed = changed or len(direct[name]) > before
    return direct


def registry_verb_map(root: Path, graph: dict | None) -> dict[str, set[str]] | None:
    """Top-level CLI verb -> set of harness_lib module NODE PATHS the verb's
    handler traverses. Direct bindings (`func=MOD.fn` in cli_registry.register)
    map exactly; h-bound bindings (`func=h.cmd_x`) map through the cmd_x
    function-body reference analysis of harness.py. A verb ABSENT from the map
    (workflow tree, parse gap) has unknown reach; the whole map is None on any
    error. Both mean: caller falls back to the full harness closure."""
    try:
        nodes = _node_paths(graph)
        reg_src = (Path(root) / "scripts" / "harness_lib" / "cli_registry.py").read_text(encoding="utf-8")
        tree = ast.parse(reg_src)
        register = next((n for n in tree.body if isinstance(n, ast.FunctionDef)
                         and n.name == "register"), None)
        if register is None:
            return None
        cmd_refs: dict[str, set[str]] | None = None  # lazy: parse harness.py once
        var_verb: dict[str, str] = {}
        out: dict[str, set[str]] = {}
        # SOURCE-ORDER traversal (ast.walk is BFS: every set_defaults would see
        # the LAST-assigned parser var and collapse all verbs onto one). The
        # register() body is flat `p = add_parser(...); p.set_defaults(...)`
        # statements; verbs inside nested constructs simply don't map (fail-open).
        stmts: list[ast.AST] = []
        for stmt in register.body:
            stmts.append(stmt)
            if isinstance(stmt, ast.Expr):
                stmts[-1] = stmt.value
        for node in stmts:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Attribute)
                    and node.value.func.attr == "add_parser"
                    and isinstance(node.value.func.value, ast.Name)
                    and node.value.func.value.id == "sub"  # TOP-LEVEL verbs only
                    and node.value.args and isinstance(node.value.args[0], ast.Constant)):
                var_verb[node.targets[0].id] = str(node.value.args[0].value)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "set_defaults"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in var_verb):
                verb = var_verb[node.func.value.id]
                func_kw = next((k.value for k in node.keywords if k.arg == "func"), None)
                if not (isinstance(func_kw, ast.Attribute) and isinstance(func_kw.value, ast.Name)):
                    continue
                base, fn = func_kw.value.id, func_kw.attr
                if base == "h":
                    if cmd_refs is None:
                        h_src = (Path(root) / "scripts" / "harness.py").read_text(encoding="utf-8")
                        cmd_refs = _harness_cmd_refs(h_src)
                    if fn in cmd_refs:
                        out[verb] = {p for m in cmd_refs[fn]
                                     if (p := f"scripts/harness_lib/{m}.py") in nodes}
                    # fn not found in harness.py -> verb stays ABSENT (unknown reach)
                else:
                    mod_path = f"scripts/harness_lib/{base}.py"
                    out[verb] = {mod_path} if mod_path in nodes else set()
        return out or None
    except Exception:  # noqa: BLE001 — fail-open: None => full-closure fallback
        return None


def _wrapper_verb(call: ast.Call) -> str | None:
    """The literal verb of a run(...) wrapper call, or None (opaque)."""
    if not call.args:
        return None
    a0 = call.args[0]
    return a0.value if isinstance(a0, ast.Constant) and isinstance(a0.value, str) else None


def _subprocess_harness_verb(call: ast.Call, names: dict[str, ast.AST]) -> tuple[str | None, bool]:
    """(verb, is_harness_call) for a direct subprocess argv that invokes
    scripts/harness.py (python form or the bash py-run.sh form). verb None with
    is_harness_call True = the verb element is not a literal (opaque)."""
    if not call.args:
        return None, False
    elements, ok = _argv_elements(call.args[0], names)
    if not ok:
        return None, False  # opaque argv is already flagged unresolved elsewhere
    for i, el in enumerate(elements):
        r = _resolve(el, names)
        if r and r.replace("\\", "/").endswith("scripts/harness.py"):
            for nxt in elements[i + 1:]:
                if isinstance(nxt, ast.Constant) and isinstance(nxt.value, str):
                    if nxt.value.startswith("-"):
                        continue  # a flag before the verb; keep scanning
                    return nxt.value, True
                return None, True  # non-literal verb element -> opaque
            return None, True  # bare harness.py call, no verb literal
    return None, False


# --- per-file + whole-graph extraction ---------------------------------------

def extract_file(src: str, path: str, nodes: set[str]) -> dict:
    """{edges, unresolved} for one file's source. Never raises: a parse error is
    an unresolved file (unknown blast radius), the fail-open safe default."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {"edges": set(), "unresolved": True, "docsReads": set(),
                "execEdges": set(), "wrapperVerbs": set(), "wrapperOpaque": True}
    names = _collect_names(tree)
    is_scenario = path.startswith(SCEN_REL + "/")
    # fb3: docs literals inside `_self_check` bodies are TEST FIXTURES by repo
    # convention (dep_graph/scenario_isolation carry the backlog path only as
    # self-check data) — collecting them makes every-closure modules readers of
    # everything. Skipped for docs collection ONLY; edges/unresolved still count.
    self_spans = [(n.lineno, getattr(n, "end_lineno", n.lineno) or n.lineno)
                  for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "_self_check"]

    def _in_self_check(n: ast.AST) -> bool:
        ln = getattr(n, "lineno", None)
        return ln is not None and any(a <= ln <= b for a, b in self_spans)

    edges: set[str] = set()
    exec_edges: set[str] = set()
    docs: set[str] = set()
    verbs: set[str] = set()
    verb_opaque = False
    unresolved = False
    for node in ast.walk(tree):
        r = _resolve(node, names)                          # ONE collection rule
        if r and r != path and r in nodes:
            edges.add(r)
        # fb3-hub-fanin lever (a): argv elements are EXECUTED programs/args; a
        # node path inside a subprocess argv is an exec edge. Everything else
        # (read_text targets, docstrings) stays a source-read edge — reading a
        # .py file's BYTES cannot transport a docs change, so the docs-affected
        # BFS follows exec edges only. `edges` (the union) keeps feeding the V2
        # shadow unchanged.
        if _is_subprocess_call(node) and node.args:
            elements, ok = _argv_elements(node.args[0], names)
            if ok:
                for el in elements:
                    er = _resolve(el, names)
                    if er and er != path and er in nodes:
                        exec_edges.add(er)
        # fb3-docs-path-refinement: docs/**.md literals are NOT graph nodes, but a
        # file resolving one is a docs READER — the FB-3 refinement bounds a docs
        # change by these instead of ALL-affected. A docs literal WITHOUT the .md
        # suffix is a DIRECTORY-listing read (research_admin's "docs/research"):
        # collected as a trailing-slash PREFIX entry matching every docs path
        # under it. The BARE "docs/" literal is dropped: it appears in
        # `path.startswith("docs/")` PATH-CLASS checks (release_integrity,
        # scenario_isolation, this very refinement), which read no docs at all —
        # keeping it would flag readers-of-everything and kill the refinement.
        # Fixture/docstring literals otherwise over-collect, which only ever ADDS
        # readers (conservative: runs more, never less).
        if r and r.startswith("docs/") and r != "docs/" and not _in_self_check(node):
            docs.add(r if r.endswith(".md") else r.rstrip("/") + "/")
        if is_scenario and WRAPPER_EDGE_TARGET in nodes and _is_wrapper_call(node):
            edges.add(WRAPPER_EDGE_TARGET)
            exec_edges.add(WRAPPER_EDGE_TARGET)
            fn = node.func
            fname = fn.id if isinstance(fn, ast.Name) else fn.attr
            if fname == "scrub":
                verbs.add("workflow")   # _lib.scrub always drives `workflow scrub`
            else:
                v = _wrapper_verb(node)
                if v is None:
                    verb_opaque = True
                else:
                    verbs.add(v)
        if _is_subprocess_call(node):
            if _subprocess_unresolved(node, names):
                unresolved = True
            else:
                hv, is_h = _subprocess_harness_verb(node, names)
                if is_h:
                    if hv is None:
                        verb_opaque = True
                    else:
                        verbs.add(hv)
    return {"edges": edges, "unresolved": unresolved, "docsReads": docs,
            "execEdges": exec_edges, "wrapperVerbs": verbs, "wrapperOpaque": verb_opaque}


def extract(root: Path, graph: dict | None) -> dict[str, dict]:
    """{path: {edges, unresolved}} over every `.py` graph node under `scripts/` +
    `testing/scenarios/`. `_lib.py` is the wrapper exception (edge-free). A missing
    or unreadable file degrades to unresolved (conservative), never raises.

    ponytail: parses O(nodes) small files per call (~314 today, sub-second). Cache
    keyed on the git index if this ever shows on the gate-perf drum."""
    nodes = _node_paths(graph)
    out: dict[str, dict] = {}
    for path in sorted(p for p in nodes if p.endswith(".py")
                       and (p.startswith("scripts/") or p.startswith(SCEN_REL + "/"))):
        if path == WRAPPER:
            # ponytail: the wrapper exception hides _lib's own reads too — _lib reads
            # no docs today; revisit if it ever grows a docs literal.
            out[path] = {"edges": set(), "unresolved": False, "docsReads": set(),
                         "execEdges": set(), "wrapperVerbs": set(), "wrapperOpaque": False}
            continue
        try:
            src = (Path(root) / path).read_text(encoding="utf-8")
        except OSError:
            out[path] = {"edges": set(), "unresolved": True, "docsReads": set(),
                         "execEdges": set(), "wrapperVerbs": set(), "wrapperOpaque": True}
            continue
        out[path] = extract_file(src, path, nodes)
    return out


# --- self-check --------------------------------------------------------------

def _self_check() -> int:
    import tempfile

    node_paths = [
        "scripts/harness.py", "scripts/spec_test_gate.py", "scripts/harness_lib/helper.py",
        "testing/scenarios/rootjoin.py", "testing/scenarios/assign.py",
        "testing/scenarios/starred.py", "testing/scenarios/pyrun.py",
        "testing/scenarios/reader.py", "testing/scenarios/dyn.py",
        "testing/scenarios/wrap.py", "testing/scenarios/_lib.py",
        "testing/scenarios/docreader.py",
    ]
    graph = {"schemaVersion": "graphify-code-ast/v1",
             "nodes": [{"path": p, "id": p} for p in node_paths], "edges": []}

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        def w(rel: str, src: str) -> None:
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(src, encoding="utf-8")

        w("scripts/harness.py", "x = 1\n")
        w("scripts/spec_test_gate.py", "x = 1\n")
        w("scripts/harness_lib/helper.py", "x = 1\n")
        # ROOT-join in a subprocess argv
        w("testing/scenarios/rootjoin.py",
          "import subprocess, sys\nfrom pathlib import Path\nROOT = Path('.')\n"
          "subprocess.run([sys.executable, str(ROOT / 'scripts' / 'harness.py'), 'gate'])\n")
        # Name-assignment following
        w("testing/scenarios/assign.py",
          "import subprocess, sys\nfrom pathlib import Path\nROOT = Path('.')\n"
          "TARGET = ROOT / 'scripts' / 'spec_test_gate.py'\n"
          "subprocess.run([sys.executable, str(TARGET)])\n")
        # starred HARNESS list splat
        w("testing/scenarios/starred.py",
          "import subprocess, sys\nfrom pathlib import Path\nROOT = Path('.')\n"
          "HARNESS = [sys.executable, str(ROOT / 'scripts' / 'harness.py')]\n"
          "subprocess.run([*HARNESS, 'gate', 'scenarios'])\n")
        # bash py-run.sh scripts/harness.py string-literal invocation
        w("testing/scenarios/pyrun.py",
          "import subprocess\n"
          "subprocess.run(['bash', 'tools/agent-sync/py-run.sh', 'scripts/harness.py', 'gate-perf'])\n")
        # read_text source target
        w("testing/scenarios/reader.py",
          "from pathlib import Path\nROOT = Path('.')\n"
          "src = (ROOT / 'scripts' / 'harness_lib' / 'helper.py').read_text()\n")
        # docs reader (fb3): a docs/**.md literal is collected even though docs
        # paths are never graph nodes
        w("testing/scenarios/docreader.py",
          "from pathlib import Path\nROOT = Path('.')\n"
          "txt = (ROOT / 'docs' / 'IMPLEMENTATION_BACKLOG.md').read_text()\n")
        # unresolved: an f-string program element
        w("testing/scenarios/dyn.py",
          "import subprocess, sys\nm = 'x'\nsubprocess.run([sys.executable, f'{m}.py'])\n")
        # wrapper call
        w("testing/scenarios/wrap.py", "from _lib import run\nrun('gate', 'scenarios')\n")
        # _lib wrapper: subprocesses harness.py itself, yet must stay edge-free
        w("testing/scenarios/_lib.py",
          "import subprocess, sys\nfrom pathlib import Path\nROOT = Path('.')\n"
          "HARNESS = [sys.executable, str(ROOT / 'scripts' / 'harness.py')]\n"
          "def run(*a):\n    return subprocess.run([*HARNESS, *a])\n")

        out = extract(root, graph)

        def e(name: str) -> set:
            return out[f"testing/scenarios/{name}.py"]["edges"]

        assert e("rootjoin") == {"scripts/harness.py"}, out["testing/scenarios/rootjoin.py"]
        assert e("assign") == {"scripts/spec_test_gate.py"}, out["testing/scenarios/assign.py"]
        assert e("starred") == {"scripts/harness.py"}, out["testing/scenarios/starred.py"]
        assert e("pyrun") == {"scripts/harness.py"}, out["testing/scenarios/pyrun.py"]
        assert e("reader") == {"scripts/harness_lib/helper.py"}, out["testing/scenarios/reader.py"]
        assert e("wrap") == {"scripts/harness.py"}, out["testing/scenarios/wrap.py"]
        # fb3: the docs literal is collected as a docsRead (no edge — not a node)
        assert out["testing/scenarios/docreader.py"] == {
            "edges": set(), "unresolved": False,
            "docsReads": {"docs/IMPLEMENTATION_BACKLOG.md"},
            "execEdges": set(), "wrapperVerbs": set(), "wrapperOpaque": False}, out["testing/scenarios/docreader.py"]
        assert out["testing/scenarios/reader.py"]["docsReads"] == set()
        # fb3: a non-.md docs literal is a directory-listing PREFIX read
        assert extract_file(
            "R = 'docs/research'\nfrom pathlib import Path\np = Path(R)\n",
            "testing/scenarios/lister.py", set(node_paths))["docsReads"] == {"docs/research/"}
        # lever (a): argv-borne node paths are EXEC edges; a read_text target is
        # a source-read edge (in `edges` but NOT in `execEdges`).
        assert out["testing/scenarios/rootjoin.py"]["execEdges"] == {"scripts/harness.py"}
        assert out["testing/scenarios/reader.py"]["execEdges"] == set()
        assert out["testing/scenarios/reader.py"]["edges"] == {"scripts/harness_lib/helper.py"}
        assert out["testing/scenarios/wrap.py"]["execEdges"] == {"scripts/harness.py"}
        # fb3: bare "docs/" (a startswith PATH-CLASS check) is NOT a read; a docs
        # literal inside a `_self_check` body is fixture data, also NOT a read.
        assert extract_file(
            "def f(p):\n    return p.startswith('docs/')\n",
            "scripts/harness_lib/classy.py", set(node_paths))["docsReads"] == set()
        assert extract_file(
            "REAL = 'docs/live.md'\n"
            "def _self_check():\n    x = 'docs/fixture-only.md'\n    return 0\n",
            "scripts/harness_lib/mixed.py", set(node_paths))["docsReads"] == {"docs/live.md"}
        # unresolved f-string program -> unresolved file, no edge
        assert out["testing/scenarios/dyn.py"] == {"edges": set(), "unresolved": True,
                                                   "docsReads": set(), "execEdges": set(),
                                                   "wrapperVerbs": set(),
                                                   "wrapperOpaque": False}, out["testing/scenarios/dyn.py"]
        # WRAPPER EXCEPTION: _lib.py is edge-free and not unresolved
        assert out["testing/scenarios/_lib.py"] == {"edges": set(), "unresolved": False,
                                                    "docsReads": set(), "execEdges": set(),
                                                    "wrapperVerbs": set(),
                                                    "wrapperOpaque": False}, out["testing/scenarios/_lib.py"]
        # fb3-verb-level: the literal verb rides out of every harness call shape —
        # run() wrapper, python argv, starred splat, and the py-run.sh form.
        assert out["testing/scenarios/wrap.py"]["wrapperVerbs"] == {"gate"}
        assert out["testing/scenarios/rootjoin.py"]["wrapperVerbs"] == {"gate"}
        assert out["testing/scenarios/starred.py"]["wrapperVerbs"] == {"gate"}
        assert out["testing/scenarios/pyrun.py"]["wrapperVerbs"] == {"gate-perf"}
        assert not any(out[f"testing/scenarios/{k}.py"]["wrapperOpaque"]
                       for k in ("wrap", "rootjoin", "starred", "pyrun"))
        # a non-literal verb after harness.py -> opaque; scrub() -> "workflow"
        opq = extract_file(
            "import subprocess, sys\nfrom pathlib import Path\nROOT = Path('.')\n"
            "v = 'x'\nsubprocess.run([sys.executable, str(ROOT / 'scripts' / 'harness.py'), v])\n",
            "testing/scenarios/opq.py", set(node_paths))
        assert opq["wrapperOpaque"] is True and opq["wrapperVerbs"] == set()
        scr = extract_file("from _lib import scrub\nscrub('WF-1')\n",
                           "testing/scenarios/scr.py", set(node_paths))
        assert scr["wrapperVerbs"] == {"workflow"} and scr["wrapperOpaque"] is False
        # every resolvable-program file is resolved (not unresolved)
        for k in ("rootjoin", "assign", "starred", "pyrun", "reader", "wrap", "docreader"):
            assert out[f"testing/scenarios/{k}.py"]["unresolved"] is False, k
        # a `python -c` inline run is resolved-no-edge (not unresolved)
        assert extract_file(
            "import subprocess, sys\nc='p'\nsubprocess.run([sys.executable, '-c', c])\n",
            "testing/scenarios/inline.py", set(node_paths)) == {
                "edges": set(), "unresolved": False, "docsReads": set(),
                "execEdges": set(), "wrapperVerbs": set(), "wrapperOpaque": False}
        # a parse error fails open to unresolved (verbs opaque too)
        assert extract_file("def (:\n", "scripts/broken.py", set(node_paths)) == {
            "edges": set(), "unresolved": True, "docsReads": set(),
            "execEdges": set(), "wrapperVerbs": set(), "wrapperOpaque": True}

        # fb3-verb-level: registry_verb_map — direct binding maps exactly; an
        # h-bound handler maps through harness.py's cmd body (lazy import +
        # helper-call fixpoint); an unregistered verb stays ABSENT.
        w("scripts/harness_lib/cli_registry.py",
          "def register(sub, h):\n"
          "    p = sub.add_parser('direct'); p.set_defaults(func=helper.cmd_direct)\n"
          "    p = sub.add_parser('hbound'); p.set_defaults(func=h.cmd_hb)\n"
          "    p = sub.add_parser('ghost'); p.set_defaults(func=h.cmd_missing)\n")
        w("scripts/harness.py",
          "from harness_lib import helper\n"
          "def _aux():\n"
          "    from harness_lib import spec_index\n"
          "def cmd_hb(args):\n"
          "    helper.go()\n"
          "    _aux()\n")
        node_paths2 = set(node_paths) | {"scripts/harness_lib/cli_registry.py",
                                         "scripts/harness_lib/spec_index.py"}
        graph2 = {"schemaVersion": "graphify-code-ast/v1",
                  "nodes": [{"path": p, "id": p} for p in sorted(node_paths2)], "edges": []}
        vm = registry_verb_map(root, graph2)
        assert vm is not None
        assert vm["direct"] == {"scripts/harness_lib/helper.py"}, vm["direct"]
        assert vm["hbound"] == {"scripts/harness_lib/helper.py",
                                "scripts/harness_lib/spec_index.py"}, vm["hbound"]
        assert "ghost" not in vm, vm.keys()  # handler not found -> unknown reach
        assert registry_verb_map(Path(tmp) / "nowhere", graph2) is None  # fail-open

    print("gate_subprocess_edges self-check OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_check())
