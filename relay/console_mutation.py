"""One completion receipt for a confirmed console mutation; never replay an action."""
import hashlib
from pathlib import Path

from compute_plane.native_journal import custody_lock, regular, save_json
from graph_backlog.jsonutil import sha256_canonical
from . import console_demand, relay_mesh
from .native_agent import read_json

OPERATIONS = {'graph_apply_requested': 'graph_applied', 'preparation_requested': 'work_prepared',
              'sequence_revision_requested': 'sequence_revised',
              'work_order_requested': 'work_order_applied', 'work_replacement_requested': 'work_replacement_applied',
              'revision_requested': 'work_revised', 'decision_requested': 'decision_observed',
              'work_control_requested': 'work_control_observed'}


def binding(value, event):
    if event['kind'] not in OPERATIONS:
        raise ValueError('Unsupported console mutation.')
    root, control = Path(value['root']).resolve(), Path(value['control_root']).resolve()
    if (control != Path(relay_mesh.RELAY_ROOT).resolve()
            or Path(event['selection']['root']).resolve() != root):
        raise ValueError('Mutation project/control binding differs from this console.')
    identity = event['detail'].get('mutation_id')
    if (not isinstance(identity, str) or len(identity) != 32
            or any(c not in '0123456789abcdef' for c in identity)):
        raise ValueError('No qualified mutation identity. Inspect the owner receipts.')
    directory = regular(console_demand.path(value['id']).parent/'mutations'/identity)
    if directory.resolve().is_relative_to(root):
        raise ValueError('Mutation custody must be outside the project.')
    return directory, {'demand_id': value['id'], 'root': str(root), 'control_root': str(control),
        'kind': event['kind'], 'selection': event['selection'], 'detail': event['detail']}


def record(value, action):
    event = value['events'][-1]
    directory, request = binding(value, event)
    # Reserve before invoking the owner. A crash never makes this identity reusable.
    directory.mkdir(parents=True, exist_ok=False)
    with custody_lock(directory):
        receipt = {'schema': 'tare.tools/console-mutation/1', 'request': request,
                   'state': 'RUNNING', 'result': None}
        save_json(directory/'result.json', receipt)
        result = action()
        if not isinstance(result, dict) or not result.get('status'):
            raise ValueError('Mutation returned no structured result.')
        receipt.update(state='FINISHED', result=result, result_sha256=sha256_canonical(result))
        save_json(directory/'result.json', receipt)
        return result


def observe(value, index, event):
    mode = event['selection']['mode']
    field = ('sequence' if event['kind'] == 'sequence_revision_requested' else
             'plan' if event['kind'] == 'graph_apply_requested' else 'work')
    observation = {'request_event': index, 'mode': mode, 'owner_field': field,
        'owner': event['selection'].get(field), 'state': 'UNKNOWN', 'result': None,
        'operation': event['kind']}
    try:
        directory, request = binding(value, event)
        if not regular(directory/'writer.lock').is_file():
            raise ValueError('No mutation completion receipt. Inspect before retrying.')
        with custody_lock(directory):
            file = regular(directory/'result.json')
            receipt = read_json(file, 4*1024*1024)
            result = receipt.get('result')
            if (receipt.get('schema') != 'tare.tools/console-mutation/1' or receipt.get('request') != request
                    or receipt.get('state') != 'FINISHED' or not isinstance(result, dict)
                    or not result.get('status') or receipt.get('result_sha256') != sha256_canonical(result)):
                raise ValueError('Mutation completion is missing or differs from this request.')
            return {**observation, 'state': 'RECOVERED', 'result': result,
                'receipts': [{'path': str(file), 'sha256': hashlib.sha256(file.read_bytes()).hexdigest()}],
                'reason': 'Recovered confirmed operation: '+result['status']+
                    '. Historical result; current readiness and approvals must be checked before execution.'}
    except Exception as exc:
        return {**observation, 'state': 'BUSY' if str(exc) == 'native_custody_has_active_writer' else 'UNKNOWN',
                'reason': str(exc)}
