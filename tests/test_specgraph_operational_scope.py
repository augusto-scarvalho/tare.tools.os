"""OS retrieval scope must not silently become a governance scope."""
from dataclasses import asdict
import hashlib
from pathlib import Path

from specgraph.config import SpecGraphConfig
from specgraph.indexer import index_repository
from specgraph.query import project_query
from specgraph.storage import RepositoryIndex


CONFIG = Path(__file__).resolve().parents[1] / "specgraph.yaml"


def write(root, name, body):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_scope_preserves_governance_and_discovery(tmp_path):
    """@verifies SPEC-OS-SPECGRAPH-COVERAGE-001:AC-01
    @verifies SPEC-OS-SPECGRAPH-COVERAGE-001:AC-05
    """
    configured = SpecGraphConfig.load(CONFIG.parent)
    defaults = SpecGraphConfig()
    for key in ("governed_paths", "spec_paths", "test_paths", "schema_paths",
                "api_paths", "adr_paths", "search_paths", "intent_mapping_paths",
                "strict_orphan_check", "project_name", "python_source_roots"):
        assert asdict(configured)[key] == asdict(defaults)[key], key
    assert configured.indexed_paths == ["src", "relay", "tools"]
    assert configured.python_source_roots == ["src", "."]
    assert not configured.config_errors
    root, state = tmp_path / "repo", tmp_path / "state"
    write(root, "src/contract.py", "def governed(): return 1\n")
    write(root, "relay/work_run.py", "def run_work(): return 2\n")
    write(root, "tools/check.py", "def check(): return 3\n")
    for excluded in ("relay/history", "relay/trains", "relay/round_tables", "tools/tests"):
        write(root, excluded + "/old.py", "def misleading_copy(): return 4\n")
    write(root, "docs/specs/kept.md", "---\nid: SPEC-KEPT\n---\nKept requirement.\n")
    write(root, "docs/adr/kept.md", "---\nid: ADR-KEPT\n---\nKept decision.\n")
    before = index_repository(root, state_dir=state)
    with RepositoryIndex(root, state_dir=state) as idx:
        original = dict(idx.connection.execute("SELECT file_path, sha256 FROM files"))
    write(root, "specgraph.yaml", CONFIG.read_text(encoding="utf-8"))
    after = index_repository(root, state_dir=state)
    with RepositoryIndex(root, state_dir=state) as idx:
        observed = dict(idx.connection.execute("SELECT file_path, sha256 FROM files"))
        names = {row[0] for row in idx.connection.execute("SELECT name FROM symbols")}
    assert all(observed.get(path) == sha for path, sha in original.items())
    assert "run_work" in names and "check" in names
    assert "misleading_copy" not in names
    assert before["governance"] == after["governance"]


def test_operational_edits_rename_delete_and_noop(tmp_path):
    """@verifies SPEC-OS-SPECGRAPH-COVERAGE-001:AC-03
    @verifies SPEC-OS-SPECGRAPH-COVERAGE-001:AC-04
    """
    root, state = tmp_path / "repo", tmp_path / "state"
    write(root, "specgraph.yaml", CONFIG.read_text(encoding="utf-8"))
    path = write(root, "relay/operational.py", 'def witness():\n    """Operational witness."""\n    return 1\n')
    first = index_repository(root, state_dir=state)
    noop = index_repository(root, state_dir=state, reuse_snapshot=True)
    assert noop["publication"] == "UNCHANGED"
    assert noop["revision_seq"] == first["revision_seq"]
    assert noop["index_run"]["strategy"] == "SOURCE_VERIFIED_NOOP"
    old = project_query(root, "witness", state_dir=state)["results"][0]
    path.write_text(path.read_text().replace("return 1", "return 2"), encoding="utf-8")
    # Reads explicitly reflect published data until indexing, not live freshness.
    assert project_query(root, "witness", state_dir=state)["results"][0]["source"] == old["source"]
    edited = index_repository(root, state_dir=state)
    new = project_query(root, "witness", state_dir=state)["results"][0]
    assert edited["revision_seq"] > first["revision_seq"]
    assert new["source"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert new["source"]["sha256"] != old["source"]["sha256"]
    moved = path.with_name("moved.py")
    path.rename(moved)
    index_repository(root, state_dir=state)
    with RepositoryIndex(root, state_dir=state) as idx:
        assert not idx.connection.execute("SELECT 1 FROM files WHERE file_path=?", ("relay/operational.py",)).fetchone()
        assert not idx.connection.execute("SELECT 1 FROM symbols WHERE file_path=?", ("relay/operational.py",)).fetchone()
    assert project_query(root, "witness", state_dir=state)["results"][0]["file_path"] == "relay/moved.py"
    moved.unlink()
    index_repository(root, state_dir=state)
    assert not project_query(root, "witness", state_dir=state)["results"]
    with RepositoryIndex(root, state_dir=state) as idx:
        assert not idx.connection.execute("SELECT 1 FROM files WHERE file_path=?", ("relay/moved.py",)).fetchone()
