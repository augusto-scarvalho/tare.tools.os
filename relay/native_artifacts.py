"""Optional role handoffs: structured claims, observed sources, no admission authority."""
import hashlib
from pathlib import Path, PurePosixPath
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_core import PydanticCustomError
from compute_plane.native_journal import io_path, regular, save_json
from compute_plane.native_models import encode, object_json


class Record(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')


class Source(Record):
    repository: str = Field(min_length=1)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class Proposal(Record):
    summary: str = Field(min_length=1)
    sources: list[Source]
    remaining: list[str]


class Location(Record):
    repository: str = Field(min_length=1)
    path: str = Field(min_length=1)
    line: int = Field(ge=1)
    claim: str = Field(min_length=1)
    status: Literal['supported', 'hypothesis'] | None = Field(default=None, description=(
        'supported: the line you read shows the claim; hypothesis: the claim goes beyond what you read.'))


class Exploration(Proposal):
    locations: list[Location] = Field(default_factory=list, max_length=100, description=(
        'Each place that answers the question: its file, line and what is there. Lists of places go here, not in '
        'the summary. tare checks that each line exists and merges the locations of explorers working together.'))
    conclusion: Literal['answered', 'partial', 'inconclusive'] | None = Field(default=None, description=(
        'answered: the locations answer the question; partial: parts remain open; inconclusive: what you read '
        'does not settle it, which is not the same as finding nothing.'))


class GraphTask(Record):
    key: str = Field(pattern=r'^[a-z0-9][a-z0-9-]{0,47}$')
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    cluster: str = Field(min_length=1)
    priority: Literal['P0', 'P1', 'P2', 'P3']
    criteria: list[str] = Field(min_length=1, max_length=64)
    target_paths: list[str] = Field(min_length=1, max_length=64)
    depends_on: list[str] = Field(max_length=64)
    # Optional checks of the task (ways of working, D6): the session's verifiers by name and existing test files
    # (with pytest, file::test names one test).
    verifiers: list[str] = Field(default_factory=list, max_length=16)
    test_paths: list[str] = Field(default_factory=list, max_length=64)


class Plan(Proposal):
    steps: list[str] = Field(min_length=1)
    criteria: list[str] = Field(min_length=1)
    tasks: list[GraphTask] = Field(default_factory=list, max_length=32)


class Finding(Record):
    claim: str = Field(min_length=1)
    sources: list[Source] = Field(min_length=1)
    paths: list[str] = Field(default_factory=list, max_length=16, description=(
        'Repository-relative files that must change to resolve this finding, including files outside the plan\'s '
        'target paths: tare then asks the planner to add them, and the user approves the new files.'))
    repeated: bool = Field(default=False, description=(
        'true only when unit.review_feedback already reported this same defect and this candidate still has it: '
        'tare then stops and asks the user instead of paying for another correction.'))


class Review(Proposal):
    verdict: Literal['ACCEPT', 'REVISE', 'INCONCLUSIVE']
    findings: list[Finding] = Field(description=(
        'Unresolved defects or evidence gaps only, never positive observations. '
        'ACCEPT requires findings=[] and remaining=[]. Put supporting observations in summary and sources.'))


MODELS = {'planner': Plan, 'implementer': Proposal, 'plan-auditor': Review,
          'auditor': Review, 'explorer': Exploration}
KINDS = {'planner': 'plan', 'implementer': 'implementation', 'plan-auditor': 'review',
         'auditor': 'review', 'explorer': 'exploration'}


def schema(role):
    return MODELS[role].model_json_schema()


def validate(role, value):
    if len(encode(value).encode()) > 32768:
        raise ValueError('native_artifact_over_budget')
    result = MODELS[role].model_validate(value).model_dump()
    if role == 'planner' and 'tasks' not in value:
        result.pop('tasks')  # Preserve existing handoff bytes when graph planning is unused.
    if role == 'explorer':
        # Likewise for an explorer that lists no places, concludes nothing or marks no location.
        if 'locations' not in value:
            result.pop('locations')
        if 'conclusion' not in value:
            result.pop('conclusion')
        for location in result.get('locations', []):
            if location['status'] is None:
                location.pop('status')
    texts = [result['summary'], *result['remaining'], *result.get('steps', []), *result.get('criteria', [])]
    if any(not text.strip() for text in texts):
        raise ValueError('native_artifact_empty_text')
    findings = result.get('findings', [])
    for finding, given in zip(findings, value.get('findings', [])):
        for key in ('paths', 'repeated'):
            if key not in given:
                finding.pop(key)  # likewise for a review naming no files to change or repeated defect
    claims = [' '.join(f['claim'].casefold().split()) for f in findings]
    if any(not claim for claim in claims) or len(claims) != len(set(claims)):
        raise ValueError('native_artifact_duplicate_or_empty_finding')
    if result.get('verdict') == 'ACCEPT' and (findings or result['remaining']):
        raise ValueError('native_artifact_accept_with_open_findings')
    for path in [s['path'] for s in result['sources'] + [s for f in findings for s in f['sources']]
                 + result.get('locations', [])] + [p for f in findings for p in f.get('paths', [])]:
        if ('\\' in path or ':' in path or PurePosixPath(path).is_absolute()
                or '..' in PurePosixPath(path).parts or '.git' in PurePosixPath(path).parts):
            raise ValueError('native_artifact_relative_source_required')
    return result


NOT_ACCEPTED = ('tare read this review as not accepted: the auditor answered ACCEPT while listing open findings or '
                'remaining items. ')


def validator(role, tools, roots, *, versioned_targets=False, same_family_audit=False):
    """same_family_audit: only the local-only conversation review of a same-family candidate sets it (config).
    There an ACCEPT that lists open items is read as not accepted (REVISE with findings, else INCONCLUSIVE), its
    items kept, instead of discarding the whole review; validate() and every other auditor keep refusing it."""
    def check(value):
        if (same_family_audit and role == 'auditor' and isinstance(value, dict) and value.get('verdict') == 'ACCEPT'
                and (value.get('findings') or value.get('remaining'))):
            value['verdict'] = 'REVISE' if value.get('findings') else 'INCONCLUSIVE'
            if isinstance(value.get('summary'), str):
                value['summary'] = NOT_ACCEPTED + value['summary']
        reads = tools.observations()['reads']
        def observed(source):
            return reads.get(f"{source.get('repository')}:{source.get('path')}") if isinstance(source, dict) else None
        sources = value.get('sources') if isinstance(value, dict) else None
        if isinstance(sources, list):
            findings = value.get('findings') if isinstance(value.get('findings'), list) else []
            for source in sources + [s for f in findings if isinstance(f, dict) and isinstance(f.get('sources'), list)
                                     for s in f['sources']]:
                while isinstance(source, dict) and isinstance(source.get('path'), str) and source['path'].startswith('./'):
                    source['path'] = source['path'][2:]  # the tools record './x' as x
            if role == 'explorer' and isinstance(value.get('remaining'), list):
                # A leaf citing a file it only found by search reports a gap; that alone never discards its report.
                value['remaining'] = value['remaining'] + [f"Not read in this session: {s['path']}" for s in sources
                    if isinstance(s, dict) and isinstance(s.get('path'), str) and not observed(s)]
                value['sources'] = sources = [s for s in sources if observed(s)]
            for source in sources + [s for f in findings if isinstance(f, dict) and isinstance(f.get('sources'), list)
                                     for s in f['sources']]:
                if observed(source) and not re.fullmatch(r'[a-f0-9]{64}', str(source.get('sha256'))):
                    # A missing or miscopied digest comes from this session's own read; a different
                    # well-formed one still names another version and is refused below.
                    source['sha256'] = observed(source)
        if role == 'explorer' and isinstance(value, dict) and isinstance(value.get('locations'), list) and value['locations']:
            # An explorer listing places sometimes leaves out what tare can derive (one did: the whole delivery
            # was refused and its 17 judged candidates lost).
            value.setdefault('sources', [])
            value.setdefault('remaining', [])
            if not isinstance(value.get('summary'), str) or not value['summary'].strip():
                value['summary'] = f"{len(value['locations'])} locations listed."
        if role == 'explorer' and isinstance(value, dict) and isinstance(value.get('locations'), list):
            value['locations'], missing = existing(value['locations'], roots)
            if missing and isinstance(value.get('remaining'), list):
                value['remaining'] = value['remaining'] + [f'No such line: {where}' for where in missing]
        result = validate(role, value)
        if versioned_targets and role == 'planner' and result.get('tasks'):
            from .conversation_workspace import ignored_targets
            if len(roots) != 1:
                raise ValueError('Plan one project at a time; select the project before implementation.')
            paths = list(dict.fromkeys(path for task in result['tasks'] for path in task['target_paths']))
            ignored = ignored_targets(next(iter(roots.values())), paths)
            if ignored:
                raise ValidationError.from_exception_data('ExecutablePlan', [
                    {'loc': ('tasks',i,'target_paths',j), 'type': PydanticCustomError(
                        'native_artifact_ignored_target','Choose a versioned target path; Git ignores this output.')}
                    for i,task in enumerate(result['tasks']) for j,path in enumerate(task['target_paths']) if path in ignored])
        citations=[(('sources',i),s) for i,s in enumerate(result['sources'])]
        citations += [(('findings',i,'sources',j),s) for i,f in enumerate(result.get('findings',[]))
                      for j,s in enumerate(f['sources'])]
        for location,source in citations:
            repo, path, digest = source['repository'], source['path'], source['sha256']
            if repo not in roots or reads.get(repo+':'+path) != digest:
                raise ValidationError.from_exception_data('ArtifactEvidence',[{'loc':location,
                    'type':PydanticCustomError('native_artifact_source_not_observed',
                        'Cite only a reference actually read in this session, using its observed hash.')}])
            file = regular(io_path(Path(roots[repo])/path))
            if not file.is_file() or file.stat().st_size > 2*1024*1024 or hashlib.sha256(file.read_bytes()).hexdigest() != digest:
                raise ValueError('native_artifact_source_changed')
        return result
    return check


def existing(locations, roots):
    """The locations whose file and line exist (a path written './x' is x), and 'path:line' of the others. A
    location may come from a search without a read; that its line exists is what tare can check."""
    kept, missing, lines = [], [], {}
    for location in locations:
        if not isinstance(location, dict) or not isinstance(location.get('path'), str):
            kept.append(location)  # malformed: left for validation to refuse
            continue
        while location['path'].startswith('./'):
            location['path'] = location['path'][2:]
        key = (location.get('repository'), location['path'])
        if key not in lines:
            try:
                file = regular(io_path(Path(roots[key[0]])/key[1]))
                lines[key] = file.read_bytes().count(b'\n') + 1 if file.is_file() and file.stat().st_size <= 2*1024*1024 else 0
            except (KeyError, ValueError, OSError):
                lines[key] = 0
        if type(location.get('line')) is int and 1 <= location['line'] <= lines[key]:
            kept.append(location)
        else:
            missing.append(f"{location['path']}:{location.get('line')}")
    return kept, missing


def read_input(path):
    file = regular(io_path(path))
    with file.open('rb') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError('native_input_artifact_over_budget')
    value = object_json(raw)
    if (set(value) != {'schema', 'role', 'kind', 'authority', 'session_id', 'call_id', 'profile_digest', 'payload'}
            or value['schema'] != 'tare.tools/native-artifact/1' or value['authority'] != 'NONE'
            or value['role'] not in MODELS or value['kind'] != KINDS[value['role']]):
        raise ValueError('native_input_artifact_contract')
    validate(value['role'], value['payload'])
    if (any(not isinstance(value[k], str) or not value[k].strip() for k in ('session_id', 'call_id'))
            or not isinstance(value['profile_digest'], str) or len(value['profile_digest']) != 64
            or any(c not in '0123456789abcdef' for c in value['profile_digest'])):
        raise ValueError('native_input_artifact_identity')
    return {'sha256': hashlib.sha256(raw).hexdigest(), 'artifact': value,
            'source_verification': 'UNVERIFIED_IN_THIS_SESSION', 'authority': 'NONE'}


def save_output(root, role, session_id, profile_digest, journal):
    event = next(r.event() for r in reversed(journal.snapshot().records) if r.event()['kind'] == 'model_returned')
    payload = object_json(event['reply']['metadata_json'])['delivery_payload']
    value = {'schema': 'tare.tools/native-artifact/1', 'role': role, 'kind': KINDS[role],
        'authority': 'NONE', 'session_id': session_id, 'call_id': event['call_id'],
        'profile_digest': profile_digest, 'payload': validate(role, payload)}
    directory = regular(root/'artifacts'); regular(io_path(directory)).mkdir(exist_ok=True)
    file = regular(directory/(hashlib.sha256(event['call_id'].encode()).hexdigest()+'.json'))
    save_json(io_path(file), value)
    save_json(io_path(root/'artifact.json'), value)
    return str(file)
