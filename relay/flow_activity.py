"""Scoped presentation for TUI-launched flows; flow owners retain execution."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
from pathlib import Path
import sys
from threading import Lock


_stream = ContextVar('flow_activity_stream', default=None)
MODES = {'parallel-explore': ('parallel', '--batch'), 'research-flow': ('research', '--research'),
         'plan': ('plan', '--plan'), 'sequence': ('sequence', '--sequence'), 'run': ('work', None)}


@contextmanager
def stream(mode, owner, project, callback=None):
    """Enable only within this invocation; a worker gets an explicit context copy."""
    lock = Lock()

    def emit(event):
        with lock:
            if callback is not None:
                callback(event)
            else:
                sys.stderr.write(json.dumps(event, ensure_ascii=False)+'\n')
                sys.stderr.flush()

    token = _stream.set({'mode': mode, 'owner': str(owner), 'project': str(Path(project).resolve()),
                         'callback': emit})
    try:
        yield
    finally:
        _stream.reset(token)


@contextmanager
def work_stage(work_id, *, graph_path=None):
    current = _stream.get()
    token = _stream.set({**current, 'work': work_id, 'graph_path': str(graph_path) if graph_path else ''}) if current is not None else None
    try:
        yield
    finally:
        if token is not None:
            _stream.reset(token)


def observer(custody, role, *, kind='session'):
    current = _stream.get()
    if current is None:
        return None, None
    from . import relay_mesh
    source = {key: current[key] for key in ('mode', 'owner', 'project')}
    source.update(session=str(Path(custody).resolve()), kind=kind,
                  control_root=str(Path(relay_mesh.RELAY_ROOT).resolve()), work=current.get('work', ''))
    if source['mode'] == 'parallel':
        stage = 'Explore '+Path(custody).name
    elif source['mode'] == 'research':
        stage = 'Research'
    else:
        phase = {'planner': 'Plan draft', 'plan-auditor': 'Plan review',
                 'auditor': 'Code review', 'implementer': 'Implement'}.get(role, role)
        stage = 'Work '+source['work']+' · '+phase if source['work'] else phase
    source['stage'] = stage
    if current.get('graph_path'):
        source['graph_path'] = current['graph_path']
    return current['callback'], source


def main(argv=None):
    """TUI subprocess entry. Ordinary CLI flows keep their existing output contract."""
    from .native_installation import main as run
    args = list(sys.argv[1:] if argv is None else argv)
    mode, flag = MODES[args[0]]
    owner = args[1] if flag is None else str(Path(args[args.index(flag)+1]).resolve())
    for output in (sys.stdout, sys.stderr):
        getattr(output, 'reconfigure', lambda **kw: None)(encoding='utf-8')
    with stream(mode, owner, Path.cwd()):
        return run(args)


def validate_source(source, project):
    """Resolve saved child custody through the selected owner's persisted scope."""
    from . import relay_mesh
    from .native_agent import read_json
    project, session = Path(project).resolve(), Path(source['session']).resolve()
    if (Path(source['project']).resolve() != project
            or Path(source['control_root']).resolve() != Path(relay_mesh.RELAY_ROOT).resolve()):
        raise ValueError('The tool activity belongs to another project or control root.')
    mode, owner = source['mode'], source['owner']
    if mode in {'parallel', 'research'}:
        from . import parallel_explore, research_flow
        directory = Path(owner).resolve()
        state = (parallel_explore if mode == 'parallel' else research_flow).read(directory)
        allowed = [directory/row['id'] for row in state['items']] if mode == 'parallel' else [directory/'analyst']
        if Path(state['root']).resolve() != project or session not in allowed or source['kind'] != 'session':
            raise ValueError('The saved tool session does not belong to this flow.')
        return session, project if mode == 'parallel' else directory/'sources'
    if mode == 'plan':
        directory = Path(owner).resolve()
        allowed = [directory/'planner', directory/'auditor']
        if (directory/'review-attempts.json').is_file():
            allowed += [directory/row['directory'] for row in read_json(directory/'review-attempts.json')['attempts']]
        if session.parent != directory or session not in allowed or source['kind'] != 'session':
            raise ValueError('The saved tool session does not belong to this plan.')
        return session, project
    if mode not in {'work', 'sequence'}:
        raise ValueError('Unknown activity owner.')
    work_id = source['work']
    if mode == 'sequence':
        from .work_sequence import inspect, FILE
        state = inspect(owner)
        if (Path(state['sequence']).resolve() != Path(owner).resolve()
                or Path(state['root']).resolve() != project or work_id not in state['requested_order']
                or read_json(Path(owner)/FILE)['graph_path'] != str(Path(relay_mesh.WORK_GRAPH_FILE).resolve())):
            raise ValueError('The Work does not belong to this sequence.')
    elif work_id != owner:
        raise ValueError('The activity belongs to another Work.')
    from .work_run import train_dir_for
    origin = read_json(train_dir_for(work_id, graph_path=source.get('graph_path') or None)/'WORK_RUN.json')
    if (origin.get('schema') != 'tare.tools/work-run-origin/1' or origin['work_id'] != work_id
            or Path(origin['repository_root']).resolve() != project):
        raise ValueError('The Work belongs to another project.')
    implementation = Path(origin['implementation_custody']).resolve()
    if session == implementation and source['kind'] == 'attempt':
        attempt = read_json(session/'attempt.json', 4*1024*1024)['identity']
        if (attempt['schema'] != 'tare.tools/native-attempt/1' or attempt['binding']['work_id'] != work_id
                or attempt['descriptor_sha256'] != origin['descriptor_sha256']):
            raise ValueError('The recorded implementation identity changed.')
        return session, None  # Validated Work custody uses attempt.json, not native-agent.json.
    if source['kind'] != 'session':
        raise ValueError('Unsupported saved tool custody.')
    plans = [origin['plan_custody']] if origin.get('plan_custody') else []
    plans += origin.get('plan_audit_attempts', [])
    if any(session == Path(path).resolve() for path in plans):
        return session, project
    from .work_audit import audit_custody
    roots = [audit_custody(origin), *map(Path, origin.get('audit_attempts', []))]
    if any(session == root.resolve()/str(i) for root in roots for i, _ in enumerate(origin.get('audit', {}).get('routes', []))):
        attempt = read_json(implementation/'attempt.json', 4*1024*1024)['identity']
        if attempt['descriptor_sha256'] != origin['descriptor_sha256'] or attempt['binding']['work_id'] != work_id:
            raise ValueError('The audited implementation identity changed.')
        return session, Path(attempt['worktree']).resolve()
    raise ValueError('The saved tool session does not belong to this Work.')
