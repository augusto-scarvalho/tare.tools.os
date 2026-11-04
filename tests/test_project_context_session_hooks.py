"""Lifecycle composition with an explicit root and external dedup receipt."""
import json
from pathlib import Path

import pytest

indexer = pytest.importorskip("specgraph.indexer")
pytest.importorskip("compute_plane.session_hooks")
from relay.session_hooks import dispatch


@pytest.fixture(autouse=True)
def observation_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("TARE_HOOK_STATE_DIR", str(tmp_path / "hook-observations"))
    monkeypatch.setenv("TARE_HOOK_OBSERVATION_MODE", "fixture")


def test_lifecycle_projects_snapshot_once_and_never_blocks_stop(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src/example.py").write_text("def alpha(): return 1\n")
    monkeypatch.setenv("TARE_SPECGRAPH_STATE_DIR", str(tmp_path / "index"))
    indexer.index_repository(root)
    config = {"root": str(root), "project_context": {"role": "explorer", "files": ["src/example.py"],
                                                    "timeout_ms": 1000},
              "context_receipt": str(tmp_path / "receipt.json")}
    payload = {"hook_event_name": "SessionStart", "cwd": str(root), "session_id": "fixture-session"}
    rendered, report = dispatch("claude", payload, config)
    assert rendered["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert report["work"] is None
    assert report["project"]["work_id"] is None
    assert report["project"]["root"] == str(root)
    assert report["project"]["role"] == "explorer"
    assert dispatch("claude", payload, config)[0] == {}
    payload["hook_event_name"] = "Stop"
    assert dispatch("claude", payload, config)[0] == {}
    assert json.loads((tmp_path / "receipt.json").read_text())["authority"] == "NONE"


def test_lifecycle_refuses_cross_root_and_candidate_receipt(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    payload = {"hook_event_name": "SessionStart", "cwd": str(tmp_path), "session_id": "s"}
    with pytest.raises(ValueError, match="session_workspace_mismatch"):
        dispatch("codex", payload, {"root": str(root), "project_context": {"role": "planner"}})
    payload["cwd"] = str(root)
    with pytest.raises(ValueError, match="context_receipt_inside_candidate"):
        dispatch("codex", payload, {"root": str(root), "project_context": {"role": "planner"},
                                    "context_receipt": str(root / "receipt.json")})


def test_session_receipts_isolate_hosts_agents_roles_resume_and_explicit_roots(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src/example.py").write_text("def alpha(): return 1\n")
    monkeypatch.setenv("TARE_SPECGRAPH_STATE_DIR", str(tmp_path / "index"))
    indexer.index_repository(root)
    receipt = tmp_path / "context-receipts.json"
    config = {"root": str(root), "project_context": {"files": ["src/example.py"], "timeout_ms": 1000},
              "context_receipt": str(receipt)}
    base = {"hook_event_name": "SessionStart", "cwd": str(root), "session_id": "session"}
    for host, extra in [("codex", {}), ("claude", {}), ("codex", {"agent_id": "child"}),
                        ("codex", {"agent_role": "explorer"}), ("codex", {"source": "resume"})]:
        payload = base | extra
        assert dispatch(host, payload, config)[0]
        assert dispatch(host, payload, config)[0] == {}
    assert dispatch("codex", base, config)[0] == {}
    entries = json.loads(receipt.read_text())["entries"]
    assert len(entries) == 5
    assert "session" not in json.dumps(entries) and "child" not in json.dumps(entries)
    other = tmp_path / "other"
    other.mkdir()
    indexer.index_repository(other)
    assert dispatch("agy", {"workspacePaths": [str(other), str(root)], "session_id": "session"},
                    config, event_hint="start")[0]
    assert dispatch("agy", {"workspacePaths": [str(other), str(root)], "session_id": "session"},
                    config, event_hint="start")[0] == {}


def test_session_dedup_uses_semantic_context_digest_not_observation_age(tmp_path, monkeypatch):
    from specgraph import hook_context
    root = tmp_path / "repo"
    root.mkdir()
    age = [1]
    def context(*args, **kwargs):
        return {"root": str(root), "status": "HIT", "digest": "same-mandatory-context", "role": "planner",
                "lifecycle": {"observation_age_seconds": age[0]}}
    monkeypatch.setattr(hook_context, "context_hint", context)
    config = {"root": str(root), "project_context": {"role": "planner"}, "context_receipt": str(tmp_path / "receipt.json")}
    payload = {"hook_event_name": "SessionStart", "cwd": str(root), "session_id": "s"}
    assert dispatch("codex", payload, config)[0]
    age[0] += 10
    assert dispatch("codex", payload, config)[0] == {}


def test_on_demand_session_policy_has_no_empty_banner_or_context_call(tmp_path, monkeypatch):
    from specgraph import hook_context
    from compute_plane.hook_observations import hook_host_status
    root = tmp_path / "repo"
    root.mkdir()
    def forbidden(*args, **kwargs):
        raise AssertionError("on-demand session invoked automatic context")
    monkeypatch.setattr(hook_context, "context_hint", forbidden)
    config = {"root": str(root), "project_context": {"automatic_roles": []}}
    payload = {"hook_event_name": "SessionStart", "cwd": str(root), "session_id": "s"}
    rendered, report = dispatch("codex", payload, config)
    assert rendered == {}
    assert report["project"]["reason"] == "ON_DEMAND_POLICY"
    host = hook_host_status(root, registration_paths={})["hosts"]["codex"]
    assert host["context_output"] == "NOT_OBSERVED"
    assert host["observations"][0]["reason"] == "ON_DEMAND_POLICY"


def test_malformed_external_context_receipt_keeps_usable_context_and_identifies_reference(tmp_path, monkeypatch):
    from specgraph import hook_context
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(hook_context, "context_hint", lambda *args, **kwargs: {
        "root": str(root), "status": "HIT", "role": "planner", "digest": "mandatory"})
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{broken")
    config = {"root": str(root), "project_context": {"role": "planner"}, "context_receipt": str(receipt)}
    payload = {"hook_event_name": "SessionStart", "cwd": str(root), "session_id": "s"}
    rendered, report = dispatch("codex", payload, config)
    assert rendered and report["project"]["status"] == "HIT"
    assert report["receipt_warning"]["reference"] == str(receipt)
    assert dispatch("codex", payload, config)[0] == {}
