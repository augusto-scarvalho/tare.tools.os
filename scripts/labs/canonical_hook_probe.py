#!/usr/bin/env python3
"""Bounded Probe A: one pure prompt policy, two installed CLI adapters."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, BinaryIO, TextIO

SCHEMA_VERSION = "1.0.0"
POLICY_ID = "prompt_budget"
PHASE = "user_prompt_submit"
MAX_INPUT_BYTES = 32_768
MAX_PROMPT_BYTES = 16_384
MAX_OUTPUT_BYTES = 4_096


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ERROR = "error"


class UnsupportedFieldsError(ValueError):
    def __init__(self, fields: set[str]) -> None:
        self.fields = tuple(sorted(fields))
        super().__init__("hook payload contains unsupported fields")


@dataclass(frozen=True)
class PromptBudgetRequest:
    prompt: str


@dataclass(frozen=True)
class PolicyOutcome:
    decision: Decision
    reason_code: str
    prompt_bytes: int


@dataclass(frozen=True)
class AdapterResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""

    def __post_init__(self) -> None:
        if len(self.stdout.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise ValueError("adapter stdout exceeds the bounded output size")
        if len(self.stderr.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise ValueError("adapter stderr exceeds the bounded output size")


@dataclass(frozen=True)
class AdapterContract:
    """Closed semantic contract; it does not attest the vendor CLI control path."""

    runtime: str
    vendor_event: str
    canonical_phase: str
    accepted_fields: frozenset[str]
    supported_decisions: tuple[Decision, ...]
    supported_control_effects: tuple[str, ...]
    verification_scope: str = "semantic_translation_only"


def evaluate_prompt_budget(request: PromptBudgetRequest) -> PolicyOutcome:
    """Pure functional core: no environment, filesystem, clock, network or state."""
    prompt_bytes = len(request.prompt.encode("utf-8"))
    if not request.prompt.strip():
        return PolicyOutcome(Decision.DENY, "EMPTY_PROMPT", prompt_bytes)
    if prompt_bytes > MAX_PROMPT_BYTES:
        return PolicyOutcome(Decision.DENY, "PROMPT_TOO_LARGE", prompt_bytes)
    return PolicyOutcome(Decision.ALLOW, "WITHIN_PROMPT_BUDGET", prompt_bytes)


class _Adapter:
    contract: AdapterContract

    def parse(self, payload: Any) -> PromptBudgetRequest:
        if not isinstance(payload, dict):
            raise TypeError("hook payload must be an object")
        unexpected = set(payload) - self.contract.accepted_fields
        if unexpected:
            raise UnsupportedFieldsError(unexpected)
        if payload.get("hook_event_name") != self.contract.vendor_event:
            raise ValueError("hook event is unsupported")
        prompt = payload.get("prompt")
        if not isinstance(prompt, str):
            raise TypeError("prompt must be a string")
        return PromptBudgetRequest(prompt=prompt)

    def render(self, outcome: PolicyOutcome) -> AdapterResult:
        raise NotImplementedError

    def block(self, reason_code: str) -> AdapterResult:
        return AdapterResult(
            2,
            stderr=f"TARE_HOOK_PROBE_BLOCK:{reason_code}\n",
        )

    def _require_supported_outcome(self, outcome: PolicyOutcome) -> None:
        if outcome.decision not in self.contract.supported_decisions:
            raise TypeError("adapter does not support the policy decision")

    @staticmethod
    def _shadow_evidence(outcome: PolicyOutcome) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "policy_id": POLICY_ID,
            "phase": PHASE,
            "decision": outcome.decision.value,
            "reason_code": outcome.reason_code,
            "prompt_bytes": outcome.prompt_bytes,
            "evidence_scope": "ephemeral_shadow_stdout",
        }


class ClaudeAdapter(_Adapter):
    contract = AdapterContract(
        runtime="claude",
        vendor_event="UserPromptSubmit",
        canonical_phase=PHASE,
        accepted_fields=frozenset(
            {
                "session_id",
                "transcript_path",
                "cwd",
                "permission_mode",
                "prompt_id",
                "hook_event_name",
                "prompt",
            }
        ),
        supported_decisions=(Decision.ALLOW, Decision.DENY, Decision.ERROR),
        supported_control_effects=("block",),
    )

    def render(self, outcome: PolicyOutcome) -> AdapterResult:
        self._require_supported_outcome(outcome)
        evidence = self._shadow_evidence(outcome)
        if outcome.decision is Decision.ALLOW:
            return AdapterResult(
                0, stdout=json.dumps({"tare_probe": evidence}, separators=(",", ":"))
            )
        if outcome.decision in (Decision.DENY, Decision.ERROR):
            return self.block(outcome.reason_code)
        raise TypeError("unsupported policy decision")


class CodexAdapter(_Adapter):
    contract = AdapterContract(
        runtime="codex",
        vendor_event="UserPromptSubmit",
        canonical_phase=PHASE,
        accepted_fields=frozenset(
            {
                "session_id",
                "transcript_path",
                "cwd",
                "permission_mode",
                "prompt_id",
                "turn_id",
                "agent_id",
                "agent_type",
                "model",
                "hook_event_name",
                "prompt",
            }
        ),
        supported_decisions=(Decision.ALLOW, Decision.DENY, Decision.ERROR),
        supported_control_effects=("block",),
    )

    def render(self, outcome: PolicyOutcome) -> AdapterResult:
        self._require_supported_outcome(outcome)
        evidence = self._shadow_evidence(outcome)
        if outcome.decision is Decision.ALLOW:
            return AdapterResult(
                0,
                stdout=json.dumps(
                    {
                        "systemMessage": "TARE_HOOK_PROBE:PROMPT_BUDGET_ALLOW",
                        "tare_probe": evidence,
                    },
                    separators=(",", ":"),
                ),
            )
        if outcome.decision in (Decision.DENY, Decision.ERROR):
            return self.block(outcome.reason_code)
        raise TypeError("unsupported policy decision")

    def block(self, reason_code: str) -> AdapterResult:
        marker = f"TARE_HOOK_PROBE_BLOCK:{reason_code}"
        return AdapterResult(
            0,
            stdout=json.dumps(
                {"decision": "block", "reason": marker},
                separators=(",", ":"),
            ),
        )


_ADAPTERS: dict[str, _Adapter] = {
    "claude": ClaudeAdapter(),
    "codex": CodexAdapter(),
}
ADAPTER_CONTRACTS: Mapping[str, AdapterContract] = MappingProxyType(
    {runtime: adapter.contract for runtime, adapter in _ADAPTERS.items()}
)
SUPPORTED_RUNTIMES = tuple(ADAPTER_CONTRACTS)


def dispatch(runtime: str, raw_input: bytes) -> AdapterResult:
    """Closed dispatcher: fixed runtime, schema, phase and policy allowlists only."""
    adapter = _ADAPTERS.get(runtime)
    if adapter is None:
        return AdapterResult(2, stderr="TARE_HOOK_PROBE_BLOCK:UNKNOWN_RUNTIME\n")
    if not raw_input:
        return adapter.block("EMPTY_INPUT")
    if len(raw_input) > MAX_INPUT_BYTES:
        return adapter.block("INPUT_TOO_LARGE")
    try:
        try:
            payload = json.loads(raw_input.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError):
            payload = json.loads(raw_input.decode("utf-16"))
        request = adapter.parse(payload)
        outcome = evaluate_prompt_budget(request)
        return adapter.render(outcome)
    except UnsupportedFieldsError as exc:
        fields = ",".join(exc.fields)
        return adapter.block(f"UNSUPPORTED_FIELDS:{fields}")
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return adapter.block("MALFORMED_INPUT")


def _read_bounded(stream: BinaryIO) -> bytes:
    return stream.read(MAX_INPUT_BYTES + 1)


def run_command(
    runtime: str,
    *,
    stdin: BinaryIO,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    result = dispatch(runtime, _read_bounded(stdin))
    try:
        if result.stdout:
            stdout.write(result.stdout)
            stdout.flush()
        if result.stderr:
            stderr.write(result.stderr)
            stderr.flush()
    except (BrokenPipeError, OSError):
        try:
            stderr.write("TARE_HOOK_PROBE_BLOCK:OUTPUT_UNAVAILABLE\n")
            stderr.flush()
        except (BrokenPipeError, OSError):
            pass
        return 2
    return result.exit_code


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in SUPPORTED_RUNTIMES:
        print("TARE_HOOK_PROBE_BLOCK:UNKNOWN_RUNTIME", file=sys.stderr)
        return 2
    return run_command(
        args[0],
        stdin=sys.stdin.buffer,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())
