"""SPEC-118 v5 -- per-role session context diet, vendor-neutral intent -> vendor flags.

A role in model-routing.json may declare a `contextDiet` block:

    {"vendorUserLayer": false,   # drop user-scope config/plugins/skills
     "canonicalReinject": false, # skip the harness canonical-pack reinjection
     "keepTools": ["exec"]}      # built-in tool capabilities kept; absent key = vendor default

This module is the ONLY place that knows how each vendor spells the cut.
Consumers (chat_engines.build_engine, route_loop._model_triage) pass the diet
dict through resolve_role and append `flags_for(...)` / merge `env_for(...)`.
Unknown executors and absent diets translate to nothing -- byte-compat.

Measured (2026-07-15, haiku probes, this repo): user layer -12.0K tok, tool
schemas -7.0K, canonical pack -3.8K; full diet 40,264 -> 16,520 tok/turn (-59%).
Self-check: python scripts/harness_lib/context_diet.py
"""
from __future__ import annotations

from typing import Any

# SPEC-118 v4 session-diet flags, verbatim from the claude worker template
# (verified CLI 2.1.207; min 2.1.x). Subscription auth survives them.
CLAUDE_USER_LAYER_FLAGS = [
    "--strict-mcp-config", "--setting-sources", "project,local",
    "--disable-slash-commands", "--exclude-dynamic-system-prompt-sections",
]

# Capability -> claude built-in tool names. Vendor-neutral diets speak in
# capabilities; each vendor table maps them to its own tool ids.
CLAUDE_CAPABILITY_TOOLS = {
    "exec": ("Bash", "BashOutput", "KillShell"),
    "todo": ("TodoWrite",),
    "read": ("Read", "Grep", "Glob"),
    "edit": ("Edit", "Write", "NotebookEdit"),
    "web": ("WebFetch", "WebSearch"),
    "agents": ("Task",),
    "skills": ("Skill",),
    "plan": ("EnterPlanMode", "ExitPlanMode"),
}

# SPEC-118 v6 (Phase C resolved): the default diet for writeAllowed:false workers.
# Denies only {web, agents, plan} -- the measured -2,670 tok/turn slice. The edit
# slice (-722) was measured-and-REJECTED (denying Write forces the stdout result
# fallback -- robustness not worth 0.7K); the Skill slice measured +-0 and review/
# security declare skills. An explicit contextDiet on the role overrides this.
READONLY_WORKER_DIET = {"keepTools": ["exec", "todo", "read", "edit", "skills"]}


def denied_tools(executor: str | None, diet: dict[str, Any] | None,
                 keep_extra: tuple[str, ...] = ()) -> list[str]:
    """Vendor tool names the diet denies (keepTools complement), [] when the
    diet has no keepTools or the vendor has no tool knob. Exposed separately so
    a caller that already emits its own --disallowedTools (ClaudeEngine's plan
    -mode lockout) can merge into ONE flag -- repeated flags risk a silent
    last-one-wins override."""
    if executor != "claude" or not isinstance(diet, dict):
        return []
    keep = diet.get("keepTools")
    if not isinstance(keep, list):
        return []
    kept = {t for cap in {*keep, *keep_extra} for t in CLAUDE_CAPABILITY_TOOLS.get(cap, ())}
    return [t for cap in sorted(CLAUDE_CAPABILITY_TOOLS)
            for t in CLAUDE_CAPABILITY_TOOLS[cap] if t not in kept]


def layer_flags(executor: str | None, diet: dict[str, Any] | None) -> list[str]:
    """The vendorUserLayer cut only (no tool trim), per vendor."""
    if not isinstance(diet, dict) or diet.get("vendorUserLayer") is not False:
        return []
    if executor == "claude":
        return list(CLAUDE_USER_LAYER_FLAGS)
    if executor == "codex":
        # Skips repo AGENTS.md project docs (codex >=0.144 `-c` override).
        # ponytail: keepTools has no known codex knob -- honest no-op.
        return ["-c", "project_doc_max_bytes=0"]
    return []


def env_for(diet: dict[str, Any] | None) -> dict[str, str]:
    """Env additions for a dieted child. HARNESS_SKIP_REINJECT is vendor-neutral:
    the SessionStart reinjection hook is OURS, installed by agent-sync wherever
    the vendor supports hooks, and it honors the variable everywhere."""
    if isinstance(diet, dict) and diet.get("canonicalReinject") is False:
        return {"HARNESS_SKIP_REINJECT": "1"}
    return {}


def tool_flags(executor: str | None, diet: dict[str, Any] | None,
               keep_extra: tuple[str, ...] = ()) -> list[str]:
    """The tool-trim cut only, as ready argv flags -- for spawn paths whose
    template already carries the layer flags (the claude worker template,
    SPEC-118 v4) and has no baseline --disallowedTools to merge with.

    Single `--flag=value` token, NEVER the two-token form: --disallowedTools is
    VARIADIC in the claude CLI, and these flags get spliced right before the
    positional {prompt} -- the two-token form swallowed the prompt (live worker
    smoke, 2026-07-15: "Input must be provided either through stdin...")."""
    denied = denied_tools(executor, diet, keep_extra)
    return [f"--disallowedTools={','.join(denied)}"] if denied else []


def flags_for(executor: str | None, diet: dict[str, Any] | None,
              keep_extra: tuple[str, ...] = ()) -> list[str]:
    """CLI flags realizing the whole diet on `executor` (layer + tool trim as
    one flat argv tail -- for spawn paths with no baseline --disallowedTools of
    their own). `keep_extra` is the calling SURFACE's own tool floor (e.g. the
    chat REPL needs exec+todo to run harness commands and feed the plan HUD)
    unioned into the role's keepTools. Unknown executors (openai/generic/...)
    return [] -- they are already minimal."""
    return layer_flags(executor, diet) + tool_flags(executor, diet, keep_extra)


def demo() -> None:
    # absent/None diet or unknown executor -> nothing (byte-compat).
    assert flags_for("claude", None) == []
    assert flags_for("openai", {"vendorUserLayer": False}) == []
    assert flags_for(None, {"vendorUserLayer": False}) == []
    assert env_for(None) == {} and env_for({}) == {}

    # canonicalReinject:false -> the skip env, regardless of vendor.
    assert env_for({"canonicalReinject": False}) == {"HARNESS_SKIP_REINJECT": "1"}
    assert env_for({"canonicalReinject": True}) == {}

    # claude: user-layer flags + tool complement; keep_extra unions in.
    full = flags_for("claude", {"vendorUserLayer": False, "keepTools": []},
                     keep_extra=("exec", "todo"))
    assert full[:len(CLAUDE_USER_LAYER_FLAGS)] == CLAUDE_USER_LAYER_FLAGS
    denied = full[-1].split("=", 1)[1]
    assert "Bash" not in denied.split(",") and "TodoWrite" not in denied.split(",")
    assert "Read" in denied and "Task" in denied and "WebSearch" in denied

    # zero tools (triage router): every capability denied, as ONE =-form token.
    bare = flags_for("claude", {"keepTools": []})
    all_tools = {t for ts in CLAUDE_CAPABILITY_TOOLS.values() for t in ts}
    assert len(bare) == 1 and bare[0].startswith("--disallowedTools=")
    assert set(bare[0].split("=", 1)[1].split(",")) == all_tools
    assert "--setting-sources" not in bare, "no vendorUserLayer key -> no layer flags"

    # keepTools absent entirely -> no tool trim.
    assert "--disallowedTools" not in flags_for("claude", {"vendorUserLayer": False})

    # codex: best-effort project-doc skip only.
    assert flags_for("codex", {"vendorUserLayer": False, "keepTools": []}) == \
        ["-c", "project_doc_max_bytes=0"]
    assert flags_for("codex", {"keepTools": []}) == []

    # denied_tools/layer_flags split (chat merges denied into its own flag).
    names = denied_tools("claude", {"keepTools": []}, keep_extra=("exec",))
    assert "Bash" not in names and "TodoWrite" in names and "Read" in names
    assert denied_tools("codex", {"keepTools": []}) == []
    assert denied_tools("claude", {"vendorUserLayer": False}) == []
    assert layer_flags("claude", {"vendorUserLayer": False}) == CLAUDE_USER_LAYER_FLAGS
    assert layer_flags("claude", {"keepTools": []}) == []

    # tool_flags = the trim alone (template paths that already carry the layer).
    assert tool_flags("claude", {"vendorUserLayer": False}) == []
    tf = tool_flags("claude", {"vendorUserLayer": False, "keepTools": []})
    assert len(tf) == 1 and tf[0].startswith("--disallowedTools=") and "--setting-sources" not in tf
    assert flags_for("claude", {"vendorUserLayer": False, "keepTools": []}) == \
        CLAUDE_USER_LAYER_FLAGS + tf

    # READONLY_WORKER_DIET (v6 Phase C): denies exactly web+agents+plan, keeps
    # the working set; vendor no-op elsewhere; keep-all diet -> no flag at all.
    ro = tool_flags("claude", READONLY_WORKER_DIET)
    ro_denied = set(ro[0].split("=", 1)[1].split(","))
    assert ro_denied == {"Task", "WebFetch", "WebSearch", "EnterPlanMode", "ExitPlanMode"}, ro_denied
    assert tool_flags("generic", READONLY_WORKER_DIET) == []
    assert tool_flags("codex", READONLY_WORKER_DIET) == []
    keep_all = {"keepTools": sorted(CLAUDE_CAPABILITY_TOOLS)}
    assert tool_flags("claude", keep_all) == [], "keep-all == trim off"

    print("context_diet self-check OK")


if __name__ == "__main__":
    demo()
