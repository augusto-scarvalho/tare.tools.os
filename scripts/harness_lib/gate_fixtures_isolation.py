"""Gate fixture — cross-project isolation audit (Phase 0 of the DATA-isolation
roadmap, docs/roadmap/cross-project-isolation-data.md).

Dual-subject deterministic probe: two throwaway git repos (A and B, each with
a unique sentinel token) are registered as governed targets, A's real target
gate runs, and the fixture asserts the isolation invariant from the outside:

  env-canary     — a var exported in the harness process is INVISIBLE to A's
                   own gate commands (the target-gate-env-filter, live);
  b-tree-clean   — running A's flows leaves B's tree byte-identical
                   (git porcelain empty, file list unchanged);
  cross-bleed    — A's harness-side artifact dirs (graphify-out/targets/A,
                   .harness/state/targets/A) contain neither B's token nor
                   B's root path, and vice versa;
  records-credit — a records entry written during target work carries the
                   structural `subject` (records-subject-dimension, live);
  calibration-scope — OBSERVE-ONLY: names the known remaining gap (global
                   charsPerToken override; per-target-token-calibration OPEN).

Shared gate helpers arrive via bind(env) from spec_test_gate (MF.2 idiom).
Registered as WORKFLOW_FIXTURE_FUNCTIONS["isolation-audit"], run in the
workflow gate and standalone via `spec_test_gate.py --fixture isolation-audit`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any


def bind(env: dict[str, Any]) -> None:
    """Receive shared gate helpers/constants from spec_test_gate."""
    globals().update(env)


_NAMES = ("iso-fixture-a", "iso-fixture-b")
_CANARY = "ISOCANARY_FIXTURE"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, timeout=60)


def _tree_files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                  if p.is_file() and ".git" not in p.parts)


def _porcelain(root: Path) -> str:
    proc = subprocess.run(["git", "status", "--porcelain"], cwd=str(root),
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    return (proc.stdout or "").strip()


def _scan_dirs_for(dirs: list[Path], needles: list[str]) -> list[str]:
    hits: list[str] = []
    for base in dirs:
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for needle in needles:
                if needle and needle in text:
                    hits.append(f"{needle[:24]}… in {path}")
    return hits


def _cleanup(root: Path, tmp: Path | None) -> None:
    os.environ.pop(_CANARY, None)
    for n in _NAMES:
        for base in (root / ".harness" / "targets" / n,
                     root / ".harness" / "state" / "targets" / n,
                     root / "graphify-out" / "targets" / n):
            shutil.rmtree(base, ignore_errors=True)
    if tmp is not None:
        shutil.rmtree(tmp, ignore_errors=True)


def check_isolation_audit_fixture() -> list[dict[str, str]]:
    from harness_lib import gate_generic, records
    from harness_lib import targets as targets_lib

    out: list[dict[str, str]] = []
    tmp: Path | None = None
    try:
        tmp = Path(tempfile.mkdtemp(prefix="iso-audit-"))
        _cleanup(ROOT, None)  # stale registrations from a crashed prior run
        os.environ[_CANARY] = "leak-me"
        token = {n: f"ISOTOKEN-{n}-{uuid.uuid4().hex[:8]}" for n in _NAMES}
        roots: dict[str, Path] = {}
        for n in _NAMES:
            repo = tmp / n
            repo.mkdir(parents=True)
            (repo / "sentinel.py").write_text(f'TOKEN = "{token[n]}"\n', encoding="utf-8")
            _git(repo, "init", "-q")
            _git(repo, "add", "-A")
            _git(repo, "-c", "user.name=iso", "-c", "user.email=iso@fixture",
                 "commit", "-qm", "init")
            targets_lib.register(ROOT, n, repo)
            profile = ROOT / ".harness" / "targets" / n / "target.json"
            entry = json.loads(profile.read_text(encoding="utf-8"))
            probe = (f'"{sys.executable}" -c "import os;'
                     f"print('ISOLEAK' if os.environ.get('{_CANARY}') else 'ENVCLEAN')\"")
            entry["validation"] = {"smoke": {"commands": [probe]}}
            profile.write_text(json.dumps(entry, indent=2), encoding="utf-8")
            roots[n] = repo

        b_files = _tree_files(roots["iso-fixture-b"])
        results_a, _status_a = gate_generic.run_target_gate("iso-fixture-a", "smoke")

        # env-canary: A's own command saw a filtered environment.
        cmd_rows = [r for r in results_a if ":command:" in r["name"]]
        canary_ok = bool(cmd_rows) and all(
            r["status"] == "pass" and "ENVCLEAN" in r["detail"] for r in cmd_rows)
        out.append(result("isolation-audit:env-canary",
                          "pass" if canary_ok else "fail",
                          "harness env canary invisible to target commands" if canary_ok
                          else f"canary visible or probe failed: {cmd_rows}"))

        # b-tree-clean: A's flow left B byte-identical.
        clean = _porcelain(roots["iso-fixture-b"]) == "" and _tree_files(roots["iso-fixture-b"]) == b_files
        out.append(result("isolation-audit:b-tree-clean",
                          "pass" if clean else "fail",
                          "sibling target tree unchanged by A's gate" if clean
                          else "B's tree changed during A's flow"))

        # cross-bleed: run B's gate too, then scan both artifact worlds.
        gate_generic.run_target_gate("iso-fixture-b", "smoke")
        hits = []
        for me, other in (("iso-fixture-a", "iso-fixture-b"), ("iso-fixture-b", "iso-fixture-a")):
            hits.extend(_scan_dirs_for(
                [targets_lib.out_dir(me), targets_lib.state_dir(me)],
                [token[other], str(roots[other])]))
        out.append(result("isolation-audit:cross-bleed",
                          "pass" if not hits else "fail",
                          "no sibling token/path in either subject's artifacts" if not hits
                          else "; ".join(hits[:5])))

        # records-credit: the subject dimension is structural (temp harness root).
        rec_root = tmp / "rec-harness"
        (rec_root / ".harness" / "state").mkdir(parents=True)
        entry = records.add_entry(rec_root, "note", "iso fixture probe",
                                  subject="iso-fixture-a")
        worklog = (rec_root / ".harness" / "state" / "worklog.json").read_text(encoding="utf-8")
        credited = entry.get("subject") == "iso-fixture-a" and "iso-fixture-a" in worklog
        out.append(result("isolation-audit:records-credit",
                          "pass" if credited else "fail",
                          "records entry carries its subject" if credited
                          else f"entry={entry}"))

        # calibration-scope: HARD since per-target-token-calibration landed —
        # a target-subject calibration write must resolve to ITS state dir and
        # never move the harness's own learned value (or another subject's).
        from harness_lib import token_calibration
        harness_file = ROOT / token_calibration.CALIBRATION_REL
        before = harness_file.read_bytes() if harness_file.exists() else None
        token_calibration.write_override(ROOT, 2.5, subject="iso-fixture-a")
        after = harness_file.read_bytes() if harness_file.exists() else None
        scoped_ok = (token_calibration.read_override(ROOT, "iso-fixture-a") == 2.5
                     and token_calibration.read_override(ROOT, "iso-fixture-b") is None
                     and before == after)
        out.append(result("isolation-audit:calibration-scope",
                          "pass" if scoped_ok else "fail",
                          "per-subject calibration isolated (target write never moves "
                          "the harness or a sibling)" if scoped_ok
                          else "target calibration write leaked across subjects"))
        return out
    except Exception as exc:  # a broken probe is a fixture failure, never a crash
        out.append(result("isolation-audit:fixture", "fail",
                          f"{type(exc).__name__}: {exc}"))
        return out
    finally:
        _cleanup(ROOT, tmp)
