"""Measure OS scope with one pinned engine, tracked corpus and disposable edits.

@spec SPEC-OS-SPECGRAPH-COVERAGE-001
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import time

from specgraph.indexer import index_repository
from specgraph.query import project_query, serialize_query
from specgraph.storage import RepositoryIndex
import specgraph.indexer


CASES = [
    {"id": "work-exact", "query": "relay/work_run.py", "file": "relay/work_run.py"},
    {"id": "research-exact", "query": "relay/research.py", "file": "relay/research.py"},
    {"id": "landing-exact", "query": "relay/landing_policy.py", "file": "relay/landing_policy.py"},
    {"id": "work-natural", "query": "descriptor payload from Work node", "file": "relay/work_run.py", "name": "derive_descriptor"},
    {"id": "research-natural", "query": "breadth first crawl into corpus manifest", "file": "relay/research.py", "name": "collect"},
    {"id": "landing-natural", "query": "evaluate immutable evidence without provider calls", "file": "relay/landing_policy.py", "name": "evaluate_admission"},
]
OVERLAYS = ["docs/specs/SPEC-OS-SPECGRAPH-COVERAGE-001.md",
            "docs/adr/ADR-069_SPECGRAPH_OPERATIONAL_CODE_SCOPE.md",
            "tests/test_specgraph_operational_scope.py", "tools/qualify_specgraph_coverage.py"]
ROOTS = {"src", "relay", "tools", "specs", "docs", "tests", "schemas", "sql", "adrs", "rfcs"}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def files(root, state):
    with RepositoryIndex(root, state_dir=state) as index:
        return {p: {"sha256": sha, "kind": kind} for p, sha, kind in index.connection.execute(
            "SELECT file_path, sha256, artifact_kind FROM files ORDER BY file_path")}


def measured_index(root, state, output, operation, **kwargs):
    start = time.perf_counter()
    metadata = index_repository(root, state_dir=state, **kwargs)
    receipt = {"operation": operation, "duration_ms": (time.perf_counter() - start) * 1000,
               "metadata": metadata}
    save(output / (operation + ".json"), receipt)
    return receipt


def queries(root, state, output):
    metrics = []
    for case in CASES:
        start = time.perf_counter()
        response = project_query(root, case["query"], state_dir=state, max_bytes=8192)
        elapsed = (time.perf_counter() - start) * 1000
        raw = serialize_query(response).encode("utf-8")
        (output / (case["id"] + ".json")).write_bytes(raw)
        ranks = [i for i, r in enumerate(response["results"], 1)
                 if r.get("file_path") == case["file"] and
                 ("name" not in case or r.get("name") == case["name"])]
        metrics.append({"id": case["id"], "rank": min(ranks) if ranks else None,
                        "duration_ms": elapsed, "bytes": len(raw), "sha256": digest(raw)})
    return metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--scratch", type=Path, required=True, help="New disposable native-filesystem directory")
    parser.add_argument("--output", type=Path, required=True, help="New evidence directory")
    args = parser.parse_args()
    repo, scratch, output = args.repo.resolve(), args.scratch.resolve(), args.output.resolve()
    if scratch.exists() or output.exists():
        parser.error("Scratch and output must both be new; previous evidence is never overwritten")
    if scratch == repo or repo in scratch.parents or output == scratch or scratch in output.parents:
        parser.error("Scratch must be outside the repository and separate from evidence")
    revision = subprocess.check_output(["git", "rev-parse", args.revision], cwd=repo, text=True).strip()
    archive = subprocess.check_output(["git", "archive", "--format=tar", revision], cwd=repo)
    scratch.mkdir(parents=True)
    output.mkdir(parents=True)
    corpus = scratch / "corpus"
    corpus.mkdir()
    manifest = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar:
            path = Path(member.name)
            if not member.isfile() or path.parts[0] not in ROOTS:
                continue
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("Unsafe archive path")
            raw = tar.extractfile(member).read()
            destination = corpus / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
            manifest[path.as_posix()] = {"sha256": digest(raw), "bytes": len(raw), "origin": revision}
    for name in OVERLAYS:
        raw = (repo / name).read_bytes()
        destination = corpus / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
        manifest[name] = {"sha256": digest(raw), "bytes": len(raw), "origin": "explicit current delivery overlay"}
    config_bytes = (repo / "specgraph.yaml").read_bytes()
    engine_dir = Path(specgraph.indexer.__file__).resolve().parent
    save(output / "protocol.json", {"schema": "tare.tools/os-specgraph-coverage/1",
        "revision": revision, "cases": CASES, "cases_frozen_before_measurement": True,
        "config_sha256": digest(config_bytes), "config": config_bytes.decode(),
        "engine_directory": str(engine_dir), "engine_sources": {p.relative_to(engine_dir).as_posix(): digest(p.read_bytes()) for p in sorted(engine_dir.rglob("*.py"))},
        "python": sys.version, "platform": platform.platform(), "rounds": 2,
        "timing": "in-process API wall time, native filesystem, uncontrolled OS cache/load; no SLO inference",
        "scope": "pinned tracked OS files plus four explicit equal overlays; only candidate adds config",
        "source_manifest": manifest, "authority": "NONE"})
    summaries = []
    for round_number in (1, 2):
        per_arm = {}
        # Alternate arm order to expose basic warm-cache/order variation.
        for arm in (("baseline", "candidate") if round_number == 1 else ("candidate", "baseline")):
            root = scratch / f"round-{round_number}" / arm
            shutil.copytree(corpus, root)
            state = scratch / f"state-{round_number}-{arm}"
            arm_output = output / f"round-{round_number}" / arm
            arm_output.mkdir(parents=True)
            if arm == "candidate":
                (root / "specgraph.yaml").write_bytes(config_bytes)
            full = measured_index(root, state, arm_output, "full", incremental=False)
            assert full["metadata"]["status"] == "READY", full["metadata"]["coverage"]["errors"]
            original = files(root, state)
            save(arm_output / "observations.json", original)
            noop = measured_index(root, state, arm_output, "noop", reuse_snapshot=True)
            assert noop["metadata"]["content_hash"] == full["metadata"]["content_hash"]
            assert noop["metadata"]["revision_seq"] == full["metadata"]["revision_seq"]
            assert noop["metadata"]["publication"] == "UNCHANGED"
            assert noop["metadata"]["index_run"]["strategy"] == "SOURCE_VERIFIED_NOOP"
            results = queries(root, state, arm_output)
            operations = {"full": full["duration_ms"], "noop": noop["duration_ms"]}
            mutation = {"status": "NOT_APPLICABLE_NO_CODE_SCOPE"}
            if arm == "candidate":
                path = root / "relay/work_run.py"
                old_bytes = path.read_bytes()
                path.write_bytes(old_bytes + b"\n# Disposable coverage qualification edit.\n")
                before_reindex = files(root, state)["relay/work_run.py"]["sha256"]
                assert before_reindex == digest(old_bytes)
                edit = measured_index(root, state, arm_output, "small-edit")
                assert files(root, state)["relay/work_run.py"]["sha256"] == digest(path.read_bytes())
                renamed = path.with_name("work_run_coverage_renamed.py")
                path.rename(renamed)
                rename = measured_index(root, state, arm_output, "rename")
                assert "relay/work_run.py" not in files(root, state)
                with RepositoryIndex(root, state_dir=state) as index:
                    assert not index.connection.execute("SELECT 1 FROM symbols WHERE file_path='relay/work_run.py'").fetchone()
                    assert index.connection.execute("SELECT 1 FROM symbols WHERE file_path='relay/work_run_coverage_renamed.py'").fetchone()
                renamed.unlink()
                deleted = measured_index(root, state, arm_output, "delete")
                assert "relay/work_run_coverage_renamed.py" not in files(root, state)
                with RepositoryIndex(root, state_dir=state) as index:
                    assert not index.connection.execute("SELECT 1 FROM symbols WHERE file_path='relay/work_run_coverage_renamed.py'").fetchone()
                operations.update({"small_edit": edit["duration_ms"], "rename": rename["duration_ms"], "delete": deleted["duration_ms"]})
                mutation = {"status": "PASS", "published_reads_stale_until_reindex": True,
                            "old_sha256": digest(old_bytes), "edited_sha256": digest(old_bytes + b"\n# Disposable coverage qualification edit.\n"),
                            "revisions": [r["metadata"]["revision_seq"] for r in (full, noop, edit, rename, deleted)]}
            per_arm[arm] = {"coverage": full["metadata"]["coverage"], "governance": full["metadata"]["governance"],
                            "operations_ms": operations, "noop_strategy": noop["metadata"]["index_run"].get("strategy"),
                            "queries": results, "mutations": mutation, "observations": original}
        baseline, candidate = per_arm["baseline"], per_arm["candidate"]
        lost = [p for p, value in baseline["observations"].items() if candidate["observations"].get(p) != value]
        assert not lost, lost
        assert baseline["governance"] == candidate["governance"]
        invariants = {"baseline_observations_preserved": len(baseline["observations"]),
                      "lost_or_changed_baseline_observations": lost, "governance_equal": True}
        for arm in per_arm.values():
            del arm["observations"]
        summaries.append({"round": round_number, "invariants": invariants, "arms": per_arm})
    save(output / "summary.json", {"schema": "tare.tools/os-specgraph-coverage-result/1", "rounds": summaries,
        "limits": ["two runs do not qualify p95", "descriptive queries informed by docstrings", "no agent task or token-savings claim",
                   "indexed code remains outside governance unless under src", "no automatic watcher; explicit reindex required"]})
    print(json.dumps({"output": str(output), "rounds": len(summaries), "status": "PASS"}))


if __name__ == "__main__":
    main()
