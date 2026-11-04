#!/usr/bin/env python3
"""SPEC-160 wf-parallel-scenarios — sharded parallel scenarios (the DEFAULT).

The serial scenarios gate loops `_run_isolated_scenario` (per-scenario
snapshot/restore on the SHARED ROOT/.harness) — the ~35% isolation tax AND the
serialization point (gate-perf, 10 runs). This module runs N workers, each a
SERIAL SHARD of the scenarios against its OWN disposable repo copy, results
merged. DEFAULT since the 2026-07-21 owner flip (D041): EXP-30 proved verdict
equivalence over the real battery (pairs 3-5: zero non-quarantined systematic
divergences; ~3.5-4x wall win). `--serial` forces the old loop, which also
remains the automatic fallback on any parallel-infra error.

Design (correctness-first):
  - **Race-free by construction.** Each shard worker runs inside its own
    disposable HEAD-faithful repo copy: a DETACHED `git worktree` pinned to the
    run's HEAD sha (a real `.git`, real HEAD-tracked content). Detached is
    non-negotiable safety — a commit inside the copy is a dangling object that
    can NEVER move the real repo's `refs/heads/*` (a hard-kill mid-scenario
    cannot move the owner's branch). Workers NEVER touch the real ROOT/.harness,
    so concurrent shards cannot race on shared state.
  - **Within a worker**, scenarios run serially, each wrapped by the EXISTING
    `scenario_isolation.snapshot_volatile_state`/`restore_volatile_state` on the
    COPY's root (order-independence, now on private state).
  - **Prove equivalence.** The orchestration (shard -> run -> merge) is pure and
    injectable: `verdicts_serial(scenarios, run_one)` and
    `verdicts_parallel(scenarios, n, run_one)` MUST return an identical
    per-scenario verdict map. `equivalent(...)` asserts it; the gps teeth prove
    it on a fixture set, and this is the guard the default-flip follow-up runs
    over the real battery.
  - **Fail-safe.** ANY parallel-infra error (copy fails, worker raises, merge
    inconsistent/incomplete) -> `run_parallel_scenarios` returns None. The
    dispatch then retries parallel ONCE (2026-08-03: observed aborts were
    transient); a SECOND abort falls back to the serial loop, except under the
    time-bounded gate (`HARNESS_GATE_TIME_BOUNDED=1`, the 1800s `validate
    --staged` cap) where serial provably cannot finish, so the gate FAILS FAST
    with a pointer instead of burning 30min to certify nothing. Abort reasons
    are appended to `.harness/runs/gate-parallel-aborts.jsonl` AT ABORT TIME —
    the settle-time gate_perf flush dies with a hard-kill. Parallel machinery
    must never fail the gate on its own.

Residual (declared, post-flip): worktree copies carry a real `.git` and the
HEAD-tracked tree (fidelity gap CLOSED). Declared quarantine: m4's noDrift
clause only (raw-byte hash vs fresh-checkout EOLs, owner decision (a)/D040).
The predicted branch-NAME residue never materialized in EXP-30 pairs 2-5. Known
transient: a scenario's own dirty-surface guard may self-skip under shard load
(rt6, pair 5) — fail-safe by protocol, tracked by the copy-env-flake-hunter hunt.

Serial parity (2026-07-20 audit): the worker path carries the serial loop's
per-scenario duration, retry-once + recovered-on-retry reporting, failure
forensics (stdout+rc into the real root's sidecar) and the SCENARIO-SKIP
protocol. The SPEC-159 shadow never runs INSIDE a worker: since the 2026-07-21
flip (D041, EXP-30 equivalence proven) the GATE annotates the shadow from the
merged verdicts on the real root — skips still never feed the verdict cache.

Self-check: python scripts/harness_lib/gate_parallel.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_MAX_WORKERS = 8  # ponytail: fixed oversubscription ceiling; raise if a host has more cores than scenarios benefit from

# SPEC-160 v4 invariant 11 — the LATENCY CLASS. Shards are sized for THROUGHPUT
# (N workers on N cores); a scenario driving a real browser needs event-loop
# RESPONSIVENESS, and loses that fight deterministically: 8 workers on 8 cores
# (measured `os.cpu_count() == 8`) plus a desktop, against `wait_for_selector`
# deadlines. Members leave the pool and run serially on the real root after it
# drains. ADMISSION RULE, both parts required, evidence not vibe:
#   (a) it drives a real browser (the mechanism: scheduling latency, not CPU), AND
#   (b) forensics show contention-shaped fail-through-retry that passes standalone.
# `worker_live_tail` is HEAVIER (126.9s vs 124.3s) and stays OUT: not a browser
# scenario, no fail-through-retry on record. Rollback for the whole feature is
# `()` — the partition degenerates to the pre-v4 flow byte-equivalently.
_SERIAL_LANE: tuple[str, ...] = ("ui_e2e",)


class ParallelInfraError(RuntimeError):
    """Any parallel-machinery failure. Always caught -> serial fallback."""


def _stem(scenario: Any) -> str:
    return scenario.stem if isinstance(scenario, Path) else str(scenario)


def resolve_workers(n: int | None, total: int) -> int:
    """Bounded worker count. n falsy/<=0 -> cores (>=2); capped at total and the
    oversubscription ceiling. Independent of the unrelated workflow max_workers."""
    if not n or n <= 0:
        n = max(2, os.cpu_count() or 2)
    return max(1, min(n, _MAX_WORKERS, total or 1))


def lpt_shard(
    items: list[Any],
    n: int,
    dur_s: dict[str, float] | None = None,
    default_dur: float = 5.0,
) -> list[list[Any]]:
    """Deterministic Longest Processing Time (LPT) balanced scenario scheduler.
    
    Sorts scenarios by estimated execution duration descending, then places each
    scenario into the shard with the smallest cumulative estimated duration.
    Tie-breaking is deterministic (alphabetical stem ordering).
    """
    n = max(1, min(n, _MAX_WORKERS, len(items) or 1))
    dur_map = dur_s or {}
    
    def _est(item: Any) -> float:
        st = _stem(item)
        val = dur_map.get(st)
        return float(val) if isinstance(val, (int, float)) and val > 0 else default_dur

    sorted_items = sorted(items, key=lambda it: (-_est(it), _stem(it)))

    buckets: list[list[Any]] = [[] for _ in range(n)]
    bucket_times: list[float] = [0.0] * n

    for item in sorted_items:
        min_idx = min(range(n), key=lambda i: (bucket_times[i], i))
        buckets[min_idx].append(item)
        bucket_times[min_idx] += _est(item)

    return [b for b in buckets if b]


def calculate_scheduler_metrics(
    shards: list[list[Any]],
    dur_s: dict[str, float] | None = None,
    actual_durations: dict[str, float] | None = None,
    default_dur: float = 5.0,
) -> dict[str, Any]:
    """Calculate makespan, prediction error, and shard balance ratio across shards."""
    dur_map = dur_s or {}
    
    predicted_shard_durations: list[float] = []
    for s_list in shards:
        tot = sum(dur_map.get(_stem(it), default_dur) for it in s_list)
        predicted_shard_durations.append(tot)
        
    pred_makespan = max(predicted_shard_durations) if predicted_shard_durations else 0.0
    min_pred = min(predicted_shard_durations) if predicted_shard_durations else 0.0
    pred_balance = (min_pred / pred_makespan) if pred_makespan > 0 else 1.0

    result: dict[str, Any] = {
        "shard_count": len(shards),
        "predicted_makespan": round(pred_makespan, 2),
        "predicted_shard_balance": round(pred_balance, 4),
        "predicted_shard_durations": [round(d, 2) for d in predicted_shard_durations],
    }

    if actual_durations:
        actual_shard_durations: list[float] = []
        for s_list in shards:
            tot = sum(actual_durations.get(_stem(it), default_dur) for it in s_list)
            actual_shard_durations.append(tot)
            
        act_makespan = max(actual_shard_durations) if actual_shard_durations else 0.0
        min_act = min(actual_shard_durations) if actual_shard_durations else 0.0
        act_balance = (min_act / act_makespan) if act_makespan > 0 else 1.0
        
        errors = [abs(p - a) for p, a in zip(predicted_shard_durations, actual_shard_durations)]
        mean_err = (sum(errors) / len(errors)) if errors else 0.0

        result.update({
            "actual_makespan": round(act_makespan, 2),
            "actual_shard_balance": round(act_balance, 4),
            "prediction_error_mae": round(mean_err, 2),
        })

    return result


def shard(items: list[Any], n: int, root: Path | str | None = None, lpt: bool | None = None) -> list[list[Any]]:
    """Split scenarios into <=n non-empty shards.
    
    If lpt=True (or HARNESS_LPT_SCHEDULER=1 env set), uses Longest Processing Time (LPT)
    balancing using historical durations; otherwise defaults to round-robin split.
    """
    use_lpt = lpt if lpt is not None else (os.environ.get("HARNESS_LPT_SCHEDULER") == "1")
    if use_lpt:
        try:
            from harness_lib import gate_scenario_order
            r_path = Path(root) if root else Path.cwd()
            dur_s = gate_scenario_order.known_durations(r_path)
            return lpt_shard(items, n, dur_s=dur_s)
        except Exception:
            pass
    buckets = [items[i::n] for i in range(max(1, n))]
    return [b for b in buckets if b]



def merge_verdicts(maps: list[dict[str, Any]]) -> dict[str, Any]:
    """Union of per-shard verdict maps. A scenario appearing in two shards with
    conflicting verdicts is an inconsistency -> raise (caller falls back)."""
    out: dict[str, bool] = {}
    for m in maps:
        for k, v in m.items():
            if k in out and out[k] != v:
                raise ParallelInfraError(f"inconsistent verdict for {k!r}: {out[k]} vs {v}")
            out[k] = v
    return out


def verdicts_serial(scenarios: list[Any], run_one: Callable[[Any], bool]) -> dict[str, bool]:
    """Per-scenario verdict map, run serially. `run_one(scenario) -> ok`."""
    return {_stem(s): bool(run_one(s)) for s in scenarios}


def verdicts_parallel(scenarios: list[Any], n: int | None, run_one: Callable[[Any], bool],
                      run_shard: Callable[[list[Any]], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Sharded verdict map. Shards run concurrently; results merged. Raises
    ParallelInfraError on an inconsistent or incomplete merge (missing/extra
    scenarios) so the caller can fall back to serial."""
    workers = resolve_workers(n, len(scenarios))
    shards = shard(scenarios, workers)
    run_shard = run_shard or (lambda sc: verdicts_serial(sc, run_one))
    with ThreadPoolExecutor(max_workers=len(shards) or 1) as ex:
        maps = list(ex.map(run_shard, shards))
    merged = merge_verdicts(maps)
    expected = {_stem(s) for s in scenarios}
    if set(merged) != expected or len(merged) != len(expected):
        raise ParallelInfraError(f"incomplete merge: {len(merged)} verdicts for {len(expected)} scenarios")
    return merged


def run_with_fallback(scenarios: list[Any], n: int | None, run_one: Callable[[Any], bool],
                      run_shard: Callable[[list[Any]], dict[str, Any]] | None = None) -> tuple[dict[str, Any], str]:
    """(verdicts, mode). Try parallel; ANY error -> serial verdicts, mode
    'serial-fallback'. The gate always completes with a full verdict map."""
    try:
        return verdicts_parallel(scenarios, n, run_one, run_shard), "parallel"
    except Exception:  # noqa: BLE001 -- parallel machinery must never fail the gate
        return verdicts_serial(scenarios, run_one), "serial-fallback"


def equivalent(scenarios: list[Any], n: int | None, run_one: Callable[[Any], bool]) -> bool:
    """The acceptance property: sharded-parallel verdicts == serial verdicts."""
    return verdicts_serial(scenarios, run_one) == verdicts_parallel(scenarios, n, run_one)


# --- real gate integration (per-worker detached-worktree repo copies) --------

# serial's in-gate hold MATERIALIZES these live dirs before a scenario runs; the
# worktree checkout has their TRACKED content but not the untracked live state —
# creating them empty matches serial's hold-materialized baseline (the fidelity
# target). Empty dirs stay invisible to `git status --porcelain`, so attestation
# still sees a clean tree.
_VOLATILE_DIRS = (
    ".harness/state", ".harness/context", ".harness/runtime", ".harness/routing",
    ".harness/workflows/active", ".harness/runs",
)


def _run_git(args: list[str], *, cwd: Path, timeout: int, check: bool = True) -> Any:
    """`git <args>` via the bounded quiet spawn (hidden console on nt, always a
    timeout). Captures text output for the callers that read it."""
    from harness_lib import processes
    return processes.run_quiet(["git", *args], cwd=str(cwd), check=check, timeout=timeout,
                               capture_output=True, text=True)


def _head_sha(root: Path) -> str:
    """The full HEAD commit sha. Pin it ONCE per parallel run (before sharding) so
    every shard's detached worktree checks out the SAME commit even if HEAD moves
    mid-run."""
    return _run_git(["rev-parse", "HEAD"], cwd=root, timeout=60).stdout.strip()


def _pin_sha(root: Path) -> str:
    """The sha worker copies check out: HEAD — or, when the INDEX differs from
    HEAD (a `validate --staged` run; option-3 enforces staged==worktree there), a
    DANGLING commit of the index tree (`write-tree` + `commit-tree`) so shards
    exercise the exact STAGED code serial validates in place. Without this the
    copies would run the PREVIOUS commit's battery and stamp a fingerprint whose
    code never executed — a false gate (found 2026-07-21, D041 flip review). The
    dangling commit moves no ref (ref-safety intact; git gc sweeps it); any git
    failure here raises into run_parallel_scenarios' net -> serial fallback."""
    head = _head_sha(root)
    if _run_git(["diff", "--cached", "--quiet"], cwd=root, timeout=60, check=False).returncode == 0:
        return head
    tree = _run_git(["write-tree"], cwd=root, timeout=60).stdout.strip()
    return _run_git(["commit-tree", tree, "-p", head, "-m", "gate-parallel staged pin (dangling)"],
                    cwd=root, timeout=60).stdout.strip()


@dataclass(frozen=True)
class FrozenSubject:
    """ONE frozen observation of the validation subject: base HEAD `H`, its tree
    `base_head_tree`, the candidate tree (index `write-tree`), the execution/
    enumeration/selection identity `pin` (P), and `candidate_bound`. Everything is
    derived from the SINGLE captured `base_head` plus ONE index read — after `H` is
    captured, mutable symbolic `HEAD` is never consulted again, so a later HEAD move
    cannot change candidate-bound, the selection baseline, or the execution subject.

    Three outcomes, distinguished so an UNKNOWN identity never reads as non-candidate:
    - `ok=True,  in_repo=False`: PROVEN not a git work tree — no candidate concept;
      the caller keeps incumbent working-tree behavior.
    - `ok=True,  in_repo=True` : a work tree whose subject was fully resolved.
    - `ok=False`               : identity could NOT be established (git unavailable,
      or a work tree whose HEAD/tree/write-tree failed) — the caller FAILS CLOSED;
      a failed proof is NOT proof that no candidate exists.

    `candidate_bound = (candidate_tree != base_head_tree)`, both from the same frozen
    observation — never a separate `diff --cached` or a re-resolved `HEAD`."""
    ok: bool
    in_repo: bool
    base_head: str | None
    base_head_tree: str | None
    pin: str | None
    candidate_tree: str | None
    candidate_bound: bool


_FREEZE_RETRY_LIMIT = 3  # bounded, deterministic re-acquisition when HEAD moves mid-freeze


def freeze_subject(root: Path) -> FrozenSubject:
    """Acquire a STABLE frozen subject against a single captured base HEAD. `pin` is
    `base_head` when the index tree equals `base_head`'s tree, else a DANGLING commit
    of the index tree parented on `base_head` (ref-safe; git gc sweeps it).

    Repository identity is established POSITIVELY: only a git that answers
    `--is-inside-work-tree == true` proceeds. A failed/erroring probe is an UNKNOWN
    identity — NOT proof of non-repo — so it fails closed (`ok=False`). No current
    caller runs outside a work tree; there is no infer-non-repo-from-nonzero path.

    Stable acquisition: HEAD is re-read AFTER `write-tree`; if it moved during the
    window the pair `(base_head, candidate_tree)` may never have co-existed, so the
    attempt is discarded and retried (bounded). `commit-tree` runs only AFTER
    stability. Never-stable within the bound -> fail closed."""
    try:
        probe = _run_git(["rev-parse", "--is-inside-work-tree"], cwd=root, timeout=60, check=False)
    except Exception:  # noqa: BLE001 -- cannot invoke git: identity UNKNOWN -> fail closed
        return FrozenSubject(False, False, None, None, None, None, False)
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        # A nonzero/unexpected answer is a FAILED query, not positive proof of
        # non-repo. Canonical Validation must not treat UNKNOWN identity as
        # non-candidate: fail closed.
        return FrozenSubject(False, probe.returncode == 0, None, None, None, None, False)
    for _ in range(_FREEZE_RETRY_LIMIT):
        try:
            h_before = _run_git(["rev-parse", "HEAD"], cwd=root, timeout=60).stdout.strip()
            base_tree = _run_git(["rev-parse", f"{h_before}^{{tree}}"], cwd=root, timeout=60).stdout.strip()
            cand_tree = _run_git(["write-tree"], cwd=root, timeout=60).stdout.strip()
            h_after = _run_git(["rev-parse", "HEAD"], cwd=root, timeout=60).stdout.strip()
        except Exception:  # noqa: BLE001 -- a work tree whose identity FAILED -> fail closed
            return FrozenSubject(False, True, None, None, None, None, False)
        if h_after != h_before:  # HEAD moved during acquisition: (h_before, cand_tree) is unproven
            continue             # discard and re-acquire from the top (h_after is a CHECK, not the baseline)
        bound = cand_tree != base_tree
        try:
            pin = h_before if not bound else _run_git(  # commit-tree only AFTER stability
                ["commit-tree", cand_tree, "-p", h_before, "-m", "gate-parallel staged pin (dangling)"],
                cwd=root, timeout=60).stdout.strip()
        except Exception:  # noqa: BLE001
            return FrozenSubject(False, True, None, None, None, None, False)
        return FrozenSubject(True, True, h_before, base_tree, pin, cand_tree, bound)
    return FrozenSubject(False, True, None, None, None, None, False)  # never stabilized -> fail closed


def restore_worker_copy(copy: Path, pin: str | None = None) -> None:
    """Restore a SHARED P worker copy to its pinned tree BETWEEN scenario executions,
    so NEITHER a tracked-source mutation NOR an ignored/untracked artifact a previous
    scenario created can poison the closure the next scenario imports
    (`snapshot_volatile_state` restores only volatile `.harness` state, never source).
    Reset tracked source to the pin, drop untracked AND `.gitignore`d leftovers
    (`clean -fdxq` — a plain `-fdq` leaves ignored files behind, which `git status`
    also hides), re-materialize the authorized empty volatile dirs, ATTEST the copy is
    exactly P, then assert NO residual ignored/untracked state survives (which a
    porcelain-only attest would not catch). Raises ParallelInfraError if integrity
    cannot be restored. `pin` defaults to the copy's own detached HEAD (which IS P)."""
    _run_git(["reset", "-q", "--hard", "HEAD"], cwd=copy, timeout=60)
    _run_git(["clean", "-fdxq"], cwd=copy, timeout=60, check=False)  # -x ALSO removes IGNORED artifacts
    for rel in _VOLATILE_DIRS:
        (copy / rel).mkdir(parents=True, exist_ok=True)
    attest_worker_copy(copy, pin or _head_sha(copy))  # HEAD == pin AND (tracked) worktree clean
    # Ignored files are invisible to `git status --porcelain`; check `--ignored` too so a
    # scenario-created ignored artifact capable of affecting the next scenario cannot
    # pass attestation. The authorized volatile dirs are empty -> invisible to git.
    residual = _run_git(["status", "--porcelain", "--ignored"], cwd=copy, timeout=60).stdout.strip()
    if residual:
        raise ParallelInfraError("P-copy integrity: residual ignored/untracked state survives restore: "
                                 f"{residual[:200]!r}")


def _candidate_scenario_blobs(root: Path, pin: str) -> dict[str, str] | None:
    """`{'<name>.py': blobSha}` for the DIRECT-CHILD `*.py` blobs of the candidate
    tree's `testing/scenarios` (NON-recursive `git ls-tree` — byte-for-byte the
    direct-child `*.py` contract of the incumbent glob; files only, `_*.py`
    included). None on any git error or an implausibly empty result -> the caller
    decides fail-open (non-candidate) vs fail-closed (candidate-bound)."""
    try:
        raw = _run_git(["ls-tree", pin, "--", "testing/scenarios/"], cwd=root, timeout=60).stdout
    except Exception:  # noqa: BLE001
        return None
    blobs: dict[str, str] = {}
    for line in raw.splitlines():
        meta, _, path = line.partition("\t")
        if not path:
            continue
        fields = meta.split()  # "<mode> <type> <object>"
        if len(fields) < 3 or fields[1] != "blob":  # skip subtrees; direct-child files only
            continue
        name = path.rsplit("/", 1)[-1]
        if name.endswith(".py"):
            blobs[name] = fields[2]
    return blobs or None


def candidate_scenario_paths(root: Path, pin: str) -> list[Path] | None:
    """Scenario SET projected from the frozen candidate identity `pin`, not from the
    developer working tree. Because the set comes from the SAME sha workers check
    out, a staged-only ADD is discovered, a staged DELETE/RENAME is honored, and no
    untracked developer-tree file can smuggle itself into candidate coverage. Paths
    are rooted at the real tree so ordering and the (subject-guarded) real-root
    lanes keep working on on-disk files. None on any git error."""
    blobs = _candidate_scenario_blobs(root, pin)
    if not blobs:
        return None
    return sorted(root / "testing" / "scenarios" / name for name in blobs)


def attest_worker_copy(copy: Path, sha: str) -> str:
    """EXP-30 fidelity attestation (a testable seam): the copy's HEAD IS the pinned
    sha AND its worktree is clean (`git status --porcelain` empty). On mismatch
    raise ParallelInfraError so the gate falls back to serial — never a silent
    unfaithful copy. Returns the short sha for the perf row."""
    got = _run_git(["rev-parse", "HEAD"], cwd=copy, timeout=60).stdout.strip()
    if got != sha:
        raise ParallelInfraError(f"worktree HEAD {got[:7]!r} != pinned {sha[:7]!r}")
    porcelain = _run_git(["status", "--porcelain"], cwd=copy, timeout=60).stdout.strip()
    if porcelain:
        raise ParallelInfraError(f"worktree not clean after checkout: {porcelain[:200]!r}")
    return sha[:7]


def make_worker_copy(root: Path, head_sha: str | None = None) -> Path:
    """Disposable HEAD-faithful repo copy for one shard worker: a DETACHED git
    worktree pinned to `head_sha` (resolved from HEAD now when None — the gps
    fixture path). Detached is non-negotiable safety — a commit inside the copy is
    a dangling object that can NEVER move the real repo's refs/heads/* (a hard-kill
    mid-scenario cannot move the owner's branch). Post-checkout the volatile live
    dirs serial's in-gate hold materializes are created empty, then the copy is
    attested faithful. Default home is the system tempdir (never under ROOT, and
    kept shallow so a scenario that nests its own repo/worktree stays under the
    Windows path limit); an explicit HARNESS_SCRATCH_DIR routes it through the
    canonical scratch resolver instead."""
    sha = head_sha or _head_sha(root)
    if os.environ.get("HARNESS_SCRATCH_DIR"):
        from harness_lib.candidate_snapshot import resolve_scratch_root
        target = Path(tempfile.mkdtemp(prefix="gate-par-", dir=str(
            resolve_scratch_root("gate-parallel")))) / "repo"
    else:
        target = Path(tempfile.mkdtemp(prefix="gate-par-")) / "repo"
    _run_git(["worktree", "add", "--detach", str(target), sha], cwd=root, timeout=120)
    for rel in _VOLATILE_DIRS:
        (target / rel).mkdir(parents=True, exist_ok=True)
    attest_worker_copy(target, sha)
    return target


def dispose_worker_copy(copy_root: Path) -> None:
    """Dispose a worker's detached worktree: `git worktree remove --force` run from
    the ROOT repo (so git drops the worktree metadata), THEN rmtree whatever
    tempdir remains. Fail-open — disposal never fails the gate; a leaked worktree
    entry is swept by the `git worktree prune` at the next parallel run's start."""
    try:  # derive ROOT from the copy, then remove the worktree from there
        common = _run_git(["rev-parse", "--git-common-dir"], cwd=copy_root, timeout=60).stdout.strip()
        root = Path(common).resolve().parent
        _run_git(["worktree", "remove", "--force", str(copy_root)], cwd=root, timeout=60)
    except Exception:  # noqa: BLE001 -- worktree metadata cleanup is best-effort
        pass
    try:
        from harness_lib import sandbox_spawn
        sandbox_spawn.fs_release(copy_root.parent)
    except Exception:  # noqa: BLE001
        pass
    try:
        from harness_lib.common import rmtree_robust
        rmtree_robust(copy_root.parent)
    except Exception:  # noqa: BLE001
        shutil.rmtree(copy_root.parent, ignore_errors=True)


def _capture_parallel_forensics(real_root: Path, stem: str, attempt: str, proc: Any) -> None:
    """Minimal failure forensics for the parallel path, persisted under the REAL
    root's sidecar (the worker copy is disposed). stdout+stderr+rc — EXP-30 pair 4
    cost two extra probes because the crash traceback lived in the uncaptured
    stderr. The serial path's run-log/survivor sweep still needs the live tree."""
    try:
        import datetime as _dt
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = real_root / ".harness" / "runs" / "scenario-forensics" / f"{stem}{attempt}-par-{ts}"
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "scenario-stdout.log").write_text(
            f"rc={getattr(proc, 'returncode', '?')}\n\n{getattr(proc, 'stdout', '') or ''}"
            f"\n\n--- STDERR ---\n{getattr(proc, 'stderr', '') or ''}",
            encoding="utf-8")
    except Exception:  # noqa: BLE001 — forensics must never break a worker
        pass


def _scenario_run_in_copy(copy_root: Path, stem: str, env: dict[str, str], timeout: int,
                          real_root: Path) -> dict[str, Any]:
    """Run one scenario inside a worker copy under the SAME per-scenario
    snapshot/restore isolation serial uses, now on the copy's private .harness.
    Serial-parity row: retry-once net, per-scenario duration, SCENARIO-SKIP
    protocol, failure forensics. SPEC-159 shadow stays ABSENT inside the worker:
    the gate annotates it post-merge on the real root (D041 flip, 2026-07-21);
    a copy never touches the verdict cache directly."""
    from harness_lib import scenario_isolation, processes, scenario_verdict
    scen = copy_root / "testing" / "scenarios" / f"{stem}.py"

    def one_attempt(attempt: str = "") -> Any:
        hold = Path(tempfile.mkdtemp(prefix="scniso-par-"))
        token = scenario_isolation.snapshot_volatile_state(copy_root, hold)
        proc = None
        try:
            proc = processes.run_process_tree_bounded(
                [sys.executable, str(scen)], timeout=timeout, cwd=str(copy_root), env=env)
            return proc
        except subprocess.TimeoutExpired:
            return None
        finally:
            # Forensics follow the VERDICT, not the rc: an rc-0 attempt whose
            # scorecard reports failures is exactly the case that used to pass
            # silently with nothing kept to look at (2026-07-28).
            if proc is not None and scenario_verdict.classify(
                    proc.returncode, getattr(proc, "stdout", "") or "")[0] == "fail":
                _capture_parallel_forensics(real_root, stem, attempt, proc)
            scenario_isolation.restore_volatile_state(token)
            shutil.rmtree(hold, ignore_errors=True)

    t0 = time.monotonic()
    proc, verdict, contradiction, recovered = scenario_verdict.run_with_retry(one_attempt)
    ms = (time.monotonic() - t0) * 1000
    stdout = getattr(proc, "stdout", "") or ""
    if verdict == "skip":
        recovered = False  # a retry that self-skips is a skip, not a recovery
    tail = (stdout.strip().splitlines() or [""])[-1]
    if contradiction:  # never let a two-channel disagreement read as a bare tail
        tail = f"{contradiction} | {tail}"
    return {"ok": verdict != "fail", "verdict": verdict,
            "durationMs": round(ms, 1), "recovered": recovered, "tail": tail[:200]}


def _persist_abort_row(root: Path, stage: str, exc: BaseException) -> None:
    """Abort reasons must survive a hard-kill: the settle-time gate_perf flush
    died with both 1800s kills on 2026-08-03 and left ZERO abort rows. Append
    the reason to disk the moment the abort happens. Fail-open."""
    try:
        import datetime as _dt
        p = root / ".harness" / "runs" / "gate-parallel-aborts.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                "stage": stage,
                "error": f"{type(exc).__name__}: {exc}"[:300],
            }, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — telemetry must never break the gate
        pass


def _tag_retry_abort(perf_rows: "list[dict[str, Any]] | None") -> None:
    """Mark the LAST `_parallel_abort` perf row as the RETRY's abort.

    Without this both aborts land as `attempt: parallel` and the ledger reads as
    one abort logged twice -- the operator cannot tell "aborted once" from
    "aborted, retried, aborted again". Caller-side only: the retry stays invisible
    to run_parallel_scenarios' signature. Fail-open, like every ledger write here."""
    for row in reversed(perf_rows or []):
        if row.get("scenario") == "_parallel_abort":
            row["attempt"] = "parallel-retry"
            return


def run_parallel_scenarios(scenario_paths: list[Path], n: int | None, *, root: Path,
                           result_fn: Callable[..., dict[str, str]], env_scrub: set[str],
                           perf_rows: list[dict[str, Any]] | None = None,
                           timeout: int = 900, pin: str | None = None) -> list[dict[str, str]] | None:
    """Sharded run (the gate DEFAULT since the 2026-07-21 flip, D041). Returns
    `result(...)` rows in the ORIGINAL scenario order, or None to signal the gate
    to fall back to its serial loop. ANY parallel-infra error returns None — the
    machinery never fails the gate."""
    try:
        stems = [p.stem for p in scenario_paths if not p.name.startswith("_")]
        if not stems:
            return None
        print("scenarios: SPEC-160 parallel (default since the 2026-07-21 flip, D041; EXP-30 equivalence "
              "proven over the real battery): detached HEAD-pinned worktree copies, attested per worker. "
              "Declared quarantine: m4 noDrift clause only (copy-run EOL-hash). --serial forces the serial "
              "loop; any parallel-infra error falls back to serial automatically.",
              file=sys.stderr, flush=True)
        try:  # crash hygiene: drop any worktree entries leaked by a hard-killed prior run
            _run_git(["worktree", "prune"], cwd=root, timeout=60, check=False)
        except Exception:  # noqa: BLE001 -- prune is best-effort
            pass
        # SINGLE-FREEZE: use the pin the caller already froze for ENUMERATION so
        # discovery and worker execution share one immutable candidate identity (no
        # enumeration/execution TOCTOU). Only recompute when called standalone
        # (pin=None) — other callers and the module self-check are unchanged.
        head = pin or _pin_sha(root)
        env = {k: v for k, v in os.environ.items() if k not in env_scrub}
        env["HARNESS_SCENARIO_ISOLATED"] = "1"
        env["HARNESS_E2E_TIMEOUT_SCALE"] = "3"
        # Declared EXP-30 quarantine marker (owner decision (a), 2026-07-20): a
        # scenario check that is ENVIRONMENTALLY false inside a fresh worktree copy
        # (m4's protected-drift clause — raw-byte hashes vs fresh-checkout EOLs)
        # keys off this to quarantine ITS ONE CLAUSE, loudly. Never set elsewhere.
        env["HARNESS_PARALLEL_COPY"] = "1"
        # kill-audit lines written from inside a disposed worker copy re-root to the
        # real ledger (processes._audit_kill) instead of dying with the copy.
        env["HARNESS_REAL_ROOT"] = str(root)

        def run_shard(shard_stems: list[str]) -> dict[str, dict[str, Any]]:
            copy_root = make_worker_copy(root, head)
            try:
                return {s: _scenario_run_in_copy(copy_root, s, env, timeout, root) for s in shard_stems}
            finally:
                dispose_worker_copy(copy_root)

        t0 = time.monotonic()
        workers = resolve_workers(n, len(stems))
        verdicts = verdicts_parallel(stems, n, run_one=lambda s: False, run_shard=run_shard)
        wall = time.monotonic() - t0
        print(f"scenarios: parallel mode — {len(stems)} scenario(s) across "
              f"{len(shard(stems, workers))} shard(s) in {wall:.1f}s", flush=True)
        recovered = [s for s in stems if verdicts[s].get("recovered")]
        for s in recovered:
            print(f"! scenarios:{s}: recovered-on-retry (flaky; forensics kept)", flush=True)
        if recovered:
            print(f"scenarios recovered-on-retry ({len(recovered)}): {', '.join(recovered)}", flush=True)
        if perf_rows is not None:  # gate-perf visibility (reuses the sidecar): per-scenario + the wall
            for s in stems:  # faithful=True is honest: an unfaithful worktree raises in attest -> serial fallback (rows never emit)
                perf_rows.append({"scenario": s, "attempt": "parallel", "snapshotS": 0.0,
                                  "subprocessS": round(verdicts[s].get("durationMs", 0.0) / 1000, 3),
                                  "restoreS": 0.0, "rc": 0 if verdicts[s].get("ok") else 1,
                                  "faithful": True, "headSha": head[:7]})
            perf_rows.append({"scenario": "_parallel_wall", "attempt": "parallel",
                              "snapshotS": 0.0, "subprocessS": round(wall, 3), "restoreS": 0.0,
                              "rc": 0 if all(v.get("ok") for v in verdicts.values()) else 1,
                              "shards": len(shard(stems, workers)), "copyMode": "worktree"})
        return [result_fn(f"scenarios:{s}", verdicts[s].get("verdict", "fail"),
                          (("recovered-on-retry | " + verdicts[s].get("tail", "")) if verdicts[s].get("recovered")
                           else verdicts[s].get("tail", "parallel"))[:200],
                          duration_ms=verdicts[s].get("durationMs"))
                for s in stems]
    except Exception as exc:  # noqa: BLE001 -- fall back to serial, never fail the gate
        print(f"!! scenarios: parallel infra error ({type(exc).__name__}: {exc}); "
              f"falling back to serial", flush=True)
        _persist_abort_row(root, "parallel", exc)
        if perf_rows is not None:
            # The printed line above goes to the inner runner's stdout, which
            # gate-staged does not retain (its log kept 69 bytes: the verdict
            # only). A 2026-07-26 fallback cost 27min and its trigger was
            # UNRECOVERABLE from artifacts. The ledger is the surface the
            # operator already reads, so the reason lands HERE too.
            perf_rows.append({"scenario": "_parallel_abort", "attempt": "parallel",
                              "snapshotS": 0.0, "subprocessS": 0.0, "restoreS": 0.0, "rc": 1,
                              "error": f"{type(exc).__name__}: {exc}"[:300]})
        return None


def run_serial_lane(lane_paths: "list[Path]", run_isolated: "Callable[..., Any]",
                    result_fn: "Callable[..., dict[str, str]]",
                    perf_rows: "list[dict[str, Any]] | None" = None) -> "list[dict[str, str]]":
    """Run the latency-class lane SERIALLY on the REAL root, after the pool drains.

    Mirrors the GATE'S OWN serial loop, not `recover_copy_skips`' single shot:
    the lane holds the gate's flakiest scenario, so a path without retry-once
    would be the STRICTEST in the gate for exactly the scenario that needs the
    net — reintroducing the symptom this lane exists to remove, at a lower rate.
    Real root is deliberate (invariant 10's precedent): a worktree copy carries
    TRACKED content only, so gitignored fixtures a browser check needs are absent
    there and its checks silently do not run.

    Per-scenario perf rows come free from the injected runner; the lane's own wall
    lands as `_serial_lane` so its cost is checkable from the ledger, not argued."""
    rows: "list[dict[str, str]]" = []
    lane_t0 = time.monotonic()
    ok_all = True
    for path in lane_paths:
        t0 = time.monotonic()
        proc = run_isolated(path)
        recovered = False
        if proc.returncode != 0:
            # Retry-once (serial parity): the first failure's forensics are already
            # captured by the runner before its restore; the retry is a fresh full
            # run. A recovered pass is ANNOTATED, never silent.
            proc = run_isolated(path, attempt="-retry")
            recovered = proc.returncode == 0 and "SCENARIO-SKIP:" not in (proc.stdout or "")
            if recovered:
                print(f"! scenarios:{path.stem}: recovered-on-retry (flaky; forensics kept)", flush=True)
        skipped = proc.returncode == 0 and "SCENARIO-SKIP:" in (proc.stdout or "")
        status = "skip" if skipped else ("pass" if proc.returncode == 0 else "fail")
        ok_all = ok_all and status != "fail"
        tail = ((proc.stdout or "").strip().splitlines() or [""])[-1]
        detail = ("serial-lane | " + ("recovered-on-retry | " if recovered else "") + tail)[:200]
        print(f"! scenarios:{path.stem}: serial lane (real root, uncontended) -> {status}", flush=True)
        rows.append(result_fn(f"scenarios:{path.stem}", status, detail,
                              duration_ms=(time.monotonic() - t0) * 1000))
    if perf_rows is not None and lane_paths:
        perf_rows.append({"scenario": "_serial_lane", "attempt": "parallel", "snapshotS": 0.0,
                          "subprocessS": round(time.monotonic() - lane_t0, 3), "restoreS": 0.0,
                          "rc": 0 if ok_all else 1, "lane": [p.stem for p in lane_paths]})
    return rows


def run_default_parallel(args: Any, scenario_paths: "list[Path]", *, root: Path,
                         result_fn: "Callable[..., dict[str, str]]", env_scrub: set[str],
                         perf_rows: "list[dict[str, Any]]", shadow_ledger: Any,
                         run_isolated: "Callable[[Path], Any] | None" = None,
                         pin: str | None = None) -> "list[dict[str, str]] | None":
    """Gate entry for the SPEC-160 default (D041 flip, 2026-07-21): honors
    `--serial` (returns None so the gate runs its preserved serial loop), runs the
    sharded path, recovers copy-skips serially on the REAL root, and feeds the
    SPEC-159 shadow from the final verdicts.

    Abort policy (2026-08-03, after two 1800s gate kills): an abort retries the
    sharded path ONCE; a SECOND abort returns None (gate falls back serial) —
    unless `HARNESS_GATE_TIME_BOUNDED=1`, the time-bounded `validate --staged`
    run, where the serial loop cannot finish inside the 1800s cap: there it
    returns a single failing `scenarios:_parallel_infra` row (fail fast) instead.
    Every abort reason is persisted at abort time to
    `.harness/runs/gate-parallel-aborts.jsonl`. The SERIAL-LANE MERGE runs under
    the SAME policy (retry once, then bounded fail-fast or serial fallback): it is
    the same class of transient infra failure, and it aborts AFTER the pool has
    already been paid for -- the most expensive place to give up without a retry."""
    if getattr(args, "serial", False):
        return None
    perf_start = len(perf_rows)
    # invariant 11: the latency-class lane leaves the pool BEFORE the purity
    # boundary, so `verdicts_serial(X) == verdicts_parallel(X)` still holds with
    # X = the pool, and run_parallel_scenarios stays byte-identical (gps-5 and the
    # module self-check untouched). Costs ZERO lines in spec_test_gate.py.
    lane = [p for p in scenario_paths
            if p.stem in _SERIAL_LANE and not p.name.startswith("_")] if run_isolated else []
    pool = [p for p in scenario_paths if p not in lane]
    # An EMPTY pool is NOT an abort. run_parallel_scenarios returns None for both
    # (its None-means-fallback contract stays -- other callers and the module
    # self-check read it), so the discriminant lives HERE, in the one caller that
    # owns the partition: an upstream partition_skips drain (scenarioSkipEnabled)
    # would otherwise buy a pointless retry, a lying `_parallel_abort` perf row and
    # an aborts-jsonl line, then fall back to a serial loop with nothing to run.
    # The discriminant is the RUNNABLE set, not `pool`: run_parallel_scenarios drops
    # underscore-prefixed paths itself, so a pool holding only `_*.py` is just as
    # empty to it (F3) -- while `pool` itself still goes in, unchanged.
    runnable_pool = [p for p in pool if not p.name.startswith("_")]
    rows = run_parallel_scenarios(pool, getattr(args, "parallel", None), root=root,
                                  result_fn=result_fn, env_scrub=env_scrub,
                                  perf_rows=perf_rows, pin=pin) if runnable_pool else []
    if rows is None:
        # 2026-08-03: both observed aborts were transient (same path passed at
        # 08:43 and 09:51); one retry converts abort -> ~6min re-run instead of a
        # doomed serial fallback. worktree prune runs inside the retry already.
        print("!! scenarios: parallel aborted; retrying parallel ONCE before any fallback", flush=True)
        rows = run_parallel_scenarios(pool, getattr(args, "parallel", None), root=root,
                                      result_fn=result_fn, env_scrub=env_scrub, perf_rows=perf_rows, pin=pin)
        if rows is None:
            _tag_retry_abort(perf_rows)  # the retry's row must not read as the first abort
    if rows is None:
        if os.environ.get("HARNESS_GATE_TIME_BOUNDED") == "1":
            # Bounded run (gate-staged, 1800s cap): the serial loop needs >1800s
            # for the full battery, so falling back = burning 30min to certify
            # nothing. Fail fast with a durable pointer instead.
            print("!! scenarios: parallel aborted twice under the bounded gate; failing fast "
                  "(serial cannot finish in 1800s) — reasons: .harness/runs/gate-parallel-aborts.jsonl",
                  flush=True)
            return [result_fn("scenarios:_parallel_infra", "fail",
                              "parallel aborted twice; bounded gate fails fast — see "
                              ".harness/runs/gate-parallel-aborts.jsonl; manual escape: --serial")]
        return None
    if run_isolated is not None:
        rows = recover_copy_skips(rows, pool, run_isolated, result_fn)
    # The merge gets the POOL's abort policy (retry once, then bounded fail-fast or
    # serial fallback). `base_rows` is the value ENTERING the block: attempt 2 must
    # re-merge from here, never append the lane onto an already-merged list.
    base_rows = rows
    # The merge retry pays for the LANE again, so it must only buy a chance at a
    # TRANSIENT failure (F4). A pool-side row already missing from `base_rows` is
    # deterministic -- that list is frozen here, so attempt 2 re-runs the lane to
    # fail the same completeness check for the same reason.
    pool_expected = sorted(f"scenarios:{p.stem}" for p in runnable_pool)
    pool_incomplete = sorted(str(r.get("name")) for r in base_rows) != pool_expected
    for attempt in (1, 2):
        try:
            rows = base_rows + run_serial_lane(lane, run_isolated, result_fn, perf_rows)
            # THE COMPLETENESS NET. Keyed on `scenario_paths` — the GATE's own input,
            # never a second copy of the partition logic, so a bug in the partition
            # cannot also hide in its check. The length guard stops a duplicate row
            # masking a dropped one. Silently losing a scenario is the catastrophic
            # failure here; crashing is merely loud.
            expected = [f"scenarios:{p.stem}" for p in scenario_paths if not p.name.startswith("_")]
            by = {str(r.get("name")): r for r in rows}
            if len(rows) != len(expected) or sorted(by) != sorted(expected):
                raise ParallelInfraError(f"serial-lane merge incomplete: {len(rows)} row(s) "
                                         f"for {len(expected)} scenario(s)")
            rows = [by[n] for n in expected]  # emit in ORIGINAL scenario order
            break
        except Exception as exc:  # noqa: BLE001 — MUST be caught here: the gate calls this
            # function inside a try/FINALLY (spec_test_gate.py), so anything raised past
            # this frame crashes the whole gate instead of falling back. Fail-safe to
            # serial, and land the reason in the ledger — gate-staged discards the
            # inner stdout, so a printed-only reason is an unrecoverable trigger.
            print(f"!! scenarios: serial-lane merge error ({type(exc).__name__}: {exc})", flush=True)
            _persist_abort_row(root, "serial-lane-merge", exc)
            if perf_rows is not None:
                perf_rows.append({"scenario": "_parallel_abort", "attempt": "parallel",
                                  "snapshotS": 0.0, "subprocessS": 0.0, "restoreS": 0.0, "rc": 1,
                                  "error": f"{type(exc).__name__}: {exc}"[:300]})
            if attempt == 1 and pool_incomplete:
                print("!! scenarios: serial-lane merge is short a POOL row; the retry re-runs the "
                      "lane only and cannot fix that -- skipping it", flush=True)
            elif attempt == 1:
                print("!! scenarios: retrying serial-lane merge ONCE before any fallback", flush=True)
                continue
            _tag_retry_abort(perf_rows)
            # The terminal branch is shared, so it must not claim two aborts when the
            # retry was skipped -- the operator reads these lines to size the failure.
            spent = "aborted once, retry skipped (pool-side)" if pool_incomplete else "aborted twice"
            if os.environ.get("HARNESS_GATE_TIME_BOUNDED") == "1":
                # Same reasoning as the pool site: the serial loop needs >1800s, so a
                # fallback here burns the cap to certify nothing -- and the pool has
                # ALREADY been paid for. Fail fast with a durable pointer.
                print(f"!! scenarios: serial-lane merge {spent} under the bounded gate; failing "
                      "fast (serial cannot finish in 1800s) -- reasons: "
                      ".harness/runs/gate-parallel-aborts.jsonl", flush=True)
                return [result_fn("scenarios:_parallel_infra", "fail",
                                  f"serial-lane merge {spent}; bounded gate fails fast -- see "
                                  ".harness/runs/gate-parallel-aborts.jsonl; manual escape: --serial")]
            print(f"!! scenarios: serial-lane merge {spent}; falling back to serial", flush=True)
            return None
    try:  # shadow is measure-only: a bug here must neither fail the gate nor
        annotate_shadow_from_merged(shadow_ledger, rows, perf_rows[perf_start:])
    except Exception:  # noqa: BLE001 — lose the already-merged verdict rows
        pass
    return rows


def recover_copy_skips(rows: "list[dict[str, str]]", scenario_paths: "list[Path]",
                       run_isolated: "Callable[[Path], Any]",
                       result_fn: "Callable[..., dict[str, str]]") -> "list[dict[str, str]]":
    """SPEC-160 skip-recovery (owner direction 2026-07-21: serial divergences
    matter — the default gate must not silently lose coverage serial had). A
    scenario that SELF-SKIPPED inside a worker copy (environmental absence:
    gitignored node_modules, live-only state) gets ONE serial re-run on the REAL
    root under the standard per-scenario isolation. A serial re-skip stays an
    honest skip (the guard evaluated real conditions); a pass/fail REPLACES the
    row, loudly labeled — and a fail fails the gate like any other. Fail-open:
    an error re-running keeps the original skip row (never worse than before)."""
    out: "list[dict[str, str]]" = []
    by_stem = {p.stem: p for p in scenario_paths if not p.name.startswith("_")}
    for row in rows:
        name = str(row.get("name", ""))
        stem = name.split(":", 1)[1] if name.startswith("scenarios:") else ""
        if row.get("status") != "skip" or stem not in by_stem:
            out.append(row)
            continue
        try:
            proc = run_isolated(by_stem[stem])
            if proc.returncode == 0 and "SCENARIO-SKIP:" in (proc.stdout or ""):
                out.append(row)  # skips on the real root too: honest, keep it
                continue
            status = "pass" if proc.returncode == 0 else "fail"
            tail = ((proc.stdout or "").strip().splitlines() or [""])[-1]
            print(f"! scenarios:{stem}: copy-skip recovered serially on the real root -> {status}", flush=True)
            out.append(result_fn(f"scenarios:{stem}", status, ("serial-recovery | " + tail)[:200]))
        except Exception:  # noqa: BLE001 — recovery must never fail the gate
            out.append(row)
    return out


def annotate_shadow_from_merged(shadow_ledger: Any, merged_rows: list[dict[str, str]],
                                parallel_perf_rows: list[dict[str, Any]]) -> None:
    """SPEC-159 shadow wiring for the parallel default (D041 flip, 2026-07-21):
    annotate the shadow ledger from the MERGED per-scenario verdicts, on the real
    root. Skip verdicts never annotate and never reach the verdict cache (same
    rule as the serial loop); the `_parallel_wall` row has no verdict and is
    excluded naturally. Fail-open end to end (annotate itself is fail-open)."""
    status_by_stem = {r["name"].split(":", 1)[1]: r["status"]
                      for r in merged_rows if r.get("name", "").startswith("scenarios:")}
    for row in parallel_perf_rows:
        stem = row.get("scenario")
        if status_by_stem.get(stem) in ("pass", "fail"):
            shadow_ledger.annotate(row, stem, status_by_stem[stem] == "pass")


def _real_git_worktree_check() -> None:
    """REAL-git integration for the worktree copy: a scratch repo proves the copy
    is HEAD-faithful, the detached-commit REF-SAFETY property (non-negotiable #1),
    and clean disposal. Skips (no-op) if git is unavailable."""
    import shutil as _sh
    if _sh.which("git") is None:
        print("gate_parallel real-git check SKIPPED (git not on PATH)")
        return

    def git(args: list[str], cwd: Path, check: bool = True) -> Any:
        return _run_git(args, cwd=cwd, timeout=60, check=check)

    scratch = Path(tempfile.mkdtemp(prefix="gate-par-selfcheck-"))
    try:
        git(["init", "-q"], scratch)
        git(["config", "user.email", "t@t.test"], scratch)
        git(["config", "user.name", "selfcheck"], scratch)
        git(["config", "commit.gpgsign", "false"], scratch)
        (scratch / "file.txt").write_text("hello\n", encoding="utf-8")
        (scratch / ".harness" / "state").mkdir(parents=True, exist_ok=True)
        (scratch / ".harness" / "state" / "x.json").write_text('{"k": 1}\n', encoding="utf-8")
        git(["add", "-A"], scratch)
        git(["commit", "-q", "-m", "init"], scratch)
        branch = git(["rev-parse", "--abbrev-ref", "HEAD"], scratch).stdout.strip()
        ref_before = git(["rev-parse", f"refs/heads/{branch}"], scratch).stdout.strip()
        pinned = _head_sha(scratch)
        assert ref_before == pinned, (ref_before, pinned)

        copy = make_worker_copy(scratch, pinned)  # attests faithful internally
        try:
            assert _head_sha(copy) == pinned, _head_sha(copy)          # .git works, HEAD matches
            assert git(["status", "--porcelain"], copy).stdout.strip() == ""   # porcelain empty
            for rel in _VOLATILE_DIRS:                                  # volatile dirs materialized
                assert (copy / rel).is_dir(), rel
            assert attest_worker_copy(copy, pinned) == pinned[:7]       # attest seam true on faithful

            # NON-NEGOTIABLE #1: a commit INSIDE the detached copy is a dangling
            # object — it must NOT move the scratch repo's branch ref.
            (copy / "file.txt").write_text("mutated inside the copy\n", encoding="utf-8")
            git(["add", "-A"], copy)
            git(["commit", "-q", "-m", "copy-side commit"], copy)
            copy_head = _head_sha(copy)
            assert copy_head != pinned, "commit in copy must advance the copy's detached HEAD"
            ref_after = git(["rev-parse", f"refs/heads/{branch}"], scratch).stdout.strip()
            assert ref_after == ref_before, (
                f"REF-SAFETY VIOLATED: detached-copy commit moved real branch {branch} "
                f"{ref_before[:7]} -> {ref_after[:7]}")
        finally:
            dispose_worker_copy(copy)
        # dispose removed the worktree: only the main worktree remains.
        remaining = [l for l in git(["worktree", "list", "--porcelain"], scratch).stdout.splitlines()
                     if l.startswith("worktree ")]
        assert len(remaining) == 1, remaining
        assert not copy.exists(), copy

        # STAGED-PIN (D041 flip review, 2026-07-21): index != HEAD must pin a
        # DANGLING commit of the index tree — copies exercise the STAGED code
        # (not the previous commit's), and no ref ever moves.
        assert _pin_sha(scratch) == pinned, "clean index must pin HEAD"
        (scratch / "file.txt").write_text("staged content\n", encoding="utf-8")
        git(["add", "file.txt"], scratch)
        staged_pin = _pin_sha(scratch)
        assert staged_pin != pinned, "dirty index must pin a fresh dangling commit"
        assert git(["rev-parse", f"refs/heads/{branch}"], scratch).stdout.strip() == ref_before, \
            "staged pin must move no ref"
        copy2 = make_worker_copy(scratch, staged_pin)
        try:
            assert (copy2 / "file.txt").read_text(encoding="utf-8") == "staged content\n", \
                "copy must contain the STAGED content"
        finally:
            dispose_worker_copy(copy2)
        print("gate_parallel real-git worktree check OK (faithful + ref-safety + staged-pin + dispose)")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _self_check() -> int:
    # Deterministic fixture: stem -> verdict. A pure runner, no repo copies.
    truth = {"a": True, "b": False, "c": True, "d": True, "e": False, "f": True, "g": True}
    scenarios = list(truth)
    run_one = lambda s: truth[_stem(s)]

    # shard split: partition, no loss/dup, balanced (<=1 apart).
    for n in (1, 2, 3, 4, 7, 10):
        sh = shard(scenarios, n)
        flat = [x for b in sh for x in b]
        assert sorted(flat) == sorted(scenarios), (n, sh)
        assert len(flat) == len(set(flat)) == len(scenarios), (n, sh)
        assert len(sh) == min(n, len(scenarios)), (n, sh)
        assert max(len(b) for b in sh) - min(len(b) for b in sh) <= 1, (n, sh)

    # merge preserves verdicts; conflicting duplicate raises.
    assert merge_verdicts([{"a": True}, {"b": False}]) == {"a": True, "b": False}
    try:
        merge_verdicts([{"a": True}, {"a": False}])
        raise AssertionError("conflicting merge must raise")
    except ParallelInfraError:
        pass

    # EQUIVALENCE: serial verdicts == parallel verdicts for every worker count.
    serial = verdicts_serial(scenarios, run_one)
    assert serial == truth, serial
    for n in (1, 2, 3, 4, 8, 16):
        par = verdicts_parallel(scenarios, n, run_one)
        assert par == serial, (n, par, serial)
        assert equivalent(scenarios, n, run_one), n

    # incomplete merge (a shard drops a scenario) -> raise, then fall back.
    def dropping_shard(sc: list[Any]) -> dict[str, bool]:
        m = verdicts_serial(sc, run_one)
        m.pop(next(iter(m)), None)  # lose one verdict
        return m
    try:
        verdicts_parallel(scenarios, 3, run_one, run_shard=dropping_shard)
        raise AssertionError("incomplete merge must raise")
    except ParallelInfraError:
        pass

    # FALLBACK: a shard that raises -> serial verdicts, mode serial-fallback, full map.
    def exploding_shard(sc: list[Any]) -> dict[str, bool]:
        raise RuntimeError("simulated worker crash")
    verdicts, mode = run_with_fallback(scenarios, 4, run_one, run_shard=exploding_shard)
    assert mode == "serial-fallback" and verdicts == serial, (mode, verdicts)
    # a healthy run reports parallel and stays equivalent.
    verdicts, mode = run_with_fallback(scenarios, 4, run_one)
    assert mode == "parallel" and verdicts == serial, (mode, verdicts)

    # resolve_workers bounds.
    assert resolve_workers(0, 100) >= 2 and resolve_workers(0, 100) <= _MAX_WORKERS
    assert resolve_workers(4, 2) == 2 and resolve_workers(100, 50) == _MAX_WORKERS and resolve_workers(3, 10) == 3

    # SERIAL-PARITY GLUE: run_parallel_scenarios carries verdict/skip/duration/
    # recovered/tail through to result rows + perf rows (runner patched — no repo
    # copies; the pure orchestration above stays the equivalence proof).
    fake_rows = {
        "a": {"ok": True, "verdict": "pass", "durationMs": 10.0, "recovered": False, "tail": "a ok"},
        "b": {"ok": True, "verdict": "skip", "durationMs": 1.0, "recovered": False,
              "tail": "SCENARIO-SKIP: b guard"},
        "c": {"ok": True, "verdict": "pass", "durationMs": 20.0, "recovered": True, "tail": "c ok"},
        "d": {"ok": False, "verdict": "fail", "durationMs": 30.0, "recovered": False, "tail": "d bad"},
    }
    saved = (globals()["_scenario_run_in_copy"], globals()["make_worker_copy"],
             globals()["dispose_worker_copy"])
    globals()["_scenario_run_in_copy"] = lambda copy_root, s, env, timeout, real_root: fake_rows[s]
    globals()["make_worker_copy"] = lambda root, head_sha=None: root
    globals()["dispose_worker_copy"] = lambda copy_root: None
    try:
        captured: list[dict] = []
        def fake_result(name, status, detail="", duration_ms=None):
            row = {"name": name, "status": status, "detail": detail, "durationMs": duration_ms}
            captured.append(row)
            return row
        perf: list[dict] = []
        rows = run_parallel_scenarios([Path(f"testing/scenarios/{s}.py") for s in fake_rows],
                                      2, root=Path("."), result_fn=fake_result,
                                      env_scrub=set(), perf_rows=perf)
        assert rows is not None and [r["name"] for r in rows] == [f"scenarios:{s}" for s in fake_rows], rows
        by = {r["name"].split(":", 1)[1]: r for r in rows}
        assert by["b"]["status"] == "skip", by["b"]  # SCENARIO-SKIP parity
        assert by["c"]["detail"].startswith("recovered-on-retry | "), by["c"]
        assert by["d"]["status"] == "fail" and by["a"]["durationMs"] == 10.0, (by["d"], by["a"])
        per_scen = [p for p in perf if p["scenario"] in fake_rows]
        assert len(per_scen) == 4 and {p["scenario"] for p in per_scen} == set(fake_rows), perf
        wall_row = [p for p in perf if p["scenario"] == "_parallel_wall"]
        assert len(wall_row) == 1 and wall_row[0]["rc"] == 1, wall_row  # d failed
    finally:
        (globals()["_scenario_run_in_copy"], globals()["make_worker_copy"],
         globals()["dispose_worker_copy"]) = saved

    # DEFAULT-DISPATCH (D041 flip): --serial -> None (gate runs its serial loop);
    # default -> merged rows + shadow fed from MERGED verdicts (skip + wall rows
    # excluded); a crashing ledger neither fails the dispatch nor loses the rows.
    import types
    fake_merged = [{"name": "scenarios:a", "status": "pass"},
                   {"name": "scenarios:b", "status": "skip"},
                   {"name": "scenarios:d", "status": "fail"}]
    # The scenario list must MATCH the rows the fake returns. It used to be `[]`
    # while the fake returned three rows — harmless before invariant 11, but the
    # v4 completeness net reads exactly that correspondence and (correctly) called
    # the mismatch a dropped-scenario bug. The fixture was inconsistent, not the net.
    fake_paths = [Path(f"testing/scenarios/{s}.py") for s in ("a", "b", "d")]
    def fake_rps(paths, n, *, root, result_fn, env_scrub, perf_rows=None, timeout=900, pin=None):
        perf_rows.extend([{"scenario": "a", "subprocessS": 1.0}, {"scenario": "b", "subprocessS": 0.1},
                          {"scenario": "d", "subprocessS": 2.0}, {"scenario": "_parallel_wall"}])
        return fake_merged
    saved_rps, globals()["run_parallel_scenarios"] = globals()["run_parallel_scenarios"], fake_rps
    try:
        class _Ledger:
            def __init__(self) -> None:
                self.calls: list[tuple] = []
            def annotate(self, row, stem, passed) -> None:
                self.calls.append((stem, passed))
        led = _Ledger()
        assert run_default_parallel(types.SimpleNamespace(serial=True, parallel=None), fake_paths,
                                    root=Path("."), result_fn=fake_result, env_scrub=set(),
                                    perf_rows=[], shadow_ledger=led) is None
        assert led.calls == [], "--serial must run nothing and annotate nothing"
        pr: list[dict] = []
        out = run_default_parallel(types.SimpleNamespace(serial=False, parallel=None), fake_paths,
                                   root=Path("."), result_fn=fake_result, env_scrub=set(),
                                   perf_rows=pr, shadow_ledger=led)
        assert out == fake_merged, out
        assert led.calls == [("a", True), ("d", False)], led.calls  # skip 'b' + wall excluded
        class _Boom:
            def annotate(self, *a) -> None:
                raise RuntimeError("ledger boom")
        out2 = run_default_parallel(types.SimpleNamespace(serial=False, parallel=None), fake_paths,
                                    root=Path("."), result_fn=fake_result, env_scrub=set(),
                                    perf_rows=[], shadow_ledger=_Boom())
        assert out2 == fake_merged, "annotate crash must not lose the merged rows"
    finally:
        globals()["run_parallel_scenarios"] = saved_rps

    # ABORT RETRY + BOUNDED FAIL-FAST (2026-08-03, two 1800s gate kills): an abort
    # retries the sharded path ONCE; a double abort fails FAST under the bounded
    # gate (serial cannot finish in 1800s) and keeps the serial fallback otherwise.
    # Same injected-rps idiom — no repo copies, no subprocesses.
    calls: list[int] = []

    def flaky_rps(paths, n, *, root, result_fn, env_scrub, perf_rows=None, timeout=900, pin=None):
        calls.append(1)
        return None if len(calls) == 1 else fake_merged

    def dead_rps(paths, n, *, root, result_fn, env_scrub, perf_rows=None, timeout=900, pin=None):
        calls.append(1)
        return None

    saved_rps2 = globals()["run_parallel_scenarios"]
    saved_bounded = os.environ.get("HARNESS_GATE_TIME_BOUNDED")
    try:
        os.environ.pop("HARNESS_GATE_TIME_BOUNDED", None)
        globals()["run_parallel_scenarios"] = flaky_rps
        retried = run_default_parallel(types.SimpleNamespace(serial=False, parallel=None), fake_paths,
                                       root=Path("."), result_fn=fake_result, env_scrub=set(),
                                       perf_rows=[], shadow_ledger=_Ledger())
        assert retried == fake_merged and len(calls) == 2, (retried, calls)

        globals()["run_parallel_scenarios"] = dead_rps
        calls.clear()
        os.environ["HARNESS_GATE_TIME_BOUNDED"] = "1"
        bounded = run_default_parallel(types.SimpleNamespace(serial=False, parallel=None), fake_paths,
                                       root=Path("."), result_fn=fake_result, env_scrub=set(),
                                       perf_rows=[], shadow_ledger=_Ledger())
        assert bounded is not None and len(bounded) == 1 and len(calls) == 2, (bounded, calls)
        assert bounded[0]["name"] == "scenarios:_parallel_infra", bounded
        assert bounded[0]["status"] == "fail", bounded

        calls.clear()
        os.environ.pop("HARNESS_GATE_TIME_BOUNDED", None)
        assert run_default_parallel(types.SimpleNamespace(serial=False, parallel=None), fake_paths,
                                    root=Path("."), result_fn=fake_result, env_scrub=set(),
                                    perf_rows=[], shadow_ledger=_Ledger()) is None, \
            "unbounded double abort must keep the serial fallback"
        assert len(calls) == 2, calls  # retried once, then fell back
    finally:
        globals()["run_parallel_scenarios"] = saved_rps2
        if saved_bounded is None:
            os.environ.pop("HARNESS_GATE_TIME_BOUNDED", None)
        else:
            os.environ["HARNESS_GATE_TIME_BOUNDED"] = saved_bounded

    # DURABLE ABORT LEDGER: the reason lands on disk AT ABORT TIME — the
    # settle-time gate_perf flush died with both 1800s kills. Tmp root only.
    ledger_root = Path(tempfile.mkdtemp(prefix="gate-par-ledger-"))
    try:
        _persist_abort_row(ledger_root, "parallel", RuntimeError("boom"))
        lines = (ledger_root / ".harness" / "runs" / "gate-parallel-aborts.jsonl").read_text(
            encoding="utf-8").splitlines()
        assert len(lines) == 1, lines
        entry = json.loads(lines[0])
        assert entry["stage"] == "parallel", entry
        assert "RuntimeError: boom" in entry["error"], entry
    finally:
        shutil.rmtree(ledger_root, ignore_errors=True)

    _real_git_worktree_check()

    print("gate_parallel self-check OK")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/ -> `from harness_lib import ...`
    raise SystemExit(_self_check())
