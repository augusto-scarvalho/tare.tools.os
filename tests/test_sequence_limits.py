"""Finite sequence overrides use the existing per-native-stage limits and journals.

Offline owners/temporary Git projects only. These are not a shared campaign quota.
"""
import json
from pathlib import Path

import pytest

from relay import console_backend, relay_mesh, work_run, work_sequence
from test_work_sequence import sequence, save_graph
from test_work_resume import host
from test_work_run import os_root


def saved(case):
    return json.loads((case['dir']/work_sequence.FILE).read_bytes())


def argv(case, *extra):
    return ['--sequence', str(case['dir']), '--works', 'work-two', 'work-one',
            '--root', str(case['repo']), '--native-deployment', str(case['host'][2]),
            '--format', 'json', *extra]


def test_console_sequence_carries_finite_execution_options(tmp_path):
    selection = console_backend.Selection(root=str(tmp_path), mode='sequence',
        deployment='fixture', sequence_works='work-one,work-two', unlimited=False,
        max_model_calls='6', max_seconds='180')
    args = console_backend.command(selection)
    assert args[0] == 'sequence'
    assert args[args.index('--max-model-calls')+1] == '6'
    assert float(args[args.index('--max-seconds')+1]) == 180
    assert '--unlimited' not in args
    selection.max_model_calls = selection.max_seconds = ''
    inherited = console_backend.command(selection)
    assert '--max-model-calls' not in inherited and '--max-seconds' not in inherited


def test_explicit_limit_format_survives_revision_preview_and_serial_control(sequence):
    work_sequence.create(sequence['dir'], ['work-one','work-two'], sequence['repo'],
                         sequence['profile'], limits={'max_model_calls':6})
    assert saved(sequence)['schema'] == 'tare.tools/work-sequence/2'
    assert work_sequence.inspect(sequence['dir'])['status'] == 'READY'
    reason = 'Drop only the unstarted second Work'
    view = work_sequence.preview_revision(sequence['dir'], reason, work_ids=['work-one'])
    work_sequence.revise(sequence['dir'], view['sha256'], reason, work_ids=['work-one'])
    work_sequence.serial(sequence['dir'])
    assert saved(sequence)['config']['limits'] == {'max_model_calls':6}
    assert saved(sequence)['schema'] == 'tare.tools/work-sequence/2'
    assert not sequence['calls']


def test_new_format_cannot_lose_its_budget_before_dispatch(sequence):
    work_sequence.create(sequence['dir'], ['work-one','work-two'], sequence['repo'],
                         sequence['profile'], limits={'max_model_calls':6})
    state = saved(sequence)
    del state['config']['limits']
    (sequence['dir']/work_sequence.FILE).write_text(json.dumps(state))
    with pytest.raises(ValueError, match='SEQUENCE_EXECUTION_LIMITS_MISSING'):
        work_sequence.execute(sequence['dir'])
    assert not sequence['calls']


def test_inherited_settings_preserve_legacy_format(sequence):
    work_sequence.create(sequence['dir'], ['work-one','work-two'], sequence['repo'], sequence['profile'])
    assert saved(sequence)['schema'] == 'tare.tools/work-sequence/1'
    assert work_sequence.inspect(sequence['dir'])['status'] == 'READY'
    assert not sequence['calls']


@pytest.mark.parametrize('field,value', [('max_model_calls', '0'), ('max_model_calls', '1.5'),
    ('max_seconds', '0'), ('max_seconds', 'nan'), ('max_seconds', '-1')])
def test_console_rejects_invalid_finite_limits(tmp_path, field, value):
    selection = console_backend.Selection(root=str(tmp_path), mode='sequence',
        deployment='fixture', sequence_works='work-one', unlimited=False)
    setattr(selection, field, value)
    with pytest.raises(ValueError):
        console_backend.command(selection)


@pytest.mark.timeout(90)
def test_public_cli_persists_and_forwards_limits_to_both_real_works(sequence, monkeypatch, capsys):
    original = work_run.run_work
    options = []
    def run(key, **kwargs):
        options.append((key, kwargs))
        assert saved(sequence)['config']['limits'] == {'max_model_calls': 6, 'max_seconds': 180.0}
        return original(key, **kwargs)
    monkeypatch.setattr(work_run, 'run_work', run)
    assert work_sequence.main(argv(sequence, '--max-model-calls', '6', '--max-seconds', '180')) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'COMPLETED', result
    assert result['completed'] == 2 and len(sequence['calls']) == 10
    assert [key for key, _ in options] == ['work-one', 'work-two']
    assert all(kw['limits'] == {'max_model_calls': 6, 'max_seconds': 180} for _, kw in options)
    receipts = [json.loads(Path(row['attempts'][-1]['receipt']).read_bytes()) for row in result['items']]
    assert all(r['status'] == 'SETTLED' for r in receipts)
    assert receipts[0]['candidate_commit'] != receipts[1]['candidate_commit']
    for receipt in receipts:
        for stage in ('plan', 'implementation'):
            assert receipt['effective_limits'][stage]['max_model_calls'] == 6
            assert receipt['effective_limits'][stage]['max_seconds'] == 180
    assert (sequence['repo']/'src/value.txt').read_text() == 'strict\n'
    assert (sequence['repo']/'src/second.txt').read_text() == 'strict\n'
    before = saved(sequence)
    assert work_sequence.main(['--sequence', str(sequence['dir']), '--resume', '--format', 'json']) == 0
    capsys.readouterr()
    assert saved(sequence) == before and len(sequence['calls']) == 10 and len(options) == 2


@pytest.mark.parametrize('flag,value', [('--max-model-calls', '0'), ('--max-seconds', '0')])
def test_cli_invalid_limits_refused_before_creation(sequence, capsys, flag, value):
    assert work_sequence.main(argv(sequence, flag, value)) == 2
    assert json.loads(capsys.readouterr().out)['status'] == 'REFUSED'
    assert not (sequence['dir']/work_sequence.FILE).exists() and not sequence['calls']


@pytest.mark.parametrize('action', ['--resume', '--show', '--preview-revision', '--revise'])
def test_saved_settings_cannot_be_overridden(sequence, capsys, action):
    work_sequence.create(sequence['dir'], ['work-one', 'work-two'], sequence['repo'],
                         sequence['profile'], limits={'max_model_calls': 6})
    before = (sequence['dir']/work_sequence.FILE).read_bytes()
    assert work_sequence.main(['--sequence', str(sequence['dir']), action, '--max-model-calls', '7']) == 2
    assert json.loads(capsys.readouterr().out)['status'] == 'REFUSED'
    assert (sequence['dir']/work_sequence.FILE).read_bytes() == before and not sequence['calls']


def test_native_call_exhaustion_stops_dependent_and_resume_preserves_journal(sequence, capsys):
    assert work_sequence.main(argv(sequence, '--max-model-calls', '2', '--max-seconds', '180')) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'PAUSED' and result['completed'] == 0
    assert result['items'][0]['status'] == 'IMPLEMENTATION_EXHAUSTED'
    assert not result['items'][1]['attempts'] and len(sequence['calls']) == 2
    receipt = json.loads(Path(result['items'][0]['attempts'][-1]['receipt']).read_bytes())
    assert receipt['candidate_commit'] is None and receipt['landing'] is None
    assert receipt['effective_limits']['implementation']['max_model_calls'] == 2
    # Managed Work keeps its native journal in run custody, not the train.
    custody = Path(result['items'][0]['attempts'][-1]['receipt']).parent/'implement'
    states = {custody/name: (custody/name).read_bytes()
              for name in ('host.json', 'attempt.json', 'journal.jsonl')}
    assert states
    assert work_sequence.main(['--sequence', str(sequence['dir']), '--resume', '--format', 'json']) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed['status'] == 'PAUSED' and not resumed['items'][1]['attempts']
    assert len(sequence['calls']) == 2
    # Recovery may update terminal bookkeeping; it must retain the original
    # prompt/cache identity, absolute deadline and all prior journal records.
    before = json.loads(states[custody/'attempt.json'])
    after = json.loads((custody/'attempt.json').read_bytes())
    assert after['identity'] == before['identity']
    assert after['deadline_utc'] == before['deadline_utc']
    assert (custody/'host.json').read_bytes() == states[custody/'host.json']
    assert (custody/'journal.jsonl').read_bytes().startswith(states[custody/'journal.jsonl'])
    assert (sequence['repo']/'src/value.txt').read_text() == 'base\n'
    assert (sequence['repo']/'src/second.txt').read_text() == 'base\n'


def test_legacy_sequence_dispatch_inherits_without_inventing_overrides(sequence, monkeypatch):
    work_sequence.create(sequence['dir'], ['work-one', 'work-two'], sequence['repo'], sequence['profile'])
    state = saved(sequence)
    del state['config']['limits']
    (sequence['dir']/work_sequence.FILE).write_text(json.dumps(state))
    seen = []
    def run(key, **kwargs):
        seen.append(kwargs)
        raise ValueError('offline dispatch boundary')
    monkeypatch.setattr(work_run, 'run_work', run)
    assert work_sequence.execute(sequence['dir'])['status'] == 'PAUSED'
    assert len(seen) == 1 and seen[0]['limits'] is None and seen[0]['unlimited'] is False
    assert 'limits' not in saved(sequence)['config']


def test_parallel_preparation_receives_and_persists_same_finite_limits(sequence, monkeypatch):
    from relay import work_prepared
    data = json.loads(relay_mesh.WORK_GRAPH_FILE.read_bytes())
    data['edges'] = []
    save_graph(data)
    limits = {'max_model_calls': 2, 'max_seconds': 180}
    work_sequence.create(sequence['dir'], ['work-one', 'work-two'], sequence['repo'],
        sequence['profile'], workers=2, isolated_effects=True, limits=limits)
    original = work_prepared.prepare
    seen = []
    def prepare(*args, **kwargs):
        assert kwargs['limits'] == limits
        seen.append(args[0])
        return original(*args, **kwargs)
    monkeypatch.setattr(work_prepared, 'prepare', prepare)
    result = work_sequence.execute(sequence['dir'])
    assert result['status'] == 'PAUSED' and len(seen) == 2, result
    for row in result['items']:
        assert row['status'] == 'EXHAUSTED', row
        assert row['preparation']['reason'] == 'MODEL_CALL_BUDGET', row
    assert len(sequence['calls']) == 4, result
    assert all(not row['attempts'] for row in result['items'])
    for directory in seen:
        intent = json.loads((Path(directory)/'intent.json').read_bytes())
        assert intent['limits']['max_model_calls'] == 2 and intent['limits']['max_seconds'] == 180
        state = json.loads((Path(directory)/'session/native-agent.json').read_bytes())
        assert state['limits'] == intent['limits']
