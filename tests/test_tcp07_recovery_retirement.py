"""TRAIN-09-TCP-07 falsifiers: retire recovery authority (the TCP capping seam).

Permanent revocation of the bootstrap recovery/override authority, verified by
trusted_verifier. Every fixture lives in an isolated scratch root — no
production trust seeding, the real trust store / work graph / relay untouched.
"""
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

for p in [str(HARNESS_SCRIPTS_LOCAL), str(HARNESS_LIB_LOCAL),
          str(HARNESS_LIB_REMOTE), str(HARNESS_SCRIPTS_REMOTE)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from harness_lib import bootstrap_envelope as be  # noqa: E402
from harness_lib import decision_inbox  # noqa: E402
from harness_lib import recovery_retirement as rr  # noqa: E402
from harness_lib import trusted_verifier as tv  # noqa: E402

TASK = "tcp07-demo"
TREE = "tree-tcp07-1"
DECISION_ID = "DEC-TCP07-1"


def _record():
    return tv.TrustedVerifierRecord(
        "TV-TCP07-1", TREE, "v1", "epoch-1", "2026-08-16T00:00:00+00:00", "owner", None,
        "evidence:test", "CANDIDATE", "HEAD", tv.STRICT_PROOF_PROFILE,
        ("tiny",), ("trusted-harness-test",), ("policy-v1",),
        {"decisionId": DECISION_ID,
         "decisionDigest": decision_inbox.decision_digest_v2(
             DECISION_ID, "escalation", "retire recovery authority", TREE)})


@pytest.fixture()
def env(tmp_path):
    root = tmp_path / "scratch-root"
    root.mkdir()
    t0 = time.time()
    root_env = be.mint_root("owner", [TASK], max_children=4, issued_at=t0)
    owner_child = be.derive_child(root_env, TASK, now=t0 + 1)
    record = _record()
    verifier = {"trustedVerifierId": record.trusted_verifier_id,
                "verifierTreeId": record.verifier_tree_id,
                "proofProfileDigest": record.proof_profile_digest}
    return root, root_env, owner_child, record, verifier


# -- DoD 1+3: retirement mints a receipt trusted_verifier can re-verify --------

def test_retire_mints_receipt_bound_to_verifier(env):
    root, root_env, owner_child, record, verifier = env
    receipt = rr.retire_recovery_authority(
        root, root_envelope=root_env, owner_child_envelope=owner_child,
        task_id=TASK, retired_by="owner", verifier=verifier)
    assert receipt["sealed"] and receipt["irreversible"]
    assert set(receipt["revokedAuthorities"]) == set(rr.REVOKED_AUTHORITIES)
    root_oid = root_env["oid"]
    ok, why = rr.verify_retirement_receipt(root, root_oid, root_envelope=root_env, verifier=verifier)
    assert ok, why
    out = tv.validate_recovery_retirement(root, root_oid, record, root_envelope=root_env)
    assert out["ok"] and out["retired"], out
    assert rr.is_retired(root, root_oid, root_envelope=root_env)


# -- Falsifier: non-owner revoke rejected -------------------------------------

def test_non_owner_retirement_rejected(env):
    root, root_env, owner_child, record, verifier = env
    forged = dict(owner_child, oid="deadbeef")  # authority envelope that won't re-derive
    with pytest.raises(rr.RecoveryRetirementRefused, match="NON_OWNER"):
        rr.retire_recovery_authority(root, root_envelope=root_env, owner_child_envelope=forged,
                                     task_id=TASK, retired_by="intruder")
    # an identity string / empty envelope is not authority
    with pytest.raises(rr.RecoveryRetirementRefused):
        rr.retire_recovery_authority(root, root_envelope=root_env, owner_child_envelope={},
                                     task_id=TASK, retired_by="intruder")
    # nothing was sealed
    assert not rr.is_retired(root, root_env["oid"], root_envelope=root_env)


# -- Falsifier + DoD 2: any override after retirement fails closed ------------

def test_override_rejected_after_retirement(env):
    root, root_env, owner_child, record, verifier = env
    root_oid = root_env["oid"]
    # before retirement: an owner-authorized override is granted
    req = {"ownerEnvelope": owner_child, "taskId": TASK}
    pre = rr.authorize_recovery_override(root, root_oid=root_oid, override_request=req,
                                         root_envelope=root_env)
    assert pre["ok"] and pre["outcome"] == "RECOVERY_OVERRIDE_GRANTED", pre
    # an unauthorized override is refused even before retirement (never a free bypass)
    bad = rr.authorize_recovery_override(root, root_oid=root_oid,
                                         override_request={"ownerEnvelope": {}, "taskId": TASK},
                                         root_envelope=root_env)
    assert not bad["ok"]
    # retire, then EVERY override — even the previously-valid owner one — fails closed
    rr.retire_recovery_authority(root, root_envelope=root_env, owner_child_envelope=owner_child,
                                 task_id=TASK, retired_by="owner", verifier=verifier)
    post = rr.authorize_recovery_override(root, root_oid=root_oid, override_request=req,
                                          root_envelope=root_env)
    assert not post["ok"] and post["outcome"] == "RECOVERY_REVOKED_REJECTED", post


# -- Falsifier: forged / stale / mis-bound receipts rejected ------------------

def test_forged_stale_and_misbound_receipts_rejected(env):
    root, root_env, owner_child, record, verifier = env
    root_oid = root_env["oid"]
    rr.retire_recovery_authority(root, root_envelope=root_env, owner_child_envelope=owner_child,
                                 task_id=TASK, retired_by="owner", verifier=verifier)
    path = rr._receipt_path(root, root_oid)
    good = json.loads(path.read_text(encoding="utf-8"))

    # forged: mutate a field without repairing the digest
    path.write_text(json.dumps(dict(good, retiredBy="attacker")), encoding="utf-8")
    assert not rr.verify_retirement_receipt(root, root_oid, root_envelope=root_env)[0]

    # stale: a valid receipt presented under a DIFFERENT (newer) root
    path.write_text(json.dumps(good), encoding="utf-8")
    other_root = be.mint_root("owner", [TASK], max_children=4, issued_at=time.time() + 5)
    ok, why = rr.verify_retirement_receipt(root, root_oid, root_envelope=other_root)
    assert not ok and "stale" in why.lower()

    # mis-bound: receipt minted for verifier A, validated against verifier B (shopping)
    other = tv.TrustedVerifierRecord(
        "TV-OTHER", TREE, "v1", "e", "2026-08-16T00:00:00+00:00", "owner", None,
        "evidence:test", "CANDIDATE", "HEAD", tv.STRICT_PROOF_PROFILE,
        ("tiny",), ("trusted-harness-test",), ("policy-v1",), record.promotion_receipt)
    out = tv.validate_recovery_retirement(root, root_oid, other, root_envelope=root_env)
    assert not out["ok"] and "VERIFIER" in out["reason"]

    # absent: a never-retired root is simply not retired
    assert not rr.is_retired(root, "nonexistent-root-oid")


# -- Falsifier + DoD: retirement is irreversible ------------------------------

def test_retirement_is_irreversible(env):
    root, root_env, owner_child, record, verifier = env
    root_oid = root_env["oid"]
    first = rr.retire_recovery_authority(root, root_envelope=root_env, owner_child_envelope=owner_child,
                                         task_id=TASK, retired_by="owner", verifier=verifier)
    # idempotent: a second retire returns the same seal, never a fresh mint
    again = rr.retire_recovery_authority(root, root_envelope=root_env, owner_child_envelope=owner_child,
                                         task_id=TASK, retired_by="owner", verifier=verifier)
    assert again["receiptDigest"] == first["receiptDigest"]
    # reinstatement is refused for EVERYONE (owner included)
    out = rr.reinstate_recovery_authority(root, root_oid=root_oid)
    assert not out["ok"] and out["outcome"] == "RECOVERY_RETIREMENT_IRREVERSIBLE"
    # a corrupted existing seal cannot be silently overwritten by a re-retire
    rr._receipt_path(root, root_oid).write_text('{"schema":"x"}', encoding="utf-8")
    with pytest.raises(rr.RecoveryRetirementRefused, match="irreversible"):
        rr.retire_recovery_authority(root, root_envelope=root_env, owner_child_envelope=owner_child,
                                     task_id=TASK, retired_by="owner", verifier=verifier)


# -- Falsifier: the other TCP seams stay intact (no regression) ---------------

def test_genesis_preconditions_still_intact():
    report = tv.genesis_precondition_report()
    assert set(report["preconditions"]) == set(tv.GENESIS_PRECONDITIONS)
    assert report["genesisEligible"] is False
