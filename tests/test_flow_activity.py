import asyncio
import json
from pathlib import Path

import pytest
from textual.widgets import Select, TextArea

from relay import flow_activity, parallel_explore, research_flow, work_run
from relay.native_activity import activity_key, full_output
from relay.console_output import ToolActivity
from relay.native_console import WorkConsole
from test_native_agent import setup
from test_parallel_explore import exploration
from test_research_flow import study
from test_native_console import FakeBackend
from test_native_settlement import runner_for, os_root, evidence
from test_native_plan import proposal
from test_work_resume import host
from test_work_sequence import sequence


def test_parallel_workers_emit_bound_records_and_resume_does_not_replay(exploration):
    directory, root, clients, _ = exploration
    events = []
    with flow_activity.stream('parallel', directory, root, events.append):
        result = parallel_explore.execute(directory)
        assert parallel_explore.execute(directory)['status'] == 'COMPLETED'
    assert result['status'] == 'COMPLETED' and len(events) == 8
    assert all(client.generations == 2 for client in clients)
    agents = [e['activity'] for e in events if e['activity']['operation'] == 'agent_run']
    assert len(agents) == 4 and all(a['source']['mode'] == 'parallel' and a['actor']['role'] == 'explorer' for a in agents)
    rows = [event['activity'] for event in events if event['activity']['state'] == 'succeeded' and event['activity']['operation'] != 'agent_run']
    assert {Path(row['source']['session']).name for row in rows} == {'1', '2'}
    assert len({activity_key(row) for row in rows}) == 2
    (directory/'other').mkdir()
    for row in rows:
        assert 'value = 1' in full_output(None, root, row)
        with pytest.raises(ValueError, match='another project'):
            full_output(None, root.parent, row)
        with pytest.raises(ValueError, match='does not belong'):
            full_output(None, root, {**row, 'source': {**row['source'], 'session': str(directory/'other')}})
    assert flow_activity.observer(directory, 'explorer') == (None, None)


def test_research_activity_resolves_saved_sources_through_original_project(study):
    directory, root, clients, fetched, collect = study
    events = []
    with flow_activity.stream('research', directory, root, events.append):
        result = research_flow.execute(directory, fetch=collect)
    assert result['status'] == 'COMPLETED' and len(clients) == len(fetched) == 1
    row = next(e['activity'] for e in reversed(events) if e['activity']['operation'] != 'agent_run')
    assert row['state'] == 'succeeded' and '5 minutes' in full_output(None, root, row)
    assert row['source']['stage'] == 'Research'
    with pytest.raises(ValueError, match='another project'):
        full_output(None, root.parent, row)


def test_failed_presentation_does_not_repeat_parallel_tools(exploration):
    directory, root, clients, _ = exploration
    observed = []
    def broken(event):
        observed.append(event)
        raise OSError('closed display')
    with flow_activity.stream('parallel', directory, root, broken):
        assert parallel_explore.execute(directory)['status'] == 'COMPLETED'
        assert parallel_explore.execute(directory)['status'] == 'COMPLETED'
    assert len(observed) == 8 and all(client.generations == 2 for client in clients)


def test_work_resume_preserves_effects_with_activity_enabled(os_root, tmp_path, monkeypatch):
    from compute_plane import candidate_git
    from test_native_settlement import prepared
    runner, args = prepared(os_root, tmp_path)
    events = []
    original = candidate_git.pin_candidate_commit
    def interrupted(*a, **kw):
        original(*a, **kw)
        raise SystemExit('crash after pin')
    with flow_activity.stream('work', 'work-one', os_root[1], events.append), flow_activity.work_stage('work-one'):
        with monkeypatch.context() as patch:
            patch.setattr(candidate_git, 'pin_candidate_commit', interrupted)
            with pytest.raises(SystemExit):
                runner.run(**args)
        count = len(events)
        result = runner.run(**args, resume=True)
        assert result['status'] == 'PROPOSED'
        assert runner.run(**args, resume=True)['settlement'] == result['settlement']
        assert count == len(events) == 8
    assert len(runner.settings['client'].generations) == 5
    assert (args['lease'].root/'src/value.txt').read_text() == 'strict\n'


def test_plan_and_review_attempts_keep_their_own_output(proposal, monkeypatch):
    import hashlib
    from relay import native_agent, native_plan
    from test_native_agent import Client, deployment
    directory, graph, value, _ = proposal
    target = directory.parent/'new-plan'
    root = directory.parent/'project'
    original = native_agent.run_session
    clients, events = [], []
    def run(config, selected, custody, instruction, **kwargs):
        if config['role'] == 'planner':
            steps = [('read', {'repository': 'project', 'path': 'a.py'}), ('tare_deliver', value['plan'])]
        else:
            steps = [('read', {'repository': 'proposal', 'path': name}) for name in ('proposal.json', 'request.json')]
            sources = [{'repository': 'proposal', 'path': name,
                        'sha256': hashlib.sha256((target/'input'/name).read_bytes()).hexdigest()}
                       for name in ('proposal.json', 'request.json')]
            steps += [('tare_deliver', {'summary': 'Reviewed', 'verdict': 'ACCEPT', 'findings': [],
                                       'remaining': [], 'sources': sources})]
        client = Client(steps=steps)
        if config['role'] == 'plan-auditor' and len(clients) == 1:
            from tare_tools_agent_runtime import CancellationToken
            cancellation = CancellationToken()
            kwargs['cancellation'] = cancellation
            client.after_model = lambda: cancellation.cancel() if client.generations == 3 else None
        clients.append(client)
        return original(config, selected, custody, instruction, client=client, local_counter=client, **kwargs)
    monkeypatch.setattr(native_agent, 'run_session', run)
    before = graph.read_bytes()
    with flow_activity.stream('plan', target, root, events.append):
        planned = native_plan.generate(root, graph, target, 'Change and test', deployment(role='planner'))
        assert planned['status'] == 'REVIEW_REQUIRED'
        route = planned['routes']['plan-auditor']
        assert route['independent'] is False and 'selection' not in route
        assert route['selection_attempt']['reason'] == 'PRODUCER_LINEAGE_UNAVAILABLE'
        reviewed = native_plan.retry_review(target)
        assert reviewed['status'] == 'REVIEWED' and 'not attested' in reviewed['review']['independence']
    assert len(clients) == 3 and graph.read_bytes() == before
    rows = [e['activity'] for e in events if e['activity']['state'] == 'succeeded' and e['activity']['operation'] != 'agent_run']
    assert len(rows) == 5 and len({r['source']['session'] for r in rows}) == 3
    for row in rows:
        assert json.loads(full_output(None, root, row))
    assert 'value = 1' in full_output(None, root, rows[0])


def test_work_implementation_activity_does_not_change_settlement(os_root, tmp_path):
    _, root = os_root
    from relay import relay_mesh
    node = work_run.load_node(relay_mesh.WORK_GRAPH_FILE, 'work-one')
    runner = runner_for(tmp_path, node)
    events = []
    with flow_activity.stream('work', 'work-one', root, events.append):
        result = work_run.run_work('work-one', repository_root=root, native_runner=runner,
            plan_auditor=lambda *a: {'verdict': 'ACCEPT', 'findings': []}, evidence_factory=evidence)
    assert result['status'] == 'SETTLED', result
    assert len(runner.settings['client'].generations) == 5
    rows = [event['activity'] for event in events if event['activity']['state'] == 'succeeded']
    assert {'read', 'edit', 'verify'} <= {row['operation'] for row in rows}
    assert all(row['source']['work'] == 'work-one' and row['source']['kind'] == 'attempt' for row in rows)
    assert json.loads(full_output(None, root, rows[0]))
    with pytest.raises(ValueError, match='another Work|does not belong'):
        full_output(None, root, {**rows[0], 'source': {**rows[0]['source'], 'work': 'other-work'}})


def test_sequence_activity_follows_each_work_and_refuses_rebound_graph(sequence):
    from relay import work_sequence
    from test_work_sequence import create
    create(sequence)
    owner, root, events = sequence['dir'], sequence['repo'], []
    with flow_activity.stream('sequence', owner, root, events.append):
        result = work_sequence.execute(owner)
        assert result['status'] == 'COMPLETED', result
        assert work_sequence.execute(owner)['status'] == 'COMPLETED'
    assert len(sequence['calls']) == 10
    rows = [e['activity'] for e in events if e['activity']['state'] == 'succeeded']
    assert {row['source']['work'] for row in rows} == {'work-one', 'work-two'}
    for row in rows:
        assert json.loads(full_output(None, root, row))
    state = json.loads((owner/'sequence.json').read_bytes())
    state['graph_path'] = str(owner/'another-graph.json')
    (owner/'sequence.json').write_text(json.dumps(state))
    with pytest.raises(ValueError, match='does not belong'):
        full_output(None, root, rows[0])


@pytest.mark.parametrize('size', [(80, 24), (130, 44)])
@pytest.mark.parametrize('mode', ['assistant', 'parallel'])
def test_flow_rows_preserve_distinct_children_draft_and_saved_history(tmp_path, monkeypatch, size, mode):
    from test_console_activity import activity
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    root = tmp_path/'project'; root.mkdir()

    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode=mode)
        async with app.run_test(size=size) as pilot:
            await app.workers.wait_for_complete()
            prompt = app.query_one('#prompt', TextArea); prompt.load_text('Keep this draft.')
            for child in ('1', '2'):
                with flow_activity.stream('parallel', tmp_path/'batch', root):
                    _, source = flow_activity.observer(tmp_path/'batch'/child, 'explorer')
                app.assistant_activity({'channel': 'activity', 'activity': activity('succeeded', source=source)})
            await pilot.pause()
            assert len(app.query(ToolActivity)) == len(app.assistant_history) == 2
            assert all('Explore' in widget.title for widget in app.query(ToolActivity))
            app.checkpoint('draft_saved'); identity = app.demand_id
            app.new_demand(); await app.list_demands().wait()
            app.assistant_activity({'channel': 'activity', 'activity': activity('failed', identity='unrelated')})
            app.query_one('#saved-demand', Select).value = identity
            app.open_demand(); await app.workers.wait_for_complete(); await pilot.pause()
            assert len(app.query(ToolActivity)) == 2 and prompt.text == 'Keep this draft.'
            assert not app.backend.launches
    asyncio.run(scenario())


def test_long_stage_keeps_failure_and_timing_visible(tmp_path, monkeypatch):
    from test_console_activity import activity
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    root = tmp_path/'project'; root.mkdir()
    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            with flow_activity.stream('work', 'very-long-work-name', root):
                _, source = flow_activity.observer(tmp_path/'attempt', 'implementer', kind='attempt')
            source['stage'] = 'Work '+'計画'*20+' · implementer'
            app.assistant_activity({'channel': 'activity', 'activity': activity('failed', source=source,
                target='src/'+'長いディレクトリ/'*10+'test.py')})
            await pilot.pause()
            title = app.query_one(ToolActivity).query_one('CollapsibleTitle')
            visible = ''.join(title.render_line(n).text for n in range(title.size.height))
            assert 'Failed' in visible and '1.2s' in visible and 'Check' in visible
    asyncio.run(scenario())


def test_conversation_delegations_stream_their_rows_and_full_output_stays_in_the_conversation(setup, tmp_path):
    from relay.native_agent import run_session
    from test_native_agent import Client, deployment, steps
    config, _, root = setup
    owner = tmp_path/'conversation'; owner.mkdir()
    (owner/'native-agent.json').write_text(json.dumps({'config': {'repositories': {'workspace': str(root)}}}))
    events, client = [], Client(steps=steps())
    with flow_activity.stream('conversation', owner, root, events.append) as emit:
        assert emit is not None
        result = run_session(config, deployment(), owner/'units'/'u1'/'session-1', 'Change the value and verify.',
                             client=client, local_counter=client)
    assert result['status'] == 'PROPOSED', result
    rows = [e['activity'] for e in events if e['activity']['state'] == 'succeeded'
            and e['activity']['operation'] in {'read', 'edit', 'verify'}]
    assert [row['operation'] for row in rows] == ['read', 'edit', 'verify']
    assert all(row['source']['mode'] == 'conversation' and row['source']['stage'] == 'Implement' for row in rows)
    assert 'value = 1' in full_output(None, root, rows[0])
    with pytest.raises(ValueError, match='another project'):
        full_output(None, root.parent, rows[0])
    (tmp_path/'elsewhere').mkdir()
    for session in (tmp_path/'elsewhere', owner):  # outside the conversation, or the conversation itself
        with pytest.raises(ValueError, match='does not belong to this conversation'):
            full_output(None, root, {**rows[0], 'source': {**rows[0]['source'], 'session': str(session)}})


def test_conversation_stream_crosses_copied_contexts_not_plain_threads_and_keeps_lines_whole(tmp_path):
    import contextvars, io, threading
    from concurrent.futures import ThreadPoolExecutor
    lines = io.StringIO()
    def write(event):  # two writes, as print() does: only the stream's lock keeps them together
        lines.write(json.dumps(event)); lines.write('\n')
    child = tmp_path/'delegations'/'x'/'session'
    with flow_activity.stream('conversation', tmp_path, tmp_path, write) as emit:
        assert flow_activity.observer(child, 'explorer')[1]['stage'] == 'Explore'
        with ThreadPoolExecutor(4) as pool:
            copied = pool.submit(contextvars.copy_context().run, flow_activity.observer, child, 'explorer').result()
            assert copied[1]['mode'] == 'conversation'
            list(pool.map(lambda n: [emit({'n': n, 'i': i}) for i in range(200)], range(4)))
        seen = []
        thread = threading.Thread(target=lambda: seen.append(flow_activity.observer(child, 'explorer')))
        thread.start(); thread.join()
        assert seen == [(None, None)]  # a plain thread runs no delegated session; nothing is passed there
    assert len([json.loads(line) for line in lines.getvalue().splitlines()]) == 800
    assert flow_activity.observer(child, 'explorer') == (None, None)


def test_conversation_rows_are_saved_in_a_shape_the_previous_release_reopens(tmp_path):
    from relay import relay_mesh
    from relay.native_activity import activity_view, saved_history
    source = {'session': str(tmp_path/'c'/'units'/'u'/'session-1'), 'kind': 'session', 'mode': 'conversation',
              'owner': str(tmp_path/'c'), 'project': str(tmp_path.resolve()),
              'control_root': str(Path(relay_mesh.RELAY_ROOT).resolve()), 'stage': 'Implement', 'work': ''}
    rows = [activity_view({'kind': 'tool-activity', 'id': f's/effect/{n}', 'operation': operation,
                           'state': 'succeeded', 'target': 'a.py', 'source': source})
            for n, operation in enumerate(('edit', 'folder_read'))]
    saved = saved_history(rows)
    for row in saved:  # the previous release's validator: a Work source, which needs no absolute owner
        assert row['source']['mode'] == 'work' and row['source']['work'] == '#conversation'
    assert saved[1]['operation'] == 'read' and saved[1]['target'] == 'folder_read: a.py'
    assert [activity_view(row) for row in saved] == rows  # this release restores both
    with pytest.raises(ValueError, match='another Work'):  # the previous release's Full output fails closed
        flow_activity.validate_source(saved[0]['source'], tmp_path)


@pytest.mark.parametrize('events', [True, False])
def test_conversation_subprocess_streams_its_delegations(tmp_path, monkeypatch, capsys, events):
    from relay import native_agent
    from test_native_agent import deployment
    profile = tmp_path/'profile.json'; profile.write_text(json.dumps(deployment()))
    seen = []
    def run(config, *args, on_activity=None, **kwargs):
        seen.append(flow_activity.observer(tmp_path/'session'/'units'/'u'/'session-1', 'implementer'))
        if on_activity is not None:
            on_activity({'channel': 'activity', 'probe': True})
        return {'status': 'PROPOSED', 'summary': 'No inference in this fixture.'}
    monkeypatch.setattr(native_agent, 'run_session', run)
    assert native_agent.main(['--native-deployment', str(profile), '--root', str(tmp_path), '--role', 'explorer',
        '--conversation', '--instruction', 'Look', '--session', str(tmp_path/'session'),
        *(['--activity-events'] if events else [])]) == 0
    callback, source = seen[0]
    if events:
        assert source['mode'] == 'conversation' and source['stage'] == 'Implement'
        assert source['owner'] == str(tmp_path/'session') and source['project'] == str(tmp_path.resolve())
        err = [line for line in capsys.readouterr().err.splitlines() if line.startswith('{')]
        assert {'channel': 'activity', 'probe': True} in [json.loads(line) for line in err]
    else:
        assert (callback, source) == (None, None)
    assert flow_activity._stream.get() is None  # the stream ends with the command
