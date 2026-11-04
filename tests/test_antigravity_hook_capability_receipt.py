from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# The decision record of round table CASE-2026-08-24-CANONICAL-HOOK-INSTRUMENTATION, kept with the decisions.
RECEIPT = ROOT / "docs" / "decisions" / "ANTIGRAVITY_PROMPT_HOOK_FEASIBILITY_2026-08-24.json"


def test_antigravity_prompt_budget_hold_is_narrow_and_fail_closed() -> None:
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    assert receipt["status"] == "HELD_UNSUPPORTED_VENDOR_SURFACE"
    assert receipt["policy"] == "prompt_budget"
    assert receipt["required_capability"] == "blocking_pre_model_prompt_payload"
    assert "UserPromptSubmit" not in receipt["documented_events"]
    assert receipt["pre_invocation_prompt_field"] is False
    assert receipt["adapter_implemented"] is False
    assert receipt["transcript_fallback"] == "REJECTED_UNSTABLE_AND_BYTE_MUTATION_RISK"
    assert receipt["production_rollout"] is False


def test_antigravity_keeps_real_supported_surface_available_for_later_policy() -> None:
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    assert receipt["documented_events"] == [
        "PreToolUse",
        "PostToolUse",
        "PreInvocation",
        "PostInvocation",
        "Stop",
    ]
    assert receipt["future_compatible_surface"] == {
        "event": "PreToolUse",
        "disposition": "AVAILABLE_FOR_A_DIFFERENT_EXPLICITLY_AUTHORIZED_POLICY",
    }
