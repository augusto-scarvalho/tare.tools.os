from __future__ import annotations

import json
from pathlib import Path

import pytest

from relay.train_execution import (
    ExecutionDescriptorRejected,
    canonical_json_bytes,
    create_execution_descriptor,
    parse_execution_descriptor,
    read_execution_descriptor,
    validate_descriptor_for_train,
)


def _payload(**overrides):
    payload = {
        "schema": "tare.tools/train-execution/1",
        "train_id": "TRAIN-42",
        "work_id": "implement-hooks",
        "work_graph": "work-graph.json",
        "execution_repository": "tare.tools.kernel",
        "base_commit": "a" * 40,
        "target_ref": "refs/heads/main",
        "library_manifest": "LIBRARY_MANIFEST.json",
        "tracked_paths": ["compute_plane", "tests"],
        "verification_checks": [
            {
                "criterion": "hooks-pass",
                "command": [
                    "python",
                    "-m",
                    "pytest",
                    "-q",
                    "tests/test_tool_hooks.py",
                ],
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_descriptor_accepts_only_exact_canonical_bytes_and_hashes_same_buffer():
    raw = canonical_json_bytes(_payload())
    descriptor = parse_execution_descriptor(raw)

    assert descriptor.canonical_bytes == raw
    assert descriptor.train_id == "TRAIN-42"
    assert descriptor.tracked_paths == ("compute_plane", "tests")
    assert len(descriptor.sha256) == 64

    with pytest.raises(ExecutionDescriptorRejected, match="NOT_CANONICAL"):
        parse_execution_descriptor(json.dumps(_payload(), indent=2).encode())


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"base_commit": "abc123"}, "BASE_COMMIT_INVALID"),
        ({"tracked_paths": ["../escape"]}, "TRACKED_PATH_INVALID"),
        ({"tracked_paths": ["tests", "tests/unit"]}, "TRACKED_PATHS_OVERLAP"),
        ({"tracked_paths": ["tests", "compute_plane"]}, "NOT_CANONICAL"),
        ({"target_ref": "main"}, "TARGET_REF_INVALID"),
        (
            {
                "verification_checks": [
                    {"criterion": "hooks-pass", "command": ["sh", "-c", "pytest"]}
                ]
            },
            "VERIFICATION_SHELL_FORBIDDEN",
        ),
    ],
)
def test_descriptor_rejects_ambiguous_or_unbounded_authority(overrides, reason):
    with pytest.raises(ExecutionDescriptorRejected, match=reason):
        parse_execution_descriptor(canonical_json_bytes(_payload(**overrides)))


def test_descriptor_rejects_duplicate_json_keys():
    raw = canonical_json_bytes(_payload())
    duplicate = raw.replace(b'{"base_commit":', b'{"base_commit":"b","base_commit":', 1)
    with pytest.raises(ExecutionDescriptorRejected, match="DUPLICATE_KEY"):
        parse_execution_descriptor(duplicate)


def test_create_only_and_work_criterion_binding(tmp_path: Path):
    path = tmp_path / "EXECUTION_DESCRIPTOR.json"
    expected_sha = create_execution_descriptor(path, _payload())
    descriptor = read_execution_descriptor(path)
    assert descriptor.sha256 == expected_sha

    with pytest.raises(ExecutionDescriptorRejected, match="ALREADY_EXISTS"):
        create_execution_descriptor(path, _payload())

    graph = tmp_path / "work-graph.json"
    graph.write_text(
        json.dumps(
            {
                "nodes": [
                    {"id": "implement-hooks", "exit_criteria": ["hooks-pass"]}
                ]
            }
        ),
        encoding="utf-8",
    )
    validate_descriptor_for_train(
        descriptor,
        train_id="TRAIN-42",
        task_ids=["implement-hooks"],
        work_graph_file=graph,
    )

    graph.write_text(
        json.dumps(
            {
                "nodes": [
                    {"id": "implement-hooks", "exit_criteria": ["different"]}
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ExecutionDescriptorRejected, match="CRITERIA_CHECK_MISMATCH"):
        validate_descriptor_for_train(
            descriptor,
            train_id="TRAIN-42",
            task_ids=["implement-hooks"],
            work_graph_file=graph,
        )
