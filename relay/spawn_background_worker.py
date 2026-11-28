"""Minimal finite background job launcher used by audit_execution."""
from __future__ import annotations

import subprocess

from . import relay_io


def spawn_finite(command, *, cwd, stdout, env=None):
    """Launch one caller-owned finite job; no swarm PID file or daemon startup."""
    return subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stdout,
        **relay_io.safe_subprocess_kwargs(),
    )
