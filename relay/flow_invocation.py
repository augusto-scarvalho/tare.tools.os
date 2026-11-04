"""Optional caller receipt around an existing compound flow, never another loop."""
from datetime import datetime, timezone
from pathlib import Path

from compute_plane.native_journal import custody_lock, regular, save_json
from . import relay_mesh
from .native_agent import read_json

FIELDS = {'parallel': 'batch', 'research': 'research', 'sequence': 'sequence', 'synthesis': 'synthesis'}
RESULT_FIELDS = {**FIELDS, 'research': 'directory', 'synthesis': 'directory'}


def receipt_path(directory, identity):
    if (not isinstance(identity, str) or len(identity) != 32
            or any(c not in '0123456789abcdef' for c in identity)):
        raise ValueError('FLOW_INVOCATION_ID_INVALID')
    return regular(Path(directory)/'invocation-results'/identity/'result.json')


def record(directory, identity, mode, state_file, action):
    if identity is None:
        return action()
    directory = regular(directory).resolve()
    file = receipt_path(directory, identity)
    state = read_json(directory/state_file, 4*1024*1024)
    root = Path(state['config']['repository_root'] if mode == 'sequence' else state['root']).resolve()
    if Path(state['directory']).resolve() != directory or directory.is_relative_to(root):
        raise ValueError('FLOW_INVOCATION_REQUIRES_BOUND_EXTERNAL_CUSTODY')
    # An interrupted caller cannot reuse this ID, even if no outcome was written.
    file.parent.mkdir(parents=True, exist_ok=False)
    with custody_lock(file.parent):
        from dataclasses import asdict
        from .execution_scope import current
        receipt = {'schema': 'tare.tools/flow-invocation/1', 'invocation_id': identity,
            'execution_scope': asdict(current()),
            'mode': mode, 'directory': str(directory), 'repository_root': str(root),
            'control_root': str(Path(relay_mesh.RELAY_ROOT).resolve()), 'state': 'RUNNING', 'result': None}
        save_json(file, receipt)
        result = action()  # Owner locks, cancellation, evidence and resumption stay in the owner.
        if (Path(result['root']).resolve() != root or Path(result[RESULT_FIELDS[mode]]).resolve() != directory
                or not result.get('status') or result['status'] in {'RUNNING', 'COLLECTING', 'ANALYZING'}):
            raise ValueError('FLOW_INVOCATION_RESULT_BINDING_OR_COMPLETION_CHANGED')
        result = {**result, 'invocation_id': identity}
        receipt.update(state='FINISHED', result=result, finished_at=datetime.now(timezone.utc).isoformat())
        save_json(file, receipt)
        return result


def observe(directory, identity, mode, root, control_root):
    file = receipt_path(directory, identity)
    if not regular(file.parent/'writer.lock').is_file():
        raise ValueError('No invocation completion custody exists yet.')
    with custody_lock(file.parent):
        receipt = read_json(file, 4*1024*1024)
        result = receipt.get('result')
        if (receipt.get('schema') != 'tare.tools/flow-invocation/1' or receipt.get('invocation_id') != identity
                or receipt.get('mode') != mode or Path(receipt['directory']).resolve() != Path(directory).resolve()
                or Path(receipt['repository_root']).resolve() != Path(root).resolve()
                or Path(receipt['control_root']).resolve() != Path(control_root).resolve()
                or receipt.get('state') != 'FINISHED' or not receipt.get('finished_at')
                or not result or result.get('invocation_id') != identity
                or Path(result['root']).resolve() != Path(root).resolve()
                or Path(result[RESULT_FIELDS[mode]]).resolve() != Path(directory).resolve()
                or not result.get('status') or result['status'] in {'RUNNING', 'COLLECTING', 'ANALYZING'}):
            raise ValueError('Flow invocation is incomplete or does not match the requested owner.')
        import hashlib
        return result, [{'path': str(file), 'sha256': hashlib.sha256(file.read_bytes()).hexdigest()}]
