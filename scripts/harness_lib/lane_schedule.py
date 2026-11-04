"""Schedule one detached executor lane for a future local time."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Callable

from harness_lib import processes
from harness_lib.common import HarnessError, ROOT, gate_holding, now, read_json, write_json


def _executors(root: Path) -> dict:
    doc = read_json(root / ".harness" / "routing" / "executors.json", {}) or {}
    return doc.get("executors", {})


def _instruction(path_text: str) -> tuple[Path, str]:
    path = Path(path_text).expanduser().resolve()
    try:
        return path, path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HarnessError(f"instruction file is not readable: {path}") from exc


def _fire_at(at: str, date_text: str | None) -> dt.datetime:
    current = dt.datetime.now().astimezone()
    parsed_time = None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            parsed_time = dt.datetime.strptime(at, fmt).time()
            break
        except ValueError:
            pass
    if parsed_time is None:
        raise HarnessError("--at must be HH:MM (HH:MM:SS is accepted for probes)")
    try:
        day = dt.date.fromisoformat(date_text) if date_text else current.date()
    except ValueError as exc:
        raise HarnessError("--date must be YYYY-MM-DD") from exc
    fire = dt.datetime.combine(day, parsed_time, tzinfo=current.tzinfo)
    if fire <= current:
        raise HarnessError("scheduled fire time must be in the future")
    if fire - current > dt.timedelta(hours=24):
        raise HarnessError("scheduled fire time must be within 24h (security audit 2026-07-29)")
    return fire


def _sweep_old(runs: Path, keep: int = 20) -> None:
    # keep=20 not 5: a fired lane's marker holds vendorPid/vendorLog — sweeping it
    # while the vendor still runs orphans the record (security audit 2026-07-29).
    try:
        markers = sorted(runs.glob("lane-*.marker"))
        for marker in markers[:-keep]:
            base = marker.with_suffix("")
            marker.unlink(missing_ok=True)
            base.with_suffix(".log").unlink(missing_ok=True)
            base.with_suffix(".prompt.md").unlink(missing_ok=True)
            base.with_suffix(".instruction.md").unlink(missing_ok=True)
            base.with_name(base.name + "-vendor.log").unlink(missing_ok=True)
    except OSError:
        pass


def _write_marker(path: Path, data: dict) -> None:
    data.setdefault("finishedAt", now())
    write_json(path, data)
    _sweep_old(path.parent)


def _apply_pins(argv: list[str], template: list, model: str | None, effort: str | None) -> list[str]:
    """Apply explicit pins only where the declared executor template exposes them."""
    wanted = {"model": model, "effort": effort, "reasoning": effort}
    if not model and not effort:
        return argv
    out = list(argv)
    index = 0
    applied: set[str] = set()
    for raw in template:
        token = str(raw)
        if token == "{mcpConfig}":
            continue
        for key, value in wanted.items():
            placeholder = "{" + key + "}"
            if value and placeholder in token:
                out[index] = token.replace(placeholder, value)
                applied.add("effort" if key == "reasoning" else key)
        index += 1
    missing = [name for name, value in (("model", model), ("effort", effort))
               if value and name not in applied]
    if missing:
        raise HarnessError(
            "executor command template does not expose override(s): " + ", ".join(missing)
        )
    return out


def _run_worker(
    args: argparse.Namespace,
    spawn_command: Callable[[str, str | None], tuple[list[str], str, str, dict[str, str]]],
) -> int:
    marker = Path(args.marker)
    try:
        fire = dt.datetime.fromisoformat(args.fire_at)
        if fire.tzinfo is None:
            raise ValueError("--fire-at must include a timezone")
        while True:
            remaining = (fire - dt.datetime.now().astimezone()).total_seconds()
            if remaining <= 0:
                break
            time.sleep(min(remaining, 30))

        if gate_holding(ROOT):
            _write_marker(marker, {"status": "aborted", "reason": "gate-hold"})
            return 0

        guards = {
            "executor": "valid",
            "instructionFile": "readable",
            "gateHold": "clear",
        }
        if args.probe:
            # Probe mode never touches canonical vendor-fuel state — the gate runs
            # this path via vf_bg_tooling (security audit 2026-07-29).
            guards["fuelProbe"] = "skipped (probe mode)"
            _write_marker(marker, {"status": "probe-pass", "guards": guards})
            return 0

        fuel = processes.run_quiet(
            [sys.executable, str(ROOT / "scripts" / "harness.py"), "fuel", "probe"],
            timeout=60,
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=os.environ.copy(),
        )
        print(f"fuel probe rc={fuel.returncode}")
        if fuel.stdout:
            print(fuel.stdout, end="" if fuel.stdout.endswith("\n") else "\n")
        if fuel.stderr:
            print(fuel.stderr, end="" if fuel.stderr.endswith("\n") else "\n", file=sys.stderr)
        guards["fuelProbeExitCode"] = fuel.returncode

        instruction_path, instruction = _instruction(args.instruction_file)
        print(f"instruction file: {instruction_path}")
        print(instruction)
        # prompt_file, NEVER inline argv: a .CMD/.BAT argv0 makes cmd.exe re-parse
        # the command line, so quotes/& in the instruction escape the vendor sandbox
        # (proven by PoC, security audit 2026-07-29; mirrors route_dispatcher).
        argv, _profile, executor, spawn_env = spawn_command(
            instruction, args.executor, prompt_file=marker.with_suffix(".prompt.md")
        )
        executor_doc = _executors(ROOT)[executor]
        argv = _apply_pins(
            argv,
            executor_doc.get("commandTemplate", []),
            args.model,
            args.effort,
        )
        env = processes.filter_spawn_env(
            os.environ,
            spawn_env,
            keep_list=executor_doc.get("envKeepList", ()),
        )
        vendor_log = marker.with_suffix("").with_name(marker.stem + "-vendor.log")
        pid = processes.launch_detached(argv, log_path=vendor_log, cwd=ROOT, env=env)
        _write_marker(
            marker,
            {
                "status": "fired",
                "vendorPid": pid,
                "vendorLog": vendor_log.relative_to(ROOT).as_posix(),
                "firedAt": now(),
            },
        )
        return 0
    except BaseException as exc:  # marker is the detached worker's only completion channel
        try:
            _write_marker(marker, {"status": "error", "error": str(exc)})
        except OSError as marker_exc:
            print(f"lane worker marker error: {marker_exc}", file=sys.stderr)
        return 1


def _launch(args: argparse.Namespace, fire: dt.datetime) -> dict[str, object]:
    runs = ROOT / ".harness" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    _sweep_old(runs)
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    log = runs / f"lane-{ts}.log"
    marker = runs / f"lane-{ts}.marker"
    # Snapshot the instruction NOW: what fires is what was reviewed at schedule
    # time, not whatever the file mutates into meanwhile (security audit 2026-07-29).
    _path, text = _instruction(args.instruction_file)
    snapshot = runs / f"lane-{ts}.instruction.md"
    snapshot.write_text(text, encoding="utf-8")
    worker = [
        sys.executable,
        str(ROOT / "scripts" / "harness.py"),
        "lane",
        "schedule",
        "--run",
        "--marker",
        str(marker),
        "--fire-at",
        fire.isoformat(),
        "--executor",
        args.executor,
        "--instruction-file",
        str(snapshot),
    ]
    if args.model:
        worker.extend(["--model", args.model])
    if args.effort:
        worker.extend(["--effort", args.effort])
    if args.probe:
        worker.append("--probe")
    pid = processes.launch_detached(worker, log_path=log, cwd=ROOT)
    return {
        "status": "scheduled",
        "pid": pid,
        "fireAt": fire.isoformat(),
        "log": log.relative_to(ROOT).as_posix(),
        "marker": marker.relative_to(ROOT).as_posix(),
    }


def cmd_lane(args: argparse.Namespace) -> None:
    spawn_command = args.spawn_command
    if args.run:
        if not args.marker or not args.fire_at:
            raise HarnessError("lane schedule worker requires --marker and --fire-at")
        marker_path = Path(args.marker).expanduser().resolve()
        if not marker_path.is_relative_to(ROOT / ".harness" / "runs"):
            raise HarnessError("--marker must live under .harness/runs/")
        args.marker = str(marker_path)
        raise SystemExit(_run_worker(args, spawn_command))
    if not args.at:
        raise HarnessError("lane schedule requires --at HH:MM")
    executors = _executors(ROOT)
    if args.executor not in executors:
        raise HarnessError(
            f"unknown executor {args.executor!r}; available: {', '.join(sorted(executors))}"
        )
    _instruction(args.instruction_file)
    fire = _fire_at(args.at, args.date)
    print(json.dumps(_launch(args, fire), ensure_ascii=False))
