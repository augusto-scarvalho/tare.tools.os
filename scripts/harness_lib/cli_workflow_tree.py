"""Workflow subcommand tree (harness-lines burndown, workflow-block r2).

The `workflow <cmd>` argparse tree used to be the last parser mass pinned
inline in scripts/harness.py's main(). It now registers here; the parent
`wp = sub.add_parser("workflow", ...)` and every cmd_workflow_* HANDLER
function stay in harness.py -- this module owns parser wiring only. Adding a
future workflow subcommand is one `wsub.add_parser` line HERE plus its
handler in harness.py.

register(wsub, h): `wsub` is the workflow parser's _SubParsersAction; `h` is
the harness runtime module, passed by harness.py as sys.modules[__name__] so
this module never imports harness (no circular import). Handlers resolve as
h.cmd_workflow_*; AWAIT_MODES (defined in harness.py) resolves via h too.
observability_exporters constants come from their harness_lib home directly.

The gate's `workflow-command-surface` check (spec_test_gate.py) scans
harness.py + this file for `wsub.add_parser` name literals and compares the
union against .harness/project.json's supported/internal workflow commands.
Every moved line is byte-identical to its main() original (help output is
golden-pinned); registration order is preserved.
"""
from __future__ import annotations

import argparse

from harness_lib import observability_exporters


def register(wsub, h) -> None:
    """Register every workflow subcommand on `wsub`, verbatim from main()."""
    AWAIT_MODES = h.AWAIT_MODES
    p = wsub.add_parser("plan")
    p.add_argument("words", nargs="*")
    p.add_argument("--task")
    p.add_argument("--type", choices=["map-reduce", "fork-join"])
    p.add_argument("--profile", help="Optional workflow profile from .harness/workflows/workflow-profiles.json")
    p.add_argument("--split", choices=["roots", "changed-files", "graphify", "explicit", "specs-tasks-docs"], default="roots")
    p.add_argument("--shard", action="append")
    p.add_argument("--branch", action="append")
    p.add_argument("--branch-json", help="SPEC-119 v7: a JSON array of fork-join branch OBJECTS [{title, taskProfile?, workerRole?, paths?, spawn?}] (the composer materializing composed branches); mutually exclusive with --branch. Unknown taskProfile fails at plan time (closed vocab via default_fork_branches).")
    p.add_argument("--include", action="append")
    p.add_argument("--exclude", action="append")
    p.add_argument("--max-workers", type=int)
    p.add_argument("--concurrency", type=int)
    p.add_argument("--diff-scope", choices=["working-tree", "staged", "unstaged"], default="working-tree")
    p.add_argument("--base")
    p.add_argument("--range")
    p.add_argument("--write-allowed", action="store_true", help="Enable controlled write workflow; requires --approval-token")
    p.add_argument("--parallel-writes", action="store_true", help="Allow concurrent write workers only after explicit approval and prepare-write")
    p.add_argument("--isolation", choices=["temp-copy", "git-worktree"], default=None)
    p.add_argument("--approval-token")
    p.add_argument("--seed", action="append", metavar="PRIOR_WF", help="Seed a convergence wave from a prior workflow's reduce (F1); repeatable: multiple seeds fan one critic wave into several reduced sources (round-robin cap 12, depth max+1, own-executor branches excluded); convergence-only (forbidden for divergence profiles), depth-bounded")
    p.add_argument("--validate-only", action="store_true", help="N1 compile loop: validate the candidate plan (profile/branches/budgets, token-audit) and secret-scan candidate content, print the report, write NO active/WF-* dir. A token-budget breach is a compile error unless --override-budget.")
    p.add_argument("--override-budget", action="store_true", help="With --validate-only, downgrade a token-budget breach from a compile error to a warning")
    p.add_argument("--width-mode", choices=["focused", "exploratory", "custom"], help="D010: declare the fan-out width band — focused (1-2), exploratory (4-5), or custom (1..maxWorkers). Required for research-* profiles; width itself is --max-workers/branch trimming.")
    p.add_argument("--width-why", help="D010: justify the fan-out width (arbitrated by Delta_m). Required for research-* profiles. No width written = do not start the wave.")
    p.add_argument("--explain", action="store_true", help="DW.4: after planning, also print the observe-only topologyRouter verdict (why it would/wouldn't fork; recommendedWorkers is always 1 this phase)")
    p.add_argument("--target", help="Plan a read-only analysis workflow over a registered target repository (HT-T5); shard paths are target-relative"); p.set_defaults(func=h.cmd_workflow_plan)
    p = wsub.add_parser("execute")
    p.add_argument("words", nargs="*")
    p.add_argument("--task")
    p.add_argument("--type", choices=["map-reduce", "fork-join"])
    p.add_argument("--profile")
    p.add_argument("--split", choices=["roots", "changed-files", "graphify", "explicit", "specs-tasks-docs"], default="roots")
    p.add_argument("--shard", action="append")
    p.add_argument("--branch", action="append")
    p.add_argument("--executor")
    p.add_argument("--concurrency", type=int)
    p.add_argument("--timeout", type=int)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--override-budget", action="store_true", help="proceed past an enforced token-budget failure (recorded as an event)")
    p.add_argument("--allow-placeholder", action="store_true")
    p.add_argument("--no-finalize", action="store_true")
    p.add_argument("--promote", action="store_true")
    p.add_argument("--promote-target", choices=["draft", "canonical-task"], default="draft")
    p.add_argument("--pause-before-run", action="store_true")
    p.add_argument("--pause-before-reduce", action="store_true")
    p.add_argument("--pause-before-finalize", action="store_true")
    p.add_argument("--pause-before-promote", action="store_true")
    p.add_argument("--write-allowed", action="store_true", help="Enable controlled write workflow; requires --approval-token")
    p.add_argument("--parallel-writes", action="store_true")
    p.add_argument("--isolation", choices=["temp-copy", "git-worktree"], default=None)
    p.add_argument("--approval-token")
    p.set_defaults(func=h.cmd_workflow_execute)
    p = wsub.add_parser("suggest"); p.add_argument("words", nargs="*"); p.add_argument("--task"); p.set_defaults(func=h.cmd_workflow_suggest)
    p = wsub.add_parser("status"); p.add_argument("workflow_id", nargs="?"); p.set_defaults(func=h.cmd_workflow_status)
    p = wsub.add_parser("list"); p.set_defaults(func=h.cmd_workflow_status, workflow_id=None)
    p = wsub.add_parser("token-audit"); p.add_argument("workflow_id", nargs="?"); p.add_argument("--no-write-report", action="store_true"); p.set_defaults(func=h.cmd_workflow_token_audit)
    p = wsub.add_parser("trace"); p.add_argument("workflow_id"); p.add_argument("--limit", type=int, default=200); p.add_argument("--digest", action="store_true"); p.set_defaults(func=h.cmd_workflow_trace)
    p = wsub.add_parser("trace-export"); p.add_argument("workflow_id"); p.add_argument("--format", choices=sorted(observability_exporters.SUPPORTED_TRACE_EXPORT_FORMATS), default="otlp-json"); p.add_argument("--output"); p.add_argument("--limit", type=int, default=200); p.set_defaults(func=h.cmd_workflow_trace_export)
    p = wsub.add_parser("trace-push"); p.add_argument("workflow_id"); p.add_argument("--target", choices=sorted(observability_exporters.SUPPORTED_TRACE_PUSH_TARGETS), default="otlp-http"); p.add_argument("--endpoint"); p.add_argument("--api-key-env"); p.add_argument("--limit", type=int, default=200); p.add_argument("--timeout", type=int, default=10); p.add_argument("--send", action="store_true", help="Opt-in network send; also requires HARNESS_ALLOW_NETWORK_EXPORT=1"); p.set_defaults(func=h.cmd_workflow_trace_push)
    p = wsub.add_parser("state"); p.add_argument("workflow_id"); p.add_argument("--mirror", action="store_true", help="Mirror known workflow JSON artifacts into StateStore before summarizing"); p.add_argument("--recover-artifacts", action="store_true", help="Recover missing known JSON artifacts from StateStore"); p.set_defaults(func=h.cmd_workflow_state)
    p = wsub.add_parser("spawn-commands"); p.add_argument("workflow_id"); p.add_argument("--executor"); p.add_argument("--all", dest="only_missing", action="store_false", default=True); p.set_defaults(func=h.cmd_workflow_spawn_commands)
    p = wsub.add_parser("run"); p.add_argument("workflow_id"); p.add_argument("--executor"); p.add_argument("--concurrency", type=int); p.add_argument("--timeout", type=int); p.add_argument("--dry-run", action="store_true"); p.add_argument("--all", dest="only_missing", action="store_false", default=True); p.add_argument("--allow-placeholder", action="store_true"); p.add_argument("--force-round", action="store_true"); p.add_argument("--force-lock", action="store_true"); p.add_argument("--allow-frontier", action="store_true", help="SPEC-153: acknowledge a frontier-model fan-out (the only ack; --executor claude is NOT one). HARNESS_ALLOW_FRONTIER=1 also acks."); p.add_argument("--model", help="DD-mechanization P1: pin EVERY worker's model card, overriding the routed profile spawn (exact, no fallback)"); p.add_argument("--effort", help="DD-mechanization P1: pin EVERY worker's effort/reasoning level"); p.set_defaults(func=h.cmd_workflow_run)
    p = wsub.add_parser("start"); p.add_argument("workflow_id"); p.add_argument("--executor"); p.add_argument("--concurrency", type=int); p.add_argument("--timeout", type=int); p.add_argument("--mode", choices=sorted(AWAIT_MODES)); p.add_argument("--min-success", type=int); p.add_argument("--dry-run", action="store_true"); p.add_argument("--override-budget", action="store_true", help="proceed past an enforced token-budget failure (recorded as an event)"); p.add_argument("--all", dest="only_missing", action="store_false", default=True); p.add_argument("--allow-placeholder", action="store_true"); p.add_argument("--force-round", action="store_true"); p.add_argument("--force-lock", action="store_true"); p.add_argument("--allow-frontier", action="store_true", help="SPEC-153: acknowledge a frontier-model async fan-out (the only ack; --executor claude is NOT one). HARNESS_ALLOW_FRONTIER=1 also acks."); p.add_argument("--model", help="DD-mechanization P1: pin EVERY worker's model card, overriding the routed profile spawn (exact, no fallback)"); p.add_argument("--effort", help="DD-mechanization P1: pin EVERY worker's effort/reasoning level"); p.set_defaults(func=h.cmd_workflow_start)
    p = wsub.add_parser("async-supervisor", help=argparse.SUPPRESS); p.add_argument("workflow_id"); p.add_argument("--executor", required=True); p.add_argument("--concurrency", type=int, required=True); p.add_argument("--timeout", type=int, required=True); p.set_defaults(func=h.cmd_workflow_async_supervisor)
    p = wsub.add_parser("await"); p.add_argument("workflow_id"); p.add_argument("--mode", choices=sorted(AWAIT_MODES)); p.add_argument("--timeout", type=int); p.add_argument("--min-success", type=int); p.add_argument("--poll-interval", type=float, default=1.0); p.add_argument("--cancel-on-timeout", action="store_true"); p.set_defaults(func=h.cmd_workflow_await)
    p = wsub.add_parser("collect"); p.add_argument("workflow_id"); p.add_argument("--recover", action="store_true"); p.set_defaults(func=h.cmd_workflow_collect)
    p = wsub.add_parser("cancel"); p.add_argument("workflow_id"); p.add_argument("--worker-id"); p.add_argument("--reason"); p.add_argument("--grace-seconds", type=int, default=10); p.set_defaults(func=h.cmd_workflow_cancel)
    p = wsub.add_parser("reorder", help="Reorder the pending async queue: `workflow reorder <wfid...>` persists an explicit queue order over the active/ listing (stable fallback keeps unlisted pending ids in today's order); running/settled/unknown ids are refused (pending only)"); p.add_argument("workflow_ids", nargs="+"); p.set_defaults(func=h.cmd_workflow_reorder)
    p = wsub.add_parser("async-status"); p.add_argument("workflow_id"); p.add_argument("--recover", action="store_true"); p.set_defaults(func=h.cmd_workflow_async_status)
    p = wsub.add_parser("async-recover"); p.add_argument("workflow_id"); p.set_defaults(func=h.cmd_workflow_async_recover)
    p = wsub.add_parser("doctor"); p.add_argument("workflow_id"); p.set_defaults(func=h.cmd_workflow_doctor)
    p = wsub.add_parser("watch"); p.add_argument("workflow_id"); p.add_argument("--follow", action="store_true"); p.add_argument("--limit", type=int, default=100); p.add_argument("--poll-interval", type=float, default=1.0); p.add_argument("--timeout", type=int); p.set_defaults(func=h.cmd_workflow_watch)
    p = wsub.add_parser("tail"); p.add_argument("workflow_id"); p.add_argument("--worker-id"); p.add_argument("--lines", type=int, default=50); p.add_argument("--follow", action="store_true"); p.add_argument("--poll-interval", type=float, default=1.0); p.add_argument("--timeout", type=int); p.set_defaults(func=h.cmd_workflow_tail)
    p = wsub.add_parser("validate-results"); p.add_argument("workflow_id"); p.set_defaults(func=h.cmd_workflow_validate_results)
    p = wsub.add_parser("reduce"); p.add_argument("workflow_id"); p.add_argument("--allow-partial", action="store_true"); p.add_argument("--agent", action="store_true"); p.add_argument("--executor"); p.set_defaults(func=h.cmd_workflow_reduce)
    p = wsub.add_parser("review-reduce"); p.add_argument("workflow_id"); p.add_argument("--executor"); p.add_argument("--validate-existing", action="store_true"); p.set_defaults(func=h.cmd_workflow_review_reduce)
    p = wsub.add_parser("adopt-agent-reduce"); p.add_argument("workflow_id"); p.add_argument("--force", action="store_true"); p.set_defaults(func=h.cmd_workflow_adopt_agent_reduce)
    p = wsub.add_parser("mark"); p.add_argument("workflow_id"); p.add_argument("worker_id"); p.add_argument("status"); p.add_argument("--note"); p.add_argument("--force", action="store_true"); p.set_defaults(func=h.cmd_workflow_mark)
    p = wsub.add_parser("retry"); p.add_argument("workflow_id"); p.add_argument("worker_id"); p.add_argument("--clear-result", action="store_true"); p.add_argument("--force-round", action="store_true"); p.set_defaults(func=h.cmd_workflow_retry)
    p = wsub.add_parser("finalize"); p.add_argument("workflow_id"); p.add_argument("--no-update-context", action="store_true"); p.add_argument("--no-regenerate-handoff", action="store_true"); p.add_argument("--skip-review", action="store_true"); p.set_defaults(func=h.cmd_workflow_finalize)
    p = wsub.add_parser("resume"); p.add_argument("workflow_id"); p.add_argument("--executor"); p.add_argument("--concurrency", type=int); p.add_argument("--timeout", type=int); p.add_argument("--dry-run", action="store_true"); p.add_argument("--allow-placeholder", action="store_true"); p.add_argument("--force-round", action="store_true"); p.add_argument("--force-lock", action="store_true"); p.add_argument("--only-blocked-rate-limit", action="store_true"); p.set_defaults(func=h.cmd_workflow_resume)
    p = wsub.add_parser("unlock"); p.add_argument("workflow_id"); p.add_argument("--stale-only", action="store_true"); p.add_argument("--force", action="store_true"); p.set_defaults(func=h.cmd_workflow_unlock)
    p = wsub.add_parser("events"); p.add_argument("workflow_id"); p.add_argument("--worker-id"); p.add_argument("--limit", type=int, default=100); p.set_defaults(func=h.cmd_workflow_events)
    p = wsub.add_parser("promote"); p.add_argument("workflow_id"); p.add_argument("--limit", type=int, default=10); p.add_argument("--to", choices=["draft", "canonical-task"], default="draft"); p.set_defaults(func=h.cmd_workflow_promote)
    p = wsub.add_parser("prepare-write"); p.add_argument("workflow_id"); p.add_argument("--mode", choices=["temp-copy", "git-worktree"], default="temp-copy"); p.add_argument("--no-create-workspaces", action="store_true"); p.add_argument("--allow-conflicts", action="store_true"); p.add_argument("--approval-token", required=True); p.add_argument("--create-lock", action="append", help="worker-id:path create permission"); p.add_argument("--delete-lock", action="append", help="worker-id:path delete permission"); p.add_argument("--rename-lock", action="append", help="worker-id:old-path:new-path rename permission"); p.set_defaults(func=h.cmd_workflow_prepare_write)
    p = wsub.add_parser("prepare-worktrees"); p.add_argument("workflow_id"); p.add_argument("--approval-token", required=True); p.add_argument("--force", action="store_true"); p.set_defaults(func=h.cmd_workflow_prepare_worktrees)
    p = wsub.add_parser("cleanup-worktrees"); p.add_argument("workflow_id"); p.add_argument("--force", action="store_true"); p.set_defaults(func=h.cmd_workflow_cleanup_worktrees)
    p = wsub.add_parser("merge-plan"); p.add_argument("workflow_id"); p.add_argument("--validation-gate"); p.set_defaults(func=h.cmd_workflow_merge_plan)
    p = wsub.add_parser("apply-merge"); p.add_argument("workflow_id"); p.add_argument("--approval-token", required=True); p.add_argument("--validation-gate"); p.add_argument("--final-gate"); p.add_argument("--rollback-on-final-gate-failure", action="store_true"); p.add_argument("--rollback-on-validation-gate-failure", action="store_true"); p.add_argument("--dry-run", action="store_true"); p.add_argument("--interactive", action="store_true", help="P-2 per-item approval: step through every planned operation (diff preview, y/n/a/q) BEFORE anything applies; needs a real terminal"); p.set_defaults(func=h.cmd_workflow_apply_merge)
    p = wsub.add_parser("review-merge"); p.add_argument("workflow_id"); p.add_argument("--executor"); p.add_argument("--validate-existing", action="store_true"); p.set_defaults(func=h.cmd_workflow_review_merge)
    p = wsub.add_parser("rollback"); p.add_argument("workflow_id"); p.add_argument("--worker-id"); p.set_defaults(func=h.cmd_workflow_rollback)
    p = wsub.add_parser("cleanup-write"); p.add_argument("workflow_id"); p.add_argument("--force", action="store_true"); p.set_defaults(func=h.cmd_workflow_cleanup_write)
    p = wsub.add_parser("scrub"); p.add_argument("workflow_id"); p.add_argument("--force", action="store_true"); p.set_defaults(func=h.cmd_workflow_scrub)
    p = wsub.add_parser("fold", help="F1: emit a non-destructive fold manifest for a FINISHED workflow (dry-run) — artifact inventory + sha256 + reclaimable bytes + secret-scan seal; moves nothing"); p.add_argument("workflow_id"); p.set_defaults(func=h.cmd_workflow_fold)
    p = wsub.add_parser("evidence", help="UX-GA.3: deterministic reviewer evidence bundle — per-worker status/failureClass/result paths + CQ.2 oracleEvidence capsules, records refs, risk flags; handles only, no re-run"); p.add_argument("workflow_id"); p.add_argument("--worker-id"); p.set_defaults(func=h.cmd_workflow_evidence)
