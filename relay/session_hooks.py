"""Opt-in lifecycle composition. No model calls, implicit repair or Work authority."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

from relay.audit_cases import AuditCases, bounded, decode, json_bytes, require
from relay.relay_io import write_text_atomic


def dispatch(vendor, payload, config, *, event_hint=None):
    from compute_plane.session_hooks import adapt_session, render_session
    from tare_ops import context

    # In a multi-workspace bridge the configuration, not list order, selects
    # the one owner for this invocation. The codec itself stays single-root.
    if vendor == "agy" and isinstance(payload, dict) and isinstance(payload.get("workspacePaths"), list) and len(payload["workspacePaths"]) > 1:
        roots = payload["workspacePaths"]
        selected = Path(config["root"]).resolve()
        require(len(roots) <= 4 and all(isinstance(value, str) and Path(value).is_absolute() for value in roots),
                "invalid_explicit_workspace_selection")
        require(selected in {Path(value).resolve() for value in roots}, "session_workspace_mismatch")
        payload = dict(payload, workspacePaths=[str(selected)])
    event = adapt_session(vendor, payload, event_hint=event_hint)
    require(set(config) <= {"root", "graph", "work", "library_manifest", "audit_store",
                            "audit_case", "receipt", "max_bytes", "project_context",
                            "context_receipt"}, "unknown_session_setting")
    root = Path(config["root"])
    require(root.is_absolute() and root.is_dir(), "absolute_configured_root_required")
    require(Path(event.root).is_absolute() and Path(event.root).resolve() == root.resolve(),
            "session_workspace_mismatch")
    for key in ("graph", "library_manifest", "audit_store", "receipt", "context_receipt"):
        if key in config:
            require(isinstance(config[key], str) and Path(config[key]).is_absolute(),
                    "absolute_setting_required:" + key)
    limit = config.get("max_bytes", 8192)
    require(type(limit) is int and 1024 <= limit <= 16384, "invalid_session_budget")
    require(bool(config.get("audit_store")) == bool(config.get("audit_case")), "incomplete_audit_selection")
    receipt = Path(config["receipt"]) if config.get("receipt") else None
    if receipt:
        require(not receipt.resolve().is_relative_to(root.resolve()), "receipt_inside_candidate")
        require(not receipt.is_symlink(), "receipt_symlink")
    require(bool(config.get("graph")) == bool(config.get("work")), "explicit_work_and_graph_required")
    context_receipt = Path(config["context_receipt"]) if config.get("context_receipt") else None
    if context_receipt:
        require(not context_receipt.resolve().is_relative_to(root.resolve()), "context_receipt_inside_candidate")
        require(not context_receipt.is_symlink(), "context_receipt_symlink")
    report = {"phase": event.phase, "session_id": event.session_id, "resumed": event.resumed,
              "authority": "NONE", "work": None, "audit": None}
    if config.get("project_context") is not None:
        from specgraph.hook_context import context_hint
        settings = config["project_context"]
        require(isinstance(settings, dict) and set(settings) <= {
            "role", "files", "query", "spec_id", "symbol_id", "source_ref", "timeout_ms", "automatic_roles"}, "invalid_project_context_setting")
        require(isinstance(settings.get("files", []), list), "project_context_files_must_be_list")
        automatic = settings.get("automatic_roles", ["planner", "implementer", "auditor", "explorer"])
        require(isinstance(automatic, list) and len(automatic) <= 4 and all(
            isinstance(role, str) and role in {"planner", "implementer", "auditor", "explorer"} for role in automatic),
            "invalid_automatic_roles")
        role = payload.get("agent_role", settings.get("role", "implementer"))
        require(isinstance(role, str) and role in {"planner", "implementer", "auditor", "explorer"}, "invalid_project_role")
        demand = settings.get("query") or payload.get("spec_id") or payload.get("symbol_id")
        if role not in automatic and not demand:
            report["project"] = {"status": "SILENT", "reason": "ON_DEMAND_POLICY", "role": role, "authority": "NONE"}
        else:
            report["project"] = context_hint(
                root, role=role, event=event.phase,
                files=tuple(settings.get("files", [])), query=settings.get("query"),
                spec_id=payload.get("spec_id", settings.get("spec_id")), symbol_id=payload.get("symbol_id", settings.get("symbol_id")), work_id=config.get("work"),
                source_ref=settings.get("source_ref"), max_bytes=max(512, limit - 512),
                timeout_ms=settings.get("timeout_ms", 50))
    if config.get("work"):
        # Recompute on resume; never reuse a stale context snapshot or infer a Work.
        report["work"] = context(graph=Path(config["graph"]), work_id=config["work"],
            manifest=Path(config["library_manifest"]) if config.get("library_manifest") else None,
            max_bytes=limit)
    if config.get("audit_case"):
        cases = AuditCases(Path(config["audit_store"]), Path(__file__).with_name("round_table_roster.json"))
        state = cases.load(config["audit_case"])
        if config.get("work"):
            nodes = (state["anchor"].get("work_ids", []) if state["schema"] == "tare.tools/audit-case/2"
                     else state["attempts"][-1]["request"]["scope"]["nodes"])
            require(config["work"] in nodes,
                    "audit_work_mismatch")
        report["audit"] = cases.summary(state)
    report["status"] = "OBSERVED"
    prior_context = {}
    if context_receipt and event.phase == "start":
        try:
            prior_context = decode(bounded(context_receipt, 16384)) if context_receipt.exists() else {}
            if not isinstance(prior_context, dict):
                raise ValueError("receipt must be an object")
        except (OSError, ValueError, UnicodeError):
            # An advisory dedup receipt cannot make otherwise usable mandatory
            # context disappear. Name the invalid reference, then renew only
            # this explicitly configured external observation cache.
            report["receipt_warning"] = {"status": "UNAVAILABLE", "reference": str(context_receipt),
                                         "reason": "invalid_context_receipt"}
            prior_context = {}
    # Refuse oversized mandatory context instead of silently truncating criteria.
    raw = json_bytes(report)
    require(len(raw) <= limit, "session_context_over_budget")
    rendered = render_session(event, raw.decode())
    if report.get("project", {}).get("reason") == "ON_DEMAND_POLICY" and not report["work"] and not report["audit"]:
        rendered = {}
    if context_receipt and event.phase == "start":
        semantic = dict(report)
        semantic.pop("receipt_warning", None)
        project = report.get("project")
        if isinstance(project, dict) and project.get("digest"):
            # SpecGraph's digest captures selected mandatory context. Its live
            # observation age/heartbeat must not cause repeated session banners.
            semantic["project"] = {"digest": project["digest"], "status": project.get("status")}
        digest = hashlib.sha256(json_bytes(semantic)).hexdigest()
        agent_id = payload.get("agent_id")
        agent_id = agent_id if isinstance(agent_id, str) and len(agent_id) <= 256 else None
        role = project.get("role") if isinstance(project, dict) else None
        identity = hashlib.sha256(json_bytes([str(root.resolve()), vendor, event.session_id, agent_id, role, event.resumed])).hexdigest()
        entries = prior_context.get("entries", [])
        entries = [entry for entry in entries if isinstance(entry, dict)
                   and isinstance(entry.get("identity"), str) and isinstance(entry.get("digest"), str)][:32] if isinstance(entries, list) else []
        if any(entry["identity"] == identity and entry["digest"] == digest for entry in entries):
            rendered = {}  # Complete mandatory report is unchanged, not truncated.
        else:
            entries = [entry for entry in entries if entry["identity"] != identity]
            entries.append({"identity": identity, "digest": digest})
            try:
                context_receipt.parent.mkdir(parents=True, exist_ok=True)
                write_text_atomic(context_receipt, json.dumps({"schema": "tare.tools/session-context-receipts/1",
                    "entries": entries[-32:], "authority": "NONE"}))
            except OSError:
                pass  # No receipt persistence is allowed to block session start.
    require(len(json_bytes(rendered)) <= 65536, "native_output_over_budget")
    if receipt and event.phase in {"stop", "end"}:
        # One explicitly configured latest observation, not a session database.
        # Compare bytes for duplicate callbacks; no append-only heartbeat ritual.
        if not receipt.exists() or bounded(receipt, 16384) != raw:
            write_text_atomic(receipt, raw.decode("utf-8"))
    if config.get("project_context") is not None:
        from compute_plane.hook_observations import record_hook_observation
        project = report.get("project", {})
        record_hook_observation(root, host=vendor, event=event.phase, tool="session_" + event.phase,
            native_tool=payload.get("hook_event_name", "explicit_" + str(event_hint)),
            session_id=event.session_id, agent_id=payload.get("agent_id"), role=project.get("role", "implementer"),
            outcome="CONTEXT_RETURNED" if rendered else "SILENT", snapshot=project.get("snapshot"),
            digest=project.get("digest"), output_bytes=len(json_bytes(rendered)), reason=project.get("reason"))
    return rendered, report


def main(argv=None):
    class HookParser(argparse.ArgumentParser):
        def error(self, message):
            raise ValueError(message)

    parser = HookParser(description=__doc__)
    parser.add_argument("vendor", choices=["claude", "codex", "agy", "aider"])
    parser.add_argument("--session-config", required=True, type=Path)
    parser.add_argument("--event", choices=["start", "stop", "end", "pre", "post"])
    if any(arg in {"-h", "--help"} for arg in (sys.argv[1:] if argv is None else argv)):
        parser.parse_args(argv)
    lifecycle = False
    try:
        # Classify actual native lifecycle input before argv errors can exit 2.
        raw = sys.stdin.buffer.read(65537)
        try:
            native = decode(raw) if len(raw) <= 65536 else None
            lifecycle = isinstance(native, dict) and native.get("hook_event_name") in {
                "SessionStart", "Stop", "SessionEnd"}
        except (ValueError, UnicodeDecodeError, RecursionError):
            pass
        args = parser.parse_args(argv)
        if args.event is not None and args.vendor != "agy":
            parser.error("--event is only for the explicit Antigravity bridge")
        lifecycle = lifecycle or args.vendor == "agy" and args.event in {"start", "stop", "end"}
        try:
            require(len(raw) <= 65536, "session_payload_over_budget")
            payload = decode(raw)
        except (ValueError, UnicodeDecodeError, RecursionError):
            if args.event in {"start", "stop", "end"}:
                raise
            from compute_plane.hook_cli import dispatch as tool_dispatch
            print(json.dumps(tool_dispatch(args.vendor, raw, event_hint=args.event), ensure_ascii=True))
            return 0
        lifecycle = lifecycle or isinstance(payload, dict) and payload.get("hook_event_name") in {
            "SessionStart", "Stop", "SessionEnd"}
        # The same configured entrypoint keeps delegating tool policy to Kernel.
        if args.event in {"pre", "post"} or args.event is None and (
            not isinstance(payload, dict)
            or payload.get("hook_event_name") not in {"SessionStart", "Stop", "SessionEnd"}
        ):
            from compute_plane.hook_cli import dispatch as tool_dispatch
            print(json.dumps(tool_dispatch(args.vendor, raw, event_hint=args.event), ensure_ascii=True))
            return 0
        rendered, report = dispatch(args.vendor, payload,
            decode(bounded(args.session_config, 16384)), event_hint=args.event)
        print(json.dumps(rendered, ensure_ascii=True))
        if report["phase"] != "start":
            print("tare-ops: lifecycle observed; no stop gate or audit launched.", file=sys.stderr)
        return 0
    except (ImportError, OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        # Advisory failure must not trap a user inside a session.
        if lifecycle:
            print("{}")
        print("tare-ops lifecycle unavailable: " + type(error).__name__ + ": " + str(error)[:160],
              file=sys.stderr)
        return 0 if lifecycle else 2


if __name__ == "__main__":
    raise SystemExit(main())
