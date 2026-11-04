"""SPEC-110 HT: the proportional gate, parameterized by subject root.

Generic structural checks that apply to ANY codebase (the harness applies them
to itself through its own gate; this module applies the same discipline to
registered target repositories) plus the target's own build/test/lint commands
from its profile. Offline by policy — generic checks never touch the network.

Shared gate helpers (result, run_bounded_command) arrive via bind(env) from
spec_test_gate, the same idiom as the gate_fixtures modules.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from harness_lib import security_baseline
from harness_lib import targets as targets_lib
from harness_lib.common import HARNESS, now, read_json, write_json
from harness_lib.processes import filter_spawn_env


def bind(env: dict[str, Any]) -> None:
    """Receive shared gate helpers from scripts/spec_test_gate.py."""
    globals().update(env)


MAX_SCAN_FILES = 400
TEXT_SUFFIXES = {".md", ".json", ".yaml", ".yml", ".py", ".toml", ".cfg", ".ini"}


def _tree(root: Path, suffixes: set[str], cap: int = MAX_SCAN_FILES) -> list[Path]:
    out: list[Path] = []
    for path in root.rglob("*"):
        if len(out) >= cap:
            break
        if path.is_file() and path.suffix.lower() in suffixes \
                and not any(part in targets_lib.EXCLUDE_PARTS for part in path.parts):
            out.append(path)
    return out


def check_syntax(root: Path, prefix: str) -> list[dict[str, str]]:
    """JSON parse + light YAML sanity over the subject tree."""
    out: list[dict[str, str]] = []
    bad_json: list[str] = []
    for path in _tree(root, {".json"}):
        try:
            json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except json.JSONDecodeError as exc:
            bad_json.append(f"{path.relative_to(root).as_posix()}: {exc.msg} (line {exc.lineno})")
    out.append(result(f"{prefix}:json-syntax", "pass" if not bad_json else "fail",
                      "ok" if not bad_json else "; ".join(bad_json[:5])))
    bad_yaml: list[str] = []
    for path in _tree(root, {".yaml", ".yml"}):
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if "\t" in line.split("#", 1)[0]:
                bad_yaml.append(f"{path.relative_to(root).as_posix()}:{lineno} tab in indentation")
                break
    out.append(result(f"{prefix}:yaml-syntax", "pass" if not bad_yaml else "fail",
                      "ok" if not bad_yaml else "; ".join(bad_yaml[:5])))
    return out


def check_python_compile(entry: dict[str, Any], prefix: str) -> list[dict[str, str]]:
    import py_compile

    errors: list[str] = []
    root = targets_lib.target_root(entry)
    files = [p for p in targets_lib.source_files(entry) if p.suffix == ".py"]
    for path in files[:MAX_SCAN_FILES]:
        try:
            py_compile.compile(str(path), doraise=True, cfile=os.devnull)
        except py_compile.PyCompileError as exc:
            errors.append(f"{path.relative_to(root).as_posix()}: {str(exc.msg).splitlines()[0][:160]}")
        except OSError:
            continue
    return [result(f"{prefix}:python-compile", "pass" if not errors else "fail",
                   f"{len(files)} files ok" if not errors else "; ".join(errors[:5]))]


def check_markdown_links(root: Path, prefix: str) -> list[dict[str, str]]:
    """Relative links in the subject's markdown must resolve inside the subject."""
    import re

    broken: list[str] = []
    for path in _tree(root, {".md"}, cap=200):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in re.finditer(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", text):
            raw = match.group(1)
            if raw.startswith(("http://", "https://", "mailto:")) or raw.startswith("<"):
                continue
            candidate = (path.parent / raw).resolve()
            try:
                candidate.relative_to(root.resolve())
            except ValueError:
                continue  # points outside the subject; not this check's business
            if not candidate.exists():
                broken.append(f"{path.relative_to(root).as_posix()} -> {raw}")
    return [result(f"{prefix}:markdown-links", "pass" if not broken else "fail",
                   "ok" if not broken else "; ".join(broken[:5]))]


def check_line_budgets(entry: dict[str, Any], prefix: str) -> list[dict[str, str]]:
    info = targets_lib.inspect(entry)
    offenders = info.get("overBudget", [])
    detail = (f"all {info['sourceFiles']} files within {info['maxFileLines']} lines"
              if not offenders else
              "; ".join(f"{o['path']}={o['lines']}" for o in offenders[:5])
              + f" | fix: extract cohesive modules (same critique the harness applies to itself); budget maxFileLines={info['maxFileLines']}")
    return [result(f"{prefix}:file-line-budget", "pass" if not offenders else "fail", detail)]


def check_hygiene_lite(entry: dict[str, Any], prefix: str) -> list[dict[str, str]]:
    """Committed junk only (working trees legitimately contain __pycache__)."""
    root = targets_lib.target_root(entry)
    try:
        proc = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        if proc.returncode != 0:
            return [result(f"{prefix}:hygiene-lite", "skip", "not a git repository — committed-junk scan skipped")]
        tracked = proc.stdout.splitlines()
    except Exception:
        return [result(f"{prefix}:hygiene-lite", "skip", "git unavailable — committed-junk scan skipped")]
    junk = [rel for rel in tracked
            if rel.endswith(".pyc") or "__pycache__" in rel or rel.startswith("tmp-") or rel.endswith(".env")]
    return [result(f"{prefix}:hygiene-lite", "pass" if not junk else "fail",
                   f"{len(tracked)} tracked files clean" if not junk else
                   "; ".join(junk[:5]) + " | fix: git rm --cached and extend the target's .gitignore")]


MARKET_INSTRUCTION_FILES = [
    "CLAUDE.md", "AGENTS.md", "GEMINI.md", ".github/copilot-instructions.md",
    ".windsurfrules", ".roorules", "CLAUDE.local.md",
]


def check_protected_instructions(entry: dict[str, Any], prefix: str) -> list[dict[str, str]]:
    """T6: the target's agent-instruction files get the same drift protection the
    harness gives its own canonical files — snapshot-if-present, sha256 compare.

    First run baselines silently; later runs fail on drift with the sanctioned
    re-baseline command in the fix line.
    """
    import hashlib

    from harness_lib import targets as targets_lib
    root = targets_lib.target_root(entry)
    watched = [*MARKET_INSTRUCTION_FILES, *(entry.get("protectedFiles") or [])]
    current: dict[str, str] = {}
    for rel in watched:
        path = root / rel
        if path.is_file():
            current[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    snapshot_path = targets_lib.state_dir(entry["name"]) / "protected-files.snapshot.json"
    previous = read_json(snapshot_path, None)
    if previous is None:
        write_json(snapshot_path, {"schemaVersion": "1.0", "target": entry["name"], "files": current})
        return [result(f"{prefix}:protected-instructions", "pass",
                       f"baseline snapshot created for {len(current)} instruction file(s)")]
    drift = [rel for rel in set(previous.get("files", {})) | set(current)
             if previous.get("files", {}).get(rel) != current.get(rel)]
    if drift:
        return [result(f"{prefix}:protected-instructions", "fail",
                       "; ".join(sorted(drift)[:5])
                       + f" | fix: review the diff in the target; if intentional, delete {snapshot_path} to re-baseline")]
    return [result(f"{prefix}:protected-instructions", "pass", f"{len(current)} instruction file(s) match the snapshot")]


def run_target_commands(entry: dict[str, Any], gate: str, prefix: str) -> list[dict[str, str]]:
    """The target's own build/test/lint commands, executed inside the target."""
    commands = ((entry.get("validation") or {}).get(gate) or {}).get("commands") or []
    if not commands:
        return [result(f"{prefix}:commands:{gate}", "skip", "no commands configured for this gate in target.json")]
    out: list[dict[str, str]] = []
    timeout = int(os.environ.get("HARNESS_GATE_TIMEOUT", "900"))
    root = targets_lib.target_root(entry)
    # Cross-project isolation (Phase 3): deny-by-default env. The target's own
    # build/test/lint commands see ONLY the OS base allowlist + this target's
    # declared requiresEnv + its subject coordinates — never the full harness
    # environment (every API key, other targets' secrets). Same least-privilege
    # composition the workflow-worker path uses (harness.build_worker_spawn_env).
    # Escape hatch: project.json workflows.workerEnvFilter = false.
    _wf_cfg = (read_json(HARNESS / "project.json", {}) or {}).get("workflows") or {}
    env = filter_spawn_env(
        os.environ,
        {"HARNESS_TARGET_NAME": entry["name"], "HARNESS_TARGET_ROOT": str(root)},
        extra=targets_lib.spawn_env(entry),
        enabled=_wf_cfg.get("workerEnvFilter", True) is not False)
    for cmd in commands:
        print(f"$ [{entry['name']}] {cmd}")
        proc = run_bounded_command(cmd, timeout=timeout, shell=True, cwd=str(root),
                                   env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        tail = (proc.stdout or "").strip().splitlines()[-1:] or [""]
        detail = tail[0]
        if proc.returncode == 124:
            detail = f"timeout after {timeout}s"
        elif proc.returncode != 0:
            detail += f" | fix: run `{cmd}` inside {root} and repair the failure before committing to this target"
        out.append(result(f"{prefix}:command:{cmd}", "pass" if proc.returncode == 0 else "fail", detail[:400]))
    return out


def _ensure_target_graph_fresh(name: str, root: Path, prefix: str) -> dict[str, str]:
    """QA.4: the same graph-freshness rule the self gate applies, per subject —
    graph.json missing, unkeyed, or built from different bytes => rebuild
    inline. The rule lives ONCE in graph_providers.staleness, which delegates
    to graphify_code_ast.freshness (content key, not mtime)."""
    from harness_lib import graph_providers
    out_dir = targets_lib.out_dir(name)
    graph = out_dir / "graph.json"
    try:
        if not graph_providers.staleness(root, graph)["stale"]:
            return result(f"{prefix}:graph-freshness", "pass", "target graph is fresh")
        graph_providers.build(root, output_dir=out_dir, target_entry=None)
        return result(f"{prefix}:graph-freshness", "pass", "target graph rebuilt inline (was stale/missing)")
    except Exception as exc:
        return result(f"{prefix}:graph-freshness", "fail", f"rebuild failed: {type(exc).__name__}: {exc}")


def check_security_baseline(entry: dict[str, Any], prefix: str) -> tuple[dict[str, str], bool]:
    """Dual-subject security baseline (security-baseline-targets): the SAME
    evaluator the harness runs on itself, observe-only, with the snapshot on
    the HARNESS side (`state_dir(name)/security-baseline.snapshot.json`) so
    the target tree gains no file, and a tree-wide sink scan (targets have no
    scripts/ convention). Returns (row, requires_security_review)."""
    name = entry["name"]
    root = targets_lib.target_root(entry)
    try:
        snap = targets_lib.state_dir(name) / "security-baseline.snapshot.json"
        report = security_baseline.evaluate(root, snapshot_path=snap, sink_scope="tree")
        new = report["new"]
        detail = (f"{len(new)} new / {len(report['baselined'])} baselined"
                  + (f"; new: {', '.join(new[:3])}" if new else ""))
        return result(f"{prefix}:security-baseline", "pass", detail), bool(new)
    except Exception as exc:  # a broken scan is a report, never a crash
        return result(f"{prefix}:security-baseline", "pass",
                      f"scan unavailable: {type(exc).__name__}: {exc}"), False


def run_target_gate(name: str, gate: str) -> tuple[list[dict[str, str]], str]:
    """Compose the proportional gate over a target; persist its quality-state."""
    entry = targets_lib.resolve(name)
    root = targets_lib.target_root(entry)
    prefix = f"target:{name}"
    results: list[dict[str, str]] = []
    results.append(_ensure_target_graph_fresh(name, root, prefix))
    results.extend(check_syntax(root, prefix))
    results.extend(check_python_compile(entry, prefix))
    results.extend(check_line_budgets(entry, prefix))
    results.extend(check_hygiene_lite(entry, prefix))
    results.extend(check_protected_instructions(entry, prefix))
    baseline_row, requires_security_review = check_security_baseline(entry, prefix)
    results.append(baseline_row)
    if gate in {"spec-pack", "commit", "feature", "release", "product-release"}:
        results.extend(check_markdown_links(root, prefix))
    results.extend(run_target_commands(entry, gate, prefix))

    status = "pass" if not any(r["status"] == "fail" for r in results) else "fail"
    counts: dict[str, int] = {"pass": 0, "fail": 0, "skip": 0}
    for item in results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
        # subject-tagged-events (isolation Phase 4): attribution as a FIELD,
        # not only the `target:<name>` check-name prefix convention.
        item.setdefault("subject", name)
    failures = [r for r in results if r["status"] == "fail"]
    state_path = targets_lib.state_dir(name) / "quality-state.json"
    previous = read_json(state_path, {}) or {}
    history = (previous.get("recentFailures") or [])
    history.append({"gate": gate, "at": now(), "failureNames": [r.get("name") for r in failures[:10]]})
    write_json(state_path, {
        "schemaVersion": "1.0",
        "target": name,
        "lastValidation": {
            "gate": gate, "status": status, "validatedAt": now(), "counts": counts,
            "failureNames": [r.get("name", "unknown") for r in failures[:10]],
        },
        # security-baseline-targets: same signal the harness quality-state
        # carries — a NEW (non-baselined) finding in this target's tree.
        "requiresSecurityReview": requires_security_review,
        # per-subject red-check history: the recurrence signal self-review needs
        "recentFailures": history[-20:],
    })
    return results, status
