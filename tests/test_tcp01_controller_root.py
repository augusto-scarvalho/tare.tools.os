"""TRAIN-02-TCP-01 tests: trusted first byte / incumbent controller root."""
import os
import sys
from pathlib import Path

import pytest

RELAY_DIR = Path(__file__).resolve().parents[1]
HARNESS_LIB_LOCAL = RELAY_DIR / "scripts" / "harness_lib"
HARNESS_LIB_REMOTE = Path(os.environ.get(
    "HARNESS_REPO_DIR", "C:/projects/universal-agent-harness-prototype")) / "scripts" / "harness_lib"

for p in [str(HARNESS_LIB_LOCAL), str(HARNESS_LIB_REMOTE)]:
    if p not in sys.path:
        sys.path.insert(0, p)

import bootstrap_envelope as be  # noqa: E402
import tcp01_controller_root as tcp  # noqa: E402

T0 = 1_000_000.0


@pytest.fixture()
def controller(tmp_path):
    root = be.mint_root("owner", ["tcp01"], max_children=8, issued_at=T0)
    c = tcp.ControllerRoot(root, tmp_path / "ledger.jsonl")
    calls = []
    c.register("status", lambda: calls.append("status") or "ok", mutating=False)
    c.register("write", lambda: calls.append("write") or "wrote", mutating=True)
    c.freeze()
    return c, root, calls


def _child(root):
    return be.derive_child(root, "tcp01", now=T0 + 1)


def test_registry_deterministic_and_frozen(controller):
    c, root, _ = controller
    with pytest.raises(tcp.AuthorityError):
        c.register("late", lambda: None, mutating=False)
    c2 = tcp.ControllerRoot(root, Path(c.ledger_path))
    c2.register("write", lambda: None, mutating=True)
    c2.register("status", lambda: None, mutating=False)
    assert c2.freeze() == c.freeze(), "registry digest must be order-independent"


def test_unfrozen_and_unknown_fail_closed(tmp_path, controller):
    c, root, calls = controller
    unfrozen = tcp.ControllerRoot(root, tmp_path / "l2.jsonl")
    unfrozen.register("x", lambda: calls.append("x"), mutating=False)
    with pytest.raises(tcp.AuthorityError):
        unfrozen.dispatch("x")
    with pytest.raises(tcp.AuthorityError):
        c.dispatch("nope")
    assert calls == [], "no handler byte may run on failed dispatch"


def test_readonly_dispatches_without_envelope(controller):
    c, _, calls = controller
    assert c.dispatch("status") == "ok"
    assert calls == ["status"]


def test_mutating_requires_valid_envelope_and_lock(controller):
    c, root, calls = controller
    child = _child(root)
    with pytest.raises(tcp.AuthorityError):  # no envelope
        c.dispatch("write")
    with pytest.raises(tcp.AuthorityError):  # forged envelope
        c.dispatch("write", child_envelope=dict(child, seq=child["seq"] + 1))
    with pytest.raises(tcp.AuthorityError):  # valid envelope but no lock
        c.dispatch("write", child_envelope=child)
    assert calls == []
    with c.lock:
        assert c.dispatch("write", child_envelope=child) == "wrote"
    assert calls == ["write"]


def test_single_writer_boundary(controller, tmp_path):
    c, root, _ = controller
    other = tcp.ControllerRoot(root, tmp_path / "other.jsonl", lock_path=c.lock.path)
    with c.lock:
        with pytest.raises(tcp.AuthorityError):
            other.lock.acquire()
    other.lock.acquire()  # released -> acquirable
    other.lock.release()


def test_ledger_hash_chain_and_tamper_detection(controller):
    c, root, _ = controller
    child = _child(root)
    c.dispatch("status")
    with c.lock:
        c.dispatch("write", child_envelope=child)
    assert tcp.verify_ledger(c.ledger_path)
    lines = c.ledger_path.read_text(encoding="utf-8").splitlines()
    tampered = lines[0].replace('"status"', '"forged"')
    c.ledger_path.write_text("\n".join([tampered] + lines[1:]) + "\n", encoding="utf-8")
    assert not tcp.verify_ledger(c.ledger_path)
