"""Rule-based CI failure triage and bounded, redacted, untrusted receipts (SPEC-OS-SHIP-001 AC-08, AC-09).

Rules run in the exact precedence order of SPEC-OS-SHIP-001:
  1. pinned_ref_missing
  2. superseded
  3. infrastructure
  4. queued
  5. required_check_missing
  6. permission
  7. hook_refused
  8. conflict
  9. base_red
 10. flaky
 11. regression
Fallback: unclassified.

Receipts are evidence only; they never grant authority and always mark captured log excerpts `untrusted: true`.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time

RECEIPT_SCHEMA = 'tare.tools/ship-triage-receipt/1'
MAX_EVIDENCE_LINES = 40
MAX_EXCERPT_BYTES = 16384
MAX_LINE_CHARS = 500

# Token-like patterns stripped from captured excerpts and evidence lines (AC-09).
_TOKEN_PATTERNS = (
    (re.compile(r'\bgh[pousr]_[A-Za-z0-9_]{20,}\b'), '[redacted]'),
    (re.compile(r'\bgithub_pat_[A-Za-z0-9_]{20,}\b'), '[redacted]'),
    (re.compile(r'\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}\b'), '[redacted]'),
    (re.compile(r'\bAIza[0-9A-Za-z_-]{20,}\b'), '[redacted]'),
    (re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b'), '[redacted]'),
    (re.compile(r'(?i)(Authorization\s*:\s*(?:Bearer|Basic|token)\s+)\S+'), r'\1[redacted]'),
    (re.compile(r'(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]{16,}'), r'\1[redacted]'),
    (re.compile(r'(https?://[^/\s:@]+:)[^/\s@]+(@)'), r'\1[redacted]\2'),
    (re.compile(r'''(?i)\b((?:access_)?token|api[_-]?key|client_secret|secret|password)(\s*[:=]\s*)(['"]?)[A-Za-z0-9._~+/=-]{12,}\3'''),
     r'\1\2[redacted]'),
)

_GH_LOG_PREFIX = re.compile(r'^[^\t]*\t[^\t]*\t\S+Z ')
_PINNED_REF_MISSING = re.compile(r'upload-pack:\s*not our ref', re.IGNORECASE)
_SETUP_JOB_ERROR = re.compile(r'\tSet up job\t.*(?:##\[error\]|\berror\b|\bfailed\b)', re.IGNORECASE)
_DNS_TLS_ERROR = re.compile(
    r'(?:Could not resolve host|schannel|SSL(?:Error|:\s*CERTIFICATE_VERIFY_FAILED)|'
    r'TLS handshake|Temporary failure in name resolution|getaddrinfo failed)',
    re.IGNORECASE,
)
_QUEUED_CAUSE = re.compile(
    r'(?:no matching runner|waiting for a runner|self-hosted runner[^\n]*offline|concurrency limit)',
    re.IGNORECASE,
)
_PERMISSION_ERROR = re.compile(
    r'(?:Resource not accessible by integration|HTTP\s+403\b|status code 403\b|403 Forbidden|'
    r'returned 403\b|missing[^\n]*token scope|insufficient[^\n]*permissions?|'
    r'secrets?\s+(?:are|is)\s+not\s+available|without secrets)',
    re.IGNORECASE,
)
_HOOK_REFUSED = re.compile(
    r'(?:pre-push\b[^\n]*(?:refused|declined|failed)|remote:\s*error:\s*hook declined|'
    r'pre-receive hook declined|Preflight refused:)',
    re.IGNORECASE,
)
_CONFLICT_LINE = re.compile(r'(?:^CONFLICT \(|Automatic merge failed; fix conflicts)', re.IGNORECASE)
_PYTEST_FAILED = re.compile(r'^(?:FAILED|ERROR)\s+([^\s]+::[^\s]+|tests/[^\s]+\.py)(?:\s+-\s+|\s*$)')
_ERROR_LINE = re.compile(r'FAILED |ERROR |Error:|error:|##\[error\]|^E  |AssertionError|Traceback|[0-9]+ failed')


def redact_tokens(text: str) -> str:
    """Replace token-like secrets in `text` with `[redacted]`."""
    out = text or ''
    for pattern, repl in _TOKEN_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def _strip_gh_columns(line: str) -> str:
    return _GH_LOG_PREFIX.sub('', line).rstrip()


def _clean_line(line: str) -> str:
    cleaned = redact_tokens(line.rstrip('\r\n'))
    if len(cleaned) > MAX_LINE_CHARS:
        cleaned = cleaned[:MAX_LINE_CHARS - 12] + ' [truncated]'
    return cleaned


def bound_evidence(lines: list[str], *, max_lines: int = MAX_EVIDENCE_LINES,
                   max_bytes: int = MAX_EXCERPT_BYTES) -> dict:
    """Redact, line-cap (`<= 40`), and byte-cap evidence lines, returning size, SHA-256, and `untrusted: True`."""
    bounded: list[str] = []
    used = 0
    for raw in lines:
        if len(bounded) >= max_lines:
            break
        line = _clean_line(raw)
        encoded = line.encode('utf-8')
        sep = 1 if bounded else 0
        if used + sep + len(encoded) > max_bytes:
            room = max_bytes - used - sep
            if room > 0:
                clipped = encoded[:room].decode('utf-8', errors='ignore')
                if clipped:
                    bounded.append(clipped)
                    used += sep + len(clipped.encode('utf-8'))
            break
        bounded.append(line)
        used += sep + len(encoded)
    payload = '\n'.join(bounded).encode('utf-8')
    return {
        'evidence': bounded,
        'excerpt_bytes': len(payload),
        'excerpt_sha256': hashlib.sha256(payload).hexdigest(),
        'untrusted': True,
    }


def extract_pytest_ids(log: str) -> list[str]:
    """Extract unique pytest failed node IDs from a CI log in order of appearance."""
    seen: set[str] = set()
    ids: list[str] = []
    for raw in (log or '').splitlines():
        text = _strip_gh_columns(raw).strip()
        match = _PYTEST_FAILED.match(text)
        if not match:
            continue
        node_id = match.group(1)
        if node_id not in seen:
            seen.add(node_id)
            ids.append(node_id)
    return ids


def _matching_lines(log: str, pattern: re.Pattern[str]) -> list[str]:
    return [line for line in (log or '').splitlines() if pattern.search(line)]


def _error_or_tail_lines(log: str, *, limit: int = MAX_EVIDENCE_LINES) -> list[str]:
    lines = (log or '').splitlines()
    if not lines:
        return []
    errors = [line for line in lines if _ERROR_LINE.search(_strip_gh_columns(line))]
    selected = errors[-limit:] if errors else lines[-limit:]
    return selected


def classify(
    *,
    log: str = '',
    run: dict | None = None,
    newer_run_exists: bool = False,
    actions_incident: str | None = None,
    queued_reason: str | None = None,
    queued_past_limit: bool = False,
    missing_required_checks: list[str] | tuple[str, ...] = (),
    hook_message: str | None = None,
    conflict_files: list[str] | tuple[str, ...] = (),
    base_failing_ids: list[str] | tuple[str, ...] = (),
    base_failing_jobs: list[str] | tuple[str, ...] = (),
    failing_jobs: list[str] | tuple[str, ...] = (),
    passed_same_tree: bool = False,
    verify_command: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """Classify a CI failure according to the exact precedence table in SPEC-OS-SHIP-001.

    Returns `{class, rule, evidence, excerpt_bytes, excerpt_sha256, untrusted, verification_checks}`.
    """
    run = run or {}
    conclusion = (run.get('conclusion') or '').lower()
    status = (run.get('status') or '').lower()
    redacted_log = redact_tokens(log or '')

    def result(kind: str, rule: str, evidence_lines: list[str], verification_checks: list[list[str]] | None = None) -> dict:
        bounded = bound_evidence(evidence_lines)
        return {
            'class': kind,
            'rule': rule,
            **bounded,
            'verification_checks': list(verification_checks or []),
        }

    # 1. pinned_ref_missing
    pinned_hits = _matching_lines(redacted_log, _PINNED_REF_MISSING)
    if pinned_hits:
        return result('pinned_ref_missing', 'upload_pack_not_our_ref', pinned_hits)

    # 2. superseded
    if conclusion == 'cancelled' and newer_run_exists:
        workflow = run.get('workflowName') or 'workflow'
        branch = run.get('headBranch') or 'ref'
        run_id = run.get('databaseId') or '?'
        return result('superseded', 'cancelled_with_newer_run',
                      [f"run {run_id} ({workflow} on {branch}) was cancelled and superseded by a newer run"])

    # 3. infrastructure
    if conclusion == 'startup_failure':
        return result('infrastructure', 'startup_failure',
                      _error_or_tail_lines(redacted_log) or ['workflow run ended with startup_failure'])
    if actions_incident:
        return result('infrastructure', 'github_actions_incident', [f'GitHub Actions status incident: {actions_incident}'])
    setup_hits = _matching_lines(redacted_log, _SETUP_JOB_ERROR)
    if setup_hits:
        return result('infrastructure', 'setup_job_failure', setup_hits)
    net_hits = _matching_lines(redacted_log, _DNS_TLS_ERROR)
    if net_hits:
        return result('infrastructure', 'dns_or_tls_error', net_hits)

    # 4. queued
    queued_hits = _matching_lines(redacted_log, _QUEUED_CAUSE)
    if queued_past_limit or (status == 'queued' and queued_reason) or queued_hits:
        lines = []
        if queued_reason:
            lines.append(f'queued past limit: {queued_reason}')
        lines.extend(queued_hits)
        if not lines:
            lines = ['workflow run remained queued past limit']
        return result('queued', 'queued_past_limit', lines)

    # 5. required_check_missing
    if missing_required_checks:
        missing = [str(c) for c in missing_required_checks]
        return result('required_check_missing', 'missing_required_check',
                      [f'required status check not produced on head: {ctx}' for ctx in missing])

    # 6. permission
    perm_hits = _matching_lines(redacted_log, _PERMISSION_ERROR)
    if perm_hits:
        return result('permission', 'permission_denied', perm_hits)

    # 7. hook_refused
    hook_hits = ([hook_message] if hook_message else []) + _matching_lines(redacted_log, _HOOK_REFUSED)
    if hook_hits:
        return result('hook_refused', 'hook_refused', hook_hits)

    # 8. conflict
    conflict_hits = ([f'merge conflict: {path}' for path in conflict_files]
                     + _matching_lines(redacted_log, _CONFLICT_LINE))
    if conflict_hits:
        return result('conflict', 'unresolvable_conflict', conflict_hits)

    # 9. base_red
    failed_ids = extract_pytest_ids(redacted_log)
    base_id_set = set(base_failing_ids or ())
    shared_ids = [tid for tid in failed_ids if tid in base_id_set]
    base_job_set = set(base_failing_jobs or ())
    shared_jobs = [job for job in (failing_jobs or ()) if job in base_job_set]
    if shared_ids or shared_jobs:
        lines = [f'also failing on base branch: {tid}' for tid in shared_ids]
        lines += [f'job also failing on base branch: {job}' for job in shared_jobs]
        return result('base_red', 'base_branch_failing', lines)

    # 10. flaky
    if passed_same_tree:
        lines = ['same tree passed this check previously'] + _error_or_tail_lines(redacted_log, limit=MAX_EVIDENCE_LINES - 1)
        return result('flaky', 'same_tree_passed_before', lines)

    # 11. regression
    if failed_ids:
        cmd = (list(verify_command) + failed_ids) if verify_command else ['python', '-m', 'pytest', *failed_ids, '-q']
        lines = _error_or_tail_lines(redacted_log) or [f'FAILED {tid}' for tid in failed_ids]
        return result('regression', 'failed_test_ids', lines, verification_checks=[cmd])

    # Fallback: unclassified
    return result('unclassified', 'unmatched', _error_or_tail_lines(redacted_log) or ['no triage rule matched'])


def receipt_path(folder: Path, run_id, attempt: int = 1) -> Path:
    safe_run = re.sub(r'[^A-Za-z0-9_.-]+', '_', str(run_id or 'unknown'))
    safe_attempt = max(1, int(attempt or 1))
    return Path(folder)/'receipts'/f'{safe_run}-{safe_attempt}.json'


def write_receipt(
    folder: Path,
    *,
    repo: str | None,
    pr: int | None,
    run_id,
    attempt: int = 1,
    head_sha: str | None,
    triage: dict,
    transition: str = 'RED',
) -> Path:
    """Persist one triage receipt atomically under `folder/receipts/<run_id>-<attempt>.json`."""
    target = receipt_path(folder, run_id, attempt)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'schema': RECEIPT_SCHEMA,
        'repo': repo,
        'pr': pr,
        'run_id': run_id,
        'attempt': max(1, int(attempt or 1)),
        'head_sha': head_sha,
        'class': triage['class'],
        'rule': triage['rule'],
        'evidence': list(triage['evidence']),
        'excerpt_bytes': int(triage['excerpt_bytes']),
        'excerpt_sha256': str(triage['excerpt_sha256']),
        'untrusted': True,
        'verification_checks': list(triage.get('verification_checks') or []),
        'transition': transition,
        'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    tmp = target.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    os.replace(tmp, target)
    return target
