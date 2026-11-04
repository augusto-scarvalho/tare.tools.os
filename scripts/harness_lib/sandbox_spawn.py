#!/usr/bin/env python3
"""SPEC-148 P1 — sandbox_spawn: the harness-owned confinement chokepoint.

Every autonomous worker spawn routes through here (rule 1). Composition scales
with risk tier (rule 2): R0 (read-only) = least-privilege env + the caller's
bounded/grouped spawn primitive, both OS-agnostic; R1+ (writeAllowed) =
additionally an isolated workspace cwd and OS-level deny-write+delete on the
protected canonical paths inside that workspace (Windows backend: icacls,
owner decision Option A — files present and readable, mutation blocked).
An OS without an fs backend FAILS CLOSED for R1+ (rule 3); the only bypass is
the owner-set HARNESS_SANDBOX_OVERRIDE env var, honored loudly and receipted
(rule 4). The merge-time footprint gate stays the OS-agnostic backstop under
every backend (rule 9).

Vendor inner layers ride in argv and are composed, never replaced (rule 7):
codex --sandbox from writeAllowed (S3), claude tools ceiling (S2), open-model
HTTP worker has no fs tool at all. Interactive owner-present rooms
(chat_engines) are a RECORDED exemption (Trilha S deferral), not a silent one.

Self-check: python scripts/harness_lib/sandbox_spawn.py
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # standalone self-check imports siblings
    sys.path.insert(0, str(_SCRIPTS))

from harness_lib import processes, protected_files  # noqa: E402

OVERRIDE_ENV = "HARNESS_SANDBOX_OVERRIDE"  # owner-set reason; loud + receipted
_EVERYONE_SID = "*S-1-1-0"  # locale-neutral 'Everyone' (deny binds all principals)
# SPEC-148 P2 (D2): whole-tree Job Object caps for bounded sandbox spawns.
# Generous by design — the ceiling is fork bombs / runaway allocation, not a
# working set tuner. Callers may pass job_limits=None to opt out explicitly.
DEFAULT_JOB_LIMITS: dict[str, Any] = {"activeProcesses": 64,
                                      "jobMemoryBytes": 4 * 1024 * 1024 * 1024,
                                      "breakawayOk": True}

# SPEC-148 v3 (article §3.5 / N5): risk tiers are a closed vocabulary. Two
# consumers today -- the sandbox receipt (write intent alone) and the route
# decision (deterministic risk flags). Pure, stdlib only.
RISK_TIERS: dict[str, str] = {
    "R0": "observational (read-only)",
    "R1": "reversible write (isolated workspace + merge gate)",
    "R2": "material (protected-path adjacent, external effect)",
    "R3": "critical (secrets, payments, destructive)",
}
_R3_FLAGS = {"payment", "secrets", "destructive"}


def risk_tier(*, write_allowed: bool, risk_flags: list[str] | None = None) -> str:
    """Map a spawn's write intent + deterministic risk flags to a closed tier.

    No flags -> R1 (write) or R0 (read-only). Any flag in _R3_FLAGS -> R3; any
    other non-empty flag -> R2 (SPEC-148 rule 13). Pure."""
    flags = risk_flags or []
    if not flags:
        return "R1" if write_allowed else "R0"
    return "R3" if _R3_FLAGS & set(flags) else "R2"


class SandboxRefused(RuntimeError):
    """R1+ spawn refused: missing backend, setup failure, or unfiltered env.

    Fail closed (article §3.5): a missing sandbox is "cannot run this risk
    tier here", never a silent unconfined spawn."""


STRICT_CANDIDATE_READONLY = "STRICT_CANDIDATE_READONLY"


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class SandboxPermit:
    """Controller-issued authority input; the sandbox never invents permissions."""
    permit_id: str
    authority_identity: str
    candidate_tree_id: str
    candidate_fingerprint: str
    snapshot_id: str
    profile_id: str
    profile_version: str
    scratch_identity_class: str

    @property
    def identity_digest(self) -> str:
        return _digest({"permitId": self.permit_id, "authority": self.authority_identity,
                        "candidateTreeId": self.candidate_tree_id,
                        "candidateFingerprint": self.candidate_fingerprint,
                        "snapshotId": self.snapshot_id, "profile": self.profile_id,
                        "profileVersion": self.profile_version,
                        "scratchIdentityClass": self.scratch_identity_class})


def issue_strict_candidate_permit(*, candidate_tree_id: str, candidate_fingerprint: str,
                                  snapshot_id: str, authority_identity: str,
                                  scratch_identity_class: str = "ephemeral-controller-scratch") -> SandboxPermit:
    """Issue the narrow permit consumed by the strict subject-process adapter.

    The caller supplies an authority identity from its policy/controller.  A candidate
    receives only the rendered environment and never this controller-side object.
    """
    if not authority_identity.strip() or not candidate_tree_id or not snapshot_id:
        raise SandboxRefused("SANDBOX_DOES_NOT_GRANT_AUTHORITY: missing authority-bound permit input")
    seed = {"authority": authority_identity, "candidateTreeId": candidate_tree_id,
            "candidateFingerprint": candidate_fingerprint, "snapshotId": snapshot_id,
            "profile": STRICT_CANDIDATE_READONLY, "version": "v1"}
    return SandboxPermit("SP-" + _digest(seed).split(":", 1)[1][:20], authority_identity,
                         candidate_tree_id, candidate_fingerprint, snapshot_id,
                         STRICT_CANDIDATE_READONLY, "v1", scratch_identity_class)


def _refuse(reason: str, receipt: dict[str, Any]) -> None:
    """Refuse (raise) unless the owner override env is set — then proceed LOUDLY."""
    override = os.environ.get(OVERRIDE_ENV, "").strip()
    if override:
        receipt.setdefault("overrides", []).append({"refused": reason, "ownerToken": override})
        print(f"[sandbox OVERRIDE] {reason} -- proceeding on owner override: {override}",
              file=sys.stderr)
        return
    raise SandboxRefused(f"{reason} (owner escape hatch: set {OVERRIDE_ENV}=<reason>)")


def protected_rel_paths(root: Path) -> list[str]:
    """Repo-relative protected canonical paths (exact + pattern-matched)."""
    return protected_files.snapshot_paths(Path(root))


def fs_backend() -> str | None:
    """fs-confine backend for this OS; None fails closed for R1+ (rule 8)."""
    return "nt-icacls" if os.name == "nt" else None


def fs_confine_nt(workspace: Path, rel_paths: list[str]) -> list[str]:
    """Make each protected path present under workspace unwritable AND undeletable.

    Two composed locks (measured 2026-07-18): the read-only attribute blocks
    DeleteFile even when the caller holds FILE_DELETE_CHILD on the parent dir
    (a bare deny-DE ACE does NOT — delete+recreate bypass), and the deny ACE
    blocks writes AND WRITE_ATTRIBUTES, so the worker cannot clear the RO bit.
    The ACE lists specific write rights, NOT generic W: denying W also denies
    SYNCHRONIZE/READ_CONTROL, which breaks READ opens (Option A wants readable).
    Order matters: +R first (chmod needs the not-yet-denied attribute write).
    Any icacls failure raises: a partially-applied sandbox is a setup failure,
    not a weaker sandbox."""
    applied: list[str] = []
    for rel in rel_paths:
        target = workspace / rel
        if not target.is_file():
            continue  # absent in this workspace: nothing to write to; merge gate backstops creation
        target.chmod(stat.S_IREAD)
        proc = processes.run_quiet(["icacls", str(target), "/deny",
                                    f"{_EVERYONE_SID}:(WD,AD,WA,WEA,DE)"],
                                   capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(f"icacls deny failed on {rel}: rc={proc.returncode} "
                               f"{(proc.stderr or proc.stdout or '').strip()[:200]}")
        applied.append(rel)
    return applied


def _reparse_points_outside(p: Path, tree: Path) -> bool:
    """True if reparse point ``p`` resolves OUTSIDE ``tree`` (unresolvable == outside).

    Only the direction of a reparse point we FAILED to remove matters: one pointing
    inside the tree cannot escape the recursive strip, one pointing out can."""
    try:
        target = Path(os.path.realpath(p))
        tree_r = Path(tree).resolve()
    except OSError:
        return True  # cannot resolve -> assume it escapes (fail-safe)
    return target != tree_r and tree_r not in target.parents


def _neutralize_reparse_points(tree: Path) -> bool:
    """Remove workspace reparse points (junctions/symlinks) BEFORE the recursive strip.

    SECREV M3 (confused-deputy): ``icacls /t`` and ``attrib /S`` FOLLOW junctions, so
    a worker that plants ``workspace\\x`` -> the live tree would have the harness cleanup
    strip the deny ACEs + RO bit off the REAL canonical files. We remove the LINK, never
    the target (``os.rmdir`` on a directory junction/symlink and ``os.unlink`` on a file
    symlink leave the target intact), and prune reparse dirs so ``os.walk`` itself never
    descends through one (junctions are not reliably ``islink`` pre-3.12, so we test the
    reparse ATTRIBUTE explicitly). FAIL-SAFE, not best-effort: returns False iff a reparse
    point resolving OUTSIDE ``tree`` could not be removed — the caller then SKIPS the
    recursive strip (a dirty workspace beats a strip on the live tree)."""
    safe = True
    for dirpath, dirnames, filenames in os.walk(tree, topdown=True, followlinks=False):
        for name in list(dirnames) + filenames:
            p = Path(dirpath) / name
            try:
                attrs = os.lstat(p).st_file_attributes
            except OSError:
                # FAIL-SAFE (reckon 2026-07-21): an entry we cannot stat might be a
                # junction we cannot verify. Prune it so os.walk never descends, and
                # mark unsafe so the recursive strip does not run past it — a
                # non-statable entry must not be treated as "not a reparse point".
                if name in dirnames:
                    dirnames.remove(name)
                safe = False
                continue
            if not attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                continue
            is_dir_link = bool(attrs & stat.FILE_ATTRIBUTE_DIRECTORY)
            if is_dir_link and name in dirnames:
                dirnames.remove(name)  # never descend through the link into its target
            try:
                os.rmdir(p) if is_dir_link else os.unlink(p)
            except OSError:
                if _reparse_points_outside(p, tree):
                    safe = False  # external link survived -> unsafe to strip recursively
    return safe


def fs_release(tree: Path | str) -> bool:
    """Remove our deny ACEs recursively under ``tree`` so disposal can delete.

    Neutralizes workspace reparse points first (SECREV M3): the recursive icacls/attrib
    strip follows junctions, so a planted junction would let this cleanup strip protections
    off the live tree. FAIL-SAFE — if an external-pointing reparse point cannot be removed,
    the recursive strip is SKIPPED (a dirty workspace beats a strip on the real tree).
    Best-effort otherwise (cleanup must not die on a half-removed workspace); no-op off nt.

    INVARIANT (reckon 2026-07-21): callers MUST run this only AFTER the write-worker
    is dead (all current call sites do: dispose runs post-communicate/tree-kill), else
    a live worker could re-plant a junction in the neutralize->strip window (TOCTOU)."""
    if os.name != "nt" or not Path(tree).exists():
        return False
    try:
        if not _neutralize_reparse_points(Path(tree)):
            return False  # fail-safe: an external junction survived -> do NOT strip
        # Reverse order of fs_confine_nt: drop the deny ACEs first (clearing the
        # RO attribute needs WRITE_ATTRIBUTES back), then clear RO for rmtree.
        processes.run_quiet(["icacls", str(tree), "/remove:d", _EVERYONE_SID, "/t", "/c", "/q"],
                            capture_output=True, text=True, timeout=60)
        processes.run_quiet(["attrib", "-R", str(Path(tree) / "*"), "/S", "/D"],
                            capture_output=True, text=True, timeout=60)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# TCP-04: controller-boundary path authority & canonical confinement evidence
# ---------------------------------------------------------------------------

CONFINEMENT_EVIDENCE_REL = ".harness/runs/confinement"
CONFINEMENT_EVIDENCE_SCHEMA = "tcp04-confinement-evidence/1.0"
# Stale evidence cannot satisfy a LIVE precondition (falsifier 14): a record
# older than this is negative evidence, exactly like an absent one.
CONFINEMENT_EVIDENCE_MAX_AGE_SECONDS = 3600
_CONFINEMENT_CONTROLLERS = ("harness-governance-controller", "harness-audit-controller")


def resolve_confined(workspace: Path | str, candidate: Path | str) -> Path:
    """Authoritative path-boundary check at the CONTROLLER seam (TCP-04).

    Resolves ``candidate`` (relative to ``workspace`` unless absolute) through
    the real filesystem — symlinks, junctions, ``..`` and 8.3/case aliases all
    collapse via realpath — and refuses unless the final target is inside the
    workspace. Callers use the RETURNED path for the actual write so there is
    no check-then-use gap on the path value itself. Fail closed: any
    unresolvable input refuses."""
    try:
        ws = Path(os.path.realpath(Path(workspace))).resolve(strict=True)
    except OSError as exc:
        raise SandboxRefused(f"CONFINEMENT_WORKSPACE_UNRESOLVABLE_FAILS_CLOSED: {exc}") from exc
    cand = Path(candidate)
    target = cand if cand.is_absolute() else ws / cand
    resolved = Path(os.path.realpath(target))
    ws_key = os.path.normcase(str(ws))
    resolved_key = os.path.normcase(str(resolved))
    if resolved_key != ws_key and not resolved_key.startswith(ws_key + os.sep):
        raise SandboxRefused(
            f"CONFINEMENT_BOUNDARY_ESCAPE: {candidate!s} resolves to {resolved} outside {ws}")
    return resolved


def _confinement_evidence_path(root: Path | str, execution_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in execution_id)
    return Path(root) / CONFINEMENT_EVIDENCE_REL / f"{safe}.json"


def mint_confinement_evidence(root: Path | str, receipt: dict[str, Any], *,
                              execution_id: str, subject_fingerprint: str = "",
                              controller_identity: str = "harness-governance-controller") -> dict[str, Any]:
    """Mint canonical, execution-bound confinement evidence from an ACTUAL
    spawn/sandbox receipt (sandbox_prepare / strict_candidate_execute output).

    Producer authenticity comes from WHERE this record lives (controller-owned
    canonical runs dir, outside any candidate workspace) and from digest
    verification — never from candidate-supplied metadata. The receipt's
    honesty invariants are preserved verbatim: fsApplied is the per-run
    APPLICATION fact, overrides/bypass are carried, not laundered."""
    fs_applied = receipt.get("fsApplied")
    payload: dict[str, Any] = {
        "schema": CONFINEMENT_EVIDENCE_SCHEMA,
        "executionId": execution_id,
        "controllerIdentity": controller_identity,
        "subjectFingerprint": subject_fingerprint,
        "backend": receipt.get("fsBackend") or receipt.get("platformBackend") or None,
        "fsEnforced": bool(receipt.get("fsEnforced", False)),
        "fsApplied": list(fs_applied) if isinstance(fs_applied, list) else bool(fs_applied),
        "sandboxBypassed": bool(receipt.get("sandboxBypassed", False)),
        "overrides": list(receipt.get("overrides", [])),
        "mintedAt": time.time(),
    }
    payload["digest"] = _digest(payload)
    path = _confinement_evidence_path(root, execution_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def verify_confinement_evidence(root: Path | str, execution_id: str, *,
                                expected_fingerprint: str | None = None,
                                max_age_seconds: float = CONFINEMENT_EVIDENCE_MAX_AGE_SECONDS,
                                now: float | None = None) -> tuple[bool, str]:
    """Fail-closed verification of canonical confinement evidence (TCP-04).

    Refuses: absent record (missing execution evidence), digest mismatch
    (tamper/reseal), executionId mismatch (replay), unauthorized producer,
    override/bypass runs (never production proof), fsEnforced without fsApplied
    (capability is not application), subject mismatch, and stale records."""
    path = _confinement_evidence_path(root, execution_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return False, "no canonical confinement evidence on disk (missing/absent execution evidence)"
    except json.JSONDecodeError:
        return False, "confinement evidence unreadable (fails closed)"
    claimed = payload.pop("digest", None)
    if claimed != _digest(payload):
        return False, "confinement evidence digest mismatch (tampered or resealed)"
    if payload.get("schema") != CONFINEMENT_EVIDENCE_SCHEMA:
        return False, f"unsupported confinement evidence schema {payload.get('schema')!r}"
    if payload.get("executionId") != execution_id:
        return False, "confinement evidence executionId mismatch (replayed evidence)"
    if payload.get("controllerIdentity") not in _CONFINEMENT_CONTROLLERS:
        return False, f"unauthorized confinement producer {payload.get('controllerIdentity')!r}"
    if payload.get("sandboxBypassed") or payload.get("overrides"):
        return False, "override/bypass run is never production confinement proof"
    if not payload.get("fsEnforced"):
        return False, "no enforced filesystem backend recorded (CONVENTION_ONLY is not proof)"
    applied = payload.get("fsApplied")
    if not applied:
        return False, "fsEnforced without fsApplied: host capability is not per-run application"
    if expected_fingerprint is not None and payload.get("subjectFingerprint") != expected_fingerprint:
        return False, "confinement evidence subject fingerprint mismatch"
    age = (time.time() if now is None else now) - float(payload.get("mintedAt") or 0)
    if age > max_age_seconds or age < 0:
        return False, f"stale confinement evidence ({int(age)}s old) cannot satisfy a live precondition"
    return True, "verified canonical confinement evidence"


def latest_confinement_evidence(root: Path | str) -> str | None:
    """Most recently minted evidence executionId under root, if any."""
    base = Path(root) / CONFINEMENT_EVIDENCE_REL
    try:
        files = sorted(base.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for f in files:
        try:
            eid = json.loads(f.read_text(encoding="utf-8")).get("executionId")
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(eid, str) and eid:
            return eid
    return None


def _backend_capability(*, backend_id: str, backend_version: str, platform: str,
                        availability: str, reason: str, experimental: bool,
                        enforced: bool = False, evidence_refs: list[str] | None = None) -> dict[str, Any]:
    """One honest, platform-neutral description of a strict backend."""
    return {
        "backendId": backend_id,
        "backendVersion": backend_version,
        "platform": platform,
        "availability": availability,
        "filesystemReadConfinement": enforced,
        "filesystemWriteConfinement": enforced,
        "childProcessConfinement": enforced,
        "networkConfinement": enforced,
        "reparseOrSymlinkProtection": enforced,
        "handleInheritanceProtection": enforced,
        "failClosed": True,
        "experimental": experimental,
        "reason": reason,
        "evidenceRefs": evidence_refs or [],
    }


def _windows_processmodel_capability() -> dict[str, Any]:
    """Probe only the documented ProcessModel API surface.

    Microsoft documents dynamic resolution from processmodel.dll but exposes no
    public header nor SandboxSpec.fbs schema.  Without an authoritative compiler
    for its SBOX FlatBuffer, executing would be an undocumented guess.  That is
    API_INCOMPATIBLE, not a reason to fall back to the old AppContainer ctypes POC.
    """
    if os.name != "nt":
        return _backend_capability(backend_id="windows-processmodel-experimental",
                                   backend_version="0.1.0", platform="windows",
                                   availability="NOT_AVAILABLE", experimental=True,
                                   reason="not running on Windows", evidence_refs=[])
    try:
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        load_library = kernel.LoadLibraryExW
        load_library.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint32]
        load_library.restype = ctypes.c_void_p
        get_proc = kernel.GetProcAddress
        get_proc.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        get_proc.restype = ctypes.c_void_p
        free_library = kernel.FreeLibrary
        free_library.argtypes = [ctypes.c_void_p]
        free_library.restype = ctypes.c_bool
        handle = load_library("processmodel.dll", None, 0x00000800)  # LOAD_LIBRARY_SEARCH_SYSTEM32
        if not handle:
            return _backend_capability(backend_id="windows-processmodel-experimental",
                                       backend_version="0.1.0", platform="windows",
                                       availability="API_UNAVAILABLE", experimental=True,
                                       reason=f"processmodel.dll unavailable: winerror={ctypes.get_last_error()}")
        try:
            exported = bool(get_proc(handle, b"Experimental_CreateProcessInSandbox"))
        finally:
            free_library(handle)
    except Exception as exc:
        return _backend_capability(backend_id="windows-processmodel-experimental",
                                   backend_version="0.1.0", platform="windows",
                                   availability="API_UNAVAILABLE", experimental=True,
                                   reason=f"ProcessModel probe failed: {type(exc).__name__}: {exc}")
    if not exported:
        return _backend_capability(backend_id="windows-processmodel-experimental",
                                   backend_version="0.1.0", platform="windows",
                                   availability="API_UNAVAILABLE", experimental=True,
                                   reason="Experimental_CreateProcessInSandbox export unavailable")
    return _backend_capability(backend_id="windows-processmodel-experimental",
                               backend_version="0.1.0", platform="windows",
                               availability="API_INCOMPATIBLE", experimental=True,
                               reason="official SBOX FlatBuffer compiler/schema is not available to this runtime",
                               evidence_refs=["processmodel.dll!Experimental_CreateProcessInSandbox",
                                              "Microsoft CreateProcessInSandbox SandboxSpec.fbs contract"])


def _linux_bwrap_capability() -> dict[str, Any]:
    """Probe the complete first-slice bwrap primitive, not merely its pathname."""
    if not sys.platform.startswith("linux"):
        return _backend_capability(backend_id="linux-bubblewrap", backend_version="1",
                                   platform="linux", availability="NOT_AVAILABLE",
                                   experimental=False, reason="not running on Linux")
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return _backend_capability(backend_id="linux-bubblewrap", backend_version="1",
                                   platform="linux", availability="NOT_AVAILABLE",
                                   experimental=False, reason="bubblewrap executable unavailable")
    probe = processes.run_quiet([bwrap, "--die-with-parent", "--new-session", "--unshare-user",
                                 "--unshare-pid", "--unshare-net", "--ro-bind", "/usr", "/usr",
                                 "--proc", "/proc", "--dev", "/dev", "--", "/usr/bin/true"],
                                capture_output=True, text=True, timeout=10)
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout or "probe failed").strip()[:240]
        return _backend_capability(backend_id="linux-bubblewrap", backend_version="1",
                                   platform="linux", availability="FAILED", experimental=False,
                                   reason=f"bubblewrap namespace probe failed: {detail}")
    return _backend_capability(backend_id="linux-bubblewrap", backend_version="1",
                               platform="linux", availability="AVAILABLE", experimental=False,
                               reason="bubblewrap user/PID/network namespace probe passed", enforced=True,
                               evidence_refs=["bwrap --unshare-user --unshare-pid --unshare-net"])


def strict_backend_capability() -> dict[str, Any]:
    """Return the only backend capability relevant to this host's strict profile."""
    if os.name == "nt":
        return _windows_processmodel_capability()
    if sys.platform.startswith("linux"):
        return _linux_bwrap_capability()
    return _backend_capability(backend_id="none", backend_version="0", platform=sys.platform,
                               availability="NOT_AVAILABLE", experimental=False,
                               reason="no strict filesystem-confinement backend for this platform")


def strict_backend() -> str | None:
    """Return a backend only after its complete capability probe passes."""
    capability = strict_backend_capability()
    return str(capability["backendId"]) if capability["availability"] == "AVAILABLE" else None


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _require_distinct_roots(subject_root: Path, scratch_parent: Path, developer_root: Path) -> None:
    subject, scratch, developer = subject_root.resolve(), scratch_parent.resolve(), developer_root.resolve()
    if subject == developer or _inside(subject, developer) or _inside(scratch, developer):
        raise SandboxRefused("SANDBOX_SUBJECT_OR_SCRATCH_MUST_NOT_BE_DEVELOPER_ROOT")
    if _inside(developer, subject) or _inside(developer, scratch):
        raise SandboxRefused("SANDBOX_DEVELOPER_ROOT_MUST_BE_OUTSIDE_ALLOWED_ROOTS")


def _strict_env(runtime_root: Path, subject_root: Path, scratch_root: Path,
                extra: dict[str, str] | None = None) -> dict[str, str]:
    """No inherited path, git, home, credential, or controller-root pointers."""
    env = {
        "PATH": str(runtime_root),
        "PYTHONHOME": str(runtime_root),
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TEMP": str(scratch_root),
        "TMP": str(scratch_root),
        "HOME": str(scratch_root),
        "USERPROFILE": str(scratch_root),
        "HARNESS_SUBJECT_ROOT": str(subject_root),
        "HARNESS_SANDBOX_PROFILE": STRICT_CANDIDATE_READONLY,
    }
    if extra:
        forbidden = {"PYTHONPATH", "GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                     "HARNESS_ROOT", "HARNESS_REAL_ROOT", "HOME", "USERPROFILE", "TEMP", "TMP"}
        for key, value in extra.items():
            if key.upper() in forbidden or "TOKEN" in key.upper() or "KEY" in key.upper():
                raise SandboxRefused(f"SANDBOX_ENV_DOES_NOT_DISCLOSE_OR_REDIRECT_TO_DEVELOPER_ROOT: {key}")
            env[key] = value
    return env


def _bwrap_parent_dirs(paths: list[Path]) -> list[str]:
    """Return exactly the destination skeleton required for explicit binds."""
    dirs: set[Path] = {Path("/")}
    for path in paths:
        current = path.resolve().parent
        while current != current.parent:
            dirs.add(current)
            current = current.parent
    return [str(path) for path in sorted(dirs, key=lambda item: (len(item.parts), str(item)))]


def _linux_runtime_roots(subject: Path, developer: Path) -> list[Path]:
    """Runtime roots are explicit and may never cover the developer root."""
    candidates = [Path(sys.base_prefix), Path(sys.executable).parent,
                  Path("/lib"), Path("/lib64"), Path("/usr/lib"), Path("/usr/lib64")]
    roots: list[Path] = []
    for path in candidates:
        if not path.exists():
            continue
        resolved = path.resolve()
        if _inside(developer, resolved) or _inside(subject, resolved):
            raise SandboxRefused("STRICT_SANDBOX_RUNTIME_ROOT_OVERLAPS_SUBJECT_OR_DEVELOPER")
        if resolved not in roots:
            roots.append(resolved)
    if not _inside(Path(sys.executable), Path(sys.base_prefix)):
        raise SandboxRefused("STRICT_SANDBOX_RUNTIME_EXECUTABLE_OUTSIDE_RUNTIME_ROOT")
    return roots


def _linux_bwrap_command(*, executable: Path, argv: list[str], subject: Path,
                         scratch: Path, developer: Path, env: dict[str, str]) -> list[str]:
    """Compile an allowlist-only bwrap view.  No host root is mounted."""
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise SandboxRefused("UNSUPPORTED_SANDBOX_BACKEND_FAILS_CLOSED")
    runtime_roots = _linux_runtime_roots(subject, developer)
    paths = [subject, scratch, *runtime_roots, Path("/proc"), Path("/dev"), Path("/tmp")]
    command = [bwrap, "--die-with-parent", "--new-session", "--unshare-user", "--unshare-pid",
               "--unshare-net", "--clearenv"]
    for path in _bwrap_parent_dirs(paths):
        command += ["--dir", path]
    for root in runtime_roots:
        command += ["--ro-bind", str(root), str(root)]
    command += ["--ro-bind", str(subject), str(subject), "--bind", str(scratch), str(scratch),
                "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for key, value in sorted(env.items()):
        command += ["--setenv", key, value]
    command += ["--chdir", str(subject), "--", str(executable), *argv]
    return command


def strict_candidate_execute(argv: list[str], *, permit: SandboxPermit,
                             subject_root: Path | str, scratch_parent: Path | str,
                             developer_root: Path | str, timeout: int = 120,
                             extra_env: dict[str, str] | None = None) -> dict[str, Any]:
    """Run an untrusted candidate command inside the canonical strict sandbox.

    The physical scratch path is intentionally receipt-only.  It never enters the
    candidate or permit identity, and all setup failures occur before the child is
    resumed.  Unsupported hosts refuse rather than silently running unconfined.
    """
    if permit.profile_id != STRICT_CANDIDATE_READONLY or permit.profile_version != "v1":
        raise SandboxRefused("CANDIDATE_CANNOT_EXPAND_ITS_OWN_SANDBOX: unsupported permit profile")
    capability = strict_backend_capability()
    backend = strict_backend()
    if backend is None:
        failure = ("WINDOWS_BACKEND_FAILURE_NEVER_DEGRADES_TO_UNCONFINED"
                   if capability["platform"] == "windows"
                   else "STRICT_BACKEND_FAILURE_NEVER_DEGRADES_TO_UNCONFINED")
        raise SandboxRefused(
            f"{capability['backendId']}:{capability['availability']}: "
            f"{failure}: {capability['reason']}"
        )
    subject, parent, developer = Path(subject_root).resolve(), Path(scratch_parent).resolve(), Path(developer_root).resolve()
    _require_distinct_roots(subject, parent, developer)
    if not subject.is_dir() or not parent.is_dir():
        raise SandboxRefused("STRICT_SANDBOX_ALLOWED_ROOT_MISSING")
    run_root = Path(tempfile.mkdtemp(prefix="strict-candidate-", dir=str(parent)))
    scratch = run_root / "scratch"
    scratch.mkdir()
    try:
        if backend != "linux-bubblewrap":
            # No current Windows implementation may substitute the former manual
            # AppContainer POC for the documented ProcessModel contract.
            raise SandboxRefused("UNSUPPORTED_SANDBOX_BACKEND_FAILS_CLOSED")
        executable = Path(sys.executable).resolve()
        env = _strict_env(Path(sys.base_prefix).resolve(), subject, scratch, extra_env)
        command = _linux_bwrap_command(executable=executable, argv=argv, subject=subject,
                                       scratch=scratch, developer=developer, env=env)
        proc = processes.run_process_tree_bounded(command, timeout=timeout, cwd=str(subject), env={})
        exit_code, timed_out = int(proc.returncode), bool(getattr(proc, "timed_out", False))
        receipt = {
            "sandboxProfileId": permit.profile_id, "sandboxProfileVersion": permit.profile_version,
            "permitIdentityDigest": permit.identity_digest, "permitAuthorityIdentity": permit.authority_identity,
            "candidateTreeId": permit.candidate_tree_id,
            "candidateFingerprint": permit.candidate_fingerprint, "snapshotId": permit.snapshot_id,
            "subjectRootIdentity": _digest({"candidateTreeId": permit.candidate_tree_id,
                                              "snapshotId": permit.snapshot_id}),
            "platformBackend": backend, "backendVersion": capability["backendVersion"],
            "scratchIdentityClass": permit.scratch_identity_class,
            "processExecutionId": "SX-" + uuid.uuid4().hex,
            "environmentFiltered": True,
            "childProcessContained": capability["childProcessConfinement"],
            "backendCapability": capability,
            "exitCode": exit_code, "timedOut": timed_out,
        }
        return {"returncode": exit_code, "sandboxReceipt": receipt, "scratchPath": str(scratch)}
    except SandboxRefused:
        raise
    except Exception as exc:
        raise SandboxRefused(f"SANDBOX_SETUP_FAILURE_FAILS_CLOSED: {exc}") from exc
    finally:
        shutil.rmtree(run_root, ignore_errors=True)


def _is_admin() -> bool:
    """True when the process could install a per-process WFP egress rule.

    On nt that needs an elevated token (IsUserAnAdmin); elsewhere there is no
    per-process WFP backend at all, so egress is never harness-enforceable."""
    if os.name == "nt":
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except Exception:
            return False
    return False


# SPEC-151 invariant 3: the legible confinement manifest carried on every receipt.
# SPEC-163 M2: *Enforced = host CAPABILITY ("this host CAN enforce it"); *Applied =
# per-spawn APPLICATION FACT ("THIS spawn actually entered a Job Object / had fs-confine
# ACEs applied"). procEnforced:true with procApplied:false is the honest "capable but not
# applied to this spawn" -- the gap the manifest used to mask (procApplied default False).
# SPEC-163 v2 M4 (owner 2026-07-27): treeKillReach/treeKillTarget are the THIRD
# column. *Enforced = the host CAN confine; *Applied = THIS spawn was confined;
# reach = what the harness's kill decision for this seam would actually HIT --
# and target = where we have declared we are going. Owner: "parar de mentir
# primeiro, mas declarando nosso objetivo final". Closed vocabulary, not a scale:
# `job` and `tree` are different mechanisms, not different amounts.
TREE_KILL_REACH: dict[str, str] = {
    "none": "nothing kills this spawn -- it exists to outlive its launcher (the recover sweep is its frontier)",
    "pid": "only the spawned process; descendants orphan",
    "session": "the POSIX session / process group (killpg)",
    "job": "the Windows Job Object -- every descendant, by kernel inheritance",
    "tree": "the visible descendant tree (nt taskkill /T; POSIX killpg + the procfs walk)",
}

MANIFEST_KEYS: tuple[str, ...] = ("fsEnforced", "egressEnforced", "procEnforced",
                                  "procApplied", "procAppliedScope", "fsApplied",
                                  "treeKillReach", "treeKillTarget",
                                  "sandboxBypassed", "reason")


def _kill_platform() -> str:
    """Label for the reach matrix. The discriminator is PROCFS, not the OS name:
    ``processes.process_children_map`` reads ``/proc``, so a POSIX host without
    one has darwin's reach whatever it calls itself -- and gets darwin's row."""
    if os.name == "nt":
        return "nt"
    return "linux" if Path("/proc").exists() else "darwin"


def tree_kill_reach(mode: str) -> str:
    """What a kill decision for a ``mode`` spawn reaches ON THIS HOST, TODAY.

    A pure read of the primitive each seam actually issues:
    bounded -> ``processes.signal_process_tree`` (taskkill /T | killpg + procfs walk);
    async timeout -> the LADDER then the SWEEP -- ``safe_signal_pid`` stays the primary
    kill (and is already tree-complete on POSIX, where ``signal_pid_group`` killpg's the
    session), and on nt the same ``finally`` follows it with
    ``processes.terminate_win_job`` on the worker's own Job Object handle, so nt reaches
    the whole cage (SPEC-163 v4 invariant 11);
    detached -> nothing kills it, by design."""
    if mode == "detached":
        return "none"
    if mode == "bounded":
        # Reads the real capability rather than the OS name: procfs OR ps enumerates
        # descendants, and a host with neither honestly reaches only the session.
        return "tree" if processes.descendants_enumerable() else "session"
    if mode == "async":
        return "job" if _kill_platform() == "nt" else "session"
    return "unknown"


def tree_kill_target(mode: str) -> str:
    """The DECLARED OBJECTIVE for ``mode`` on this host, stamped beside the reach.

    bounded -> every descendant, on every OS. async -> the task's CAGE (the Job
    Object on nt, the session on POSIX): the cage is what inheritance fills, so
    reaching it costs no per-child bookkeeping and no per-profile declaration.
    detached -> ``none`` BY DESIGN; outliving its launcher is its purpose."""
    if mode == "detached":
        return "none"
    if mode == "bounded":
        return "tree"
    if mode == "async":
        return "job" if os.name == "nt" else "session"
    return "unknown"


# Every (mode, platform) where reach != target TODAY, each naming the row that
# closes it. D045's "declare the gap" half, made machine-checkable: hsb-13
# asserts this dict is EXACTLY the live divergence on the running platform, so
# closing a gap without striking its entry goes red -- and so does a NEW gap
# nobody declared. The CI matrix runs all three columns.
DECLARED_KILL_GAPS: dict[str, str] = {
    # `bounded:darwin` was struck when process_children_map gained its ps fallback
    # (proc-darwin-descendants-walk). Its removal was FORCED by hsb-13, which is the
    # mechanism working as designed -- the first real exercise of the drift lock.
    # `async:nt` was struck by job-tree-lifetime Phase 2 (SPEC-163 v4, invariant 11):
    # the async timeout kill now sweeps the worker's Job Object via the handle it was
    # already holding, so nt's reach IS the cage. The control was earned, not assumed --
    # EXP-21 probe P1 (owner-ratified, verdict `shipped`) measured 3,3,3 codex / 5,5,5
    # claude grandchildren surviving a timed-out async worker against 0,0,0 bounded.
    # EMPTY is the honest state here, not a forgotten declaration: hsb-13 asserts this
    # dict is EXACTLY the live divergence, so it re-fills the moment one reopens.
}


def sandbox_capabilities() -> dict[str, Any]:
    """Probe what confinement THIS host can actually enforce (SB-3, invariant 3).

    proc = Windows Job Object process containment (ctypes CreateJobObjectW +
    the bounded-spawn tree-kill); fs = an OS fs-confine backend (nt icacls);
    egress = a per-process kernel egress block, which on Windows needs admin
    (WFP). A False here is a primitive the manifest MUST NOT claim as enforced —
    the caller degrades legibly (reason recorded) or refuses. Not cached: the
    self-check and scenario monkeypatch fs_backend/_is_admin to simulate a host
    without a primitive, and a stale cache would hide the degradation."""
    proc = os.name == "nt"
    fs = fs_backend() is not None
    egress = _is_admin() if os.name == "nt" else False
    missing: list[str] = []
    if not proc:
        missing.append(f"no Job Object process containment for os={os.name!r}")
    if not fs:
        missing.append(f"no fs-confine backend for os={os.name!r}")
    if not egress:
        missing.append("per-process egress kernel-block needs admin (WFP); process not elevated"
                       if os.name == "nt" else f"no per-process egress backend for os={os.name!r}")
    return {"proc": proc, "fs": fs, "egress": egress, "os": os.name,
            "reason": "; ".join(missing) or "all primitives enforceable on this host"}


def manifest(receipt: dict[str, Any]) -> dict[str, Any]:
    """The legible confinement manifest projected from a spawn receipt (SB-3).

    Surfaces both the host CAPABILITY keys (procEnforced/fsEnforced/egressEnforced)
    and the per-spawn APPLICATION facts (procApplied, fsApplied) so an auditor can
    tell a genuinely-confined spawn from a merely-enforceable host (SPEC-163 M2)."""
    return {k: receipt.get(k) for k in MANIFEST_KEYS if k in receipt}


def sandbox_prepare(*, vendor: str, write_allowed: bool, cwd: Path | str,
                    env: dict[str, str] | None, root: Path | str,
                    env_filtered: bool = True, inherit_reason: str | None = None,
                    protected: list[str] | None = None,
                    declares_egress: bool = False, sandbox_enabled: bool = True,
                    mode: str | None = None) -> dict[str, Any]:
    """Confinement decisions for one spawn; returns the receipt or refuses.

    ``env`` must already be the filter_spawn_env product (env_filtered=True, the
    default); an unfiltered env needs an explicit inherit_reason to be receipted.
    ``protected`` overrides the repo registry (tests only).

    SPEC-151: the receipt carries a legible confinement manifest stamped from
    ``sandbox_capabilities`` (invariant 3). ``declares_egress`` refuses a spawn
    whose task DECLARES an egress need when per-process egress is not enforceable
    (invariant 4 — advisory --allow-hosts/netsh/CaMeL are never counted as the
    boundary). ``sandbox_enabled=False`` is the ``workflows.workerSandbox=false``
    break-glass: the layer is disabled wholesale and recorded, never silent
    (invariant 5). ``mode`` ("bounded"/"async"/"detached") stamps the SPEC-163 v2
    kill-reach declaration; omitted -> those keys are absent, since no claim beats
    a wrong one."""
    caps = sandbox_capabilities()
    receipt: dict[str, Any] = {"tier": risk_tier(write_allowed=write_allowed),
                               "vendor": vendor, "fsApplied": [],
                               "envFiltered": bool(env_filtered),
                               # invariant 3: legible manifest, stamped from the probe.
                               "procEnforced": caps["proc"], "fsEnforced": caps["fs"],
                               "egressEnforced": caps["egress"], "reason": caps["reason"],
                               # SPEC-163 M2 (invariant 1): per-spawn APPLICATION fact --
                               # default False; the bounded seam confirms it True only AFTER
                               # a Job Object is actually assigned. Never a host capability.
                               "procApplied": False}
    if mode:
        # SPEC-163 v2 M4: reach is a property of the KILL PRIMITIVE, so break-glass
        # and refusals do not change it -- it is stamped before either branch.
        receipt["treeKillReach"] = tree_kill_reach(mode)
        receipt["treeKillTarget"] = tree_kill_target(mode)
    # invariant 5: break-glass disables the layer wholesale, LOUDLY (never silent).
    if not sandbox_enabled:
        receipt.update({"sandboxBypassed": True, "procEnforced": False,
                        "fsEnforced": False, "egressEnforced": False,
                        "procApplied": False,  # SPEC-163 M2: bypass applies nothing
                        "reason": "workflows.workerSandbox=false (break-glass)"})
        return receipt
    if not env_filtered and not inherit_reason:
        _refuse("unfiltered env without a recorded inherit reason", receipt)
    if inherit_reason:
        receipt["envInheritReason"] = inherit_reason
    # invariant 4: egress is honest. A task that DECLARES an egress need with no
    # enforceable per-process kernel egress is refused — the open-model HTTP worker's
    # --allow-hosts (SPEC-148 P3) is advisory defense-in-depth, never the boundary.
    receipt["egressDeclared"] = bool(declares_egress)
    if declares_egress and not caps["egress"]:
        _refuse(f"task declares an egress need but per-process egress is not enforceable "
                f"({caps['reason']})", receipt)
    if not write_allowed:
        return receipt  # R0: env scoping + the caller's bounded/grouped primitive
    cwd_r, root_r = Path(cwd).resolve(), Path(root).resolve()
    try:
        backend = fs_backend()
        if cwd_r == root_r:
            _refuse("write worker with cwd == live tree (no isolated workspace)", receipt)
        elif backend is None:
            _refuse(f"no fs-confine backend for os={os.name!r} (R1+ fails closed)", receipt)
        else:
            rels = protected if protected is not None else protected_rel_paths(root_r)
            receipt["fsApplied"] = fs_confine_nt(cwd_r, rels)
            receipt["fsBackend"] = backend
    except SandboxRefused:
        raise
    except Exception as exc:
        _refuse(f"sandbox setup failed: {exc}", receipt)
    return receipt


def sandbox_spawn(argv: list[str], *, vendor: str, write_allowed: bool,
                  cwd: Path | str, env: dict[str, str] | None, root: Path | str,
                  mode: str = "bounded", timeout: int | None = None,
                  log_path: Path | str | None = None, env_filtered: bool = True,
                  inherit_reason: str | None = None,
                  protected: list[str] | None = None,
                  declares_egress: bool = False, sandbox_enabled: bool = True,
                  **kwargs: Any) -> Any:
    """Prepare confinement, then spawn via the mediated processes.py primitive.

    mode="bounded" -> run_process_tree_bounded (Job Object + tree-kill), returns
    CompletedProcess with .sandbox_receipt; mode="detached" -> launch_detached
    (group-isolated fire-and-forget), returns {"pid", "sandboxReceipt"}."""
    receipt = sandbox_prepare(vendor=vendor, write_allowed=write_allowed, cwd=cwd,
                              env=env, root=root, env_filtered=env_filtered,
                              inherit_reason=inherit_reason, protected=protected,
                              declares_egress=declares_egress, sandbox_enabled=sandbox_enabled,
                              mode=mode)
    if mode == "bounded":
        if timeout is None:
            raise SandboxRefused("bounded spawn requires a timeout")
        kwargs.setdefault("job_limits", DEFAULT_JOB_LIMITS)  # P2 caps (nt); explicit None opts out
        proc = processes.run_process_tree_bounded(argv, timeout=timeout, cwd=str(cwd),
                                                  env=env, **kwargs)
        # SPEC-163 M2 (invariant 2): confirm the per-spawn APPLICATION fact here -- the
        # bounded seam is the only one that assigns a Job Object. procEnforced = host CAN;
        # procApplied = THIS spawn entered a Job Object. Async/detached never reach this
        # branch, so their procApplied stays the honest default False (invariant 3).
        receipt["procApplied"] = getattr(proc, "proc_applied", False)
        proc.sandbox_receipt = receipt  # type: ignore[attr-defined]
        return proc
    if mode == "detached":
        if log_path is None:
            raise SandboxRefused("detached spawn requires a log_path")
        pid = processes.launch_detached(argv, log_path=log_path, cwd=cwd, env=env)
        return {"pid": pid, "sandboxReceipt": receipt}
    raise SandboxRefused(f"unknown spawn mode {mode!r}")


# The closed set of autonomous worker-spawn functions that MUST reference this
# module (SPEC-148 rule 1). New worker paths land here in the same commit; new
# raw spawn sites are already caught by the raw-subprocess ratchet.
CHOKEPOINT_SITES: tuple[tuple[str, str], ...] = (
    ("scripts/harness_lib/workflow_run.py", "run_one_worker"),
    ("scripts/harness.py", "cmd_route"),
    ("scripts/harness_lib/async_runtime.py", "workflow_async_run_one_worker"),
)


def evaluate_chokepoint(root: Path | str) -> dict[str, list[str]]:
    """Gate check: each chokepoint site references sandbox_spawn/sandbox_prepare."""
    missing: list[str] = []
    for rel, func in CHOKEPOINT_SITES:
        ok = False
        try:
            tree = ast.parse((Path(root) / rel).read_text(encoding="utf-8", errors="ignore"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func:
                    names = ({n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
                             | {n.id for n in ast.walk(node) if isinstance(n, ast.Name)})
                    ok = bool({"sandbox_spawn", "sandbox_prepare"} & names)
                    break
        except Exception:
            ok = False
        if not ok:
            missing.append(f"{rel}::{func}")
    return {"missing": missing, "sites": [f"{r}::{f}" for r, f in CHOKEPOINT_SITES]}


if __name__ == "__main__":
    import tempfile

    _root = Path(tempfile.mkdtemp(prefix="sbx-root-"))
    _ws = Path(tempfile.mkdtemp(prefix="sbx-ws-"))
    try:
        # N5 risk tiers (rule 13): pure closed-vocabulary table.
        assert risk_tier(write_allowed=False) == "R0" and risk_tier(write_allowed=True) == "R1"
        assert risk_tier(write_allowed=False, risk_flags=["payment"]) == "R3"
        assert risk_tier(write_allowed=False, risk_flags=["rule-of-two"]) == "R2"
        assert set(RISK_TIERS) == {"R0", "R1", "R2", "R3"}

        # R0: no fs work on any OS, receipt says so.
        r0 = sandbox_prepare(vendor="claude", write_allowed=False, cwd=_root, env={},
                             root=_root)
        assert r0["tier"] == "R0" and r0["fsApplied"] == [], r0

        # SPEC-151 invariant 3: every receipt carries the legible manifest.
        caps = sandbox_capabilities()
        assert set(caps) == {"proc", "fs", "egress", "os", "reason"}, caps
        assert all(k in r0 for k in ("procEnforced", "fsEnforced", "egressEnforced", "reason")), r0
        assert set(manifest(r0)) <= set(MANIFEST_KEYS) and "reason" in manifest(r0), manifest(r0)
        # SPEC-163 M2: the per-spawn application facts surface and default honest-False —
        # a receipt straight from prepare (no bounded seam) never ran a Job Object.
        assert manifest(r0)["procApplied"] is False and manifest(r0)["fsApplied"] == [], manifest(r0)

        # SPEC-163 v2 M4: reach/target stamp only under a declared mode, come from the
        # closed vocabulary, and every LIVE divergence is a DECLARED one.
        assert "treeKillReach" not in manifest(r0), manifest(r0)  # no mode -> no claim
        _plat = _kill_platform()
        assert all(tree_kill_reach(m) in TREE_KILL_REACH and tree_kill_target(m) in TREE_KILL_REACH
                   for m in ("bounded", "async", "detached"))
        _live = {f"{m}:{_plat}" for m in ("bounded", "async", "detached")
                 if tree_kill_reach(m) != tree_kill_target(m)}
        assert _live == {k for k in DECLARED_KILL_GAPS if k.endswith(f":{_plat}")}, _live
        _async = manifest(sandbox_prepare(vendor="claude", write_allowed=False, cwd=_root,
                                          env={}, root=_root, mode="async"))
        # SPEC-163 v4: nt reach moved pid -> job when the async timeout kill gained its
        # cage sweep, so reach now EQUALS target on every platform and DECLARED_KILL_GAPS
        # is empty (asserted above, which is why this pair must move with it).
        assert _async["treeKillReach"] == ("job" if os.name == "nt" else "session"), _async
        assert _async["treeKillTarget"] == ("job" if os.name == "nt" else "session"), _async
        print("tree-kill reach/target declaration self-check OK")

        # invariant 4: a task that DECLARES egress with no enforceable egress is refused;
        # the default (no declaration) proceeds with egressEnforced honestly recorded.
        _real_caps = sandbox_capabilities
        try:
            sandbox_capabilities = lambda: {"proc": True, "fs": True, "egress": False,  # noqa: E731
                                            "os": "nt", "reason": "egress needs admin (WFP)"}
            try:
                sandbox_prepare(vendor="local-llama", write_allowed=False, cwd=_root,
                                env={}, root=_root, declares_egress=True)
                raise AssertionError("declared egress with no enforceable egress must refuse")
            except SandboxRefused:
                pass
            adv = sandbox_prepare(vendor="local-llama", write_allowed=False, cwd=_root,
                                  env={}, root=_root, declares_egress=False)
            assert adv["egressEnforced"] is False and adv["egressDeclared"] is False, adv
            sandbox_capabilities = lambda: {"proc": True, "fs": True, "egress": True,  # noqa: E731
                                            "os": "nt", "reason": "all enforceable"}
            ok_eg = sandbox_prepare(vendor="local-llama", write_allowed=False, cwd=_root,
                                    env={}, root=_root, declares_egress=True)
            assert ok_eg["egressEnforced"] is True and ok_eg["egressDeclared"] is True, ok_eg
        finally:
            sandbox_capabilities = _real_caps

        # invariant 5: break-glass records the bypass, never silent — no fs work, no refuse.
        byp = sandbox_prepare(vendor="codex", write_allowed=True, cwd=_ws, env={},
                              root=_root, sandbox_enabled=False, declares_egress=True,
                              protected=["AGENTS.md"])
        assert byp["sandboxBypassed"] is True and byp["fsApplied"] == [], byp
        assert byp["procApplied"] is False, byp  # SPEC-163 M2: bypass applies nothing
        print("manifest/egress-honesty/break-glass self-check OK")

        # Unfiltered env refused without a reason; receipted with one.
        try:
            sandbox_prepare(vendor="claude", write_allowed=False, cwd=_root, env=None,
                            root=_root, env_filtered=False)
            raise AssertionError("unfiltered env must refuse")
        except SandboxRefused:
            pass
        r_room = sandbox_prepare(vendor="claude", write_allowed=False, cwd=_root, env=None,
                                 root=_root, env_filtered=False,
                                 inherit_reason="interactive owner room (Trilha S deferral)")
        assert r_room["envInheritReason"].startswith("interactive"), r_room

        # R1 with cwd == live tree refuses (no isolated workspace).
        try:
            sandbox_prepare(vendor="codex", write_allowed=True, cwd=_root, env={},
                            root=_root, protected=[])
            raise AssertionError("cwd==root write worker must refuse")
        except SandboxRefused:
            pass

        # Fail closed on a backend-less OS; owner override proceeds loudly, receipted.
        _real_backend = fs_backend
        try:
            fs_backend = lambda: None  # noqa: E731 — simulate an OS without a backend
            try:
                sandbox_prepare(vendor="codex", write_allowed=True, cwd=_ws, env={},
                                root=_root, protected=[])
                raise AssertionError("missing backend must refuse R1+")
            except SandboxRefused:
                pass
            os.environ[OVERRIDE_ENV] = "self-check override"
            ro = sandbox_prepare(vendor="codex", write_allowed=True, cwd=_ws, env={},
                                 root=_root, protected=[])
            assert ro["overrides"][0]["ownerToken"] == "self-check override", ro
        finally:
            fs_backend = _real_backend
            os.environ.pop(OVERRIDE_ENV, None)
        print("tier/fail-closed/override self-check OK")

        if os.name == "nt":
            prot = _ws / "AGENTS.md"
            prot.write_text("canonical\n", encoding="utf-8")
            r1 = sandbox_prepare(vendor="codex", write_allowed=True, cwd=_ws, env={},
                                 root=_root, protected=["AGENTS.md", "missing.md"])
            assert r1["fsApplied"] == ["AGENTS.md"] and r1["fsBackend"] == "nt-icacls", r1
            for op in (lambda: prot.open("a").close(), lambda: prot.unlink()):
                try:
                    op()
                    raise AssertionError("deny-write ACL did not block mutation")
                except PermissionError:
                    pass
            assert prot.read_text(encoding="utf-8") == "canonical\n"  # Option A: readable
            assert fs_release(_ws)
            prot.open("a").close()  # writable again for disposal
            print("nt icacls deny/release self-check OK")

        live = evaluate_chokepoint(Path(__file__).resolve().parents[2])
        assert live["missing"] == [], f"unrouted chokepoint site(s): {live['missing']}"
        print("chokepoint routing self-check OK")
    finally:
        import shutil
        fs_release(_ws)
        shutil.rmtree(_ws, ignore_errors=True)
        shutil.rmtree(_root, ignore_errors=True)
