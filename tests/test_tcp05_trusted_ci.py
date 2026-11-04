"""TRAIN-05-TCP-05 falsifiers: trusted CI control plane (trusted_ci.py +
trusted_verifier.validate_ci_execution)."""
import json
import os
import sys
import time
from pathlib import Path

import pytest

RELAY_DIR = Path(__file__).resolve().parents[1]
HARNESS_LIB_LOCAL = RELAY_DIR / "scripts" / "harness_lib"
HARNESS_SCRIPTS_LOCAL = RELAY_DIR / "scripts"
HARNESS_LIB_REMOTE = Path(os.environ.get(
    "HARNESS_REPO_DIR", "C:/projects/universal-agent-harness-prototype")) / "scripts" / "harness_lib"
HARNESS_SCRIPTS_REMOTE = HARNESS_LIB_REMOTE.parent

for p in [str(HARNESS_SCRIPTS_LOCAL), str(HARNESS_LIB_LOCAL), str(HARNESS_LIB_REMOTE), str(HARNESS_SCRIPTS_REMOTE)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from harness_lib import sandbox_spawn as sbx  # noqa: E402
from harness_lib import trusted_ci as tci  # noqa: E402
from harness_lib import trusted_verifier as tv  # noqa: E402

PY = sys.executable
FP = "fp-tcp05-1"


@pytest.fixture()
def env(tmp_path):
    root = tmp_path / "root"
    ws = tmp_path / "ws"
    root.mkdir()
    ws.mkdir()
    return root, ws


def _run_ok(root, ws, **kw):
    return tci.run_hermetic([PY, "-c", "print('ok')"], root=root, workspace=ws,
                            subject_fingerprint=FP, timeout=120, **kw)


def test_green_run_mints_verifiable_receipt(env):
    root, ws = env
    out = _run_ok(root, ws)
    assert out["returncode"] == 0
    ok, why = tci.verify_test_receipt(root, out["executionId"], expected_fingerprint=FP)
    assert ok, why


def test_failing_run_is_never_ci_proof(env):
    root, ws = env
    out = tci.run_hermetic([PY, "-c", "raise SystemExit(3)"], root=root, workspace=ws,
                           subject_fingerprint=FP, timeout=120)
    assert out["returncode"] == 3
    ok, why = tci.verify_test_receipt(root, out["executionId"])
    assert not ok and "exited 3" in why


def test_tampered_receipt_refused_even_flipping_exit_to_zero(env):
    root, ws = env
    out = tci.run_hermetic([PY, "-c", "raise SystemExit(1)"], root=root, workspace=ws,
                           subject_fingerprint=FP, timeout=120)
    path = tci._receipt_path(root, out["executionId"])
    data = json.loads(path.read_text(encoding="utf-8"))
    data["exitCode"] = 0  # forge PASS
    path.write_text(json.dumps(data), encoding="utf-8")
    ok, why = tci.verify_test_receipt(root, out["executionId"])
    assert not ok and "tamper" in why


def test_unauthenticated_network_call_fails_closed(env):
    root, ws = env
    out = tci.run_hermetic(
        [PY, "-c", "import urllib.request;urllib.request.urlopen('http://example.com', timeout=5)"],
        root=root, workspace=ws, subject_fingerprint=FP, timeout=120)
    assert out["returncode"] != 0, "network call should fail under the blackhole proxy"
    ok, why = tci.verify_test_receipt(root, out["executionId"])
    assert not ok  # failing run, and honestly recorded
    receipt = out["receipt"]
    assert receipt["networkBoundary"] == {"class": "env-proxy-blackhole", "kernelEnforced": False}


def test_fingerprint_mismatch_and_replay_refused(env):
    root, ws = env
    out = _run_ok(root, ws)
    ok, why = tci.verify_test_receipt(root, out["executionId"], expected_fingerprint="fp-other")
    assert not ok and "fingerprint mismatch" in why
    ok, why = tci.verify_test_receipt(root, "CI-deadbeef")
    assert not ok and "missing" in why


def test_stale_receipt_refused(env):
    root, ws = env
    out = _run_ok(root, ws)
    ok, why = tci.verify_test_receipt(root, out["executionId"], now=time.time() + 7200)
    assert not ok and "stale" in why


def test_workspace_outside_boundary_refused(env):
    root, ws = env
    with pytest.raises(sbx.SandboxRefused):
        tci.run_hermetic([PY, "-c", "print(1)"], root=root,
                         workspace=ws / ".." / "nonexistent-escape",
                         subject_fingerprint=FP, timeout=60)


def test_confinement_link_composes_tcp04(env):
    root, ws = env
    good = {"fsEnforced": True, "fsApplied": ["AGENTS.md"], "fsBackend": "nt-icacls"}
    sbx.mint_confinement_evidence(root, good, execution_id="SX-ci-1", subject_fingerprint=FP)
    out = _run_ok(root, ws, confinement_execution_id="SX-ci-1")
    ok, why = tci.verify_test_receipt(root, out["executionId"], require_confinement=True)
    assert ok, why
    # broken link: evidence for a bypassed run
    sbx.mint_confinement_evidence(root, dict(good, sandboxBypassed=True),
                                  execution_id="SX-ci-2", subject_fingerprint=FP)
    out2 = _run_ok(root, ws, confinement_execution_id="SX-ci-2")
    ok, why = tci.verify_test_receipt(root, out2["executionId"])
    assert not ok and "confinement evidence refused" in why
    # no link at all when caller requires one
    out3 = _run_ok(root, ws)
    ok, why = tci.verify_test_receipt(root, out3["executionId"], require_confinement=True)
    assert not ok and "no confinement evidence link" in why


def _record(tree="tree-ci-1"):
    return tv.TrustedVerifierRecord(
        "TV-CI-1", tree, "v1", "epoch-1", "2026-08-16T00:00:00+00:00", "owner", None,
        "evidence:test", "CANDIDATE", "HEAD", tv.STRICT_PROOF_PROFILE,
        ("tiny",), ("trusted-harness-test",), ("policy-v1",), None)


def test_ci_receipt_bound_to_pinned_verifier(env):
    root, ws = env
    record = _record()
    fields = {"trustedVerifierId": record.trusted_verifier_id,
              "verifierTreeId": record.verifier_tree_id,
              "proofProfileDigest": record.proof_profile_digest}
    out = _run_ok(root, ws, verifier_fields=fields)
    res = tv.validate_ci_execution(root, out["executionId"], record, expected_fingerprint=FP)
    assert res["ok"], res
    # receipt shopping: same receipt against a DIFFERENT verifier tree refuses
    other = _record(tree="tree-ci-OTHER")
    res = tv.validate_ci_execution(root, out["executionId"], other)
    assert not res["ok"] and res["reason"] == "CI_RECEIPT_NOT_BOUND_TO_THIS_VERIFIER"
    # unbound receipt (no verifier fields) refuses too
    out2 = _run_ok(root, ws)
    res = tv.validate_ci_execution(root, out2["executionId"], record)
    assert not res["ok"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only symlink semantics")
def test_posix_workspace_symlink_escape_refused(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    alias = tmp_path / "workspace-alias"
    root.mkdir()
    outside.mkdir()
    alias.symlink_to(outside, target_is_directory=True)

    with pytest.raises(sbx.SandboxRefused, match="WORKSPACE_ALIAS_REFUSED"):
        tci.run_hermetic([PY, "-c", "print('must not run')"], root=root,
                         workspace=alias, subject_fingerprint=FP, timeout=60)


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only descriptor-pinned cwd")
def test_posix_workspace_swap_cannot_redirect_execution(tmp_path, monkeypatch):
    root = tmp_path / "root"
    workspace = tmp_path / "workspace"
    held_workspace = tmp_path / "workspace-held"
    outside = tmp_path / "outside"
    for path in (root, workspace, outside):
        path.mkdir()

    real_run = tci.subprocess.run

    def swap_then_run(*args, **kwargs):
        workspace.rename(held_workspace)
        workspace.symlink_to(outside, target_is_directory=True)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(tci.subprocess, "run", swap_then_run)
    script = (
        "import os, pathlib; "
        "pathlib.Path('cwd-marker').write_text('inside'); "
        "(pathlib.Path(os.environ['HOME']) / 'home-marker').write_text('inside')"
    )
    out = tci.run_hermetic([PY, "-c", script], root=root, workspace=workspace,
                           subject_fingerprint=FP, timeout=60)

    assert out["returncode"] == 0, out["stderr"]
    assert (held_workspace / "cwd-marker").read_text() == "inside"
    assert (held_workspace / ".trusted-ci-scratch" / "home-marker").read_text() == "inside"
    assert not (outside / "cwd-marker").exists()
    assert not (outside / ".trusted-ci-scratch" / "home-marker").exists()
    assert out["receipt"]["workspaceIdentity"].startswith("posix:")
