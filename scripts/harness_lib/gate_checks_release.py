"""Release-hygiene checks for the validation gate (extracted from spec_test_gate.py).

Product portability blockers: generated artifacts, bytecode, local absolute
paths, stale docs, CI matrix, export manifest, product-hardening artifacts.
Shared gate helpers arrive via bind(env).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import Any


def bind(env: dict[str, Any]) -> None:
    """Receive shared gate helpers/constants from scripts/spec_test_gate.py."""
    globals().update(env)


# Local absolute-path leaks forbidden in tracked files. The Windows-home pattern
# covers every spelling that reaches the tree: C:\Users\, JSON-escaped C:\\Users\\,
# C:/Users/, git-bash /c/Users/, WSL /mnt/c/Users/ (Users|workspace|tmp home marker).
# Module-level so the path-hygiene scenario can assert against the very same regex.
LOCAL_ABSOLUTE_PATH_PATTERNS = [
    re.compile(r"/mnt/data"),
    re.compile(r"/opt/pyvenv"),
    re.compile(r"/home/oai"),
    re.compile(r"(?i)(?:[A-Za-z]:(?:\\\\?|/)|/(?:c|mnt/c)/)(?:Users|workspace|tmp)(?:\\\\?|/)"),
]


def python_pin_verdict(have: tuple[int, int], pin: str | None) -> tuple[str, str]:
    """Pure verdict for release-hygiene:python-pin (python-pin-312). `have` is
    sys.version_info[:2]. Absent pin -> skip; a present but non-numeric or
    under-specified pin -> fail (a guarantee must not be disarmed by a malformed
    pin); else pass iff the running MINOR matches -- patch releases (3.13.x) fine.
    Kept pure so _selfcheck can exercise every branch without a real interpreter."""
    if not pin:
        return 'skip', 'no runtime.python.pinnedVersion declared'
    try:
        want = tuple(int(x) for x in pin.split('.')[:2])
    except (ValueError, TypeError):
        return 'fail', f'runtime.python.pinnedVersion is not a numeric version: {pin!r}'
    if len(want) < 2:
        return 'fail', f'runtime.python.pinnedVersion needs major.minor, got {pin!r}'
    if tuple(have) == want:
        return 'pass', f'{have[0]}.{have[1]} == pinned {pin}'
    return 'fail', (f'running {have[0]}.{have[1]} but .harness/project.json pins {pin}; '
                    'run under HARNESS_PYTHON (the pinned venv) or tools/agent-sync/py-run.sh')


def check_release_hygiene() -> list[dict[str, str]]:
    """Check release hygiene and product portability blockers.

    This intentionally runs for every gate because product-only leaks such as
    local absolute paths or root-level fixture files can otherwise survive normal
    functional fixtures and be shipped in a release ZIP.
    """
    hard_forbidden: list[str] = []
    bytecode: list[str] = []
    local_path_hits: list[str] = []
    stale_doc_hits: list[str] = []
    forbidden_local_patterns = LOCAL_ABSOLUTE_PATH_PATTERNS
    stale_agent_phrases = [
        "Parallel writes remain forbidden unless a future project-specific policy",
        "Parallel source edits remain prohibited. The current workflow runtime is for controlled concurrent analysis",
    ]
    # Local-path scan skips gitignored operator state: it never ships in the release
    # ZIP (which packages tracked files) and is machine-local by nature (e.g.
    # .claude/settings.local.json, untracked .harness/targets/*/target.json). Without
    # this, untracking a machine-path file wouldn't clear the gate (rglob still sees
    # it on disk). Empty set on non-git checkouts (release extract) => scan everything.
    git_ignored: set[str] = set()
    try:
        proc = subprocess.run(['git', 'ls-files', '-o', '-i', '--exclude-standard'],
                              cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        if proc.returncode == 0:
            git_ignored = {ln.strip() for ln in proc.stdout.splitlines() if ln.strip()}
    except Exception:
        git_ignored = set()
    # Files that must legibly quote the forbidden patterns in order to document them
    # are exempt from the local-path scan (same reason the gate file is exempt): the
    # gate names the patterns it checks, and the hygiene roadmap/backlog demonstrate
    # the exact leaking strings.
    path_pattern_exempt = {
        'scripts/spec_test_gate.py',
        'scripts/harness_lib/gate_checks_release.py',
        'docs/roadmap/git-onedrive-path-hygiene.md',
        'docs/IMPLEMENTATION_BACKLOG.md',
        'testing/scenarios/ph_path_hygiene.py',
    }
    for path in ROOT.rglob('*'):
        rel = path.relative_to(ROOT).as_posix()
        if '.git/' in rel or rel.startswith('.git/') or rel.startswith('.venv/'):
            continue
        if path.is_dir():
            # Directories cannot leak into a file-based release; their files are
            # judged individually. Empty leftovers (transient-lock survivors) are
            # not release blockers.
            continue
        if '__pycache__' in path.parts or path.suffix == '.pyc':
            bytecode.append(rel)
            continue
        # Same rationale as the local-path scan above: the release ZIP packages
        # TRACKED files, so a gitignored file (e.g. .harness/runtime/executor-
        # circuit-*.json) cannot ship and is not a release-hygiene concern. Empty
        # git_ignored on a non-git checkout => nothing skipped (fail-closed).
        if rel in git_ignored:
            continue
        if (
            # MF.4/I5: discover cache + enrichment are durable operator data
            # (bought with API tokens, gitignored, spared by cleanup) — not
            # release leaks. Everything else under graphify-out/ stays forbidden.
            (rel.startswith('graphify-out/') and rel not in {
                'graphify-out/discover-cache.json',
                'graphify-out/docs-enrichment.json',
                'graphify-out/image-enrichment.json',
            } and not rel.startswith('graphify-out/targets/'))
            or rel.startswith('.harness/workflows/active/WF-')
            or rel.startswith('.harness/runtime/executor-circuit-')
            or (path.suffix in {'.jsonl', '.log'} and rel != 'release/slsa-provenance.intoto.jsonl')
            or rel.startswith('.harness/runs/')
            or rel.startswith('.harness/state-store/')
            or (path.parent == ROOT and path.name.startswith('tmp-async') and path.suffix == '.py')
        ):
            hard_forbidden.append(rel)
        if path.is_file() and path.suffix.lower() in {'.json', '.md', '.py', '.toml', '.yaml', '.yml', '.sh'}:
            try:
                content = path.read_text(encoding='utf-8', errors='ignore')
            except Exception:
                content = ''
            # Skip gitignored operator state and files that document the patterns.
            if rel not in path_pattern_exempt and rel not in git_ignored:
                for pattern in forbidden_local_patterns:
                    if pattern.search(content):
                        local_path_hits.append(rel)
                        break
            if rel == 'AGENTS.md':
                for phrase in stale_agent_phrases:
                    if phrase in content:
                        stale_doc_hits.append(f"{rel}: {phrase}")
    # Race hardening (2026-07-22 live case): a GITIGNORED runtime file can be
    # CREATED between the git_ignored snapshot above and the rglob walk — e.g. a
    # session hook's forensic append_event landing .harness/runs/events.jsonl
    # mid-gate — and would fail the gate on pure timing. Re-check ignore status at
    # flag time; a non-git checkout (release extract) errors -> list unchanged,
    # preserving the fail-closed semantics there.
    if hard_forbidden:
        try:
            proc = subprocess.run(['git', 'check-ignore', '--stdin'], cwd=ROOT,
                                  input='\n'.join(hard_forbidden),
                                  capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
            if proc.returncode in (0, 1):  # 0 = some ignored, 1 = none; both mean git answered
                ignored_now = {ln.strip() for ln in proc.stdout.splitlines() if ln.strip()}
                hard_forbidden = [p for p in hard_forbidden if p not in ignored_now]
        except Exception:
            pass
    out = [result('release-hygiene:generated-artifacts', 'pass' if not hard_forbidden else 'fail', 'clean' if not hard_forbidden else '; '.join(hard_forbidden[:10]))]
    out.append(result('release-hygiene:python-bytecode', 'skip' if bytecode else 'pass', 'will be removed by scripts/package-release.py' if bytecode else 'clean'))
    # python-pin-312: the gate certifies only under the pinned interpreter. Read
    # is fail-CLOSED but distinguishes ABSENT (skip -- e.g. a release extract with
    # no project.json) from CORRUPT (fail -- a guarantee must not be disarmed by a
    # broken config). The verdict itself is python_pin_verdict (pure + self-checked).
    try:
        proj = json.loads((ROOT / '.harness' / 'project.json').read_text(encoding='utf-8'))
        # .get() inside the try: a valid-JSON-but-non-object project.json (array or
        # scalar at top level) raises AttributeError here and is caught as corrupt
        # -> fail, never an uncaught crash of the whole release gate.
        pin = str(((proj.get('runtime') or {}).get('python') or {}).get('pinnedVersion') or '').strip() or None
    except FileNotFoundError:
        out.append(result('release-hygiene:python-pin', 'skip', 'no .harness/project.json'))
    except Exception as exc:
        out.append(result('release-hygiene:python-pin', 'fail', f'.harness/project.json unreadable: {type(exc).__name__}: {exc}'[:120]))
    else:
        status, detail = python_pin_verdict(sys.version_info[:2], pin)
        out.append(result('release-hygiene:python-pin', status, detail))
    out.append(result('release-hygiene:local-absolute-paths', 'pass' if not local_path_hits else 'fail', 'none' if not local_path_hits else '; '.join(sorted(set(local_path_hits))[:10])))
    out.append(result('release-hygiene:agents-doc-current', 'pass' if not stale_doc_hits else 'fail', 'controlled-write policy current' if not stale_doc_hits else '; '.join(stale_doc_hits[:10])))
    out.append(result('release-hygiene:package-script', 'pass' if (ROOT/'scripts'/'package-release.py').exists() else 'fail', 'scripts/package-release.py' if (ROOT/'scripts'/'package-release.py').exists() else 'missing'))
    ci_path = ROOT / '.github' / 'workflows' / 'harness-ci.yml'
    ci_ok = False
    ci_detail = 'missing .github/workflows/harness-ci.yml'
    if ci_path.exists():
        ci_text = ci_path.read_text(encoding='utf-8', errors='ignore')
        required = ['ubuntu-latest', 'windows-latest', 'macos-latest', 'product-release', 'spec-pack', 'token-budget', 'handoff-budget', 'engineering-guardrails', 'protected-files', 'workflow-id-boundary', 'profile-planning', 'graphify-adapter', 'graphify-code-ast', 'graphify-api-providers', 'controlled-write', 'execute-output', 'git-worktree', 'async-workflow', 'trace-secret-redaction', 'process-timeout-cleanup', 'fixture-liveness', 'provider-canary']
        missing = [item for item in required if item not in ci_text]
        ci_ok = not missing
        ci_detail = 'matrix present' if ci_ok else 'missing: ' + ', '.join(missing)
    out.append(result('release-hygiene:ci-matrix', 'pass' if ci_ok else 'fail', ci_detail))
    manifest_path = ROOT / 'EXPORT_MANIFEST.md'
    manifest_ok = False
    manifest_detail = 'missing EXPORT_MANIFEST.md'
    if manifest_path.exists():
        manifest_text = manifest_path.read_text(encoding='utf-8', errors='ignore')
        required_manifest_entries = ['.github/workflows/harness-ci.yml', 'testing/CI_MATRIX.md', 'scripts/package-release.py', 'scripts/harness_lib/common.py', 'scripts/harness_lib/controlled_writes.py', 'scripts/harness_lib/async_runtime.py', 'scripts/harness_lib/token_economics.py', 'scripts/harness_lib/tracing.py', 'scripts/harness_lib/state_store.py', 'scripts/harness_lib/observability_exporters.py', 'scripts/harness_lib/processes.py', 'scripts/harness_lib/graphify_adapter.py', 'scripts/harness_lib/handoff.py', 'scripts/harness_lib/engineering_guardrails.py', 'scripts/harness_lib/baseline_catalog.py', 'scripts/harness_lib/graphify_code_ast.py', 'scripts/harness_lib/protected_files.py', 'scripts/harness_lib/deprecation_hygiene.py', 'scripts/harness_lib/fixture_liveness.py', 'scripts/harness_lib/async_runtime.py', 'testing/deprecation-hygiene.json', 'scripts/harness_lib/workflow_ids.py', 'scripts/harness_lib/workflow_state.py', 'scripts/release_integrity.py', 'release/harness-sbom.cdx.json', 'release/checksums.sha256', 'release/release-attestation.json', 'release/release-signature.sha256', 'release/slsa-provenance.intoto.jsonl', 'release/external-signing-plan.json', 'docs/HARNESS_OBSERVABILITY.md', 'docs/STATE_STORE_CONTRACT.md', 'docs/THREAT_MODEL.md', 'testing/golden/cli-contract.json', 'testing/RED_TEAM_FIXTURES.md', 'testing/engineering-guardrails.json', '.harness/protected-files.json', '.harness/protected-files.snapshot.json', 'scripts/harness_lib/protected_files.py', 'scripts/harness_lib/deprecation_hygiene.py', 'scripts/harness_lib/fixture_liveness.py', 'scripts/harness_lib/async_runtime.py', 'testing/deprecation-hygiene.json', 'tools/hooks/protect_canonical_files.py', 'specs/00-universal/software-engineering-guardrails.md', 'specs/00-universal/canonical-file-protection.md', 'specs/00-universal/UNIVERSAL_BASELINE.md']
        missing_entries = [entry for entry in required_manifest_entries if entry not in manifest_text]
        forbidden_entries = [entry for entry in ['.harness/runs/validation-results.jsonl', '.harness/state-store/workflows.sqlite3', 'tmp-async-fixture-agent.py', 'tmp-async-cancel-fixture-agent.py'] if entry in manifest_text]
        manifest_ok = not missing_entries and not forbidden_entries
        if manifest_ok:
            manifest_detail = 'current'
        else:
            parts = []
            if missing_entries:
                parts.append('missing: ' + ', '.join(missing_entries))
            if forbidden_entries:
                parts.append('forbidden: ' + ', '.join(forbidden_entries))
            manifest_detail = '; '.join(parts)
    out.append(result('release-hygiene:export-manifest', 'pass' if manifest_ok else 'fail', manifest_detail))
    product_hardening_required = [
        'scripts/harness_lib/tracing.py',
        'scripts/harness_lib/state_store.py',
        'scripts/harness_lib/observability_exporters.py',
        'scripts/harness_lib/processes.py',
        'scripts/harness_lib/graphify_adapter.py',
        'scripts/harness_lib/handoff.py',
        'scripts/harness_lib/workflow_ids.py',
        'scripts/harness_lib/workflow_state.py',
        'scripts/harness_lib/engineering_guardrails.py',
        'scripts/harness_lib/baseline_catalog.py',
        'scripts/harness_lib/graphify_code_ast.py',
        'scripts/harness_lib/protected_files.py', 'scripts/harness_lib/deprecation_hygiene.py', 'scripts/harness_lib/fixture_liveness.py', 'scripts/harness_lib/async_runtime.py', 'testing/deprecation-hygiene.json',
        'scripts/release_integrity.py',
        'release/harness-sbom.cdx.json',
        'release/checksums.sha256',
        'release/release-attestation.json',
        'release/slsa-provenance.intoto.jsonl',
        'release/external-signing-plan.json',
        'docs/HARNESS_OBSERVABILITY.md',
        'docs/STATE_STORE_CONTRACT.md',
        'docs/THREAT_MODEL.md',
        'testing/golden/cli-contract.json',
        'testing/RED_TEAM_FIXTURES.md',
        'testing/engineering-guardrails.json',
        'specs/00-universal/software-engineering-guardrails.md',
        'specs/00-universal/canonical-file-protection.md',
        '.harness/protected-files.json',
        '.harness/protected-files.snapshot.json',
        'tools/hooks/protect_canonical_files.py',
    ]
    missing_hardening = [item for item in product_hardening_required if not (ROOT / item).exists()]
    out.append(result('release-hygiene:product-hardening-artifacts', 'pass' if not missing_hardening else 'fail', 'present' if not missing_hardening else 'missing: ' + ', '.join(missing_hardening)))
    return out


def _selfcheck() -> int:  # pragma: no cover — exercised as a script
    """python_pin_verdict branch coverage: the runnable check the pin gate needs
    (inverting have==want or breaking a branch fails HERE)."""
    cases = [
        ((3, 13), '3.13', 'pass'),
        ((3, 13), '3.13.14', 'pass'),   # patch tolerated
        ((3, 12), '3.13', 'fail'),      # the drift the gate exists to catch
        ((3, 13), None, 'skip'),
        ((3, 13), '', 'skip'),
        ((3, 13), '3.x', 'fail'),       # malformed -> fail-closed, never skip
        ((3, 13), 'three.twelve', 'fail'),
        ((3, 13), '3', 'fail'),         # under-specified -> fail
    ]
    bad = []
    for have, pin, want in cases:
        status, _ = python_pin_verdict(have, pin)
        ok = status == want
        print(f"{'PASS' if ok else 'FAIL'}  have={have} pin={pin!r} -> {status} (want {want})")
        if not ok:
            bad.append((have, pin, status, want))
    print(f"\npython_pin_verdict self-check: {len(cases) - len(bad)}/{len(cases)}"
          + (' — all green' if not bad else f'; FAILED: {bad}'))
    return 1 if bad else 0


if __name__ == '__main__':
    raise SystemExit(_selfcheck())
