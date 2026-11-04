#!/usr/bin/env python3
"""
tare.tools Graph Ops — Standalone deterministic Work Graph & DAG Backlog CLI.
"""
from __future__ import annotations
import sys
from pathlib import Path

# Ensure src/ is on python path when running script directly
_src_dir = Path(__file__).resolve().parent / "src"
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))

from graph_backlog.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
