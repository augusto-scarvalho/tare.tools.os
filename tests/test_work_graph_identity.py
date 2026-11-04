"""Tests for graph-scoped Work ownership foundation (AGY_WORK_IDENTITY_TASK).

Demonstrates collision prevention across graph files sharing Work IDs,
independent active and archive states, mixed receipt filtering, default and
explicit control compatibility, refusal of relative and symlink paths,
invalid execution revision handling, no fallback for missing external graphs,
and stable lock roots under graph edits.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import pytest

from relay import relay_mesh, work_run


def make_graph_file(path: Path, nodes: list[dict]) -> Path:
    payload = {"meta": {"schema": "work-graph-poc/0.5"}, "nodes": nodes, "edges": []}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def isolated_relay(tmp_path):
    """Point relay_mesh constants at an isolated temporary root for clean isolation."""
    original = relay_mesh.RELAY_ROOT
    root = tmp_path / "control_root"
    root.mkdir()
    relay_mesh.rebind_root(root)
    relay_mesh.ensure_dirs()

    control_nodes = [{
        "id": "work-one",
        "title": "Control Work One",
        "metrics": {},
        "repository_scopes": [{"repository": "tare.tools.os", "target_paths": ["src"]}]
    }]
    make_graph_file(relay_mesh.WORK_GRAPH_FILE, control_nodes)

    yield root
    relay_mesh.rebind_root(original)


def make_receipt(run_id: str, work_id: str, *, train_id: str | None = None,
                 graph_path: str | None = None, status: str = "SETTLED",
                 mtime_offset: int = 0) -> dict:
    runs_dir = relay_mesh.RELAY_ROOT.parent / ".reconciliation" / "work-runs" / run_id
    runs_dir.mkdir(parents=True, exist_ok=True)
    receipt = {
        "schema": work_run.RUN_SCHEMA,
        "run_id": run_id,
        "work_id": work_id,
        "train_id": train_id,
        "status": status,
        "started_at": work_run.now()
    }
    if graph_path is not None:
        receipt["graph_path"] = graph_path
    path = runs_dir / "run.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    if mtime_offset:
        current_mtime = path.stat().st_mtime
        os.utime(path, (current_mtime + mtime_offset, current_mtime + mtime_offset))
    return receipt


def test_collision_prevention_two_graphs_sharing_work_id(tmp_path, isolated_relay):
    """Two distinct external graph files sharing a Work ID must produce distinct lock roots and train IDs."""
    graph_a = make_graph_file(tmp_path / "graph_a.json", [{
        "id": "work-clash",
        "title": "Work in Graph A",
        "metrics": {},
        "repository_scopes": [{"repository": "repo-a"}]
    }])
    graph_b = make_graph_file(tmp_path / "graph_b.json", [{
        "id": "work-clash",
        "title": "Work in Graph B",
        "metrics": {"execution_revision": "0123456789abcdef01234567"},
        "repository_scopes": [{"repository": "repo-b"}]
    }])

    lock_a = work_run.lock_root_for("work-clash", graph_path=graph_a)
    lock_b = work_run.lock_root_for("work-clash", graph_path=graph_b)
    assert lock_a != lock_b, "Locks must not collide across different graphs sharing a Work ID"
    assert lock_a.parent == lock_b.parent
    assert lock_a.parent == isolated_relay.parent / ".reconciliation" / "work-runs" / "locks"
    assert len(lock_a.name) == 64 and all(c in "0123456789abcdef" for c in lock_a.name)
    assert len(lock_b.name) == 64 and all(c in "0123456789abcdef" for c in lock_b.name)

    train_id_a = work_run.train_id_for("work-clash", graph_path=graph_a)
    train_id_b = work_run.train_id_for("work-clash", graph_path=graph_b)
    assert train_id_a != train_id_b, "Train IDs must not collide across different graphs"
    assert train_id_a.startswith("GRAPH-RUN-")
    assert len(train_id_a) == 10 + 32
    assert train_id_b.startswith("GRAPH-RUN-")
    assert train_id_b.endswith("-r0123456789abcdef01234567")
    assert len(train_id_b) == 10 + 32 + 26


def test_external_train_namespace_cannot_be_a_legacy_control_work_id(tmp_path, isolated_relay):
    graph = make_graph_file(tmp_path / 'external.json', [{'id': 'work-one', 'metrics': {}}])
    external = work_run.train_id_for('work-one', graph_path=graph)
    # RUN-G<digest> would collide with a legitimate control Work named G<digest>.
    control_work = 'G' + work_run.lock_root_for('work-one', graph_path=graph).name[:32]
    assert work_run.train_id_for(control_work) != external


def test_independent_active_and_archive_states(tmp_path, isolated_relay):
    """External graphs sharing a Work ID must resolve their active and archive directories independently."""
    graph_a = make_graph_file(tmp_path / "graph_a.json", [{
        "id": "work-clash",
        "title": "Work in Graph A",
        "metrics": {},
        "repository_scopes": [{"repository": "repo-a"}]
    }])
    graph_b = make_graph_file(tmp_path / "graph_b.json", [{
        "id": "work-clash",
        "title": "Work in Graph B",
        "metrics": {},
        "repository_scopes": [{"repository": "repo-b"}]
    }])

    tid_a = work_run.train_id_for("work-clash", graph_path=graph_a)
    tid_b = work_run.train_id_for("work-clash", graph_path=graph_b)

    # Graph A: Active train in TRAINS_DIR
    active_dir_a = relay_mesh.TRAINS_DIR / tid_a
    active_dir_a.mkdir(parents=True)
    (active_dir_a / "STATE.json").write_text(json.dumps({"status": "IN_PROGRESS_IMPLEMENTER", "revision": 2}), encoding="utf-8")

    # Graph B: Archived train in HISTORY_DIR
    archive_dir_b = relay_mesh.HISTORY_DIR / f"{tid_b}_20260919T120000Z"
    archive_dir_b.mkdir(parents=True)
    (archive_dir_b / "STATE.json").write_text(json.dumps({"status": "LANDED", "revision": 5}), encoding="utf-8")

    assert work_run.train_dir_for("work-clash", graph_path=graph_a) == active_dir_a
    assert work_run.train_dir_for("work-clash", graph_path=graph_b) == archive_dir_b

    status_a = work_run.status("work-clash", graph_path=graph_a)
    status_b = work_run.status("work-clash", graph_path=graph_b)

    assert status_a["train"] is not None
    assert status_a["train"]["status"] == "IN_PROGRESS_IMPLEMENTER"
    assert status_a["train"]["revision"] == 2
    assert status_a["train"]["dir"] == str(active_dir_a)

    assert status_b["train"] is not None
    assert status_b["train"]["status"] == "LANDED"
    assert status_b["train"]["revision"] == 5
    assert status_b["train"]["dir"] == str(archive_dir_b)


def test_mixed_receipt_records_filtering(tmp_path, isolated_relay):
    """Receipt filtering in status must isolate external scopes, accept legacy/explicit control, and reject cross-graph leakage."""
    graph_a = make_graph_file(tmp_path / "graph_a.json", [{
        "id": "work-shared",
        "title": "Shared Work Graph A",
        "metrics": {},
        "repository_scopes": [{"repository": "repo-a"}]
    }])
    graph_b = make_graph_file(tmp_path / "graph_b.json", [{
        "id": "work-shared",
        "title": "Shared Work Graph B",
        "metrics": {},
        "repository_scopes": [{"repository": "repo-b"}]
    }])
    other_graph = make_graph_file(tmp_path / "graph_other.json", [{
        "id": "work-shared",
        "title": "Other Graph",
        "metrics": {},
        "repository_scopes": [{"repository": "repo-other"}]
    }])

    tid_a = work_run.train_id_for("work-shared", graph_path=graph_a)
    tid_b = work_run.train_id_for("work-shared", graph_path=graph_b)

    # 1. Legacy control receipt: no graph_path, control train ID
    make_receipt("11" * 16, "work-shared", train_id="RUN-work-shared", graph_path=None, mtime_offset=10)
    # 2. Graph A receipt
    make_receipt("22" * 16, "work-shared", train_id=tid_a, graph_path=str(graph_a), mtime_offset=20)
    # 3. Graph B receipt
    make_receipt("33" * 16, "work-shared", train_id=tid_b, graph_path=str(graph_b), mtime_offset=30)
    # 4. Receipt with malformed graph_path
    make_receipt("44" * 16, "work-shared", train_id=tid_a, graph_path="not-an-absolute-path.json", mtime_offset=40)
    # 5. Overlapping train ID explicitly naming another graph
    make_receipt("55" * 16, "work-shared", train_id="RUN-work-shared", graph_path=str(other_graph), mtime_offset=50)
    # 6. External train receipt missing graph_path
    make_receipt("66" * 16, "work-shared", train_id=tid_a, graph_path=None, mtime_offset=60)

    # Control lookup (default None): matches legacy receipt (run_id 11...11); excludes external receipts and receipt 5
    st_ctrl = work_run.status("work-shared")
    assert st_ctrl["last_run"] is not None
    assert st_ctrl["last_run"]["run_id"] == "11" * 16
    assert st_ctrl["graph_path"] == str(Path(relay_mesh.WORK_GRAPH_FILE).resolve())

    # Control lookup (explicit WORK_GRAPH_FILE equivalent): same result
    st_ctrl_exp = work_run.status("work-shared", graph_path=relay_mesh.WORK_GRAPH_FILE)
    assert st_ctrl_exp["last_run"] is not None
    assert st_ctrl_exp["last_run"]["run_id"] == "11" * 16
    assert st_ctrl_exp["graph_path"] == str(Path(relay_mesh.WORK_GRAPH_FILE).resolve())

    # Graph A lookup: matches only receipt 2 (run_id 22...22); ignores legacy, graph B, malformed, and missing graph_path
    st_a = work_run.status("work-shared", graph_path=graph_a)
    assert st_a["last_run"] is not None
    assert st_a["last_run"]["run_id"] == "22" * 16
    assert st_a["graph_path"] == str(Path(graph_a).resolve())

    # Graph B lookup: matches only receipt 3 (run_id 33...33)
    st_b = work_run.status("work-shared", graph_path=graph_b)
    assert st_b["last_run"] is not None
    assert st_b["last_run"]["run_id"] == "33" * 16
    assert st_b["graph_path"] == str(Path(graph_b).resolve())


def test_default_and_explicit_control_compatibility(tmp_path, isolated_relay):
    """Default graph_path=None and explicit WORK_GRAPH_FILE paths produce identical control identities and paths."""
    lock_default = work_run.lock_root_for("work-one")
    lock_path_obj = work_run.lock_root_for("work-one", graph_path=relay_mesh.WORK_GRAPH_FILE)
    lock_str_obj = work_run.lock_root_for("work-one", graph_path=str(relay_mesh.WORK_GRAPH_FILE))
    lock_resolved = work_run.lock_root_for("work-one", graph_path=str(Path(relay_mesh.WORK_GRAPH_FILE).resolve()))

    assert lock_default == lock_path_obj == lock_str_obj == lock_resolved

    tid_default = work_run.train_id_for("work-one")
    tid_explicit = work_run.train_id_for("work-one", graph_path=relay_mesh.WORK_GRAPH_FILE)
    assert tid_default == tid_explicit == "RUN-work-one"

    tdir_default = work_run.train_dir_for("work-one")
    tdir_explicit = work_run.train_dir_for("work-one", graph_path=relay_mesh.WORK_GRAPH_FILE)
    assert tdir_default == tdir_explicit == relay_mesh.TRAINS_DIR / "RUN-work-one"

    # Explicit control receipt in work-runs
    make_receipt("77" * 16, "work-one", train_id="RUN-work-one",
                 graph_path=str(Path(relay_mesh.WORK_GRAPH_FILE).resolve()), mtime_offset=10)

    st_def = work_run.status("work-one")
    st_exp = work_run.status("work-one", graph_path=relay_mesh.WORK_GRAPH_FILE)
    assert st_def["last_run"] is not None and st_def["last_run"]["run_id"] == "77" * 16
    assert st_exp["last_run"] is not None and st_exp["last_run"]["run_id"] == "77" * 16
    assert st_def["graph_path"] == st_exp["graph_path"] == str(Path(relay_mesh.WORK_GRAPH_FILE).resolve())


def test_relative_and_symlink_refusal(tmp_path, isolated_relay):
    """Explicit graph_path must reject relative paths, traversal, non-json extensions, and symlinks."""
    with pytest.raises(ValueError, match="absolute JSON path without parent traversal"):
        work_run.lock_root_for("w", graph_path="relative.json")

    with pytest.raises(ValueError, match="absolute JSON path without parent traversal"):
        work_run.train_id_for("w", graph_path="./relative.json")

    with pytest.raises(ValueError, match="absolute JSON path without parent traversal"):
        work_run.train_dir_for("w", graph_path="../relative.json")

    with pytest.raises(ValueError, match="absolute JSON path without parent traversal"):
        work_run.status("w", graph_path="work-graph.json")

    traversal_path = str(tmp_path / "sub" / ".." / "graph.json")
    with pytest.raises(ValueError, match="absolute JSON path without parent traversal"):
        work_run.lock_root_for("w", graph_path=traversal_path)

    non_json_path = str(tmp_path / "graph.yaml")
    with pytest.raises(ValueError, match="absolute JSON path without parent traversal"):
        work_run.lock_root_for("w", graph_path=non_json_path)

    # Symlink refusal test (skipped if platform does not support symlinks)
    real_target = make_graph_file(tmp_path / "real_target.json", [{"id": "w", "metrics": {}}])
    symlink_path = tmp_path / "symlink_graph.json"
    try:
        os.symlink(real_target, symlink_path)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks not supported or permitted on this platform/environment")

    with pytest.raises(ValueError, match="native_custody_link_forbidden"):
        work_run.lock_root_for("w", graph_path=str(symlink_path))


def test_invalid_execution_revision(tmp_path, isolated_relay):
    """Validation of execution_revision must be preserved for external graphs."""
    # Revision with invalid length (23 hex chars)
    graph_short = make_graph_file(tmp_path / "graph_short.json", [{
        "id": "work-rev",
        "metrics": {"execution_revision": "0" * 23}
    }])
    with pytest.raises(ValueError, match="WORK_EXECUTION_REVISION_INVALID"):
        work_run.train_id_for("work-rev", graph_path=graph_short)

    # Revision with non-hex characters
    graph_nonhex = make_graph_file(tmp_path / "graph_nonhex.json", [{
        "id": "work-rev",
        "metrics": {"execution_revision": "g" * 24}
    }])
    with pytest.raises(ValueError, match="WORK_EXECUTION_REVISION_INVALID"):
        work_run.train_id_for("work-rev", graph_path=graph_nonhex)

    # Non-string revision
    graph_int = make_graph_file(tmp_path / "graph_int.json", [{
        "id": "work-rev",
        "metrics": {"execution_revision": 123456789012345678901234}
    }])
    with pytest.raises(ValueError, match="WORK_EXECUTION_REVISION_INVALID"):
        work_run.train_id_for("work-rev", graph_path=graph_int)


def test_no_fallback_for_missing_or_malformed_external_graph(tmp_path, isolated_relay):
    """External graph that is missing or malformed must fail and never fall back to the control graph."""
    # Control graph contains 'work-one'
    missing = tmp_path / "non_existent_graph.json"

    # Missing external graph must raise FileNotFoundError, not return control graph's RUN-work-one
    with pytest.raises(FileNotFoundError):
        work_run.train_id_for("work-one", graph_path=missing)

    with pytest.raises(FileNotFoundError):
        work_run.status("work-one", graph_path=missing)

    # Malformed JSON in external graph must raise JSONDecodeError, not fall back to control graph
    corrupted = tmp_path / "corrupted_graph.json"
    corrupted.write_text("{corrupt json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        work_run.train_id_for("work-one", graph_path=corrupted)

    # Malformed non-dictionary in external graph
    non_dict = tmp_path / "array_graph.json"
    non_dict.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="GRAPH_MALFORMED"):
        work_run.train_id_for("work-one", graph_path=non_dict)


def test_stable_locks_under_graph_edits(tmp_path, isolated_relay):
    """Lock roots for an external graph must remain stable when graph contents, revisions, or nodes change."""
    ext_graph = make_graph_file(tmp_path / "editable_graph.json", [{
        "id": "work-stable",
        "title": "Version 1",
        "metrics": {"execution_revision": "111111111111111111111111"}
    }])

    lock_v1 = work_run.lock_root_for("work-stable", graph_path=ext_graph)

    # Edit node metrics and title
    make_graph_file(ext_graph, [{
        "id": "work-stable",
        "title": "Version 2 Modified",
        "metrics": {"execution_revision": "222222222222222222222222"}
    }])
    lock_v2 = work_run.lock_root_for("work-stable", graph_path=ext_graph)
    assert lock_v1 == lock_v2, "Lock root must not change when graph content or execution revision changes"

    # Delete file and re-create with additional nodes
    ext_graph.unlink()
    lock_v3 = work_run.lock_root_for("work-stable", graph_path=ext_graph)
    assert lock_v1 == lock_v3, "Lock root must remain invariant even if graph file is temporarily absent"

    make_graph_file(ext_graph, [
        {"id": "work-stable", "title": "Version 3", "metrics": {}},
        {"id": "work-other", "title": "Other", "metrics": {}}
    ])
    lock_v4 = work_run.lock_root_for("work-stable", graph_path=ext_graph)
    assert lock_v1 == lock_v4


def test_train_id_execution_revision_sensitivity_in_status(tmp_path, isolated_relay):
    """Status receipt matching respects execution_revision when present and matches base when unrevised."""
    rev_a = "aaaaaaaaaaaaaaaaaaaaaaaa"
    rev_b = "bbbbbbbbbbbbbbbbbbbbbbbb"

    # External graph with node revision rev_a
    graph_file = make_graph_file(tmp_path / "rev_graph.json", [{
        "id": "work-rev-test",
        "metrics": {"execution_revision": rev_a}
    }])

    tid_a = work_run.train_id_for("work-rev-test", graph_path=graph_file)
    assert tid_a.endswith("-r" + rev_a)

    # Receipt with rev_a
    make_receipt("88" * 16, "work-rev-test", train_id=tid_a, graph_path=str(graph_file), mtime_offset=10)
    # Receipt with rev_b
    tid_b = tid_a.replace(rev_a, rev_b)
    make_receipt("99" * 16, "work-rev-test", train_id=tid_b, graph_path=str(graph_file), mtime_offset=20)

    # Query status when node has rev_a: only receipt with rev_a matches
    st_reva = work_run.status("work-rev-test", graph_path=graph_file)
    assert st_reva["last_run"] is not None
    assert st_reva["last_run"]["run_id"] == "88" * 16

    # Removing the revision must not present a different revision's execution.
    make_graph_file(graph_file, [{
        "id": "work-rev-test",
        "metrics": {}
    }])
    st_norev = work_run.status("work-rev-test", graph_path=graph_file)
    assert st_norev["last_run"] is None


@pytest.mark.parametrize('payload', [{}, {'nodes': None}, {'nodes': [None]},
    {'nodes': []}, {'nodes': [{'id': 'work-one'}, {'id': 'work-one'}]}])
def test_external_graph_requires_one_observed_work(tmp_path, isolated_relay, payload):
    graph = tmp_path / 'bad-shape.json'
    graph.write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(ValueError):
        work_run.status('work-one', graph_path=graph)


def test_similar_train_prefix_and_explicit_null_graph_do_not_match(tmp_path, isolated_relay):
    graph = make_graph_file(tmp_path / 'external.json', [{'id': 'work-one', 'metrics': {}}])
    tid = work_run.train_id_for('work-one', graph_path=graph)
    make_receipt('a' * 32, 'work-one', train_id=tid, graph_path=str(graph), mtime_offset=10)
    make_receipt('b' * 32, 'work-one', train_id=tid+'-other', graph_path=str(graph), mtime_offset=20)
    assert work_run.status('work-one', graph_path=graph)['last_run']['run_id'] == 'a' * 32
    make_receipt('c' * 32, 'work-one', train_id='RUN-work-one', mtime_offset=30)
    invalid = work_run.receipt_path('c' * 32)
    data = json.loads(invalid.read_bytes()); data['graph_path'] = None
    invalid.write_text(json.dumps(data), encoding='utf-8')
    assert work_run.status('work-one')['last_run'] is None
