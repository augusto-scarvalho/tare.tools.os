"""Candidate-bound scenarios-gate orchestration, extracted from `spec_test_gate`
(gs-7 burndown ratchet: gate logic migrates OUT of `spec_test_gate.py` into
`harness_lib`). This holds the v6 candidate-subject PLANNING and the real-root
execution TARGET resolution; `spec_test_gate` keeps the pinned gate skeleton
(single-freeze -> serial loop, ShadowLedger wiring, per-scenario snapshot/restore)
and injects the fail-closed `result` emitter by handling the returned plan.

Dependency direction is ONE-WAY: this module imports the harness_lib gate
primitives; it NEVER imports `spec_test_gate`. The gate passes ROOT/pin/paths in
and gets plain data back (a `ScenarioSubjectPlan`, or an execution target), so no
candidate-subject / selection / execution logic is duplicated. Not a new canonical
primitive — an internal orchestration seam behind the same v6 behaviour."""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

if __name__ == "__main__":  # bare `python scripts/harness_lib/gate_scenarios.py`
    sys.dont_write_bytecode = True  # keep the developer source tree pristine (no __pycache__)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/ -> `from harness_lib import ...`
from harness_lib import gate_affected, gate_parallel, gate_scenario_order, validation_stamp

_NOT_PROVEN = "CANDIDATE_SCENARIO_SUBJECT_NOT_PROVEN: "


@dataclass
class ScenarioSubjectPlan:
    """The frozen scenarios subject S=(H,C,P) resolved into an executable plan.
    `ok=False` carries `fail_detail` for the gate's single fail-closed row."""
    ok: bool
    fail_detail: str | None
    candidate_bound: bool
    scenario_pin: str | None
    base_head: str | None
    candidate_tree: str | None
    scenario_run_order: list[Path]
    frozen_gate_version: str | None
    skip_enabled: bool


def plan_scenario_subject(root: Path, project_skip_enabled: bool) -> ScenarioSubjectPlan:
    """Freeze S=(H,C,P) ONCE and derive the whole plan from it, so enumeration,
    selection and execution can never disagree with P (v2's classify-vs-freeze race
    is gone). CANDIDATE-BOUND (index tree != HEAD tree): enumeration = P's projection
    (`candidate_scenario_paths`), the skip-cache version = `validator_version` OF P
    (frozen; a live validator-input mutation cannot move it), and an unprovable frozen
    version DISABLES skipping (run all -- never a live/mismatched key, so candidate
    coverage is never removed on the version-error path). NON-candidate (index == HEAD):
    the working tree IS the subject -> incumbent glob (untracked scenarios INCLUDED).
    Any subject-proof gap (unprovable/unstable identity, or a failed P projection)
    returns `ok=False` -> the gate fails closed; a failed proof is never read as
    'no candidate'."""
    subject = gate_parallel.freeze_subject(root)
    if not subject.ok:
        return ScenarioSubjectPlan(False, "candidate identity could not be established "
                                   "(repository state unprovable / unstable); refusing (fail-closed)",
                                   False, None, None, None, [], None, False)
    candidate_bound = subject.candidate_bound
    if candidate_bound:
        candidate_paths = gate_parallel.candidate_scenario_paths(root, subject.pin)
        if candidate_paths is None:
            return ScenarioSubjectPlan(False, "candidate scenario projection from P failed; "
                                       "refusing to validate the working tree (fail-closed)",
                                       True, subject.pin, subject.base_head, subject.candidate_tree,
                                       [], None, False)
        order = gate_scenario_order.fail_fast_order(root, paths=candidate_paths)
    else:
        order = gate_scenario_order.fail_fast_order(root)  # incumbent glob (parity)
    frozen_gate_version = (validation_stamp.validator_version_from_tree(root, subject.candidate_tree)
                           if candidate_bound else None)
    skip_enabled = project_skip_enabled and not (candidate_bound and frozen_gate_version is None)
    return ScenarioSubjectPlan(True, None, candidate_bound, subject.pin, subject.base_head,
                               subject.candidate_tree, order, frozen_gate_version, skip_enabled)


def resolve_run_target(copy: Path | None, pin: str | None, scenario_path: Path,
                       root: Path) -> "tuple[Path, Path] | subprocess.CompletedProcess[str]":
    """Resolve where a real-root lane executes. Candidate-bound (a shared P copy is
    set): RESTORE the copy to pristine P and attest BEFORE the run -- so a prior
    scenario's mutation of tracked candidate source (or a leaked ignored artifact)
    cannot poison this scenario's closure -- then execute FROM the copy, whose entire
    tree is P (entrypoint AND every imported helper). A scenario absent from P, or a
    copy whose integrity cannot be restored, fails CLOSED (a 1-rc CompletedProcess),
    never the developer tree. Non-candidate (copy is None): the real ROOT, byte-identical
    incumbent. Returns (run_root, run_path) OR a fail-closed CompletedProcess."""
    if copy is None:
        return root, scenario_path
    run_path = copy / "testing" / "scenarios" / scenario_path.name
    try:
        gate_parallel.restore_worker_copy(copy, pin)  # pristine P + attest
    except Exception as exc:  # noqa: BLE001 -- integrity unrecoverable -> fail closed
        print(f"✗ scenarios:{scenario_path.stem}: {_NOT_PROVEN.rstrip(': ')} "
              f"(P-copy integrity lost: {type(exc).__name__}: {exc})", flush=True)
        return subprocess.CompletedProcess([sys.executable, str(scenario_path)], 1,
                                           stdout=f"{_NOT_PROVEN}P-copy integrity lost: {exc}\n", stderr="")
    if not run_path.exists():
        print(f"✗ scenarios:{scenario_path.stem}: {_NOT_PROVEN.rstrip(': ')} "
              f"(absent from candidate P; real-root execution refused)", flush=True)
        return subprocess.CompletedProcess([sys.executable, str(scenario_path)], 1,
                                           stdout=f"{_NOT_PROVEN}scenario absent from candidate P\n", stderr="")
    return copy, run_path


def open_subject_copy(root: Path, plan: ScenarioSubjectPlan) -> "tuple[Path | None, str | None]":
    """Candidate-bound real-root binding: materialize the frozen P ONCE as a shared
    detached worktree so every real-root lane executes P's bytes (entrypoint AND
    dependency closure). Returns (copy, None) on success, or (None, fail_detail) if P
    cannot be materialized -> the gate fails closed. Non-candidate: (None, None)."""
    if not plan.candidate_bound:
        return None, None
    try:
        return gate_parallel.make_worker_copy(root, plan.scenario_pin), None
    except Exception as exc:  # noqa: BLE001
        return None, (f"could not materialize P for real-root lanes: {type(exc).__name__}: {exc}")


def dispose_subject_copy(copy: Path | None) -> None:
    """Best-effort disposal of the shared P copy (never fails the gate)."""
    if copy is not None:
        try:
            gate_parallel.dispose_worker_copy(copy)
        except Exception:  # noqa: BLE001 -- disposal is best-effort
            pass


def _selfcheck() -> None:
    """FULLY HERMETIC (v6.3). The developer repo is SOURCE ONLY: no worktree/index/refs/
    object-db/filesystem writes against it. EVERY git-writing seam -- freeze's write-tree/
    commit-tree (planning) AND the destructive restore (reset --hard + clean -fdxq) -- runs
    against a DISPOSABLE scratch repo, deleted afterward. v6.2 called plan_scenario_subject(
    root), whose freeze wrote dangling tree/commit objects into the DEV repo's .git/objects
    (and the bare run left __pycache__); v6.3 confines all of that to scratch (+ __main__ sets
    sys.dont_write_bytecode). Only `resolve_run_target(None, ...)` (which runs NO git) touches
    the dev root, and only for the path constant."""
    import shutil
    import tempfile
    root = Path(__file__).resolve().parents[2]
    # (1) non-candidate target = the real ROOT (path only; resolve_run_target(None,...) runs NO git).
    assert resolve_run_target(None, None, root / "x.py", root) == (root, root / "x.py")
    scratch = Path(tempfile.mkdtemp(prefix="gsc-selfcheck-"))
    try:
        def _g(*a: str) -> None:
            subprocess.run(["git", "-C", str(scratch), *a], check=True, capture_output=True, text=True)
        _g("init", "-q"); _g("config", "user.email", "s@s"); _g("config", "user.name", "s")
        sc = scratch / "testing" / "scenarios"; sc.mkdir(parents=True)
        present = sc / "present.py"; present.write_text("x = 1\n", encoding="utf-8")
        _g("add", "-A"); _g("commit", "-q", "-m", "base")
        # (2A) scratch CLEAN (index == HEAD) -> planning NON-candidate.
        pclean = plan_scenario_subject(scratch, True)
        assert pclean.ok and pclean.candidate_bound is False
        # (2B) scratch STAGED candidate (index != HEAD) -> candidate-bound plan, P generated,
        # projection includes the added scenario. All write-tree/commit-tree land in SCRATCH.
        (sc / "added.py").write_text("y = 2\n", encoding="utf-8"); _g("add", "-A")
        pcand = plan_scenario_subject(scratch, True)
        assert pcand.ok and pcand.candidate_bound is True and pcand.scenario_pin
        assert any(p.name == "added.py" for p in pcand.scenario_run_order)
        # (3) the candidate-copy restore seam stays DESTRUCTIVE to the scratch copy (production
        # property preserved), never the dev root: absent -> fail-closed; present -> (copy, run_path).
        miss = resolve_run_target(scratch, None, Path("absent.py"), scratch)
        assert isinstance(miss, subprocess.CompletedProcess) and miss.returncode == 1
        assert resolve_run_target(scratch, None, present, scratch) == (scratch, present)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    _selfcheck()
    print("gate_scenarios self-check OK")
