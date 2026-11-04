"""SPEC-141 — packet-economy compose seam.

`spawn_command` builds a prompt + model/effort and, before this seam, composed
none of the other token levers. `compose_spawn` is the one place that folds the
per-profile levers into the spawn path:

- compact agent output (TE.5): sets HARNESS_AGENT_OUTPUT=compact ONLY when the
  profile opts in via `tokenEconomy.compactOutput` (owner Q2 — default absent =
  byte-identical indent=2). The env is READ by `common.emit`; nothing here
  reimplements that mechanism.
- per-delegation budget: the built packet's estimated tokens (the same
  chars-divisor estimator the workflow token-audit uses) vs
  `tokenEconomy.delegationBudgetTokens`. Over-budget is a WARN, never a block
  (owner Q1); the caller prints it.

Pure over its args. Absent `tokenEconomy` block = `{env: {}, budget over False}`
= byte-identical spawn.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

try:
    from .common import read_json
    from . import token_economics
except ImportError:  # run directly for the __main__ self-check
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from harness_lib.common import read_json
    from harness_lib import token_economics

PROFILES_REL = ".harness/routing/task-profiles.json"


def _token_economy(profile: Any) -> dict[str, Any]:
    te = profile.get("tokenEconomy") if isinstance(profile, dict) else None
    return te if isinstance(te, dict) else {}


def _budget_tokens(te: dict[str, Any]) -> int | None:
    b = te.get("delegationBudgetTokens")
    return int(b) if isinstance(b, (int, float)) else None


def _output_cap(te: dict[str, Any]) -> int | None:
    c = te.get("outputCapChars")
    return int(c) if isinstance(c, (int, float)) and not isinstance(c, bool) else None


def compose_spawn(root: Path | str, profile_name: str, profile: dict[str, Any],
                  task: str) -> dict[str, Any]:
    """Compose the spawn-packet levers for one delegation.

    `task` is the built packet text to size (spawn_command passes the fully-built
    prompt). Returns `{"env": {...}, "budget": {"estTokens", "budgetTokens",
    "over"}, "promptSuffix": "..."}`. `root`/`profile_name` are part of the fixed
    seam contract; the pure computation uses only `profile` + `task`.
    """
    te = _token_economy(profile)
    env = {"HARNESS_AGENT_OUTPUT": "compact"} if te.get("compactOutput") else {}
    budget_tokens = _budget_tokens(te)
    est = token_economics.estimate_tokens_from_text(task)
    over = budget_tokens is not None and est > budget_tokens
    # SPEC-142: output-cap CONTRACT line asking the delegated worker to bound its
    # own output. This is NOT truncation here — real worker-output truncation is
    # EXP-1 phase-2, owner-gated. Absent `outputCapChars` -> "" -> byte-identical
    # prompt. The keep-the-tail wording is motivated by the EXP-1 phase-1 finding
    # (head-preserving truncation drops the decisive tail):
    # .harness/handoff/result-exp1-probe.md — cited as motivation, not adopted.
    cap = _output_cap(te)
    suffix = "" if cap is None else (
        f"\n\nCap your output to ~{cap} chars; if you must truncate, keep the "
        f"decisive tail (file paths, error/traceback lines), not the head.")
    return {"env": env,
            "budget": {"estTokens": est, "budgetTokens": budget_tokens, "over": over},
            "promptSuffix": suffix}


def budget_for_agent(root: Path | str, agent_type: str | None) -> int | None:
    """Self-review tie: map a recorded delegation's `agentType` back to its
    profile's `delegationBudgetTokens`, so the PRE-spawn warn and the POST-hoc
    `delegation-cost-outlier` flag share ONE number. `None` when no budgeted
    profile matches (self-review keeps its median/absolute fallback)."""
    if not agent_type:
        return None
    profiles = (read_json(Path(root) / PROFILES_REL, {}) or {}).get("profiles", {})
    if not isinstance(profiles, dict):
        return None
    for name, prof in profiles.items():
        if not isinstance(prof, dict):
            continue
        agent = ((prof.get("spawn") or {}).get("claude") or {}).get("agent")
        if agent_type in (name, agent):
            b = _budget_tokens(_token_economy(prof))
            if b is not None:
                return b
    return None


def _self_check() -> None:
    root = Path(__file__).resolve().parents[2]

    # compactOutput opt-in gates the env; budget under/over; absent = byte-identical.
    on = compose_spawn(root, "impl", {"tokenEconomy": {"compactOutput": True,
                                                       "delegationBudgetTokens": 100000}}, "small task")
    assert on["env"] == {"HARNESS_AGENT_OUTPUT": "compact"}, on
    assert on["budget"]["over"] is False and on["budget"]["budgetTokens"] == 100000, on

    off = compose_spawn(root, "impl", {"tokenEconomy": {"delegationBudgetTokens": 100000}}, "small task")
    assert off["env"] == {}, off  # compactOutput absent -> no env

    over = compose_spawn(root, "impl", {"tokenEconomy": {"delegationBudgetTokens": 5}}, "x" * 100)
    assert over["budget"]["over"] is True and over["budget"]["estTokens"] > 5, over

    absent = compose_spawn(root, "scanx", {"spawn": {"generic": {}}}, "task")
    assert absent["env"] == {} and absent["budget"]["over"] is False, absent
    assert absent["budget"]["budgetTokens"] is None, absent

    # SPEC-142: outputCapChars -> one cap line naming N + the keep-the-tail rule;
    # absent -> "" (byte-identical prompt).
    capped = compose_spawn(root, "impl", {"tokenEconomy": {"outputCapChars": 20000}}, "small task")
    assert "20000" in capped["promptSuffix"] and "decisive tail" in capped["promptSuffix"], capped
    assert on["promptSuffix"] == "" and off["promptSuffix"] == "" and absent["promptSuffix"] == "", \
        (on["promptSuffix"], off["promptSuffix"], absent["promptSuffix"])

    # tie: agentType -> profile budget over the REAL seeded profiles.
    assert budget_for_agent(root, "implementer") == 140000, budget_for_agent(root, "implementer")
    assert budget_for_agent(root, "implementation") == 140000  # profile name also resolves
    assert budget_for_agent(root, "docs-writer") is None  # no budget on docs
    assert budget_for_agent(root, None) is None
    print("packet_economy self-check ok")


if __name__ == "__main__":
    _self_check()
