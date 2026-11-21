"""Preflight trust inspection for privileged GitHub Actions workflows (SPEC-OS-SHIP-001 AC-21).

Flags privileged jobs that execute untrusted code:
- A `workflow_run` job that does not require both a `push` event and the same repository (directly or through a
  required `needs` chain), or whose `actions/checkout` step checks out the triggering run's commit.
- A `pull_request_target` job whose `actions/checkout` step checks out the pull request's head.

A pull request that adds or modifies a flagged job is refused before pushing; a flagged job that already exists
unchanged on the default branch is reported in the preflight receipt with a proposed backlog item.

Pure Python standard library: no third-party YAML dependency required.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import time

from compute_plane.managed_process import host_executable

RECEIPT_SCHEMA = 'tare.tools/workflow-trust-preflight/1'
WORKFLOW_DIR = '.github/workflows'
WORKFLOW_SUFFIXES = ('.yml', '.yaml')

_BLOCK_SCALAR = re.compile(r'^([|>])([1-9]?[+-]?|[+-]?[1-9]?)\s*(?:#.*)?$')
_NEEDS_BYPASS = re.compile(r'\b(?:always|failure|cancelled)\s*\(\s*\)', re.IGNORECASE)
_ENV_REF = re.compile(r'\benv\.([A-Za-z_][A-Za-z0-9_]*)\b')

_WORKFLOW_RUN_PUSH = re.compile(
    r"""(?:\bgithub\.event\.workflow_run\.event\s*==\s*['"]push['"]|['"]push['"]\s*==\s*github\.event\.workflow_run\.event\b)"""
)
_WORKFLOW_RUN_SAME_REPO = re.compile(
    r"""(?:\bgithub\.event\.workflow_run\.head_repository\.full_name\s*==\s*github\.repository\b"""
    r"""|\bgithub\.repository\s*==\s*github\.event\.workflow_run\.head_repository\.full_name\b"""
    r"""|\bgithub\.event\.workflow_run\.head_repository\.id\s*==\s*github\.repository_id\b"""
    r"""|\bgithub\.repository_id\s*==\s*github\.event\.workflow_run\.head_repository\.id\b)"""
)
_EVENT_EQUALS = re.compile(
    r"""(?:\bgithub\.event_name\s*==\s*['"]([A-Za-z0-9_-]+)['"]|['"]([A-Za-z0-9_-]+)['"]\s*==\s*github\.event_name\b)"""
)
_EVENT_NOT_EQUALS = re.compile(
    r"""(?:\bgithub\.event_name\s*!=\s*['"]([A-Za-z0-9_-]+)['"]|['"]([A-Za-z0-9_-]+)['"]\s*!=\s*github\.event_name\b)"""
)
_WR_TRIGGER_CHECKOUT = re.compile(
    r'\bgithub\.event\.workflow_run\.(?:head_sha|head_branch|head_repository\b|pull_requests\b)'
)
_PRT_HEAD_CHECKOUT = re.compile(
    r'(?:\bgithub\.event\.pull_request\.head\b|\bgithub\.head_ref\b|refs/pull/\S+/(?:head|merge)\b)'
)


def _strip_comment(text: str) -> str:
    """Drop an unquoted YAML `# ...` comment while keeping `#` inside quotes or `${{ ... }}`."""
    in_single = False
    in_double = False
    expr_depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_single:
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    i += 2
                    continue
                in_single = False
        elif in_double:
            if ch == '\\' and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                in_double = False
        else:
            if text[i:i + 3] == '${{':
                expr_depth += 1
                i += 3
                continue
            if expr_depth and text[i:i + 2] == '}}':
                expr_depth -= 1
                i += 2
                continue
            if not expr_depth:
                if ch == "'":
                    in_single = True
                elif ch == '"':
                    in_double = True
                elif ch == '#' and (i == 0 or text[i - 1].isspace()):
                    return text[:i].rstrip()
        i += 1
    return text.rstrip()


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        inner = value[1:-1]
        return inner.replace('\\"', '"').replace('\\\\', '\\')
    return value


def _split_kv(text: str):
    """Split a YAML mapping entry `key: value` outside quotes; returns `(key, rest)` or `None`."""
    in_single = False
    in_double = False
    for i, ch in enumerate(text):
        if in_single:
            if ch == "'":
                in_single = False
        elif in_double:
            if ch == '\\':
                continue
            if ch == '"':
                in_double = False
        else:
            if ch == "'":
                in_single = True
            elif ch == '"':
                in_double = True
            elif ch == ':' and (i + 1 == len(text) or text[i + 1].isspace()):
                key = _unquote(text[:i].strip())
                return key, text[i + 1:].strip()
    return None


def _parse_flow_list(text: str) -> list[str]:
    inner = text.strip()[1:-1].strip()
    if not inner:
        return []
    items, buf = [], []
    in_single = in_double = False
    for ch in inner:
        if in_single:
            if ch == "'":
                in_single = False
            buf.append(ch)
        elif in_double:
            if ch == '"':
                in_double = False
            buf.append(ch)
        else:
            if ch == "'":
                in_single = True
                buf.append(ch)
            elif ch == '"':
                in_double = True
                buf.append(ch)
            elif ch == ',':
                items.append(_unquote(''.join(buf)))
                buf = []
            else:
                buf.append(ch)
    if buf:
        items.append(_unquote(''.join(buf)))
    return [item for item in items if item]


def _consume_block_scalar(lines: list[str], start: int, parent_indent: int, style: str) -> tuple[str, int]:
    collected = []
    content_indent = None
    i = start
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if not stripped:
            collected.append('')
            i += 1
            continue
        indent = len(raw) - len(raw.lstrip(' '))
        if indent <= parent_indent:
            break
        if content_indent is None:
            content_indent = indent
        collected.append(raw[min(indent, content_indent):])
        i += 1
    while collected and collected[-1] == '':
        collected.pop()
    if style == '>':
        parts = []
        for line in collected:
            if line == '':
                parts.append('\n')
            elif parts and parts[-1] != '\n':
                parts.append(' ' + line.strip())
            else:
                parts.append(line.strip())
        return ''.join(parts).strip(), i
    return '\n'.join(collected), i


def _consume_scalar(lines: list[str], idx: int, indent: int, raw_rest: str) -> tuple[str, int]:
    match = _BLOCK_SCALAR.match(raw_rest)
    if match:
        return _consume_block_scalar(lines, idx + 1, indent, match.group(1))
    cleaned = _strip_comment(raw_rest)
    if not cleaned:
        return '', idx + 1
    parts = [_unquote(cleaned)]
    i = idx + 1
    if not (cleaned.startswith(("'", '"', '[', '{')) and cleaned.endswith(("'", '"', ']', '}'))):
        while i < len(lines):
            nxt = lines[i]
            s = nxt.strip()
            if not s or s.startswith('#'):
                i += 1
                continue
            n_indent = len(nxt) - len(nxt.lstrip(' '))
            if n_indent <= indent:
                break
            if s.startswith('- ') or _split_kv(s) is not None:
                break
            parts.append(_strip_comment(s))
            i += 1
    return ' '.join(p for p in parts if p).strip(), i


def _consume_list_or_scalar(lines: list[str], idx: int, indent: int, raw_rest: str) -> tuple[list[str], int]:
    cleaned = _strip_comment(raw_rest)
    if cleaned.startswith('[') and cleaned.endswith(']'):
        return _parse_flow_list(cleaned), idx + 1
    if cleaned:
        return [_unquote(cleaned)], idx + 1
    items = []
    i = idx + 1
    while i < len(lines):
        raw = lines[i]
        s = raw.strip()
        if not s or s.startswith('#'):
            i += 1
            continue
        n_indent = len(raw) - len(raw.lstrip(' '))
        if n_indent <= indent:
            break
        if s.startswith('- '):
            items.append(_unquote(_strip_comment(s[2:])))
        i += 1
    return items, i


def _consume_mapping(lines: list[str], start: int, parent_indent: int) -> tuple[dict[str, str], int]:
    mapping: dict[str, str] = {}
    i = start
    while i < len(lines):
        raw = lines[i]
        s = raw.strip()
        if not s or s.startswith('#'):
            i += 1
            continue
        indent = len(raw) - len(raw.lstrip(' '))
        if indent <= parent_indent:
            break
        kv = _split_kv(s)
        if kv is None:
            i += 1
            continue
        key, rest = kv
        val, i = _consume_scalar(lines, i, indent, rest)
        mapping[key] = val
    return mapping, i


def _skip_block(lines: list[str], start: int, parent_indent: int) -> int:
    i = start
    while i < len(lines):
        raw = lines[i]
        s = raw.strip()
        if not s or s.startswith('#'):
            i += 1
            continue
        indent = len(raw) - len(raw.lstrip(' '))
        if indent <= parent_indent:
            break
        i += 1
    return i


def _parse_steps(lines: list[str], start: int, steps_indent: int) -> tuple[list[dict], int]:
    steps: list[dict] = []
    i = start
    step_list_indent = None
    while i < len(lines):
        raw = lines[i]
        s = raw.strip()
        if not s or s.startswith('#'):
            i += 1
            continue
        indent = len(raw) - len(raw.lstrip(' '))
        if indent <= steps_indent:
            break
        if s.startswith('-') and (len(s) == 1 or s[1].isspace()):
            if step_list_indent is None:
                step_list_indent = indent
            if indent != step_list_indent:
                i += 1
                continue
            step = {'name': '', 'uses': '', 'if': '', 'run': '', 'with': {}, 'env': {}}
            after_dash = s[1:].lstrip(' ')
            item_indent = indent + (len(s) - len(after_dash)) if after_dash else indent + 2
            if after_dash:
                kv = _split_kv(after_dash)
                if kv:
                    k, rest = kv
                    if k in ('name', 'uses', 'if', 'run'):
                        step[k], i = _consume_scalar(lines, i, item_indent, rest)
                    elif k in ('with', 'env'):
                        step[k], i = _consume_mapping(lines, i + 1, item_indent)
                    else:
                        _, i = _consume_scalar(lines, i, item_indent, rest)
                else:
                    i += 1
            else:
                i += 1
            while i < len(lines):
                r2 = lines[i]
                s2 = r2.strip()
                if not s2 or s2.startswith('#'):
                    i += 1
                    continue
                ind2 = len(r2) - len(r2.lstrip(' '))
                if ind2 <= step_list_indent:
                    break
                kv2 = _split_kv(s2)
                if kv2 is None:
                    i += 1
                    continue
                k2, rest2 = kv2
                if k2 in ('name', 'uses', 'if', 'run'):
                    step[k2], i = _consume_scalar(lines, i, ind2, rest2)
                elif k2 in ('with', 'env'):
                    step[k2], i = _consume_mapping(lines, i + 1, ind2)
                elif _BLOCK_SCALAR.match(rest2):
                    _, i = _consume_scalar(lines, i, ind2, rest2)
                elif not _strip_comment(rest2):
                    i = _skip_block(lines, i + 1, ind2)
                else:
                    _, i = _consume_scalar(lines, i, ind2, rest2)
            steps.append(step)
        else:
            i += 1
    return steps, i


def parse_workflow(text: str) -> dict:
    """Extract top-level `on` triggers, workflow `env`, and each job's `if`, `needs`, `env`, and `steps`."""
    lines = text.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n').split('\n')
    triggers: set[str] = set()
    workflow_env: dict[str, str] = {}
    jobs: dict[str, dict] = {}
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        s = raw.strip()
        if not s or s.startswith('#'):
            i += 1
            continue
        indent = len(raw) - len(raw.lstrip(' '))
        if indent != 0:
            i += 1
            continue
        kv = _split_kv(s)
        if kv is None:
            i += 1
            continue
        key, rest = kv
        if key == 'on':
            cleaned = _strip_comment(rest)
            if cleaned.startswith('[') and cleaned.endswith(']'):
                triggers.update(_parse_flow_list(cleaned))
                i += 1
            elif cleaned.startswith('{') and cleaned.endswith('}'):
                for part in cleaned[1:-1].split(','):
                    sub = _split_kv(part.strip())
                    if sub:
                        triggers.add(sub[0])
                i += 1
            elif cleaned:
                triggers.add(_unquote(cleaned))
                i += 1
            else:
                i += 1
                sub_indent = None
                while i < n:
                    r2 = lines[i]
                    s2 = r2.strip()
                    if not s2 or s2.startswith('#'):
                        i += 1
                        continue
                    ind2 = len(r2) - len(r2.lstrip(' '))
                    if ind2 == 0:
                        break
                    if sub_indent is None:
                        sub_indent = ind2
                    if ind2 == sub_indent:
                        if s2.startswith('- '):
                            triggers.add(_unquote(_strip_comment(s2[2:])))
                        else:
                            sub_kv = _split_kv(s2)
                            if sub_kv:
                                triggers.add(sub_kv[0])
                    i += 1
        elif key == 'env':
            workflow_env, i = _consume_mapping(lines, i + 1, 0)
        elif key == 'jobs':
            i += 1
            job_indent = None
            while i < n:
                r2 = lines[i]
                s2 = r2.strip()
                if not s2 or s2.startswith('#'):
                    i += 1
                    continue
                ind2 = len(r2) - len(r2.lstrip(' '))
                if ind2 == 0:
                    break
                if job_indent is None:
                    job_indent = ind2
                if ind2 != job_indent:
                    i += 1
                    continue
                jkv = _split_kv(s2)
                if jkv is None:
                    i += 1
                    continue
                job_id, _ = jkv
                job = {'if': '', 'needs': [], 'env': {}, 'steps': []}
                i += 1
                while i < n:
                    r3 = lines[i]
                    s3 = r3.strip()
                    if not s3 or s3.startswith('#'):
                        i += 1
                        continue
                    ind3 = len(r3) - len(r3.lstrip(' '))
                    if ind3 <= job_indent:
                        break
                    fkv = _split_kv(s3)
                    if fkv is None:
                        i += 1
                        continue
                    fkey, frest = fkv
                    if fkey == 'if':
                        job['if'], i = _consume_scalar(lines, i, ind3, frest)
                    elif fkey == 'needs':
                        job['needs'], i = _consume_list_or_scalar(lines, i, ind3, frest)
                    elif fkey == 'env':
                        job['env'], i = _consume_mapping(lines, i + 1, ind3)
                    elif fkey == 'steps':
                        job['steps'], i = _parse_steps(lines, i + 1, ind3)
                    elif _BLOCK_SCALAR.match(frest):
                        _, i = _consume_scalar(lines, i, ind3, frest)
                    elif not _strip_comment(frest):
                        i = _skip_block(lines, i + 1, ind3)
                    else:
                        _, i = _consume_scalar(lines, i, ind3, frest)
                jobs[job_id] = job
        elif _BLOCK_SCALAR.match(rest):
            _, i = _consume_scalar(lines, i, 0, rest)
        elif not _strip_comment(rest):
            i = _skip_block(lines, i + 1, 0)
        else:
            _, i = _consume_scalar(lines, i, 0, rest)
    return {'triggers': sorted(triggers), 'env': workflow_env, 'jobs': jobs}


def _strip_expr_wrapper(expr: str) -> str:
    expr = expr.strip()
    if expr.startswith('${{') and expr.endswith('}}'):
        return expr[3:-2].strip()
    return expr


def _split_top_level_or(expr: str) -> list[str]:
    """Split an Actions expression on top-level `||`, respecting parentheses and quotes."""
    expr = _strip_expr_wrapper(expr)
    while expr.startswith('(') and expr.endswith(')'):
        depth = 0
        in_s = in_d = False
        wraps = True
        for i, ch in enumerate(expr):
            if in_s:
                if ch == "'":
                    in_s = False
            elif in_d:
                if ch == '"':
                    in_d = False
            else:
                if ch == "'":
                    in_s = True
                elif ch == '"':
                    in_d = True
                elif ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                    if depth == 0 and i < len(expr) - 1:
                        wraps = False
                        break
        if wraps and depth == 0:
            expr = expr[1:-1].strip()
        else:
            break
    clauses, buf = [], []
    depth = 0
    in_s = in_d = False
    i = 0
    n = len(expr)
    while i < n:
        ch = expr[i]
        if in_s:
            if ch == "'":
                in_s = False
            buf.append(ch)
            i += 1
        elif in_d:
            if ch == '"':
                in_d = False
            buf.append(ch)
            i += 1
        else:
            if ch == "'":
                in_s = True
                buf.append(ch)
                i += 1
            elif ch == '"':
                in_d = True
                buf.append(ch)
                i += 1
            elif ch == '(':
                depth += 1
                buf.append(ch)
                i += 1
            elif ch == ')':
                depth = max(0, depth - 1)
                buf.append(ch)
                i += 1
            elif depth == 0 and expr[i:i + 2] == '||':
                clauses.append(''.join(buf).strip())
                buf = []
                i += 2
            else:
                buf.append(ch)
                i += 1
    if buf:
        clauses.append(''.join(buf).strip())
    return [c for c in clauses if c]


def _clause_excludes_event(clause: str, event_name: str) -> bool:
    for m in _EVENT_NOT_EQUALS.finditer(clause):
        if (m.group(1) or m.group(2)) == event_name:
            return True
    equals = [m.group(1) or m.group(2) for m in _EVENT_EQUALS.finditer(clause)]
    return bool(equals) and event_name not in equals


def _expression_excludes_event(expr: str, event_name: str) -> bool:
    clauses = _split_top_level_or(expr)
    return bool(clauses) and all(_clause_excludes_event(c, event_name) for c in clauses)


def _workflow_run_clause_guarded(clause: str) -> bool:
    """True when one `||` branch cannot execute on an untrusted `workflow_run` event."""
    if _clause_excludes_event(clause, 'workflow_run'):
        return True
    # Strip enclosing parens if the whole branch is wrapped.
    nested = _split_top_level_or(clause)
    if len(nested) > 1:
        return all(_workflow_run_clause_guarded(sub) for sub in nested)
    return bool(_WORKFLOW_RUN_PUSH.search(clause) and _WORKFLOW_RUN_SAME_REPO.search(clause))


def _job_workflow_run_guarded(job_id: str, jobs: dict[str, dict], visiting: set[str] | None = None) -> bool:
    visiting = set() if visiting is None else visiting
    if job_id in visiting:
        return False
    visiting.add(job_id)
    job = jobs.get(job_id) or {}
    expr = job.get('if', '').strip()
    if expr:
        clauses = _split_top_level_or(expr)
        if clauses and all(_workflow_run_clause_guarded(c) for c in clauses):
            return True
    # Downstream job inherits guard from any required ancestor unless it explicitly bypasses failure/skip.
    if job.get('needs') and not _NEEDS_BYPASS.search(expr):
        for parent in job['needs']:
            if parent in jobs and _job_workflow_run_guarded(parent, jobs, set(visiting)):
                return True
    return False


def _is_checkout_step(step: dict) -> bool:
    uses = (step.get('uses') or '').strip().lower()
    return uses == 'actions/checkout' or uses.startswith('actions/checkout@')


def _resolve_env(value: str, *env_maps: dict[str, str]) -> str:
    merged: dict[str, str] = {}
    for mapping in env_maps:
        merged.update(mapping or {})
    if not merged or 'env.' not in value:
        return value
    return _ENV_REF.sub(lambda m: merged.get(m.group(1), m.group(0)), value)


def analyze_workflow(path: str, text: str) -> tuple[dict, list[dict]]:
    """Return `(parsed_workflow, findings)` for one GitHub Actions workflow file."""
    parsed = parse_workflow(text)
    triggers = set(parsed['triggers'])
    jobs = parsed['jobs']
    findings: list[dict] = []

    if 'workflow_run' in triggers:
        for job_id, job in jobs.items():
            if not _job_workflow_run_guarded(job_id, jobs):
                findings.append({
                    'workflow': path,
                    'job': job_id,
                    'trigger': 'workflow_run',
                    'rule': 'workflow_run_unguarded',
                    'reason': (f"{path} job '{job_id}' runs on workflow_run without requiring "
                               "github.event.workflow_run.event == 'push' and the same repository "
                               "(github.event.workflow_run.head_repository.full_name == github.repository)"),
                })
            if _expression_excludes_event(job.get('if', ''), 'workflow_run'):
                continue
            for step in job['steps']:
                if not _is_checkout_step(step):
                    continue
                with_map = step.get('with') or {}
                ref_val = _resolve_env(with_map.get('ref', ''), parsed['env'], job.get('env'), step.get('env'))
                repo_val = _resolve_env(with_map.get('repository', ''), parsed['env'], job.get('env'), step.get('env'))
                if _WR_TRIGGER_CHECKOUT.search(ref_val) or _WR_TRIGGER_CHECKOUT.search(repo_val):
                    findings.append({
                        'workflow': path,
                        'job': job_id,
                        'trigger': 'workflow_run',
                        'rule': 'workflow_run_trigger_checkout',
                        'reason': (f"{path} job '{job_id}' runs on workflow_run and checks out the triggering run's "
                                   f"commit ({ref_val or repo_val})"),
                    })
                    break

    if 'pull_request_target' in triggers:
        for job_id, job in jobs.items():
            if _expression_excludes_event(job.get('if', ''), 'pull_request_target'):
                continue
            for step in job['steps']:
                if not _is_checkout_step(step):
                    continue
                with_map = step.get('with') or {}
                ref_val = _resolve_env(with_map.get('ref', ''), parsed['env'], job.get('env'), step.get('env'))
                repo_val = _resolve_env(with_map.get('repository', ''), parsed['env'], job.get('env'), step.get('env'))
                if _PRT_HEAD_CHECKOUT.search(ref_val) or _PRT_HEAD_CHECKOUT.search(repo_val):
                    findings.append({
                        'workflow': path,
                        'job': job_id,
                        'trigger': 'pull_request_target',
                        'rule': 'pull_request_target_head_checkout',
                        'reason': (f"{path} job '{job_id}' runs on pull_request_target and checks out the pull "
                                   f"request head ({ref_val or repo_val})"),
                    })
                    break

    return parsed, findings


def _git_workflows(repo_dir: Path, ref: str, git: str) -> dict[str, str]:
    """Read `.github/workflows/*.yml|*.yaml` at `ref` via git, returning `{posix_path: text}`."""
    listed = subprocess.run([git, '-C', str(repo_dir), 'ls-tree', '-r', '--name-only', ref, '--', WORKFLOW_DIR],
                            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
    if listed.returncode != 0:
        return {}
    result: dict[str, str] = {}
    for rel in listed.stdout.splitlines():
        rel = rel.strip()
        if not rel.lower().endswith(WORKFLOW_SUFFIXES):
            continue
        shown = subprocess.run([git, '-C', str(repo_dir), 'show', f'{ref}:{rel}'],
                               capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
        if shown.returncode == 0:
            result[rel] = shown.stdout
    return result


def _resolve_base_ref(repo_dir: Path, base: str, git: str) -> str | None:
    for candidate in (f'origin/{base}', base):
        check = subprocess.run([git, '-C', str(repo_dir), 'rev-parse', '--quiet', '--verify', candidate],
                               capture_output=True, text=True, timeout=30)
        if check.returncode == 0:
            return candidate
    return None


def _job_signature(parsed: dict, job_id: str) -> dict:
    return {'triggers': parsed.get('triggers'), 'env': parsed.get('env'), 'job': parsed.get('jobs', {}).get(job_id)}


def _backlog_proposal(finding: dict) -> dict:
    slug = re.sub(r'[^a-z0-9]+', '-', f"{finding['workflow']}-{finding['job']}-{finding['rule']}".lower()).strip('-')
    return {
        'id': f'workflow-trust-{slug[:48]}',
        'title': f"Harden privileged workflow job {finding['workflow']}:{finding['job']} ({finding['rule']})",
        'workflow': finding['workflow'],
        'job': finding['job'],
        'rule': finding['rule'],
        'reason': finding['reason'],
    }


def inspect_repo(repo_dir, *, base: str = 'main', git: str | None = None) -> dict:
    """Compare HEAD workflows against the default branch (`origin/<base>`).

    Returns `{status, refused, existing, proposed_backlog_items}`:
    - `refused`: findings added or modified by this branch (blocks preflight before pushing).
    - `existing`: findings already present unchanged on the default branch (reported, not blocking).
    - `proposed_backlog_items`: one proposed backlog item per pre-existing finding.
    """
    repo_dir = Path(repo_dir)
    executable = git or host_executable('git', required=True)
    head_files = _git_workflows(repo_dir, 'HEAD', executable)
    base_ref = _resolve_base_ref(repo_dir, base, executable)
    base_files = _git_workflows(repo_dir, base_ref, executable) if base_ref else {}

    base_parsed: dict[str, dict] = {}
    base_findings_keys: set[tuple[str, str, str]] = set()
    for path, text in base_files.items():
        parsed, findings = analyze_workflow(path, text)
        base_parsed[path] = parsed
        for item in findings:
            base_findings_keys.add((item['workflow'], item['job'], item['rule']))

    refused: list[dict] = []
    existing: list[dict] = []
    for path in sorted(head_files):
        parsed, findings = analyze_workflow(path, head_files[path])
        old_parsed = base_parsed.get(path)
        for item in findings:
            key = (item['workflow'], item['job'], item['rule'])
            unchanged = (old_parsed is not None and key in base_findings_keys
                         and _job_signature(parsed, item['job']) == _job_signature(old_parsed, item['job']))
            if unchanged:
                existing.append(item)
            else:
                refused.append(item)

    status = 'REFUSED' if refused else ('REPORTED' if existing else 'OK')
    return {
        'status': status,
        'refused': refused,
        'existing': existing,
        'proposed_backlog_items': [_backlog_proposal(item) for item in existing],
    }


def write_receipt(folder: Path, *, repo: str | None, base: str, head_sha: str, report: dict) -> Path:
    """Persist a preflight workflow-trust receipt atomically under `folder/receipts/`."""
    receipts = Path(folder)/'receipts'
    receipts.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(f"{repo or ''}|{base}|{head_sha}".encode('utf-8')).hexdigest()[:12]
    target = receipts/f'workflow-trust-{digest}.json'
    payload = {
        'schema': RECEIPT_SCHEMA,
        'repo': repo,
        'base': base,
        'head_sha': head_sha,
        'status': report['status'],
        'refused': report['refused'],
        'existing': report['existing'],
        'proposed_backlog_items': report['proposed_backlog_items'],
        'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    tmp = target.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    tmp.replace(target)
    return target
