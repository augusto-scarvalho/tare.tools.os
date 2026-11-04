#!/usr/bin/env python3
"""W29.N5 — AST test-quality proxies (observe-only).

Coverage says a line RAN; it never says a test would notice if that line
lied. arXiv:2607.12068 measures the cheap static proxies that do correlate
with fault detection — assertion density, edge-case and null-safety
exercise, and flaky-dependency surface — so this module reads them straight
off the AST of the test files a diff touched:

  assertionDensity     — assertions per 100 physical lines
  edgeCaseHits         — boundary literals (0, -1, "", empty containers)
  nullSafetyHits       — None comparisons / None passed to an assertion
  flakinessCandidates  — clock/random/net/fs/order dependencies

FOUR SEPARATE signals, never a composite score: the W29 round rejected a
single quality number, and averaging these back into one throws away the
only thing they are good at — saying WHICH kind of teeth a test is missing.

Observe-only, like the mutation probe: this module measures and persists one
ledger point; no gate check reads it yet (N6 owns the control phase). The
keys are frozen in result_contracts.TEST_QUALITY_SIGNALS (W29.N4) and this
producer writes exactly those, validated BEFORE anything is persisted.

These are proxies, not proof: they count shapes, not semantics. Every
failure path is fail-open — an unreadable or unparsable file is skipped by
name, never a crash. Self-check: python scripts/harness_lib/test_quality_ast.py.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))
from harness_lib import mutation_probe  # noqa: E402 — read-only reuse of changed_python
from harness_lib.result_contracts import (  # noqa: E402 — N4 pieces are bind()-free
    TEST_QUALITY_SIGNALS,
    validate_test_quality_signals,
)

# Frozen flakiness buckets: shapes that make a test depend on something it does
# not control. Pattern grammar: "a.b" exact (or dotted suffix), "*.b" any call to
# an attribute named b, "m.*" any call under module m. Kept as five named
# constants so a hand-mutant has one data line per bucket to edit.
FLAKY_CLOCK = ("time.sleep", "time.time", "*.now", "*.utcnow", "*.today")
FLAKY_RANDOM = ("random.*", "uuid.uuid4")
FLAKY_NET = ("socket", "requests", "urllib.request", "http.client", "ssl")  # imports
FLAKY_FS = ("os.stat", "os.path.getmtime", "*.stat")  # mtime-dependent
FLAKY_ORDER = ("os.listdir", "os.walk", "glob.glob", "*.iterdir")
# tempfile is deliberately ABSENT: it is this repo's blessed self-check pattern,
# and counting it would flag every good demo.
# ponytail: order bucket counts all listdir/glob; sorted()-wrapper detection when
# false positives hurt.
_CALL_BUCKETS = (("clock", FLAKY_CLOCK), ("random", FLAKY_RANDOM),
                 ("fs", FLAKY_FS), ("order", FLAKY_ORDER))
_NULL_OPS = (ast.Is, ast.IsNot, ast.Eq, ast.NotEq)


def _dotted(node: ast.AST) -> str:
    """Dotted source name of a call target ("time.sleep"); the attribute tail
    alone when the base is not a plain name (`foo().stat` -> "stat")."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _matches(dotted: str, pattern: str) -> bool:
    if pattern.startswith("*."):
        return dotted.rsplit(".", 1)[-1] == pattern[2:]
    if pattern.endswith(".*"):
        return dotted.startswith(pattern[:-1])
    return dotted == pattern or dotted.endswith("." + pattern)


def _is_boundary(node: ast.AST) -> bool:
    """0, -1, "" and empty containers ([], {}, (), set()). None is EXCLUDED —
    it belongs to nullSafetyHits, and counting it twice would blur two signals."""
    if isinstance(node, ast.Constant):
        return not isinstance(node.value, bool) and node.value in (0, "")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        # -1 is UnaryOp(USub, Constant(1)) in the AST, never Constant(-1)
        return isinstance(node.operand, ast.Constant) and node.operand.value == 1
    if isinstance(node, (ast.List, ast.Dict, ast.Tuple, ast.Set)):
        return not getattr(node, "elts", None) and not getattr(node, "keys", None)
    return isinstance(node, ast.Call) and _dotted(node.func) == "set" and not node.args


def _is_assert_call(node: ast.Call) -> bool:
    """This repo's scenario idiom `check(...)` plus unittest's `assert*` methods."""
    return ((isinstance(node.func, ast.Name) and node.func.id == "check")
            or (isinstance(node.func, ast.Attribute) and node.func.attr.startswith("assert")))


def _none_compares(node: ast.Compare) -> int:
    """`x is None` / `x == None` and their negations — the null-safety shape."""
    if not any(isinstance(op, _NULL_OPS) for op in node.ops):
        return 0
    return sum(1 for operand in [node.left, *node.comparators]
               if isinstance(operand, ast.Constant) and operand.value is None)


def _net_imports(node: ast.Import | ast.ImportFrom) -> int:
    mods = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
            else [node.module or ""])
    return sum(1 for m in mods
               if any(m == p or m.startswith(p + ".") for p in FLAKY_NET))


def analyze_file(path: Path | str) -> dict[str, Any] | None:
    """Per-file breakdown, or None when the file cannot be read or parsed
    (fail-open: a broken file is reported by name, it never fails a run)."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(text)
    except Exception:  # noqa: BLE001 — unreadable/unparsable is a skip, not a crash
        return None
    loc = len(text.splitlines())
    asserts = edge = nulls = 0
    buckets = {"clock": 0, "random": 0, "net": 0, "fs": 0, "order": 0}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            asserts += 1
        elif isinstance(node, ast.Compare):
            edge += sum(1 for operand in [node.left, *node.comparators]
                        if _is_boundary(operand))
            nulls += _none_compares(node)
        elif isinstance(node, ast.Call):
            if _is_assert_call(node):
                asserts += 1
                edge += sum(1 for a in node.args if _is_boundary(a))
                nulls += sum(1 for a in node.args
                             if isinstance(a, ast.Constant) and a.value is None)
            dotted = _dotted(node.func)
            for name, patterns in _CALL_BUCKETS:
                if any(_matches(dotted, p) for p in patterns):
                    buckets[name] += 1
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            buckets["net"] += _net_imports(node)
    return {"loc": loc, "asserts": asserts,
            "assertionDensity": round(100 * asserts / max(loc, 1), 2),
            "edgeCaseHits": edge, "nullSafetyHits": nulls,
            "flakinessCandidates": sum(buckets.values()), "buckets": buckets}


def signals_of(breakdown: dict[str, Any]) -> dict[str, Any]:
    """The TEST_QUALITY_SIGNALS subset of a breakdown (drops loc/asserts/buckets)."""
    return {k: breakdown[k] for k in TEST_QUALITY_SIGNALS if k in breakdown}


def _is_test_surface(rel: str) -> bool:
    # changed_python normalizes backslashes only in its `??` branch — normalize here.
    norm = rel.replace("\\", "/")
    # overseer review catch (W29.N5 integration): harness library code is never a
    # test surface — without the scripts/ exclusion this module's own filename
    # (test_quality_ast.py) selects the producer to measure itself.
    return norm.endswith(".py") and (norm.startswith("testing/scenarios/")
                                     or (norm.rsplit("/", 1)[-1].startswith("test_")
                                         and not norm.startswith("scripts/")))


def analyze_diff(root: Path | str, diff: str = "HEAD") -> dict[str, Any]:
    """Signals over the test surfaces this diff touched. The diff decides WHICH
    files are analyzed, NOT which lines: a test's quality is a property of the
    whole file, not of the three lines someone happened to edit."""
    root = Path(root)
    try:
        changed = sorted(mutation_probe.changed_python(root, diff))
    except Exception:  # noqa: BLE001 — no git / not a repo: nothing to observe
        changed = []
    files: dict[str, dict[str, Any]] = {}
    skipped: list[str] = []
    for rel in changed:
        norm = rel.replace("\\", "/")
        if not _is_test_surface(norm):
            continue
        breakdown = analyze_file(root / norm)
        if breakdown is None:
            skipped.append(norm)
        else:
            files[norm] = breakdown
    asserts = sum(f["asserts"] for f in files.values())
    loc = sum(f["loc"] for f in files.values())
    signals: dict[str, Any] = {}
    if files:  # a clean tree reports NO signals rather than a fabricated 0.0
        signals = {"assertionDensity": round(100 * asserts / max(loc, 1), 2),
                   "edgeCaseHits": sum(f["edgeCaseHits"] for f in files.values()),
                   "nullSafetyHits": sum(f["nullSafetyHits"] for f in files.values()),
                   "flakinessCandidates": sum(f["flakinessCandidates"]
                                              for f in files.values())}
    return {"diff": diff, "files": files, "signals": signals, "skipped": skipped}


def _record_point(root: Path, point: dict[str, Any]) -> None:
    """One data point in the records ledger; ledger loss never breaks the read."""
    sig = point["signals"]
    try:
        from harness_lib import records
        records.add_entry(root, "note",
                          f"test-quality: files={point['fileCount']} " +
                          " ".join(f"{k}={sig.get(k, 0)}" for k in TEST_QUALITY_SIGNALS),
                          body=json.dumps(point, sort_keys=True),
                          tags=["test-quality", "w29"])
    except Exception:  # noqa: BLE001 — observe-only: a ledger failure is not a failure
        pass


def record(root: Path | str, diff: str = "HEAD") -> dict[str, Any]:
    """Persist ONE point, but only after the shared schema accepts the signals —
    on validation errors nothing is written and the errors come back instead."""
    root = Path(root)
    report = analyze_diff(root, diff)
    point = {"mode": "diff", "diff": diff, "signals": report["signals"],
             "fileCount": len(report["files"]), "skipped": report["skipped"]}
    errors = validate_test_quality_signals(point["signals"])
    if errors:
        return {**point, "errors": errors}
    _record_point(root, point)
    return point


def latest_point(root: Path | str) -> dict[str, Any] | None:
    """Newest recorded test-quality point (hot worklog first, then archive) —
    W29.N6's gate check and EXP-32 read this; None when nothing was recorded.
    Mirrors mutation_probe.trend's tag-scan read path, never a new store."""
    from harness_lib import records as records_mod
    from harness_lib.common import read_json

    root = Path(root)
    points: list[dict[str, Any]] = []
    for rel in (records_mod.WORKLOG_REL, records_mod.ARCHIVE_REL):
        for entry in (read_json(root / rel, {}) or {}).get("entries") or []:
            if "test-quality" not in (entry.get("tags") or []):
                continue
            try:
                body = json.loads(entry.get("body") or "{}")
            except Exception:  # noqa: BLE001 — a torn body is skipped, never raised
                continue
            points.append({"at": entry.get("at"), **body})
    points.sort(key=lambda p: str(p.get("at") or ""))
    return points[-1] if points else None


def gate_check(root: Path | str) -> list[tuple[str, str, str]]:
    """W29.N6 observe-only gate row (name, status, detail): surface the newest
    recorded point — no threshold, never fails; an empty ledger is honest, not
    red. Lives here (not spec_test_gate) per the gs-7 line ratchet."""
    try:
        point = latest_point(Path(root))
        sig = (point or {}).get("signals") or {}
        detail = ("no test-quality points recorded yet" if point is None else
                  f"at={point.get('at')} files={point.get('fileCount')} "
                  + " ".join(f"{k}={sig[k]}" for k in sorted(sig)))
    except Exception as exc:  # noqa: BLE001 — observe-only: the reader fails open
        detail = f"reader failed open: {exc!r}"
    return [("test-quality", "pass", detail[:200])]


_SNIPPET = ("import time\n"
            "\n"
            "\n"
            "def probe(x):\n"
            "    assert x == 0\n"
            "    time.sleep(0)\n"
            "    return x is None\n")
_SNIPPET_EXPECTED = {"loc": 7, "asserts": 1, "assertionDensity": 14.29,
                     "edgeCaseHits": 1, "nullSafetyHits": 1, "flakinessCandidates": 1,
                     "buckets": {"clock": 1, "random": 0, "net": 0, "fs": 0, "order": 0}}


def _demo() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        good = Path(tmp) / "test_snippet.py"
        good.write_text(_SNIPPET, encoding="utf-8")
        breakdown = analyze_file(good)
        assert breakdown == _SNIPPET_EXPECTED, breakdown
        assert validate_test_quality_signals(signals_of(breakdown)) == []

        broken = Path(tmp) / "test_broken.py"
        broken.write_text("def broken(:\n", encoding="utf-8")
        assert analyze_file(broken) is None  # fail-open on a syntax error
        assert analyze_file(Path(tmp) / "absent.py") is None  # and on a missing file

        # survivor-killer (integration 2026-07-28): a mutant that counted ANY
        # constant compare as a null hit still scored 1 on _SNIPPET (it traded
        # `x is None` for `x == 0`). A non-boundary, non-None compare must score
        # zero on BOTH null and edge — this pins WHICH compare landed.
        plain = Path(tmp) / "test_plain.py"
        plain.write_text("def f(x):\n    assert x == 5\n", encoding="utf-8")
        pb = analyze_file(plain) or {}
        assert (pb.get("nullSafetyHits"), pb.get("edgeCaseHits"), pb.get("asserts")) == (0, 0, 1), pb

    edges = ast.parse("assert a == -1\nassert b == ''\nassert c == []\n"
                      "assert d == {}\nassert e == ()\nassert f == set()\n")
    hits = sum(_is_boundary(o) for n in ast.walk(edges) if isinstance(n, ast.Compare)
               for o in [n.left, *n.comparators])
    assert hits == 6, hits  # -1 is a UnaryOp, not a Constant — all six must land
    assert not _is_boundary(ast.parse("True", mode="eval").body)  # bool is not 0
    assert _is_test_surface("testing/scenarios/x.py") and _is_test_surface("a\\b\\test_x.py")
    assert not _is_test_surface("scripts/harness.py") and not _is_test_surface("t/test_x.txt")
    # the producer never measures itself (scripts/ exclusion on the test_ arm)
    assert not _is_test_surface("scripts/harness_lib/test_quality_ast.py")
    print("test_quality_ast self-check: ok")


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--record" in argv:
        target = "HEAD"
        if "--diff" in argv and argv.index("--diff") + 1 < len(argv):
            target = argv[argv.index("--diff") + 1]
        print(json.dumps(record(_SCRIPTS.parent, diff=target), indent=2, sort_keys=True))
    else:
        _demo()
