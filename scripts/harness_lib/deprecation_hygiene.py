"""Deprecation hygiene checks for the universal harness.

The harness may carry compatibility shims for a short time, but stale aliases
and old command names must not keep appearing in active docs/config/CLI after a
burn-down retires them.  This module keeps that policy executable and small.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

CONFIG_REL = "testing/deprecation-hygiene.json"


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""


def load_config(root: Path) -> dict[str, Any]:
    data = _read_json(root / CONFIG_REL, {}) or {}
    return data if isinstance(data, dict) else {}


def _contains_token(text: str, token: str) -> bool:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token):
        return re.search(rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])", text) is not None
    return token in text


def evaluate(root: Path) -> list[dict[str, str]]:
    cfg = load_config(root)
    checks: list[dict[str, str]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "status": "pass" if ok else "fail", "detail": detail})

    config_path = root / CONFIG_REL
    add("deprecation-hygiene:config", config_path.exists(), CONFIG_REL if config_path.exists() else "missing")

    retired_files = [str(item) for item in cfg.get("retiredFiles", [])]
    present = [rel for rel in retired_files if (root / rel).exists()]
    add("deprecation-hygiene:retired-files-absent", not present, "absent" if not present else ", ".join(present[:10]))

    harness_text = _read_text(root / "scripts" / "harness.py")
    retired_cli = [str(item) for item in cfg.get("retiredCliCommands", [])]
    cli_hits = [cmd for cmd in retired_cli if cmd in harness_text]
    add("deprecation-hygiene:retired-cli-absent", not cli_hits, "absent" if not cli_hits else ", ".join(cli_hits))

    project = _read_json(root / ".harness" / "project.json", {}) or {}
    project_text = json.dumps(project, sort_keys=True)
    retired_keys = [str(item) for item in cfg.get("retiredConfigKeys", [])]
    key_hits = [key for key in retired_keys if key.split(".")[-1] in project_text]
    add("deprecation-hygiene:retired-config-absent", not key_hits, "absent" if not key_hits else ", ".join(key_hits))

    active_docs = [str(item) for item in cfg.get("activeDocs", [])]
    retired_phrases = [str(item) for item in cfg.get("retiredPhrases", [])]
    doc_hits: list[str] = []
    missing_docs: list[str] = []
    for rel in active_docs:
        path = root / rel
        if not path.exists():
            missing_docs.append(rel)
            continue
        text = _read_text(path)
        for phrase in retired_phrases:
            if _contains_token(text, phrase):
                doc_hits.append(f"{rel}:{phrase}")
    add("deprecation-hygiene:active-docs-exist", not missing_docs, "present" if not missing_docs else ", ".join(missing_docs[:10]))
    add("deprecation-hygiene:active-docs-no-retired-terms", not doc_hits, "clean" if not doc_hits else "; ".join(doc_hits[:10]))

    code_scan_files = [root / "scripts" / "harness.py", root / "scripts" / "harness_lib" / "graphify_code_ast.py"]
    code_hits: list[str] = []
    for path in code_scan_files:
        text = _read_text(path)
        for phrase in retired_phrases:
            if _contains_token(text, phrase):
                code_hits.append(f"{path.relative_to(root)}:{phrase}")
    add("deprecation-hygiene:runtime-code-no-retired-terms", not code_hits, "clean" if not code_hits else "; ".join(code_hits[:10]))

    return checks
