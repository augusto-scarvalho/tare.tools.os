#!/usr/bin/env python3
"""Execute the ADR-057 H0-H3 Aider baseline in disposable Git clones."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def _disposable_root():
    root = Path(tempfile.mkdtemp(prefix="tare-agent-runtime-f1-"))
    try:
        yield root
    finally:
        gc.collect()
        def remove_readonly(function, path, error):
            if not isinstance(error, PermissionError):
                raise error
            os.chmod(path, stat.S_IWRITE)
            function(path)

        shutil.rmtree(root, onexc=remove_readonly)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _request_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 - explicit lab endpoint
        return json.load(response)


def _load_contracts(agent_runtime_root: Path, kernel_root: Path) -> dict[str, Any]:
    for path in (agent_runtime_root / "src", kernel_root, REPO_ROOT):
        if not path.exists():
            raise FileNotFoundError(f"required repository path is missing: {path}")
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))

    from compute_plane.aider_driver import AiderConfig
    from relay import agent_runtime_host, cluster_coordinator
    from tare_tools_agent_runtime import OutcomeStatus, RunBudget

    return {
        "AiderConfig": AiderConfig,
        "OutcomeStatus": OutcomeStatus,
        "RunBudget": RunBudget,
        "host": agent_runtime_host,
        "coordinator": cluster_coordinator,
    }


def _create_fixture_source(root: Path) -> tuple[Path, str, str]:
    source = root / "fixture-source"
    source.mkdir()
    (source / ".gitignore").write_text(
        ".aider*\n__pycache__/\n*.pyc\n", encoding="utf-8"
    )
    (source / "calculator.py").write_text(
        "def subtract(left: int, right: int) -> int:\n"
        "    \"\"\"Return left minus right.\"\"\"\n"
        "    return left + right\n",
        encoding="utf-8",
    )
    (source / "test_calculator.py").write_text(
        "import unittest\n\n"
        "from calculator import subtract\n\n\n"
        "class CalculatorTests(unittest.TestCase):\n"
        "    def test_subtract(self):\n"
        "        self.assertEqual(subtract(7, 2), 5)\n\n\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n",
        encoding="utf-8",
    )
    _git(source, "init", "--quiet")
    _git(source, "config", "user.email", "f1-lab@tare.tools")
    _git(source, "config", "user.name", "tare.tools F1 Lab")
    _git(source, "add", ".gitignore", "calculator.py", "test_calculator.py")
    _git(source, "commit", "--quiet", "-m", "fixture: broken subtraction")
    return source, _git(source, "rev-parse", "HEAD"), _git(source, "rev-parse", "HEAD^{tree}")


def _clone(source: Path, destination: Path) -> Path:
    subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", str(source), str(destination)],
        capture_output=True,
        text=True,
        check=True,
    )
    return destination


def _run_turn(
    modules: dict[str, Any],
    *,
    database: Path,
    workspace: Path,
    work_id: str,
    files: tuple[str, ...],
    instruction: str,
    api_base: str,
    model: str,
    timeout_s: float,
) -> tuple[Any, float]:
    config = modules["AiderConfig"](
        api_base=api_base,
        api_key="local-key",
        model=model,
        edit_format="diff",
        auto_commits=False,
        stream=False,
        map_tokens=2048,
    )
    db = modules["coordinator"].CoordinatorDB(database)
    runtime, gate = modules["host"].compose_aider_runtime(
        db,
        files=files,
        cwd=workspace,
        aider_config=config,
        workspace_is_ephemeral=True,
    )
    binding = gate.acquire(work_id, "f1-aider-baseline", ttl_s=timeout_s + 30)
    if binding is None:
        raise RuntimeError(f"could not acquire binding for {work_id}")
    started = time.monotonic()
    outcome = runtime.run(
        binding=binding,
        instruction=instruction,
        budget=modules["RunBudget"](1, 0, timeout_s),
    )
    elapsed = time.monotonic() - started
    if outcome.status is not modules["OutcomeStatus"].SUCCEEDED or not outcome.claim_accepted:
        raise RuntimeError(
            f"{work_id} failed: status={outcome.status.value}, "
            f"claim_accepted={outcome.claim_accepted}, output_tail={outcome.output[-1000:]!r}"
        )
    return outcome, elapsed


def _output_evidence(outcome: Any, elapsed: float, marker: str) -> dict[str, Any]:
    encoded = outcome.output.encode("utf-8")
    return {
        "status": outcome.status.value,
        "claim_accepted": outcome.claim_accepted,
        "turns": outcome.turns,
        "attempts": outcome.attempts,
        "elapsed_seconds": round(elapsed, 3),
        "output_chars": len(outcome.output),
        "output_sha256": _sha256(encoded),
        "required_marker_observed": marker in outcome.output,
    }


def _coding_cell(
    modules: dict[str, Any],
    *,
    root: Path,
    source: Path,
    name: str,
    api_base: str,
    model: str,
    timeout_s: float,
) -> dict[str, Any]:
    workspace = _clone(source, root / name)
    outcome, elapsed = _run_turn(
        modules,
        database=root / f"{name}.db",
        workspace=workspace,
        work_id=f"F1-{name.upper()}",
        files=("calculator.py", "test_calculator.py"),
        instruction=(
            "Fix only the implementation bug in calculator.py so the existing test passes. "
            "Do not modify the test, add files, commit, or change repository configuration."
        ),
        api_base=api_base,
        model=model,
        timeout_s=timeout_s,
    )
    test = subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "-q"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=30,
    )
    changed = tuple(filter(None, _git(workspace, "diff", "--name-only").splitlines()))
    diff = _git(workspace, "diff", "--", "calculator.py", "test_calculator.py")
    calculator = (workspace / "calculator.py").read_bytes()
    if test.returncode != 0:
        raise RuntimeError(f"{name} tests failed: {(test.stdout + test.stderr)[-1000:]}")
    if changed != ("calculator.py",):
        raise RuntimeError(f"{name} changed unexpected paths: {changed!r}")
    if b"return left - right" not in calculator:
        raise RuntimeError(f"{name} did not produce the required subtraction behavior")
    repository_state = _git(
        workspace, "status", "--porcelain", "--untracked-files=all"
    ).splitlines()
    if repository_state != ["M calculator.py"]:
        raise RuntimeError(
            f"{name} produced unexpected repository state: {repository_state!r}; "
            f"output_tail={outcome.output[-1000:]!r}"
        )
    return {
        **_output_evidence(outcome, elapsed, ""),
        "tests": "PASS",
        "changed_paths": list(changed),
        "calculator_sha256": _sha256(calculator),
        "diff_sha256": _sha256(diff.encode("utf-8")),
    }


def run_qualification(
    agent_runtime_root: Path,
    kernel_root: Path,
    api_base: str,
    model: str,
    timeout_s: float,
) -> dict[str, Any]:
    modules = _load_contracts(agent_runtime_root, kernel_root)
    server_root = api_base.removesuffix("/v1").rstrip("/")
    health = _request_json(f"{server_root}/health")
    models = _request_json(f"{server_root}/v1/models")
    model_alias = model.removeprefix("openai/")
    model_ids = sorted(item["id"] for item in models.get("data", []))
    if health.get("status") != "ok" or model_alias not in model_ids:
        raise RuntimeError(
            f"model attestation failed: health={health!r}, model_ids={model_ids!r}"
        )

    with _disposable_root() as root:
        source, fixture_commit, fixture_tree = _create_fixture_source(root)

        h0_workspace = _clone(source, root / "h0")
        h0_before = _git(h0_workspace, "status", "--porcelain", "--untracked-files=all")
        h0_outcome, h0_elapsed = _run_turn(
            modules,
            database=root / "h0.db",
            workspace=h0_workspace,
            work_id="F1-H0",
            files=(),
            instruction="Do not edit or create files. Reply with exactly H0_OK.",
            api_base=api_base,
            model=model,
            timeout_s=timeout_s,
        )
        h0_after = _git(h0_workspace, "status", "--porcelain", "--untracked-files=all")
        h0 = _output_evidence(h0_outcome, h0_elapsed, "H0_OK")
        if h0_before != h0_after or not h0["required_marker_observed"]:
            raise RuntimeError(
                "H0 changed the clone or omitted the required marker: "
                f"before={h0_before!r}, after={h0_after!r}, "
                f"output_tail={h0_outcome.output[-1000:]!r}"
            )
        h0["workspace_unchanged"] = True

        h1_workspace = _clone(source, root / "h1")
        h1_before = _git(h1_workspace, "status", "--porcelain", "--untracked-files=all")
        h1_outcome, h1_elapsed = _run_turn(
            modules,
            database=root / "h1.db",
            workspace=h1_workspace,
            work_id="F1-H1",
            files=("calculator.py", "test_calculator.py"),
            instruction=(
                "Diagnose the failing test without editing or creating files. "
                "Include the exact marker H1_DIAGNOSIS in the response."
            ),
            api_base=api_base,
            model=model,
            timeout_s=timeout_s,
        )
        h1_after = _git(h1_workspace, "status", "--porcelain", "--untracked-files=all")
        h1 = _output_evidence(h1_outcome, h1_elapsed, "H1_DIAGNOSIS")
        if h1_before != h1_after or not h1["required_marker_observed"]:
            raise RuntimeError(
                "H1 changed the clone or omitted the required marker: "
                f"before={h1_before!r}, after={h1_after!r}, "
                f"output_tail={h1_outcome.output[-1000:]!r}"
            )
        h1["workspace_unchanged"] = True

        h2 = _coding_cell(
            modules, root=root, source=source, name="h2",
            api_base=api_base, model=model, timeout_s=timeout_s,
        )
        h3 = _coding_cell(
            modules, root=root, source=source, name="h3-repeat",
            api_base=api_base, model=model, timeout_s=timeout_s,
        )
        reproducible = (
            h2["calculator_sha256"] == h3["calculator_sha256"]
            and h2["diff_sha256"] == h3["diff_sha256"]
        )
        if not reproducible:
            raise RuntimeError("H3 repeated baseline produced a different accepted patch")

        result = {
            "schema_version": "1.0.0",
            "status": "PASS",
            "scope": "ADR-057 complete Aider H0-H3 baseline; no F2 or production authority",
            "server_attestation": {"health": "ok", "model_id": model_alias},
            "fixture": {"commit": fixture_commit, "tree": fixture_tree},
            "h0_inference_mechanics": h0,
            "h1_read_only_diagnosis": h1,
            "h2_disposable_clone_coding": h2,
            "h3_repeated_baseline": {**h3, "reproducible_patch": True},
            "cost_to_trust": {
                "model_calls": 4,
                "accepted_coding_runs": 2,
                "total_elapsed_seconds": round(
                    h0["elapsed_seconds"] + h1["elapsed_seconds"]
                    + h2["elapsed_seconds"] + h3["elapsed_seconds"],
                    3,
                ),
            },
            "strands": "NOT_RUN_OPTIONAL_AND_REMOVABLE",
            "shadow_eligibility": "REQUIRES_EXPLICIT_PROGRESSION_DECISION",
            "production_entrypoint": "HELD",
        }
        return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--agent-runtime-root", type=Path,
        default=REPO_ROOT.parent / "tare.tools.agent-runtime",
    )
    parser.add_argument(
        "--kernel-root", type=Path,
        default=REPO_ROOT.parent / "tare.tools.kernel",
    )
    parser.add_argument("--api-base", default=f"http://{os.getenv('TARE_NODE_HOST', 'localhost')}:8080/v1")
    parser.add_argument("--model", default="openai/fable-tc-l1.0")
    parser.add_argument("--timeout-seconds", type=float, default=180)
    args = parser.parse_args()
    try:
        result = run_qualification(
            args.agent_runtime_root.resolve(),
            args.kernel_root.resolve(),
            args.api_base,
            args.model,
            args.timeout_seconds,
        )
    except BaseException as exc:
        print(json.dumps({"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
