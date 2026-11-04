"""GRAPH-BACKLOG-02 — Harness thin client for the relay work-graph engine.

Read-only projection bridge: delegates CLI verbs (next, packet, why,
blockers, doctor, diagnose, lint, validate) to the decoupled graph_ops.py engine in the relay via
subprocess and parses its JSON stdout. Zero coupling to the harness
TaskStore or its write locks; never mutates work-graph.json.

Relay location defaults to C:/projects/tare-tools-relay; override with
the TARE_RELAY_DIR environment variable.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_RELAY_DIR = Path("C:/projects/tare-tools-relay")


class GraphBacklogAdapter:
    def __init__(self, relay_dir: str | os.PathLike | None = None):
        self.relay_dir = Path(relay_dir or os.environ.get("TARE_RELAY_DIR") or DEFAULT_RELAY_DIR)
        candidates = [
            self.relay_dir / "scripts" / "harness_lib" / "graph_ops.py",
            self.relay_dir / "satellites" / "tare.tools.backlog-graph" / "graph_ops.py",
            self.relay_dir.parent / "tare.tools.backlog-graph" / "graph_ops.py",
            self.relay_dir / "side_projects" / "tare.tools.graph-backlog" / "graph_ops.py",
        ]
        self.engine = None
        for c in candidates:
            if c.is_file():
                self.engine = c
                break
        self.graph_file = self.relay_dir / "work-graph.json"
        if not self.graph_file.is_file():
            raise FileNotFoundError(f"work graph not found: {self.graph_file}")

    def _run(self, *args: str):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        if self.engine and self.engine.is_file():
            cmd = [sys.executable, str(self.engine), "--graph", str(self.graph_file), *args]
        else:
            cmd = [sys.executable, "-m", "graph_backlog.cli", "--graph", str(self.graph_file), *args]
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True, encoding="utf-8",
            cwd=str(self.relay_dir), env=env, timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"graph_ops failed (exit {proc.returncode}):\n{proc.stderr}")
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"graph_ops non-JSON output: {proc.stdout!r}") from exc

    def get_frontier(self) -> list[dict]:
        return self._run("frontier")

    def get_next(self, limit: int = 5) -> list[dict]:
        return self._run("next", "--limit", str(limit))

    def get_packet(self, task_id: str) -> dict:
        return self._run("packet", task_id)

    def packet(self, task_id: str) -> dict:
        return self.get_packet(task_id)

    def get_why(self, task_id: str) -> dict:
        return self._run("why", task_id)

    def why(self, task_id: str) -> dict:
        return self.get_why(task_id)

    def get_blockers(self, task_id: str) -> dict:
        return self._run("blockers", task_id)

    def blockers(self, task_id: str) -> dict:
        return self.get_blockers(task_id)

    def doctor(self) -> dict:
        return self._run("doctor")

    def diagnose(self) -> dict:
        return self._run("validate")

    def validate(self) -> dict:
        return self._run("validate")

    def lint(self) -> dict:
        return self._run("lint")
