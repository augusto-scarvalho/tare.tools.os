"""Frugal OS facade over the existing Backlog, SpecGraph, Kernel and audit APIs.

No daemon, new engine, implicit Work selection, hook installation or settlement.
"""
from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import sys
import tempfile
from uuid import uuid4
from relay import execution_resources


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def read_graph(path):
    # The graph owner validates semantics; this facade only bounds file input.
    with Path(path).open("rb") as stream:
        raw = stream.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError("GRAPH_TOO_LARGE")
    return json.loads(raw.decode("utf-8-sig"))


def parse_verify_spec(item):
    """Parse one --verify value into a checks dict entry.

    Supports the simple space-split form and a JSON array form:
      criterion=python -m pytest -q
      criterion=["executable", "arg with spaces", ""]

    No shell interpretation is ever applied.
    """
    if "=" not in item:
        raise ValueError("--verify requires criterion=command format")
    criterion, _, command_part = item.partition("=")
    if not criterion.strip():
        raise ValueError("--verify criterion must not be empty or whitespace")
    if not command_part:
        raise ValueError("--verify command must not be empty")
    if command_part.startswith("["):
        try:
            parsed = json.loads(command_part)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"--verify JSON array is malformed: {exc}") from exc
        if not isinstance(parsed, list):
            raise ValueError("--verify JSON value must be a list of argv strings")
        if not parsed:
            raise ValueError("--verify JSON array must not be empty")
        for idx, elem in enumerate(parsed):
            if not isinstance(elem, str):
                raise ValueError(f"--verify JSON element {idx} must be a string")
        if not parsed[0].strip():
            raise ValueError("--verify executable (first element) must not be empty or whitespace")
        command = parsed
    else:
        command = command_part.split()
        if not command:
            raise ValueError("--verify command must not be empty")
    return {"criterion": criterion, "command": command}


def doctor(*, root, graph=None):
    """Read-only observations, with unconfigured layers distinguished from PASS."""
    if not root.is_dir():
        raise ValueError("repository root is unavailable")
    layers = []

    def check(name, configured, inspect):
        if not configured:
            layers.append({"name": name, "status": "NOT_CONFIGURED"})
            return
        try:
            layers.append({"name": name, **inspect()})
        except Exception as error:
            # Diagnostics continue for independent layers, never invent health.
            layers.append({"name": name, "status": "UNAVAILABLE", "error": type(error).__name__,
                           "reason": str(error)[:240]})

    def backlog_health():
        from graph_backlog.validation import validate_work_graph
        report = validate_work_graph(read_graph(graph))
        return {"status": report["status"], "error_count": len(report.get("errors", [])),
                "verification": "graph_structure_and_contract"}

    def cache_health():
        from specgraph.cache import IncrementalCacheEngine
        report = IncrementalCacheEngine.detect_staleness(root)
        return {"status": "UNAVAILABLE" if report.discovery_errors else "PASS" if report.status == "FRESH" else "STALE",
                "cache_status": report.status, "checked_files": report.checked_files_count,
                "discovery_errors": len(report.discovery_errors),
                "verification": "cache_freshness_not_semantic_qualification"}

    check("backlog", graph is not None, backlog_health)
    check("specgraph", (root / "specgraph.yaml").is_file(), cache_health)
    configured = [layer for layer in layers if layer["status"] != "NOT_CONFIGURED"]
    healthy = bool(configured) and all(layer["status"] == "PASS" for layer in configured)
    return {"operational_health": "CHECKS_PASSED" if healthy else "ACTION_REQUIRED" if configured else "NOT_CONFIGURED",
            "qualification_status": "NOT_EVALUATED", "authority": "NONE", "layers": layers}


def context(*, graph, work_id, manifest=None, max_bytes=16384):
    """Project only the explicitly selected Work and its declared document refs.

    ``manifest`` is a run's sources file; without one, the refs are read from their owner trees at HEAD."""
    from graph_backlog.grounding import ground_work_item, encode_work_grounding
    from specgraph.ground import ground_repository, encode_grounding_report
    from tare_tools_agent_runtime import GroundingEnvelope, WorkEnvelope

    report = ground_work_item(graph, work_id, max_bytes=min(max_bytes, 8192))
    output = {"status": "BLOCKED", "authority": "NONE", "work": report.to_dict(), "groundings": []}
    if report.status != "READY":
        return output
    work = WorkEnvelope(encode_work_grounding(report))
    with tempfile.TemporaryDirectory() as directory:
        if manifest is None:
            from relay.sources import write
            manifest = write(Path(directory) / "SOURCES.json", {"repository_scopes": [
                {"repository": scope.repository, "grounding_refs": list(scope.grounding_refs)}
                for scope in work.repository_scopes]})
        for scope in work.repository_scopes:
            grounding = ground_repository(manifest, scope.repository, include_ids=scope.grounding_refs,
                                          max_bytes=min(max_bytes, 8192))
            output["groundings"].append(grounding.to_dict())
            if grounding.status not in {"READY", "READY_TRUNCATED", "NOT_MAPPED"}:
                output["reason"] = "REQUIRED_GROUNDING_UNAVAILABLE"
                return output
            GroundingEnvelope(encode_grounding_report(grounding))
    output["status"] = "READY_CONTEXT"
    # This is not an acquired Runtime binding or permission to execute Work.
    return output


def maintain(root, *, apply=False):
    """Only the owner's derived cache; no canonical graph, evidence or pins."""
    from specgraph.cache import IncrementalCacheEngine

    if not root.is_dir() or not (root / "specgraph.yaml").is_file():
        raise ValueError("explicit configured SpecGraph repository required")
    for path in (root / ".specgraph", root / ".specgraph" / "cache.json"):
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("derived cache must remain inside the selected repository")
    before = IncrementalCacheEngine.detect_staleness(root)
    if before.status == "FRESH":
        return {"status": "UNCHANGED", "applied": False, "authority": "NONE"}
    if not apply:
        return {"status": "REPAIR_AVAILABLE", "applied": False, "target": ".specgraph/cache.json",
                "authority": "NONE"}
    from specgraph.graph import CausalTraceabilityMatrix
    matrix = CausalTraceabilityMatrix.build(root, auto_cache=False)
    if matrix.parse_errors:
        raise ValueError("PARSE_ERRORS_NOT_REPAIRABLE_BY_CACHE_REFRESH")
    matrix.sync_cache(root_dir=root)
    after = IncrementalCacheEngine.detect_staleness(root)
    return {"status": "REPAIRED" if after.status == "FRESH" else "REPAIR_FAILED",
            "applied": True, "target": ".specgraph/cache.json", "authority": "NONE"}


CUSTODY = Path(__file__).resolve().parent.parent / ".reconciliation"
EXPLORER_API = None  # this machine's model gateway (tare setup --local-gateway)


def explore(root, question, *, focus=(), custody=CUSTODY, objective="Answer the caller's question",
            api_base=EXPLORER_API, timeout=240, sources_only=False, native_deployment=None,
            index_state=None, query=None):
    """Explore through native sources, optionally with a qualified native model."""
    ranges = []
    for item in focus:  # path:start-end
        path, _, lines = item.rpartition(":")
        start, _, end = lines.partition("-")
        ranges.append({"path": path, "start_line": int(start), "end_line": int(end or start)})
    if not sources_only and native_deployment is None:
        from relay.native_catalog import select
        native_deployment = select()
        if api_base is not None:
            raise ValueError('custom_api_base_requires_native_deployment_profile')
    if sources_only or native_deployment is not None:
        if sources_only and native_deployment is not None:
            raise ValueError('sources_only_does_not_invoke_a_model')
        from relay.native_exploration import explore as native_explore, view
        return view(native_explore(root, question, focus=ranges, custody=custody,
            objective=objective, timeout=timeout, deployment=native_deployment,
            index_state=index_state, query=query))


def evidence(root, path, start, end, *, revision="WORKTREE", custody=CUSTODY):
    """Citable live or Git range from the repository, up to 200 lines, no model."""
    import time
    from compute_plane.audit_sources import repository_refs
    from relay.native_audit_access import AuditAccess
    from relay.relay_io import write_json_atomic
    root = Path(root).resolve()
    custody = Path(custody).resolve()
    if custody.is_relative_to(root):
        raise ValueError('evidence_requires_external_custody')
    directory = custody / 'evidence' / root.name / uuid4().hex
    directory.mkdir(parents=True)
    manifest = directory / 'manifest.json'
    write_json_atomic(manifest, {'schema': 'tare.tools/audit-exploration/2',
        'repositories': repository_refs([{'id': root.name, 'root': str(root), 'target': 'WORKTREE'}]),
        'obligation_ids': [], 'config': {'transport': 'native-cli'},
        'cancel_path': str(directory / 'cancel'), 'expires_at': time.time() + 30})
    return AuditAccess(manifest).invoke('read', repository=root.name, path=path,
                                      start_line=start, end_line=end, revision=revision)


@execution_resources.cli
def main(argv=None):
    global CUSTODY
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ['models']:
        from relay.model_choices import main as models
        return models(argv[1:])
    if argv and argv[0] in {'explore', 'evidence', 'research'} and not any(x in argv for x in ('--help', '-h')):
        from relay.native_installation import storage_root
        CUSTODY = storage_root()
    if argv and argv[0] == "hook" and any(
        arg == "--session-config" or arg.startswith("--session-config=") for arg in argv
    ):
        from relay.session_hooks import main as session_hook
        return session_hook(argv[1:])
    # Pass through existing public CLIs; no second parser or policy for them.
    owners = {"findings": "relay.planning_findings", "research-flow": "relay.research_flow", "parallel-explore": "relay.parallel_explore", "sequence": "relay.work_sequence", "decision": "relay.human_wait", "work-control": "relay.work_control", "console": "relay.native_console", "plan": "relay.native_plan", "prepare": "relay.work_prepare", "revise": "relay.work_change", "telemetry": "scripts.round_table.report_round_table_telemetry",
              "sessions": "relay.native_sessions", "setup": "relay.environment", "review": "relay.native_review", "message": "relay.native_messages",
              "profiles": "relay.native_profiles", "mcp": "relay.native_mcp", "serve": "relay.native_serve", "ci": "relay.ci_watch", "gpu": "relay.gpu", "quota": "relay.provider_quota", "fleet": "relay.fleet", "storage": "relay.storage",
              "agent": "relay.agent", "audit": "relay.audit_cases", "audit-evidence": "relay.native_audit_access", "hook": "compute_plane.hook_cli",
              "backlog": "graph_backlog.cli", "specgraph": "specgraph.cli",
              "test-selection": "relay.test_selection_pilot", "workflow": "relay.workflow_compiler"}
    if argv and argv[0] in owners:
        try:
            return importlib.import_module(owners[argv[0]]).main(argv[1:])
        except ImportError:
            print("Required owner package is unavailable; no fallback selected.", file=sys.stderr)
            return 2
    from relay.model_choices import PIN_HELP, help_summary
    parser = argparse.ArgumentParser(prog="tare-ops", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Terminal UI: tare opens the current folder; tare PATH opens another folder (quote paths with spaces). Explicit form: console [--root PATH]. Owner CLIs: profiles ..., quota, mcp ..., serve ..., sessions ..., review ..., telemetry ..., agent ..., audit ..., hook <vendor> ..., backlog ..., specgraph ... "
               "Local delegation: explore, evidence, research (see docs/guides/DELEGATION_POLICY.md)\n"+
               (help_summary() if any(v in ('--help', '-h') for v in argv) else PIN_HELP))
    sub = parser.add_subparsers(dest="action", required=True)
    health = sub.add_parser("doctor", help="Read-only, independent diagnostic layers")
    health.add_argument("--root", type=Path, default=Path.cwd())
    health.add_argument("--graph", type=Path)
    health.add_argument("--native", action="store_true", help="Inspect the installed native runner and centralized custody")
    native_profile = health.add_mutually_exclusive_group()
    native_profile.add_argument('--deployment', help='Inspect a named native profile without invoking it')
    native_profile.add_argument('--native-deployment', type=Path, help='Inspect an explicit native deployment JSON')
    health.add_argument('--receipt', type=Path, help='Classify one existing native outcome or text receipt')
    ctx = sub.add_parser("context", help="Context for an explicit operational Work, not frontier[0]")
    ctx.add_argument("--graph", type=Path, required=True)
    ctx.add_argument("--work", required=True)
    repair = sub.add_parser("maintain", help="Preview derived cache repair; writes only with --apply")
    repair.add_argument("--root", type=Path, required=True)
    repair.add_argument("--apply", action="store_true")
    delegate = sub.add_parser("explore", help="Native read-only repository exploration; sources-only or explicit model")
    delegate.add_argument("--root", type=Path, default=Path.cwd())
    delegate.add_argument("--question", required=True)
    delegate.add_argument("--focus", action="append", default=[], help="path:start-end, repeatable")
    delegate.add_argument("--objective", default="Answer the caller's question")
    native_mode = delegate.add_mutually_exclusive_group()
    native_mode.add_argument("--sources-only", action="store_true", help="SpecGraph and verified reads; no model")
    native_mode.add_argument("--native-deployment", type=Path, help="Explicit native HTTP deployment JSON")
    native_mode.add_argument("--deployment", help="Named native profile; default local-qwen")
    delegate.add_argument("--index-state", type=Path, help="Published SpecGraph index for this exact root")
    delegate.add_argument("--query", help="Bounded structural lookup query; defaults to question")
    cite = sub.add_parser("evidence", help="Citable range from the live repository or a Git revision")
    cite.add_argument("--root", type=Path, default=Path.cwd())
    cite.add_argument("--path", required=True)
    cite.add_argument("--start", type=int, required=True)
    cite.add_argument("--end", type=int, required=True)
    cite.add_argument("--revision", default="WORKTREE")
    run = sub.add_parser("run", help="Execute and settle one explicit Work through the existing train lifecycle")
    run.add_argument("work_id")
    run.add_argument('--resume', action='store_true', help='Resume the original plan, implementation or accepted candidate settlement')
    run.add_argument('--run-id', help='Optional caller identity for this run; existing custody cannot be reused')
    run.add_argument('--retry-plan', action='store_true', help='With --resume: explicitly start a new review after a finished plan attempt; preserve previous evidence and saved per-attempt limits')
    run.add_argument('--retry-audit', action='store_true', help='With --resume: new code-audit attempt after operational failure; semantic reviews cannot be retried')
    run.add_argument("--deployment", help="Named native model profile; default local-qwen")
    run.add_argument("--native-deployment", type=Path, help="Native HTTP profile with a registered model identity")
    auditors = run.add_mutually_exclusive_group()
    auditors.add_argument('--auditor-deployment', action='append', help='Independent native auditor; auto selects configured independent routes, or repeat names for explicit fallback order')
    auditors.add_argument('--auditor-profile', type=Path, action='append', help='Independent auditor profile JSON; repeat for fallback order')
    run.add_argument("--repo-root", type=Path)
    run.add_argument('--findings', type=Path, help='Reviewed project-exploration findings reused by this Work')
    run.add_argument('--findings-sha256', help='Reviewed findings manifest identity')
    run.add_argument("--verify", action="append", default=[],
                     help="criterion=argv words or criterion=[\"argv\", ...] JSON, repeatable")
    run.add_argument("--dry-run", action="store_true", help="stop after descriptor and plan audit; no workspace")
    run.add_argument('--wait-before-implement', default='', metavar='QUESTION', help='Optional saved human decision after plan approval; answer with decision, then resume')
    from relay import native_limits
    run.add_argument('--command-review', choices=('tare', 'codex', 'both'), default=argparse.SUPPRESS)
    run.add_argument('--command-isolation', choices=('mxc', 'codex'), default=argparse.SUPPRESS)
    run.add_argument('--command-classifier', choices=('model', 'jev'), default=argparse.SUPPRESS)
    run.add_argument('--command-review-policy', choices=('always', 'on_demand'), default=argparse.SUPPRESS)
    run.add_argument('--max-command-reviews', type=int, default=argparse.SUPPRESS)
    run.add_argument('--command-review-seconds', type=int, default=argparse.SUPPRESS)
    execution_resources.add_argument(run)
    for flag, kind in (('max-model-calls', int), ('max-stagnant-turns', int), ('max-retries', int),
                       ('max-seconds', float), ('request-seconds', float), ('connect-seconds', float), ('read-seconds', float)):
        run.add_argument('--'+flag, type=native_limits.cli_limit(kind), default=argparse.SUPPRESS,
                         help='Per model stage (plan and implementation): number or unlimited; overrides --unlimited')
    run.add_argument('--max-compactions', type=native_limits.cli_limit(int), default=argparse.SUPPRESS,
                     help='Per model stage: count or unlimited; overrides --unlimited')
    state = sub.add_parser("status", help="Train state and last run receipt of a Work, from custody")
    state.add_argument("work_id")
    upcoming = sub.add_parser("next", help="Suggest Work using the Backlog owner's ordering; does not execute")
    upcoming.add_argument('--limit', type=int, default=10)
    explain = sub.add_parser("explain", help="Inspect one Work, its criteria and blockers without running it")
    explain.add_argument('work_id')
    for command in (upcoming, explain):
        command.add_argument('--profile', choices=('operational', 'planning'), default='operational')
    for command in (upcoming, explain, run, state):
        command.add_argument('--project-graph', type=Path)
        command.add_argument('--format', choices=('auto', 'text', 'json'), default='auto')
    study = sub.add_parser("research", help="Mass web collection plus local map/reduce; frontier gets one envelope")
    study.add_argument("--question")
    study.add_argument("--seed", action="append", default=[])
    study.add_argument("--domain", action="append", default=[])
    study.add_argument("--max-pages", type=int, default=50)
    study.add_argument("--max-depth", type=int, default=1)
    study.add_argument("--collect-only", action="store_true")
    study.add_argument("--native-deployment", type=Path, help="Native profile for map/reduce; no SDK defaults")
    study.add_argument("--deployment", help="Named native profile; default local-qwen")
    study.add_argument("--max-model-calls", type=int, default=32, help="Shared native map/reduce call ceiling")
    study.add_argument("--result", metavar="JOB", help="Print the envelope of a finished job")
    study.add_argument("--ref", help="With --result: print up to 200 lines of this W- ref")
    study.add_argument("--start", type=int, default=1, help="With --ref: first line (1-based)")
    study.add_argument("--end", type=int, default=200, help="With --ref: last line, at most 200 lines after --start")
    for command in (delegate, cite, study):
        command.add_argument("--custody", type=Path, default=CUSTODY)
        command.add_argument("--api-base", default=EXPLORER_API)
        command.add_argument("--timeout", type=int, default=240)
    for command in (health, ctx, repair, delegate, cite, study, run, state, upcoming, explain):
        command.add_argument("--max-bytes", type=int, default=16384)
    args = parser.parse_args(argv)
    output_format = getattr(args, 'format', 'json')
    if output_format == 'auto':
        output_format = 'text' if sys.stdout.isatty() else 'json'

    def present(output):
        if args.action == 'run' and isinstance(output, dict) and isinstance(output.get('audit'), dict):
            # Keep the exact review in its saved audit session; the terminal
            # needs only the decision and failure fields to explain this run.
            audit = output['audit']
            result = audit.get('result')
            if isinstance(result, dict):
                summary = {key: result[key] for key in
                           ('status', 'reason', 'failure_code', 'delivery_validation') if key in result}
                output = {**output, 'audit': {**audit, 'result': summary}}
        text = (encode(output).decode('utf-8') if output_format == 'json' else
                importlib.import_module('relay.work_console').render(args.action, output))
        if len((text + '\n').encode('utf-8')) > args.max_bytes:
            raise ValueError('OUTPUT_OVER_BUDGET: use the owner CLI for a narrower query')
        getattr(sys.stdout, 'reconfigure', lambda **kw: None)(encoding='utf-8')
        print(text)

    try:
        if not 1024 <= args.max_bytes <= 65536:
            raise ValueError("max-bytes must be between 1024 and 65536")
        if args.action in {'next', 'explain'}:
            from relay.work_console import inspect_work
            output = inspect_work(work_id=args.work_id if args.action == 'explain' else None,
                                  profile=args.profile, limit=getattr(args, 'limit', 10), graph_path=args.project_graph)
            ok = True
        elif args.action == "run":
            from relay.work_run import run_work
            from relay.native_catalog import select
            if args.retry_plan and not args.resume:
                raise ValueError('--retry-plan requires --resume')
            policy_keys = {'command_review': 'review', 'command_isolation': 'backend',
                           'command_classifier': 'classifier', 'command_review_policy': 'review_policy',
                           'max_command_reviews': 'max_reviews', 'command_review_seconds': 'review_seconds'}
            policy = {target: vars(args)[key] for key, target in policy_keys.items() if key in vars(args)}
            policy_options = {}
            if policy:
                if args.resume:
                    raise ValueError('WORK_RESUME_REUSES_ORIGINAL_EXECUTION_OPTIONS')
                if 'review' not in policy:
                    raise ValueError('Choose --command-review before setting command protection options.')
                from relay.command_protection import validate
                policy_options['command_protection'] = validate(policy)
            checks = [parse_verify_spec(item) for item in args.verify] or None
            limits = {('max_model_retries' if key == 'max_retries' else key): value
                      for key, value in vars(args).items() if key in native_limits.FIELDS or key == 'max_retries'}
            native_limits.validate(limits)
            if args.resume and (args.auditor_deployment or args.auditor_profile or args.deployment or args.native_deployment or args.verify or args.dry_run or args.wait_before_implement
                                or args.unlimited or limits or hasattr(args, 'max_compactions')):
                raise ValueError('WORK_RESUME_REUSES_ORIGINAL_EXECUTION_OPTIONS')
            from relay.work_audit import select_deployments
            from tare_tools_agent_runtime import CancellationToken
            import signal
            cancel = CancellationToken()
            from relay import native_signals
            previous = native_signals.install(cancel)
            try:
                output = run_work(args.work_id, graph_path=args.project_graph, deployment=args.deployment or 'local-qwen', repository_root=args.repo_root, checks=checks, dry_run=args.dry_run,
                                  native_deployment=None if args.resume else select(args.deployment, args.native_deployment, role='implementer'),
                                  limits=limits, unlimited=args.unlimited, wait_before_implement=args.wait_before_implement, run_id=args.run_id,
                                  findings=args.findings, findings_sha256=args.findings_sha256,
                                  audit_deployments=select_deployments(args.auditor_deployment, args.auditor_profile),
                                  max_compactions=getattr(args, 'max_compactions', native_limits.UNSET), cancellation=cancel, resume=args.resume, retry_plan=args.retry_plan, retry_audit=args.retry_audit, **policy_options)
            finally:
                native_signals.restore(previous)
            ok = output["status"] in {"SETTLED", "DRY_RUN", "WAITING_FOR_HUMAN", "HUMAN_DECLINED", "PAUSED", "RESUME_REQUESTED"}
        elif args.action == "status":
            from relay.work_run import status as run_status
            output, ok = run_status(args.work_id, graph_path=args.project_graph), True
        elif args.action == "explore":
            from relay.native_catalog import select
            output = explore(args.root, args.question, focus=args.focus, custody=args.custody,
                             objective=args.objective, api_base=args.api_base, timeout=args.timeout,
                             sources_only=args.sources_only, index_state=args.index_state, query=args.query,
                             native_deployment=None if args.sources_only else select(args.deployment, args.native_deployment))
            ok = output["status"] in {"COMPLETED", "SOURCES_READY"}
        elif args.action == "evidence":
            output = evidence(args.root, args.path, args.start, args.end, revision=args.revision, custody=args.custody)
            ok = True
        elif args.action == "research":
            from relay import research as study_owner
            jobs = args.custody / "research"
            if args.result and args.ref:
                output, ok = study_owner.read_ref(jobs, args.result, args.ref, args.start, args.end), True
            elif args.result:
                output = json.loads((study_owner.job_dir(jobs, args.result) / "result.json").read_bytes())
                output, ok = study_owner.view(output), output["status"] in {"COMPLETED", "COLLECTED"}
            else:
                if not args.question:
                    raise ValueError("research requires --question, --seed and --domain")
                from relay.native_catalog import select
                output = study_owner.view(study_owner.research(jobs, args.question, args.seed, domains=args.domain,
                    max_pages=args.max_pages, max_depth=args.max_depth, collect_only=args.collect_only,
                    config={"timeout": args.timeout, **({"api_base": args.api_base} if args.api_base else {})},
                    lock_path=args.custody / "explorer.lock",
                    native_deployment=None if args.collect_only else select(args.deployment, args.native_deployment),
                    max_model_calls=args.max_model_calls))
                ok = output["status"] in {"COMPLETED", "COLLECTED"}
        elif args.action == "doctor":
            if not args.native and (args.deployment or args.native_deployment or args.receipt):
                raise ValueError('native_diagnostic_options_require_--native')
            if args.native:
                from relay.native_installation import doctor as native_doctor
                output = native_doctor(deployment=args.native_deployment,
                                       deployment_name=args.deployment, receipt_path=args.receipt,
                                       root=args.root.resolve())
            else:
                output = doctor(root=args.root.resolve(), graph=args.graph)
            ok = output["operational_health"] == "CHECKS_PASSED"
        elif args.action == "context":
            output = context(graph=args.graph, work_id=args.work, max_bytes=args.max_bytes)
            ok = output["status"] == "READY_CONTEXT"
        else:
            output = maintain(args.root.resolve(), apply=args.apply)
            ok = output["status"] != "REPAIR_FAILED"
        present(output)
        return 0 if ok else 1
    except (ImportError, OSError, ValueError, TypeError, KeyError) as error:
        output = {"status": "UNAVAILABLE", "error": str(error)[:240], "authority": "NONE"}
        if output_format == 'text':
            print('Status: UNAVAILABLE\nReason: ' + output['error'])
        else:
            print(json.dumps(output))
        return 2


if __name__ == "__main__":
    # Match the installed tare/tare-ops entry, including --os-root and settings.
    from relay.native_installation import main as installed_main
    raise SystemExit(installed_main())
