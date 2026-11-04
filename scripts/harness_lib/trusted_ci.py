"""TCP-05 — Trusted CI control plane (TRAIN-05-TCP-05).

Hermetic test runner whose outcomes are bound to cryptographically sealed
TestExecutionReceipts, composed on top of the landed seams:
- TCP-04: the working directory is validated through
  ``sandbox_spawn.resolve_confined`` and the receipt may link canonical
  confinement evidence (verified fail-closed on consumption);
- TCP-02 pattern: receipts are canonical controller-owned records re-read and
  re-digested on every verification — structural validity never passes alone.

Network boundary: the hermetic environment black-holes HTTP(S) proxies to
127.0.0.1:1 so any unauthenticated HTTP client call fails deterministically.
This is an ENVIRONMENT boundary, not a kernel one, and the receipt says so
(``networkBoundary.kernelEnforced: false`` — manifest honesty, SPEC-163 M2
parity): a receipt can never claim confinement the host did not apply.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from harness_lib import sandbox_spawn  # noqa: E402

TRUSTED_CI_RUNS_REL = ".harness/runs/trusted-ci"
TEST_RECEIPT_SCHEMA = "tcp05-test-execution-receipt/1.0"
RECEIPT_MAX_AGE_SECONDS = 3600
_CI_CONTROLLERS = ("harness-governance-controller", "harness-audit-controller")
_BLACKHOLE = "http://127.0.0.1:1"


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _receipt_path(root: Path | str, execution_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in execution_id)
    return Path(root) / TRUSTED_CI_RUNS_REL / f"{safe}.json"


def hermetic_env(scratch: Path) -> dict[str, str]:
    """Deterministic environment: no user site, no inherited secrets, and all
    HTTP(S) proxy routes black-holed (unauthenticated network fails closed)."""
    env = {
        "PATH": str(Path(sys.executable).parent),
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "TEMP": str(scratch), "TMP": str(scratch),
        "HOME": str(scratch), "USERPROFILE": str(scratch),
        "HTTP_PROXY": _BLACKHOLE, "HTTPS_PROXY": _BLACKHOLE,
        "http_proxy": _BLACKHOLE, "https_proxy": _BLACKHOLE,
        "NO_PROXY": "", "no_proxy": "",
    }
    if os.name == "nt":  # child processes need the system root to start at all
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return env


def _canonical_workspace(workspace: Path | str) -> Path:
    """Resolve a workspace once and reject aliases that already escape it."""
    requested = Path(os.path.abspath(os.fspath(workspace)))
    resolved = sandbox_spawn.resolve_confined(workspace, ".")
    if os.path.normcase(str(requested)) != os.path.normcase(str(resolved)):
        raise sandbox_spawn.SandboxRefused(
            f"TRUSTED_CI_WORKSPACE_ALIAS_REFUSED: {requested} resolves to {resolved}")
    return resolved


def _run_posix_pinned(argv: list[str], ws: Path, *, timeout: int) -> tuple[Any, str]:
    """Run from an opened directory, so a later path swap cannot redirect cwd."""
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        workspace_fd = os.open(ws, flags)
    except OSError as exc:
        raise sandbox_spawn.SandboxRefused(
            f"TRUSTED_CI_WORKSPACE_OPEN_FAILED_CLOSED: {exc}") from exc
    try:
        fd_root = next((Path(candidate) for candidate in ("/proc/self/fd", "/dev/fd")
                        if Path(candidate).is_dir()), None)
        if fd_root is None:
            raise sandbox_spawn.SandboxRefused(
                "TRUSTED_CI_PINNED_WORKSPACE_UNAVAILABLE: no descriptor filesystem")
        fd_workspace = fd_root / str(workspace_fd)
        scratch = fd_workspace / ".trusted-ci-scratch"
        scratch.mkdir(exist_ok=True)
        identity = os.fstat(workspace_fd)
        proc = subprocess.run(
            argv, cwd=str(fd_workspace), env=hermetic_env(scratch), pass_fds=(workspace_fd,),
            capture_output=True, text=True, timeout=timeout)
        return proc, f"posix:{identity.st_dev}:{identity.st_ino}"
    finally:
        os.close(workspace_fd)


def run_hermetic(argv: list[str], *, root: Path | str, workspace: Path | str,
                 subject_fingerprint: str, timeout: int = 600,
                 confinement_execution_id: str | None = None,
                 controller_identity: str = "harness-governance-controller",
                 verifier_fields: dict[str, str] | None = None) -> dict[str, Any]:
    """Run one hermetic CI command and mint its canonical TestExecutionReceipt.

    The cwd is validated through the TCP-04 boundary authority; the receipt is
    written under the controller-owned runs dir (never the workspace) and
    sealed by digest. The exit code is recorded verbatim — a failing suite
    mints a FAILING receipt; nothing here converts red to green."""
    ws = _canonical_workspace(workspace)
    execution_id = "CI-" + uuid.uuid4().hex
    started = time.time()
    if os.name == "nt":
        scratch = ws / ".trusted-ci-scratch"
        scratch.mkdir(exist_ok=True)
        proc = subprocess.run(argv, cwd=str(ws), env=hermetic_env(scratch),
                              capture_output=True, text=True, timeout=timeout)
        workspace_identity = "windows-canonical-path"
    else:
        proc, workspace_identity = _run_posix_pinned(argv, ws, timeout=timeout)
    receipt: dict[str, Any] = {
        "schema": TEST_RECEIPT_SCHEMA,
        "executionId": execution_id,
        "controllerIdentity": controller_identity,
        "subjectFingerprint": subject_fingerprint,
        "argvDigest": _digest(list(argv)),
        "workspace": str(ws),
        "workspaceIdentity": workspace_identity,
        "exitCode": int(proc.returncode),
        "stdoutDigest": _digest(proc.stdout or ""),
        "stderrDigest": _digest(proc.stderr or ""),
        "startedAt": started,
        "endedAt": time.time(),
        "networkBoundary": {"class": "env-proxy-blackhole", "kernelEnforced": False},
        "confinementExecutionId": confinement_execution_id,
        "verifier": dict(verifier_fields or {}),
    }
    receipt["digest"] = _digest(receipt)
    path = _receipt_path(root, execution_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return {"returncode": proc.returncode, "executionId": execution_id,
            "receipt": receipt, "stdout": proc.stdout, "stderr": proc.stderr}


def verify_test_receipt(root: Path | str, execution_id: str, *,
                        expected_fingerprint: str | None = None,
                        require_confinement: bool = False,
                        max_age_seconds: float = RECEIPT_MAX_AGE_SECONDS,
                        now: float | None = None) -> tuple[bool, str]:
    """Fail-closed verification of a canonical TestExecutionReceipt.

    Refuses: absent record, digest mismatch (tamper — including any attempt to
    rewrite a non-zero exit code into a PASS), executionId mismatch (replay),
    unauthorized producer, non-zero exit code, subject mismatch, staleness,
    and — when the receipt links TCP-04 confinement evidence or the caller
    requires it — an unverifiable confinement link."""
    path = _receipt_path(root, execution_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return False, "no canonical test receipt on disk (missing execution evidence)"
    except json.JSONDecodeError:
        return False, "test receipt unreadable (fails closed)"
    claimed = payload.pop("digest", None)
    if claimed != _digest(payload):
        return False, "test receipt digest mismatch (tampered or resealed)"
    if payload.get("schema") != TEST_RECEIPT_SCHEMA:
        return False, f"unsupported test receipt schema {payload.get('schema')!r}"
    if payload.get("executionId") != execution_id:
        return False, "test receipt executionId mismatch (replayed receipt)"
    if payload.get("controllerIdentity") not in _CI_CONTROLLERS:
        return False, f"unauthorized CI producer {payload.get('controllerIdentity')!r}"
    if payload.get("exitCode") != 0:
        return False, f"test execution exited {payload.get('exitCode')} — a failing run is not CI proof"
    if expected_fingerprint is not None and payload.get("subjectFingerprint") != expected_fingerprint:
        return False, "test receipt subject fingerprint mismatch"
    age = (time.time() if now is None else now) - float(payload.get("endedAt") or 0)
    if age > max_age_seconds or age < 0:
        return False, f"stale test receipt ({int(age)}s old) cannot satisfy a live CI claim"
    confinement_id = payload.get("confinementExecutionId")
    if confinement_id:
        ok, why = sandbox_spawn.verify_confinement_evidence(
            root, confinement_id, expected_fingerprint=payload.get("subjectFingerprint") or None)
        if not ok:
            return False, f"linked confinement evidence refused: {why}"
    elif require_confinement:
        return False, "receipt carries no confinement evidence link (required by caller)"
    return True, "verified canonical test execution receipt"
