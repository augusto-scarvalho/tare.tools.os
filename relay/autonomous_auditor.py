#!/usr/bin/env python3
"""relay/autonomous_auditor.py — on-demand EXTERNAL auditor spawner (agy/Antigravity).

Bootstrap fix for the frozen GUI auditor: the supervisor's `_delegate` already
spawns a critic on demand (`RELAY_PLAN_AUDIT_CMD` / `RELAY_CODE_AUDIT_CMD` with
`{prompt_file}` substituted). This is that critic — a ONE-SHOT process that runs
the audit through the headless `agy` CLI and exits. Kill the active process and
the next supervisor tick re-spawns it cleanly; nothing persists but the report
the model earns.

Session discipline (owner ruling): a conversation is keyed by `train::stage`.
The SAME train+stage across replica rounds (code-audit ↔ implementer remediation,
plan-audit ↔ planner back-and-forth) RESUMES the same agy conversation so prior
findings survive. Different train OR different stage => different tracker key =>
different (or fresh) conversation. Isolation is structural: we only ever resume
the conversation id stored for the exact key — a code-auditor can never inherit
a plan-auditor's session, nor one train another's.

Fail closed: agy missing/error/timeout, or a report that does not pass
`relay_mesh.audit_valid`, exits nonzero so the supervisor holds the gate. We
never write a passing report the model did not produce.

Usage (from the supervisor env var, cwd=relay root):
    python relay/autonomous_auditor.py <plan-audit|code-audit> "{prompt_file}" [--fresh]
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import relay_io  # noqa: E402
import relay_mesh  # noqa: E402  — audit_valid + paths + hostname
import vendor_chairs  # noqa: E402 — multi-vendor fallbacks (Codex/Claude)

TRACKER_FILE = relay_mesh.RELAY_DIR / "history" / "auditor-sessions.json"
AGY_TIMEOUT = 3300  # < supervisor AUDIT_TIMEOUT (3600) so we report before hard-kill
STAGE_GATE = {"plan-audit": "PLAN", "code-audit": "AUDIT"}
STAGE_REPORT = {"plan-audit": "PLAN_AUDIT_REPORT.md", "code-audit": "AUDIT_REPORT.md"}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] [AUTONOMOUS AUDITOR] {msg}", flush=True)


def find_agy() -> str:
    for cand in (os.environ.get("AGY_EXE"), shutil.which("agy"),
                 os.path.expandvars(r"%LOCALAPPDATA%\agy\bin\agy.exe")):
        if cand and Path(cand).exists():
            return cand
    return "agy"


def _tracker() -> dict:
    return relay_io.read_json(TRACKER_FILE) or {}


def _save_tracker(data: dict) -> None:
    relay_io.write_json_atomic(TRACKER_FILE, data)


def _run_agy(prompt: str, conversation_id: str | None) -> dict:
    """One headless agy turn under the existing process-trusted CLI profile.
    No sandbox is asserted: it returns audit TEXT and this spawner writes it.
    Returns the parsed JSON envelope (conversation_id, status, response)."""
    # Fix WinError 206 on Windows: if prompt is large, pass via temporary prompt file
    temp_prompt_file = None
    if len(prompt) > 3000:
        temp_dir = relay_mesh.HISTORY_DIR / "prompts"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_prompt_file = temp_dir / f"audit_prompt_{int(time.time()*1000)}.txt"
        relay_io.write_text_atomic(temp_prompt_file, prompt)
        prompt_arg = (
            f"Read and execute the adversarial audit instructions in file: {temp_prompt_file}\n\n"
            "Return the entire markdown audit report as your direct response text."
        )
    else:
        prompt_arg = prompt

    _runtime_key, agy_model = vendor_chairs.runtime_pin("antigravity", "auditor")
    argv = [find_agy(), "-p", prompt_arg, "--model", agy_model,
            "--output-format", "json",
            "--dangerously-skip-permissions", "--effort", "high"]
    if conversation_id:
        argv[1:1] = ["--conversation", conversation_id]
    proc = subprocess.run(argv, cwd=str(relay_mesh.RELAY_ROOT), capture_output=True,
                          stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                          errors="replace", timeout=120, **relay_io.safe_subprocess_kwargs())
    if proc.returncode != 0:
        raise RuntimeError(f"agy exit {proc.returncode}: {proc.stderr.strip()[:300]}")
    for line in reversed([l for l in proc.stdout.splitlines() if l.strip()]):
        try:
            env = json.loads(line)
            if isinstance(env, dict) and "conversation_id" in env:
                return env
        except json.JSONDecodeError:
            continue
    raise RuntimeError("agy produced no JSON envelope (auth prompt / unexpected output)")


def _source_names(stage: str) -> tuple[str, ...]:
    return (("PACKET.md", "IMPLEMENTATION_EVIDENCE.md", "DIFF.patch", "PLAN_AUDIT_REPORT.md")
            if stage == "code-audit" else ("PACKET.md",))


def _subject_digests(stage: str, train_dir: Path) -> dict[str, str | None]:
    return {name: relay_io.sha256_hex((train_dir / name).read_bytes())
            if (train_dir / name).exists() else None for name in _source_names(stage)}


def _compose(stage: str, train_dir: Path, directive: str) -> str:
    """Inline the complete declared sources and instruct
    it to RETURN the full report as text — no tools, no file writes."""
    def _src(name: str) -> str:
        t = relay_io.read_text(train_dir / name)
        return f"\n\n===== {name} =====\n{t}" if t.strip() else ""
    sources = "".join(_src(name) for name in _source_names(stage))
    verdict_requirements = (
        "For PLAN AUDIT: You MUST include every Task ID and end with 'PLAN_VERDICT: APPROVED' or 'PLAN_VERDICT: REJECTED'.\n"
        "For CODE AUDIT: You MUST include every Task ID, bind the 12-char packet digest, include section '## Audit Recommendation', and conclude with 'AUDIT_VERDICT: VERIFIED_PASS' (or 'AUDIT_VERDICT: FAIL')."
    )
    return (
        "You are the EXTERNAL ADVERSARIAL RED-TEAM AUDITOR. Review the finite scope and "
        "criteria in the packet. Require concrete evidence for blockers against those criteria; "
        "keep accepted limitations and suggestions separate from blockers. Do not invent new tasks.\n"
        "Do NOT use file-writing tools. RETURN the report as text; this instruction is not a sandbox. "
        "Judge INDEPENDENTLY, bind the exact subject, and state residual risks and evidence gaps. "
        "Previous reports are navigation, never inherited approval.\n"
        "Use the exact required verdict marker.\n"
        f"----- REQUIRED MARKERS -----\n{verdict_requirements}\n\n"
        f"----- DIRECTIVE -----\n{directive}\n"
        f"----- SOURCES (read-only, inlined) -----{sources}\n"
        "----- END -----\nReturn ONLY the full audit report content now."
    )


def _strict_audit_routing(train_dir: Path, stage: str) -> tuple[object | None, dict[str, object]]:
    """Attest candidates and remove every auditor in the implementer bias domain."""

    from relay.landing_policy import (
        IMPLEMENTER_IDENTITY_FILE,
        attest_identity,
        model_id_for_runtime,
        read_identity,
    )

    roster = relay_mesh.RELAY_DIR / "round_table_roster.json"
    # A plan's producer uses the same attested producer schema as code, in a
    # stage-specific receipt. Missing legacy provenance must not imply independence.
    identity_file = IMPLEMENTER_IDENTITY_FILE if stage == "code-audit" else "PLANNER_IDENTITY.json"
    implementer = read_identity(train_dir / identity_file)
    if implementer.payload["role"] != "implementer":
        raise ValueError("audit subject identity must attest its producer")
    role = "auditor" if stage == "code-audit" else "plan-auditor"
    candidates: dict[str, object] = {}
    for engine in vendor_chairs.ROLE_CHAIRS[role]:
        try:
            runtime_key, runtime_model = vendor_chairs.runtime_pin(
                engine, "auditor" if engine == "antigravity" else role)
            candidate = attest_identity(
                roster_path=roster,
                agent_id=f"auditor-{engine}",
                role="auditor",
                model_id=model_id_for_runtime(roster, runtime_key, runtime_model),
                runtime_key=runtime_key,
                runtime_model=runtime_model,
            )
        except Exception:
            continue
        if candidate.bias_domain != implementer.bias_domain:
            candidates[engine] = candidate
    return implementer, candidates


def audit(stage: str, prompt_file: Path, fresh: bool = False) -> int:
    if stage not in STAGE_GATE:
        log(f"unknown stage {stage!r}"); return 2
    prompt_file = Path(prompt_file).resolve()
    train_dir = prompt_file.parent
    train_id = train_dir.name
    if train_dir.parent != relay_mesh.TRAINS_DIR.resolve():  # miswiring guard
        log(f"prompt file not under trains/ ({prompt_file}) — refusing"); return 2
    directive = relay_io.read_text(prompt_file)
    if not directive.strip():
        log("empty prompt file"); return 2

    try:
        subject_before = _subject_digests(stage, train_dir)
        strict_implementer, audit_candidates = _strict_audit_routing(train_dir, stage)
    except Exception as exc:
        log(f"strict audit identity preflight failed: {exc}")
        return 1
    excluded_engines = (
        set(vendor_chairs.ROLE_CHAIRS["auditor" if stage == "code-audit" else "plan-auditor"])
        - set(audit_candidates)
    )
    selected_identity = None

    key = f"{train_id}::{stage}"
    tracker = _tracker()
    entry = tracker.get(key) if not fresh else None
    conv = (entry or {}).get("conversation_id")
    if (entry or {}).get("host") != relay_mesh.get_hostname():
        conv = None
    composed = _compose(stage, train_dir, directive)
    report_text = ""
    conv_id = None
    try:
        env = (
            {}
            if "antigravity" in excluded_engines
            else _run_agy(composed, conv)
        )
        if env.get("status") == "SUCCESS":
            report_text = str(env.get("response") or "").strip()
            conv_id = env.get("conversation_id")
    except Exception as exc:
        log(f"agy delegation failed: {exc} — attempting vendor_chairs fallback...")

    if report_text and "antigravity" in excluded_engines:
        log("discarding same-bias Antigravity audit before settlement")
        report_text = ""
        conv_id = None
    elif report_text:
        selected_identity = audit_candidates.get("antigravity")

    if not report_text:
        role = "plan-auditor" if stage == "plan-audit" else "auditor"
        res = vendor_chairs.dispatch_role(
            role,
            composed,
            cwd=relay_mesh.RELAY_ROOT,
            timeout=1200,
            excluded_engines=excluded_engines,
        )
        if res.ok and res.stdout.strip():
            report_text = res.stdout.strip()
            selected_identity = audit_candidates.get(res.engine)
            log(f"vendor_chairs ({res.engine}) generated audit report ({len(report_text)} chars)")
        else:
            log(f"vendor_chairs fallback failed: {res.error_reason or res.stderr[:200]}")
            return 1

    try:
        if _subject_digests(stage, train_dir) != subject_before:
            log("audit source changed during audit — stale subject binding, refusing")
            return 1
    except OSError as exc:
        log(f"cannot recheck audit subject: {exc}")
        return 1

    if strict_implementer is not None:
        if selected_identity is None:
            log("strict auditor identity was not independently attested; refusing")
            return 1
        if selected_identity.bias_domain == strict_implementer.bias_domain:
            log("strict auditor shares implementer bias_domain; refusing")
            return 1
        from relay.landing_policy import AUDITOR_IDENTITY_FILE, read_identity, write_identity

        identity_path = train_dir / (AUDITOR_IDENTITY_FILE if stage == "code-audit"
                                     else "PLAN_AUDITOR_IDENTITY.json")
        if identity_path.exists():
            if read_identity(identity_path).sha256 != selected_identity.sha256:
                log("strict auditor identity changed after create-only receipt; refusing")
                return 1
        else:
            write_identity(identity_path, selected_identity)

    # Publish only after subject and producer checks; never repair conclusions.
    relay_io.write_text_atomic(train_dir / STAGE_REPORT[stage], report_text + "\n")

    # Persist the conversation for replica-round resume (host-scoped).
    tracker[key] = {"conversation_id": conv_id,
                    "host": relay_mesh.get_hostname(),
                    "rounds": int((entry or {}).get("rounds", 0)) + 1,
                    "updated_at": relay_mesh.now_iso()}
    _save_tracker(tracker)

    # Honest exit code: the written report must pass the anti-rubber-stamp gate,
    # OR be an explicit FAIL the supervisor routes to AUDIT_REJECTED.
    if "AUDIT_VERDICT: FAIL" in report_text:
        log("auditor returned FAIL verdict"); return 0
    ok, why = relay_mesh.audit_valid(train_id, STAGE_GATE[stage])
    if ok:
        log(f"audit valid for {key} (rounds={tracker[key]['rounds']})"); return 0
    log(f"report failed audit_valid: {why}")
    return 1


def main() -> int:
    relay_io.utf8_console()
    args = [a for a in sys.argv[1:] if a != "--fresh"]
    fresh = "--fresh" in sys.argv
    if len(args) < 2:
        print("usage: autonomous_auditor.py <plan-audit|code-audit> <prompt_file> [--fresh]",
              file=sys.stderr)
        return 2
    return audit(args[0], Path(args[1]), fresh=fresh)


if __name__ == "__main__":
    raise SystemExit(main())
