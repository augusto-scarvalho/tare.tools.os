"""Always-run policy checks for the validation gate (extracted from spec_test_gate.py).

Graphify policy pins, workflow policy/command-surface assertions, and the
self-review freshness cadence. Shared gate helpers arrive via bind(env).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


def bind(env: dict[str, Any]) -> None:
    """Receive shared gate helpers/constants from scripts/spec_test_gate.py."""
    globals().update(env)


def check_knowledge_graph_policy() -> list[dict[str, str]]:
    """Validate Graphify policy wiring without requiring Graphify to be installed."""
    out: list[dict[str, str]] = []
    project = project_config()
    kg = project.get("knowledgeGraph", {}) if isinstance(project.get("knowledgeGraph"), dict) else {}
    out.append(result("graphify:project-config", "pass" if kg.get("enabled") and kg.get("requiredSkill") == "graphify" else "fail", "enabled" if kg.get("enabled") else "missing/disabled"))
    out.append(result("graphify:policy", "pass" if kg.get("policy") == "graphify-code-ast-first-source-verified" else "fail", str(kg.get("policy"))))
    order = kg.get("discoveryOrder") if isinstance(kg.get("discoveryOrder"), list) else []
    out.append(result("graphify:discovery-order", "pass" if order[:3] == ["graphify-code-ast", "graphify-artifacts", "gemini-text-image-free-tier-assisted"] else "fail", ", ".join(map(str, order)) or "missing"))
    graphify_ast_cfg = kg.get("graphifyAst", {}) if isinstance(kg.get("graphifyAst"), dict) else {}
    ast_ok = graphify_ast_cfg.get("enabled") and graphify_ast_cfg.get("preferred") and graphify_ast_cfg.get("dependencyPolicy") == "stdlib-only" and graphify_ast_cfg.get("parserPolicy") == "pure-ast-no-llm-for-code" and graphify_ast_cfg.get("inputKinds") == ["code"]
    out.append(result("graphify:graphify-code-ast-first", "pass" if ast_ok else "fail", json.dumps(graphify_ast_cfg, sort_keys=True)))
    deprecated_aliases = kg.get("deprecatedAliases", {}) if isinstance(kg.get("deprecatedAliases"), dict) else {}
    out.append(result("graphify:no-deprecated-local-ast-alias", "pass" if "localAst" not in deprecated_aliases and "localAst" not in json.dumps(kg) else "fail", json.dumps(deprecated_aliases, sort_keys=True)))
    llm_cfg = kg.get("llmAssisted", {}) if isinstance(kg.get("llmAssisted"), dict) else {}
    models = llm_cfg.get("models") or []
    forbidden = llm_cfg.get("forbiddenInputKinds") or []
    gemini_ok = llm_cfg.get("provider") == "gemini-api" and llm_cfg.get("usagePolicy") == "free-tier-only" and llm_cfg.get("allowPaidTier") is False and llm_cfg.get("networkDefault") is False and llm_cfg.get("inputKinds") == ["text", "image"] and llm_cfg.get("sourceVerificationRequired") is True and bool(set(models) & {"gemini-3.1-flash-lite-preview", "gemini-2.5-flash-lite", "gemini-2.5-flash"}) and "source-code-ast-generation" in forbidden
    out.append(result("graphify:gemini-text-image-free-tier-policy", "pass" if gemini_ok else "fail", json.dumps(llm_cfg, sort_keys=True)))
    out.append(result("graphify:generated-artifacts-ignored", "pass" if "graphify-out/" in (ROOT / ".gitignore").read_text(encoding="utf-8", errors="ignore") else "fail", "graphify-out/ ignored"))
    for item in ["docs/GRAPHIFY_INTEGRATION.md", "specs/00-universal/structural-discovery.md", "tools/hooks/graphify_search_guard.py"]:
        out.append(result(f"graphify-artifact:{item}", "pass" if (ROOT / item).exists() else "fail", "exists" if (ROOT / item).exists() else "missing"))
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8", errors="ignore") if (ROOT / "AGENTS.md").exists() else ""
    out.append(result("graphify:agents-policy", "pass" if "Graphify" in agents and "source-verified" in agents and "Graphify code AST" in agents and "Gemini API" in agents and "text/image" in agents else "fail", "AGENTS.md policy present"))
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8", errors="ignore") if (ROOT / "CLAUDE.md").exists() else ""
    out.append(result("graphify:claude-policy", "pass" if "Graphify" in claude and "AGENTS.md" in claude else "fail", "CLAUDE.md points to shared policy"))
    contract = (ROOT / ".harness" / "prompts" / "subagent-contract.md").read_text(encoding="utf-8", errors="ignore") if (ROOT / ".harness" / "prompts" / "subagent-contract.md").exists() else ""
    for token in ["contextDiscovery", "graphify", "sourceFilesVerified"]:
        out.append(result(f"graphify-contract:{token}", "pass" if token in contract else "fail", "present" if token in contract else "missing"))
    settings = read_json(ROOT / ".claude" / "settings.json", {}) or {}
    settings_text = json.dumps(settings)
    out.append(result("graphify:claude-search-hook", "pass" if "graphify_search_guard.py" in settings_text and "Glob|Grep" in settings_text else "skip", "hook configured" if "graphify_search_guard.py" in settings_text else "no Claude hook configured"))
    codex_cfg = (ROOT / ".codex" / "config.toml").read_text(encoding="utf-8", errors="ignore") if (ROOT / ".codex" / "config.toml").exists() else ""
    out.append(result("graphify:codex-multi-agent", "pass" if "multi_agent = true" in codex_cfg else "skip", "configured" if "multi_agent = true" in codex_cfg else "not configured"))
    report = ROOT / str(kg.get("reportPath", "graphify-out/GRAPH_REPORT.md"))
    graph = ROOT / str(kg.get("graphPath", "graphify-out/graph.json"))
    # SPEC-108 H5: the gate keeps the AST graph fresh (stdlib, local) so no
    # human/agent has to remember a post-commit rebuild step. The "~1s" this
    # comment used to claim was never measured; a real rebuild of this repo is
    # 3.67s (400 files, 1463 edges, 2026-07-24), which is why the freshness
    # rule below must not cry stale over an unchanged tree.
    if kg.get("enabled") and graphify_ast_cfg.get("enabled"):
        try:
            # ONE shared freshness rule (graph_providers.staleness, which
            # delegates to graphify_code_ast.freshness) so the gate, the
            # metrics view, `doctor` and `graph-status` cannot disagree.
            from harness_lib import graph_providers
            fresh = graph_providers.staleness(ROOT, graph)
            stale_reason = fresh["reason"] if fresh["stale"] else None
            if stale_reason:
                from harness_lib import graph_providers  # active provider builds (loud fallback)
                meta = graph_providers.build(ROOT)
                out.append(result("graphify:graph-freshness", "pass", f"rebuilt ({stale_reason}): filesParsed={meta.get('filesParsed')}, edges={meta.get('edges')}"))
            else:
                out.append(result("graphify:graph-freshness", "pass", "graph inputs match the graph's key"))
        except Exception as exc:
            out.append(result("graphify:graph-freshness", "fail", f"rebuild failed: {exc}; fix: python scripts/harness.py graph-build-code-ast"))
    if report.exists():
        out.append(result("graphify:report", "pass", str(report.relative_to(ROOT))))
    else:
        out.append(result("graphify:report", "skip", "no generated graph report in template"))
    if graph.exists() and not report.exists():
        out.append(result("graphify:artifact-consistency", "fail", "graph.json exists without GRAPH_REPORT.md"))
    else:
        out.append(result("graphify:artifact-consistency", "pass", "no inconsistent generated artifacts"))
    return out

def check_workflow_policy() -> list[dict[str, str]]:
    """Validate agentic map-reduce/fork-join workflow support."""
    project = project_config()
    workflows = project.get("workflows", {}) if isinstance(project.get("workflows"), dict) else {}
    out: list[dict[str, str]] = []
    out.append(result("workflow:project-config", "pass" if workflows.get("enabled") else "fail", "enabled" if workflows.get("enabled") else "missing/disabled"))
    expected_types = {"map-reduce", "fork-join"}
    configured_types = set(workflows.get("supportedTypes", []) or [])
    out.append(result("workflow:supported-types", "pass" if expected_types.issubset(configured_types) else "fail", ", ".join(sorted(configured_types)) or "none"))
    execution_modes = set(workflows.get("supportedExecutionModes", []) or [])
    out.append(result("workflow:execution-modes", "pass" if {"blocking-run", "async-await"}.issubset(execution_modes) else "fail", ", ".join(sorted(execution_modes)) or "none"))
    if workflows.get("writeAllowedByDefault") is False and workflows.get("parallelWritesAllowed") is False:
        out.append(result("workflow:safe-defaults", "pass", "read-only/no parallel writes by default"))
    else:
        out.append(result("workflow:safe-defaults", "fail", "workflows must default to read-only and no parallel writes"))
    splits = set(workflows.get("supportedSplitStrategies", []) or [])
    expected_splits = {"roots", "changed-files", "graphify", "explicit", "specs-tasks-docs"}
    out.append(result("workflow:split-strategies", "pass" if expected_splits.issubset(splits) else "fail", ", ".join(sorted(splits)) or "none"))
    limits = workflows.get("limits", {}) if isinstance(workflows.get("limits"), dict) else {}
    out.append(result("workflow:runner-limits", "pass" if int(limits.get("concurrency", 0)) >= 1 and int(limits.get("timeoutSeconds", 0)) >= 30 and int(limits.get("maxRounds", 0)) >= 1 else "fail", f"concurrency={limits.get('concurrency')}; timeoutSeconds={limits.get('timeoutSeconds')}; maxRounds={limits.get('maxRounds')}"))
    budgets = workflows.get("budgets", {}) if isinstance(workflows.get("budgets"), dict) else {}
    required_budgets = {"promptBudget", "logBudget", "reduceBudget", "tokenBudget", "handoffBudget"}
    out.append(result("workflow:budgets", "pass" if required_budgets.issubset(set(budgets.keys())) else "fail", ", ".join(sorted(budgets.keys())) or "none"))
    observability = workflows.get("observability", {}) if isinstance(workflows.get("observability"), dict) else {}
    trace_redaction = observability.get("traceRedaction", {}) if isinstance(observability.get("traceRedaction"), dict) else {}
    out.append(result("workflow:trace-redaction", "pass" if trace_redaction.get("enabled") is True and trace_redaction.get("redactSensitiveKeys") is True else "fail", "configured" if trace_redaction.get("enabled") is True and trace_redaction.get("redactSensitiveKeys") is True else f"expected workflows.observability.traceRedaction.enabled=true and redactSensitiveKeys=true in .harness/project.json; got enabled={trace_redaction.get('enabled')!r}, redactSensitiveKeys={trace_redaction.get('redactSensitiveKeys')!r}"))
    isolation = workflows.get("executionIsolation", {}) if isinstance(workflows.get("executionIsolation"), dict) else {}
    out.append(result("workflow:execution-isolation", "pass" if isolation.get("defaultMode") == "shared-readonly" and isolation.get("writeSandbox") is False else "fail", str(isolation.get("defaultMode"))))
    out.append(result("workflow:round-limit", "pass" if workflows.get("roundLimitEnforced") is True else "fail", "round limit enforced" if workflows.get("roundLimitEnforced") else f"expected workflows.roundLimitEnforced=true in .harness/project.json; got {workflows.get('roundLimitEnforced')!r}"))
    cw = workflows.get("controlledWrites", {}) if isinstance(workflows.get("controlledWrites"), dict) else {}
    cw_ok = cw.get("enabled") is True and cw.get("requiresLocks") is True and cw.get("requiresMergePlan") is True and cw.get("requiresHumanApprovalBeforeApply") is True
    out.append(result("workflow:controlled-writes", "pass" if cw_ok else "fail", "configured" if cw_ok else f"expected workflows.controlledWrites.{{enabled,requiresLocks,requiresMergePlan,requiresHumanApprovalBeforeApply}}=true in .harness/project.json; got {({k: cw.get(k) for k in ('enabled', 'requiresLocks', 'requiresMergePlan', 'requiresHumanApprovalBeforeApply')})!r}"))
    expected_workflow_commands = {"suggest","plan","execute","status","list","token-audit","trace","trace-export","trace-push","state","run","start","await","collect","cancel","watch","async-status","async-recover","resume","unlock","spawn-commands","validate-results","reduce","review-reduce","adopt-agent-reduce","finalize","mark","retry","events","promote","prepare-write","prepare-worktrees","cleanup-worktrees","merge-plan","apply-merge","review-merge","rollback","cleanup-write","scrub"}
    configured_workflow_commands = set(workflows.get("supportedWorkflowCommands", []) or [])
    missing_workflow_commands = sorted(expected_workflow_commands - configured_workflow_commands)
    out.append(result("workflow:supported-commands", "pass" if not missing_workflow_commands else "fail", "complete" if not missing_workflow_commands else "missing: " + ", ".join(missing_workflow_commands)))
    expected_write_commands = {"prepare-write","prepare-worktrees","cleanup-worktrees","merge-plan","apply-merge","review-merge","rollback","cleanup-write"}
    configured_write_commands = set(workflows.get("controlledWriteCommands", []) or [])
    missing_write_commands = sorted(expected_write_commands - configured_write_commands)
    out.append(result("workflow:controlled-write-commands", "pass" if not missing_write_commands else "fail", "complete" if not missing_write_commands else "missing: " + ", ".join(missing_write_commands)))
    out.append(result("workflow:controlled-write-validation-rollback-capability", "pass" if cw.get("supportsRollbackOnValidationGateFailure") is True else "fail", "enabled" if cw.get("supportsRollbackOnValidationGateFailure") else f"expected workflows.controlledWrites.supportsRollbackOnValidationGateFailure=true in .harness/project.json; got {cw.get('supportsRollbackOnValidationGateFailure')!r}"))
    profiles = read_json(HARNESS / "workflows" / "workflow-profiles.json", {}) or {}
    profile_names = set((profiles.get("profiles") or {}).keys()) if isinstance(profiles, dict) else set()
    required_profiles = {"repository-review", "diff-review", "pre-release-review", "security-triage"}
    out.append(result("workflow:profiles", "pass" if required_profiles.issubset(profile_names) else "fail", ", ".join(sorted(profile_names)) or "none"))
    high_risk_profiles = ["pre-release-review", "security-triage"]
    missing_review_policy = [name for name in high_risk_profiles if not isinstance((profiles.get("profiles") or {}).get(name, {}).get("reviewPolicy"), dict)]
    out.append(result("workflow:review-policies", "pass" if not missing_review_policy else "fail", "configured" if not missing_review_policy else "missing: " + ", ".join(missing_review_policy)))
    await_policy = workflows.get("awaitPolicy", {}) if isinstance(workflows.get("awaitPolicy"), dict) else {}
    async_scheduler = workflows.get("asyncScheduler", {}) if isinstance(workflows.get("asyncScheduler"), dict) else {}
    out.append(result("workflow:async-await-policy", "pass" if await_policy.get("mode") in {"all", "all-settled", "race", "first-success", "quorum"} else "fail", str(await_policy.get("mode"))))
    out.append(result("workflow:async-scheduler", "pass" if int(async_scheduler.get("queueMaxSize", 0) or 0) >= 1 and int(async_scheduler.get("maxInFlight", 0) or 0) >= 1 else "fail", f"queueMaxSize={async_scheduler.get('queueMaxSize')}; maxInFlight={async_scheduler.get('maxInFlight')}"))
    profiles_map = profiles.get("profiles") or {}
    missing_await_policy = [name for name, cfg in profiles_map.items() if not isinstance(cfg.get("awaitPolicy"), dict)]
    out.append(result("workflow:profile-await-policies", "pass" if not missing_await_policy else "fail", "configured" if not missing_await_policy else "missing: " + ", ".join(missing_await_policy[:5])))
    missing_token_budget = [name for name, cfg in profiles_map.items() if not isinstance(cfg.get("tokenBudget"), dict)]
    out.append(result("workflow:profile-token-budgets", "pass" if not missing_token_budget else "fail", "configured" if not missing_token_budget else "missing: " + ", ".join(missing_token_budget[:5])))
    token_budget = budgets.get("tokenBudget", {}) if isinstance(budgets.get("tokenBudget"), dict) else {}
    token_budget_ok = int(token_budget.get("maxWorkerPromptTokens", 0) or 0) > 0 and int(token_budget.get("maxTotalWorkerPromptTokens", 0) or 0) > 0 and float(token_budget.get("charsPerToken", 0) or 0) >= 1
    out.append(result("workflow:token-budget-config", "pass" if token_budget_ok else "fail", f"maxWorkerPromptTokens={token_budget.get('maxWorkerPromptTokens')}; maxTotalWorkerPromptTokens={token_budget.get('maxTotalWorkerPromptTokens')}; charsPerToken={token_budget.get('charsPerToken')}"))
    handoff_budget = budgets.get("handoffBudget", {}) if isinstance(budgets.get("handoffBudget"), dict) else {}
    handoff_budget_ok = int(handoff_budget.get("maxChangedFiles", 0) or 0) > 0 and int(handoff_budget.get("maxRequiredReadTokens", 0) or 0) > 0 and handoff_budget.get("conditionalReadsAreDeferred") is True
    out.append(result("workflow:handoff-token-budget", "pass" if handoff_budget_ok else "fail", f"strategy={handoff_budget.get('strategy')}; maxChangedFiles={handoff_budget.get('maxChangedFiles')}; maxRequiredReadTokens={handoff_budget.get('maxRequiredReadTokens')}"))
    handoff_state = read_json(HARNESS / "handoff" / "handoff.json", {}) or {}
    handoff_ctx = handoff_state.get("contextBudget", {}) if isinstance(handoff_state.get("contextBudget"), dict) else {}
    required_tokens = int(handoff_ctx.get("estimatedRequiredReadTokens", 0) or 0)
    max_required_tokens = int(handoff_budget.get("maxRequiredReadTokens", 0) or 0)
    handoff_pack_ok = bool(required_tokens and max_required_tokens and required_tokens <= max_required_tokens and isinstance(handoff_state.get("conditionalReads"), list))
    out.append(result("workflow:handoff-context-pack", "pass" if handoff_pack_ok else "fail", f"requiredTokens={required_tokens}; maxRequiredReadTokens={max_required_tokens}; strategy={handoff_ctx.get('strategy')}"))
    required = [
        "specs/00-universal/agentic-map-reduce.md",
        "specs/00-universal/agentic-fork-join.md",
        ".harness/workflows/WORKFLOWS.md",
        ".harness/workflows/workflow-profiles.json",
        "docs/AGENTIC_WORKFLOWS.md",
        "schemas/workflow.schema.json",
        "schemas/worker-result.schema.json",
        "schemas/reduce-result.schema.json",
        "schemas/reviewer-result.schema.json",
        "schemas/workflow-locks.schema.json",
        "schemas/merge-plan.schema.json",
        "schemas/async-task.schema.json",
        "schemas/async-group.schema.json",
        "schemas/await-result.schema.json",
        "specs/00-universal/agentic-async-await.md",
        "docs/WORKER_RESULT_EXAMPLES.md",
        "docs/CONTROLLED_PARALLEL_WRITES.md",
        "docs/WORKFLOW_TOKEN_ECONOMICS.md",
    ]
    for item in required:
        path = ROOT / item
        out.append(result(f"workflow:file:{item}", "pass" if path.exists() else "fail", "exists" if path.exists() else "missing"))
    required_spec_sections = ["## Goal", "## Applies to", "## Invariants", "## Agent behavior", "## Validation evidence", "## Escalation triggers", "## Reference anchors"]
    for item in ["specs/00-universal/agentic-map-reduce.md", "specs/00-universal/agentic-fork-join.md"]:
        path = ROOT / item
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        missing = [section for section in required_spec_sections if section not in text]
        out.append(result(f"workflow:spec-sections:{item}", "pass" if not missing else "fail", "ok" if not missing else "missing: " + ", ".join(missing)))
    contract = (ROOT / ".harness" / "prompts" / "subagent-contract.md").read_text(encoding="utf-8", errors="ignore") if (ROOT / ".harness" / "prompts" / "subagent-contract.md").exists() else ""
    out.append(result("workflow:worker-result-contract", "pass" if "WORKER_RESULT" in contract and "REDUCE_RESULT" in contract else "fail", "contracts present" if "WORKER_RESULT" in contract and "REDUCE_RESULT" in contract else "missing contracts"))
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8", errors="ignore") if (ROOT / ".gitignore").exists() else ""
    out.append(result("workflow:runtime-ignored", "pass" if ".harness/workflows/active/" in gitignore else "fail", "active workflows ignored" if ".harness/workflows/active/" in gitignore else "active workflows not ignored"))
    hook = ROOT / "tools" / "hooks" / "workflow_write_guard.py"
    out.append(result("workflow:write-guard", "pass" if hook.exists() else "fail", "exists" if hook.exists() else "missing"))
    runtime_sources = []
    for runtime_path in [ROOT / "scripts" / "harness.py", ROOT / "scripts" / "harness_lib" / "async_runtime.py", ROOT / "scripts" / "harness_lib" / "workflow_reduce.py", ROOT / "scripts" / "harness_lib" / "workflow_plan.py", ROOT / "scripts" / "harness_lib" / "workflow_run.py", ROOT / "scripts" / "harness_lib" / "workflow_writes.py", ROOT / "scripts" / "harness_lib" / "workflow_lifecycle.py"]:
        if runtime_path.exists():
            runtime_sources.append(runtime_path.read_text(encoding="utf-8", errors="ignore"))
    harness_py = "\n".join(runtime_sources)
    for token in ["workflow_resume", "workflow_unlock", "acquire_workflow_lock", "workflow_harness_result", "workflow_execute", "executor_concurrency_limit", "rate_limit_detected", "allow_partial", "workflow_suggestion", "graphify_shards", "normalize_graphify_graph", "workflow_promote", "prepare_agentic_reducer_packet", "cmd_workflow_review_reduce", "validate_executor_adapter", "balance_map_shards", "validate_reviewer_result", "workflow_review_gate_status", "cmd_workflow_adopt_agent_reduce", "workflow_prepare_write", "workflow_merge_plan", "workflow_apply_merge", "workflow_rollback", "workflow_cleanup_write", "workflow_start", "workflow_await", "workflow_cancel", "workflow_async_collect", "workflow_async_status", "workflow_async_recover", "workflow_async_supervisor", "workflow_token_audit", "estimate_tokens_from_text", "asyncio.create_subprocess_exec", "asyncio.Queue"]:
        out.append(result(f"workflow:runtime-function:{token}", "pass" if token in harness_py else "fail", "present" if token in harness_py else "missing"))
    proc = run_fixture_command([sys.executable, "scripts/harness.py", "executor", "validate"], timeout=fixture_timeout())
    ok = proc.returncode == 0 and '"allValid": true' in proc.stdout
    out.append(result("workflow:executor-validate", "pass" if ok else "fail", "ok" if ok else (proc.stderr or proc.stdout)[:300]))
    return out

def check_self_review_freshness() -> list[dict[str, str]]:
    """SPEC-109 SE.3: the gate owns the self-review cadence (graph-freshness pattern).

    When the last self-review is stale (>N days or >M commits), run it inline —
    local, offline, seconds — so improvement findings are born from the process
    instead of periodic human prompts. Diagnosis never blocks work: the check
    fails only if the review itself crashes.
    """
    try:
        # A gate spawned INSIDE scenario isolation (e.g. rt6's inner smoke gate) must
        # never run the MUTATING inline review: it rewrites the real backlog inbox +
        # escalations mid-scenario (2026-07-20 rt6 audit — surfaced as phantom new
        # dirt). Cadence belongs to the outer gate on the live tree.
        if os.environ.get("HARNESS_SCENARIO_ISOLATED") == "1":
            return [result("self-review:freshness", "skip",
                           "scenario-isolated context: inline self-review suppressed (mutates real backlog)")]
        from harness_lib import self_review as self_review_lib
        config = self_review_lib.load_config(ROOT)
        reason = self_review_lib.staleness(ROOT, config)
        if reason is None:
            state = read_json(ROOT / self_review_lib.STATE_REL, {}) or {}
            actionable = sum(1 for f in state.get("findings", []) if f.get("severity") == "action")
            return [result("self-review:freshness", "pass",
                           f"fresh (last run {state.get('runAt')}); {actionable} actionable finding(s) pending triage")]
        import harness  # late import: the CLI router provides list_escalations/append_event
        state = self_review_lib.run_self_review(ROOT, list_escalations=harness.list_escalations,
                                                emit_event=harness.append_event)
        actionable = sum(1 for f in state.get("findings", []) if f.get("severity") == "action")
        return [result("self-review:freshness", "pass",
                       f"ran ({reason}): {actionable} actionable finding(s), "
                       f"{len(state.get('raisedEscalations', []))} new escalation(s), "
                       f"safe actions: {'; '.join(state.get('safeActions', [])) or 'none'}")]
    except Exception as exc:  # noqa: BLE001
        return [result("self-review:freshness", "fail",
                       f"self-review crashed: {exc}; fix: python scripts/harness.py self-review")]


def check_commit_hook_adoption() -> list[dict[str, str]]:
    """Report whether the commit-attached graph refresh is wired in.

    Advisory only (never fails): freshness is gate-owned as the safety net;
    per-commit refresh is an opt-in convenience.
    """
    try:
        proc = subprocess.run(["git", "config", "core.hooksPath"],
                              cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
        configured = proc.stdout.strip()
    except Exception:  # noqa: BLE001
        configured = ""
    if configured == "tools/git-hooks":
        return [result("graphify:commit-hook", "pass", "core.hooksPath -> tools/git-hooks")]
    return [result("graphify:commit-hook", "skip",
                   "enable per-commit graph refresh: git config core.hooksPath tools/git-hooks")]


def check_operator_only_gates() -> list[dict[str, str]]:
    """RF.1: the two operator-only escapes must not be reachable from automation.

    `role_calibration` claims in prose that no gate or scenario may pass `--execute`
    (real token spend) and that `apply_recommendation` never auto-flips a role's
    routing. Both claims were ENFORCED BY DOCSTRING ONLY. A claim that costs the
    owner money or silently rewrites routing is not an advisory class -- this FAILS.

    The rule is deliberately about the CALLER, not the callee: passing the flags is
    legitimate from an operator shell, so the check scans the automated surface
    (`testing/`, `tools/`, and the gate runner) for a call that supplies them. The
    module's own docstrings/self-check are excluded -- they name the flags in prose,
    which is the point of the claim, not a violation of it."""
    out: list[dict[str, str]] = []
    root = Path(__file__).resolve().parents[2]  # scripts/harness_lib/ -> repo root
    offenders: list[str] = []
    scanned = [p for rel_dir in ("testing", "tools") if (root / rel_dir).is_dir()
               for p in (root / rel_dir).rglob("*.py")]
    scanned.append(root / "scripts" / "spec_test_gate.py")  # the gate runner runs unattended: automation too
    for path in scanned:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # The module's OWN acceptance scenario must be able to exercise the
        # guarded path — every guard in this repo works that way (gps sabotages
        # the lane, hsb monkeypatches fs_backend). It drives a tmp root with
        # injected routing and never reaches real config, so it is excluded by
        # NAME, narrowly. Any other automated caller is a violation.
        if path.name == "rc_role_calibration.py":
            continue
        for needle in ("confirm=True", '"--execute"', "'--execute'"):
            if needle in text and "role_calibration" in text:
                offenders.append(f"{path.relative_to(root).as_posix()} -> {needle}")
    out.append({
        "name": "rf1:operator-only-escapes",
        "status": "fail" if offenders else "pass",
        "detail": ("automation reaches an operator-only escape: " + "; ".join(sorted(offenders)))
        if offenders else "no automated caller supplies --execute or confirm=True",
    })
    return out


def check_gate_controlplane() -> list[dict[str, str]]:
    """SPEC-137 / row gate-controlplane-excluded: the gate's OWN control-plane must
    live inside the surface it enforces AND inside `VALIDATOR_INPUTS`.

    Reads the LIVE `.harness/project.json` + the LIVE `VALIDATOR_INPUTS` — never a
    scenario's hardcoded `POLICY` copy. Every gate scenario carries its own dict, so
    asserting against one proves nothing about this repo (a measured tautology).
    This check FAILS, unlike `check_precommit_override` below, which is advisory by
    release decision.

    It failed before 2026-07-27: `exclude` carried `tools/git-hooks/**`, so
    `tools/git-hooks/pre-commit` — 3 lines that `exec` the gate — could be replaced
    by `exit 0`, killing gate + reckon + post-commit audit at once, while staling no
    stamp (no hook was in `VALIDATOR_INPUTS` either, nor was `validation_stamp.py`,
    which defines the manifest, the exclude matcher and the profile map).

    Not satisfiable by destroying the surface: an emptied `surfaceRoots` is falsy and
    falls back to `SURFACE_DEFAULT`, which still contains `tools`; dropping `tools`
    outright makes the manifest miss the shims and this check FAIL. `skip` only when
    there is no control-plane to check — no tracked shims (a downstream project that
    never installed them) or no git index (the parallel temp-copy omits `.git`).
    """
    def _tracked(*pathspec: str) -> list[str] | None:
        try:
            proc = subprocess.run(["git", "ls-files", "--", *pathspec], cwd=str(ROOT),
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=30)
        except Exception:  # noqa: BLE001 — no git => nothing to assert against
            return None
        if proc.returncode != 0:
            return None
        return sorted(p for p in (ln.strip() for ln in proc.stdout.splitlines()) if p)

    try:
        from harness_lib import validation_stamp as vs
        shims = _tracked("tools/git-hooks")
        if shims is None:
            return [result("precommit:control-plane", "skip", "no readable git index")]
        if not shims:
            return [result("precommit:control-plane", "skip",
                           "no tracked tools/git-hooks shims; nothing to guard")]
        out: list[dict[str, str]] = []
        policy = vs.load_policy(ROOT)
        if policy is None:
            out.append(result("precommit:control-plane-surface", "fail",
                              "precommitValidation is absent/disabled while the git-hook shims "
                              "are tracked — the gate's own control-plane is unguarded"))
        else:
            manifest = {ln.split("\t", 1)[1] for ln in vs.staged_manifest(ROOT, policy)
                        if "\t" in ln}
            missing = [p for p in shims if p not in manifest]
            out.append(result(
                "precommit:control-plane-surface", "fail" if missing else "pass",
                ("outside the gate surface (excluded, or off every surfaceRoot): "
                 + ", ".join(missing)) if missing
                else f"{len(shims)} git-hook shim(s) inside the gate surface"))
        # An edit to any control-plane file must stale every existing stamp.
        guarded = {"tools/hooks/precommit_validation_gate.py",
                   "scripts/harness_lib/validation_stamp.py", *shims}
        hashed = _tracked(*vs.VALIDATOR_INPUTS)
        if hashed is None:
            out.append(result("precommit:control-plane-validator-inputs", "skip",
                              "VALIDATOR_INPUTS not resolvable against the index"))
        else:
            unhashed = sorted(guarded - set(hashed))
            out.append(result(
                "precommit:control-plane-validator-inputs", "fail" if unhashed else "pass",
                ("not hashed into validator_version, so editing it stales no stamp: "
                 + ", ".join(unhashed)) if unhashed
                else f"{len(guarded)} control-plane file(s) hashed into validator_version"))
        return out
    except Exception as exc:  # noqa: BLE001 — a security check that cannot run is a fail
        return [result("precommit:control-plane", "fail", f"control-plane check crashed: {exc}")]


def check_precommit_override() -> list[dict[str, str]]:
    """SPEC-137: advisory audit for a `--no-verify` bypass of the pre-commit gate.

    Advisory only (never fails, per the release decision): `pass` when the policy is
    disabled, the HEAD surface fingerprint is in the validation history, or the surface
    is unchanged vs the parent; `skip` when HEAD changed the surface without a matching
    stamp. Degrades to `pass` inside the temp-tree gate run (HARNESS_STAGED_SNAPSHOT=1)
    and on any git error — this check itself runs inside a fresh, commit-less scratch
    tree during `validate --staged`, so it must never raise there.
    """
    try:
        if os.environ.get("HARNESS_STAGED_SNAPSHOT") == "1":
            return [result("precommit:override-audit", "pass", "staged-snapshot run; override audit not applicable")]
        from harness_lib import validation_stamp
        if validation_stamp.load_policy(ROOT) is None:
            return [result("precommit:override-audit", "pass", "precommitValidation disabled; override audit not applicable")]
        override = validation_stamp.detect_override(ROOT)
        if override is None:
            return [result("precommit:override-audit", "pass", "HEAD surface validated or unchanged")]
        return [result("precommit:override-audit", "skip",
                       f"HEAD {str(override.get('sha', ''))[:12]} surface not in validation history (--no-verify bypass?)")]
    except Exception as exc:  # noqa: BLE001 — advisory, never fail the gate
        return [result("precommit:override-audit", "skip", f"override audit unavailable: {exc}")]
