#!/usr/bin/env python3
"""State Authority Map loader and validator.

SPEC-175: Formalizes state authority across harness state domains into a
machine-readable registry (`.harness/state-authority.json`), validating owner
modules, allowed writers, canonical stores, and detecting domain collisions.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def get_map_path(root: Path | str | None = None) -> Path:
    if root is not None:
        return Path(root) / ".harness" / "state-authority.json"
    env_root = os.environ.get("HARNESS_ROOT")
    if env_root:
        return Path(env_root) / ".harness" / "state-authority.json"
    base = Path.cwd()
    if (base / ".harness").exists():
        return base / ".harness" / "state-authority.json"
    parents = Path(__file__).resolve().parents
    if len(parents) >= 3:
        return parents[2] / ".harness" / "state-authority.json"
    return base / ".harness" / "state-authority.json"


def load_state_authority_map(root: Path | str | None = None) -> dict[str, Any]:
    path = get_map_path(root)
    if not path.exists():
        return {"schemaVersion": "1.0", "domains": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"schemaVersion": "1.0", "domains": [], "error": str(e)}


def validate_state_authority(
    root: Path | str | None = None,
    map_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate state authority map against repository source code and paths.
    
    Returns:
        {"ok": bool, "issues": list[str], "domainCount": int}
    """
    r_path = Path(root) if root else get_map_path().parents[1]
    if map_data is None:
        map_data = load_state_authority_map(r_path)

    
    issues: list[str] = []
    domains = map_data.get("domains")
    if not isinstance(domains, list):
        return {"ok": False, "issues": ["Invalid state-authority.json: 'domains' must be a list"], "domainCount": 0}
        
    canonical_store_owners: dict[str, str] = {}
    
    for idx, d in enumerate(domains):
        if not isinstance(d, dict):
            issues.append(f"Domain entry at index {idx} is not an object")
            continue
            
        domain_name = d.get("domain")
        if not domain_name or not isinstance(domain_name, str):
            issues.append(f"Domain entry at index {idx} missing valid 'domain' name")
            continue
            
        owner = d.get("ownerModule")
        if not owner or not isinstance(owner, str):
            issues.append(f"Domain '{domain_name}' missing valid 'ownerModule'")
        else:
            owner_path = r_path / owner
            if not owner_path.exists():
                issues.append(f"Domain '{domain_name}' ownerModule '{owner}' does not exist on disk")
                
        writers = d.get("allowedWriters")
        if not isinstance(writers, list):
            issues.append(f"Domain '{domain_name}' allowedWriters must be a list")
        else:
            for w in writers:
                if isinstance(w, str):
                    w_path = r_path / w
                    if not w_path.exists():
                        issues.append(f"Domain '{domain_name}' allowedWriter '{w}' does not exist on disk")
                        
        store = d.get("canonicalStore")
        if not store or not isinstance(store, str):
            issues.append(f"Domain '{domain_name}' missing valid 'canonicalStore'")
        else:
            if store in canonical_store_owners:
                issues.append(
                    f"Conflict: canonicalStore '{store}' claimed by both '{canonical_store_owners[store]}' and '{domain_name}'"
                )
            else:
                canonical_store_owners[store] = domain_name

    return {
        "ok": len(issues) == 0,
        "issues": issues,
        "domainCount": len(domains),
    }
