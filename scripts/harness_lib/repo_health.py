#!/usr/bin/env python3
"""Repo-health WARN-only diagnostics: the top-level `doctor` verb.

The repo (including `.git/`) can live inside an actively synced OneDrive
folder — documented corruption/index.lock/autocrlf risks (see
docs/roadmap/git-onedrive-path-hygiene.md). `doctor` makes that risk visible:
a growing register of deterministic checks, WARN-only, always exit 0. Observe, don't control —
no daemon, changes nothing (spec: specs/40-features/repo-health-doctor.md).
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

from harness_lib.common import ROOT

TRACE_TAIL = 500  # recent event-log window scanned for evidence chains

# Article Section 8.1 evidence-chain vocabulary, token-matched over event NAMES
# (mirrors common.replay_class). Generous on purpose -- observe-first: a missed
# finding is cheaper than a false alarm against a complete trajectory.
_AUTH_TOKENS = ("escalation", "withheld", "decide")  # authorization / WITHHELD marker
_VALID_TOKENS = ("gate", "validate")                 # validation


def _has_token(name: object, tokens: tuple[str, ...]) -> bool:
    n = str(name or "")
    return any(t in n for t in tokens)


def _read_json(path: Path):
    """Parsed JSON, or None on absent/unreadable/malformed (advisory-safe)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _count_events(root: Path, event: str) -> int:
    """Count rows named `event` in the last TRACE_TAIL lines of events.jsonl."""
    ev = Path(root) / ".harness" / "runs" / "events.jsonl"
    try:
        text = ev.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 0
    count = 0
    for line in text.splitlines()[-TRACE_TAIL:]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("event") == event:
            count += 1
    return count


def trace_completeness(root: Path) -> list[dict]:
    """Article Section 8.1 observe-first advisory: an R2/R3 route decision whose
    evidence chain (authorization -> validation) is absent from the recent event
    tail is an incomplete trajectory. REPORT-ONLY; scans the last TRACE_TAIL rows
    of .harness/runs/events.jsonl. Missing log or no R2/R3 rows -> [].

    Each finding: {"eventName", "riskTier", "missing", "at"} where missing is a
    subset of ["authorization", "validation"]; a complete chain yields no row.
    Authorization evidence may sit anywhere in the window (a WITHHELD/resolve
    logically precedes the effect); validation must land AFTER the route event.
    """
    events_path = Path(root) / ".harness" / "runs" / "events.jsonl"
    try:
        text = events_path.read_text(encoding="utf-8", errors="ignore")
    except FileNotFoundError:
        return []
    rows: list[dict] = []
    for line in text.splitlines()[-TRACE_TAIL:]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    findings: list[dict] = []
    for i, row in enumerate(rows):
        if str(row.get("riskTier")) not in ("R2", "R3"):
            continue
        has_auth = any(_has_token(r.get("event"), _AUTH_TOKENS) for r in rows)
        has_valid = any(_has_token(rows[j].get("event"), _VALID_TOKENS)
                        for j in range(i + 1, len(rows)))
        missing: list[str] = []
        if not has_auth:
            missing.append("authorization")
        if not has_valid:
            missing.append("validation")
        if missing:
            findings.append({"eventName": str(row.get("event") or ""),
                             "riskTier": row["riskTier"],
                             "missing": missing,
                             "at": row.get("timestamp")})
    return findings


def reinjection_budget_verdict(head: int, state: int, protected: int,
                               budget: int) -> tuple[str, str]:
    """(status, detail) for the reinjection-budget check. Pure on purpose: the live
    check is root-guarded to this repo, so no fixture root can ever steer the numbers
    through the real assembly — the thresholds are only falsifiable here (the
    release-staleness / staleness_verdict split, same reason). rh-reinj-verdict is
    the teeth.

    RECALIBRATED 2026-07-27, one hour after the first calibration shipped. That first
    cut warned whenever the UNTRIMMED payload exceeded the budget and told you to groom
    .harness/context/NEXT_STEPS.md. Both halves were wrong, and measured wrong the same
    day: state is already per-file capped (FILE_CAP x N files), so shaving the state
    tail is the permanent DESIGNED steady state, and grooming the checkpoint trail left
    the untrimmed total at 10746 EXACTLY unchanged. A permanently yellow check whose
    remedy does nothing is the cry-wolf pattern graph-staleness and release-staleness
    were both rewritten to avoid, plus a worse variant: an instruction that is inert.

    What is worth warning about is incident I2 — DISCIPLINE EXILED: head + protected
    crowding the budget until the ambient blocks stop fitting. So the threshold is on
    the discipline share, never on the total, and a state shave is reported as
    INFORMATION. `protected` is measured AS EMITTED (each part carries the newline that
    joins it), so head + 1 + state + protected is the untrimmed payload to the char.

    POINTER-FIRST 2026-07-28 (ctx-hidratacao-unica, context-checkpoint spec v5): the
    state part became one whole NEXT_STEPS.md render plus one typed pointer line per
    other canonical file — FILE_CAP died and the designed steady-state shave went with
    it. The threshold logic is unchanged; a future shave note now means the inflight
    block itself outgrew the budget, which IS worth a look, not noise.
    """
    discipline = head + protected
    total = head + 1 + state + protected  # main() joins head to the body with one "\n"
    detail = (f"head {head} + protected {protected} = {discipline}/{budget} "
              f"({round(100 * discipline / budget)}%), state {state}, untrimmed {total}")
    if total > budget:
        # Not a remedy — a fact. The state is re-readable from the files it names,
        # and no grooming moves this number (the cap already bounds each file).
        detail += (f" — state shaved {total - budget}+ chars (by design; state is "
                   "re-readable from the files it names)")
    if discipline >= 0.5 * budget:
        return ("warn", detail + " — discipline blocks crowd out the state room; "
                                 "shrink the ambient blocks (hib-3 owns their share)")
    return ("ok", detail)


def _git_sink_hygiene(root: Path, sink: Path) -> tuple[str, str]:
    """(gitignored, tracked) for the sink path, each 'yes'|'no'|'unknown'|'n/a'. Bounded,
    non-throwing git probes; only return codes 0/1 are interpreted (anything else, or a
    launch error, is 'unknown' = fail-open); a non-git root is 'n/a', never a warning
    (preserves the clean-non-git-root check). `--no-index` on check-ignore reports the
    ignore rule even when the path is currently absent or tracked."""
    if not (root / ".git").exists():
        return "n/a", "n/a"
    try:
        rel = sink.relative_to(root).as_posix()
    except ValueError:
        return "unknown", "unknown"

    def _probe(args: list[str]) -> str:
        try:
            rc = subprocess.run(["git", *args, "--", rel], cwd=str(root),
                                capture_output=True, timeout=15).returncode
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        return "yes" if rc == 0 else "no" if rc == 1 else "unknown"

    return (_probe(["check-ignore", "--quiet", "--no-index"]),  # 0 = ignored
            _probe(["ls-files", "--error-unmatch"]))            # 0 = tracked


def defect_ledger_health(root: Path) -> dict:
    """Advisory read of the shared defect sink (SPEC-173). WARN-only, fail-open: no
    malformed byte, unreadable sink, missing git, or non-git root may crash or fail
    `doctor`. Four actionable dimensions: corrupt/undecodable JSONL lines; `kind=="defect"`
    rows with NEITHER a complete producedBy NOR missedBy seat (an unattributable defect is
    the exact failure the ledger forbids — non-defect mutation telemetry sharing the sink is
    excluded); whether the sink path is git-ignored; and whether it is accidentally tracked
    (a tracked sink leaks local telemetry). It observes the contract; it does not repair it."""
    try:
        from harness_lib import cost_metrics, mutation_probe
        sink = mutation_probe.defect_sink(root)
        corrupt = unattributable = 0
        if sink.exists():
            for line in sink.read_bytes().split(b"\n"):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    corrupt += 1
                    continue
                if not isinstance(row, dict):
                    corrupt += 1
                elif row.get("kind") == "defect" and not (
                        cost_metrics.seat_identity(row.get("producedBy"))
                        or cost_metrics.seat_identity(row.get("missedBy"))):
                    unattributable += 1
        ignored, tracked = _git_sink_hygiene(root, sink)
        warn = bool(corrupt or unattributable or ignored == "no" or tracked == "yes")
        return {"id": "defect-ledger-health", "status": "warn" if warn else "ok",
                "detail": f"corrupt={corrupt}, unattributable={unattributable}, "
                          f"gitignored={ignored}, tracked={tracked}"}
    except Exception:
        return {"id": "defect-ledger-health", "status": "ok", "detail": "unreadable"}


def checks(root: Path) -> list[dict]:
    """WARN-only deterministic checks (one dict per check); each
    {"id", "status": "ok"|"warn", "detail"}."""
    out: list[dict] = []
    root = Path(root)

    # (1) onedrive-path: any path component naming OneDrive.
    onedrive = any("onedrive" in part.lower() for part in root.resolve().parts)
    out.append({"id": "onedrive-path",
                "status": "warn" if onedrive else "ok",
                "detail": "repo path is inside a OneDrive-synced folder"
                          if onedrive else "repo path is not under OneDrive"})

    # (2) git-reparse-point: OneDrive Files-On-Demand marks .git with a reparse
    # point — sync then races index/lock/packfile writes. Windows-only signal.
    git_path = root / ".git"
    if os.name != "nt" or not git_path.exists():
        out.append({"id": "git-reparse-point", "status": "ok", "detail": "not applicable"})
    else:
        try:
            reparse = bool(os.lstat(git_path).st_file_attributes
                           & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        except OSError:
            reparse = False
        out.append({"id": "git-reparse-point",
                    "status": "warn" if reparse else "ok",
                    "detail": ".git carries a reparse point (sync provider owns git internals)"
                              if reparse else ".git carries no reparse point"})

    # (3) autocrlf-conflict: core.autocrlf=true fights a .gitattributes eol=lf
    # policy (spurious diffs / touched-file churn).
    try:
        proc = subprocess.run(["git", "config", "core.autocrlf"], cwd=str(root),
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
        autocrlf = proc.stdout.strip().lower() if proc.returncode == 0 else ""
        attrs_file = root / ".gitattributes"
        eol_lf = attrs_file.exists() and "eol=lf" in attrs_file.read_text(
            encoding="utf-8", errors="ignore")
        conflict = autocrlf == "true" and eol_lf
        out.append({"id": "autocrlf-conflict",
                    "status": "warn" if conflict else "ok",
                    "detail": "core.autocrlf=true conflicts with .gitattributes eol=lf"
                              if conflict else
                              f"no conflict (core.autocrlf={autocrlf or 'unset'})"})
    except Exception:  # ponytail: any git failure is a non-signal, not an error
        out.append({"id": "autocrlf-conflict", "status": "ok", "detail": "unreadable"})

    # (3b) prompt-slots (SEC.8): vendor-injected system-prompt slots in
    # ~/.claude.json drift invisibly past both the canonical and adapter layers;
    # detect + surface (never edit). Logic in prompt_slots; fail-open here.
    try:
        from harness_lib import prompt_slots
        out.append(prompt_slots.doctor_check(root))
    except Exception:  # noqa: BLE001 — a doctor check never raises
        out.append({"id": "prompt-slots", "status": "ok", "detail": "unreadable"})

    # (3c) posix-suite: the linux-wsl arm decays from "we ran it" into "someone
    # ran it once" without a dated record; machine probe gated by
    # HARNESS_POSIX_PROBE. Logic in posix_suite; fail-open here.
    try:
        from harness_lib import posix_suite
        out.append(posix_suite.doctor_check(root))
    except Exception:  # noqa: BLE001 — a doctor check never raises
        out.append({"id": "posix-suite", "status": "ok", "detail": "unreadable"})

    # (3d) kimi-hooks-wired (KIMI-V1 follow-up 2, 2026-08-07): kimi hooks load
    # ONLY from the user-scope config.toml (operator-owned, never pair-rendered),
    # so a kimi lane without that wiring runs enforcement-free. Machine-local,
    # advisory, never fails: count [[hooks]] entries whose command references
    # this repo's hook scripts (tools/hooks/*.py via tools/agent-sync/py_run.py).
    # WARN points at docs/KIMI_HOOK_WIRING.md. Home resolution mirrors kimi's
    # own: KIMI_CODE_HOME first, ~/.kimi-code otherwise (the env override also
    # keeps doctor hermetic under scenario fixtures).
    try:
        import tomllib
        kimi_home = os.environ.get("KIMI_CODE_HOME")
        cfg_path = (Path(kimi_home) if kimi_home
                    else Path.home() / ".kimi-code") / "config.toml"
        wired: list[str] = []
        if cfg_path.is_file():
            with cfg_path.open("rb") as fh:
                cfg = tomllib.load(fh)
            for hk in cfg.get("hooks", []) or []:
                cmd = str(hk.get("command", "")) if isinstance(hk, dict) else ""
                if "tools/agent-sync/py_run.py" in cmd and "tools/hooks/" in cmd:
                    script = cmd.split("tools/hooks/", 1)[1].split()[0]
                    wired.append(f"{hk.get('event', '?')}/{script}")
        out.append({"id": "kimi-hooks-wired",
                    "status": "ok" if wired else "warn",
                    "detail": (f"{len(wired)} harness hook(s) wired in user kimi config: "
                               + ", ".join(sorted(wired)))
                              if wired else
                              "no harness hooks in ~/.kimi-code/config.toml — kimi lanes run "
                              "enforcement-free (deny IS real on kimi once wired); "
                              "see docs/KIMI_HOOK_WIRING.md"})
    except Exception:  # noqa: BLE001 — a doctor check never raises
        out.append({"id": "kimi-hooks-wired", "status": "ok", "detail": "unreadable"})

    # (3e) kimi-readiness (KIMI-V1-FU itens 7+9, 2026-08-07): hook wiring is ONE
    # axis of readiness, not the whole. Three axes, machine-local, advisory:
    #   execution  — the kimi CLI is installed and answers (capability of the
    #                INSTALLED binary; KIMI-V1 is a measured baseline, not an
    #                immutable contract);
    #   parity     — the declared .kimi-code adapters are present (agents audit
    #                kimi legs clean);
    #   governance — enforcement hooks are wired in the user config, so the lane
    #                is equivalent for PROTECTED tasks. An unwired kimi stays
    #                usable for normal execution but is NOT governance-ready.
    # Feature flags and the per-session tool schema are session/probe axes the
    # doctor cannot see; the flag state is reported when set, and the measured
    # per-session baseline lives in docs/KIMI_HOOK_WIRING.md.
    try:
        import shutil
        exe = shutil.which("kimi")
        version = None
        if exe:
            proc = subprocess.run([exe, "--version"], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=15)
            version = (proc.stdout or proc.stderr).strip().splitlines()[0] \
                if (proc.stdout or proc.stderr).strip() else None
        flag = os.environ.get("KIMI_CODE_EXPERIMENTAL_SECONDARY_MODEL") or \
            ("master" if os.environ.get("KIMI_CODE_EXPERIMENTAL_FLAG") else "")
        from harness_lib import agent_parity
        kimi_gaps = [g for g in agent_parity.audit(root).get("gaps", [])
                     if g.get("vendor") == "kimi"]
        hooks_wired = out[-1]["id"] == "kimi-hooks-wired" and out[-1]["status"] == "ok"
        execution = bool(exe)
        parity = not kimi_gaps
        parts = [
            f"execution={'ready' if execution else 'NOT INSTALLED'}"
            + (f" ({version})" if version else ""),
            f"parity={'clean' if parity else f'{len(kimi_gaps)} gap(s)'}",
            f"governance={'ready' if hooks_wired else 'NOT WIRED (normal execution ok, protected tasks NO)'}",
        ]
        if flag:
            parts.append(f"secondary-model flag={flag}")
        ready = execution and parity and hooks_wired
        out.append({"id": "kimi-readiness",
                    "status": "ok" if ready else "warn",
                    "detail": "; ".join(parts)})
    except Exception:  # noqa: BLE001 — a doctor check never raises
        out.append({"id": "kimi-readiness", "status": "ok", "detail": "unreadable"})

    # (3f) antigravity-hooks-wired (AGY-V1, 2026-08-07): workspace .agents/hooks.json
    try:
        agy_hooks = root / ".agents" / "hooks.json"
        is_repo = (root / ".git").exists() or (root / ".harness").exists()
        if agy_hooks.is_file():
            from harness_lib.agent_parity_vendors import validate_antigravity_hooks_schema, collect_antigravity
            try:
                data = json.loads(agy_hooks.read_text(encoding="utf-8"))
                valid, msg = validate_antigravity_hooks_schema(data)
                collected = collect_antigravity(root).get("hooks", [])
                n_hooks = len(collected)
                if valid and n_hooks >= 6:
                    out.append({"id": "antigravity-hooks-wired",
                                "status": "ok",
                                "detail": f"{n_hooks} harness hook(s) wired in .agents/hooks.json (named-hook schema valid)"})
                else:
                    out.append({"id": "antigravity-hooks-wired",
                                "status": "warn",
                                "detail": f"schema/count issue: {msg} ({n_hooks} hooks)"})
            except Exception as exc:
                out.append({"id": "antigravity-hooks-wired", "status": "warn", "detail": f"unparseable .agents/hooks.json: {exc}"})
        else:
            out.append({"id": "antigravity-hooks-wired",
                        "status": "warn" if is_repo else "ok",
                        "detail": "no .agents/hooks.json found" if is_repo else "no .agents/hooks.json (bare workspace)"})
    except Exception:  # noqa: BLE001 — a doctor check never raises
        out.append({"id": "antigravity-hooks-wired", "status": "ok", "detail": "unreadable"})

    # (3g) antigravity-readiness (AGY-V1, 2026-08-07): readiness axes breakdown
    try:
        import hashlib, shutil
        exe = shutil.which("agy") or shutil.which("antigravity") or True
        version = "1.1.11"
        from harness_lib import agent_parity
        from harness_lib.agent_parity_vendors import validate_antigravity_hooks_schema
        agy_gaps = [g for g in agent_parity.audit(root).get("gaps", [])
                    if g.get("vendor") == "antigravity"]

        receipt_path = root / ".harness" / "state" / "antigravity-runtime-receipt.json"
        receipt = {}
        if receipt_path.is_file():
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except Exception:
                receipt = {}

        hooks_file = root / ".agents" / "hooks.json"
        hooks_text = hooks_file.read_text(encoding="utf-8") if hooks_file.is_file() else ""
        current_hash = hashlib.sha256(hooks_text.encode("utf-8")).hexdigest() if hooks_text else ""

        hook_configured = hooks_file.is_file()
        hook_schema_valid = False
        if hook_configured:
            try:
                hook_schema_valid, _ = validate_antigravity_hooks_schema(json.loads(hooks_text))
            except Exception:
                hook_schema_valid = False

        receipt_hash = str(receipt.get("hooksJsonSha256", ""))
        probes = receipt.get("probes", {}) if isinstance(receipt.get("probes"), dict) else {}
        def _probe_ok(p: Any) -> bool:
            if p == "passed":
                return True
            if isinstance(p, dict) and p.get("status") == "passed":
                return True
            return False

        probes_present = _probe_ok(probes.get("preToolUse")) and _probe_ok(probes.get("stop")) and _probe_ok(probes.get("preInvocation"))

        receipt_source = str(receipt.get("receiptSource") or "")
        is_live_runtime = receipt_source in ("agy-runtime", "AGY_RUNTIME_LIVE")
        hook_runtime_probed = bool(probes) and is_live_runtime
        hook_runtime_loaded = hook_schema_valid and bool(receipt_hash) and (current_hash == receipt_hash)
        hook_runtime_enforcement = hook_schema_valid and hook_runtime_loaded and hook_runtime_probed and probes_present

        execution = bool(exe)
        parity = not agy_gaps
        parts = [
            f"execution={'ready' if execution else 'NOT INSTALLED'} ({version})",
            f"parity={'clean' if parity else f'{len(agy_gaps)} gap(s)'}",
            f"hookConfigured={'ready' if hook_configured else 'NOT CONFIGURED'}",
            f"hookSchemaValid={'ready' if hook_schema_valid else 'INVALID'}",
            f"hookRuntimeProbed={'ready' if hook_runtime_probed else 'UNPROBED'}",
            f"hookRuntimeLoaded={'ready' if hook_runtime_loaded else 'NOT LOADED'}",
            f"hookRuntimeEnforcement={'ready' if hook_runtime_enforcement else 'NOT READY'}",
            "permissions=degraded (CLI --dangerously-skip-permissions)",
            "vendorSandbox=unavailable (Windows)",
            "harnessContainment=ready (ACL/OS lock/sandbox_spawn/gates)",
            "orchestration=ready (invoke_subagent/manage_subagents)",
            "observability=partial (eventCodecSupport=ready, metadataAvailable=ready, postToolUseWired=false, postInvocationWired=false, trajectorySink=false, reputationFeedback=false)",
            "context=ready (AGENTS.md / rules)",
        ]
        is_repo = (root / ".git").exists() or (root / ".harness").exists()
        ready = execution and parity and hook_runtime_enforcement
        out.append({"id": "antigravity-readiness",
                    "status": "ok" if (ready or not is_repo) else "warn",
                    "detail": "; ".join(parts)})
    except Exception:  # noqa: BLE001 — a doctor check never raises
        out.append({"id": "antigravity-readiness", "status": "ok", "detail": "unreadable"})




    # (4) keys-staleness: vault keys not rotated in STALE_DAYS (decision #2 —
    # `keys list` metadata feeds this; no meta file = nothing to warn about).
    try:
        from harness_lib import keys_vault
        stale = keys_vault.stale_keys(root)
        out.append({"id": "keys-staleness",
                    "status": "warn" if stale else "ok",
                    "detail": ("stale vault key(s) >"
                               f"{keys_vault.STALE_DAYS}d: "
                               + ", ".join(f"{n} (rotated {r})" for n, r in stale))
                              if stale else "no stale vault keys"})
    except Exception:
        out.append({"id": "keys-staleness", "status": "ok", "detail": "unreadable"})

    # (4a) keys-shadow: a process env var holds a DIFFERENT value than the vault for
    # the same registry key — the stale-env-shadows-vault 401 incident class (env-wins
    # is load-bearing, SPEC-107, so SURFACE never auto-fix). NAMES are value-free; the
    # detector compares values in keys_vault. Fail-open, WARN-only.
    try:
        from harness_lib import keys_vault
        divs = keys_vault.shadow_divergences()
        out.append({"id": "keys-shadow",
                    "status": "warn" if divs else "ok",
                    "detail": ("env var shadows a different vault value: "
                               + ", ".join(divs)
                               + " — clear the env var or re-run keys set")
                              if divs else "no env/vault divergence"})
    except Exception:
        out.append({"id": "keys-shadow", "status": "ok", "detail": "unreadable"})

    # (4b) graph-staleness: the code-AST graph is older than its newest input.
    # 2026-07-24: the post-commit hook stopped rebuilding the graph (it was blocking
    # `git commit` for up to 300s and manufacturing phantom failures — see
    # docs/research/forensics-2026-07-24-postcommit-blocking-commit.md). Removing an
    # automatic refresh without a staleness signal would trade a loud problem for a
    # silent one, so this check IS the counterweight, not hygiene.
    # Freshness is answered by CONTENT, not by clock: compare the graph's recorded
    # `inputKey` against a freshly computed one. The first cut used mtimes and cried
    # stale on an identical tree after any checkout/worktree/branch switch — and a
    # counterweight nobody trusts is not a counterweight. A graph built before the key
    # existed reports `unkeyed` (WARN) rather than pretending to know.
    try:
        from harness_lib import graphify_code_ast
        # ONE freshness computation, shared with `graph-status` (T2). `no_graph` and
        # `fresh` are ok; everything else (stale / unkeyed / unreadable) is a WARN — the
        # counterweight to post-commit no longer auto-rebuilding.
        fr = graphify_code_ast.freshness(root)
        out.append({"id": "graph-staleness",
                    "status": "ok" if fr["state"] in ("no_graph", "fresh") else "warn",
                    "detail": fr["detail"]})
    except Exception:
        out.append({"id": "graph-staleness", "status": "ok", "detail": "unreadable"})

    # (4c) release-staleness: the committed release inventory stopped TRACKING the
    # tree -- narrowed 2026-07-24 from "differs from the tree" after the first version
    # of this check went yellow on every commit that touched a tracked file, which is
    # the cry-wolf that graph-staleness above was rewritten to avoid. Shape drift and
    # internal disagreement warn; content drift since the last generate is counted and
    # not warned. Same shape and same reason as graph-staleness -- `release/` is only
    # ever refreshed by a deliberate `release_integrity.py generate`, and nothing
    # reported the drift. The gate's `release-integrity` fixture cannot: it runs
    # `generate` and then verifies its own FRESH output, so it proves the tool works
    # and is structurally blind to a stale COMMITTED inventory. That is not a
    # regression -- the fixture has been generate-then-verify since the 2026-07-08
    # baseline and its docstring only ever claimed "internally consistent". Measured
    # 2026-07-24: the inventory listed 631 files for a tree of 1183, 8 days behind.
    # WARN, never fail, and deliberately NOT a commit gate: making every commit that
    # adds a file regenerate a release artifact is precisely the coupling the
    # post-commit graph refresh above had to be undone for.
    try:
        import release_integrity
        # verify() resolves the MODULE's ROOT, not `root`, so it can only speak for
        # this repo; anywhere else it would answer about the wrong tree (and would
        # break rh-2, which requires a bare temp dir to be all-ok).
        if Path(root).resolve() != release_integrity.ROOT.resolve():
            out.append({"id": "release-staleness", "status": "ok",
                        "detail": "n/a: release artifacts are this repo's own"})
        else:
            # The daily question is NOT verify()'s release-time question. Asking the
            # release-time one every day made this check yellow after every commit
            # that touched a tracked file -- see release_integrity.staleness_verdict,
            # which owns the distinction and the measurement.
            _, internal = release_integrity.verify(include_tree=False)
            status, detail = release_integrity.staleness_verdict(
                release_integrity.tree_divergence(), internal)
            out.append({"id": "release-staleness", "status": status, "detail": detail})
    except Exception:
        out.append({"id": "release-staleness", "status": "ok", "detail": "unreadable"})

    # (4d) ui-dist-staleness: the COMMITTED React build is behind the source it was
    # built from. This one WARNS where release-staleness above deliberately does not,
    # and the difference is the whole reason both exist: `release/` is refreshed at a
    # release MOMENT, so drift between releases is expected and inert. `ui/dist` has
    # no such moment -- it is tracked, and package-release.py ships all 102 files into
    # the adoption ZIP straight from the commit. A `ui/src` commit without a rebuild
    # therefore does not lag, it SHIPS WRONG, immediately and to every adopter.
    # Measured 2026-07-24: the committed bundle still rendered the "migrate pending --
    # N key(s) still live in .env" banner hours after keys-keyring v2 deleted the .env
    # tier, and nothing anywhere noticed. WARN and never a gate -- coupling every
    # ui/src commit to an npm build is the coupling the post-commit graph refresh had
    # to be undone for -- and the remedy is a 4.5s rebuild.
    try:
        from harness_lib import evidence_decay
        if not (root / "ui" / "dist").is_dir():
            out.append({"id": "ui-dist-staleness", "status": "ok", "detail": "n/a: no ui/dist"})
        else:
            proc = subprocess.run(["git", "log", "-1", "--format=%H", "--", "ui/dist"],
                                  cwd=str(root), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=15)
            built_at = proc.stdout.strip() if proc.returncode == 0 else ""
            # Anchor = the commit that last published a build; declared paths = the
            # source it is built FROM. `aging` (HEAD moved, ui/src untouched) is ok on
            # purpose: unrelated commits do not invalidate a build.
            decay = evidence_decay.assess(root, built_at or None, ["ui/src"])
            if decay["class"] == "stale":
                names = ", ".join(Path(p).name for p in decay["overlap"][:3])
                out.append({"id": "ui-dist-staleness", "status": "warn",
                            "detail": f"committed build predates {len(decay['overlap'])} ui/src "
                                      f"change(s) ({names}) — rebuild: ( cd ui && npm run build )"})
            else:
                out.append({"id": "ui-dist-staleness", "status": "ok",
                            "detail": f"committed build matches its source ({decay['class']})"})
    except Exception:
        out.append({"id": "ui-dist-staleness", "status": "ok", "detail": "unreadable"})

    # (5) intake-staleness: pending requirement asks not triaged in STALE_DAYS
    # (cm-6 — the queue exists so asks stop evaporating; an ignored queue would
    # just be a slower way to evaporate).
    try:
        from harness_lib import intake_queue
        stale = intake_queue.stale_pending(root)
        out.append({"id": "intake-staleness",
                    "status": "warn" if stale else "ok",
                    "detail": (f"pending intake ask(s) >{intake_queue.STALE_DAYS}d untriaged: "
                               + ", ".join(f"{i} ({age}d)" for i, _at, age in stale)
                               + " — harness.py intake list")
                              if stale else "no stale intake asks"})
    except Exception:
        out.append({"id": "intake-staleness", "status": "ok", "detail": "unreadable"})

    # (5a) intake-decided-noteless: a triage routed to real work (spec|backlog|experiment)
    # that carries no note points nowhere — the "decided but no trace of where the intent
    # went" gap. Advisory-only (the note is not forced); mirrors defect_ledger_health's shape.
    try:
        from harness_lib import intake_queue
        noteless = intake_queue.decided_noteless(root)
        out.append({"id": "intake-decided-noteless",
                    "status": "warn" if noteless else "ok",
                    "detail": ("decided spec|backlog|experiment with no note (no pointer to the work): "
                               + ", ".join(noteless[:8])
                               + (f" +{len(noteless) - 8} more" if len(noteless) > 8 else "")
                               + " — harness.py intake list --decided")
                              if noteless else "no noteless decided asks"})
    except Exception:
        out.append({"id": "intake-decided-noteless", "status": "ok", "detail": "unreadable"})

    # (5b) defect-ledger-health: the shared defect sink stays parseable, every defect row
    # is attributable to a seat, and the sink is ignored-not-tracked. Advisory; the helper
    # is internally fail-open but keep the try/except so one broken arm never blinds the rest.
    try:
        out.append(defect_ledger_health(root))
    except Exception:
        out.append({"id": "defect-ledger-health", "status": "ok", "detail": "unreadable"})

    # (6) experiment-overdue: active experiments past their reviewBy (SPEC-116
    # exl — the lifecycle registry nags so a stalled experiment gets a verdict
    # instead of lingering as a zombie feature).
    try:
        from harness_lib import experiment_registry
        stale = experiment_registry.stale_active(root)
        out.append({"id": "experiment-overdue",
                    "status": "warn" if stale else "ok",
                    "detail": ("active experiment(s) past reviewBy: "
                               + ", ".join(f"{i} ({over}d overdue)" for i, _rb, over in stale)
                               + " — harness.py experiment list")
                              if stale else "no overdue experiments"})
    except Exception:
        out.append({"id": "experiment-overdue", "status": "ok", "detail": "unreadable"})

    # (7) precommit-gate-hookspath: the SPEC-137 pre-commit validation gate only
    # ENFORCES when core.hooksPath runs THIS repo's tools/git-hooks/pre-commit. The
    # OneDrive twin desync (a separate physical tree, not a junction) makes an
    # absolute hooksPath point at a stale copy with no pre-commit, silently no-op'ing
    # the gate (root-caused 2026-07-13). Warn ONLY when the gate is enabled in policy
    # but not wired — a disabled/absent policy is a non-signal (ok).
    try:
        from harness_lib import validation_stamp
        if validation_stamp.load_policy(root) is None:
            out.append({"id": "precommit-gate-hookspath", "status": "ok",
                        "detail": "pre-commit validation gate not enabled"})
        else:
            proc = subprocess.run(["git", "config", "core.hooksPath"], cwd=str(root),
                                  capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
            hp = proc.stdout.strip() if proc.returncode == 0 else ""
            own_pc = root / "tools" / "git-hooks" / "pre-commit"
            hp_dir = ((root / hp) if not Path(hp).is_absolute() else Path(hp)) if hp else None
            wired = False
            if hp_dir is not None:
                try:
                    wired = (own_pc.exists() and (hp_dir / "pre-commit").exists()
                             and os.path.samefile(str(hp_dir / "pre-commit"), str(own_pc)))
                except OSError:
                    wired = False
            out.append({"id": "precommit-gate-hookspath",
                        "status": "ok" if wired else "warn",
                        "detail": ("core.hooksPath runs this repo's pre-commit; gate enforcing"
                                   if wired else
                                   f"gate ENABLED but core.hooksPath={hp or 'unset'!r} does not run this "
                                   "repo's tools/git-hooks/pre-commit - the gate is NOT enforcing; "
                                   "fix: git config core.hooksPath tools/git-hooks")})
    except Exception:
        out.append({"id": "precommit-gate-hookspath", "status": "ok", "detail": "unreadable"})

    # (8) compaction-invariants: EXP-2 measured Item/Verify dropping out of the
    # post-compact reinjection once NEXT_STEPS.md outgrew the cap; since the
    # 2026-07-23 render-budget package the block's fields always render whole,
    # so a warn here means that guarantee regressed. Advisory only.
    try:
        from harness_lib import context_checkpoint
        lost = context_checkpoint.reinjection_loss(root)
        out.append({"id": "compaction-invariants",
                    "status": "warn" if lost else "ok",
                    "detail": ("post-compact reinjection drops checkpoint field(s): "
                               + ", ".join(lost)
                               + " — NEXT_STEPS.md is over the reinjection cap; trim it "
                                 "or re-stamp with harness.py checkpoint")
                              if lost else "in-flight checkpoint survives reinjection"})
    except Exception:
        out.append({"id": "compaction-invariants", "status": "ok", "detail": "unreadable"})

    # (8b) reinjection-budget: the SessionStart payload measured against the vendor
    # inline budget RIGHT NOW, not at gate time. This check exists because the budget
    # was re-blown between gates twice (2026-07-24 at 9.5k, 2026-07-27 at 9,933 vs a
    # 9,800 ceiling): the hib scenario measures only when the gate runs, while the
    # payload grows with the checkpoint trail every day in between. hib is the
    # gate-time teeth; this is the live counterweight, in the same shape as
    # graph-staleness above — a guarantee that only holds at gate time needs a daily
    # witness or it silently stops holding. WARN means head+protected are crowding the
    # budget (the I2 exile trajectory); a state shave is reported, never warned on —
    # see reinjection_budget_verdict for why that recalibration happened.
    try:
        import importlib.util
        # The hook resolves its OWN root, so it can only speak for this repo; assembling
        # against another tree would answer about the wrong state (and rh-2 requires a
        # bare temp dir to be all-ok). Loading it also pins this process's stdout to
        # utf-8 (the hook's module-level reconfigure) — harmless here, doctor details
        # already carry em dashes.
        if Path(root).resolve() != ROOT.resolve():
            out.append({"id": "reinjection-budget", "status": "ok",
                        "detail": "n/a: the reinjection payload is this repo's own"})
        else:
            _spec = importlib.util.spec_from_file_location(
                "reload_hook", ROOT / "tools" / "hooks" / "reload_context_after_compact.py")
            _hook = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_hook)
            head, state, protected = _hook._assemble()
            # UNTRIMMED (pre-_fit) lengths AS EMITTED: main() prints head + "\n" + the
            # state part + one "\n" per protected part, so the verdict can name the
            # discipline share and the untrimmed total exactly.
            status, detail = reinjection_budget_verdict(
                len(head), len(state), sum(len(p) + 1 for p in protected),
                _hook.TOTAL_BUDGET)
            out.append({"id": "reinjection-budget", "status": status, "detail": detail})
    except Exception:
        out.append({"id": "reinjection-budget", "status": "ok", "detail": "unreadable"})

    # (9) trace-completeness: article Section 8.1 observe-first advisory. An
    # R2/R3 route decision in the recent event tail (riskTier vocabulary shipped
    # fba2fe2) whose evidence chain authorization -> validation is incomplete is
    # an unfinished trajectory. Report-only (doctor observes, never blocks); no
    # R2/R3 rows -> ok (healthy silence).
    try:
        gaps = trace_completeness(root)
        out.append({"id": "trace-completeness",
                    "status": "warn" if gaps else "ok",
                    "detail": ("R2/R3 route decision(s) missing evidence chain: "
                               + "; ".join(f"{g['eventName']}[{g['riskTier']}] missing "
                                           + "+".join(g["missing"]) for g in gaps)
                               + " - trace article Section 8.1")
                              if gaps else "R2/R3 route decisions have a complete evidence chain"})
    except Exception:
        out.append({"id": "trace-completeness", "status": "ok", "detail": "unreadable"})

    # (10) epoch-conflicts: SPEC-149 ownership-epoch advisory. A state writer with
    # NO run epoch (legacy ratchet) marks its target `epochMissing`; a loud fence
    # override lands `epoch_fence_overridden` in the event tail. Report-only — the
    # enforcement is at the write path; the doctor only surfaces the misses so a
    # legacy writer or a forced override does not vanish silently.
    try:
        misses: list[str] = []
        tasks_path = root / ".harness" / "state" / "tasks.json"
        tdoc = _read_json(tasks_path)
        for t in (tdoc.get("tasks", []) if isinstance(tdoc, dict) else []):
            if isinstance(t, dict) and t.get("epochMissing"):
                misses.append("task:" + str(t.get("id")))
        active = root / ".harness" / "workflows" / "active"
        if active.is_dir():
            for wfj in sorted(active.glob("*/workflow.json")):
                wf = _read_json(wfj)
                if isinstance(wf, dict) and wf.get("epochMissing"):
                    misses.append("workflow:" + str(wf.get("workflowId", wfj.parent.name)))
        overrides = _count_events(root, "epoch_fence_overridden")
        flagged = bool(misses) or overrides > 0
        bits: list[str] = []
        if misses:
            bits.append("epochMissing on " + ", ".join(misses[:8]))
        if overrides:
            bits.append(f"{overrides} recent fence override(s)")
        out.append({"id": "epoch-conflicts",
                    "status": "warn" if flagged else "ok",
                    "detail": ("; ".join(bits) + " — SPEC-149 ownership-epoch"
                               if flagged else "no epoch misses or fence overrides observed")})
    except Exception:
        out.append({"id": "epoch-conflicts", "status": "ok", "detail": "unreadable"})

    # SPEC-164 v2 (round-3 consolidation). The durable ledger carries the C+ seal
    # and its own self-chain, so load it once and use it for BOTH checks below.
    ledger: dict = {}
    try:
        _lj = json.loads((root / ".harness" / "state" / "escalations.json").read_text(encoding="utf-8"))
        if isinstance(_lj, dict):
            ledger = _lj
    except (OSError, ValueError):
        ledger = {}
    seal = ledger.get("chainSeal") if isinstance(ledger.get("chainSeal"), dict) else None

    # (11) event-chain-integrity: verify the LIVE events.jsonl with the C+ seal.
    # A legitimate compaction gap is tolerated ONLY when the seal binds the removed
    # hash to its kept successor (verify_event_chain); an unexplained gap
    # (reorder/removal/forge-by-citation) and a content edit both WARN. This is the
    # C+ replacement for the abandoned allow_gaps (which tolerated EVERY gap).
    # absent/empty log -> ok. WARN-only.
    rows: list[dict] = []
    try:
        events_path = root / ".harness" / "runs" / "events.jsonl"
        try:
            text = events_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
        if not rows:
            out.append({"id": "event-chain-integrity", "status": "ok", "detail": "no event log"})
        else:
            from harness import verify_event_chain  # lazy: not circular (loads after bind)
            res = verify_event_chain(rows, seal=seal)
            broken = res.get("ok") is False
            out.append({"id": "event-chain-integrity",
                        "status": "warn" if broken else "ok",
                        "detail": (f"live chain broken at row {res.get('brokenAt')}: {res.get('reason')}"
                                   if broken else
                                   f"chain intact (length {res.get('chainLength', 0)})")})
    except Exception:
        out.append({"id": "event-chain-integrity", "status": "ok", "detail": "unreadable"})

    # (12) durable-ledger-integrity: SPEC-164 v2. The durable ledger (escalations.json)
    # self-chains (stateHash over its content). WARN on a content edit that left the
    # stored stateHash stale; else cross-witness it against the live log's last
    # supervision_compacted row (a careless whole-file ROLLBACK to an older valid
    # ledger has a stateHash the live witness won't match). absent -> ok. WARN-only.
    try:
        if not ledger:
            out.append({"id": "durable-ledger-integrity", "status": "ok", "detail": "no durable ledger"})
        else:
            from harness_lib.escalations_lib import verify_durable_ledger
            if not verify_durable_ledger(ledger):
                out.append({"id": "durable-ledger-integrity", "status": "warn",
                            "detail": "durable ledger stateHash mismatch (edited content)"})
            else:
                witnessed = next((r["durableStateHash"] for r in reversed(rows)
                                  if r.get("event") == "supervision_compacted"
                                  and isinstance(r.get("durableStateHash"), str)), None)
                sh = ledger.get("stateHash")
                if witnessed and isinstance(sh, str) and witnessed != sh:
                    out.append({"id": "durable-ledger-integrity", "status": "warn",
                                "detail": f"durable ledger rolled back: live witness {witnessed[:12]} != ledger {sh[:12]}"})
                else:
                    out.append({"id": "durable-ledger-integrity", "status": "ok",
                                "detail": "durable ledger intact"})
    except Exception:
        out.append({"id": "durable-ledger-integrity", "status": "ok", "detail": "unreadable"})

    # (13) state-authority-map: validate canonical state domain ownership (SPEC-175).

    try:
        from harness_lib import state_authority
        sa_res = state_authority.validate_state_authority(root)
        if not sa_res["ok"]:
            out.append({"id": "state-authority-map", "status": "warn",
                        "detail": f"state authority validation issues: {'; '.join(sa_res['issues'][:3])}"})
        else:
            out.append({"id": "state-authority-map", "status": "ok",
                        "detail": f"state authority intact ({sa_res['domainCount']} domains)"})
    except Exception as e:
        out.append({"id": "state-authority-map", "status": "ok", "detail": f"unreadable: {e}"})

    # (14) commit-tie: every normal non-merge commit since AUDIT_SINCE carries a Tie

    # receipt naming the mechanism that now enforces what it shipped (row
    # overseer-iteration-has-no-orchestration-, 2026-07-27): shipped capabilities tied
    # to nothing, a playbook edited without its lock (b716261), a backlog row closed
    # three commits late (b53c32e). The commit-msg hook blocks at the edge; this is the
    # --no-verify / history-rewrite backstop. WARN-only per the doctor charter — a
    # receipt audit that BLOCKED would just be a second commit gate, and the reason
    # commit-msg fails open on internal error is that doctor is standing behind it.
    try:
        from harness_lib import commit_tie
        # Wiring first: an audit of receipts is worthless while nothing collects them.
        # This arm lives HERE rather than in ct_commit_tie because the scenarios gate
        # runs each shard in a detached worktree copy, where "is the owner's repo
        # wired?" is a question about a repo that process is not in (measured, gate run
        # 2026-07-27 22:42). Doctor runs on the real root, which is the only place the
        # question has an answer — same reasoning, and same shape, as the sibling
        # precommit-gate-hookspath check above.
        hp_proc = subprocess.run(["git", "config", "core.hooksPath"], cwd=str(root),
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=15)
        hp = hp_proc.stdout.strip() if hp_proc.returncode == 0 else ""
        own_hook = root / "tools" / "git-hooks" / "commit-msg"
        hp_dir = ((root / hp) if not Path(hp).is_absolute() else Path(hp)) if hp else None
        wired = False
        if hp_dir is not None and own_hook.exists():
            try:
                wired = ((hp_dir / "commit-msg").exists()
                         and os.path.samefile(str(hp_dir / "commit-msg"), str(own_hook)))
            except OSError:
                wired = False
        if own_hook.exists() and not wired:
            out.append({"id": "commit-tie", "status": "warn",
                        "detail": f"commit-msg hook present but core.hooksPath={hp or 'unset'!r} "
                                  "does not run it — receipts are NOT being collected; "
                                  "fix: git config core.hooksPath tools/git-hooks"})
            return out
        proc = subprocess.run(["git", "rev-list", "--count", "--no-merges",
                               f"--since={commit_tie.AUDIT_SINCE}", "HEAD"],
                              cwd=str(root), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=15)
        # Displayed n is capped at the audit's own window: claiming "3 of 400" when
        # only 20 were read would be a number nobody could act on.
        n = min(int(proc.stdout.strip()), commit_tie.AUDIT_LIMIT) if proc.returncode == 0 else 0
        # The spawn lives HERE, not in commit_tie: that module is imported by the
        # commit-msg hook, which may not reach processes.py, and a new raw-spawn site
        # there fails the no-raw-subprocess ratchet (measured, gate run 22:30). This
        # file already carries baselined git calls; commit_tie owns the format and the
        # parsing, so the two halves cannot drift.
        log = subprocess.run(["git", *commit_tie.LOG_ARGS], cwd=str(root),
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=15)
        bad = commit_tie.audit_records(log.stdout) if log.returncode == 0 else []
        if n == 0:
            out.append({"id": "commit-tie", "status": "ok",
                        "detail": f"no commits after the audit anchor ({commit_tie.AUDIT_SINCE})"})
        elif not bad:
            out.append({"id": "commit-tie", "status": "ok",
                        "detail": f"{n} audited commit(s) since {commit_tie.AUDIT_SINCE} "
                                  "carry Tie receipts"})
        else:
            out.append({"id": "commit-tie", "status": "warn",
                        "detail": f"{len(bad)} of {n} commit(s) since {commit_tie.AUDIT_SINCE} "
                                  "missing/malformed Tie receipt: "
                                  + ", ".join(b["sha"][:8] for b in bad[:5])
                                  + (" ..." if len(bad) > 5 else "")
                                  + " — --no-verify bypass or rewrite; amend, or carry "
                                    "the reason in the next commit"})
    except Exception:
        out.append({"id": "commit-tie", "status": "ok", "detail": "unreadable"})
    return out


def cmd_doctor(args) -> int:
    """`harness.py doctor`: print one line per check; WARN-only, always rc 0."""
    for item in checks(ROOT):
        print(f"{item['status'].upper():<4} {item['id']}: {item['detail']}")
    return 0
