#!/usr/bin/env python3
"""M5.mem — the memory snapshot: what an agent will actually find, by
scope × purpose.

A CLOSED registry of memory/ledger sources (user auto-memory, harness state
ledgers, the always-in-context CONTEXT head, the derived records index) —
each row `{id, scope, purpose, path, exists, mtime, size, preview}`. Every
preview and full body is SECRET-REDACTED server-side
(`secret_scan.redact_text`) before it leaves the process — MEMORY.md has
already carried a leaked key once; the client is never trusted.

`memory_file(root, source_id)` serves ONE registered source's full text —
closed id set, no free paths (the `_safe_log` discipline); the binary records
index reports metadata only. The user-scope resolver derives the Claude Code
project-memory dir from the repo path slug and degrades calmly when absent
(user/machine scope — absence is normal on CI).

Consumed by `GET /api/memory` + `/api/memory/file` and the Memory panel view;
CLI parity = `records sources`. Target-scope cards (M-A3) and change pulses
(M-A4) stay OPEN. Self-check: `python scripts/harness_lib/ui_memory.py`.
"""
from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:  # allow the standalone self-check to import siblings
    sys.path.insert(0, str(_SCRIPTS))
from harness_lib import secret_scan  # noqa: E402

PREVIEW_LINES = 10


def user_memory_dir(root: Path | str, home: Path | str | None = None) -> Path:
    """The Claude Code per-project auto-memory dir: home/.claude/projects/
    <slug>/memory, slug = repo path with each [:\\/] char replaced by '-'
    (C:\\projects\\x → C--projects-x — per-char, never collapsed)."""
    slug = re.sub(r"[:\\/]", "-", str(Path(root)))
    return Path(home or Path.home()) / ".claude" / "projects" / slug / "memory"


def _sources(root: Path, home: Path | None = None) -> list[dict[str, Any]]:
    state = root / ".harness" / "state"
    mem = user_memory_dir(root, home)
    return [
        {"id": "user:memory-index", "scope": "user", "purpose": "preferences / lessons",
         "path": mem / "MEMORY.md", "note": "machine-local auto-memory, not repo state"},
        {"id": "harness:context-head", "scope": "harness", "purpose": "always-in-context head",
         "path": root / ".harness" / "context" / "CONTEXT.md"},
        {"id": "harness:worklog", "scope": "harness", "purpose": "decisions / milestones / changes",
         "path": state / "worklog.json"},
        {"id": "harness:worklog-archive", "scope": "harness", "purpose": "decisions (archive)",
         "path": state / "worklog-archive.json"},
        {"id": "harness:escalations", "scope": "harness", "purpose": "escalations",
         "path": state / "escalations.json"},
        {"id": "harness:cost-metrics", "scope": "harness", "purpose": "cost",
         "path": state / "cost-metrics.json"},
        {"id": "harness:token-calibration", "scope": "harness", "purpose": "cost",
         "path": state / "token-calibration.json"},
        {"id": "harness:self-review", "scope": "harness", "purpose": "self-review findings",
         "path": state / "self-review.json"},
        {"id": "harness:quality-state", "scope": "harness", "purpose": "validation memory",
         "path": state / "quality-state.json"},
        {"id": "harness:records-index", "scope": "harness", "purpose": "retrieval index (derived)",
         "path": root / ".harness" / "state-store" / "records.db", "binary": True},
    ]


def _preview(path: Path) -> str:
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    if len(lines) <= PREVIEW_LINES * 2:
        body = "\n".join(lines)
    else:
        body = "\n".join(lines[:PREVIEW_LINES] + [f"…[{len(lines) - 2 * PREVIEW_LINES} lines]…"]
                         + lines[-PREVIEW_LINES:])
    return secret_scan.redact_text(body)


def memory_snapshot(root: Path | str, home: Path | None = None) -> dict[str, Any]:
    root = Path(root)
    rows: list[dict[str, Any]] = []
    for src in _sources(root, home):
        path: Path = src["path"]
        row: dict[str, Any] = {"id": src["id"], "scope": src["scope"],
                               "purpose": src["purpose"], "note": src.get("note"),
                               "path": str(path), "exists": False, "mtime": None,
                               "size": None, "preview": None}
        try:
            st = path.stat()
            row.update({"exists": True, "size": st.st_size,
                        "mtime": dt.datetime.fromtimestamp(
                            st.st_mtime).astimezone().isoformat(timespec="seconds")})
            if not src.get("binary"):
                row["preview"] = _preview(path)
        except OSError:
            pass
        except Exception as exc:  # per-source degrade, never a crash
            row["error"] = f"{type(exc).__name__}"
        rows.append(row)
    return {"generatedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "sources": rows}


def memory_file(root: Path | str, source_id: str,
                home: Path | None = None) -> dict[str, Any]:
    """Full text of ONE registered source — closed id set, redacted."""
    root = Path(root)
    src = next((s for s in _sources(root, home) if s["id"] == source_id), None)
    if src is None:
        return {"error": f"unknown memory source: {source_id}", "id": source_id}
    if src.get("binary"):
        return {"error": "derived binary index (no text view; metadata only)",
                "id": source_id}
    path: Path = src["path"]
    if not path.is_file():
        return {"error": "source not present on this machine", "id": source_id}
    text = path.read_text(encoding="utf-8", errors="ignore")
    return {"id": source_id, "path": str(path),
            "body": secret_scan.redact_text(text)}


def render_sources(root: Path | str) -> str:
    """`records sources` — the same scope × purpose table, for the terminal."""
    rows = memory_snapshot(root)["sources"]
    out = [f"{'ID':<26} {'SCOPE':<8} {'PURPOSE':<30} {'STATE':<8} PATH"]
    for r in rows:
        state = "present" if r["exists"] else "absent"
        out.append(f"{r['id']:<26} {r['scope']:<8} {r['purpose']:<30} {state:<8} {r['path']}")
    out.append(f"\n{sum(1 for r in rows if r['exists'])}/{len(rows)} sources present; "
               "panel Memory view shows redacted previews")
    return "\n".join(out)


def _demo() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        home = Path(tmp) / "home"
        state = root / ".harness" / "state"
        state.mkdir(parents=True)
        (state / "worklog.json").write_text(
            '{"entries": [{"note": "sk-FAKEKEYdonotleak0123456789abcdef"}]}',
            encoding="utf-8")
        mem = user_memory_dir(root, home)
        mem.mkdir(parents=True)
        (mem / "MEMORY.md").write_text("- lesson one\n", encoding="utf-8")
        snap = memory_snapshot(root, home)
        by_id = {r["id"]: r for r in snap["sources"]}
        assert by_id["harness:worklog"]["exists"] and by_id["user:memory-index"]["exists"]
        assert by_id["harness:escalations"]["exists"] is False  # calm absence
        blob = str(snap)
        assert "sk-FAKEKEYdonotleak" not in blob and "sk-F…(len=" in blob, "leak!"
        full = memory_file(root, "harness:worklog")
        assert "sk-FAKEKEYdonotleak" not in full["body"]
        assert "unknown memory source" in memory_file(root, "ghost")["error"]
        assert "binary index" in memory_file(root, "harness:records-index")["error"]
        slug_dir = user_memory_dir(Path(r"C:\projects\universal-agent-harness-prototype"),
                                   Path("/h"))
        assert slug_dir.parts[-2] == "C--projects-universal-agent-harness-prototype", slug_dir
    print("ui_memory self-check: ok")


if __name__ == "__main__":
    _demo()
