"""Declared regen contract (wf-regen-manifest, research round gate-phase2-safety C6).

The single declared list of runtime DOCS the harness itself (re)generates during
normal machinery operation — workflow finalize's handoff/context regen plus the
SPEC-137 validation stamp. Scenarios that assert working-tree cleanliness while
driving the real machinery (the rt6 class) IMPORT this instead of hardcoding an
exclusion set discovered by archaeology; `rm_regen_manifest.py` gate-defends the
contract BOTH ways (every driven write lands in the manifest; every manifest
entry is produced by a real writer).

Scope: porcelain-relevant regenerated docs only. Class-D runtime sidecars
(`.harness/runs/**`, `.harness/workflows/**`, `.harness/state-store/**`) and
per-target scopes (`.harness/state/targets/**`) are OUTSIDE the contract — the
isolation layer owns those (scenario_isolation._targets snapshots whole dirs; a
coarser, complementary net).

ponytail: a python constant, not a JSON file — one source of truth, versioned
atomically with the writers; publish a JSON export only when a non-python
consumer exists (the research proposed a file; simplificada per D036).
"""
from __future__ import annotations

# Repo-relative POSIX paths. Writers, per path:
#   workflow_lifecycle.update_context_from_workflow -> NEXT_STEPS + workflow/task state
#   harness.generate_handoff                        -> handoff.{json,md} + next-agent-prompt
#   validation_stamp.stamp_staged/stamp_reckon      -> quality-state.json
REGEN_PATHS: tuple[str, ...] = (
    ".harness/context/NEXT_STEPS.md",
    ".harness/handoff/handoff.json",
    ".harness/handoff/handoff.md",
    ".harness/handoff/next-agent-prompt.md",
    ".harness/state/workflow-state.json",
    ".harness/state/task-state.json",
    ".harness/state/quality-state.json",
)

# Prefixes outside the contract (class-D runtime; excluded when diffing "what the
# machinery wrote" against the manifest).
RUNTIME_PREFIXES: tuple[str, ...] = (
    ".harness/runs/",
    ".harness/workflows/",
    ".harness/state-store/",
    ".harness/state/targets/",
)


def porcelain_exclusions() -> frozenset[str]:
    """The set a porcelain-delta cleanliness check (rt6 class) excludes."""
    return frozenset(REGEN_PATHS)


def is_runtime(path: str) -> bool:
    """True for a repo-relative path under a class-D runtime prefix."""
    p = str(path).replace("\\", "/")
    return any(p.startswith(pre) for pre in RUNTIME_PREFIXES)


def _self_check() -> int:
    assert ".harness/context/NEXT_STEPS.md" in porcelain_exclusions()
    assert is_runtime(".harness/runs/gate-perf.jsonl")
    assert is_runtime(".harness\\workflows\\active\\WF-x")  # windows separators normalize
    assert not is_runtime(".harness/context/NEXT_STEPS.md")
    assert len(REGEN_PATHS) == len(set(REGEN_PATHS))
    print("regen_manifest self-check OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_check())
