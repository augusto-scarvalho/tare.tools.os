"""Read PLAN and Work owner receipts by caller identity, without executing either."""
import hashlib
from pathlib import Path

from compute_plane.native_journal import custody_lock, regular
from . import native_plan, relay_mesh, work_run
from .native_agent import read_json


def observe(selected, value, identity):
    root = Path(value['root']).resolve()
    if (Path(value['control_root']).resolve() != Path(relay_mesh.RELAY_ROOT).resolve()
            or Path(selected['root']).resolve() != root):
        raise ValueError('Flow project/control root changed.')
    mode = selected['mode']
    from . import flow_invocation
    if mode in flow_invocation.FIELDS:
        directory = selected[flow_invocation.FIELDS[mode]]
        if not Path(directory).is_absolute() or regular(directory).resolve().is_relative_to(root):
            raise ValueError('Saved flow custody must be absolute and outside the project.')
        return flow_invocation.observe(directory, identity, mode, root, value['control_root'])
    if mode == 'work':
        file = work_run.receipt_path(identity)
        lock = regular(work_run.lock_root_for(selected['work'], graph_path=selected.get('graph_path') or None))
    else:
        if not Path(selected['plan']).is_absolute():
            raise ValueError('Saved PLAN custody must be absolute.')
        lock = regular(selected['plan'])
        file = native_plan.result_path(lock, identity)
    if file.resolve().is_relative_to(root) or not regular(lock/'writer.lock').is_file():
        raise ValueError('No external owner completion custody is available yet.')
    with custody_lock(lock):
        result = read_json(file, 4*1024*1024)
        if Path(result['repository_root']).resolve() != root:
            raise ValueError('Owner receipt belongs to another project.')
        if mode == 'work':
            if (result.get('schema') != work_run.RUN_SCHEMA or result.get('run_id') != identity
                    or Path(result.get('graph_path') or relay_mesh.WORK_GRAPH_FILE).resolve() != Path(selected.get('graph_path') or relay_mesh.WORK_GRAPH_FILE).resolve()
                    or result.get('work_id') != selected['work'] or not result.get('status')):
                raise ValueError('Work receipt does not attest this completed attempt.')
            if not result.get('ended_at') or result['status'] == 'RUNNING':
                raise ValueError('Work interrupted after '+str(result.get('stage') or 'preparation')+
                    '. Writer released. Resume checks the accepted candidate and reconciles Git and backlog '
                    'before executing any remaining stage.')
        else:
            if (result.get('invocation_id') != identity or not result.get('invocation_ended_at')
                    or Path(result['plan_dir']).resolve() != lock.resolve()
                    or Path(result['graph_path']).resolve() != Path(selected.get('graph_path') or relay_mesh.WORK_GRAPH_FILE).resolve()
                    or not result.get('status') or result['status'] == 'RUNNING'):
                raise ValueError('PLAN receipt does not attest this completed invocation.')
        return result, [{'path': str(file), 'sha256': hashlib.sha256(file.read_bytes()).hexdigest()}]
