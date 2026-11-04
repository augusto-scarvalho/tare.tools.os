"""Unit tests for relay/cluster_sre.py (Autonomous Cluster SRE & Self-Healing Overseer)."""
import sys
import time
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "relay"))

from relay import cluster_sre, vendor_chairs, relay_io, relay_mesh


def test_flush_stale_outcome_backoffs(tmp_path, monkeypatch):
    # Mock TRAINS_DIR
    trains_dir = tmp_path / "trains"
    trains_dir.mkdir()
    train_dir = trains_dir / "TRAIN-TEST-01"
    train_dir.mkdir()
    relay_io.write_json_atomic(train_dir / "STATE.json", {
        "train_id": "TRAIN-TEST-01",
        "status": "PLAN_APPROVED",
        "revision": 2
    })
    
    # Create fake outcome backoff file
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    outcomes_dir = history_dir / "outcomes"
    outcomes_dir.mkdir()
    
    monkeypatch.setattr(cluster_sre.relay_mesh, "TRAINS_DIR", trains_dir)
    monkeypatch.setattr(cluster_sre.relay_mesh, "RELAY_DIR", tmp_path)
    monkeypatch.setattr(cluster_sre.relay_io, "_outcome_path", lambda key: outcomes_dir / f"{key.replace(':', '_')}.json")
    
    key = "TRAIN-TEST-01::r2::implementer"
    fake_backoff = outcomes_dir / f"{key.replace(':', '_')}.json"
    relay_io.write_json_atomic(fake_backoff, {"retry_count": 5, "backoff_until": time.time() + 1000})
    assert fake_backoff.exists()

    # SRE flush removes the backoff
    flushed = cluster_sre.flush_stale_outcome_backoffs()
    assert flushed == 1
    assert not fake_backoff.exists()


def test_probe_and_heal_vendor_fuel(tmp_path, monkeypatch):
    test_state_file = tmp_path / "vendor_fuel_state.json"
    monkeypatch.setattr(vendor_chairs, "FUEL_STATE_FILE", test_state_file)
    monkeypatch.setattr(cluster_sre, "SRE_STATE_FILE", tmp_path / "sre_state.json")
    monkeypatch.setattr(cluster_sre.overseer_agent, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(
        cluster_sre.overseer_agent,
        "CHAT_FILE",
        tmp_path / "history" / "overseer_chat.json",
    )

    # Trip circuit breaker with past cooldown
    vendor_chairs.trip_vendor_circuit("claude", "Quota exceeded", "")
    state = vendor_chairs.load_fuel_state()
    # Set cooldown to past
    state["vendors"]["claude"]["cooldown_until"] = time.time() - 10
    vendor_chairs.save_fuel_state(state)

    healthy_before, _ = vendor_chairs.is_vendor_healthy("claude")
    assert healthy_before is True  # cooldown expired

    healed = cluster_sre.probe_and_heal_vendor_fuel()
    assert healed == 1
    
    # Verify vendor restored
    state_after = vendor_chairs.load_fuel_state()
    assert state_after["vendors"]["claude"]["healthy"] is True
