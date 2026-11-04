"""POSIX-suite runner over WSL: clone, run, RECORD — capability out of prose.

Row `the-posix-suite-is-runnable-via-wsl-but-`: this machine runs the POSIX
suite today (WSL2 + Ubuntu + python3) by cloning into the WSL filesystem —
NEVER over /mnt/c, so scenarios cannot touch the live Windows `.harness/` —
and its first real run found a P1 cross-platform defect. But the capability
lived in prose only; "we ran it" was already decaying into "someone ran it
once". This module makes it repeatable and DATED:

  run(root, scenarios=[...])  clone HEAD into ~/harness-posix (inside WSL),
                              run the named scenario subset (or the smoke
                              default), and record {at, sha, rc, results}
                              at .harness/runs/posix-suite.json
  last_run(root)              the record (None when never run)
  doctor_check(root)          WARN when WSL is available and the suite has
                              never run / HEAD moved > STALE_COMMITS past the
                              recorded sha; machine-probe is gated by
                              HARNESS_POSIX_PROBE (unset/1 = probe, 0 = skip)
                              so fixture roots stay deterministic

EXPLICIT NON-GOAL (from the row): this is the LINUX arm only. WSL has /proc,
so every darwin-discriminated branch stays unexercised — a green run here must
never read as "POSIX coverage"; the record says `arm: "linux-wsl"` and the
doctor detail repeats it. darwin needs a real macOS host.

Self-check: run this file (hermetic; no WSL spawned). `--run [names...]`
executes for real; `--status` prints the record.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path, PureWindowsPath
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
from harness_lib import processes  # noqa: E402 — mediated spawns only (SEC.2 ratchet)

RECORD_REL = ".harness/runs/posix-suite.json"
PROBE_ENV = "HARNESS_POSIX_PROBE"  # "0" disables the machine probe (fixtures/gates)
STALE_COMMITS = 30  # doctor nags when HEAD moved this far past the last run
# smoke default: cheap, deterministic, exercises subprocess + fs + verdict paths
DEFAULT_SCENARIOS = ("cec_exit_codes",)
_WSL_TIMEOUT = 20 * 60


def wsl_available() -> bool:
    """Bounded probe; honors the kill-switch so fixture/gate runs never spawn wsl."""
    if os.environ.get(PROBE_ENV, "1") == "0":
        return False
    try:
        proc = processes.run_quiet(["wsl", "-e", "sh", "-c", "echo ok"],
                                   timeout=15, capture_output=True, text=True)
        return proc.returncode == 0 and "ok" in (proc.stdout or "")
    except Exception:  # noqa: BLE001 — no wsl / hung wsl = not available
        return False


def _win_to_wsl(path: Path) -> str:
    # PureWindowsPath parses the drive on ANY host OS — the plain Path version
    # raised IndexError on the linux CI runner (matrix run-2 finding: rh's
    # posix self-check red), where "C:/x" has no .drive.
    p = PureWindowsPath(str(path))
    drive = p.drive.rstrip(":").lower()
    rest = "/".join(p.parts[1:]) if p.drive else str(path).replace("\\", "/").lstrip("/")
    return f"/mnt/{drive}/{rest}"


def _head_sha(root: Path) -> str | None:
    try:
        proc = processes.run_quiet(["git", "rev-parse", "HEAD"], timeout=30,
                                   cwd=str(root), capture_output=True, text=True)
        return proc.stdout.strip() if proc.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def last_run(root: Path | str) -> dict[str, Any] | None:
    from harness_lib.common import read_json
    doc = read_json(Path(root) / RECORD_REL, None)
    return doc if isinstance(doc, dict) else None


def run(root: Path | str, scenarios: tuple[str, ...] | list[str] = DEFAULT_SCENARIOS) -> dict[str, Any]:
    """Clone HEAD into the WSL filesystem and run the named scenarios there.
    Records the outcome; raises only on a broken invocation, never mid-run."""
    from harness_lib.common import now, write_json
    root = Path(root)
    sha = _head_sha(root)
    names = list(scenarios) or list(DEFAULT_SCENARIOS)
    src = _win_to_wsl(root.resolve())
    runs = " && ".join(
        f"echo '## {n}' && python3 testing/scenarios/{n}.py; echo rc:{n}:$?" for n in names)
    script = (f"rm -rf ~/harness-posix && git clone -q {src} ~/harness-posix "
              f"&& cd ~/harness-posix && {runs}")
    proc = processes.run_quiet(["wsl", "-e", "bash", "-lc", script],
                               timeout=_WSL_TIMEOUT, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
    results: dict[str, int] = {}
    for line in (proc.stdout or "").splitlines():
        if line.startswith("rc:"):
            _, name, rc = line.split(":", 2)
            try:
                results[name] = int(rc.strip())
            except ValueError:
                results[name] = -1
    record = {"at": now(), "arm": "linux-wsl", "sha": sha,
              "cloneRc": proc.returncode, "scenarios": results,
              "pass": sum(1 for r in results.values() if r == 0),
              "fail": sum(1 for r in results.values() if r != 0),
              "tail": (proc.stdout or "").strip()[-400:]}
    target = root / RECORD_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    write_json(target, record)
    return record


def _commits_between(root: Path, old_sha: str) -> int | None:
    try:
        proc = processes.run_quiet(["git", "rev-list", "--count", f"{old_sha}..HEAD"],
                                   timeout=30, cwd=str(root), capture_output=True, text=True)
        return int(proc.stdout.strip()) if proc.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def doctor_check(root: Path | str) -> dict[str, str]:
    """WARN-only; linux arm only, and the detail says so. Fail-open everywhere."""
    try:
        root = Path(root)
        record = last_run(root)
        if not wsl_available():
            return {"id": "posix-suite", "status": "ok",
                    "detail": ("linux arm not probed (no WSL or probe disabled); "
                               "darwin has no answering environment")}
        if record is None or not record.get("sha"):
            return {"id": "posix-suite", "status": "warn",
                    "detail": ("WSL is available but the POSIX suite has NO recorded run - "
                               "python scripts/harness_lib/posix_suite.py --run "
                               "(linux arm only; darwin stays unexercised)")}
        behind = _commits_between(root, str(record["sha"]))
        if behind is not None and behind > STALE_COMMITS:
            return {"id": "posix-suite", "status": "warn",
                    "detail": (f"last POSIX (linux-wsl) run is {behind} commits behind HEAD "
                               f"(recorded {str(record.get('at'))[:10]}, fail={record.get('fail')}) - re-run")}
        return {"id": "posix-suite", "status": "ok",
                "detail": (f"linux-wsl arm exercised at {str(record.get('sha'))[:12]} "
                           f"({behind if behind is not None else '?'} commits behind, "
                           f"fail={record.get('fail')}); darwin still unexercised")}
    except Exception as exc:  # noqa: BLE001 — a doctor check never raises
        return {"id": "posix-suite", "status": "ok", "detail": f"unreadable: {exc!r}"}


def _demo() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        troot = Path(tmp)
        (troot / ".harness" / "runs").mkdir(parents=True)
        os.environ[PROBE_ENV] = "0"
        try:
            assert wsl_available() is False  # kill-switch wins on any machine
            dc = doctor_check(troot)
            assert dc["status"] == "ok" and "not probed" in dc["detail"], dc
            assert last_run(troot) is None

            # a recorded run flips the doctor arms without any WSL spawn
            (troot / RECORD_REL).write_text(json.dumps(
                {"at": "2026-07-29T00:00:00-03:00", "arm": "linux-wsl",
                 "sha": "a" * 40, "scenarios": {"x": 0}, "pass": 1, "fail": 0}),
                encoding="utf-8")
            assert last_run(troot)["arm"] == "linux-wsl"
        finally:
            os.environ.pop(PROBE_ENV, None)
    assert _win_to_wsl(Path("C:/projects/x")) == "/mnt/c/projects/x"
    with tempfile.TemporaryDirectory() as tmp2:
        # no git in the temp root: sha must read None, never rc-ignored stdout
        assert _head_sha(Path(tmp2)) is None
    parsed = {}
    for line in ("## a", "rc:a:0", "rc:b:3", "junk"):
        if line.startswith("rc:"):
            _, n, rc = line.split(":", 2)
            parsed[n] = int(rc)
    assert parsed == {"a": 0, "b": 3}
    print("posix_suite self-check: ok")


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--run" in argv:
        names = tuple(a for a in argv if not a.startswith("--")) or DEFAULT_SCENARIOS
        print(json.dumps(run(_SCRIPTS.parent, names), indent=2))
    elif "--status" in argv:
        print(json.dumps({"record": last_run(_SCRIPTS.parent),
                          "doctor": doctor_check(_SCRIPTS.parent)}, indent=2))
    else:
        _demo()
