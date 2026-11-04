"""Process-tree helpers shared by the harness runtime and validation gate.

The standard ``subprocess.run(..., timeout=...)`` cleanup semantics only cover
its direct child. Agent executors, fixture shells, and validation commands can
spawn grandchildren that keep pipes open or mutate workspaces after the parent
has been killed. These helpers isolate process groups/sessions and perform
best-effort descendant cleanup without adding third-party dependencies.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


# nt: a console-less parent makes CreateProcess allocate a NEW VISIBLE console for
# each console-subsystem child (the black terminal windows). CREATE_NO_WINDOW gives
# the child a HIDDEN console instead — process-group semantics and taskkill /T tree
# cleanup are unchanged (unlike DETACHED_PROCESS, which is mutually exclusive with it).
CREATE_NO_WINDOW = 0x08000000
# nt twin of setsid for children that must escape the parent's Job Object.
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
# The LOUD twin of CREATE_NO_WINDOW: a console of the child's OWN, on screen, for the
# one case where the whole point is that a human sees it and types into it. Mutually
# exclusive with CREATE_NO_WINDOW (and with DETACHED_PROCESS) — OR-ing the pair yields
# no window at all, which is exactly the failure this flag exists to avoid.
CREATE_NEW_CONSOLE = 0x00000010


def _stdio_redirected(kwargs: dict[str, Any]) -> bool:
    """True when stdout AND stderr are redirected away from the inherited console.

    capture_output / PIPE / DEVNULL / a file handle all redirect; None (the default)
    inherits the parent's stdio. We only hide when both output streams are redirected,
    so interactive flows (chat ``!``-escape, vendor login) that inherit a stream keep a
    real, visible console.
    """
    if kwargs.get("capture_output"):
        return True
    return kwargs.get("stdout") is not None and kwargs.get("stderr") is not None


def quiet_creationflags(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Return ``kwargs`` with CREATE_NO_WINDOW ORed in when nt + stdio redirected.

    Pure kwargs transform (no spawn) so it is unit-checkable on any platform.
    No-op on POSIX and for inherited-stdio (interactive) spawns.
    """
    if os.name == "nt" and _stdio_redirected(kwargs):
        kwargs = dict(kwargs)
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_NO_WINDOW
    return kwargs


def open_console(argv: list[str], *, cwd: Any = None) -> bool:
    """Start ``argv`` in a NEW VISIBLE console window; True iff it started.

    The loud pair of ``quiet_creationflags``. Every other spawn in this module hides
    the child's window; this one is worthless unless the owner can SEE and TYPE into
    it (the "I'll do it myself" acceptance door). Same care with flags, inverted:

      * the three stdio streams stay unset — passing any of them makes CPython set
        STARTF_USESTDHANDLES and hand the child the PARENT's pipes, so the visible
        console would paint nothing and swallow every keystroke;
      * CREATE_NO_WINDOW is never ORed in (mutually exclusive — see the constant).

    NEVER raises and never waits: a session with no console (a service/headless
    launcher), a missing binary, or an OS with no console-per-process notion all
    return False so the caller can degrade to handing the owner the command. The raw
    ``Popen`` lives HERE because spawn_ratchet blocks new subprocess sites elsewhere;
    nobody owns this child's lifecycle — it belongs to the human at the keyboard.
    ponytail: nt only. POSIX has no portable "open a terminal window" (the emulator is
    the user's choice and DISPLAY may not exist), and the printed command is the
    sanctioned floor there; add a terminal probe the day a POSIX owner asks for the button.
    """
    if os.name != "nt" or not argv:
        return False
    try:
        subprocess.Popen(list(argv), cwd=str(cwd) if cwd else None,
                         creationflags=CREATE_NEW_CONSOLE)
    except (OSError, ValueError):
        return False
    return True


def run_quiet(cmd, *, timeout, **kwargs):
    """``subprocess.run`` that hides the child console on nt when stdio is redirected.

    ``timeout`` is required so every quiet spawn stays bounded (the whole reason this
    module exists); pass it explicitly at the call site.
    """
    if "input" not in kwargs:  # esh-2: same stdin hygiene as run_process_tree_bounded
        kwargs.setdefault("stdin", subprocess.DEVNULL)
    if (kwargs.get("text") or kwargs.get("universal_newlines")) and "encoding" not in kwargs:
        kwargs.setdefault("encoding", "utf-8")  # encoding default: mojibake class (see encoding-audit)
        kwargs.setdefault("errors", "replace")
    return subprocess.run(cmd, timeout=timeout, **quiet_creationflags(kwargs))


def run_passthrough(cmd: list[str], *, env: dict[str, str] | None = None) -> int:
    """Transparent hand-off: run `cmd` INHERITING this process's stdio and return
    its exit code. No pipes (an interactive verb keeps its tty), no timeout (the
    child owns its own budget — this is a re-run of the caller's own command, not
    a bounded worker). The raw ``subprocess.run`` lives HERE because spawn_ratchet
    blocks new subprocess sites elsewhere.

    Used by common.enforce_declared_python to re-run the harness under the
    interpreter the operator DECLARED (runtime.python.envOverride), instead of
    failing and making the caller spend a whole turn re-issuing the command.
    ``os.execv`` was rejected: on Windows it returns the console to the prompt
    before the replacement finishes, which truncates captured output.

    ``Popen`` + ``wait()``, NOT ``subprocess.run``: the caller owns the lifecycle
    here, exactly like popen_interactive above. The static
    `subprocess-run-timeouts` check requires every ``subprocess.run`` to carry a
    timeout, and rightly so -- but this child is the caller's OWN command
    (`harness.py chat` can sit on a tty for hours), so there is no honest bound to
    give. Passing `timeout=None` would satisfy that check while defeating it."""
    return subprocess.Popen(cmd, env=env).wait()


def _parse_ps_ppid(text: str) -> dict[int, list[int]]:
    """PPID -> children map from ``ps -axo pid=,ppid=`` output. PURE, so the
    parsing half is testable on every OS (esh-6) while only the exec itself
    needs the CI darwin leg. A header row or a torn line is skipped, never fatal."""
    children: dict[int, list[int]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
    return children


def descendants_enumerable() -> bool:
    """True when this host can enumerate a process's descendants at all.

    nt walks the tree with ``taskkill /T``; POSIX needs procfs OR ``ps``. A False
    here means ``signal_process_tree`` degrades to a bare ``killpg``, which the
    SPEC-163 ``treeKillReach`` declaration must STATE rather than mask — so the
    manifest reads this instead of assuming."""
    if os.name == "nt":
        return True
    return Path("/proc").exists() or shutil.which("ps") is not None


def process_children_map() -> dict[int, list[int]]:
    """Return a best-effort PPID -> child PID map on Unix-like systems."""
    children: dict[int, list[int]] = {}
    if os.name == "nt":
        return children
    proc_dir = Path("/proc")
    if not proc_dir.exists():
        # darwin (and any POSIX without procfs): ASK ps rather than return empty.
        # The empty map silently made signal_process_tree a bare killpg there --
        # the SPEC-163 v2 gap `bounded:darwin`. D045: evolve darwin UP, never level
        # linux down. Fail-safe: any failure returns today's empty map, never worse.
        # ponytail: one ps exec; a libproc ctypes walker only if it ever measures hot.
        try:
            proc = run_quiet(["ps", "-axo", "pid=,ppid="], capture_output=True,
                             text=True, timeout=10)
        except Exception:
            return children
        return _parse_ps_ppid(proc.stdout or "") if proc.returncode == 0 else children
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = entry.joinpath("status").read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        ppid: int | None = None
        for line in status.splitlines():
            if line.startswith("PPid:"):
                try:
                    ppid = int(line.split()[1])
                except Exception:
                    ppid = None
                break
        if ppid is not None:
            children.setdefault(ppid, []).append(int(entry.name))
    return children


def process_descendants(pid: int) -> list[int]:
    """Return known descendants of ``pid`` using the current process snapshot."""
    children = process_children_map()
    out: list[int] = []
    stack = list(children.get(int(pid), []))
    while stack:
        child = stack.pop()
        if child in out:
            continue
        out.append(child)
        stack.extend(children.get(child, []))
    return out


def signal_process_tree(pid: int | None, sig: int) -> None:
    """Signal an isolated process group plus best-effort descendants."""
    if not pid:
        return
    target = int(pid)
    try:  # every tree kill leaves an attributed audit line (wv/w29/rt6 class)
        import inspect
        caller = inspect.stack(0)[1]
        _audit_kill("kill", target, f"{Path(caller.filename).name}:{caller.lineno}", sig=int(sig))
    except Exception:
        pass
    # rt6/exp21 POSIX teardown: snapshot the parentage tree BEFORE any signal.
    # os.killpg reaches target's own session, but a start_new_session child leads
    # its OWN session (killpg cannot reach it) -- only the descendant walk + os.kill
    # can. Killing target first reparents those children to init before the walk
    # runs, so they would survive. Capture-then-kill mirrors nt taskkill /T. (On nt
    # the walk is empty -- no /proc -- and taskkill /T already reaches the tree.)
    descendants = sorted(process_descendants(target), reverse=True)
    if os.name == "nt":
        # /proc is unavailable on Windows, so descendants are invisible to
        # process_descendants(); taskkill /T is the only stdlib-free way to
        # reach grandchildren. Windows has no graceful SIGTERM equivalent for
        # detached trees, so /F is used for both term and kill passes.
        # ponytail: taskkill /T; ctypes Toolhelp32 walker if taskkill availability ever matters
        try:
            run_quiet(["taskkill", "/PID", str(target), "/T", "/F"], capture_output=True, timeout=10)
        except Exception:
            pass
    else:
        try:
            os.killpg(os.getpgid(target), sig)
        except Exception:
            pass
        for child in descendants:
            try:
                os.kill(child, sig)
            except Exception:
                pass
    try:
        os.kill(target, sig)
    except Exception:
        pass


def pid_alive(pid: int | None) -> bool:
    """Return True when ``pid`` is a live, non-zombie process.

    os.kill(pid, 0) reports zombies as alive; a zombie has already exited and
    only awaits reaping, so recovery/cancel loops treating it as alive can spin
    until their outer timeout.
    """
    if not pid:
        return False
    target = int(pid)
    if os.name == "nt":
        # os.kill(pid, 0) succeeds for a terminated child while any handle to
        # it stays open, so poll the real exit status instead.
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, target)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        stat = Path(f"/proc/{target}/stat").read_text(encoding="utf-8", errors="ignore")
        parts = stat.split()
        if len(parts) > 2 and parts[2] == "Z":
            return False
    except FileNotFoundError:
        # procfs present (linux): a missing pid entry means the process is gone.
        # procfs absent entirely (macOS/BSD have no /proc): this FileNotFoundError
        # means nothing about THIS pid -- fall through to the portable os.kill probe.
        if Path("/proc").is_dir():
            return False
        # ponytail: os.kill can't see zombie state without /proc, so a zombie reads
        # alive here -- a smaller ceiling than the previous all-dead false-negative;
        # add a libproc zombie check only if a scenario ever needs it.
    except Exception:
        pass
    try:
        os.kill(target, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _posix_creation_time_no_procfs(pid: int) -> float | None:
    """macOS/BSD creation time when there is no /proc to read.

    psutil first (clean, correct, no ctypes) if the dep happens to already be
    installed; otherwise a direct libproc(3) proc_pidinfo(..., PROC_PIDTBSDINFO)
    call reads pbi_start_tvsec/usec straight from the kernel. ANY failure here
    (missing lib, bad struct, pid gone, permission) degrades to None --
    conservative identity-unknown, which the kill-guard already tolerates;
    never a crash, never a garbage value.
    """
    try:
        import psutil  # noqa: F401  (only used if already an installed dep)
    except Exception:  # never crash: a broken/partial psutil degrades to the libproc path
        psutil = None
    if psutil is not None:
        try:
            return float(psutil.Process(pid).create_time())
        except Exception:
            return None
    try:
        import ctypes
        import ctypes.util

        MAXCOMLEN = 16
        PROC_PIDTBSDINFO = 3

        class _ProcBsdInfo(ctypes.Structure):
            # <sys/proc_info.h> struct proc_bsdinfo -- the two start-time fields
            # sit near the end, so every field ahead of them must line up exactly.
            _fields_ = [
                ("pbi_flags", ctypes.c_uint32),
                ("pbi_status", ctypes.c_uint32),
                ("pbi_xstatus", ctypes.c_uint32),
                ("pbi_pid", ctypes.c_uint32),
                ("pbi_ppid", ctypes.c_uint32),
                ("pbi_uid", ctypes.c_uint32),
                ("pbi_gid", ctypes.c_uint32),
                ("pbi_ruid", ctypes.c_uint32),
                ("pbi_rgid", ctypes.c_uint32),
                ("pbi_svuid", ctypes.c_uint32),
                ("pbi_svgid", ctypes.c_uint32),
                ("rfu_1", ctypes.c_uint32),
                ("pbi_comm", ctypes.c_char * MAXCOMLEN),
                ("pbi_name", ctypes.c_char * (2 * MAXCOMLEN)),
                ("pbi_nfiles", ctypes.c_uint32),
                ("pbi_pgid", ctypes.c_uint32),
                ("pbi_pjobc", ctypes.c_uint32),
                ("e_tdev", ctypes.c_uint32),
                ("e_tpgid", ctypes.c_uint32),
                ("pbi_nice", ctypes.c_int32),
                ("pbi_start_tvsec", ctypes.c_uint64),
                ("pbi_start_tvusec", ctypes.c_uint64),
            ]

        lib = ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib"
        libproc = ctypes.CDLL(lib)
        libproc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                          ctypes.c_void_p, ctypes.c_int]
        libproc.proc_pidinfo.restype = ctypes.c_int
        info = _ProcBsdInfo()
        n = libproc.proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, ctypes.byref(info), ctypes.sizeof(info))
        if n != ctypes.sizeof(info) or info.pbi_start_tvsec == 0:
            return None
        return info.pbi_start_tvsec + info.pbi_start_tvusec / 1_000_000.0
    except Exception:
        return None


def pid_creation_time(pid: int | None) -> float | None:
    """Epoch seconds when ``pid`` was created, or None when undeterminable.

    The identity half of any kill-by-recorded-pid: a PID number alone names
    whichever process currently wears it — Windows re-issues a freed PID within
    ~100 short-lived spawns under gate churn (measured 2026-07-28), so cleanup
    must compare creation time against when the record was written."""
    if not pid:
        return None
    target = int(pid)
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = k32.OpenProcess(0x1000, False, target)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            times = (wintypes.FILETIME * 4)()
            if not k32.GetProcessTimes(ctypes.c_void_p(handle), *(ctypes.byref(t) for t in times)):
                return None
            filetime = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            return filetime / 10_000_000 - 11644473600  # 100ns since 1601 -> epoch
        finally:
            k32.CloseHandle(ctypes.c_void_p(handle))
    try:  # linux: /proc/<pid>/stat gives exact start ticks.
        stat = Path(f"/proc/{target}/stat").read_text(encoding="utf-8", errors="ignore")
        start_ticks = int(stat.rsplit(")", 1)[1].split()[19])
        uptime = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
        return time.time() - uptime + start_ticks / os.sysconf("SC_CLK_TCK")
    except FileNotFoundError:
        # procfs present (linux) but this pid's entry is gone: no timestamp to read.
        # procfs absent entirely (macOS/BSD): fall through to the real macOS impl below.
        if Path("/proc").is_dir():
            return None
        return _posix_creation_time_no_procfs(target)
    except Exception:
        return None


def _audit_kill(event: str, pid: int, site: str, **extra: Any) -> None:
    """Best-effort JSONL line per kill decision, so the next mystery rc-1 victim
    self-attributes instead of costing another two-lane forensic hunt."""
    try:
        import json
        # __file__ inside a gate worker copy roots the ledger in the COPY, which is
        # disposed with every shard — 5 days of in-copy kill decisions left ZERO lines
        # (2026-07-29..08-03 blackout). HARNESS_REAL_ROOT re-roots to the live tree.
        real = os.environ.get("HARNESS_REAL_ROOT")
        base = Path(real) if real else Path(__file__).resolve().parents[2]
        path = base / ".harness" / "runs" / "kill-audit.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": time.time(), "event": event, "pid": int(pid), "site": site, **extra}
        if os.environ.get("HARNESS_SCENARIO_ISOLATED") == "1":
            row["iso"] = True  # in-scenario synthetic kill — analysis splits these from live cleanup
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    except Exception:
        pass


def filter_stale_records(records: "dict[int, float]", site: str, slack: float = 2.0) -> list[int]:
    """Split recorded PIDs into kill-eligible vs identity-mismatched.

    A record names the process that HELD the pid when the record was written; a
    process CREATED after that moment wears a re-issued number and is never a
    cleanup target (the wv/w29/rt6 innocent-kill class, 2026-07-28: taskkill
    /T /F stamps exit 1 with empty stderr on whole healthy trees). Unknown
    creation time keeps the old kill-by-number behavior but leaves an
    identity-unknown audit line — the leak class is the lesser evil there."""
    eligible: list[int] = []
    for pid, recorded_at in sorted(records.items(), reverse=True):
        if not pid_alive(pid):
            continue
        created = pid_creation_time(pid)
        if created is not None and created > float(recorded_at) + slack:
            _audit_kill("skip-stale", pid, site, created=created, recordedAt=float(recorded_at))
            continue
        if created is None:
            _audit_kill("identity-unknown", pid, site, recordedAt=float(recorded_at))
        eligible.append(pid)
    return eligible


def _record_epoch(value: Any, fallback: Path) -> float:
    """startedAt ISO (written at spawn) -> epoch; the record file's mtime as
    fallback. ponytail: mtime moves on rewrites (heartbeats), which can re-bless
    a reused pid; startedAt-first is why that window stays narrow."""
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value)).timestamp()
    except Exception:
        try:
            return fallback.stat().st_mtime
        except OSError:
            return time.time()


def collect_pid_records(workflow_dir: Path) -> "dict[int, float]":
    """pid -> epoch its record was written, from a workflow dir's runtime state
    (the layout async_runtime persists: lock/pid files, group.json, tasks/)."""
    import json
    records: "dict[int, float]" = {}

    def add(value: Any, recorded_at: float) -> None:
        try:
            pid = int(value)
        except Exception:
            return
        if pid > 0:
            records[pid] = recorded_at

    for rel in ("workflow.lock", "async/supervisor.pid"):
        path = workflow_dir / rel
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
            data = json.loads(text) if text.startswith("{") else {}
            if data:
                add(data.get("pid"), _record_epoch(data.get("startedAt"), path))
            elif text:
                add(text.splitlines()[0], _record_epoch(None, path))
        except Exception:
            pass
    for rel, key in (("async/group.json", "supervisorPid"),):
        path = workflow_dir / rel
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore")) or {}
        except Exception:
            data = {}
        if isinstance(data, dict):
            add(data.get(key), _record_epoch(data.get("startedAt"), path))
    tasks_dir = workflow_dir / "async" / "tasks"
    if tasks_dir.exists():
        for task_path in tasks_dir.glob("*.json"):
            try:
                task = json.loads(task_path.read_text(encoding="utf-8", errors="ignore")) or {}
            except Exception:
                task = {}
            if isinstance(task, dict):
                add(task.get("pid"), _record_epoch(task.get("startedAt"), task_path))
    return records


def cleanup_recorded_tree(workflow_dir: Path, site: str) -> None:
    """Identity-guarded replacement for kill-every-recorded-pid fixture cleanup:
    term pass, short grace, kill pass — each pass re-collects and re-filters."""
    for sig in (signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)):
        pids = filter_stale_records(collect_pid_records(workflow_dir), site)
        if not pids:
            return
        for pid in pids:
            signal_process_tree(pid, sig)
        time.sleep(0.2)


def signal_pid_group(pid: int | None, sig: int) -> bool:
    """Signal ``pid`` and its process group without scanning for descendants.

    Returns True when at least one signal was delivered. Callers that need
    grandchild cleanup should use ``signal_process_tree`` instead.
    """
    if not pid:
        return False
    target = int(pid)
    sent = False
    if os.name != "nt":
        try:
            os.killpg(os.getpgid(target), sig)
            sent = True
        except Exception:
            pass
    try:
        os.kill(target, sig)
        sent = True
    except Exception:
        pass
    return sent


def decode_pipe(data: Any) -> str:
    """Decode stdout/stderr payloads from text or byte-mode subprocesses."""
    if isinstance(data, str):
        return data
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="ignore")
    return ""


def process_group_kwargs() -> dict[str, Any]:
    """Return kwargs that isolate the child process group/session.

    On nt the child also gets CREATE_NO_WINDOW so a console-less harness parent
    (e.g. the detached workflow supervisor) does not pop a visible console for it.
    """
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | CREATE_NO_WINDOW}
    return {"start_new_session": True}


def popen_detached(cmd: list[str], *, stdin: Any = subprocess.DEVNULL,
                   stdout: Any = None, stderr: Any = None,
                   cwd: Any = None, env: dict[str, str] | None = None) -> subprocess.Popen:
    """Spawn a group-isolated child that explicitly escapes a parent Job Object."""
    kwargs = process_group_kwargs()
    if os.name == "nt":
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_BREAKAWAY_FROM_JOB
    try:
        return subprocess.Popen(cmd, stdin=stdin, stdout=stdout, stderr=stderr,
                                cwd=cwd, env=env, **kwargs)
    except OSError as exc:
        if os.name != "nt" or getattr(exc, "winerror", None) != 5:
            raise
        print("breakaway denied; child stays in the parent job (caged-but-killable)",
              file=sys.stderr)
        kwargs["creationflags"] &= ~CREATE_BREAKAWAY_FROM_JOB
        return subprocess.Popen(cmd, stdin=stdin, stdout=stdout, stderr=stderr,
                                cwd=cwd, env=env, **kwargs)


def launch_detached(cmd: list[str], *, log_path: Any, cwd: Any, env: dict[str, str] | None = None) -> int:
    """Fire-and-forget a child, wiring stdout+stderr to ``log_path``; return its pid."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_f = open(log_path, "wb")
    try:
        proc = popen_detached(cmd, stdin=subprocess.DEVNULL, stdout=log_f,
                              stderr=subprocess.STDOUT, cwd=str(cwd), env=env)
    finally:
        log_f.close()
    return proc.pid





def popen_interactive(cmd: list[str], *, cwd: Any, env: dict[str, str] | None = None,
                      stderr: Any = None) -> subprocess.Popen:
    """Long-lived INTERACTIVE child on line-buffered UTF-8 text pipes (stdin and
    stdout piped) — the shape a JSON-RPC/stdio engine needs (codex app-server;
    any future vendor transport). stderr defaults to DEVNULL (pass a pipe to
    drain it yourself). The raw ``subprocess.Popen`` lives HERE because
    spawn_ratchet blocks new subprocess sites elsewhere; the CALLER owns the
    lifecycle (these children die with the parent's pipes, not with a timeout)."""
    return subprocess.Popen(
        cmd, cwd=str(cwd), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL if stderr is None else stderr,
        text=True, encoding="utf-8", errors="replace", bufsize=1)


def popen_with_tty(cmd: list[str], *, cwd: Any, env: dict[str, str] | None = None,
                   size: tuple[int, int] = (40, 120)) -> tuple[subprocess.Popen, int]:
    """POSIX: run ``cmd`` attached to a fresh pty; return ``(proc, master_fd)``.

    A TUI that only paints when stdout is a terminal cannot be driven through the
    pipes ``popen_interactive`` hands out, and the stdlib has no Windows pty — see
    ``harness_lib/pty_drive.py``, which pairs this with pywinpty on nt. The raw
    ``subprocess.Popen`` lives HERE because spawn_ratchet blocks new subprocess
    sites elsewhere; the CALLER owns the lifecycle and MUST close the master fd.
    """
    if os.name == "nt":
        # Without this the caller gets a bare `No module named 'fcntl'` three frames
        # down. pty_drive.available() is the supported way to ask.
        raise NotImplementedError(
            "popen_with_tty is POSIX-only; nt drives a pty through pywinpty "
            "(see harness_lib/pty_drive.py)")
    import fcntl
    import pty
    import struct
    import termios

    master, slave = pty.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", size[0], size[1], 0, 0))
        # start_new_session: the child leads its own session, so the pty becomes its
        # controlling terminal and Ctrl-C in the operator's shell does not reach it.
        proc = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdin=slave, stdout=slave,
                                stderr=slave, start_new_session=True, close_fds=True)
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)  # the child holds its own dup; ours would fake a live writer
    return proc, master


def popen_stdio_bytes(cmd: list[str], **kwargs: Any) -> subprocess.Popen:
    """Long-lived child on BINARY stdio pipes — the shape Content-Length-framed
    JSON-RPC needs (LSP counts BYTES; text mode + universal newlines would corrupt
    the framing). Window-suppressed like every spawn here. The raw ``Popen`` lives
    HERE because spawn_ratchet blocks new subprocess sites elsewhere; the CALLER
    owns the lifecycle."""
    return subprocess.Popen(
        cmd, **quiet_creationflags({"stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
                                    "stderr": subprocess.DEVNULL, **kwargs}))


def _child_cpu_seconds_posix_baseline() -> float | None:
    try:
        import resource
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        return usage.ru_utime + usage.ru_stime
    except Exception:
        return None


def _win_job_set_limits(k32, job, limits: dict[str, Any]) -> None:
    """SPEC-148 P2: JOBOBJECT_EXTENDED_LIMIT_INFORMATION caps on the whole tree.
    Supported: activeProcesses (fork-bomb ceiling), jobMemoryBytes (commit cap),
    breakawayOk (explicit child breakaway), killOnClose (strict sandbox tree reaper).
    Raises on failure — a requested-but-unapplied cap must not pass silently."""
    import ctypes

    class _JobBasic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", ctypes.c_uint32),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_void_p),
                    ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32)]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in
                    ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                     "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _JobExt(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _JobBasic), ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    info = _JobExt()
    flags = 0
    if limits.get("activeProcesses"):
        flags |= 0x8  # JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        info.BasicLimitInformation.ActiveProcessLimit = int(limits["activeProcesses"])
    if limits.get("jobMemoryBytes"):
        flags |= 0x200  # JOB_OBJECT_LIMIT_JOB_MEMORY
        info.JobMemoryLimit = int(limits["jobMemoryBytes"])
    if limits.get("breakawayOk"):
        flags |= 0x800  # JOB_OBJECT_LIMIT_BREAKAWAY_OK
    if limits.get("killOnClose"):
        flags |= 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not flags:
        return
    info.BasicLimitInformation.LimitFlags = flags
    if not k32.SetInformationJobObject(ctypes.c_void_p(job), 9,  # JobObjectExtendedLimitInformation
                                       ctypes.byref(info), ctypes.sizeof(info)):
        raise RuntimeError(f"SetInformationJobObject failed for limits {limits}")


def _win_job_assign(proc: subprocess.Popen, limits: dict[str, Any] | None = None):
    """nt: put the child in a fresh Job Object so CPU accounting covers the
    WHOLE tree (venv python.exe is a launcher that respawns the interpreter;
    per-process GetProcessTimes sees ~0). One-time setup, no polling.

    Without ``limits`` this stays best-effort (accounting only). WITH limits
    (SPEC-148 sandbox spawns) any failure RAISES so the caller can kill the
    still-suspended child instead of running it uncapped."""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        job = k32.CreateJobObjectW(None, None)
        if not job:
            raise RuntimeError("CreateJobObjectW failed")
        if not k32.AssignProcessToJobObject(ctypes.c_void_p(job),
                                            ctypes.c_void_p(int(proc._handle))):  # type: ignore[attr-defined]
            k32.CloseHandle(ctypes.c_void_p(job))
            raise RuntimeError("AssignProcessToJobObject failed")
        if limits:
            try:
                _win_job_set_limits(k32, job, limits)
            except Exception:
                k32.CloseHandle(ctypes.c_void_p(job))
                raise
        return job
    except Exception:
        if limits:
            raise
        return None


def assign_job_to_pid(pid: int, limits: dict[str, Any] | None = None):
    """nt: cage a CREATE_SUSPENDED child (identified only by PID) in a fresh Job Object,
    then resume it — the by-PID twin of ``_win_job_assign`` for spawn seams that hold no
    Popen (the asyncio fan-out has only ``proc.pid``). Sequence: OpenProcess(pid) ->
    CreateJobObjectW -> AssignProcessToJobObject -> set_limits -> NtResumeProcess. Returns
    the job handle (caller keeps it and ``close_win_handle``s it after the child exits, the
    same lifetime the bounded seam gives its job); returns None off nt.

    Resume lives INSIDE (not in the async caller) so that seam carries no ctypes and the
    suspended->assign->resume anti-race stays atomic here; the child is resumed ONLY after
    the cap is applied, so it never runs uncapped. Fail-closed: WITH ``limits`` any
    OpenProcess/CreateJob/Assign/set_limits failure RAISES (mirrors ``_win_job_assign``), and
    a resume failure ALWAYS raises (even best-effort) — a suspended-but-unresumed child must
    be killed by the caller, never left hung. OpenProcess rights: PROCESS_SET_QUOTA |
    PROCESS_TERMINATE (assign) | PROCESS_SUSPEND_RESUME (resume) = 0x0901.
    # ponytail: built for CREATE_SUSPENDED children + limits (the live async seam); a
    # without-limits caller gets best-effort assign and must handle a None return itself."""
    if os.name != "nt":
        return None
    import ctypes
    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    hproc = k32.OpenProcess(0x0100 | 0x0001 | 0x0800, False, int(pid))
    if not hproc:
        if limits:
            raise RuntimeError(f"OpenProcess failed for pid {pid}")
        return None
    try:
        try:
            job = k32.CreateJobObjectW(None, None)
            if not job:
                raise RuntimeError("CreateJobObjectW failed")
            if not k32.AssignProcessToJobObject(ctypes.c_void_p(job), ctypes.c_void_p(hproc)):
                k32.CloseHandle(ctypes.c_void_p(job))
                raise RuntimeError("AssignProcessToJobObject failed")
            if limits:
                try:
                    _win_job_set_limits(k32, job, limits)
                except Exception:
                    k32.CloseHandle(ctypes.c_void_p(job))
                    raise
        except Exception:
            if limits:
                raise  # fail-closed: caller kills the still-suspended child
            return None  # best-effort (no caps): non-fatal create/assign failure
        # Cap is in place; resume the child. A resume failure ALWAYS raises so the caller
        # kills the suspended child instead of leaving it hung (never an uncapped run).
        status = ctypes.windll.ntdll.NtResumeProcess(ctypes.c_void_p(hproc))  # type: ignore[attr-defined]
        if status != 0:
            k32.CloseHandle(ctypes.c_void_p(job))
            raise OSError(f"NtResumeProcess failed: 0x{status & 0xFFFFFFFF:08X}")
        return job
    finally:
        k32.CloseHandle(ctypes.c_void_p(hproc))  # process handle done; only the job escapes


def create_win_job_for_handle(process_handle: int, limits: dict[str, Any]):
    """Cage a suspended Windows process handle before it can execute user code."""
    if os.name != "nt":
        raise RuntimeError("Windows Job Object backend unavailable")
    import ctypes
    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    job = k32.CreateJobObjectW(None, None)
    if not job:
        raise RuntimeError("CreateJobObjectW failed")
    try:
        if not k32.AssignProcessToJobObject(ctypes.c_void_p(job), ctypes.c_void_p(process_handle)):
            raise RuntimeError("AssignProcessToJobObject failed")
        _win_job_set_limits(k32, job, limits)
        return job
    except Exception:
        k32.CloseHandle(ctypes.c_void_p(job))
        raise


def close_win_handle(handle) -> None:
    """CloseHandle a raw win32 handle (e.g. the job from ``assign_job_to_pid``). No-op off
    nt or on a falsy handle, so callers can close unconditionally in a finally."""
    if os.name != "nt" or not handle:
        return
    import ctypes
    ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))  # type: ignore[attr-defined]


def terminate_win_job(handle, exit_code: int = 15) -> bool:
    """nt: kill EVERY member of a Job Object at once -- the caged process and every
    descendant it spawned, reached by kernel job inheritance, with no pid enumeration
    and no cross-process machinery (the jobs we create are UNNAMED, so only the handle
    owner can do this). SPEC-163 v4 invariant 11; the async timeout seam is the caller.

    No-op returning False off nt or on a falsy handle, so callers can sweep
    unconditionally in a ``finally`` -- the same contract as ``close_win_handle``.
    Already-dead members are a no-op, and terminating a job does NOT release its
    handle: the CALLER must still ``close_win_handle`` afterwards, and must sweep
    BEFORE closing (dropping the last handle destroys the job).

    ``exit_code`` defaults to 15 to match the pid ladder's SIGTERM, so a swept tree and
    a laddered pid carry the same code and kill forensics stay uniform."""
    if os.name != "nt" or not handle:
        return False
    import ctypes
    return bool(ctypes.windll.kernel32.TerminateJobObject(  # type: ignore[attr-defined]
        ctypes.c_void_p(handle), ctypes.c_uint(exit_code)))


_LIVE_JOB_HANDLES: list = []  # process-lifetime: job handles held so each cap outlives its
# cap_process_best_effort() call but dies with THIS process. No KILL_ON_JOB_CLOSE and never
# CloseHandle'd -> on our exit the OS destroys the (last-handle-closed) job and the detached
# child survives UNCAPPED. That IS the option-b ceiling (D044): the cap lasts only while the
# spawning parent lives.


def cap_process_best_effort(pid: int, limits: dict[str, Any]) -> bool:
    """Multi-OS resource cap on an already-RUNNING pid (D044 abstraction embryo, SECREV H1
    Fase 2b). Applies a fork-bomb/memory cap to a pid the caller no longer holds suspended,
    FAIL-OPEN (returns False on any failure, never raises — the detached path runs trusted
    infra; a failed cap must not break it; the opposite of the fail-closed async seam). The
    cap lasts while THIS process lives: the job handle is held in ``_LIVE_JOB_HANDLES`` with
    NO KILL_ON_JOB_CLOSE, so the detached child survives our exit but loses the cap.

    nt: OpenProcess(PROCESS_SET_QUOTA|PROCESS_TERMINATE) -> CreateJobObjectW -> Assign ->
        set_limits, then hold the handle. NO resume (the child already runs) — unlike
        ``assign_job_to_pid``, which is left untouched. RACE (declared ceiling): the child
        already ran between spawn and this cap, so grandchildren spawned in that window can
        escape the job — acceptable for trusted fail-open code.
    posix/off-nt: DECLARED HOOK — a per-OS cap belongs here (D045: each OS its BEST
        mechanism, never a weak common denominator — Linux cgroups v2, macOS setrlimit
        RLIMIT_NPROC/RLIMIT_AS), a follow-up (resource-cap-posix-parity). Returns False
        today (honest: no cap off nt), so the manifest never overstates."""
    if os.name != "nt" or not pid or not limits:
        return False
    try:
        import ctypes
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        # PROCESS_SET_QUOTA | PROCESS_TERMINATE — assign rights minus PROCESS_SUSPEND_RESUME
        # (we never resume the already-running child).
        hproc = k32.OpenProcess(0x0100 | 0x0001, False, int(pid))
        if not hproc:
            return False
        job = None
        try:
            job = k32.CreateJobObjectW(None, None)
            if not job:
                return False
            if not k32.AssignProcessToJobObject(ctypes.c_void_p(job), ctypes.c_void_p(hproc)):
                k32.CloseHandle(ctypes.c_void_p(job))
                return False
            _win_job_set_limits(k32, job, limits)  # raises on failure -> caught below
        except Exception:
            if job:
                k32.CloseHandle(ctypes.c_void_p(job))
            return False
        finally:
            k32.CloseHandle(ctypes.c_void_p(hproc))  # process handle done; the job escapes
        _LIVE_JOB_HANDLES.append(job)  # held for THIS process's lifetime; never closed
        return True
    except Exception:
        return False


def _win_job_cpu_seconds(job) -> float | None:
    """Total user+kernel CPU of every process (incl. exited) in the job."""
    if not job:
        return None
    try:
        import ctypes

        class _JobAcct(ctypes.Structure):
            _fields_ = [("TotalUserTime", ctypes.c_longlong),
                        ("TotalKernelTime", ctypes.c_longlong),
                        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                        ("TotalPageFaultCount", ctypes.c_uint32),
                        ("TotalProcesses", ctypes.c_uint32),
                        ("ActiveProcesses", ctypes.c_uint32),
                        ("TotalTerminatedProcesses", ctypes.c_uint32)]

        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        acct = _JobAcct()
        ok = k32.QueryInformationJobObject(ctypes.c_void_p(job), 1,  # JobObjectBasicAccountingInformation
                                           ctypes.byref(acct), ctypes.sizeof(acct), None)
        k32.CloseHandle(ctypes.c_void_p(job))
        if not ok:
            return None
        return round((acct.TotalUserTime + acct.TotalKernelTime) / 10_000_000, 3)
    except Exception:
        return None


def _reap_cpu_seconds(job, posix_baseline: float | None) -> float | None:
    """CPU (user+kernel) consumed by a reaped child tree. One query at reap —
    deterministic machine-impact signal with no polling (SPEC-111 R27/R28)."""
    if os.name == "nt":
        return _win_job_cpu_seconds(job)
    try:
        import resource
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        if posix_baseline is None:
            return None
        return round(max(usage.ru_utime + usage.ru_stime - posix_baseline, 0.0), 3)
    except Exception:
        return None


def run_process_tree_bounded(cmd: list[str], *, timeout: int,
                             job_limits: dict[str, Any] | None = None,
                             **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run a subprocess and clean its process tree if it times out.

    Raises ``subprocess.TimeoutExpired`` after cleanup, matching the previous
    harness runtime contract while ensuring descendants are terminated first.
    The returned CompletedProcess carries a best-effort ``cpu_seconds``
    attribute (None when the platform cannot report it) and a ``proc_applied``
    bool — True iff a Job Object was actually assigned to this spawn (SPEC-163
    M2 per-spawn honesty; always False off nt, where there is no Job Object).

    ``job_limits`` (SPEC-148 P2, nt only, ignored elsewhere): Job Object caps
    {"activeProcesses": N, "jobMemoryBytes": M} on the whole tree. When passed,
    a cap that cannot be applied kills the still-suspended child and raises —
    never an uncapped run that claimed to be capped.
    """
    kwargs.setdefault("text", True)
    if (kwargs.get("text") or kwargs.get("universal_newlines")) and "encoding" not in kwargs:
        kwargs.setdefault("encoding", "utf-8")  # encoding default: mojibake class (see encoding-audit)
        kwargs.setdefault("errors", "replace")
    kwargs.setdefault("stdout", subprocess.PIPE)
    kwargs.setdefault("stderr", subprocess.PIPE)
    for key, value in process_group_kwargs().items():
        kwargs.setdefault(key, value)
    input_data = kwargs.pop("input", None)
    if input_data is not None:
        kwargs.setdefault("stdin", subprocess.PIPE)
    else:
        # Spawn hygiene (esh-1): never inherit the harness's stdin. A stdin-reading
        # CLI (codex exec under a background pipe) blocks on an open pipe with no
        # EOF until the timeout kills it; DEVNULL gives it EOF immediately.
        kwargs.setdefault("stdin", subprocess.DEVNULL)
    cpu_before = None if os.name == "nt" else _child_cpu_seconds_posix_baseline()
    suspended = False
    if os.name == "nt":
        # CREATE_SUSPENDED closes the job-assignment race: launcher shims
        # (venv python.exe) respawn the real interpreter within milliseconds,
        # and children only inherit the job if the parent was in it at spawn.
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | 0x00000004
        suspended = True
    proc = subprocess.Popen(cmd, **kwargs)  # type: ignore[arg-type]
    try:
        job = _win_job_assign(proc, job_limits) if os.name == "nt" else None
    except Exception:
        proc.kill()  # child is still CREATE_SUSPENDED; never run it uncapped
        raise
    if suspended:
        try:
            import ctypes
            status = ctypes.windll.ntdll.NtResumeProcess(  # type: ignore[attr-defined]
                ctypes.c_void_p(int(proc._handle)))  # type: ignore[attr-defined]
            if status != 0:
                raise OSError(f"NtResumeProcess failed: 0x{status & 0xFFFFFFFF:08X}")
        except Exception:
            proc.kill()  # never leave a suspended zombie behind
            raise
    try:
        stdout, stderr = proc.communicate(input=input_data, timeout=timeout)
        completed = subprocess.CompletedProcess(cmd, proc.returncode, stdout=stdout or "", stderr=stderr or "")
        completed.cpu_seconds = _reap_cpu_seconds(job, cpu_before)  # type: ignore[attr-defined]
        completed.proc_applied = job is not None  # type: ignore[attr-defined]  # SPEC-163 M2: per-spawn Job-Object application fact
        return completed
    except subprocess.TimeoutExpired:
        signal_process_tree(proc.pid, signal.SIGTERM)
        try:
            stdout, stderr = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            signal_process_tree(proc.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            try:
                stdout, stderr = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
        _win_job_cpu_seconds(job)  # closes the job handle; value discarded
        raise subprocess.TimeoutExpired(cmd, timeout, output=decode_pipe(stdout), stderr=decode_pipe(stderr))



def _status_to_returncode(status: int) -> int:
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return -os.WTERMSIG(status)
    return 1


def run_posix_spawn_bounded(cmd: list[str], *, timeout: int, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str] | None:
    """Run a command via posix_spawn with bounded process-tree cleanup.

    Returns None on platforms without os.posix_spawn support so callers can
    fall back to Popen-based execution. Captures stdout/stderr together to a
    temporary file, avoiding inherited pipe stalls in long-lived validators.
    """
    if os.name == "nt" or not hasattr(os, "posix_spawn"):
        return None
    output_path = Path(tempfile.mkstemp(prefix="harness-fixture-output-", suffix=".log")[1])
    fd = os.open(output_path, os.O_WRONLY | os.O_TRUNC)
    timed_out = False
    try:
        actions = [(os.POSIX_SPAWN_DUP2, fd, 1), (os.POSIX_SPAWN_DUP2, fd, 2)]
        pid = os.posix_spawn(cmd[0], cmd, env or os.environ.copy(), file_actions=actions, setsid=True)
        os.close(fd)
        fd = -1
        deadline = time.monotonic() + timeout
        returncode: int | None = None
        while returncode is None:
            try:
                done, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                done, status = pid, 0
            if done == pid:
                returncode = _status_to_returncode(status)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                signal_process_tree(pid, signal.SIGTERM)
                grace = time.monotonic() + 3
                while time.monotonic() < grace:
                    done, status = os.waitpid(pid, os.WNOHANG)
                    if done == pid:
                        returncode = _status_to_returncode(status)
                        break
                    time.sleep(0.05)
                if returncode is None:
                    signal_process_tree(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
                    try:
                        done, status = os.waitpid(pid, 0)
                        returncode = _status_to_returncode(status)
                    except ChildProcessError:
                        returncode = 124
                if timed_out:
                    returncode = 124
                break
            time.sleep(0.05)
        output = output_path.read_text(encoding="utf-8", errors="ignore") if output_path.exists() else ""
        stderr = f"timeout after {timeout}s" if timed_out else ""
        return subprocess.CompletedProcess(cmd, returncode if returncode is not None else 124, stdout=output, stderr=stderr)
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            output_path.unlink(missing_ok=True)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# SPEC-119 v5 / E3-C6a: least-privilege spawn env for workflow workers.
# Workers used to inherit the FULL parent env (every API key included). The
# filter keeps only: a minimal OS base allowlist + the executor's envKeepList +
# the HARNESS_* vars the runtime sets. Everything else is DROPPED.
# --------------------------------------------------------------------------- #

# Minimal OS vars a spawned CLI/interpreter needs. Windows needs SYSTEMROOT for
# sockets/ssl; vendor CLIs read auth/config under USERPROFILE/APPDATA/LOCALAPPDATA.
OS_BASE_ALLOWLIST = frozenset({
    # Windows
    "SYSTEMROOT", "SYSTEMDRIVE", "PATH", "PATHEXT", "TEMP", "TMP", "USERPROFILE",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "COMSPEC", "HOMEDRIVE", "HOMEPATH",
    "NUMBER_OF_PROCESSORS", "OS", "WINDIR",
    # POSIX
    "HOME", "LANG", "SHELL",
})


def _keep_by_list(key: str, keep_list) -> bool:
    """Exact match, or prefix match when a keep entry ends in ``*`` (e.g. ``CLAUDE_*``)."""
    for pat in keep_list or ():
        if pat.endswith("*"):
            if key.startswith(pat[:-1]):
                return True
        elif key == pat:
            return True
    return False


def filter_spawn_env(parent_env, harness_vars: dict[str, str], *, keep_list=(),
                     extra: dict[str, str] | None = None, enabled: bool = True) -> dict[str, str]:
    """Build a least-privilege worker env (SPEC-119 v5 / E3-C6a).

    enabled=False is the escape hatch (project.json workflows.workerEnvFilter=false):
    it restores full-env inheritance. When on, the result is the OS base allowlist +
    keep_list (LC_* locale vars always kept) + harness_vars + extra; all other
    parent-env entries (unrelated API keys/tokens/secrets) are dropped.
    """
    if enabled:
        env = {k: v for k, v in parent_env.items()
               if k in OS_BASE_ALLOWLIST or k.startswith("LC_") or _keep_by_list(k, keep_list)}
    else:
        env = dict(parent_env)
    env.update(harness_vars)
    if extra:
        env.update(extra)
    return env


def registry_path_windows() -> str:
    """The persistent USER+MACHINE PATH from the Windows registry — what a
    freshly launched process sees.

    A long-lived launcher (a service, IDE, scheduled task, or a shell opened
    before an installer edited PATH) holds a PATH snapshot from its own start;
    every child it spawns inherits that STALE PATH, so an executor that is on
    the real PATH (e.g. a just-installed vendor CLI) is unresolvable via
    ``shutil.which``. Reading the registry recovers the current effective PATH
    without relaunching. nt-only; returns "" elsewhere or on any failure so the
    caller falls back to the normal (process-PATH) behaviour.
    """
    if os.name != "nt":
        return ""
    parts: list[str] = []
    try:
        import winreg  # stdlib on Windows
        for root, sub in ((winreg.HKEY_CURRENT_USER, "Environment"),
                          (winreg.HKEY_LOCAL_MACHINE,
                           r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
            try:
                with winreg.OpenKey(root, sub) as key:
                    val, _ = winreg.QueryValueEx(key, "Path")
                    if val:
                        parts.append(os.path.expandvars(str(val)))  # REG_EXPAND_SZ carries %vars%
            except OSError:
                pass  # key/value absent (HKLM read may also be denied) — skip it
    except Exception:
        return ""
    return os.pathsep.join(parts)


if __name__ == "__main__":
    _parent = {"PATH": "/bin", "LC_ALL": "C", "OPENAI_API_KEY": "sk-keep",
               "CLAUDE_CODE_ENTRYPOINT": "cli", "GEMINI_API_KEY": "leak", "RANDOM_SECRET": "x"}
    oc = filter_spawn_env(_parent, {"HARNESS_WORKER_ID": "w1"},
                          keep_list=["OPENAI_API_KEY", "GEMINI_API_KEY"])
    assert oc.get("OPENAI_API_KEY") == "sk-keep" and oc.get("GEMINI_API_KEY") == "leak"
    assert "RANDOM_SECRET" not in oc and oc["PATH"] == "/bin" and oc["LC_ALL"] == "C"
    assert oc["HARNESS_WORKER_ID"] == "w1"
    cl = filter_spawn_env(_parent, {}, keep_list=["CLAUDE_*", "ANTHROPIC_*"])
    assert cl.get("CLAUDE_CODE_ENTRYPOINT") == "cli" and "OPENAI_API_KEY" not in cl
    assert "GEMINI_API_KEY" not in cl and "RANDOM_SECRET" not in cl
    off = filter_spawn_env(_parent, {}, keep_list=(), enabled=False)
    assert off.get("RANDOM_SECRET") == "x"  # escape hatch restores full inheritance
    print("filter_spawn_env self-check OK")

    # console-hiding (black-terminal-windows Phase 0): both branches asserted on any
    # platform by flipping os.name — pure kwargs checks, no window is ever spawned.
    _orig_name = os.name
    try:
        os.name = "nt"
        assert process_group_kwargs()["creationflags"] & CREATE_NO_WINDOW, "nt group spawn must hide console"
        assert quiet_creationflags({"capture_output": True})["creationflags"] & CREATE_NO_WINDOW
        assert quiet_creationflags({"stdout": subprocess.PIPE, "stderr": subprocess.PIPE})["creationflags"] & CREATE_NO_WINDOW
        assert "creationflags" not in quiet_creationflags({}), "inherited stdio must keep a visible console"
        os.name = "posix"
        assert process_group_kwargs() == {"start_new_session": True}, "posix must not set Windows flags"
        assert "creationflags" not in quiet_creationflags({"capture_output": True}), "no Windows flags on posix"
    finally:
        os.name = _orig_name
    print("console-hiding self-check OK")

    # rt6/exp21 teardown ordering: signal_process_tree MUST snapshot descendants
    # BEFORE any kill (killing the session leader first reparents its cross-session
    # start_new_session children to init before the walk sees them). Asserted via the
    # nt branch (which the reorder also reordered), so a revert reddens this on any
    # platform; no real process is signalled (every kill primitive is mocked).
    _order: list[str] = []
    _sp_pd, _sp_rq, _sp_ak = process_descendants, run_quiet, _audit_kill
    _sp_kill, _sp_name = os.kill, os.name
    try:
        os.name = "nt"
        globals()["process_descendants"] = lambda _pid: _order.append("snapshot") or []
        globals()["run_quiet"] = lambda *_a, **_k: _order.append("kill")
        globals()["_audit_kill"] = lambda *_a, **_k: None
        os.kill = lambda *_a, **_k: _order.append("kill")
        signal_process_tree(999999, 15)
        assert _order[:1] == ["snapshot"], f"descendants must be snapshotted before any kill, got {_order}"
    finally:
        globals()["process_descendants"] = _sp_pd
        globals()["run_quiet"] = _sp_rq
        globals()["_audit_kill"] = _sp_ak
        os.kill, os.name = _sp_kill, _sp_name
    print("signal_process_tree ordering self-check OK")

    # registry-PATH recovery (stale-launcher-PATH fix): never raises, always a
    # str; "" on posix. On nt it reflects the real HKCU/HKLM PATH (machine-
    # specific, so only the type/liveness is asserted here).
    _rp = registry_path_windows()
    assert isinstance(_rp, str), "registry_path_windows must return a str"
    _saved = os.name
    try:
        os.name = "posix"
        assert registry_path_windows() == "", "posix must return empty registry PATH"
    finally:
        os.name = _saved
    print("registry-PATH self-check OK")

    # SPEC-148 P2: the Job Object memory cap is REAL — a child allocating 300MB
    # under a 96MB job cap dies with MemoryError (exit 7). nt-only behavior.
    if os.name == "nt":
        _alloc = ("import sys\n"
                  "try:\n    b = b'x' * (300 * 1024 * 1024)\n    sys.exit(1)\n"
                  "except MemoryError:\n    sys.exit(7)\n")
        _capped = run_process_tree_bounded([sys.executable, "-c", _alloc], timeout=60,
                                           job_limits={"jobMemoryBytes": 96 * 1024 * 1024})
        assert _capped.returncode == 7, f"memory cap not enforced (rc={_capped.returncode})"
        _free = run_process_tree_bounded([sys.executable, "-c", _alloc], timeout=60)
        assert _free.returncode == 1, f"uncapped control failed (rc={_free.returncode})"
        print("job-limits self-check OK")

        # SPEC-163 Phase 2a: assign_job_to_pid caps a CREATE_SUSPENDED child BY PID (the
        # async-seam primitive) — the same 300MB alloc under a 96MB cap dies (rc 7), and a
        # live job handle is returned (=> the async seam records procApplied True).
        _susp = subprocess.Popen([sys.executable, "-c", _alloc], creationflags=0x00000004,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _job = assign_job_to_pid(_susp.pid, {"jobMemoryBytes": 96 * 1024 * 1024})
        _rc = _susp.wait(timeout=60)
        close_win_handle(_job)
        assert _rc == 7 and _job is not None, f"assign_job_to_pid cap not enforced (rc={_rc}, job={_job})"
        print("assign_job_to_pid self-check OK")

        # SECREV H1 Fase 2b: cap_process_best_effort caps an already-running (here
        # CREATE_SUSPENDED-then-capped, for determinism) child BY PID, holds the handle in
        # _LIVE_JOB_HANDLES (no resume inside — we resume below), and is FAIL-OPEN. The same
        # 300MB alloc under a 96MB cap dies (rc 7); a dead pid returns False without raising.
        import ctypes
        _before = len(_LIVE_JOB_HANDLES)
        _c = subprocess.Popen([sys.executable, "-c", _alloc], creationflags=0x00000004,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _applied = cap_process_best_effort(_c.pid, {"jobMemoryBytes": 96 * 1024 * 1024})
        ctypes.windll.ntdll.NtResumeProcess(ctypes.c_void_p(int(_c._handle)))  # type: ignore[attr-defined]
        _crc = _c.wait(timeout=60)
        assert _applied is True and _crc == 7, f"cap_process_best_effort not enforced (applied={_applied}, rc={_crc})"
        assert len(_LIVE_JOB_HANDLES) == _before + 1, "cap handle not held for process lifetime"
        assert cap_process_best_effort(_c.pid, {"jobMemoryBytes": 1}) is False, "fail-open: dead pid must return False"
        print("cap_process_best_effort self-check OK")

    # launch_detached: a real detached child writes to its log and exits (poll <=5s).
    import shutil
    _tmp = Path(tempfile.mkdtemp())
    try:
        _log = _tmp / "detached.log"
        _pid = launch_detached([sys.executable, "-c", "print('ok')"], log_path=_log, cwd=_tmp)
        assert _pid > 0, "launch_detached returned no pid"
        _deadline = time.time() + 5
        while time.time() < _deadline:
            if _log.exists() and "ok" in _log.read_text(encoding="utf-8", errors="ignore"):
                break
            time.sleep(0.05)
        assert _log.exists() and "ok" in _log.read_text(encoding="utf-8", errors="ignore"), \
            "launch_detached child did not write to the log"
    finally:
        shutil.rmtree(_tmp, ignore_errors=True)
    print("launch_detached self-check OK")
