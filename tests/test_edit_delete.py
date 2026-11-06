"""Offline deletion application checks using a temporary repository."""
import pytest

from relay import work_units
from relay.challenger_workspace import snapshot
from tests.test_implement_unit import repository
from tests.test_native_review import session
from compute_plane.native_review import digest
from tare_tools_agent_runtime import EffectRequest, ToolCall
import json
import time
from tare_tools_agent_runtime import BindingRef, CancellationToken, ExecutionControl


@pytest.mark.parametrize('delete', [True, 'true'])
@pytest.mark.parametrize('before', [b'value = 1\n', b''])
def test_real_edit_deletion_has_review_evidence(session, delete, before):
    repo, root, tools, events, edit, check, report = session
    target = repo/'a.py'
    target.write_bytes(before)
    args = {'repository': 'repo', 'path': 'a.py', 'sha256': digest(before),
            'delete': delete, 'changes': []}
    effect = 's/effect/delete'
    request = {'kind': 'effect_requested', 'effect_id': effect,
               'tool_call': {'name': 'edit', 'arguments_json': json.dumps(args)}}
    binding = BindingRef('w', 'b', 'o', 1, 't')
    control = ExecutionControl(binding, CancellationToken(), time.monotonic()+30)
    result = tools.execute(EffectRequest('s', effect, ToolCall('t', 'edit', json.dumps(args))), control)
    assert result.status == 'succeeded', result.output_json
    assert not target.exists()
    events.extend([request, {'kind': 'effect_returned', 'result': {
        'effect_id': effect, 'status': result.status, 'output_json': result.output_json}}])
    row = report()['edits'][0]
    assert row['diff'] is not None, row
    assert row['deleted'] is True


@pytest.mark.parametrize('before', [b'value = 1\n', b''])
def test_candidate_deletion_obeys_scope_and_freshness(tmp_path, before):
    root, tree = tmp_path/'root', tmp_path/'tree'
    root.mkdir()
    target = root/'a.py'
    target.write_bytes(before)
    repository(root)
    base = snapshot(root, tree)
    changed = {'a.py': None}
    assert work_units.apply(root, tree, base, changed, ['other.py']) == ('OUTSIDE', ['a.py'])
    assert target.read_bytes() == before
    target.write_bytes(b'new project bytes\n')
    assert work_units.apply(root, tree, base, changed, ['a.py']) == ('CONFLICT', ['a.py'])
    assert target.read_bytes() == b'new project bytes\n'
    target.write_bytes(before)
    assert work_units.apply(root, tree, base, changed, ['a.py']) == ('APPLIED', ['a.py'])
    assert not target.exists()


@pytest.mark.parametrize('before', [b'value = 1\n', b''])
@pytest.mark.parametrize('delete', [True, 'true'])
def test_retained_real_deletion_is_revised_and_applied(tmp_path, monkeypatch, before, delete):
    import sys
    from pathlib import Path
    from relay import native_agent
    from relay.native_review import summarize
    from relay.work_forms import record_plan
    from tests.test_native_agent import deployment
    from tests.test_native_activity import Journal
    from relay.native_activity import ActivityJournal
    from relay import heads_up
    from compute_plane.native_capabilities import NativeCapabilities

    project = tmp_path/'project'; project.mkdir()
    (project/'a.py').write_bytes(before)
    repository(project)
    custody = tmp_path/'session'; custody.mkdir()
    config = {'repositories': {'repo': str(project)}, 'writable': {'repo': ['a.py']},
              'verifiers': {'absent': {'repository': 'repo', 'argv': [sys.executable, '-c',
                  "from pathlib import Path; assert not Path('a.py').exists()"]}}}
    unit = {'key': 'remove', 'title': 'Remove file', 'target_paths': ['a.py'],
            'criteria': ['File is absent'], 'verifiers': ['absent']}
    record_plan(custody, config, {'units': [unit], 'criteria': unit['criteria']})
    state = {'config': config, 'conversation_flow': True, 'deployment': deployment(),
             'limits': {'max_model_calls': 30}, 'deadline_utc': None, 'session_id': 's',
             'instruction': 'Remove file'}
    monkeypatch.setattr(work_units, 'routes', lambda *a, **kw: iter([('fixture', deployment())]))
    calls = []
    def child(child_config, dep, session, instruction, **kwargs):
        path = Path(child_config['repositories']['repo'])/'a.py'
        assert (project/'a.py').read_bytes() == before
        if not calls:
            binding = BindingRef('w', 'b', 'o', 1, 't')
            (tmp_path/'scratch').mkdir()
            tools = NativeCapabilities(session_id='s', binding=binding,
                repositories=child_config['repositories'], writable={'repo': ['a.py']},
                verifiers={}, scratch_root=tmp_path/'scratch', specgraph_edit_context=False,
                review_root=Path(session)/'review')
            args = {'repository': 'repo', 'path': 'a.py', 'sha256': digest(path.read_bytes()),
                    'delete': delete, 'changes': []}
            control = ExecutionControl(binding, CancellationToken(), time.monotonic()+30)
            result = tools.execute(EffectRequest('s', 'delete', ToolCall('t', 'edit', json.dumps(args))), control)
            assert result.status == 'succeeded', result.output_json
            observer = ActivityJournal(Journal())
            observer.append({'kind': 'effect_requested', 'effect_id': 'delete',
                'tool_call': {'name': 'edit', 'arguments_json': json.dumps(args)}})
            observer.append({'kind': 'effect_returned', 'result': {'effect_id': 'delete',
                'status': result.status, 'output_json': result.output_json}})
            assert 'Deleted file.' in observer.presentation()['items'][0]['detail']
            assert heads_up.step('edit', args, result.status, json.loads(result.output_json))['detail'] == 'Deleted file.'
        else:
            assert not path.exists()  # retained deletion survived patch replay into revision
        calls.append(True)
        return {'status': 'PROPOSED', 'model_calls': 1}
    monkeypatch.setattr(native_agent, 'run_session', child)
    def audit(tree, dep, unit, changed, checks):
        assert changed == {'a.py': None}
        assert not (tree/'a.py').exists()
        assert (project/'a.py').read_bytes() == before
        return {'verdict': 'INCONCLUSIVE' if len(calls) == 1 else 'ACCEPT'}
    first = work_units.run_unit(custody, state, {'unit': 'remove'}, lambda: False, review=audit)
    assert not first.get('applied') and first.get('candidate'), first
    assert (project/'a.py').read_bytes() == before
    second = work_units.run_unit(custody, state, {'unit': 'remove'}, lambda: False,
        review=audit, candidate=first['candidate'], review_effect='s/implement')
    assert second['applied'], second
    assert len(calls) == 2 and not (project/'a.py').exists()
    assert second['review_edits'][0]['deleted'] is True
    events = [{'kind': 'effect_requested', 'effect_id': 's/implement',
               'tool_call': {'name': 'implement', 'arguments_json': '{}'}},
              {'kind': 'effect_returned', 'result': {'effect_id': 's/implement',
               'status': 'succeeded', 'output_json': json.dumps(second)}}]
    row = summarize(custody, state, events, include_diffs=False)['edits'][0]
    assert row['deleted'] is True and 'diff_unavailable' not in row


@pytest.mark.parametrize('delete', [False, 'false'])
def test_false_preserves_normal_edits(session, delete):
    repo, root, tools, events, edit, check, report = session
    before = (repo/'a.py').read_bytes()
    args = {'repository': 'repo', 'path': 'a.py', 'sha256': digest(before), 'delete': delete,
            'changes': [{'search': 'value = 1', 'replace': 'value = 2'}]}
    binding = BindingRef('w', 'b', 'o', 1, 't')
    control = ExecutionControl(binding, CancellationToken(), time.monotonic()+30)
    result = tools.execute(EffectRequest('s', 'false', ToolCall('t', 'edit', json.dumps(args))), control)
    assert result.status == 'succeeded', result.output_json
    assert (repo/'a.py').read_bytes() == before.replace(b'value = 1', b'value = 2')


@pytest.mark.parametrize('fault', ['stale', 'outside'])
def test_real_kernel_refuses_unauthorized_deletion(session, fault):
    repo, root, tools, events, edit, check, report = session
    target = repo/('outside.py' if fault == 'outside' else 'a.py')
    target.write_bytes(b'preserve me\n')
    args = {'repository': 'repo', 'path': target.name,
            'sha256': '0'*64 if fault == 'stale' else digest(target.read_bytes()),
            'delete': True, 'changes': []}
    binding = BindingRef('w', 'b', 'o', 1, 't')
    control = ExecutionControl(binding, CancellationToken(), time.monotonic()+30)
    result = tools.execute(EffectRequest('s', 'refused', ToolCall('t', 'edit', json.dumps(args))), control)
    assert result.status == 'refused'
    assert target.read_bytes() == b'preserve me\n'
