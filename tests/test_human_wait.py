"""Durable operator pause with real Work recovery and a deterministic provider."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import tare_ops
from relay import human_wait, relay_mesh, work_run
from test_work_resume import host, run
from test_work_run import os_root, _git


def pause(host, capsys):
    assert run(host, '--wait-before-implement', 'Proceed with this implementation scope?') == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'WAITING_FOR_HUMAN', result
    train = work_run.train_dir_for('work-one')
    assert not list(train.glob('WORKSPACE_INTENT.*'))
    assert not host[3].generations and len(host[4]) == 1
    return train, human_wait.inspect(train)


@pytest.mark.parametrize('revision', [None, 'c'*24])
def test_wait_survives_process_change_and_resumes_without_repeating_plan(host, capsys, revision):
    if revision:
        graph = json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
        graph['nodes'][0]['metrics']['execution_revision'] = revision
        relay_mesh.WORK_GRAPH_FILE.write_text(json.dumps(graph))
    train, view = pause(host, capsys)
    original = (train/'PLAN_AUDIT_REPORT.md').read_bytes()
    for _ in range(2):
        assert tare_ops.main(['run', 'work-one', '--resume']) == 0
        assert json.loads(capsys.readouterr().out)['status'] == 'WAITING_FOR_HUMAN'
    assert not host[3].generations and len(host[4]) == 1
    # A different process answers from saved custody; no provider mocks cross this boundary.
    script = ('from relay import relay_mesh; import tare_ops; '
              'relay_mesh.rebind_root('+repr(str(relay_mesh.RELAY_ROOT))+'); '
              'raise SystemExit(tare_ops.main('+repr(['decision','work-one','--decision','approve',
                  '--expected',view['request_sha256'],'--note','Proceed as scoped','--format','json'])+'))')
    reply = subprocess.run([sys.executable, '-B', '-c', script], capture_output=True, text=True)
    assert reply.returncode == 0, reply.stderr+reply.stdout
    assert json.loads(reply.stdout)['status'] == 'HUMAN_APPROVED'
    assert not host[3].generations
    assert tare_ops.main(['run', 'work-one', '--resume']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'SETTLED', result
    assert len(host[3].generations) == 5 and len(host[4]) == 1
    archived = work_run.train_dir_for('work-one')
    assert (archived/'PLAN_AUDIT_REPORT.md').read_bytes() == original
    assert human_wait.inspect(archived)['answer']['note'] == 'Proceed as scoped'
    assert (host[1]/'src/value.txt').read_text() == 'strict\n'
    assert tare_ops.main(['run','work-one','--resume']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'SETTLED'
    assert len(host[3].generations) == 5 and len(host[4]) == 1


def test_decline_is_durable_and_cannot_be_silently_changed(host, capsys):
    train, view = pause(host, capsys)
    result = human_wait.answer('work-one', view['request_sha256'], 'decline', 'Needs another proposal')
    assert result['status'] == 'HUMAN_DECLINED'
    assert human_wait.answer('work-one', view['request_sha256'], 'decline', 'Needs another proposal') == result
    with pytest.raises(ValueError, match='ALREADY_ANSWERED'):
        human_wait.answer('work-one', view['request_sha256'], 'approve')
    assert tare_ops.main(['run','work-one','--resume']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'HUMAN_DECLINED'
    assert not host[3].generations and not list(train.glob('WORKSPACE_INTENT.*'))


@pytest.mark.parametrize('fault', ['digest', 'work', 'target', 'lock', 'origin'])
def test_changed_decision_inputs_and_concurrent_execution_are_refused(host, capsys, fault):
    train, view = pause(host, capsys)
    expected = view['request_sha256']; before = (train/human_wait.FILE).read_bytes()
    if fault == 'digest': expected = '0'*64
    elif fault == 'work':
        path = relay_mesh.WORK_GRAPH_FILE; graph = json.loads(path.read_bytes())
        graph['nodes'][0]['title'] = 'Changed'; path.write_text(json.dumps(graph))
    elif fault == 'target': _git('commit','--allow-empty','-qm','moved',cwd=host[1])
    elif fault == 'origin':
        path = train/'WORK_RUN.json'; origin = json.loads(path.read_bytes())
        origin['wait_before_implement'] = 'Different question'; path.write_text(json.dumps(origin))
    if fault == 'lock':
        from relay.work_change import work_locks
        with work_locks(['work-one']):
            with pytest.raises(ValueError): human_wait.answer('work-one', expected, 'approve')
    else:
        with pytest.raises(ValueError): human_wait.answer('work-one', expected, 'approve')
    assert (train/human_wait.FILE).read_bytes() == before and not host[3].generations


def test_lost_answer_acknowledgement_does_not_repeat_or_reverse_decision(host, capsys, monkeypatch):
    train, view = pause(host, capsys)
    from compute_plane import native_journal
    save = native_journal.save_json
    def lost(*args):
        save(*args)
        raise OSError('lost acknowledgement')
    with monkeypatch.context() as cut:
        cut.setattr(native_journal, 'save_json', lost)
        with pytest.raises(OSError): human_wait.answer('work-one', view['request_sha256'], 'approve')
    before = (train/human_wait.FILE).read_bytes()
    assert human_wait.answer('work-one', view['request_sha256'], 'approve')['status'] == 'HUMAN_APPROVED'
    assert (train/human_wait.FILE).read_bytes() == before and not host[3].generations


def test_interrupted_checkpoint_creation_is_recovered_without_plan_replay(host, capsys, monkeypatch):
    from compute_plane import native_journal
    save = native_journal.save_json
    def fail(path, value):
        if Path(path).name == human_wait.FILE: raise OSError('checkpoint unavailable')
        return save(path, value)
    with monkeypatch.context() as cut:
        cut.setattr(native_journal, 'save_json', fail)
        assert run(host, '--wait-before-implement', 'Proceed?') == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'FAILED'
    assert tare_ops.main(['run','work-one','--resume']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'WAITING_FOR_HUMAN'
    assert len(host[4]) == 1 and not host[3].generations


def test_tui_opens_saved_decision_and_refuses_changed_selection(host, capsys):
    train, view = pause(host, capsys)
    from relay.native_console import WorkConsole
    from relay.console_backend import Backend, Selection, command
    from textual.widgets import Input, Select, Button
    selection = Selection(root=str(host[1]), mode='work', work='work-one', wait_before_implement='Proceed?')
    assert '--wait-before-implement' in command(selection)
    assert '--wait-before-implement' not in command(selection, resume=True)
    class LocalBackend(Backend):
        def catalog(self, root):
            return {'models': [{'deployment':'local-qwen','model':'fixture'}], 'sessions':[], 'works':[], 'errors':[]}
    async def scenario():
        app = WorkConsole(host[1], backend=LocalBackend())
        async with app.run_test(size=(140,48)) as pilot:
            await pilot.pause()
            app.query_one('#mode', Select).value = 'work'
            app.query_one('#work-id', Input).value = 'work-one'
            await app.open_decision().wait()
            assert not app.query_one('#approve-decision', Button).disabled
            app.query_one('#work-id', Input).value = 'other'
            app.answer_decision('approve')
            assert app.reviewed_decision is None and human_wait.inspect(train)['answer'] is None
            app.query_one('#work-id', Input).value = 'work-one'
            await app.open_decision().wait()
            app.query_one('#decision-note', Input).value = 'Ready to implement'
            app.answer_decision('approve')
            while app.busy: await pilot.pause(0.05)
            assert app.last_result['status'] == 'HUMAN_APPROVED'
            assert app.query_one('#approve-decision', Button).disabled
    asyncio.run(scenario())
    assert human_wait.inspect(train)['answer']['note'] == 'Ready to implement'
    assert not host[3].generations



def test_explicit_reconsider_preserves_decline_rounds_and_replay(host, capsys):
    train, first = pause(host, capsys)
    declined = human_wait.answer('work-one', first['request_sha256'], 'decline', 'Revise please')
    assert tare_ops.main(['decision', 'work-one', '--decision', 'reconsider',
                         '--expected', first['request_sha256'], '--format', 'json']) == 0
    waiting = json.loads(capsys.readouterr().out)
    assert waiting['status'] == 'WAITING_FOR_HUMAN' and waiting['answer'] is None
    assert waiting['prior_decisions'] == [declined['answer']]
    assert waiting['request']['request_round'] == 1
    assert waiting['request_sha256'] != first['request_sha256']
    before = (train/human_wait.FILE).read_bytes()
    assert human_wait.answer('work-one', first['request_sha256'], 'reconsider') == waiting
    assert (train/human_wait.FILE).read_bytes() == before
    with pytest.raises(ValueError, match='CHANGED'):
        human_wait.answer('work-one', first['request_sha256'], 'approve')
    receipt = {}
    origin = json.loads((train/'WORK_RUN.json').read_bytes())
    assert not human_wait.checkpoint(train, origin, receipt, lambda *a, **kw: None)
    second = human_wait.answer('work-one', waiting['request_sha256'], 'decline', 'Still not ready')
    third = human_wait.answer('work-one', second['request_sha256'], 'reconsider')
    assert third['request']['request_round'] == 2
    assert third['prior_decisions'] == [declined['answer'], second['answer']]
    with pytest.raises(ValueError, match='CHANGED'):
        human_wait.answer('work-one', first['request_sha256'], 'reconsider')
    assert not host[3].generations


@pytest.mark.parametrize('decision', [None, 'approve'])
def test_reconsider_only_declined(host, capsys, decision):
    train, value = pause(host, capsys)
    if decision: human_wait.answer('work-one', value['request_sha256'], decision)
    before = (train/human_wait.FILE).read_bytes()
    with pytest.raises(ValueError, match='REQUIRES_DECLINED'):
        human_wait.answer('work-one', value['request_sha256'], 'reconsider')
    assert (train/human_wait.FILE).read_bytes() == before


def test_reconsider_preserves_origin_and_record_ceiling(host, capsys):
    train, value = pause(host, capsys)
    human_wait.answer('work-one', value['request_sha256'], 'decline')
    with pytest.raises(ValueError, match='TOO_LARGE'):
        human_wait.answer('work-one', value['request_sha256'], 'reconsider', 'x'*(1024*1024))
    waiting = human_wait.answer('work-one', value['request_sha256'], 'reconsider')
    path = train/'WORK_RUN.json'; origin = json.loads(path.read_bytes())
    origin['node']['repository_scopes'][0]['target_paths'] = ['different']
    path.write_text(json.dumps(origin))
    with pytest.raises(ValueError, match='ORIGIN_CHANGED'):
        human_wait.answer('work-one', waiting['request_sha256'], 'approve')
    assert not host[3].generations
