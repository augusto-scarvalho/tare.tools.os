"""SPEC-114 gated panel actions (the ONLY panel write path).

MF split out of ui_panel.py (self-lib-file-budget finding, 2026-07-13):
the allowlisted ACTIONS registry, the N4/N5 recovery/discard/
composer-create gates, and run_action — every mutation is an
allowlisted harness subcommand behind browser confirm + HITL backstop.
ui_panel re-exports every public name, so callers and tests are unchanged.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

try:
    from . import experiment_registry, graph_providers, intake_queue, tasks_store
    from .async_state import workflow_process_alive
    from .chat_engines import classify_command
    from .common import truncate_text
    from .processes import run_quiet
    from .ui_composer import _import_harness, compile_candidate
except ImportError:  # run directly
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from harness_lib import experiment_registry, graph_providers, intake_queue, tasks_store
    from harness_lib.async_state import workflow_process_alive
    from harness_lib.chat_engines import classify_command
    from harness_lib.common import truncate_text
    from harness_lib.processes import run_quiet
    from harness_lib.ui_composer import _import_harness, compile_candidate

ACTION_TIMEOUT = 900  # matches the gate/harness worst-case; every action is bounded
OUTPUT_CAP = 8000
GATE_ALLOW = {"smoke", "spec-pack", "scenarios"}
_WORKER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")  # sha256 hex: a browser expectedDigest is a trust boundary
_KEY_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{2,63}")  # S2: env-var-shaped key name, a trust boundary

# -- gated actions (single write path = allowlisted harness subcommands) ------

def _refuse(action: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "refused": reason, "action": action, **extra}


# Gate-in-flight guard (panel × SPEC-137, owner incident 2026-07-16): the scenarios
# gate snapshot/restores .harness/workflows/active PER SCENARIO
# (scenario_isolation.volatile_targets), so a workflow mutation clicked mid-gate is
# silently UNDONE by the next restore — a discarded WF resurrects minutes later.
# Cmdline-regex twin of tools/hooks/subagent_gate_wait.py (that hook stays
# import-isolated by contract; keep the two in sync). Fail-OPEN: a broken process
# listing must never brick the panel — the CLI keeps the unguarded escape hatch.
_GATE_CMD_RE = re.compile(r"spec_test_gate\.py|harness\.py[^\r\n]*\bvalidate\b|harness-test\.py",
                          re.IGNORECASE)


def _gate_in_flight() -> bool:
    if os.environ.get("HARNESS_SCENARIO_ISOLATED") == "1":
        return False  # inside a gate scenario the snapshot/restore IS the contract
    try:
        if sys.platform == "win32":
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
                 "Select-Object -ExpandProperty CommandLine"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
        else:
            proc = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
        return any(_GATE_CMD_RE.search(ln) for ln in (proc.stdout or "").splitlines())
    except Exception:
        return False


def _card_flags(p: dict[str, Any]) -> list[str]:
    """Optional `models add|set` flags from a card param dict (SPEC-115)."""
    out: list[str] = []
    for key, flag in (("name", "--name"), ("provider", "--provider"),
                      ("defaultReasoning", "--default-reasoning"), ("description", "--description")):
        if p.get(key):
            out += [flag, str(p[key])]
    for level in p.get("reasoning") or []:
        out += ["--reasoning", str(level)]
    if p.get("contextWindow"):
        out += ["--context-window", str(p["contextWindow"])]
    if p.get("default"):
        out += ["--default"]
    return out


def _fallback_flags(p: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for fb in p.get("fallbacks") or []:
        out += ["--fallback", str(fb)]
    return out


# -- N4 recovery console (K5): approval-as-record gate for the workflow recovery
# ACTIONS. Every id/verb a build lambda turns into argv must reference REAL state,
# never an arbitrary browser string — the "compiler is the trust boundary" rule
# applied to recovery. scrub (the sole irreversible verb) and every --force* variant
# stay CLI-only: they are simply absent from ACTIONS, and this gate makes it
# impossible for a recovery action to smuggle one in through wfid/workerId/status.
_WORKER_STATUSES = {"pending", "queued", "running", "done", "partial",
                    "blocked", "failed", "missing", "skipped"}  # == workflow_mark's set


def _active_wfid(root: Path, wfid: Any) -> bool:
    """True iff wfid has the `WF-` id shape AND names a live directory under
    .harness/workflows/active/. A recovery action may only reference a workflow that
    actually exists in state (approval-as-record); an unknown/foreign id is refused
    before it can reach argv. The id regex also forecloses path traversal and argv
    flag-injection (no leading '-', no path separators) — mirrors worker_detail's gate
    and adds the existence check."""
    wfid = str(wfid)
    if not (_WORKER_ID_RE.match(wfid) and wfid.startswith("WF-")):
        return False
    return (Path(root) / ".harness" / "workflows" / "active" / wfid).is_dir()


def _recovery_reason(root: Path, params: dict[str, Any]) -> str | None:
    """Refusal reason for a recovery action whose params are unsafe to build into
    argv, or None when they are safe. Runs in run_action BEFORE the build lambda for
    any spec marked recovery=True, so a foreign/unknown id — or a `--force`-shaped
    workerId/status injection — never reaches a subcommand. Required-param presence is
    left to the build lambda (a missing key raises → the generic missing-param refusal)."""
    if not _active_wfid(root, params.get("wfid")):
        return f"unknown or invalid workflow id: {params.get('wfid')!r} (must exist under active/)"
    if "workerId" in params and not _WORKER_ID_RE.match(str(params.get("workerId"))):
        return f"invalid worker id: {params.get('workerId')!r}"
    if "status" in params and str(params.get("status")) not in _WORKER_STATUSES:
        return f"invalid worker status: {params.get('status')!r}"
    return None


def _pid_from(path: Path) -> int | None:
    """PID recorded in a run-lock / supervisor.pid file: a JSON `{"pid": N}` lock or a
    bare integer (supervisor.pid is `str(pid)\\n`). None when absent/empty/garbage."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        return int(json.loads(text).get("pid")) if text.startswith("{") else int(text.splitlines()[0])
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return None


def _wf_running(base: Path) -> bool:
    """True iff the workflow at `base` has a LIVE supervising process: an async
    supervisor.pid, or a run lock (.run.lock — the real blocking-run lock — or the
    workflow.lock the fixtures use) whose recorded PID is still alive. These are the same
    signals scrub/cancel key off; liveness is the cross-platform, zombie-safe
    workflow_process_alive (NOT os.kill, which TerminateProcess-es on Windows). A stopped /
    planned WF carries none of these.
    ponytail: PID-liveness only, no lock-age staleness — a crashed run whose PID was
    recycled false-refuses (safe: CLI `workflow scrub --force` is the escape)."""
    for rel in ("async/supervisor.pid", ".run.lock", "workflow.lock"):
        if workflow_process_alive(_pid_from(base / rel)):
            return True
    return False


def _discard_reason(root: Path, params: dict[str, Any]) -> str | None:
    """Refusal reason for a workflow-discard, or None when it is safe to scrub. Runs in
    run_action BEFORE the scrub argv is built (mirrors _recovery_reason): (1) the wfid must
    name a real active/ workflow — a foreign/unknown id is refused before any subprocess;
    (2) a RUNNING workflow is refused — scrub of live work stays CLI-only (N4). Only a
    STOPPED WF (planned / settled / failed / cancelled, no live PID) may be discarded here."""
    wfid = params.get("wfid")
    if not _active_wfid(root, wfid):
        return f"unknown or invalid workflow id: {wfid!r} (must exist under active/)"
    if _wf_running(Path(root) / ".harness" / "workflows" / "active" / str(wfid)):
        return "workflow running — cancel it before discarding"
    return None


def _task_reason(root: Path, params: dict[str, Any]) -> str | None:
    """Refusal reason for a task-modal action, or None when safe (approval-as-
    record, mirrors _decide_reason): the id must name a REAL store task — never
    an arbitrary browser string — and every free-ish param is shape-constrained
    so a --force/--approve-writes-shaped value can never reach argv. The
    fromRun name must be a real task-refine proposal under .harness/runs/."""
    tid = str(params.get("id"))
    if not _WORKER_ID_RE.match(tid):
        return f"invalid task id: {params.get('id')!r}"
    if tid not in {str(t.get("id")) for t in tasks_store.load(root).get("tasks", [])}:
        return f"unknown task id: {tid!r} (must exist in the store)"
    for key in ("model", "effort"):
        if params.get(key) is not None and not _WORKER_ID_RE.match(str(params[key])):
            return f"invalid {key}: {params[key]!r}"
    if params.get("executor") is not None and str(params["executor"]) not in ("claude", "codex"):
        return f"invalid executor: {params['executor']!r} (claude|codex)"
    if "fromRun" in params:
        name = str(params.get("fromRun"))
        if not re.match(r"^task-refine-[A-Za-z0-9._-]+\.plan\.md$", name):
            return f"invalid proposal file name: {name!r}"
        if not (Path(root) / ".harness" / "runs" / name).is_file():
            return f"proposal file not found: {name!r}"
    return None


def _experiment_reason(root: Path, params: dict[str, Any]) -> str | None:
    """Refusal reason for an experiment-card action, or None when safe (mirrors
    _task_reason): the id must name a REAL registry entry and every free-ish
    param is shape-constrained BEFORE any argv is built. The fromRun name must
    be a real experiment-refine proposal under .harness/runs/."""
    eid = str(params.get("id"))
    if not _WORKER_ID_RE.match(eid):
        return f"invalid experiment id: {params.get('id')!r}"
    if eid not in {str(e.get("id")) for e in experiment_registry.experiments(root)}:
        return f"unknown experiment id: {eid!r} (must exist in the registry)"
    for key in ("model", "effort"):
        if params.get(key) is not None and not _WORKER_ID_RE.match(str(params[key])):
            return f"invalid {key}: {params[key]!r}"
    if params.get("executor") is not None and str(params["executor"]) not in ("claude", "codex"):
        return f"invalid executor: {params['executor']!r} (claude|codex)"
    if "decision" in params and str(params["decision"]) not in experiment_registry.VERDICTS:
        return f"invalid verdict {params['decision']!r} (allowed: {', '.join(experiment_registry.VERDICTS)})"
    if params.get("grade") is not None and str(params["grade"]) not in ("1", "2", "3", "4"):
        return f"invalid evidence grade {params['grade']!r} (App H: 1-4)"
    if "fromRun" in params:
        name = str(params.get("fromRun"))
        if not re.match(r"^experiment-refine-[A-Za-z0-9._-]+\.plan\.md$", name):
            return f"invalid proposal file name: {name!r}"
        if not (Path(root) / ".harness" / "runs" / name).is_file():
            return f"proposal file not found: {name!r}"
    return None


def _decide_reason(root: Path, params: dict[str, Any]) -> str | None:
    """Refusal reason for an intake-decide action, or None when it is safe to build.
    Runs in run_action BEFORE the argv build (mirrors _recovery_reason): approval-as-
    record — the id must name a CURRENTLY-PENDING intake entry (REAL state, never an
    arbitrary browser string) and the choice must be one of intake_queue.DECISIONS. The
    id shape check forecloses argv flag-injection / path traversal (no leading '-', no
    separators). Owner-typed --approve-writes / --force tokens can never appear: `choice`
    is constrained to the intake decision vocab, `id` to a real pending entry (SPEC-145
    invariant 1) — this is the single allowlisted panel write path for a decision."""
    iid = str(params.get("id"))
    if not _WORKER_ID_RE.match(iid):
        return f"invalid intake id: {params.get('id')!r}"
    choice = str(params.get("choice"))
    if choice not in intake_queue.DECISIONS:
        return f"invalid decision {choice!r} (allowed: {', '.join(intake_queue.DECISIONS)})"
    # expectedDigest (when present) enters argv as `decide --expected-digest`: a trust boundary.
    # It MUST be a bare sha256 hex (the row digest shape) or it is refused before the argv build.
    digest = params.get("expectedDigest")
    if digest is not None and not _DIGEST_RE.fullmatch(str(digest)):
        return f"invalid expected digest {digest!r} (must be 64 lowercase hex)"
    if iid not in {str(e.get("id")) for e in intake_queue.pending(root)}:
        return f"unknown or already-decided intake id: {iid!r}"
    return None


# The acceptance vocabulary, closed and server-owned: drive the vendor's prompt, open
# it for the owner to answer by hand, or leave the hooks inert. `visible` is the same
# ACTION as `accept` with a different choice — never a second write path.
_ACCEPTANCE_CHOICES = ("accept", "visible", "keep")


def _acceptance_reason(root: Path, params: dict[str, Any]) -> str | None:
    """Refusal reason for an acceptance-decide action, or None when safe to build
    (SPEC-172 inv. 18; mirrors _decide_reason). This is the panel's only path to an
    action that TYPES INTO A VENDOR'S TUI, so the trust boundary matters more here
    than anywhere else in the Decisions card.

    What the browser may say is deliberately tiny: an id that must name a
    CURRENTLY-PENDING acceptance row (real state, never an arbitrary string), a choice
    from a three-word vocabulary, and an optional 64-hex digest. VENDOR and MODE are NOT
    parameters at all -- the vendor is read off the pending row inside apply_decision
    and the mode is fixed there. A validated parameter is still a parameter; the way a
    clicker cannot steer a vendor argv is for the argv not to be theirs to steer.
    """
    aid = str(params.get("id"))
    if not _WORKER_ID_RE.match(aid):
        return f"invalid acceptance id: {params.get('id')!r}"
    choice = str(params.get("choice"))
    if choice not in _ACCEPTANCE_CHOICES:
        return f"invalid decision {choice!r} (allowed: {', '.join(_ACCEPTANCE_CHOICES)})"
    digest = params.get("expectedDigest")
    if digest is not None and not _DIGEST_RE.fullmatch(str(digest)):
        return f"invalid expected digest {digest!r} (must be 64 lowercase hex)"
    from harness_lib import decision_inbox
    live = {str(r["id"]) for r in decision_inbox.pending_decisions(root)
            if r.get("kind") == "acceptance"}
    if aid not in live:
        return (f"unknown or already-clear acceptance id: {aid!r} "
                "(the vendor may have nothing pending, or nothing has probed it yet)")
    return None


def _keys_name_reason(params: dict[str, Any]) -> str | None:
    """Refusal reason for a keys-set/unset whose NAME is unsafe to build into argv, or
    None when safe (S2 trust boundary, mirrors _decide_reason). The key NAME is the ONLY
    keys param that reaches argv; it must fullmatch [A-Z][A-Z0-9_]{2,63} -- an env-var-shaped
    token that forecloses argv flag-injection (no leading '-') and shell metacharacters --
    before any `keys` subcommand runs. The secret VALUE never arrives here: run_action popped
    it from params (S1) before this gate, so a refusal built from these params cannot echo it."""
    name = str(params.get("name"))
    if not _KEY_NAME_RE.fullmatch(name):
        return f"invalid key name: {params.get('name')!r} (must match [A-Z][A-Z0-9_]{{2,63}})"
    return None


_GRAPH_PROVIDER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")  # B3: provider-name shape, a trust boundary


def _graph_provider_reason(root: Path, params: dict[str, Any]) -> str | None:
    """Refusal reason for a graph-provider-set whose NAME is unsafe to build into argv, or
    None when safe (B3 server-side trust boundary, mirrors _keys_name_reason). The provider
    NAME is the only param that reaches `graph set <name>` argv: it must be provider-name-shaped
    (no leading '-' -> no argv flag-injection, no shell metacharacters) AND name a provider in the
    LIVE registry(root) -- the GUI may only SELECT among registered providers, never GUI-create one,
    so a foreign/unknown name is refused BEFORE any subprocess (the same rule set_provider enforces
    server-side, applied one layer earlier)."""
    name = str(params.get("name"))
    if not _GRAPH_PROVIDER_RE.fullmatch(name):
        return f"invalid graph provider name: {params.get('name')!r} (must match [A-Za-z0-9][A-Za-z0-9_.-]{{0,63}})"
    if name not in graph_providers.registry(root):
        return (f"unknown graph provider: {name!r} (external providers are declared in "
                ".harness/project.json knowledgeGraph.providers)")
    return None


_ROUTE_DEMAND_MAX = 2000  # a textarea demand; keeps argv well under the OS cap


def _route_triage_reason(params: dict[str, Any]) -> str | None:
    """Refusal reason for route-triage (GUI-RG3), or None when safe. The demand is the
    ONLY param and it reaches `route <demand> --triage-prompt` as a POSITIONAL argv token
    — a trust boundary: a demand STARTING with '-' would be parsed by argparse as an
    OPTION, smuggling --loop/--dispatch/--verdict/--heartbeat into what MUST stay the
    read-only, non-spawning classify path (cmd_route early-returns on --triage-prompt
    BEFORE any ledger/event write; --loop/--dispatch spawn). Refuse a blank, flag-shaped,
    or over-long demand BEFORE any argv is built."""
    demand = str(params.get("demand") or "").strip()
    if not demand:
        return "route-triage needs a demand string"
    if demand.startswith("-"):
        return ("demand must not start with '-' (a flag-shaped demand could smuggle "
                "--loop/--dispatch into the read-only classify path)")
    if len(demand) > _ROUTE_DEMAND_MAX:
        return f"demand too long (max {_ROUTE_DEMAND_MAX} chars)"
    return None


# Each entry: the argv builder (harness.py tail) plus whether it mutates state.
# rerun-gate is special-cased (it runs harness-test.py, not harness.py). SPEC-115
# adds the model-card + routing actions — every mutating one needs a browser
# confirm and inherits the human-only (--approval-token/--send) classify backstop.
ACTIONS: dict[str, dict[str, Any]] = {
    # esc-scoped-hitl-view: the panel ALWAYS forwards the card's subject, so a
    # cross-subject resolve is refused server-side (scoped_resolve_check); the
    # bare CLI keeps the unscoped operator escape hatch.
    "resolve-escalation": {"mutating": True,
                           "build": lambda p: ["escalations", "--resolve", str(p["id"]),
                                               *(["--target", str(p["subject"])]
                                                 if p.get("subject") else [])]},
    # intake-decide (SPEC-145 Decisions card): record ONE intake triage decision via the
    # single allowlisted write path -- the `decide` VERB (NOT `intake decide`: only `decide`
    # carries --expected-digest, the single TOCTOU check point in apply_decision, SPEC-145
    # C12). decide=True routes it through _decide_reason (id ∈ currently-pending intake, choice
    # ∈ DECISIONS, AND expectedDigest -- when present -- 64-hex) BEFORE the argv is built, so a
    # foreign/already-decided id, an off-vocab choice, or a malformed digest never reaches
    # `decide`. The panel forwards the row digest as --expected-digest (a stale one is REFUSED
    # server-side, never a silent apply) and NEVER --allow-expired (expiry refuses safely).
    # The note defaults to the deciding surface (invariant 4). Escalation decisions reuse the
    # resolve-escalation action above, untouched. Same write target: decide -> apply_decision
    # -> intake_queue.decide, so _STATE_AUTOCOMMIT["intake-decide"] is unchanged.
    "intake-decide": {"mutating": True, "decide": True,
                      "build": lambda p: ["decide", str(p["id"]), str(p["choice"]),
                                          "--note", str(p.get("note") or "decided via panel"),
                                          *(["--expected-digest", str(p["expectedDigest"])]
                                            if p.get("expectedDigest") else [])]},
    # acceptance-decide (SPEC-172 inv. 18): the Decisions card's acceptance row. Same
    # single write path as intake-decide -- the `decide` VERB, so the TOCTOU digest
    # check in apply_decision is the one the panel uses -- but a SMALLER parameter
    # surface: id + choice + digest and nothing else. The note is fixed here rather
    # than forwarded, because this action's argv ends in a vendor process and a
    # browser-supplied `--note` value is one more string to reason about for no gain.
    # Vendor and mode are decided from the pending row inside apply_decision, never here.
    "acceptance-decide": {"mutating": True, "acceptance": True,
                          "build": lambda p: ["decide", str(p["id"]), str(p["choice"]),
                                              "--note", "accepted via panel",
                                              *(["--expected-digest", str(p["expectedDigest"])]
                                                if p.get("expectedDigest") else [])]},
    "rerun-gate": {"mutating": True, "gate": True},
    # SPEC-147 inv 6: workspace editor/explorer file ops — in-process ws_files
    # handlers (no argv: file content would hit the Windows ~32KB argv cap).
    # Confinement + baseSha conflict live in ws_files; confirm/backstop/
    # gate-guard inherited below. M3: create/rename/delete are FILES-only.
    "ws-file-save": {"mutating": True, "ws": True},
    "ws-file-create": {"mutating": True, "ws": True},
    "ws-file-rename": {"mutating": True, "ws": True},
    "ws-file-delete": {"mutating": True, "ws": True},
    "ws-shard-discard": {"mutating": True, "build": lambda p: []},
    # IDE shard Integrate: four in-process stateful steps plus the existing
    # gate-staged/reckon verbs. integrate=True joins the gate-in-flight guard.
    "ws-integrate-apply": {"mutating": True, "integrate": True,
                           "integrateHandler": True, "build": lambda p: []},
    "ws-integrate-gate": {"mutating": True, "integrate": True,
                          "integrateGate": True,
                          "build": lambda p: ["gate-staged", "--json"]},
    "ws-integrate-review": {"mutating": True, "integrate": True,
                            "integrateHandler": True, "build": lambda p: []},
    "ws-integrate-reckon": {"mutating": True, "integrate": True,
                            "build": lambda p: ["reckon", "--record", "--verdict",
                                                str(p["verdict"]), "--note",
                                                str(p.get("note") or "")]},
    "ws-integrate-commit": {"mutating": True, "integrate": True,
                            "integrateHandler": True, "build": lambda p: []},
    "ws-integrate-rollback": {"mutating": True, "integrate": True,
                              "integrateHandler": True, "build": lambda p: []},
    "agents-audit": {"mutating": False, "build": lambda p: ["agents", "audit"]},
    "records-rebuild": {"mutating": True, "build": lambda p: ["records", "rebuild"]},
    "records-search": {"mutating": False,
                       "build": lambda p: ["records", "search", *[str(t) for t in p.get("terms") or []]]},
    "models-list": {"mutating": False, "build": lambda p: ["models", "list"]},
    "models-add": {"mutating": True, "build": lambda p: ["models", "add", str(p["id"]),
                   "--engine", str(p["engine"]), *_card_flags(p)]},
    "models-set": {"mutating": True, "build": lambda p: ["models", "set", str(p["id"]), *_card_flags(p)]},
    "models-remove": {"mutating": True, "build": lambda p: ["models", "remove", str(p["id"]),
                      *(["--force"] if p.get("force") else [])]},
    "models-replace": {"mutating": True, "build": lambda p: ["models", "replace", "--json", str(p["json"])]},
    "routing-set-role": {"mutating": True, "build": lambda p: ["routing", "set-role",
                         "--profile", str(p["profile"]), "--role", str(p["role"]),
                         "--primary", str(p["primary"]), *_fallback_flags(p)]},
    # SPEC-118 v6: per-role contextDiet editor (role dialog "Context diet" section).
    "routing-diet": {"mutating": True, "build": lambda p: ["routing", "diet",
                     "--profile", str(p["profile"]), "--role", str(p["role"]),
                     *(["--clear"] if p.get("clear") else
                       [*(["--keep", str(p["keep"])] if p.get("keep") is not None else []),
                        *(["--user-layer", str(p["userLayer"])] if p.get("userLayer") in ("on", "off") else []),
                        *(["--reinject", str(p["reinject"])] if p.get("reinject") in ("on", "off") else [])])]},
    "routing-profile-save": {"mutating": True, "build": lambda p: ["routing", "profile", "save", str(p["name"])]},
    "routing-profile-use": {"mutating": True, "build": lambda p: ["routing", "profile", "use", str(p["name"])]},
    "routing-profile-delete": {"mutating": True, "build": lambda p: ["routing", "profile", "delete", str(p["name"])]},
    "routing-target-assign": {"mutating": True, "build": lambda p: ["routing", "target", "assign",
                              str(p["target"]), str(p["profile"])]},
    "routing-target-clear": {"mutating": True, "build": lambda p: ["routing", "target", "clear", str(p["target"])]},
    "routing-restore-canonical": {"mutating": True, "build": lambda p: ["routing", "restore-canonical"]},
    "routing-replace": {"mutating": True, "build": lambda p: ["routing", "replace", "--json", str(p["json"])]},
    "targets-add": {"mutating": True, "build": lambda p: ["targets", "add", str(p["name"]), str(p["path"])]},
    # N4 recovery console (SPEC-114 v9): the four workflow recovery verbs as argv tails.
    # Every one is mutating (→ browser confirm) AND recovery (→ _recovery_reason validates
    # wfid against active/ + the workerId/status shape BEFORE the build lambda). scrub and
    # every --force / --force-round / --force-lock variant are deliberately ABSENT — they
    # stay CLI-only (K5). `mark` maps to the real `workflow mark <wf> <worker> <status>`
    # with status constrained to the closed worker-status set by the recovery gate.
    "workflow-retry": {"mutating": True, "recovery": True,
                       "build": lambda p: ["workflow", "retry", str(p["wfid"]), str(p["workerId"])]},
    # queue-cancel-worker (B1): an optional workerId narrows cancel to ONE
    # worker (queued rows too). The recovery gate already regex-validates any
    # present workerId BEFORE this builder runs, so a --force-shaped injection
    # never reaches argv; whole-WF cancel (no workerId) is unchanged.
    "workflow-cancel": {"mutating": True, "recovery": True,
                        "build": lambda p: ["workflow", "cancel", str(p["wfid"]),
                                            *(["--worker-id", str(p["workerId"])]
                                              if p.get("workerId") else [])]},
    # queue-start-action (D039): async, non-blocking `workflow start <wfid>` — the
    # correct verb for a browser click (never the blocking `workflow run`). A PLAIN
    # mutating entry: it sets NO recovery/discard/task/composerCreate flag, so no
    # pre-check gate runs (landmine — those flags carry their own validators). The
    # browser confirm gates it; the SPEC-153 spawn-economy refusal comes back as the
    # action's non-zero output and surfaces verbatim. The --allow-frontier ack stays
    # CLI-only — a browser click must NEVER carry it. --executor is optional passthrough
    # (SPEC-165 R10 resolves executors from routing roles when omitted; the GUI grows no
    # executor picker this slice).
    "workflow-start": {"mutating": True,
                       "build": lambda p: ["workflow", "start", str(p["wfid"]),
                                           *(["--executor", str(p["executor"])] if p.get("executor") else [])]},
    # queue-reorder (D039): reorder the pending async queue from an explicit {order:[wfid...]}
    # list. Plain mutating entry (browser confirm); each wfid is a SEPARATE argv element, and
    # `workflow reorder` re-validates every id server-side (WF-shaped + pending) before it
    # writes queueOrder — a running/settled/unknown id is refused (exit 2).
    "workflow-reorder": {"mutating": True,
                         "build": lambda p: ["workflow", "reorder", *[str(w) for w in (p.get("order") or [])]]},
    "workflow-mark": {"mutating": True, "recovery": True,
                      "build": lambda p: ["workflow", "mark", str(p["wfid"]), str(p["workerId"]), str(p["status"])]},
    "workflow-recover": {"mutating": True, "recovery": True,
                         "build": lambda p: ["workflow", "async-recover", str(p["wfid"])]},
    # Panel discard (SPEC-114 amendment): remove a STOPPED workflow the composer created but
    # nobody wants — the create→discard gap the owner hit. N2b materializes a `planned` WF
    # (worker cards on the board); `cancel` only signals a RUNNING async task's PIDs, so a
    # never-started WF has none → cancel is a no-op and the cards stay. discard=True routes it
    # through _discard_reason (wfid ∈ active/ AND not-running) BEFORE the argv is built. It is
    # `workflow scrub <wfid>` with NO --force: bare scrub itself refuses a locked/running WF
    # or a non-scrub-safe phase at the CLI too (belt-and-suspenders). scrub --force stays CLI-only.
    "workflow-discard": {"mutating": True, "discard": True,
                         "build": lambda p: ["workflow", "scrub", str(p["wfid"])]},
    # N2b composer-create (SPEC-120 v2): the deferred "Create workflow" step. CREATE-ONLY —
    # `workflow plan` materializes a WF dir + prompts from the composed branch OBJECTS (via
    # --branch-json, SPEC-119 v7); it spawns NO agent and NEVER starts (no start/async verb
    # in the argv). composerCreate=True routes it through the compile-must-be-valid pre-check
    # in run_action (a valid compile is required before any subprocess: the same closed-vocab
    # + secret-scan + budget trust boundary as Compile). Mutating → browser confirm + the
    # classify_command human-only backstop.
    "composer-create": {"mutating": True, "composerCreate": True,
                        "build": lambda p: ["workflow", "plan", "--profile", str(p["profile"]),
                                            "--branch-json", json.dumps(p["branches"] or []),
                                            *(["--task", str(p["task"])] if p.get("task") else [])]},
    # research-run-form (GUI-RS4): the Phase-0 scaffold form on the legacy Research view.
    # composer-create CANNOT create a research round from the form: research-* profiles ship
    # predefined branches (empty browser branches) AND widthDeclarationRequired (D010), and
    # compile_candidate cannot forward a width declaration -- so it hard-refuses them (planDeviation
    # rf-compile-empty-branches). research-plan is the minimal create-only twin: `workflow plan`
    # with the profile's own branches + the form's DECLARED width (--width-mode/--width-why). Its
    # researchPlan gate re-runs the width-aware N1 compile IN-PROCESS before any argv (same
    # closed-vocab + secret-scan + budget trust boundary as composer-create). No start verb: the
    # owner starts the round deliberately from the Queue screen (workflow-start).
    "research-plan": {"mutating": True, "researchPlan": True,
                      "build": lambda p: ["workflow", "plan", "--profile", str(p["profile"]),
                                          *(["--task", str(p["task"])] if p.get("task") else []),
                                          "--width-mode", str(p["widthMode"]),
                                          "--width-why", str(p["widthWhy"])]},
    # Task-modal actions (tasks screen): every one is task=True (_task_reason
    # validates the id against the REAL store + shape-constrains model/effort/
    # executor/fromRun BEFORE any argv is built). All writes go through the
    # `tasks` CLI verb — the store's single write path. task-refine is a bounded
    # READ-ONLY model run (writes only a runs/ proposal file); it is marked
    # mutating so the browser confirm gates the model spend.
    "task-note": {"mutating": True, "task": True,
                  "build": lambda p: ["tasks", "note", str(p["id"]), "--text", str(p["text"])]},
    "task-refine": {"mutating": True, "task": True,
                    "build": lambda p: ["tasks", "refine", str(p["id"]),
                                        *(["--model", str(p["model"])] if p.get("model") else []),
                                        *(["--effort", str(p["effort"])] if p.get("effort") else []),
                                        *(["--executor", str(p["executor"])] if p.get("executor") else [])]},
    "task-plan-append": {"mutating": True, "task": True,
                         "build": lambda p: ["tasks", "plan", str(p["id"]),
                                             "--from-run", str(p["fromRun"])]},
    "task-dispatch": {"mutating": True, "task": True,
                      "build": lambda p: ["tasks", "dispatch", str(p["id"]),
                                          *(["--executor", str(p["executor"])] if p.get("executor") else [])]},
    "task-close": {"mutating": True, "task": True,
                   "build": lambda p: ["tasks", "close", str(p["id"])]},
    "task-delete": {"mutating": True, "task": True,
                    "build": lambda p: ["tasks", "delete", str(p["id"])]},
    # Experiment-card actions (experiments screen, tasks-modal parity): every one
    # is experiment=True (_experiment_reason validates the id against the REAL
    # registry + shape-constrains model/effort/executor/decision/fromRun BEFORE
    # any argv is built). All writes go through the `experiment` CLI verb — the
    # registry's single write path. experiment-refine is a bounded READ-ONLY
    # model run (writes only a runs/ proposal file); marked mutating so the
    # browser confirm gates the model spend. Start-experiment is NOT a panel
    # action: the button opens a porteiro chat (SPEC-146); the detached AFK
    # path is the CLI verb `experiment dispatch` only.
    "experiment-activate": {"mutating": True, "experiment": True,
                            "build": lambda p: ["experiment", "activate", str(p["id"])]},
    "experiment-verdict": {"mutating": True, "experiment": True,
                           "build": lambda p: ["experiment", "verdict", str(p["id"]), str(p["decision"]),
                                               *(["--grade", str(p["grade"])] if p.get("grade") else []),
                                               *(["--evidence", str(p["evidence"])] if p.get("evidence") else []),
                                               *(["--reopen-trigger", str(p["reopen"])] if p.get("reopen") else [])]},
    "experiment-refine": {"mutating": True, "experiment": True,
                          "build": lambda p: ["experiment", "refine", str(p["id"]),
                                              *(["--model", str(p["model"])] if p.get("model") else []),
                                              *(["--effort", str(p["effort"])] if p.get("effort") else []),
                                              *(["--executor", str(p["executor"])] if p.get("executor") else [])]},
    "experiment-plan-append": {"mutating": True, "experiment": True,
                               "build": lambda p: ["experiment", "plan", str(p["id"]),
                                                   "--from-run", str(p["fromRun"])]},
    # config-keys vault (config-keys-gui, owner decision #2 2026-07-13): the GUI half of the
    # `keys` CLI. keys-set streams the secret VALUE via stdinParam (popped from params before
    # any serialization, S1; never argv) and shares the S2 name guard (keysName) with keys-unset
    # -- the key NAME is the only keys param that reaches argv. keys-migrate was REMOVED with the
    # .env tier (keys-keyring v2). No _STATE_AUTOCOMMIT key: the vault is OS-level, so there is
    # nothing in the tree to commit (landmine -- do NOT autocommit a keys write).
    "keys-set": {"mutating": True, "keysName": True, "stdinParam": "value",
                 "build": lambda p: ["keys", "set", str(p["name"])]},
    "keys-unset": {"mutating": True, "keysName": True,
                   "build": lambda p: ["keys", "unset", str(p["name"])]},
    # graphs-screen (B3): the ONLY two graph actions the GUI grows. graph-provider-set WRAPS the
    # EXISTING `graph set <name>` (R2) and is graphProvider=True -> _graph_provider_reason validates
    # the NAME shape + membership in the LIVE registry(root) BEFORE any argv (the GUI SELECTS a
    # registered provider, never creates one). It writes .harness/project.json, so it ALSO joins the
    # gate-in-flight refusal below (a mid-gate project.json write is clobbered by the per-scenario
    # restore) and takes NO _STATE_AUTOCOMMIT (project.json may carry unrelated edits, R4). graph-rebuild
    # WRAPS the EXISTING builtin verb `graph-build-code-ast` (R2) -- a PLAIN mutating entry (browser
    # confirm only): it writes graphify-out/, not project.json, so no pre-argv gate and no in-flight guard.
    "graph-provider-set": {"mutating": True, "graphProvider": True,
                           "build": lambda p: ["graph", "set", str(p["name"])]},
    "graph-rebuild": {"mutating": True, "build": lambda p: ["graph-build-code-ast"]},
    # simulate-route (GUI-RG3): the READ-ONLY, NON-SPAWNING classify surface. `route
    # <demand> --triage-prompt` prints the deterministic floor + the SPEC-144 router
    # triage prompt and RETURNS before any ledger/event write; it never spawns and never
    # runs a live model (Phase 2 is model-gated). mutating:False (no browser confirm,
    # records-search precedent). routeTriage=True routes it through _route_triage_reason
    # (the demand is a POSITIONAL argv token -> a '-'-leading demand is refused before the
    # build so --loop/--dispatch can never be smuggled into this read path).
    "route-triage": {"mutating": False, "routeTriage": True,
                     "build": lambda p: ["route", str(p["demand"]), "--triage-prompt"]},
    # N-VENDORCREDIT: the GUI refresh button. Wraps the EXISTING `fuel probe` verb
    # (R-v3.2 full pass, ~$0). Plain mutating entry: browser confirm + HITL backstop
    # inherited; deliberately NOT in the gate-in-flight set — a probe write reverted
    # by a gate restore is stale gas again (re-runnable), not corruption. No
    # autocommit: the fuel loop's own writes never autocommit either.
    "fuel-probe": {"mutating": True, "build": lambda p: ["fuel", "probe"]},
}


# -- user-origin auto-commit (UX separation) ----------------------------------
# The GUI is the owner's live write path (routing/model-card edits, escalation
# resolves, intake decisions) while an overseer programs. Left uncommitted those
# edits accrete as .harness dirt that the hermetic gate must hold aside every
# run. So a successful MUTATING user action commits ITS OWN declared files on
# their own — a scoped pathspec commit that never sweeps a room's in-flight
# staged work. runtime state (worklog, quality-state, task/workflow) is NOT here.
_ROUTING_STATE = [".harness/routing/model-routing.json", ".harness/routing/model-cards.json"]
_STATE_AUTOCOMMIT: dict[str, list[str]] = {
    "resolve-escalation": [".harness/state/escalations.json"],
    "intake-decide": [".harness/state/intake-queue.json"],
    # task-modal store writes (task-refine writes no state — absent on purpose)
    "task-note": [".harness/state/tasks.json"],
    "task-plan-append": [".harness/state/tasks.json"],
    "task-dispatch": [".harness/state/tasks.json"],
    "task-close": [".harness/state/tasks.json"],
    "task-delete": [".harness/state/tasks.json"],
    # experiment-card store writes (experiment-refine writes no state — absent on purpose)
    "experiment-activate": [".harness/state/experiments.json"],
    "experiment-verdict": [".harness/state/experiments.json"],
    "experiment-plan-append": [".harness/state/experiments.json"],
}


def _autocommit_paths(action: str) -> list[str]:
    """Declared state files for a user-origin action, or [] when it owns none."""
    if action.startswith(("routing-", "models-")):
        return _ROUTING_STATE
    return _STATE_AUTOCOMMIT.get(action, [])


def autocommit_enabled() -> bool:
    """Whether run_action may self-commit a user-origin mutation.

    THE RULE: on iff HARNESS_UI_AUTOCOMMIT=1 is in this process's env. The live
    `harness.py ui` launcher (cmd_ui) seeds it once via `os.environ.setdefault`,
    defaulting to "1" for an interactive owner session (stdout is a TTY) and "0"
    when headless — so a normal GUI launch auto-commits with no manual export, and
    an owner who exports the var explicitly still wins. Test/scenario servers build
    the panel via `harness_ui.make_server` directly (never cmd_ui) and never seed
    it, so the browser e2e (test_panel_e2e) drives the real ROOT without committing.
    ponytail: process-env flag, not a make_server param — the server runs in cmd_ui's
    own process so the seed reaches run_action; thread a bool through make_server only
    if a detached/out-of-process launcher ever needs it."""
    return os.environ.get("HARNESS_UI_AUTOCOMMIT") == "1"


def _git_state(root: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return run_quiet(["git", "-C", str(root), *args], capture_output=True,
                     text=True, encoding="utf-8", errors="replace", timeout=timeout)


def autocommit_state(root: Path, rel_paths: list[str], action: str) -> str | None:
    """Commit ONLY ``rel_paths`` on their own; return the new short sha, else None.

    Best-effort and non-blocking by contract: the underlying action already
    succeeded, so any git trouble (not a repo, nothing changed in scope, a held
    index.lock) is swallowed. The ``-- <paths>`` pathspec on both add and commit
    is load-bearing — an unrelated staged file (a room mid-write) stays staged
    and OUT of this commit. State-only .harness commits fast-pass the stamp hook.
    ponytail: one sleep-retry on index.lock, then skip — the gate tolerates dirt."""
    root = Path(root)
    paths = [p for p in (rel_paths or []) if p]
    if not paths:
        return None
    try:
        status = _git_state(root, "status", "--porcelain", "--", *paths, timeout=30)
        if status.returncode != 0 or not status.stdout.strip():
            return None  # not a git repo, or nothing changed in scope
        for attempt in (1, 2):
            add = _git_state(root, "add", "--", *paths, timeout=30)
            if add.returncode != 0:
                if attempt == 1 and "index.lock" in (add.stderr or ""):
                    time.sleep(0.5); continue
                return None
            commit = _git_state(root, "commit", "-m", f"chore(state): {action} via GUI", "--", *paths)
            if commit.returncode == 0:
                sha = _git_state(root, "rev-parse", "--short", "HEAD", timeout=30)
                return sha.stdout.strip() if sha.returncode == 0 else "committed"
            if attempt == 1 and "index.lock" in (commit.stderr or ""):
                time.sleep(0.5); continue
            return None
    except Exception:
        return None
    return None


_RESEARCH_WIDTH_MODES = {"focused", "exploratory"}  # GUI-RS4: the form's two-option width select


def _research_plan_reason(root: Path, params: dict[str, Any]) -> str | None:
    """GUI-RS4 researchPlan gate -- the composer-create trust boundary, made width-aware.
    research-* profiles carry predefined branches AND widthDeclarationRequired (D010), so
    compile_candidate (no width seam) refuses them with empty browser branches. Re-run the
    N1 compile WITH the form's declared width BEFORE any argv: a non-research/unknown profile,
    an out-of-vocab width mode, a BLANK justification, a token-budget breach, or a secret-shaped
    value all block creation. The non-blank check is load-bearing on its own: the raw compile
    only rejects width_why is None, so an empty-string justification would otherwise slip through
    (no width written = do not start the wave)."""
    profile = str(params.get("profile") or "")
    if not profile.startswith("research-"):
        return f"research-plan is restricted to research-* profiles (got {profile!r})"
    mode = str(params.get("widthMode") or "")
    if mode not in _RESEARCH_WIDTH_MODES:
        return f"width-mode must be one of {sorted(_RESEARCH_WIDTH_MODES)} (got {mode!r})"
    if not str(params.get("widthWhy") or "").strip():
        return "width justification is required (D010): no width written = do not start the wave"
    try:
        report = _import_harness().validate_workflow_plan(
            str(params.get("task") or ""), None, branches=None, profile_name=profile,
            width_mode=mode, width_why=str(params["widthWhy"]))
    except Exception as exc:
        return f"research compile failed: {type(exc).__name__}: {exc}"
    if report.get("valid") is not True:
        return ("research-plan requires a valid compile (unknown profile, token-budget breach, "
                "or secret-shaped value blocks creation): "
                + "; ".join(str(e) for e in (report.get("errors") or ["invalid candidate"])))
    return None


def run_action(root: Path, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one allowlisted action; refuse anything else, HITL, or unconfirmed."""
    params = params or {}
    spec = ACTIONS.get(action)
    if spec is None:
        return _refuse(action, "unknown action")

    # S1: a secret stdin value (keys-set) must NEVER reach argv, a refusal payload, the result
    # echo, events, records, or a log line. Pop it from params NOW -- before any code path that
    # serializes params (this also mutates the caller's dict, so the panel's own post-call
    # logging can never see it) -- and stream it to the subprocess via stdin only (below).
    secret = params.pop(spec["stdinParam"], None) if spec.get("stdinParam") else None
    # S4: an absent/empty stdin value is refused pre-argv (never an interactive hang -- the
    # subprocess always has its stdin closed after the write, subprocess.run(input=...)).
    if spec.get("stdinParam") and not secret:
        return _refuse(action, "value required (read from stdin, never argv)")

    # SPEC-147: ws-file-* verbs run in-process (rerun-gate precedent — no argv).
    # Same discipline, applied before dispatch: browser confirm for mutations and
    # the gate-in-flight guard (a mid-gate save would be clobbered by the
    # per-scenario state restore).
    if spec.get("ws"):
        if spec.get("mutating") and params.get("confirm") is not True:
            return _refuse(action, "confirmation required for a mutating action")
        # A gate restore holds the live tree, not runs/ide-shard: edits can safely
        # continue through the gate without being clobbered.
        from . import ide_shard, ws_files
        try:
            ws_root = ide_shard.ensure(root)
        except ide_shard.IdeShardRefused as exc:
            return _refuse(action, str(exc))
        result = ws_files.run_ws_action(ws_root, action, params)
        if result.get("ok") is True:
            ide_shard.record_write(
                root, action, str(params.get("path") or ""), writer="gui",
                new_path=(str(params.get("newPath")) if action == "ws-file-rename" else None))
        return result

    if spec.get("gate"):
        gate = str(params.get("gate", ""))
        if gate not in GATE_ALLOW:
            return _refuse(action, f"gate not allowed: {gate!r} (allowed: {sorted(GATE_ALLOW)})")
        cmd = [sys.executable, "scripts/harness-test.py", gate, "--no-project-commands"]
    else:
        # N4: for a recovery action, validate the browser-supplied ids against real
        # state (wfid exists under active/; workerId/status well-shaped) BEFORE building
        # any argv — an unknown wfid or a --force-shaped injection is refused here, so no
        # subcommand ever runs on it (approval-as-record; the compiler is the trust boundary).
        if spec.get("recovery"):
            bad = _recovery_reason(root, params)
            if bad is not None:
                return _refuse(action, bad)
        # Discard (SPEC-114 amendment): scrub restricted to NOT-running workflows. Validate the
        # wfid against active/ AND refuse a RUNNING workflow BEFORE building the scrub argv — a
        # foreign id or live work never reaches a subprocess (the load-bearing browser guard;
        # N4's intent preserved — you cannot nuke live work from the browser).
        if spec.get("discard"):
            bad = _discard_reason(root, params)
            if bad is not None:
                return _refuse(action, bad)
        # SPEC-145: an intake-decide must reference a REAL pending intake entry with an
        # in-vocab choice before `intake decide` runs (approval-as-record) — same trust
        # boundary as recovery/discard: validate against real state BEFORE building argv.
        if spec.get("decide"):
            bad = _decide_reason(root, params)
            if bad is not None:
                return _refuse(action, bad)
        # SPEC-172: an acceptance-decide reaches a vendor's TUI. Validate the id
        # against the LIVE pending inbox and the choice against its two-word vocab
        # before any argv exists (vendor/mode are never browser params at all).
        if spec.get("acceptance"):
            bad = _acceptance_reason(root, params)
            if bad is not None:
                return _refuse(action, bad)
        # Task-modal actions: id must name a REAL store task and every optional
        # param is shape-constrained BEFORE the argv build (same trust boundary).
        if spec.get("task"):
            bad = _task_reason(root, params)
            if bad is not None:
                return _refuse(action, bad)
        # Experiment-card actions: same trust boundary against the experiment registry.
        if spec.get("experiment"):
            bad = _experiment_reason(root, params)
            if bad is not None:
                return _refuse(action, bad)
        # config-keys (S2): the keys-set/unset NAME is a trust boundary -- validate its shape
        # BEFORE the argv build (the secret value was already popped above, S1).
        if spec.get("keysName"):
            bad = _keys_name_reason(params)
            if bad is not None:
                return _refuse(action, bad)
        # graphs-screen (B3): graph-provider-set writes knowledgeGraph.provider -- validate the NAME
        # shape + LIVE registry membership BEFORE the `graph set` argv (the GUI selects a REGISTERED
        # provider only; a foreign/metachar name never reaches a subprocess).
        if spec.get("graphProvider"):
            bad = _graph_provider_reason(root, params)
            if bad is not None:
                return _refuse(action, bad)
        # GUI-RG3 route-triage: the demand is a POSITIONAL argv token — refuse a blank /
        # flag-shaped / over-long demand BEFORE the build so the read-only classify path
        # can never be turned into a spawning --loop/--dispatch run.
        if spec.get("routeTriage"):
            bad = _route_triage_reason(params)
            if bad is not None:
                return _refuse(action, bad)
        if action == "ws-integrate-reckon" and params.get("verdict") not in {
                "no-blocker", "blocker"}:
            return _refuse(action, "invalid integrate reckon verdict "
                           "(allowed: no-blocker, blocker)")
        # N2b: composer-create is create-only but it materializes a REAL workflow from
        # browser-composed input — the load-bearing guard. Re-run the N1 compile IN-PROCESS
        # (same validator the Compose screen uses) BEFORE building any argv, and REFUSE unless
        # it is valid. An unknown profile/taskProfile, a token-budget breach, or a secret-shaped
        # value blocks creation before `workflow plan` ever runs: you can only CREATE what VALIDATES.
        if spec.get("composerCreate"):
            report = compile_candidate(root, params.get("profile"), params.get("task"), params.get("branches"))
            if report.get("valid") is not True:
                return _refuse(action, "composer-create requires a valid compile (unknown profile/taskProfile, "
                               "token-budget breach, or secret-shaped value blocks creation)",
                               errors=report.get("errors") or [])
        # GUI-RS4: research-plan re-runs the same compile WITH the form's declared width -- the
        # width-aware trust boundary composer-create's compile_candidate cannot express -- BEFORE
        # any `workflow plan` argv. A blank justification / non-research profile is refused here.
        if spec.get("researchPlan"):
            bad = _research_plan_reason(root, params)
            if bad is not None:
                return _refuse(action, bad)
        try:
            argv = spec["build"](params)
        except (KeyError, TypeError) as exc:
            return _refuse(action, f"missing/invalid param: {exc}")
        # Structural HITL backstop: --approval-token/--send are human-only and
        # never run from the panel, same rule the chat REPL applies (R1).
        if classify_command(argv) == "human-only":
            return _refuse(action, "human-only command refused (carries --approval-token/--send)",
                           argv=argv)
        cmd = [sys.executable, "scripts/harness.py", *argv]

    # Mutating actions need the deliberate browser confirm (dialog sets confirm=true).
    if spec.get("mutating") and params.get("confirm") is not True:
        return _refuse(action, "confirmation required for a mutating action")

    if action == "ws-shard-discard":
        from . import ide_shard
        try:
            ide_shard.dispose(root)
        except Exception as exc:
            return _refuse(action, str(exc))
        return {"ok": True, "action": action}

    # .harness/state mutations are refused while a gate/validate is in flight: the
    # per-scenario restore would silently undo them (see _gate_in_flight above).
    # Covers workflow/project.json writes AND experiment/task writes (experiments.json,
    # tasks.json are held by the same restore) — over-refuse is the safe side (retry
    # after the gate). After the confirm check so an unconfirmed click never pays the
    # process listing.
    if (any(spec.get(k) for k in
            # "acceptance": the drive refuses a HOLD on its own, but a gate merely
            # in flight is the broader window — better to refuse the click than to
            # spawn a vendor TUI whose transcript the hold would then discard.
            ("recovery", "discard", "composerCreate", "researchPlan", "graphProvider",
             "task", "experiment", "integrate", "acceptance"))
            and _gate_in_flight()):
        return _refuse(action, "a harness gate/validate run is in flight — its per-scenario "
                       "state restore would silently undo this .harness/state write "
                       "(experiment verdict / task change / workflow discard resurrects); "
                       "retry after the gate finishes")

    if spec.get("integrateHandler"):
        from . import ide_integrate, tasks_board
        try:
            if action == "ws-integrate-apply":
                return ide_integrate.apply(root)
            if action == "ws-integrate-rollback":
                return ide_integrate.rollback(root, params.get("applied"))
            if action == "ws-integrate-commit":
                return ide_integrate.commit(root, params.get("applied"), params.get("message"))
            pack = ide_integrate.review_pack(root)
            packet = [
                "Review this staged IDE-shard integration. Read-only; do not modify files.",
                "Put exactly `RECOMMEND: no-blocker` or `RECOMMEND: blocker` in the first line.",
                "Mechanical advisory rows:",
                json.dumps(pack["advisoryRows"], ensure_ascii=False),
                "Staged diff" + (" (truncated):" if pack["diffTruncated"] else ":"),
                pack["diff"],
            ]
            import contextlib
            import io
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                tasks_board.refine_model_run(
                    root, "integrate-review", "staged", packet,
                    params.get("model"), params.get("effort"), params.get("executor"))
            proposal = captured.getvalue().strip()
            match = re.search(r"(?im)^RECOMMEND:\s*(no-blocker|blocker)\s*$",
                              "\n".join(proposal.splitlines()[:12]))
            file_match = re.search(r"(?m)^PROPOSAL_FILE:\s*(\S+)\s*$", proposal)
            shown, truncated = truncate_text(proposal, OUTPUT_CAP)
            return {"ok": True, "action": action,
                    "proposalFile": file_match.group(1) if file_match else None,
                    "proposal": shown, "proposalTruncated": truncated,
                    "verdict": match.group(1).lower() if match else None}
        except Exception as exc:
            return _refuse(action, str(exc))

    try:
        # S1/S4: the popped secret (keys-set) reaches the child ONLY as stdin, written then
        # closed. input=None for every other action == today's behavior (no stdin redirect).
        res = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=ACTION_TIMEOUT,
                             input=secret)
    except subprocess.TimeoutExpired:
        return {"ok": False, "rc": 124, "action": action,
                "output": f"timed out after {ACTION_TIMEOUT}s"}
    output, _ = truncate_text((res.stdout or "") + (res.stderr or ""), OUTPUT_CAP)
    result = {"ok": res.returncode == 0, "rc": res.returncode, "action": action,
              "output": output.strip()}
    # The `visible` acceptance door ANSWERS with the command that does the same thing by
    # hand — the button presumes browser and server on one machine, and that line is what
    # stays actionable when they are not. `decide` prints its result as JSON on stdout, so
    # lift the one field the card must show (same merge shape as integrateGate below).
    # ... and on a REFUSAL it answers with the reason plus the same fallback instruction.
    # Without lifting it the card can only say "failed", which is the one thing the owner
    # cannot act on (owner 2026-07-30: the accept button reported nothing).
    if spec.get("acceptance"):
        try:
            answered = json.loads(res.stdout or "{}")
        except json.JSONDecodeError:
            answered = {}
        if isinstance(answered, dict):
            if result["ok"] and answered.get("instruction"):
                result["instruction"] = str(answered["instruction"])
            elif not result["ok"] and answered.get("refused"):
                result["refused"] = str(answered["refused"])
    if result["ok"] and spec.get("integrateGate"):
        try:
            launched = json.loads(res.stdout or "{}")
        except json.JSONDecodeError:
            launched = {}
        if isinstance(launched, dict):
            result.update(launched)
    # UX separation: a successful user-origin mutation commits its own state files
    # (scoped) so the owner's GUI work stops accreting as gate-held .harness dirt.
    # Gated by autocommit_enabled() (see its docstring): the browser e2e drives THIS
    # run_action against the real ROOT via the live server, so only the cmd_ui-seeded
    # env may turn committing on — tests never seed it.
    if result["ok"] and spec.get("mutating") and autocommit_enabled():
        paths = _autocommit_paths(action)
        if paths:
            committed = autocommit_state(root, paths, action)
            if committed:
                result["committed"] = committed
    return result
