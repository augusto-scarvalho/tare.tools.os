"""SPEC-113 AGY-V1 hook normalization layer (Hardened).

Provides vendor-neutral parsing of hook stdin JSON payloads across Claude,
Codex, Kimi, and Antigravity (AGY), mapping vendor-specific fields onto a
canonical `HarnessHookEvent` dataclass, processing outcomes via
`HarnessHookOutcome`, and formatting native event-specific AGY JSON codecs.

AGY 1.1.11 Event Codec Contracts:
- PreToolUse:      {"decision": "allow"|"deny"|"ask"|"force_ask"|"deny_unless_prior_grant", "reason": "..."}
- PostToolUse:     {}
- PreInvocation:   {"injectSteps": [...]}
- PostInvocation:  {"injectSteps": [...], "terminationBehavior": ""|"force_continue"|"terminate"}
- Stop:            {"decision": "continue", "reason": "..."} to BLOCK Stop, else {}
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class HarnessHookEvent:
    vendor: str
    event: str
    tool_name: str
    tool_args: dict[str, Any]
    cwd: str
    session_id: str
    transcript_path: str
    artifact_path: str
    model_name: str
    step_index: int
    error: str


@dataclass
class HarnessHookOutcome:
    policy_decision: str = "allow"  # "allow" | "deny" | "ask" | "force_ask" | "continue"
    reason: str = ""
    injected_context: list[dict[str, Any]] = field(default_factory=list)
    termination_behavior: str = ""  # "" | "force_continue" | "terminate"
    exit_code: int = 0
    error: str = ""


def normalize_hook_payload(raw_input: str | dict[str, Any] | None = None,
                           vendor_hint: str | None = None) -> HarnessHookEvent:
    """Parse vendor stdin JSON (or dict) into a unified HarnessHookEvent."""
    if isinstance(raw_input, dict):
        payload = raw_input
    elif isinstance(raw_input, str) and raw_input.strip():
        try:
            payload = json.loads(raw_input)
        except Exception:
            payload = {}
    else:
        payload = {}

    tool_call = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}

    event = str(payload.get("hook_event_name") or payload.get("hookEventName") or payload.get("event") or "").strip()
    if not event:
        if tool_call or "toolCall" in payload or payload.get("tool_name") or payload.get("toolName"):
            event = "PreToolUse"

    session_id = str(payload.get("conversationId") or payload.get("session_id") or payload.get("sessionId") or payload.get("conversation_id") or "").strip()
    
    ws_paths = payload.get("workspacePaths")
    if isinstance(ws_paths, list) and ws_paths:
        cwd = str(ws_paths[0]).strip()
    else:
        cwd = str(payload.get("cwd") or payload.get("workspace") or payload.get("working_dir") or "").strip()

    tool_name = str(tool_call.get("name") or payload.get("tool_name") or payload.get("toolName") or payload.get("name") or "").strip()

    raw_args = tool_call.get("args") or payload.get("tool_input") or payload.get("toolInput") or payload.get("args") or payload.get("input") or {}
    tool_args = raw_args if isinstance(raw_args, dict) else {}

    transcript_path = str(payload.get("transcriptPath") or payload.get("transcript_path") or "").strip()
    artifact_path = str(payload.get("artifactDirectoryPath") or payload.get("artifact_path") or payload.get("artifactPath") or "").strip()
    model_name = str(payload.get("modelName") or payload.get("model_name") or payload.get("model") or "").strip()
    error = str(payload.get("error") or "").strip()

    step_index = 0
    try:
        step_index = int(payload.get("stepIdx") or payload.get("step_index") or payload.get("stepIndex") or 0)
    except (ValueError, TypeError):
        step_index = 0

    vendor = vendor_hint or "generic"
    if ("toolCall" in payload or "conversationId" in payload or "workspacePaths" in payload
            or "artifactDirectoryPath" in payload or "antigravity" in payload or "agy" in payload
            or payload.get("client") in ("antigravity", "agy")):
        vendor = "antigravity"
    elif "kimi" in payload or payload.get("client") == "kimi":
        vendor = "kimi"
    elif "claude" in payload or "hook_event_name" in payload:
        vendor = "claude"

    return HarnessHookEvent(
        vendor=vendor,
        event=event,
        tool_name=tool_name,
        tool_args=tool_args,
        cwd=cwd,
        session_id=session_id,
        transcript_path=transcript_path,
        artifact_path=artifact_path,
        model_name=model_name,
        step_index=step_index,
        error=error,
    )


# --------------------------------------------------------------------------- #
# Event-Specific AGY JSON Codecs
# --------------------------------------------------------------------------- #

def to_agy_pre_tool_use(outcome: HarnessHookOutcome) -> dict[str, Any]:
    decision = outcome.policy_decision if outcome.policy_decision in ("allow", "deny", "ask", "force_ask", "deny_unless_prior_grant") else "allow"
    res: dict[str, Any] = {"decision": decision}
    if outcome.reason:
        res["reason"] = outcome.reason
    return res


def to_agy_post_tool_use(outcome: HarnessHookOutcome) -> dict[str, Any]:
    return {}


def to_agy_pre_invocation(outcome: HarnessHookOutcome) -> dict[str, Any]:
    if outcome.injected_context:
        return {"injectSteps": outcome.injected_context}
    if outcome.reason:
        return {"injectSteps": [{"ephemeralMessage": outcome.reason}]}
    return {"injectSteps": []}


def to_agy_post_invocation(outcome: HarnessHookOutcome) -> dict[str, Any]:
    return {
        "injectSteps": outcome.injected_context,
        "terminationBehavior": outcome.termination_behavior or "",
    }


def to_agy_stop(outcome: HarnessHookOutcome) -> dict[str, Any]:
    if outcome.policy_decision in ("deny", "continue") or outcome.exit_code != 0:
        return {"decision": "continue", "reason": outcome.reason or "Stop blocked by harness validation gate"}
    return {"decision": "allow"}


def format_native_decision(outcome: HarnessHookOutcome, event: str, vendor: str = "antigravity") -> tuple[int, str]:
    """Format HarnessHookOutcome into vendor native exit code and stdout string."""
    if vendor == "antigravity":
        ev = (event or "").strip()
        if ev == "PreToolUse":
            out = to_agy_pre_tool_use(outcome)
        elif ev == "PostToolUse":
            out = to_agy_post_tool_use(outcome)
        elif ev == "PreInvocation":
            out = to_agy_pre_invocation(outcome)
        elif ev == "PostInvocation":
            out = to_agy_post_invocation(outcome)
        elif ev == "Stop":
            out = to_agy_stop(outcome)
        else:
            out = {"decision": outcome.policy_decision, "reason": outcome.reason}
        return 0, json.dumps(out)

    if vendor in ("claude", "kimi"):
        if outcome.policy_decision == "deny":
            out = {
                "hookSpecificOutput": {
                    "hookEventName": event or "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": outcome.reason,
                }
            }
            return (2 if vendor == "kimi" else 0), json.dumps(out)

    return outcome.exit_code, outcome.reason


def _self_check() -> int:
    """Self-check validating all 5 AGY event codecs."""
    print("Testing hook_normalization self-check...")

    # 1. PreToolUse codec
    out_pre = to_agy_pre_tool_use(HarnessHookOutcome(policy_decision="deny", reason="protected file"))
    assert out_pre == {"decision": "deny", "reason": "protected file"}, out_pre

    # 2. PostToolUse codec
    out_post = to_agy_post_tool_use(HarnessHookOutcome())
    assert out_post == {}, out_post

    # 3. PreInvocation codec
    steps = [{"type": "userMessage", "content": "reinjected context"}]
    out_preinv = to_agy_pre_invocation(HarnessHookOutcome(injected_context=steps))
    assert out_preinv == {"injectSteps": steps}, out_preinv

    # 4. PostInvocation codec
    out_postinv = to_agy_post_invocation(HarnessHookOutcome(injected_context=steps, termination_behavior="terminate"))
    assert out_postinv == {"injectSteps": steps, "terminationBehavior": "terminate"}, out_postinv

    # 5. Stop codec
    out_stop_deny = to_agy_stop(HarnessHookOutcome(policy_decision="deny", reason="dirty worktree"))
    assert out_stop_deny == {"decision": "continue", "reason": "dirty worktree"}, out_stop_deny

    out_stop_allow = to_agy_stop(HarnessHookOutcome(policy_decision="allow"))
    assert out_stop_allow == {}, out_stop_allow

    print("hook_normalization self-check OK (all 5 event codecs verified).")
    return 0


if __name__ == "__main__":
    sys.exit(_self_check())
