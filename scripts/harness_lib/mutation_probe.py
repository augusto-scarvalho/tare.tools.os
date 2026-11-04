#!/usr/bin/env python3
"""Mutation-probe oracle (CQ.5 slice, observe-only) — `oracle mutate` (mpo).

The code-quality research round's #2 hole: the only per-change functional
oracle is a self-check written by the SAME implementer — the author's test
formalizes the author's misreading. This probe plants ONE deterministic
mutant per changed function (cap 3) and demands the diff's own scenario FAIL:

  killed    — the scenario caught the mutant (the oracle has teeth)
  survived  — the scenario passed anyway (weak oracle: the headline finding)
  error     — the scenario crashed on the mutant (rc outside 0/1)
  timeout   — the run exceeded the bound
  skipped   — no scenario linked to the diff / no mutable site found

Observe-only (measurement before control, the security-baseline idiom): the
VERB always exits 0. The control phase now exists but lives OUTSIDE the verb —
`validation_stamp.check_mutate` is the commit-ledger's third leg, and it reads
the `mutate-run` / `mutate-waiver` rows this module writes. Mutants are
applied to a byte-snapshotted file and restored in a finally block — the
tree is byte-identical afterwards no matter what.
Self-check: python scripts/harness_lib/mutation_probe.py.
"""
from __future__ import annotations

import ast
import re
import sys
import time
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))
from harness_lib import processes, telemetry_sink, validation_stamp  # noqa: E402

MAX_MUTANTS = 3
RUN_TIMEOUT = 120
# The exitClass vocabulary, verbatim (module docstring + probe()'s classifier).
# `counts` in a mutate-run row is keyed by EXACTLY these names — check_mutate
# reads counts["survived"], so a renamed class must break here, loudly.
MUTATE_CLASSES = ("killed", "survived", "error", "timeout", "skipped")
_CMP_SWAP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE, ast.GtE: ast.Lt,
             ast.Gt: ast.LtE, ast.LtE: ast.Gt}


def _git(root: Path, *args: str) -> str:
    proc = processes.run_quiet(["git", "-C", str(root), *args], timeout=60,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace")
    return proc.stdout or ""


def changed_python(root: Path, diff: str = "HEAD") -> dict[str, set[int]]:
    """{rel_path: changed NEW-side line numbers} for changed .py files.
    Untracked .py files count whole-file: loop workers never `git add`, so
    their new scenarios/modules only exist as `??` status entries."""
    raw = _git(root, "diff", diff, "--unified=0", "--", "*.py")
    out: dict[str, set[int]] = {}
    current: str | None = None
    for line in raw.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:].strip()
            out.setdefault(current, set())
        elif line.startswith("@@") and current:
            m = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2) or "1")
                out[current].update(range(start, start + max(count, 1)))
    for line in _git(root, "status", "--porcelain", "-uall").splitlines():
        if line.startswith("??"):
            rel = line[3:].strip().strip('"').replace("\\", "/")
            path = root / rel
            if rel.endswith(".py") and path.is_file():
                n = len(path.read_text(encoding="utf-8", errors="ignore").splitlines())
                out[rel] = set(range(1, n + 1))
    return {p: ls for p, ls in out.items() if ls}


def changed_functions(root: Path, diff: str = "HEAD") -> list[tuple[str, str]]:
    """(rel_path, function_name) pairs whose span contains changed lines.
    Test/scenario/hook files are never mutation targets."""
    pairs: list[tuple[str, str]] = []
    for rel, lines in changed_python(root, diff).items():
        norm = rel.replace("\\", "/")
        if norm.startswith(("testing/", "tools/hooks/")) or not (root / rel).exists():
            continue
        try:
            tree = ast.parse((root / rel).read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                span = set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
                if span & lines:
                    pairs.append((norm, node.name))
    return list(dict.fromkeys(pairs))


class _Mutator(ast.NodeTransformer):
    """Apply exactly ONE mutation inside the target function, priority order:
    comparison flip > and/or swap > if-negation > return-None. With a non-empty
    `prefer_lines` only nodes on those lines qualify; `mutate_source` retries
    without it, so the diff's OWN operator is probed before any pre-existing
    first site (row oracle-mutate-probes-one-site-per-change: first-site-anywhere
    both false-alarmed on lines the author never wrote and false-comforted by
    leaving the added operator unprobed)."""

    def __init__(self, func_name: str, prefer_lines: frozenset[int] = frozenset()) -> None:
        self.func_name = func_name
        self.prefer_lines = prefer_lines
        self.inside = False
        self.applied: str | None = None
        self.line: int | None = None

    def _eligible(self, node: ast.AST) -> bool:
        return not self.prefer_lines or getattr(node, "lineno", -1) in self.prefer_lines

    def visit_FunctionDef(self, node):  # noqa: N802
        if node.name == self.func_name and self.applied is None:
            self.inside = True
            self.generic_visit(node)
            if self.applied is None:  # weaker fallbacks, still single-shot
                for child in ast.walk(node):
                    if isinstance(child, ast.If) and self.applied is None and self._eligible(child):
                        child.test = ast.UnaryOp(op=ast.Not(), operand=child.test)
                        self.applied, self.line = "negate-if", child.lineno
                        break
                if self.applied is None:
                    for child in ast.walk(node):
                        if isinstance(child, ast.Return) and child.value is not None and self._eligible(child):
                            child.value = ast.Constant(value=None)
                            self.applied, self.line = "return-none", child.lineno
                            break
            self.inside = False
        else:
            self.generic_visit(node)
        return node

    visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

    def visit_Compare(self, node):  # noqa: N802
        if self.inside and self.applied is None and node.ops and self._eligible(node):
            swap = _CMP_SWAP.get(type(node.ops[0]))
            if swap:
                node.ops[0] = swap()
                self.applied, self.line = "flip-comparison", node.lineno
                return node
        self.generic_visit(node)
        return node

    def visit_BoolOp(self, node):  # noqa: N802
        if self.inside and self.applied is None and self._eligible(node):
            node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
            self.applied, self.line = "swap-bool-op", node.lineno
            return node
        self.generic_visit(node)
        return node


def mutate_source(source: str, func_name: str, prefer_lines: frozenset[int] = frozenset()
                  ) -> tuple[str, str, int | None, str] | None:
    """(mutated_source, mutation_name, mutated_line, site) or None when no
    mutable site exists. site == "in-diff" when the mutation landed on one of
    `prefer_lines` (the changed hunk); "fallback" is the pre-existing
    first-site-anywhere behavior — read its survivors knowing the probed line
    may not be the line the diff wrote."""
    for prefer in dict.fromkeys((frozenset(prefer_lines), frozenset())):
        tree = ast.parse(source)
        mutator = _Mutator(func_name, prefer)
        mutator.visit(tree)
        if mutator.applied is not None:
            return (ast.unparse(tree), mutator.applied, mutator.line,
                    "in-diff" if prefer else "fallback")
    return None


def linked_scenarios(root: Path, diff: str = "HEAD") -> list[str]:
    return sorted(p for p in changed_python(root, diff)
                  if p.replace("\\", "/").startswith("testing/scenarios/")
                  and not Path(p).name.startswith("_"))


# Tokens too common to carry linkage signal on their own (every GUI scenario
# shares "ui"/"gui"); excluded from the name-stem overlap test below.
_GENERIC_TOKENS = {"ui", "gui", "py"}


def _linkage(root: Path, diff: str, scenarios: list[str]) -> dict[str, Any]:
    """Git + graph facts a mutant needs to find its oracle, gathered ONCE (both
    are costly): which linked scenarios are NEW in this diff, each scenario's
    source text (for the module-mention test), and the SPEC-159 closure map."""
    new: set[str] = set()
    for line in _git(root, "diff", diff, "--name-status", "--", "*.py").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0][:1] == "A":
            new.add(parts[-1].strip().replace("\\", "/"))
    for line in _git(root, "status", "--porcelain", "-uall").splitlines():
        if line.startswith("??"):
            new.add(line[3:].strip().strip('"').replace("\\", "/"))
    sources: dict[str, str] = {}
    for scen in scenarios:
        try:
            sources[scen] = (root / scen).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            sources[scen] = ""
    closures: dict[str, Any] = {}
    try:
        from harness_lib import gate_affected
        closures = gate_affected.scenario_closures(
            root, gate_affected.load_graph(root / "graphify-out" / "graph.json"))
    except Exception:  # noqa: BLE001 - linkage aid only, never break the probe
        closures = {}
    return {"new": new, "sources": sources, "closures": closures}


def _tightest(cands: list[str], closures: dict[str, Any], norm: str) -> str:
    """Among already-qualified candidates, the one whose import closure is
    smallest AND contains the mutated file; else the alphabetically-first
    candidate (deterministic, graph-independent)."""
    best: str | None = None
    best_size = 0
    for scen in cands:
        cl = closures.get(Path(scen).stem)
        if cl and norm in cl and (best is None or len(cl) < best_size):
            best, best_size = scen, len(cl)
    return best or sorted(cands)[0]


def _scenario_for(root: Path, rel: str, scenarios: list[str],
                  ctx: dict[str, Any] | None = None, diff: str = "HEAD",
                  fn: str | None = None) -> str:
    """Pick the linked scenario most likely to KILL a mutant in `rel`, in order:
      (a0) a scenario whose source names the mutated FUNCTION (compound names
           only — the most specific evidence there is; audit dacbdc8 F3: a
           workflow_spawn_command_for_prompt mutant was judged vs a smaller-
           closure scenario that never calls it);
      (a) a scenario NEW in this diff whose name-stem shares a token with the
          mutated module OR whose source mentions the module name;
      (b) a MODIFIED-in-diff scenario mentioning the module;
      (c) the SPEC-159 closure heuristic (smallest closure containing the file);
      (d) the pre-fix fallback: the first linked scenario.
    Pure/deterministic, no LLM. Fixes the 2026-07-22 miss where async_state /
    ui_panel / ui_actions / ui_queue mutants were judged vs the big m5_ui_panel
    while the KILLING scenario was the new-in-diff qf/ck/rf file (10 bogus
    ORACLE-WEAK verdicts in one day). A bare single-word module stem (e.g.
    'harness') is too generic to text-match (its substring is in every scenario)
    so mention-matching applies only to compound (underscore) module names;
    single-word stems fall straight through to the closure heuristic."""
    if len(scenarios) <= 1:
        return scenarios[0]
    if ctx is None:
        ctx = _linkage(root, diff, scenarios)
    new_set, sources, closures = ctx["new"], ctx["sources"], ctx["closures"]
    norm = rel.replace("\\", "/")
    module = Path(rel).stem
    mod_tokens = set(module.split("_")) - _GENERIC_TOKENS
    compound = "_" in module

    def shares_token(scen: str) -> bool:
        return bool((set(Path(scen).stem.split("_")) - _GENERIC_TOKENS) & mod_tokens)

    def mentions(scen: str) -> bool:
        return compound and module in sources.get(scen, "")

    new = [s for s in scenarios if s in new_set]
    modified = [s for s in scenarios if s not in new_set]
    # (a0) a scenario that names the mutated function itself
    if fn and "_" in fn:
        a0 = [s for s in scenarios if fn in sources.get(s, "")]
        if a0:
            return _tightest(a0, closures, norm)
    # (a) NEW-in-diff scenario that names or mentions the module
    a = [s for s in new if shares_token(s) or mentions(s)]
    if a:
        return _tightest(a, closures, norm)
    # (b) MODIFIED-in-diff scenario that mentions the module
    b = [s for s in modified if mentions(s)]
    if b:
        return _tightest(b, closures, norm)
    # (c) closure heuristic: smallest closure that contains the file
    best: str | None = None
    best_size = 0
    for scen in scenarios:
        cl = closures.get(Path(scen).stem)
        if cl and norm in cl and (best is None or len(cl) < best_size):
            best, best_size = scen, len(cl)
    # (d) pre-fix fallback
    return best or scenarios[0]


def defect_sink(root: Path) -> Path:
    """SPEC-173 Phase 0 shared defect sink, anchored to the `root` PARAMETER
    (landmine L4): scratch-repo rows evaporate with the tmp dir."""
    return root / ".harness" / "state" / "defect-telemetry.jsonl"


def staged_fingerprint(root: Path) -> str | None:
    """The commit-ledger's staged-surface fingerprint — validation_stamp's own,
    never a second implementation. None when the policy is absent/disabled or git
    cannot be read: the probe is observe-only and must never fail over a ledger it
    only annotates (check_mutate then simply finds no fp-matching row)."""
    try:
        policy = validation_stamp.load_policy(root)
        if policy is None:
            return None
        return validation_stamp.manifest_fingerprint(
            validation_stamp.staged_manifest(root, policy))
    except Exception:  # noqa: BLE001 — the verb never fails (observe-only invariant)
        return None


def probe(root: Path | str, diff: str = "HEAD", scenario: str | None = None,
          max_mutants: int = MAX_MUTANTS, timeout: int = RUN_TIMEOUT) -> dict[str, Any]:
    root = Path(root)
    scenarios = [scenario] if scenario else linked_scenarios(root, diff)
    targets = changed_functions(root, diff)
    link = _linkage(root, diff, scenarios) if len(scenarios) > 1 else None
    rows: list[dict[str, Any]] = []
    if not scenarios:
        for rel, fn in targets[:max_mutants]:
            rows.append({"file": rel, "function": fn, "mutation": None,
                         "exitClass": "skipped", "detail": "no scenario linked to this diff"})
    else:
        lines_by_file = changed_python(root, diff)  # prefer the diff's own lines
        for rel, fn in targets:
            if len([r for r in rows if r["exitClass"] not in {"skipped"}]) >= max_mutants:
                break
            path = root / rel
            original = path.read_bytes()
            mutated = mutate_source(original.decode("utf-8", errors="ignore"), fn,
                                    frozenset(lines_by_file.get(rel, set())))
            if mutated is None:
                rows.append({"file": rel, "function": fn, "mutation": None,
                             "exitClass": "skipped", "detail": "no mutable site"})
                continue
            source, mutation, mut_line, mut_site = mutated
            scen = _scenario_for(root, rel, scenarios, link, diff, fn=fn)
            t0 = time.monotonic()
            try:
                path.write_text(source, encoding="utf-8")
                try:
                    proc = processes.run_process_tree_bounded(
                        [sys.executable, str(root / scen)], timeout=timeout, cwd=str(root))
                    exit_class = ("killed" if proc.returncode == 1
                                  else "survived" if proc.returncode == 0 else "error")
                except Exception as exc:
                    exit_class = "timeout" if "Timeout" in type(exc).__name__ else "error"
            finally:
                path.write_bytes(original)
            assert path.read_bytes() == original, f"restore failed for {rel}"
            rows.append({"file": rel, "function": fn, "mutation": mutation,
                         "line": mut_line, "site": mut_site,
                         "exitClass": exit_class, "scenario": scen,
                         "wallS": round(time.monotonic() - t0, 1)})
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["exitClass"]] = counts.get(row["exitClass"], 0) + 1
    survived = counts.get("survived", 0)
    # SPEC-173 Phase 0 rule 3: same defect sink as review-warn, `kind` discriminates.
    # Anchored to the `root` PARAMETER (landmine L4): _synthetic_probe() runs probe()
    # against a tmp repo whose weak oracle SURVIVES BY DESIGN — those rows must
    # evaporate with the tmp dir, not poison the real series on every canary run.
    sink = defect_sink(root)
    for r in rows:
        if r["exitClass"] == "survived":
            telemetry_sink.append_jsonl(sink, {"kind": "mutate-survivor", "diff": diff, **r})
    # ONE run-level row per `oracle mutate`, keyed on the staged fingerprint: the
    # evidence check_mutate (the commit-ledger's third leg) joins on. Survivor rows
    # alone could not answer "was this surface probed AT ALL?" -- absence of
    # survivors is indistinguishable from absence of a run.
    fp_str = staged_fingerprint(root)
    telemetry_sink.append_jsonl(sink, {
        "kind": "mutate-run", "diff": diff,
        "stagedFingerprint": fp_str,
        "counts": {c: counts.get(c, 0) for c in MUTATE_CLASSES},
        "scenario": ", ".join(sorted({r["scenario"] for r in rows if r.get("scenario")})) or None,
    })

    # ER-11: Mint MutationExecutionReceipt
    if fp_str:
        from harness_lib import governance_receipt
        rc = governance_receipt.GovernanceExecutionReceipt(
            execution_id=f"EXEC-MUT-{fp_str[:12]}",
            kind="mutation",
            run_id="oracle-mutate",
            fingerprint=fp_str,
            controller_identity="harness-governance-controller",
            started_at=governance_receipt.now(),
            ended_at=governance_receipt.now(),
            exit_code=0 if survived == 0 else 1,
            termination_reason="exited_cleanly",
            status="completed",
            parsed_result=counts
        )
        rc.write(governance_receipt.get_receipt_path(root, "mutation", fp_str))

    exercised = any(r["exitClass"] in {"killed", "survived", "error", "timeout"} for r in rows)
    return {"diff": diff, "rows": rows, "counts": counts,
            "verdict": (f"ORACLE-WEAK: {survived} mutant(s) SURVIVED the linked scenario — "
                        "its checks cannot see the changed behavior" if survived
                        else "oracle exercised" if exercised
                        else "nothing probed (skipped)" if rows else "nothing to probe")}


def _synthetic_probe() -> dict[str, Any]:
    """Machinery check in a throwaway repo: a strong oracle must KILL, a weak
    oracle must let the mutant SURVIVE. Both detected -> the probe's own recall
    is intact. This is the canary's fallback when the live diff has nothing to
    probe — a data point about the MACHINE, tagged as such, never about the diff."""
    import subprocess as sp
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        run = lambda *a: sp.run(["git", "-C", str(root), *a], capture_output=True,  # noqa: E731
                                text=True, encoding="utf-8", errors="replace", timeout=60)
        run("init", "-q")
        run("config", "user.email", "t@t")
        run("config", "user.name", "t")
        (root / "testing" / "scenarios").mkdir(parents=True)
        (root / "mod.py").write_text("def clamp(x):\n    return x\n", encoding="utf-8")
        run("add", "-A")
        run("commit", "-q", "-m", "base")
        (root / "mod.py").write_text(
            "def clamp(x):\n    if x < 0:\n        return 0\n    return x\n", encoding="utf-8")
        scen = root / "testing" / "scenarios" / "probe_check.py"
        strong = ("import sys\nsys.path.insert(0, r'%s')\nimport mod\n"
                  "raise SystemExit(0 if (mod.clamp(5) == 5 and mod.clamp(-3) == 0) else 1)\n")
        weak = "import sys\nsys.path.insert(0, r'%s')\nimport mod\nraise SystemExit(0)\n"
        scen.write_text(strong % str(root), encoding="utf-8")
        strong_counts = probe(root, diff="HEAD")["counts"]
        scen.write_text(weak % str(root), encoding="utf-8")
        weak_counts = probe(root, diff="HEAD")["counts"]
    return {"strongCounts": strong_counts, "weakCounts": weak_counts,
            "recallOk": bool(strong_counts.get("killed")) and bool(weak_counts.get("survived"))}


def _record_point(root: Path, point: dict[str, Any]) -> None:
    """One canary data point in the records ledger; ledger loss never breaks the probe."""
    import json

    try:
        from harness_lib import records
        records.add_entry(root, "note",
                          f"oracle canary: mode={point['mode']} " +
                          (f"recallOk={point['recallOk']}" if point["mode"] == "synthetic"
                           else f"counts={point.get('counts')}"),
                          body=json.dumps(point, sort_keys=True),
                          tags=["oracle", "canary"])
    except Exception:
        pass


def canary(root: Path | str, diff: str = "HEAD") -> dict[str, Any]:
    """W29 bet 3 — permanent adversarial canary, observe-only. Probes the
    live diff when it has teeth to test; otherwise runs the synthetic machinery
    check. Every run persists ONE point (tags oracle+canary), so gate recall
    becomes a series over time instead of a single glance."""
    root = Path(root)
    report = probe(root, diff=diff)
    exercised = any(r["exitClass"] in {"killed", "survived", "error", "timeout"}
                    for r in report["rows"])
    if exercised:
        point: dict[str, Any] = {"mode": "diff", "diff": diff,
                                 "counts": report["counts"], "verdict": report["verdict"]}
    else:
        point = {"mode": "synthetic", **_synthetic_probe()}
    _record_point(root, point)
    return point


def trend(root: Path | str) -> dict[str, Any]:
    """Read-only series of past canary points (hot worklog + archive)."""
    import json

    from harness_lib import records as records_mod
    from harness_lib.common import read_json

    root = Path(root)
    points: list[dict[str, Any]] = []
    for rel in (records_mod.WORKLOG_REL, records_mod.ARCHIVE_REL):
        for entry in (read_json(root / rel, {}) or {}).get("entries") or []:
            if "canary" not in (entry.get("tags") or []):
                continue
            try:
                body = json.loads(entry.get("body") or "{}")
            except Exception:
                body = {}
            points.append({"at": entry.get("at"), **body})
    points.sort(key=lambda p: str(p.get("at") or ""))
    survived = sum((p.get("counts") or {}).get("survived", 0)
                   for p in points if p.get("mode") == "diff")
    recall_failures = sum(1 for p in points
                          if p.get("mode") == "synthetic" and not p.get("recallOk"))
    return {"points": len(points), "survivedTotal": survived,
            "recallFailures": recall_failures,
            "lastAt": points[-1]["at"] if points else None,
            "series": points[-20:]}


def waive(root: Path | str, reason: str) -> dict[str, Any]:
    """Record the overseer's JUDGED tolerance for the survivors on the CURRENT
    staged surface (the two standing tolerated classes: `_demo`/test-code-assert
    survivors, and `(fallback)` pre-existing-code survivors). check_mutate turns a
    survivors-block into a WARN carrying this reason. An empty reason is refused —
    a waiver without a stated judgement is just a silent bypass."""
    root = Path(root)
    reason = (reason or "").strip()
    from harness_lib import common
    if not reason:
        raise common.HarnessError(
            "oracle waive: --reason must be non-empty (the judged tolerance is the "
            'whole record). fix: python scripts/harness.py oracle waive --reason "…"')
    fp = staged_fingerprint(root)
    row = {"kind": "mutate-waiver", "stagedFingerprint": fp, "reason": reason}
    telemetry_sink.append_jsonl(defect_sink(root), row)
    return row


def cmd_oracle(args) -> int:
    from harness_lib import common

    if getattr(args, "action", "mutate") == "waive":
        common.emit(waive(common.ROOT, getattr(args, "reason", None) or ""))
        return 0
    if getattr(args, "action", "mutate") == "canary":
        out = trend(common.ROOT) if getattr(args, "trend", False) else canary(
            common.ROOT, diff=getattr(args, "diff", "HEAD") or "HEAD")
        common.emit(out)
        return 0
    report = probe(common.ROOT, diff=getattr(args, "diff", "HEAD") or "HEAD",
                   scenario=getattr(args, "scenario", None),
                   max_mutants=int(getattr(args, "max_mutants", MAX_MUTANTS) or MAX_MUTANTS),
                   timeout=int(getattr(args, "timeout", RUN_TIMEOUT) or RUN_TIMEOUT))
    if getattr(args, "record", False):  # W29 N4: the probe finally persists
        _record_point(common.ROOT, {"mode": "mutate", "diff": report["diff"],
                                    "counts": report["counts"], "verdict": report["verdict"]})
    if getattr(args, "json", False) or common.agent_output_compact():
        common.emit(report)
        return 0
    for row in report["rows"]:
        extra = row.get("detail") or (
            f"{row.get('mutation')}@L{row.get('line')}({row.get('site')}) "
            f"vs {row.get('scenario')} ({row.get('wallS')}s)")
        print(f"{row['exitClass']:<9} {row['file']}::{row['function']} — {extra}")
    print(f"\n{report['verdict']}")
    return 0


def _demo() -> None:
    import subprocess as sp
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        run = lambda *a: sp.run(["git", "-C", str(root), *a], capture_output=True,  # noqa: E731
                                text=True, encoding="utf-8", errors="replace", timeout=60)
        run("init", "-q")
        run("config", "user.email", "t@t")
        run("config", "user.name", "t")
        (root / "testing" / "scenarios").mkdir(parents=True)
        (root / "mod.py").write_text("def clamp(x):\n    return x\n", encoding="utf-8")
        strong = ("import sys\nsys.path.insert(0, r'%s')\nimport mod\n"
                  "raise SystemExit(0 if (mod.clamp(5) == 5 and mod.clamp(-3) == 0) else 1)\n")
        weak = ("import sys\nsys.path.insert(0, r'%s')\nimport mod\n"
                "raise SystemExit(0)\n")  # asserts nothing: the weak oracle
        run("add", "-A")
        run("commit", "-q", "-m", "base")

        # the "change": clamp gains real logic; scenario added in the same diff
        (root / "mod.py").write_text(
            "def clamp(x):\n    if x < 0:\n        return 0\n    return x\n", encoding="utf-8")
        scen = root / "testing" / "scenarios" / "probe_check.py"
        scen.write_text(strong % str(root), encoding="utf-8")
        original = (root / "mod.py").read_bytes()

        report = probe(root, diff="HEAD")
        assert report["counts"].get("killed", 0) >= 1, report
        assert (root / "mod.py").read_bytes() == original  # honest restore
        # the whole file is the diff here, so the site must read in-diff and
        # the report must NAME the mutated line (the one-site-per-change row)
        probed = [r for r in report["rows"] if r["exitClass"] == "killed"][0]
        assert probed["site"] == "in-diff" and isinstance(probed["line"], int), probed

        scen.write_text(weak % str(root), encoding="utf-8")
        report2 = probe(root, diff="HEAD")
        assert report2["counts"].get("survived", 0) >= 1 and "ORACLE-WEAK" in report2["verdict"]
        assert (root / "mod.py").read_bytes() == original

        (root / "testing" / "scenarios" / "probe_check.py").unlink()
        report3 = probe(root, diff="HEAD")
        assert all(r["exitClass"] == "skipped" for r in report3["rows"]) or report3["rows"] == []

    with tempfile.TemporaryDirectory() as tmp2:  # canary: non-repo -> synthetic point
        croot = Path(tmp2)
        (croot / ".harness" / "state").mkdir(parents=True)
        (croot / ".harness" / "context").mkdir(parents=True)
        point = canary(croot)
        assert point["mode"] == "synthetic" and point["recallOk"], point
        series = trend(croot)
        assert series["points"] == 1 and series["recallFailures"] == 0, series

    # mutate_source prefer-lines contract (row oracle-mutate-probes-one-site-per-
    # change): the diff's line is probed FIRST; an empty/missing preference falls
    # back to first-site-anywhere and says so via site="fallback".
    two = "def f(x):\n    a = x == 1\n    b = x == 2\n    return a or b\n"
    m = mutate_source(two, "f", frozenset({3}))
    assert m and m[2] == 3 and m[3] == "in-diff", m          # the diff's own line wins
    m = mutate_source(two, "f")
    assert m and m[2] == 2 and m[3] == "fallback", m         # bare call = old behavior, labeled
    m = mutate_source(two, "f", frozenset({99}))
    assert m and m[2] == 2 and m[3] == "fallback", m         # no site on the hunk -> honest fallback
    assert mutate_source("def g():\n    pass\n", "g") is None

    # _scenario_for ordered-preference (2026-07-22 wrong-pairing fixtures). Pure
    # picker: a hand-built ctx exercises every rung without a git tree or graph.
    root = Path(".")
    xx = "testing/scenarios/xx_new_scenario.py"
    big = "testing/scenarios/big_unrelated.py"
    scen_ab = [big, xx]  # linked_scenarios is sorted -> big before xx
    ctx = {"new": {big, xx},
           "sources": {xx: "import foo_lib\nfoo_lib.run()\n", big: "unrelated\n"},
           # big's closure ALSO contains foo_lib.py + is bigger: (a) must ignore it
           "closures": {"xx_new_scenario": {"foo_lib.py"},
                        "big_unrelated": {"foo_lib.py", "a.py", "b.py", "c.py"}}}
    assert _scenario_for(root, "foo_lib.py", scen_ab, ctx) == xx  # (a) new + mention

    # no mention/token anywhere -> falls back to the closure heuristic (rung c)
    aaa, bbb = "testing/scenarios/aaa.py", "testing/scenarios/bbb.py"
    ctx_c = {"new": set(),  # both modified, neither new
             "sources": {aaa: "nothing\n", bbb: "nothing\n"},
             "closures": {"aaa": {"other.py"}, "bbb": {"foo_lib.py", "x.py"}}}
    assert _scenario_for(root, "foo_lib.py", [aaa, bbb], ctx_c) == bbb  # (c) closure

    # today's live miss: ui_queue mutant vs a big MODIFIED m5 whose closure also
    # holds it: the NEW qf that names/mentions ui_queue must win (rung a).
    ck = "testing/scenarios/ck_config_keys_gui.py"
    m5 = "testing/scenarios/m5_ui_panel.py"
    qf = "testing/scenarios/qf_queue_family.py"
    ctx_q = {"new": {ck, qf},  # m5 is modified-in-diff, not new
             "sources": {ck: "config\n", m5: "ui_panel\n",
                         qf: "from harness_lib import ui_queue\n"},
             "closures": {"m5_ui_panel": {"scripts/harness_lib/ui_queue.py", "p1", "p2", "p3"},
                          "qf_queue_family": {"scripts/harness_lib/ui_queue.py", "p1"}}}
    assert _scenario_for(root, "scripts/harness_lib/ui_queue.py", [ck, m5, qf], ctx_q) == qf

    # a bare single-word stem ('harness') is in every scenario's source: it must
    # NOT over-grab a NEW scenario by substring; with no closure hit -> fallback.
    ctx_h = {"new": {ck, qf},
             "sources": {ck: "import harness\n", m5: "import harness\n", qf: "import harness\n"},
             "closures": {}}
    assert _scenario_for(root, "scripts/harness.py", [ck, m5, qf], ctx_h) == ck  # sorted[0]

    # (a0) the mutated FUNCTION's name in a scenario source outranks closure
    # size: the dacbdc8 L1424 miss — doc has the smaller closure, pe names the
    # function. Without fn the pick is unchanged (closure wins).
    doc = "testing/scenarios/doc_delegation_output_cap.py"
    pe = "testing/scenarios/playbook_enforcement.py"
    ctx_f = {"new": set(),
             "sources": {doc: "import harness\n",
                         pe: "harness.workflow_spawn_command_for_prompt('x.md', 'scan', None)\n"},
             "closures": {"doc_delegation_output_cap": {"scripts/harness.py"},
                          "playbook_enforcement": {"scripts/harness.py", "a.py", "b.py"}}}
    assert _scenario_for(root, "scripts/harness.py", [doc, pe], ctx_f,
                         fn="workflow_spawn_command_for_prompt") == pe
    assert _scenario_for(root, "scripts/harness.py", [doc, pe], ctx_f) == doc
    assert _scenario_for(root, "scripts/harness.py", [doc, pe], ctx_f, fn="main") == doc  # single-word fn: no grab

    # single linked scenario / scenario-less paths unchanged
    assert _scenario_for(root, "x.py", ["testing/scenarios/only.py"]) == "testing/scenarios/only.py"
    print("mutation_probe self-check: ok")


if __name__ == "__main__":
    _demo()
