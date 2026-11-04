"""Baseline catalog consistency helpers.

The universal baseline is intentionally split across several small specs so
agents can load only what a task profile needs.  This module keeps the catalog
consistent without importing the validation gate or depending on PyYAML.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def read_json(path: Path, default: Any = None) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def manifest_required_baseline(manifest_path: Path) -> list[str]:
    """Return ``requiredBaseline`` entries from ``specs/MANIFEST.yaml``.

    The harness template should not require PyYAML at runtime.  The manifest
    shape used here is intentionally simple: a top-level ``requiredBaseline``
    key followed by ``- path`` entries until the next top-level key.
    """
    if not manifest_path.exists():
        return []
    entries: list[str] = []
    in_required = False
    for raw in manifest_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.rstrip()
        if re.match(r"^requiredBaseline\s*:", line):
            in_required = True
            continue
        if in_required and line and not line.startswith((" ", "\t", "-")):
            break
        if not in_required:
            continue
        match = re.match(r"^\s*-\s+(.+?)\s*$", line)
        if match:
            entries.append(match.group(1).strip().strip('"\''))
    return entries


def project_required_specs(project_path: Path) -> list[str]:
    project = read_json(project_path, {}) or {}
    universal = project.get("universalSpecs") if isinstance(project.get("universalSpecs"), dict) else {}
    required = universal.get("required") if isinstance(universal, dict) else []
    return [str(item) for item in required or []]


def missing_references(text_path: Path, required: list[str]) -> list[str]:
    if not text_path.exists():
        return [f"missing file {text_path.name}"]
    text = text_path.read_text(encoding="utf-8", errors="ignore")
    return [item for item in required if item not in text]


def duplicate_items(items: list[str]) -> list[str]:
    seen: set[str] = set()
    dupes: list[str] = []
    for item in items:
        if item in seen and item not in dupes:
            dupes.append(item)
        seen.add(item)
    return dupes


def universal_spec_paths(root: Path) -> list[str]:
    configured = project_required_specs(root / ".harness" / "project.json")
    if configured:
        return configured
    manifest = manifest_required_baseline(root / "specs" / "MANIFEST.yaml")
    return manifest


def evaluate_universal_spec_files(root: Path, required_sections: list[str] | None = None) -> list[dict[str, str]]:
    """Evaluate universal baseline spec files and manifest wiring."""
    required_sections = required_sections or [
        "## Goal",
        "## Applies to",
        "## Invariants",
        "## Agent behavior",
        "## Validation evidence",
        "## Escalation triggers",
        "## Reference anchors",
    ]
    checks: list[dict[str, str]] = []
    manifest_text = (root / "specs" / "MANIFEST.yaml").read_text(encoding="utf-8", errors="ignore") if (root / "specs" / "MANIFEST.yaml").exists() else ""
    for item in universal_spec_paths(root):
        path = root / item
        if not path.exists():
            checks.append({"name": f"universal-spec:{item}", "status": "fail", "detail": "missing"})
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        missing_sections = [section for section in required_sections if section not in text]
        checks.append({
            "name": f"universal-spec:{item}",
            "status": "pass" if not missing_sections else "fail",
            "detail": "has required sections" if not missing_sections else "missing sections: " + ", ".join(missing_sections),
        })
        checks.append({
            "name": f"manifest-lists:{item}",
            "status": "pass" if item in manifest_text else "fail",
            "detail": "listed" if item in manifest_text else "not listed in specs/MANIFEST.yaml",
        })
    return checks


def evaluate(root: Path, config: dict[str, Any]) -> list[dict[str, str]]:
    """Evaluate catalog/spec-index consistency checks."""
    checks: list[dict[str, str]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "status": "pass" if ok else "fail", "detail": detail})

    manifest_entries = manifest_required_baseline(root / "specs" / "MANIFEST.yaml")
    project_entries = project_required_specs(root / ".harness" / "project.json")
    manifest_set = set(manifest_entries)
    project_set = set(project_entries)

    missing_in_project = [item for item in manifest_entries if item not in project_set]
    missing_in_manifest = [item for item in project_entries if item not in manifest_set]
    duplicates = duplicate_items(manifest_entries) + duplicate_items(project_entries)
    add(
        "engineering-guardrails:baseline-catalog-match",
        bool(manifest_entries) and not missing_in_project and not missing_in_manifest and not duplicates,
        "manifest/project baseline catalogs match"
        if manifest_entries and not missing_in_project and not missing_in_manifest and not duplicates
        else "; ".join(
            [
                *(f"project missing {item}" for item in missing_in_project[:8]),
                *(f"manifest missing {item}" for item in missing_in_manifest[:8]),
                *(f"duplicate {item}" for item in duplicates[:8]),
            ]
        ),
    )

    docs = config.get("requiredBaselineDocs") or []
    doc_missing: list[str] = []
    for rel in docs:
        doc_missing.extend(f"{rel} missing {item}" for item in missing_references(root / str(rel), manifest_entries))
    add(
        "engineering-guardrails:baseline-doc-indexes",
        not doc_missing,
        "baseline docs list all required specs" if not doc_missing else "; ".join(doc_missing[:10]),
    )

    required_anchors = [str(item) for item in config.get("requiredReferenceAnchors", []) or []]
    anchor_files = [root / "specs" / "MANIFEST.yaml", *[root / str(rel) for rel in docs]]
    anchor_text = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in anchor_files if path.exists())
    missing_anchors = [anchor for anchor in required_anchors if anchor not in anchor_text]
    add(
        "engineering-guardrails:reference-anchor-index",
        not missing_anchors,
        "reference anchors indexed" if not missing_anchors else "missing anchors: " + ", ".join(missing_anchors),
    )
    return checks
