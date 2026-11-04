#!/usr/bin/env python3
"""
relay/snapshot_vault.py — Scoped Git & File Snapshot Isolation Vault
===================================================================
Provides atomic pre-implementation snapshots and one-step rollback capabilities.
Prevents uncommitted workspace pollution and guarantees instant recovery if an
implementer breaches bounded footprints or breaks invariants.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

RELAY_ROOT = Path(__file__).resolve().parent.parent
RELAY_DIR = RELAY_ROOT / "relay"
SNAPSHOTS_DIR = RELAY_DIR / "snapshots"

if str(RELAY_DIR) not in sys.path:
    sys.path.insert(0, str(RELAY_DIR))
import relay_io
from relay import relay_mesh


def create_train_snapshot(train_id: str, target_files: Optional[List[str]] = None) -> Path:
    """Captures an atomic snapshot of target files or git diff before implementation begins."""
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    snap_dir = SNAPSHOTS_DIR / train_id
    snap_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "train_id": train_id,
        "created_at": relay_mesh.now_iso(),
        "files": {},
        "git_head": "",
    }

    # Record git head commit if in git repo
    try:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(RELAY_ROOT),
                              capture_output=True, text=True, timeout=5,
                              stdin=subprocess.DEVNULL, **relay_io.safe_subprocess_kwargs())
        if proc.returncode == 0:
            manifest["git_head"] = proc.stdout.strip()
    except Exception:
        pass

    # Copy declared target files into snapshot directory
    if target_files:
        for tf in target_files:
            src = RELAY_ROOT / tf
            if src.exists() and src.is_file():
                dest = snap_dir / "files" / tf
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                manifest["files"][tf] = {
                    "sha256": relay_io.sha256_hex(src.read_bytes()),
                    "size": src.stat().st_size
                }

    relay_io.write_json_atomic(snap_dir / "MANIFEST.json", manifest)
    print(f"[SNAPSHOT VAULT] Created pre-implementation snapshot for {train_id} ({len(manifest['files'])} files captured).")
    return snap_dir


def restore_train_snapshot(train_id: str) -> bool:
    """Restores the captured snapshot for a train, reverting any unauthorized or broken file edits."""
    snap_dir = SNAPSHOTS_DIR / train_id
    manifest_file = snap_dir / "MANIFEST.json"
    if not manifest_file.exists():
        print(f"[SNAPSHOT VAULT] No snapshot found for {train_id}.")
        return False

    manifest = relay_io.read_json(manifest_file) or {}
    files = manifest.get("files", {})
    restored = 0

    for tf in files:
        src = snap_dir / "files" / tf
        dest = RELAY_ROOT / tf
        if src.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            restored += 1

    print(f"[SNAPSHOT VAULT] Restored {restored} files for train {train_id} from snapshot.")
    return True


if __name__ == "__main__":
    relay_io.utf8_console()
    print(f"[SNAPSHOT VAULT] Vault directory: {SNAPSHOTS_DIR}")
