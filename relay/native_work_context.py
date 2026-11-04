"""Small explicit Work packet; code retrieval and write scope stay independent."""
import hashlib
import json


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def work_packet(descriptor, node, repositories, source_refs=()):
    criteria = [c.criterion for c in descriptor.verification_checks]
    selected = node.get('exit_criteria')
    if (not isinstance(selected, list) or any(not isinstance(c, str) for c in selected)
            or len(selected) != len(set(selected)) or set(selected) != set(criteria)):
        raise ValueError('NATIVE_WORK_CRITERIA_MISMATCH')
    if len(source_refs) > 16:
        raise ValueError('NATIVE_SOURCE_SELECTION_TOO_LARGE')
    refs = json.loads(encode(source_refs))
    for ref in refs:
        if (not isinstance(ref, dict) or set(ref) != {'id', 'repository', 'path', 'sha256', 'start', 'end'}
                or not isinstance(ref['id'], str) or not 1 <= len(ref['id']) <= 256
                or ref['repository'] not in repositories
                or not isinstance(ref['sha256'], str) or len(ref['sha256']) != 64
                or any(c not in '0123456789abcdef' for c in ref['sha256'])):
            raise ValueError('NATIVE_PINNED_SOURCE_INVALID')
    if len({ref['id'] for ref in refs}) != len(refs):
        raise ValueError('NATIVE_DUPLICATE_SOURCE_ID')
    scopes = node.get('repository_scopes', [])
    declared = [{'repository': s['repository'], 'ids': s.get('grounding_refs', [])} for s in scopes]
    packet = {'work_id': descriptor.work_id, 'role': 'implementer', 'title': node.get('title', ''),
        'goal': node.get('summary', node.get('description', '')), 'constraints': node.get('constraints', []),
        'criteria': criteria, 'verification_commands': {c.criterion: list(c.command) for c in descriptor.verification_checks},
        'descriptor_sha256': descriptor.sha256,
        'writable': {descriptor.execution_repository: list(descriptor.tracked_paths)},
        'readable_repositories': repositories, 'declared_grounding_refs': declared,
        'unresolved_grounding_refs': [{'repository': s['repository'], 'id': i} for s in declared for i in s['ids']
                                     if not any(r['id'] == i and r['repository'] == s['repository'] for r in refs)],
        'sources': refs, 'source_selection': 'EXPLICIT_HOST_PINS_NOT_AUTOMATIC_LIBRARY_RESOLUTION'}
    raw = encode(packet)
    if len(raw.encode()) > 16384:
        raise ValueError('NATIVE_REQUIRED_CONTEXT_OVER_BUDGET')
    return packet, hashlib.sha256(raw.encode()).hexdigest()
