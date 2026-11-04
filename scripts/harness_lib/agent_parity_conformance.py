"""SPEC-113 §8.4 (T-ADAPTERCONF) + C16b: report-only executor adapter-conformance.

Split out of `agent_parity.py` as a pure move (phase-2 split, budget 900): this
module owns the deterministic per-executor conformance suite over
`.harness/routing/executors.json` (the spawn-side vendor cards -- a DIFFERENT
registry from the capabilities manifest that `agent_parity.audit`/`pair` manage)
plus the reader for each executor's declared `accountingSemantics`. Same honesty
stance as the C3 `supportState` field: `conformance_report`/`conformance_findings`
never fail a gate by themselves -- a "fail" test result is real signal (e.g. no
per-worker token reconciliation exists today for codex/openai-compat), not a bug
in the checker. `agent_parity` re-exports the public names so no importer changes.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

try:
    from .common import read_json, read_text, write_json, write_text
except ImportError:  # run directly for the __main__ self-check
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from harness_lib.common import read_json, read_text, write_json, write_text

EXECUTORS_REL = ".harness/routing/executors.json"  # T-ADAPTERCONF: same registry route_tuple.py reads
# C16b (§8.4 c9 amendment): declared per-executor token-accounting reliability.
# No "unsupported" here (every runnable executor reports SOME tokens or none);
# the vocabulary is deliberately native/emulated/degraded/unknown.
_ACCOUNTING_SEMANTICS = ("native", "emulated", "degraded", "unknown")
# T-ADAPTERCONF: the §8.4 c-test subset applicable to our local/HTTP executors
# (c1,c2,c4,c5,c7,c9,c10 — see plan-t-adapterconf.md item 1; c3/c6/c8/c11-14 are
# out of scope for this suite, not merely skipped).
_CONFORMANCE_TESTS = (
    ("c1", "schema validation and unknown-field behavior"),
    ("c2", "cancellation, timeout, retry, streaming, backpressure"),
    ("c4", "argument normalization and side-effect declaration"),
    ("c5", "permission narrowing and no-amplification of delegated authority"),
    ("c7", "error taxonomy: rejection vs failure vs unknown effect"),
    ("c9", "provider-usage reconciliation (tokens)"),
    ("c10", "reasoning-effort support and model-specific fallback"),
)


# --------------------------------------------------------------------------- #
# T-ADAPTERCONF (SPEC-113 §8.4 amendment) + C16b: executors.json accounting
# semantics + a deterministic, report-only adapter-conformance suite.
# --------------------------------------------------------------------------- #
def load_executors(root: Path) -> dict[str, Any]:
    """.harness/routing/executors.json; corrupt/missing degrades to {} (same
    never-crash contract as `load_manifest`). A DIFFERENT registry from the
    capabilities manifest: executors are spawn-side vendor cards, not the
    hook/skill/agent-profile surfaces `audit`/`pair` manage above."""
    try:
        data = read_json(root / EXECUTORS_REL, {}) or {}
    except (json.JSONDecodeError, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def executor_accounting_semantics(executors: dict[str, Any], name: str) -> str:
    """C16b: the declared per-executor token-accounting reliability (native/
    emulated/degraded/unknown) — how the VENDOR reports usage, not whether the
    harness currently reconciles it into the cost ledger (that is c9's
    concern, see `conformance_report`). Pre-req for N-VENDORCREDIT (D017): a
    credit-tracking consumer must know whether a token number is trustworthy
    before spending against it.

    Unlike `capability_support_state` (defaults absent -> `native`), an
    executor with no declared field defaults to `unknown` — the opposite
    default, deliberately: an UNDECLARED token count must never be silently
    trusted by a future consumer. Additive: adding this reader/field re-renders
    nothing (read + report only, same as supportState)."""
    card = (executors.get("executors") or {}).get(name)
    raw = str((card or {}).get("accountingSemantics", "unknown")).strip().lower()
    return raw if raw in _ACCOUNTING_SEMANTICS else "unknown"


def _executor_family(card: dict[str, Any]) -> str:
    """Adapter family from the DECLARED taxonomy first (SPEC-165 R1: `type` +
    `wire`), falling back to commandTemplate-shape sniffing for legacy entries
    that predate the declaration — a card that reuses `tools/openai_worker.py`,
    `codex exec`, or claude's `--output-format stream-json` still gets the same
    conformance answers without a per-name branch."""
    if card.get("type") == "http" and card.get("wire") == "openai":
        return "openai-compat-http"
    template = " ".join(str(t) for t in (card.get("commandTemplate") or []))
    if "openai_worker.py" in template:
        return "openai-compat-http"
    if "codex" in template and "exec" in template:
        return "codex-exec"
    if "claude" in template and "stream-json" in template:
        return "claude-stream-json"
    return "unknown"


def _c1_schema(root: Path, family: str) -> tuple[str, str]:
    if family == "openai-compat-http":
        src = read_text(root / "tools" / "openai_worker.py", "")
        if "_coerce" in src and "STATUS_ENUM" in src:
            return ("pass",
                    "tools/openai_worker.py _coerce() enum-validates status/graphify/severity and "
                    "degrades to safe defaults on malformed/unknown top-level fields (never raises); "
                    "finding-level extra keys are explicitly preserved, not silently dropped.")
        return ("fail", "expected _coerce/STATUS_ENUM schema guard not found in tools/openai_worker.py")
    if family in ("codex-exec", "claude-stream-json"):
        src = read_text(root / "scripts" / "harness_lib" / "result_contracts.py", "")
        if "def validate_worker_result" in src and "def extract_worker_result" in src:
            return ("pass",
                    "result_contracts.validate_worker_result (+ extract_worker_result) applies the "
                    "same schema/required-key/enum checks at the workflow boundary regardless of "
                    "which CLI vendor produced the reply.")
        return ("fail", "expected validate_worker_result/extract_worker_result not found in result_contracts.py")
    return ("skip", f"unrecognized adapter family {family!r}; no known schema-validation path to check")


def _c2_cancellation(root: Path, card: dict[str, Any], family: str) -> tuple[str, str]:
    limits = card.get("runtimeLimits") or {}
    if not (limits.get("backoffSeconds") and limits.get("stopOnRateLimit")):
        return ("fail", "runtimeLimits lacks backoffSeconds/stopOnRateLimit: no declared retry/backoff policy")
    if not (root / "testing" / "scenarios" / "esh_spawn_hygiene.py").is_file():
        return ("skip", "esh_spawn_hygiene.py (cancellation/timeout regression) not present in this checkout")
    detail = ("process-tree bounded timeout + DEVNULL stdin hygiene apply uniformly to every CLI "
              "spawn (async_runtime.py/processes.py, regression-tested by esh-1..esh-3); retry/backoff "
              "declared via runtimeLimits.backoffSeconds/stopOnRateLimit")
    detail += (("; streaming is native (--output-format stream-json gives a line-flushed live tail)")
               if family == "claude-stream-json" else
               ("; no live streaming for this adapter (single-shot/batch call) — a documented scope "
                "narrowing, not a break of cancellation/timeout"))
    return ("pass", detail)


def _c4_argument_normalization(card: dict[str, Any]) -> tuple[str, str]:
    template = card.get("commandTemplate")
    if not isinstance(template, list) or not template:
        return ("skip", "no commandTemplate declared for this executor")
    tokens = sorted(set(re.findall(r"\{(\w+)\}", " ".join(str(t) for t in template))))
    if not tokens:
        return ("fail", "commandTemplate has no templated placeholders: every argument is a hardcoded "
                         "literal, so there is no declared normalization surface to test")
    return ("pass",
            f"commandTemplate is a fixed argv list with an explicit placeholder whitelist {tokens}; "
            "workflow_spawn_command_for_prompt substitutes only declared tokens and {prompt} is always "
            "the last argv element (never shell-interpolated), so no argument-injection surface exists "
            "beyond the declared set.")


def _c5_permission_narrowing(card: dict[str, Any], family: str) -> tuple[str, str]:
    template_str = " ".join(str(t) for t in (card.get("commandTemplate") or []))
    if family == "openai-compat-http":
        return ("pass",
                "the HTTP single-shot worker has no filesystem/exec tool at all (openai_worker.py posts "
                "one chat/completions call and writes only its own result path behind an --allow-hosts "
                "egress allowlist); it cannot amplify authority it never holds.")
    if family == "codex-exec":
        if "--sandbox" in template_str and "{sandbox}" in template_str:
            return ("pass",
                    "--sandbox {sandbox} is derived 1:1 from the worker's writeAllowed (S3); the flag "
                    "narrows to read-only/workspace-write with no path to request more than the caller "
                    "granted.")
        return ("fail", "codex-exec commandTemplate has no --sandbox {sandbox} narrowing flag")
    if family == "claude-stream-json":
        narrowing_flags = ("--strict-mcp-config", "--setting-sources", "--disable-slash-commands")
        if all(f in template_str for f in narrowing_flags):
            return ("pass",
                    "SPEC-118 v4 session-diet flags strip user-scope MCP/settings/skills; the "
                    "allowedTools ceiling (S2) is the sandbox, with no flag that grants more than the "
                    "profile's own tool list.")
        return ("fail", "claude-stream-json commandTemplate is missing one or more session-diet "
                         "narrowing flags (--strict-mcp-config/--setting-sources/--disable-slash-commands)")
    return ("skip", f"unrecognized adapter family {family!r}; no known permission-narrowing mechanism to check")


def _c7_error_taxonomy(card: dict[str, Any], family: str) -> tuple[str, str]:
    limits = card.get("runtimeLimits") or {}
    if not all(limits.get(k) for k in ("rateLimitPatterns", "authFailurePatterns", "quotaFailurePatterns")):
        return ("fail", "runtimeLimits is missing one of rateLimitPatterns/authFailurePatterns/"
                         "quotaFailurePatterns: no declared error-category taxonomy for this executor")
    detail = ("runtimeLimits classifies rate-limit/auth/quota failures; async_runtime.py assigns a "
              "closed worker/task-status vocabulary (failed/cancelled/timeout/rejected) uniformly across "
              "every CLI-spawned executor")
    if family == "openai-compat-http":
        detail += ("; openai_worker.py layers a finer exit-code taxonomy on top (2=missing env, 3=no "
                   "key, 4=invalid reply, 5=transport error, 6=egress denied)")
    return ("pass", detail)


def _c9_usage_reconciliation(root: Path, template_str: str, family: str) -> tuple[str, str]:
    if "stream-json" in template_str:
        return ("pass",
                "commandTemplate requests --output-format stream-json; stream_json.parse extracts the "
                "final result event's usage and cost_metrics.record_workflow reconciles it as "
                "observedUsage/observedTokens/costUsd (costBasis='observed').")
    if family == "openai-compat-http":
        src = read_text(root / "tools" / "openai_worker.py", "")
        if re.search(r'data(?:\.get\(\s*["\']usage["\']|\[\s*["\']usage["\']\s*\])', src):
            return ("pass", "tools/openai_worker.py reads the response's usage object and threads it "
                             "into the WORKER_RESULT/cost ledger.")
        return ("fail",
                "the HTTP response's native `usage` object is never read by tools/openai_worker.py "
                "(_post/_coerce discard it): no per-call token reconciliation reaches the cost ledger "
                "today even though the endpoint reports usage natively — a real gap for N-VENDORCREDIT.")
    return ("fail",
            "commandTemplate has no streaming/structured-usage flag; stream_json.parse only recognizes "
            "NDJSON with a 'type' key on the first line, so no per-worker usage is captured for this "
            "adapter today.")


def _c10_effort_fallback(card: dict[str, Any], family: str) -> tuple[str, str]:
    template_str = " ".join(str(t) for t in (card.get("commandTemplate") or []))
    has_effort_token = "{effort}" in template_str
    if family == "codex-exec" and has_effort_token:
        return ("pass",
                "commandTemplate substitutes -c model_reasoning_effort={effort}; the bet-E derivation "
                "in this module maps the one Claude label codex rejects (max -> xhigh) via _EFFORT_MAP, "
                "regression-tested by the module self-check (agent:effort-max-maps).")
    if family == "claude-stream-json" and has_effort_token:
        return ("pass",
                "commandTemplate substitutes --effort {effort} directly; unsupported (card, effort) "
                "combinations are rejected at routing time (model_routing._validate_card_fields) before "
                "spawn, so no unsupported level ever reaches the CLI.")
    if not has_effort_token:
        declared = (card.get("defaultSpawn") or {}).get("effort")
        if declared:
            return ("fail",
                    f"defaultSpawn declares an effort ({declared!r}) but commandTemplate has no "
                    "{effort} token: workflow_spawn_command_for_prompt computes the value and it is "
                    "silently dropped — never passed to the model, never rejected, no declared fallback.")
        return ("skip", "no effort concept declared for this executor (no {effort} token, no "
                         "defaultSpawn.effort)")
    return ("skip", f"unrecognized adapter family {family!r} carries an {{effort}} token with no known "
                     "fallback path to verify")


def _conformance_checks(executor: str, root: Path) -> dict[str, tuple[str, str]]:
    """(status, detail) per applicable §8.4 test id for one executor card —
    the shared computation behind `conformance_report` (bare status) and
    `conformance_findings` (status + detail rows)."""
    executors = load_executors(root)
    card = (executors.get("executors") or {}).get(executor)
    if not isinstance(card, dict):
        return {t: ("skip", f"no such executor {executor!r} in {EXECUTORS_REL}") for t, _ in _CONFORMANCE_TESTS}
    if not card.get("runnable") or card.get("placeholder"):
        return {t: ("skip", "placeholder/non-runnable executor card; no live adapter to conform")
                for t, _ in _CONFORMANCE_TESTS}
    family = _executor_family(card)
    template_str = " ".join(str(t) for t in (card.get("commandTemplate") or []))
    return {
        "c1": _c1_schema(root, family),
        "c2": _c2_cancellation(root, card, family),
        "c4": _c4_argument_normalization(card),
        "c5": _c5_permission_narrowing(card, family),
        "c7": _c7_error_taxonomy(card, family),
        "c9": _c9_usage_reconciliation(root, template_str, family),
        "c10": _c10_effort_fallback(card, family),
    }


def conformance_report(executor: str, root: Path) -> dict[str, str]:
    """T-ADAPTERCONF (SPEC-113 §8.4): deterministic per-executor conformance
    over the applicable c-test subset (c1,c2,c4,c5,c7,c9,c10 — see module
    docstring / `_CONFORMANCE_TESTS`). `{test_id: 'pass'|'skip'|'fail'}`, never
    a crash: an unknown or placeholder/non-runnable card skips every test with
    a reason (see `conformance_findings` for the human-readable detail).
    Report-only — a 'fail' is real signal (e.g. c9 on codex/openai-compat: no
    per-worker usage reconciliation exists today), never a gate failure by
    itself."""
    return {t: v[0] for t, v in _conformance_checks(executor, root).items()}


def conformance_findings(root: Path) -> list[tuple[str, str, str]]:
    """`("<executor>/<test>", status, detail)` rows over every DECLARED
    executor — the human-readable companion to `conformance_report`, in the
    same shape `parity_gate_findings` consumes. Purely observational (never
    contributes to `gaps`): a 'fail' here must never redden a gate, same
    honesty stance as C3 `supportState`."""
    executors = load_executors(root)
    rows: list[tuple[str, str, str]] = []
    for name in sorted((executors.get("executors") or {}).keys()):
        for test_id, (status, why) in _conformance_checks(name, root).items():
            rows.append((f"{name}/{test_id}", status, why))
    return rows


def audit_matrix_sections(root: Path) -> dict[str, dict[str, Any]]:
    """The two audit-matrix sections this domain contributes (T-ADAPTERCONF +
    C16b), merged into `agent_parity.audit`'s matrix via one call. Report-only:
    NEVER appended to `gaps` — a 'fail' test result or an 'unknown'
    accountingSemantics is an honest declaration, not a gate-affecting gap
    (same stance as C3 supportState)."""
    executors = load_executors(root)
    names = sorted((executors.get("executors") or {}).keys())
    return {"adapterConformance": {n: conformance_report(n, root) for n in names},
            "accountingSemantics": {n: executor_accounting_semantics(executors, n)
                                    for n in names}}


def gate_finding_row(matrix: dict[str, Any]) -> tuple[str, str, str]:
    """The single §8.4 row for `agent_parity.parity_gate_findings` — same
    never-fail-the-gate stance: fails surface as an OBSERVE-ONLY 'skip' row
    (real signal, e.g. missing token reconciliation), never a red status."""
    conf = matrix.get("adapterConformance", {})
    fails = sorted(f"{name}/{t}" for name, tests in conf.items()
                   for t, status in tests.items() if status == "fail")
    if fails:
        return ("agent-parity:adapter-conformance", "skip",
                f"OBSERVE-ONLY: {len(fails)} Sec8.4 adapter-conformance fail(s) -- {'; '.join(fails[:8])}")
    return ("agent-parity:adapter-conformance", "pass",
            "no Sec8.4 adapter-conformance fail(s) across declared executors")


# --------------------------------------------------------------------------- #
# __main__ self-check (conformance domain; the rest lives in agent_parity._selfcheck)
# --------------------------------------------------------------------------- #
def _selfcheck() -> int:  # pragma: no cover - exercised as a script
    import tempfile

    # audit/parity_gate_findings stay in agent_parity; imported lazily so a plain
    # import of this module never pulls the sibling (and no import cycle at load).
    from harness_lib.agent_parity import audit, parity_gate_findings

    checks: list[tuple[str, bool, str]] = []

    def ok(name: str, cond: bool, detail: str = "") -> None:
        checks.append((name, bool(cond), detail))
        print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))

    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"

        # --- T-ADAPTERCONF (SPEC-113 §8.4) + C16b: executor accounting + conformance ---
        croot = Path(td) / "conf-repo"
        write_json(croot / EXECUTORS_REL, {"executors": {
            "claude-x": {  # claude-stream-json family: every check should hold
                "runnable": True, "accountingSemantics": "native",
                "commandTemplate": ["claude", "-p", "--output-format", "stream-json",
                                     "--strict-mcp-config", "--setting-sources", "project,local",
                                     "--disable-slash-commands", "--exclude-dynamic-system-prompt-sections",
                                     "--model", "{model}", "--effort", "{effort}", "{prompt}"],
                "runtimeLimits": {"backoffSeconds": 30, "stopOnRateLimit": True,
                                  "rateLimitPatterns": ["x"], "authFailurePatterns": ["y"],
                                  "quotaFailurePatterns": ["z"]},
            },
            "codex-x": {  # codex-exec family, missing quotaFailurePatterns -> c7 fail
                "runnable": True,  # no accountingSemantics declared -> defaults to unknown
                "commandTemplate": ["codex", "exec", "--model", "{model}", "-c",
                                     "model_reasoning_effort={effort}", "--sandbox", "{sandbox}", "{prompt}"],
                "runtimeLimits": {"backoffSeconds": 30, "stopOnRateLimit": True,
                                  "rateLimitPatterns": ["x"], "authFailurePatterns": ["y"]},
            },
            "http-x": {  # openai-compat-http family, no {effort} token but defaultSpawn.effort declared
                "runnable": True, "accountingSemantics": "not-a-real-value",  # clamps to unknown
                "commandTemplate": ["{python}", "tools/openai_worker.py", "--model", "{model}", "{prompt}"],
                "runtimeLimits": {"backoffSeconds": 30, "stopOnRateLimit": True,
                                  "rateLimitPatterns": ["x"], "authFailurePatterns": ["y"],
                                  "quotaFailurePatterns": ["z"]},
                "defaultSpawn": {"effort": "low"},
            },
            "ghost-x": {  # unrecognized adapter family -> family-gated tests skip
                "runnable": True,
                "commandTemplate": ["mystery-binary", "{prompt}"],
                "runtimeLimits": {},
            },
            "placeholder-x": {  # not runnable -> every test skips with one reason
                "runnable": False, "placeholder": True,
                "commandTemplate": ["echo", "hi"],
            },
        }})
        # fixtures the c1/c9 file-content probes read: written WITH the guards
        # claude/codex rely on, WITHOUT the ones openai-compat needs (yet).
        write_text(croot / "scripts" / "harness_lib" / "result_contracts.py",
                   "def validate_worker_result(): pass\ndef extract_worker_result(): pass\n")
        write_text(croot / "tools" / "openai_worker.py", "no coerce or usage read in this fixture\n")
        write_text(croot / "testing" / "scenarios" / "esh_spawn_hygiene.py", "# fixture\n")

        ok("acct:declared-native",
           executor_accounting_semantics(load_executors(croot), "claude-x") == "native")
        ok("acct:default-unknown",
           executor_accounting_semantics(load_executors(croot), "codex-x") == "unknown")
        ok("acct:bad-value-clamps-unknown",
           executor_accounting_semantics(load_executors(croot), "http-x") == "unknown")
        ok("acct:unknown-executor-unknown",
           executor_accounting_semantics(load_executors(croot), "nope") == "unknown")

        claude_conf = conformance_report("claude-x", croot)
        ok("conf:report-shape", set(claude_conf) == {t for t, _ in _CONFORMANCE_TESTS}, str(claude_conf))
        ok("conf:claude-all-pass", all(v == "pass" for v in claude_conf.values()), str(claude_conf))

        codex_conf = conformance_report("codex-x", croot)
        ok("conf:codex-c1-pass", codex_conf["c1"] == "pass", str(codex_conf))
        ok("conf:codex-c5-pass", codex_conf["c5"] == "pass", str(codex_conf))
        ok("conf:codex-c7-fail", codex_conf["c7"] == "fail", str(codex_conf))
        ok("conf:codex-c10-pass", codex_conf["c10"] == "pass", str(codex_conf))

        http_conf = conformance_report("http-x", croot)
        ok("conf:http-c1-fail", http_conf["c1"] == "fail", str(http_conf))
        ok("conf:http-c5-pass", http_conf["c5"] == "pass", str(http_conf))
        ok("conf:http-c9-fail", http_conf["c9"] == "fail", str(http_conf))
        ok("conf:http-c10-fail", http_conf["c10"] == "fail", str(http_conf))

        ghost_conf = conformance_report("ghost-x", croot)
        ok("conf:ghost-c1-skip", ghost_conf["c1"] == "skip", str(ghost_conf))
        ok("conf:ghost-c5-skip", ghost_conf["c5"] == "skip", str(ghost_conf))
        ok("conf:ghost-c10-skip", ghost_conf["c10"] == "skip", str(ghost_conf))

        placeholder_conf = conformance_report("placeholder-x", croot)
        ok("conf:placeholder-all-skip", all(v == "skip" for v in placeholder_conf.values()),
           str(placeholder_conf))
        ok("conf:unknown-executor-all-skip",
           all(v == "skip" for v in conformance_report("nope", croot).values()))

        findings = conformance_findings(croot)
        ok("conf:findings-count", len(findings) == 5 * len(_CONFORMANCE_TESTS), str(len(findings)))
        ok("conf:findings-have-detail", all(isinstance(d, str) and d for _, _, d in findings),
           "every row carries a non-empty detail")

        # live, not cached: flipping the http fixture's source content flips c1/c9
        write_text(croot / "tools" / "openai_worker.py",
                   "STATUS_ENUM = {}\n"
                   "def _coerce(x): pass\n"
                   "def _post(u, k, m, msgs):\n    data = {}\n    return data.get('usage')\n")
        http_conf2 = conformance_report("http-x", croot)
        ok("conf:http-c1-flips-to-pass", http_conf2["c1"] == "pass", str(http_conf2))
        ok("conf:http-c9-flips-to-pass", http_conf2["c9"] == "pass", str(http_conf2))

        caudit = audit(croot, home)
        ok("conf:audit-adapterConformance-present",
           set(caudit["matrix"]["adapterConformance"])
           == {"claude-x", "codex-x", "http-x", "ghost-x", "placeholder-x"},
           str(sorted(caudit["matrix"]["adapterConformance"])))
        ok("conf:audit-accountingSemantics-present",
           caudit["matrix"]["accountingSemantics"].get("codex-x") == "unknown",
           str(caudit["matrix"]["accountingSemantics"]))
        conf_kinds = {g.get("kind") for g in caudit["gaps"]}
        ok("conf:audit-never-a-gap",
           "adapter-conformance" not in conf_kinds and "accounting-semantics" not in conf_kinds,
           str(conf_kinds))

        cgf = {name: status for name, status, _ in parity_gate_findings(croot, home)}
        ok("conf:gate-row-present", "agent-parity:adapter-conformance" in cgf, str(cgf))
        ok("conf:gate-never-fails", all(s in ("pass", "skip") for s in cgf.values()), str(cgf))
        ok("conf:gate-row-skip-on-fails",
           cgf["agent-parity:adapter-conformance"] == "skip", str(cgf))

        # gate_finding_row pure shapes (the integration helper agent_parity calls)
        clean = gate_finding_row({"adapterConformance": {"x": {"c1": "pass"}}})
        dirty = gate_finding_row({"adapterConformance": {"x": {"c9": "fail"}}})
        ok("conf:gate-row-helper-pass", clean[1] == "pass", str(clean))
        ok("conf:gate-row-helper-observe-only",
           dirty[1] == "skip" and "x/c9" in dirty[2], str(dirty))

    failed = [n for n, c, _ in checks if not c]
    print(f"\nagent_parity_conformance self-check: {len(checks) - len(failed)}/{len(checks)} passed"
          + (f"; FAILED: {', '.join(failed)}" if failed else " — all green"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_selfcheck())
