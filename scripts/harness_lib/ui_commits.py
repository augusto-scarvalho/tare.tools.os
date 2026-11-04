#!/usr/bin/env python3
"""M5.rec — commit timeline + per-commit diff viewer collectors.

`commits_snapshot(root, branch=, q=, sha=, limit=)` is a thin, validated
`git log` wrapper (handles only: sha/date/author/subject/decorations) and
`commit_detail(root, sha)` returns `git show --stat --patch` capped and
SECRET-REDACTED server-side (the anchored secret_scan patterns, first-4+len
— nothing key-shaped leaves the API even on loopback). Filters are validated
against regexes and passed as single argv elements after `--`-style
separation — user input never lands in a flag position.

CLI parity is deliberate REUSE: `git log` itself and
`records recent --kind commit` already cover the read path (documented in the
spec; no new verb). Consumed by `GET /api/commits` / `GET /api/commit` and
the Changelog panel view. The on-demand LLM round summary is M5.sum (OPEN).

Self-check: `python scripts/harness_lib/ui_commits.py`.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))
from harness_lib import secret_scan  # noqa: E402

SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
BRANCH_RE = re.compile(r"^(?!-)(?!.*\.\.)[\w./-]{1,120}$")  # no leading -, no ..
MAX_DETAIL_BYTES = 200_000  # mirrors the highlighter cap
_FS, _RS = "\x1f", "\x1e"


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=30)
    return proc.stdout if proc.returncode == 0 else ""


def _redact(text: str) -> str:
    """Anchored secret shapes → first-4+len markers (values never leave)."""
    return secret_scan.redact_text(text)


def branches(root: Path | str) -> list[str]:
    out = _git(Path(root), "branch", "--format=%(refname:short)")
    return [b.strip() for b in out.splitlines() if b.strip()]


def commits_snapshot(root: Path | str, branch: str | None = None,
                     q: str | None = None, sha: str | None = None,
                     limit: int = 50) -> dict[str, Any]:
    """Validated commit timeline; invalid filters degrade to an error field."""
    root = Path(root)
    limit = max(1, min(int(limit or 50), 300))
    if branch and not BRANCH_RE.match(branch):
        return {"commits": [], "branches": branches(root), "error": "invalid branch filter"}
    if sha and not SHA_RE.match(sha):
        return {"commits": [], "branches": branches(root), "error": "invalid sha filter"}
    # %x1f/%x1e are git-side escapes (records.py precedent): literal control
    # bytes in argv get mangled by Windows CreateProcess command lines.
    args = ["log", f"-n{limit}", "--format=%H%x1f%aI%x1f%an%x1f%s%x1f%D%x1e"]
    if q:
        args.append("--grep=" + q)  # one argv element: input cannot become a flag
        args.append("--regexp-ignore-case")
    if sha:
        args.append(sha)
    elif branch:
        args.append(branch)
    out = _git(root, *args)
    rows: list[dict[str, str]] = []
    for record in out.split(_RS):
        # strip ONLY newlines/spaces: bare .strip() also eats \x1f/\x1e (they
        # are Python whitespace!), truncating records whose %D is empty.
        parts = record.strip("\r\n\t ").split(_FS)
        if len(parts) == 5:
            rows.append({"sha": parts[0], "date": parts[1], "author": parts[2],
                         "subject": _redact(parts[3])[:160], "decorations": parts[4]})
    return {"commits": rows, "branches": branches(root), "count": len(rows)}


def commit_detail(root: Path | str, sha: str) -> dict[str, Any]:
    """`git show --stat --patch <sha>` — sha-validated, capped, redacted."""
    root = Path(root)
    if not SHA_RE.match(sha or ""):
        return {"error": "invalid sha (expected 7-40 hex chars)", "sha": sha}
    out = _git(root, "show", "--stat", "--patch", "--no-color", sha)
    if not out:
        return {"error": "unknown commit (or git failed)", "sha": sha}
    truncated = len(out.encode("utf-8", "replace")) > MAX_DETAIL_BYTES
    if truncated:
        out = out[:MAX_DETAIL_BYTES] + "\n…[truncated]"
    return {"sha": sha, "body": _redact(out), "truncated": truncated}


def _demo() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)

        def git(*a: str) -> None:
            subprocess.run(["git", "-C", str(repo), *a], capture_output=True, timeout=30)

        git("init", "-q", "-b", "main")
        (repo / "a.txt").write_text("one\n", encoding="utf-8")
        git("add", "-A")
        git("-c", "user.name=x", "-c", "user.email=x@y", "commit", "-qm", "first commit")
        (repo / "b.txt").write_text('KEY = "sk-FAKEKEYdonotleak0123456789abcdef"\n',
                                    encoding="utf-8")
        git("add", "-A")
        git("-c", "user.name=x", "-c", "user.email=x@y", "commit", "-qm", "add fake key")
        snap = commits_snapshot(repo)
        assert snap["count"] == 2 and snap["branches"] == ["main"], snap
        grep = commits_snapshot(repo, q="fake key")
        assert grep["count"] == 1 and grep["commits"][0]["subject"] == "add fake key", grep
        assert commits_snapshot(repo, branch="../evil")["error"] == "invalid branch filter"
        detail = commit_detail(repo, snap["commits"][0]["sha"])
        assert "sk-FAKEKEYdonotleak" not in detail["body"], "secret leaked"
        assert "sk-F…(len=" in detail["body"], detail["body"][:200]
        assert commit_detail(repo, "--exec=evil")["error"].startswith("invalid sha")
        assert commit_detail(repo, "deadbeef")["error"].startswith("unknown commit")
    print("ui_commits self-check: ok")


if __name__ == "__main__":
    _demo()
