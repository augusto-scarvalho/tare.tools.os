"""Run one acceptance scenario in the foreground or as a detached worker."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from harness_lib import processes
from harness_lib.common import HarnessError, ROOT, now, write_json

# Bounded like every other harness spawn (processes.run_quiet demands it). 900s
# matches the panel budget pw_ui_smoke already runs under — the slowest scenario
# in the tree finishes well inside it.
SCENARIO_TIMEOUT = 900


def _available(root: Path) -> list[str]:
    scenarios = root / "testing" / "scenarios"
    return sorted(p.stem for p in scenarios.glob("*.py") if not p.stem.startswith("_"))


def _path(root: Path, name: str) -> Path | None:
    if name not in _available(root):
        return None
    return root / "testing" / "scenarios" / f"{name}.py"


def _sweep_old(runs: Path, keep: int = 20) -> None:
    try:
        markers = sorted(runs.glob("scenario-*.marker"))
        for marker in markers[:-keep]:
            marker.with_suffix(".log").unlink(missing_ok=True)
            marker.unlink(missing_ok=True)
    except OSError:
        pass


def _worker(root: Path, name: str, marker: str | None) -> int:
    rc = 1
    data: dict[str, object]
    try:
        path = _path(root, name)
        if path is None:
            rc = 2
            data = {
                "scenario": name,
                "exitCode": rc,
                "status": "error",
                "error": "unknown scenario",
                "finishedAt": now(),
            }
        else:
            rc = processes.run_quiet(
                [sys.executable, str(path)], timeout=SCENARIO_TIMEOUT, cwd=root
            ).returncode
            data = {
                "scenario": name,
                "exitCode": rc,
                "status": "pass" if rc == 0 else "fail",
                "finishedAt": now(),
            }
    except BaseException as exc:  # detached completion must survive scenario failures
        data = {
            "scenario": name,
            "exitCode": 1,
            "status": "error",
            "error": str(exc),
            "finishedAt": now(),
        }
        rc = 1
    if marker:
        try:
            marker_path = Path(marker)
            write_json(marker_path, data)
            _sweep_old(marker_path.parent)
        except OSError as exc:
            print(f"scenario worker marker error: {exc}", file=sys.stderr)
            return 1
    return rc


def _launch(root: Path, name: str) -> dict[str, object]:
    runs = root / ".harness" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    _sweep_old(runs)
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    log = runs / f"scenario-{name}-{ts}.log"
    marker = runs / f"scenario-{name}-{ts}.marker"
    worker = [
        sys.executable,
        str(root / "scripts" / "harness.py"),
        "scenario",
        name,
        "--run",
        "--marker",
        str(marker),
    ]
    pid = processes.launch_detached(worker, log_path=log, cwd=root)
    return {
        "status": "launched",
        "pid": pid,
        "log": log.relative_to(root).as_posix(),
        "marker": marker.relative_to(root).as_posix(),
    }


def cmd_scenario(args: argparse.Namespace) -> None:
    if getattr(args, "run", False):
        if not getattr(args, "marker", None):
            raise HarnessError("scenario worker requires --marker")
        marker = Path(args.marker).expanduser().resolve()
        if not marker.is_relative_to(ROOT / ".harness" / "runs"):
            raise HarnessError("--marker must live under .harness/runs/")
        raise SystemExit(_worker(ROOT, args.name, str(marker)))
    path = _path(ROOT, args.name)
    if path is None:
        print("Available scenarios: " + ", ".join(_available(ROOT)))
        raise SystemExit(2)
    if args.detach:
        print(json.dumps(_launch(ROOT, args.name), ensure_ascii=False))
        return
    raise SystemExit(
        processes.run_quiet(
            [sys.executable, str(path)], timeout=SCENARIO_TIMEOUT, cwd=ROOT
        ).returncode
    )
