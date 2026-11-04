"""Unit tests for relay/vendor_chairs.py (SPEC-115 / N-VENDORCREDIT chair fallback)."""
import sys
import time
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "relay"))

from relay import vendor_chairs


def test_out_of_fuel_detection():
    # Claude session limit signature
    out = "Warning: no stdin data received in 3s\nYou've hit your session limit · resets 2:20am (America/Sao_Paulo)"
    oof, reason = vendor_chairs.is_out_of_fuel(out)
    assert oof is True
    assert "session limit" in reason

    # Gemini quota signature
    out2 = "google.api_core.exceptions.ResourceExhausted: 429 Resource has been exhausted (e.g. check quota)."
    oof2, reason2 = vendor_chairs.is_out_of_fuel(out2)
    assert oof2 is True
    assert "resource has been exhausted" in reason2

    # Clean normal output
    out3 = "============================= 23 passed in 32.96s ============================="
    oof3, _ = vendor_chairs.is_out_of_fuel(out3)
    assert oof3 is False


def test_parse_reset_time():
    text = "You've hit your session limit · resets 2:20am (America/Sao_Paulo)"
    ts = vendor_chairs.parse_reset_time(text)
    assert ts is not None
    assert ts > time.time() - 86400


def test_circuit_breaker_trip_and_restore(tmp_path, monkeypatch):
    test_state_file = tmp_path / "vendor_fuel_state.json"
    monkeypatch.setattr(vendor_chairs, "FUEL_STATE_FILE", test_state_file)

    # Initial state is healthy
    healthy, _ = vendor_chairs.is_vendor_healthy("claude")
    assert healthy is True

    # Trip circuit breaker
    vendor_chairs.trip_vendor_circuit("claude", "Session limit exceeded", "resets 11:59pm")
    healthy_after, msg = vendor_chairs.is_vendor_healthy("claude")
    assert healthy_after is False
    assert "circuit backoff" in msg

    # Report success restores health
    vendor_chairs.report_vendor_success("claude")
    healthy_restored, _ = vendor_chairs.is_vendor_healthy("claude")
    assert healthy_restored is True


def test_role_fallback_chain_definition():
    assert "implementer" in vendor_chairs.ROLE_CHAIRS
    assert "planner" in vendor_chairs.ROLE_CHAIRS
    assert "auditor" in vendor_chairs.ROLE_CHAIRS

    # Implementer starts with claude then falls back to antigravity / codex
    assert vendor_chairs.ROLE_CHAIRS["implementer"][0] == "claude"
    assert "antigravity" in vendor_chairs.ROLE_CHAIRS["implementer"]

    # Auditor starts with antigravity
    assert vendor_chairs.ROLE_CHAIRS["auditor"][0] == "antigravity"


def test_role_models_are_exactly_pinned_for_identity_attestation(monkeypatch):
    monkeypatch.delenv("CLAUDE_MODEL", raising=False)
    monkeypatch.delenv("AGY_MODEL", raising=False)
    monkeypatch.delenv("CODEX_MODEL", raising=False)

    assert vendor_chairs.runtime_pin("claude", "implementer") == (
        "claude", "claude-opus-5-5"
    )
    assert vendor_chairs.runtime_pin("claude", "auditor") == (
        "claude", "claude-fable-5"
    )
    assert vendor_chairs.runtime_pin("antigravity", "implementer") == (
        "google-cli", "gemini-3.7-flash-medium"
    )
    assert vendor_chairs.runtime_pin("antigravity", "auditor") == (
        "google-cli", "gemini-3.7-flash-high"
    )
    assert vendor_chairs.runtime_pin("codex", "auditor") == (
        "codex", "gpt-5.6-sol"
    )


def test_resolved_commands_carry_the_exact_model_pin(monkeypatch):
    monkeypatch.setattr(vendor_chairs.shutil, "which", lambda name: name)
    assert vendor_chairs._resolve_engine_cmd("antigravity", "p", "auditor")[3:5] == [
        "--model", "gemini-3.7-flash-high"
    ]
    claude = vendor_chairs._resolve_engine_cmd("claude", "p", "implementer")
    assert claude[-2:] == ["--model", "claude-opus-5-5"]
    codex = vendor_chairs._resolve_engine_cmd("codex", "p", "auditor")
    assert codex[2:4] == ["--model", "gpt-5.6-sol"]
