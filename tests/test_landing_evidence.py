import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

from relay.landing_evidence import materialize_landing_evidence


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repository(tmp_path: Path, content: str = "changed\n"):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "src").mkdir()
    (repo / "src" / "value.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    _git(repo, "branch", "-M", "main")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "src" / "value.txt").write_text(content, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "candidate")
    candidate = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/heads/main", base)
    return repo, base, candidate


def _install_graph_fakes(monkeypatch):
    backlog = ModuleType("compute_plane.backlog_work")
    specgraph = ModuleType("compute_plane.specgraph_grounding")
    runtime = ModuleType("tare_tools_agent_runtime")

    scope = SimpleNamespace(
        repository="repo-one",
        target_paths=("src",),
        grounding_refs=("SPEC-ONE",),
    )

    class Work:
        def __init__(self, _raw):
            self.work_id = "work-one"
            self.execution_scope_sha256 = "3" * 64
            self.repository_scopes = (scope,)

    class Grounding:
        def __init__(self, _raw):
            self.repository = "repo-one"
            self.requested_ids = ("SPEC-ONE",)
            self.grounding_scope_sha256 = "4" * 64
            self.status = "READY"

    runtime.WorkEnvelope = Work
    runtime.GroundingEnvelope = Grounding
    backlog.materialize_backlog_work = lambda **kwargs: b"work"
    backlog.check_backlog_scope = lambda **kwargs: SimpleNamespace(
        decision=SimpleNamespace(value="CURRENT")
    )
    specgraph.materialize_specgraph_grounding = lambda **kwargs: b"grounding"
    specgraph.check_specgraph_scope = lambda **kwargs: SimpleNamespace(
        decision=SimpleNamespace(value="CURRENT")
    )
    monkeypatch.setitem(sys.modules, "compute_plane.backlog_work", backlog)
    monkeypatch.setitem(sys.modules, "compute_plane.specgraph_grounding", specgraph)
    monkeypatch.setitem(sys.modules, "tare_tools_agent_runtime", runtime)


def _materialize(tmp_path: Path, monkeypatch, content: str):
    _install_graph_fakes(monkeypatch)
    repo, base, candidate = _repository(tmp_path, content)
    train = tmp_path / "train"
    train.mkdir()
    descriptor = SimpleNamespace(
        work_id="work-one",
        execution_repository="repo-one",
        tracked_paths=("src",),
        base_commit=base,
        target_ref="refs/heads/main",
        sha256="1" * 64,
    )
    receipt = SimpleNamespace(
        sha256="2" * 64,
        payload={"candidate_commit": candidate, "changed_paths": ["src/value.txt"]},
    )
    return materialize_landing_evidence(
        train_dir=train,
        train_id="TRAIN-ONE",
        descriptor=descriptor,
        candidate_receipt=receipt,
        repository_root=repo,
        work_graph=tmp_path / "work-graph.json",
        library_manifest=tmp_path / "manifest.json",
    )


def test_metadata_only_fakes_no_longer_satisfy_source_admission(tmp_path, monkeypatch):
    evidence = _materialize(tmp_path, monkeypatch, "changed\n")

    assert evidence.payload["checks"]["scope_complete"] is False
    assert evidence.payload["checks"]["verification_complete"] is False
    assert all(value for key, value in evidence.payload["checks"].items() if key not in {"scope_complete", "verification_complete"})
    assert (tmp_path / "train" / "LANDING_EVIDENCE.json").read_bytes() == evidence.canonical_bytes


def test_materializer_refuses_candidate_with_secret_pattern(tmp_path, monkeypatch):
    evidence = _materialize(
        tmp_path,
        monkeypatch,
        "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456\n",
    )

    assert evidence.payload["checks"]["secrets_clear"] is False


def test_reobservation_reuses_identical_evidence_and_refuses_changed_work(tmp_path, monkeypatch):
    import pytest
    captured = {}
    original = materialize_landing_evidence
    def observe(**kwargs):
        captured.update(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(sys.modules[__name__], 'materialize_landing_evidence', observe)
    first = _materialize(tmp_path, monkeypatch, 'changed\n')
    assert original(**captured).canonical_bytes == first.canonical_bytes
    backlog = sys.modules['compute_plane.backlog_work']
    monkeypatch.setattr(backlog, 'check_backlog_scope', lambda **kw: SimpleNamespace(
        decision=SimpleNamespace(value='CHANGED')))
    with pytest.raises(ValueError, match='LANDING_EVIDENCE_CHANGED:LANDING_EVIDENCE.json'):
        original(**captured)
    assert (tmp_path/'train/LANDING_EVIDENCE.json').read_bytes() == first.canonical_bytes
