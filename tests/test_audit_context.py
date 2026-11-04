"""Real SpecGraph selection and delta measurements, no vendor calls."""
from copy import deepcopy
from pathlib import Path
import subprocess

import pytest

from relay.audit_packet import prepare, correction, validate_review, budget, recheck
from test_audit_packet import selection, candidate, cases, request_data, review


@pytest.fixture
def structural(selection):
    root = Path(selection["repositories"][0]["root"])
    (root / "specgraph.yaml").write_text(
        "governed_paths: [fix.py, helpers.py]\nspec_paths: [specs]\ntest_paths: [tests]\n"
        "schema_paths: []\napi_paths: []\nadr_paths: []\n", encoding="utf-8")
    (root / "specs").mkdir()
    (root / "specs/SPEC-001.md").write_text(
        "---\nid: SPEC-001\ntitle: Correct answer\nstatus: ACCEPTED\n"
        "acceptance_criteria:\n  AC-01: Answer must be two\n---\nNo other behavior changes.\n",
        encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests/test_answer.py").write_text(
        'def test_answer():\n    """@verifies SPEC-001:AC-01"""\n    from fix import answer\n'
        "    assert answer() == 2\n", encoding="utf-8")
    prefix = 'from helpers import identity\n\ndef answer():\n    """@spec SPEC-001"""\n'
    (root / "helpers.py").write_text("def identity(value):\n    return value\n", encoding="utf-8")
    unrelated = "\ndef unrelated():\n" + '    # unrelated unchanged body\n' * 180 + "    return 7\n"
    (root / "fix.py").write_text(prefix + "    return identity(0)\n" + unrelated, encoding="utf-8")
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), "-c", "user.name=Fixture",
            "-c", "user.email=fixture@example.invalid", "-c", "core.autocrlf=false", *args]).decode().strip()
    git("add", ".")
    git("commit", "-qm", "Governed fixture")
    selection["repositories"][0]["base"] = git("rev-parse", "HEAD")
    (root / "fix.py").write_text(prefix + "    return identity(1)\n" + unrelated, encoding="utf-8")
    return selection


def test_real_specgraph_selects_contract_tests_and_dependency_without_writes(structural, record_property):
    full = prepare(structural)
    root = Path(structural["repositories"][0]["root"])
    before = {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file() and ".git" not in path.parts}
    structural["repositories"][0]["context_strategy"] = "specgraph"
    packet = prepare(structural)
    paths = {source["path"] for source in packet["input"]["sources"]}
    assert {"specs/SPEC-001.md", "tests/test_answer.py", "helpers.py"} <= paths
    after = next(source for source in packet["input"]["sources"] if source["id"] == "C1.after")
    assert after["segments"] and after["omitted_lines"] >= 180
    assert "identity(1)" in "".join(segment["text"] for segment in after["segments"])
    assert budget(packet)["initial_context_bytes"] < budget(full)["initial_context_bytes"]
    record_property("full_context_bytes", budget(full)["initial_context_bytes"])
    record_property("specgraph_context_bytes", budget(packet)["initial_context_bytes"])
    assert prepare(structural) == packet
    assert before == {str(path): path.read_bytes() for path in root.rglob("*") if path.is_file() and ".git" not in path.parts}
    assert not (root / ".specgraph").exists()
    (root / "helpers.py").write_text("def identity(value):\n    return 9\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        recheck(packet)


def test_automatic_specgraph_context_refuses_binary_spec_body(structural):
    root = Path(structural['repositories'][0]['root'])
    spec = root / 'specs/SPEC-001.md'
    spec.write_bytes(spec.read_bytes() + b'Binary body: \0\n')
    subprocess.run(['git', '-C', str(root), 'add', 'specs/SPEC-001.md'], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=Fixture', '-c',
        'user.email=fixture@example.invalid', 'commit', '-qm', 'Binary governed spec'], check=True)
    structural['repositories'][0].update(context_strategy='specgraph', base=subprocess.check_output(
        ['git', '-C', str(root), 'rev-parse', 'HEAD']).decode().strip())
    with pytest.raises(ValueError, match='binary_source_requires_explicit_strategy'):
        prepare(structural)


def test_segment_location_must_be_present_not_just_inside_file(structural):
    structural["repositories"][0]["context_strategy"] = "specgraph"
    packet = prepare(structural)
    value = review(packet)
    value["findings"][0]["location"].update(start_line=5, end_line=5)
    validate_review(packet, value)
    value["findings"][0]["location"].update(start_line=30, end_line=30)
    with pytest.raises(ValueError, match="outside_source"):
        validate_review(packet, value)


def test_unmapped_or_unresolved_context_never_spends(structural):
    root = Path(structural["repositories"][0]["root"])
    structural["repositories"][0]["context_strategy"] = "specgraph"
    (root / "specs/SPEC-001.md").write_text("No formal mapping", encoding="utf-8")
    structural["repositories"][0]["excluded"] = [{"path": "specs/SPEC-001.md", "reason": "Fixture corruption"}]
    with pytest.raises(ValueError, match="specgraph_"):
        prepare(structural)


def test_correction_uses_last_candidate_not_feature_base(structural):
    structural["repositories"][0]["context_strategy"] = "specgraph"
    first = prepare(structural)
    value = review(first)
    value["findings"][0]["location"].update(start_line=5, end_line=5)
    root = Path(structural["repositories"][0]["root"])
    path = root / "fix.py"
    path.write_text(path.read_text(encoding="utf-8").replace("identity(1)", "identity(2)"), encoding="utf-8")
    structural["packet_id"] = "fix-r2"
    second = prepare(structural)
    reply = {"schema": "tare.tools/audit-correction/1", "packet_id": "fix-r1",
        "dispositions": [{"finding_id": "F1", "disposition": "FIXED", "change_refs": ["C1"],
                          "test_receipt_refs": ["T1"], "reason": ""}]}
    delta = correction(second, first, value, reply)
    diff = next(source["text"] for source in delta["input"]["sources"] if source["id"] == "C1.diff")
    assert "-    return identity(1)" in diff and "+    return identity(2)" in diff
    assert "identity(0)" not in diff
    assert delta["input"]["changes"][0]["diff_basis"] == "fix-r1"
    assert not any(source["id"] == "C1.before" for source in delta["input"]["sources"])
    assert len(delta["input"]["obligations"]) == len(first["input"]["obligations"])


def test_unknown_dependency_refuses_automatic_selection(structural):
    root = Path(structural["repositories"][0]["root"])
    path = root / "fix.py"
    path.write_text(path.read_text(encoding="utf-8").replace("from helpers import identity",
                                                           "from missing_package import identity"), encoding="utf-8")
    structural["repositories"][0]["context_strategy"] = "specgraph"
    with pytest.raises(ValueError, match="dependency_context_missing"):
        prepare(structural)


def test_historical_snapshot_never_uses_current_graph_by_accident(structural):
    root = structural["repositories"][0]["root"]
    subprocess.run(["git", "-C", root, "add", "fix.py"], check=True)
    structural["repositories"][0].update(context_strategy="specgraph", target="INDEX")
    with pytest.raises(ValueError, match="current_worktree"):
        prepare(structural)


@pytest.mark.parametrize("replacement,reason", [
    ("(lambda: 2)()", "dynamic_call_target"),
    ("missing_callable()", "unbound_name"),
])
def test_unknown_call_reference_without_edge_refuses_context(structural, replacement, reason):
    path = Path(structural["repositories"][0]["root"]) / "fix.py"
    path.write_text(path.read_text(encoding="utf-8").replace("identity(1)", replacement), encoding="utf-8")
    structural["repositories"][0]["context_strategy"] = "specgraph"
    with pytest.raises(ValueError, match="dependency_context_missing.*" + reason):
        prepare(structural)


def test_stdlib_and_unshadowed_builtin_remain_qualified_exceptions(structural):
    path = Path(structural["repositories"][0]["root"]) / "fix.py"
    path.write_text("import math\n" + path.read_text(encoding="utf-8").replace(
        "identity(1)", "math.floor(len([1, 2]))"), encoding="utf-8")
    structural["repositories"][0]["context_strategy"] = "specgraph"
    assert prepare(structural)["input"]["context"]["structural_selection"]
