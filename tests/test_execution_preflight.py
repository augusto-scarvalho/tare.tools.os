from __future__ import annotations

import builtins
import json
import importlib.util
import subprocess
from pathlib import Path

from relay.execution_preflight import (
    configured_mode,
    preflight_train_execution,
    record_shadow_preflight,
)
from relay.train_execution import canonical_json_bytes, parse_execution_descriptor


KERNEL_AVAILABLE = importlib.util.find_spec("compute_plane") is not None


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _subject(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    _git("init", cwd=repository)
    _git("config", "user.email", "test@example.invalid", cwd=repository)
    _git("config", "user.name", "Test", cwd=repository)
    (repository / "file.txt").write_text("base\n", encoding="utf-8")
    _git("add", ".", cwd=repository)
    _git("commit", "-m", "base", cwd=repository)
    head = _git("rev-parse", "HEAD", cwd=repository)
    descriptor = parse_execution_descriptor(canonical_json_bytes({
        "schema": "tare.tools/train-execution/1",
        "train_id": "TRAIN-SHADOW",
        "work_id": "work-one",
        "work_graph": "work-graph.json",
        "execution_repository": "repo-one",
        "base_commit": head,
        "target_ref": "refs/heads/main",
        "library_manifest": "LIBRARY_MANIFEST.json",
        "tracked_paths": ["file.txt"],
        "verification_checks": [
            {"criterion": "passes", "command": ["pytest", "-q"]}
        ],
    }))
    train = tmp_path / "train"
    train.mkdir()
    (train / "EXECUTION_DESCRIPTOR.json").write_bytes(descriptor.canonical_bytes)
    (train / "STATE.json").write_text(json.dumps({
        "train_id": descriptor.train_id,
        "task_ids": [descriptor.work_id],
        "execution_descriptor": {"sha256": descriptor.sha256},
    }), encoding="utf-8")
    graph = tmp_path / "work-graph.json"
    graph.write_text(json.dumps({
        "nodes": [{"id": "work-one", "exit_criteria": ["passes"]}]
    }), encoding="utf-8")
    return repository, train, graph, descriptor


def test_default_mode_preserves_incumbent_and_shadow_records_zero_provider_calls(
    tmp_path: Path,
):
    repository, train, graph, descriptor = _subject(tmp_path)
    assert configured_mode({}) == "incumbent"
    incumbent = preflight_train_execution(
        train, work_graph_file=graph, repository_root=None, mode="incumbent"
    )
    assert incumbent.admissible and incumbent.reason == "INCUMBENT_UNCHANGED"

    shadow = preflight_train_execution(
        train, work_graph_file=graph, repository_root=repository, mode="shadow"
    )
    if not KERNEL_AVAILABLE:
        assert not shadow.admissible
        assert shadow.reason == "KERNEL_UNAVAILABLE"
        record_shadow_preflight(train, shadow)
        receipt = json.loads((train / "SHADOW_PREFLIGHT.json").read_text(encoding="utf-8"))
        assert receipt["provider_calls"] == 0
        return
    assert shadow.admissible
    assert shadow.descriptor_sha256 == descriptor.sha256
    record_shadow_preflight(train, shadow)
    receipt = json.loads((train / "SHADOW_PREFLIGHT.json").read_text(encoding="utf-8"))
    assert receipt["provider_calls"] == 0


def test_shadow_descriptor_drift_fails_closed_without_mutation(tmp_path: Path):
    repository, train, graph, _descriptor = _subject(tmp_path)
    raw = (train / "EXECUTION_DESCRIPTOR.json").read_bytes()
    (train / "EXECUTION_DESCRIPTOR.json").write_bytes(raw.replace(b"pytest", b"py.test"))

    result = preflight_train_execution(
        train, work_graph_file=graph, repository_root=repository, mode="shadow"
    )
    assert not result.admissible
    assert result.reason == "DESCRIPTOR_SEAL_MISMATCH"
    assert not (train / "SHADOW_PREFLIGHT.json").exists()


def test_shadow_missing_kernel_fails_closed_without_provider(tmp_path: Path, monkeypatch):
    repository, train, graph, _descriptor = _subject(tmp_path)
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "compute_plane.workspace":
            raise ModuleNotFoundError(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    result = preflight_train_execution(
        train, work_graph_file=graph, repository_root=repository, mode="shadow"
    )
    assert not result.admissible
    assert result.reason == "KERNEL_UNAVAILABLE"
    record_shadow_preflight(train, result)
    receipt = json.loads((train / "SHADOW_PREFLIGHT.json").read_text(encoding="utf-8"))
    assert receipt["provider_calls"] == 0


def test_shadow_missing_repository_or_base_is_classified_before_provider(tmp_path: Path):
    repository, train, graph, _descriptor = _subject(tmp_path)
    unavailable = preflight_train_execution(
        train, work_graph_file=graph, repository_root=None, mode="shadow"
    )
    assert unavailable.reason == "EXECUTION_REPOSITORY_UNAVAILABLE"

    subprocess.run(["git", "gc", "--prune=now"], cwd=repository, check=True)
    # An unrelated directory cannot satisfy the sealed base identity.
    other = tmp_path / "other"
    other.mkdir()
    result = preflight_train_execution(
        train, work_graph_file=graph, repository_root=other, mode="shadow"
    )
    assert result.reason == (
        "BASE_COMMIT_UNAVAILABLE" if KERNEL_AVAILABLE else "KERNEL_UNAVAILABLE"
    )
