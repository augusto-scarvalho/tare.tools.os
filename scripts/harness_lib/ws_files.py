"""SPEC-147 workspace file surface (inv 6-9): confinement + read/save + ruff lint.

The panel's first file-WRITE surface. The trust boundary is
:func:`resolve_confined` — repo-relative POSIX input only, the symlink-resolved
result must land under the repo root, and the deny list covers `.harness/`,
`.git/`, `vendor/` plus the protected-instruction registry
(harness_lib.protected_files). Mutations enter ONLY through ui_actions'
`ws:` handler (confirm + human-only backstop + gate-in-flight guard inherited);
the read/lint routes in harness_ui are read-shaped.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from . import protected_files
    from .processes import run_quiet
except ImportError:  # run directly
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from harness_lib import protected_files
    from harness_lib.processes import run_quiet

DENY_PREFIXES = (".harness/", ".git/", "vendor/")
# Secrets denylist (audit 2026-07-22 CRITICAL: the Code explorer rendered .env
# plaintext, live keys included, through this shared seam — GUI React AND legacy
# panel). Name-level deny, checked case-insensitively against basename and path:
# no secret-bearing file is listable or readable through the workspace API,
# regardless of which UI asks. Extend HERE, never in a UI layer.
DENY_NAME_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore",
    "id_rsa*", "id_ed25519*", "*.credentials", "credentials*", "secrets*",
    ".mcp.json", ".npmrc", ".netrc",
)
# Explicitly NOT denied: *.example / *.sample name forms (see _secret_like).
MAX_READ = 2 * 1024 * 1024  # inv 7: refuse >2MB reads
LINT_TIMEOUT = 10


def _secret_like(inside: str) -> bool:
    """True when the path names a secret-bearing file (DENY_NAME_PATTERNS).
    Basename match, case-insensitive; .example/.sample forms stay visible —
    they are documentation, not credentials."""
    name = inside.rsplit("/", 1)[-1].lower()
    if name.endswith((".example", ".sample")):
        return False
    return any(fnmatch.fnmatch(name, pat) for pat in DENY_NAME_PATTERNS)


def resolve_confined(root: Path, rel: Any) -> tuple[Path | None, str]:
    """(path, "") when `rel` is a safe repo-relative POSIX path; (None, reason) otherwise."""
    if not isinstance(rel, str) or not rel.strip():
        return None, "path required"
    if "\\" in rel or ":" in rel or rel.startswith(("/", "~")):
        return None, "repo-relative POSIX path required (no absolute paths, drives, or backslashes)"
    root = root.resolve()
    try:
        path = (root / rel).resolve()
        inside = path.relative_to(root).as_posix()
    except (ValueError, OSError):
        return None, "path escapes the repository root"
    if inside == "." or any(inside == p.rstrip("/") or inside.startswith(p) for p in DENY_PREFIXES):
        return None, f"path is deny-listed for the workspace ({inside})"
    if _secret_like(inside):
        return None, "path matches the secrets denylist"
    protected = set(protected_files.protected_paths(root))
    patterns = protected_files.write_block_patterns(root)
    if inside in protected or any(fnmatch.fnmatch(inside, pat) for pat in patterns):
        return None, f"protected instruction file ({inside}) — edit it through the sanctioned flow"
    return path, ""


def read_file(root: Path, rel: Any) -> dict[str, Any]:
    path, bad = resolve_confined(root, rel)
    if path is None:
        return {"ok": False, "error": bad}
    if not path.is_file():
        return {"ok": False, "error": "not a file"}
    size = path.stat().st_size
    if size > MAX_READ:
        return {"ok": False, "error": f"file too large for the editor ({size} bytes > {MAX_READ})"}
    data = path.read_bytes()
    if b"\x00" in data[:8192]:
        return {"ok": False, "error": "binary file"}
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        return {"ok": False, "error": "not valid UTF-8"}
    return {"ok": True, "path": path.relative_to(root.resolve()).as_posix(),
            "content": content, "sha256": hashlib.sha256(data).hexdigest(), "size": size}


def save_file(root: Path, rel: Any, content: Any, base_sha: Any,
              overwrite: bool = False) -> dict[str, Any]:
    """inv 8: conflict-safe save. `base_sha` = sha256 of the bytes the editor
    loaded; a disk mismatch returns conflict:true unless `overwrite` (the
    second-confirm path). New files go through ws-file-create (M3), not save."""
    path, bad = resolve_confined(root, rel)
    if path is None:
        return {"ok": False, "error": bad}
    if not isinstance(content, str):
        return {"ok": False, "error": "content required"}
    if not path.is_file():
        return {"ok": False, "error": "file does not exist (create it via the explorer)"}
    disk_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    if disk_sha != base_sha and not overwrite:
        return {"ok": False, "conflict": True, "currentSha": disk_sha,
                "error": "file changed on disk since it was opened"}
    data = content.encode("utf-8")
    path.write_bytes(data)
    return {"ok": True, "path": path.relative_to(root.resolve()).as_posix(),
            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def list_dir(root: Path, rel: Any = "") -> dict[str, Any]:
    """M3 explorer: ONE directory level (lazy expand — no full walk). Deny-listed
    and protected children are omitted, not errors; the repo root itself lists."""
    root = root.resolve()
    if rel in ("", ".", None):
        path, inside = root, ""
    else:
        p, bad = resolve_confined(root, rel)
        if p is None:
            return {"ok": False, "error": bad}
        path, inside = p, p.relative_to(root).as_posix()
    if not path.is_dir():
        return {"ok": False, "error": "not a directory"}
    dirs: list[str] = []
    files: list[dict[str, Any]] = []
    try:
        entries = sorted(path.iterdir(), key=lambda e: e.name.lower())
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    for entry in entries:
        child_rel = f"{inside}/{entry.name}" if inside else entry.name
        if resolve_confined(root, child_rel)[0] is None:
            continue  # deny-listed / protected children are invisible to the explorer
        if entry.is_dir():
            dirs.append(entry.name)
        elif entry.is_file():
            files.append({"name": entry.name, "size": entry.stat().st_size})
    return {"ok": True, "path": inside, "dirs": dirs, "files": files}


def create_file(root: Path, rel: Any) -> dict[str, Any]:
    path, bad = resolve_confined(root, rel)
    if path is None:
        return {"ok": False, "error": bad}
    if path.exists():
        return {"ok": False, "error": "already exists"}
    path.parent.mkdir(parents=True, exist_ok=True)  # parents stay confined (child was)
    path.write_bytes(b"")
    return {"ok": True, "path": path.relative_to(root.resolve()).as_posix()}


def rename_file(root: Path, rel: Any, new_rel: Any) -> dict[str, Any]:
    src, bad = resolve_confined(root, rel)
    if src is None:
        return {"ok": False, "error": bad}
    dst, bad2 = resolve_confined(root, new_rel)
    if dst is None:
        return {"ok": False, "error": bad2}
    if not src.is_file():
        return {"ok": False, "error": "not a file"}
    if dst.exists():
        return {"ok": False, "error": "target already exists"}
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    return {"ok": True, "path": dst.relative_to(root.resolve()).as_posix()}


def delete_file(root: Path, rel: Any) -> dict[str, Any]:
    path, bad = resolve_confined(root, rel)
    if path is None:
        return {"ok": False, "error": bad}
    if not path.is_file():
        return {"ok": False, "error": "not a file"}  # ponytail: files only, dir ops YAGNI
    path.unlink()
    return {"ok": True, "path": rel}


def run_ws_action(root: Path, action: str, params: dict[str, Any]) -> dict[str, Any]:
    """In-process dispatch for the ws-file-* ACTIONS verbs (confirm/backstop/
    gate-guard already applied by run_action)."""
    if action == "ws-file-save":
        return {**save_file(root, params.get("path"), params.get("content"),
                            params.get("baseSha"), overwrite=params.get("overwrite") is True),
                "action": action}
    if action == "ws-file-create":
        return {**create_file(root, params.get("path")), "action": action}
    if action == "ws-file-rename":
        return {**rename_file(root, params.get("path"), params.get("newPath")), "action": action}
    if action == "ws-file-delete":
        return {**delete_file(root, params.get("path")), "action": action}
    return {"ok": False, "action": action, "error": "unknown ws action"}


_RUFF_TRIED_INSTALL = False


def _ruff_run(root: Path, args: list[str], stdin: str) -> subprocess.CompletedProcess:
    return run_quiet([sys.executable, "-m", "ruff", *args], cwd=str(root),
                     input=stdin, capture_output=True, text=True,
                     encoding="utf-8", errors="replace", timeout=LINT_TIMEOUT)


def lint_content(root: Path, rel: Any, content: Any) -> dict[str, Any]:
    """inv 9: ruff-as-a-service over stdin. Missing ruff => one zero-touch
    `uv pip install ruff` attempt (graceful offline), then {ok:false} — the
    front disables lint; the gate never requires ruff."""
    global _RUFF_TRIED_INSTALL
    if not isinstance(rel, str) or not rel.endswith(".py"):
        return {"ok": False, "error": "lint is .py-only"}
    if not isinstance(content, str):
        return {"ok": False, "error": "content required"}
    try:
        res = _ruff_run(root, ["check", "--output-format", "json",
                               "--stdin-filename", rel, "-"], content)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"ruff timed out after {LINT_TIMEOUT}s"}
    except OSError as exc:
        return {"ok": False, "error": f"ruff unavailable: {exc}"}
    if "No module named ruff" in (res.stderr or ""):
        if not _RUFF_TRIED_INSTALL:
            _RUFF_TRIED_INSTALL = True  # once per server process; offline stays graceful
            with_env = {"VIRTUAL_ENV": str(root / ".venv")}
            try:
                import os
                run_quiet(["uv", "pip", "install", "ruff"], check=True, timeout=120,
                          capture_output=True, env={**os.environ, **with_env})
                return lint_content(root, rel, content)
            except Exception:  # noqa: BLE001 - zero-touch is best-effort
                pass
        return {"ok": False, "error": "ruff not installed"}
    out = (res.stdout or "").strip()
    if res.returncode not in (0, 1) or not out:
        # `ruff check --output-format json` prints a JSON array (even "[]" when clean) and exits 0
        # (clean) or 1 (violations). Exit >=2, or EMPTY stdout, means ruff never actually linted —
        # a crash or a Smart App Control block of the native binary (WinError 4551: the `ruff` module
        # is importable but its exe is denied, so `python -m ruff` dies with an empty stdout). Reading
        # that empty output as a clean all-clear was the bug; degrade so the front disables lint
        # (inv 9: the gate never requires ruff), never a false "no diagnostics".
        return {"ok": False, "error": f"ruff unavailable (rc={res.returncode}): {(res.stderr or '').strip()[:200]}"}
    try:
        rows = json.loads(out)
    except json.JSONDecodeError:
        return {"ok": False, "error": f"ruff output unparseable (rc={res.returncode})"}
    diags = [{"line": r.get("location", {}).get("row", 1),
              "col": r.get("location", {}).get("column", 1),
              "endLine": r.get("end_location", {}).get("row", r.get("location", {}).get("row", 1)),
              "endCol": r.get("end_location", {}).get("column", r.get("location", {}).get("column", 1)),
              "code": r.get("code") or "", "message": r.get("message") or ""}
             for r in rows if isinstance(r, dict)]
    version = ""
    try:
        version = _ruff_run(root, ["--version"], "").stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "diagnostics": diags, "ruff": version}


def format_content(root: Path, rel: Any, content: Any) -> dict[str, Any]:
    """v3: `ruff format` over stdin for .py (other languages reindent
    client-side via CM6). Same graceful rules as lint_content."""
    if not isinstance(rel, str) or not rel.endswith(".py"):
        return {"ok": False, "error": "format is .py-only (other languages reindent client-side)"}
    if not isinstance(content, str):
        return {"ok": False, "error": "content required"}
    try:
        res = _ruff_run(root, ["format", "--stdin-filename", rel, "-"], content)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"ruff timed out after {LINT_TIMEOUT}s"}
    except OSError as exc:
        return {"ok": False, "error": f"ruff unavailable: {exc}"}
    if "No module named ruff" in (res.stderr or ""):
        return {"ok": False, "error": "ruff not installed"}
    if res.returncode != 0:
        return {"ok": False, "error": (res.stderr or "format failed").strip()[:400]}
    return {"ok": True, "content": res.stdout}


# -- self-check (ponytail: the confinement table must fail loudly if it breaks) --
def _selfcheck() -> None:
    root = Path(__file__).resolve().parents[2]
    ok, _ = resolve_confined(root, "testing/scenarios/_lib.py")
    assert ok is not None, "repo-relative file must resolve"
    for bad in ("../x.py", "a/../../x.py", "/etc/passwd", "C:/x.py", "c:\\x.py",
                ".harness/state/task-state.json", ".git/config", "vendor/codemirror/manifest.json",
                "AGENTS.md", "", None, 42):
        path, reason = resolve_confined(root, bad)
        assert path is None, f"{bad!r} must be refused"
        assert reason, f"{bad!r} refusal must carry a reason"


if __name__ == "__main__":
    _selfcheck()
    print("ws_files selfcheck ok")
