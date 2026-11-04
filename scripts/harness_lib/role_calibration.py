"""RF.1 phase 1 — per-role (model, effort) bake-off runner. OBSERVE-ONLY.

Role -> (model, effort) routing is hand-tuned with no empirical basis; this
module gives one role a measured value table WITHOUT touching routing. A
deterministic matrix runner executes one operator-supplied task command per
(model, effort) cell; the quality signal is `rc == 0` — never an LLM judge
(CQ independence warning: a model must not grade its own bake-off). Results
land in `.harness/state/bakeoff/<utc-ts>.json` and the printed table ends with
a `recommended:` line, which is ADVICE ONLY.

Phase 2 (2026-07-27, owner "bora fazer") adds the other half: `compare` shows the
measured recommendation beside what routing actually resolves today (read-only),
and `apply_recommendation` writes it into a NAMED profile — but ONLY behind an
explicit `confirm=True`, so no gate, scenario or scheduled path can reach a
routing write. The RF.1 rule stands: a role's routing is never auto-flipped. No
gate or scenario may ever pass `--execute` either (real token spend is
operator-only).
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

try:
    from .common import HarnessError, write_json
    from .model_routing import KNOWN_EFFORTS, enforce_spawn
except ImportError:  # run directly for the __main__ self-check
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from harness_lib.common import HarnessError, write_json
    from harness_lib.model_routing import KNOWN_EFFORTS, enforce_spawn

BAKEOFF_DIR_REL = ".harness/state/bakeoff"
CELL_TIMEOUT_S = 3600  # one real delegated task per cell; bounded, generous


def plan_cells(role: str, models: list[str], efforts: list[str],
               cards: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Pure matrix planner: one cell per (model, effort) pair, each validated
    against the model-cards rows (`model_routing.cards_list(root)`, read-only)."""
    models = [m.strip() for m in models if m and m.strip()]
    efforts = [e.strip() for e in efforts if e and e.strip()]
    if not role or not models or not efforts:
        raise HarnessError("bake-off needs a role, --models and --efforts")
    by_id = {c["id"]: c for c in cards if isinstance(c, dict) and c.get("id")}
    cells: list[dict[str, str]] = []
    for model in models:
        card = by_id.get(model)
        if card is None:
            raise HarnessError(f"unknown card {model!r} (see `models list`)")
        supported = card.get("reasoning") or []
        for effort in efforts:
            if effort not in KNOWN_EFFORTS:
                raise HarnessError(f"unknown effort {effort!r}; known: {', '.join(KNOWN_EFFORTS)}")
            if supported and effort not in supported:
                raise HarnessError(f"effort {effort!r} not supported by card {model!r} (supports {supported})")
            cells.append({"model": model, "effort": effort})
    return cells


def _cell_cmd(template: str, cell: dict[str, str]) -> str:
    # str.replace, not .format: task commands legitimately contain JSON braces.
    return template.replace("{model}", cell["model"]).replace("{effort}", cell["effort"])


def _tokens_from_stdout(stdout: str | None) -> int | float | None:
    """Tokens when the task's result JSON (stdout) reports them, else None."""
    # ponytail: cost joined from the cost_metrics ledger delta in phase 2
    try:
        data = json.loads(stdout or "")
    except (json.JSONDecodeError, TypeError):
        return None
    tokens = data.get("tokens") if isinstance(data, dict) else None
    return tokens if isinstance(tokens, (int, float)) and not isinstance(tokens, bool) else None


def _rank_key(cell: dict[str, Any]) -> tuple:
    """pass-first, then cheapest: fewest tokens (unknown ranks last), fastest."""
    return (0 if cell["pass"] else 1,
            cell["tokens"] if cell["tokens"] is not None else float("inf"),
            cell["durationS"])


def _print_table(rows: list[dict[str, Any]]) -> None:
    print(f"{'model':<18} {'effort':<7} {'pass':<5} {'durationS':>9}  tokens")
    for r in rows:
        tokens = "-" if r["tokens"] is None else r["tokens"]
        print(f"{r['model']:<18} {r['effort']:<7} {str(r['pass']).lower():<5} {r['durationS']:>9}  {tokens}")


def run(root: Path | str, role: str, cells: list[dict[str, str]], task_cmd_template: str,
        *, execute: bool = False, runner: Callable[..., Any] = subprocess.run) -> dict[str, Any]:
    """DRY-RUN by default: print the planned matrix and spawn NOTHING. With
    `execute=True` run each cell's command (`{model}`/`{effort}` placeholders),
    score pass = rc == 0 (deterministic, no LLM judge), write the value table to
    `.harness/state/bakeoff/<utc-ts>.json` and print it ranked plus a
    `recommended:` line. Never writes routing config (phase-2 boundary)."""
    root = Path(root)
    # SPEC-170 rule 6 (audit dacbdc8 F3): the one spawn door with a FREE-FORM
    # role string. Guards the dry-run too — a bogus role refuses before a matrix
    # is ever printed. Tmp-root scenarios pass via rule 7.
    enforce_spawn(root, role)
    planned = [{**cell, "cmd": _cell_cmd(task_cmd_template, cell)} for cell in cells]
    if not execute:
        print(f"bake-off dry-run role={role}: {len(planned)} cell(s), nothing spawned")
        for p in planned:
            print(f"  {p['model']}:{p['effort']}  $ {p['cmd']}")
        print("pass --execute to run the matrix (operator-only: real token spend)")
        return {"role": role, "dryRun": True, "cells": cells}
    results: list[dict[str, Any]] = []
    for p in planned:
        started = time.monotonic()
        try:
            # No shell (security sink): Windows CreateProcess parses the raw
            # string natively; POSIX gets a shlex argv. The command must be a
            # real executable, not a shell builtin.
            argv = p["cmd"] if os.name == "nt" else shlex.split(p["cmd"])
            proc = runner(argv, capture_output=True, text=True, timeout=CELL_TIMEOUT_S)
            passed = getattr(proc, "returncode", 1) == 0
            stdout = getattr(proc, "stdout", None)
        except (subprocess.TimeoutExpired, OSError):
            passed, stdout = False, None
        results.append({"model": p["model"], "effort": p["effort"], "pass": passed,
                        "durationS": round(time.monotonic() - started, 3),
                        "tokens": _tokens_from_stdout(stdout)})
    report = {"role": role, "cells": results}
    # ponytail: second-resolution filename; collides only when two executes hit
    # the same root in the same second — operator bake-offs take minutes.
    path = root / BAKEOFF_DIR_REL / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + ".json")
    write_json(path, report)
    ranked = sorted(results, key=_rank_key)
    print(f"bake-off role={role}: report {path.relative_to(root).as_posix()}")
    _print_table(ranked)
    best = ranked[0]
    print(f"recommended: {best['model']}:{best['effort']} "
          "(observe-only; `compare` shows it beside live routing, "
          "`apply_recommendation(..., confirm=True)` writes it — never automatic)")
    return report


# -- phase 2 (RF.1): the measurement meets routing, owner-gated ---------------
#
# Phase 1 printed a `recommended:` line and stopped. That line is gone the moment
# the terminal scrolls, so a hand-tuned routing could silently drift away from
# the only empirical basis anyone ever measured for it. Phase 2 is exactly two
# things: SHOW the measured recommendation beside what routing actually resolves
# today (read-only), and let the owner APPLY it deliberately. Nothing here ever
# flips a role on its own -- `apply_recommendation` refuses without an explicit
# confirm, so no gate, scenario or scheduled path can reach a routing write.


def latest_report(root: Path | str, role: str) -> dict[str, Any] | None:
    """Newest bake-off report for ``role``, or None if none was ever run.

    Report filenames are UTC second-stamps, so a filename sort IS chronological
    — no mtime read (OneDrive rewrites those; the SPEC-137 fingerprint design
    made the same call)."""
    d = Path(root) / BAKEOFF_DIR_REL
    if not d.is_dir():
        return None
    for path in sorted(d.glob("*.json"), reverse=True):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — a torn/hand-edited report is not fatal
            continue
        if isinstance(report, dict) and report.get("role") == role:
            report["reportPath"] = path.as_posix()
            report["at"] = path.stem
            return report
    return None


def recommendation(root: Path | str, role: str) -> dict[str, Any] | None:
    """The best-ranked cell of the newest report — or None.

    None when nothing was measured AND when the best cell did not PASS: a matrix
    where every combo failed recommends nothing. `_rank_key` sorts pass-first, so
    a failing best means no cell passed at all, and recommending the
    least-bad failure would be worse than admitting there is no answer."""
    report = latest_report(root, role)
    if not report:
        return None
    cells = [c for c in (report.get("cells") or []) if isinstance(c, dict)]
    if not cells:
        return None
    best = sorted(cells, key=_rank_key)[0]
    if not best.get("pass"):
        return None
    return {"model": best["model"], "effort": best["effort"],
            "at": report.get("at"), "reportPath": report.get("reportPath")}


def compare(root: Path | str, role: str, *, executor: str | None = None,
            target: str | None = None) -> dict[str, Any]:
    """The phase-2 SURFACE: measured recommendation beside live routing. Reads
    both, writes nothing. `agrees` is None when there is no measurement — an
    unmeasured role is not agreement, and must never read as one."""
    from . import model_routing  # local: avoids a module-level cycle via cli paths

    rec = recommendation(root, role)
    resolved = model_routing.resolve_role(Path(root), role, executor, target)
    primary = resolved.get("primary") or {}
    current = {"model": primary.get("card") or primary.get("model"),
               "effort": primary.get("effort")}
    agrees = None if rec is None else (rec["model"] == current["model"]
                                       and rec["effort"] == current["effort"])
    return {"role": role, "recommended": rec, "current": current,
            "profile": resolved.get("profile"), "source": resolved.get("source"),
            "agrees": agrees, "reportAt": (rec or {}).get("at")}


def apply_recommendation(root: Path | str, profile: str, role: str, *,
                         confirm: bool = False) -> dict[str, Any]:
    """OWNER-GATED write of the measured recommendation into a NAMED profile.

    Refuses without ``confirm=True`` — the RF.1 rule is "never auto-flip a role's
    routing", and a default-on write would make every caller a flipper. Refuses
    when nothing was measured (there is no recommendation to apply) and when the
    role already matches (a no-op write would churn the config and its history).

    CARRIES THE EXISTING FALLBACKS THROUGH: `model_routing.set_role` takes the
    whole role definition, so passing `[]` here would silently DELETE the role's
    fallback chain while appearing to change only the primary."""
    from . import model_routing

    if not confirm:
        raise HarnessError("applying a bake-off recommendation is owner-gated: "
                           "pass confirm=True (RF.1 — routing is never auto-flipped)")
    rec = recommendation(root, role)
    if rec is None:
        raise HarnessError(f"no passing bake-off recommendation on record for role {role!r} "
                           "— run the matrix with --execute first")
    resolved = model_routing.resolve_role(Path(root), role)
    fallbacks = list(resolved.get("fallbacks") or [])  # never dropped by a primary edit
    primary = dict(resolved.get("primary") or {})
    if primary.get("card") == rec["model"] and primary.get("effort") == rec["effort"]:
        return {"role": role, "profile": profile, "changed": False,
                "reason": "routing already matches the recommendation"}
    primary["card"], primary["effort"] = rec["model"], rec["effort"]
    out = model_routing.set_role(Path(root), profile, role, primary, fallbacks)
    return {**out, "changed": True, "appliedFrom": rec.get("reportPath"), "measuredAt": rec.get("at")}


# -- deterministic self-check (no LLM, tmp root, stub runner) -----------------

def _self_check() -> None:
    import tempfile

    cards = [{"id": "good", "reasoning": ["low", "high"]}, {"id": "bad", "reasoning": ["high"]}]
    cells = plan_cells("planner", ["good", "bad"], ["high"], cards)
    assert cells == [{"model": "good", "effort": "high"}, {"model": "bad", "effort": "high"}]
    assert len(plan_cells("r", ["good"], ["low", "high"], cards)) == 2  # full matrix
    for models, efforts in ((["nope"], ["high"]), (["bad"], ["low"]), (["good"], ["bogus"])):
        try:
            plan_cells("planner", models, efforts, cards)
            raise AssertionError("invalid cell must raise")
        except HarnessError:
            pass

    root = Path(tempfile.mkdtemp())

    def _never(*_a, **_k):
        raise AssertionError("dry-run must not spawn")

    out = run(root, "planner", cells, "task {model} {effort}", runner=_never)
    assert out["dryRun"] is True and not (root / BAKEOFF_DIR_REL).exists()

    class _P:
        def __init__(self, rc: int, stdout: str = "") -> None:
            self.returncode, self.stdout = rc, stdout

    rep = run(root, "planner", cells, "task {model}", execute=True,
              runner=lambda cmd, **_k: _P(0, '{"tokens": 7}') if "good" in cmd else _P(1))
    by = {c["model"]: c for c in rep["cells"]}
    assert by["good"]["pass"] is True and by["good"]["tokens"] == 7
    assert by["bad"]["pass"] is False and by["bad"]["tokens"] is None
    assert len(list((root / BAKEOFF_DIR_REL).glob("*.json"))) == 1
    assert sorted(rep["cells"], key=_rank_key)[0]["model"] == "good"
    print("role_calibration self-check: ok")


if __name__ == "__main__":
    _self_check()
