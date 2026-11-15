"""A delegated implementation says what it is doing: its checks, its implementer, its audit (owner, 2026-11-14).
The `implement` row is republished, still running, through ActivityJournal.describe; nothing here is saved."""
import asyncio
import sys
import tempfile
from pathlib import Path

import pytest

from relay import conversation_permissions as permissions, native_agent, work_units
from relay.native_activity import ActivityJournal, activity_view
from test_implement_unit import repository
from test_native_agent import deployment, setup  # noqa: F401 (fixture)
from test_checks_by_activity import checks


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def recorder():
    said = []
    return said, lambda target, detail: said.append((target, detail))


def sample(elapsed, idle):
    return {'elapsed_ms': elapsed*1000, 'idle_ms': idle*1000, 'cpu_ms': 0, 'output_bytes': 0}


def test_a_silent_check_says_so_after_the_note_window_and_not_before_and_is_throttled(monkeypatch):
    monkeypatch.setattr(work_units, 'PROGRESS_SECONDS', 10)
    monkeypatch.setattr(work_units, 'SILENT_NOTE_SECONDS', 30)
    clock, (said, publish) = Clock(), recorder()
    progress = work_units.Progress(publish, clock)
    progress.rung('Implementer · M · high · Codex CLI')
    progress.begin('before')
    progress.running('tests')
    assert said[-1][0] == 'check before: tests' and 'tests' in said[-1][1]
    count = len(said)
    clock.now = 5; progress.sample(sample(5, 5))
    assert len(said) == count  # throttled: not 10 s since the last publication
    clock.now = 12; progress.sample(sample(12, 12))
    assert len(said) == count+1 and said[-1][0] == 'check before: tests' and 'Running for 12 s.' in said[-1][1]
    assert 'silent' not in said[-1][0] and 'silent' not in said[-1][1]  # silent for 12 s: under the window
    clock.now = 24; progress.sample(sample(24, 24))
    assert 'silent' not in said[-1][0]
    clock.now = 36; progress.sample(sample(36, 31))
    assert said[-1][0] == 'silent for 31 s: check before: tests' and 'silent for 31 s.' in said[-1][1]
    count = len(said)
    clock.now = 37; progress.sample(sample(37, 32))
    assert len(said) == count
    progress.working()  # a step change publishes at once, and later samples belong to no check
    count = len(said)
    clock.now = 100; progress.sample(sample(100, 90))
    assert len(said) == count


def test_the_stall_watch_hands_samples_to_the_progress_it_found_when_it_was_made(monkeypatch):
    clock, (said, publish) = Clock(), recorder()
    progress = work_units.Progress(publish, clock)
    progress.begin('after'); progress.running('lint')
    monkeypatch.setattr(work_units, 'SILENT_NOTE_SECONDS', 1)
    token = work_units.PROGRESS.set(progress)
    try:
        watch = work_units.StallWatch(lambda *a: 'once')
    finally:
        work_units.PROGRESS.reset(token)
    clock.now = 20
    assert watch('effect', sample(20, 5)) is False  # the stall logic is unchanged
    assert said[-1][0] == 'silent for 5 s: check after: lint'


def test_a_real_silent_check_republishes_its_activity_and_without_a_progress_nothing_changes(monkeypatch):
    monkeypatch.setattr(work_units, 'PROGRESS_SECONDS', 1)
    monkeypatch.setattr(work_units, 'SILENT_NOTE_SECONDS', 2)
    said, publish = recorder()
    progress = work_units.Progress(publish)
    progress.begin('before')
    command = {'sleeper': [sys.executable, '-B', '-c', 'import time; time.sleep(5)']}
    token = work_units.PROGRESS.set(progress)
    try:
        state, _, _ = checks(command, lambda *a: 'once')
    finally:
        work_units.PROGRESS.reset(token)
    assert state == 'green' and said[0][0] == 'check before: sleeper'
    assert any(t.startswith('silent for ') and t.endswith(': check before: sleeper') for t, _ in said)
    assert checks(command, lambda *a: 'once')[0] == 'green'  # no Progress set: as before


@pytest.fixture
def unit_run(setup, tmp_path, monkeypatch):
    config, custody, root = setup
    repository(root)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    said, publish = recorder()

    def run(rungs, worker, *, conversation=True, review=None, progress=True):
        config.update(writable={'repo': ['a.py']}, verifiers={'conversation-diff': {'repository': 'repo',
            'argv': ['git', 'diff', '--check', 'HEAD', '--', 'a.py']}}, **({'conversation_flow': True} if conversation else {}))
        monkeypatch.setattr(work_units, 'routes', lambda *a, **kw: iter(rungs))
        monkeypatch.setattr(native_agent, 'run_session', lambda child, *a, **kw: worker(child, said))
        token = work_units.PROGRESS.set(work_units.Progress(publish) if progress else None)
        try:
            return work_units.run_unit(custody, {'config': config, 'deployment': deployment(),
                'limits': {'max_model_calls': 30}, 'deadline_utc': None, **({'conversation_flow': True} if conversation else {})},
                {'title': 'New document', 'target_paths': ['a.py'], 'criteria': ['Write the document'],
                 'verifiers': ['conversation-diff']}, lambda: False, review=review)
        finally:
            work_units.PROGRESS.reset(token)
    return run, said


def writes(child, said):
    target = Path(child['repositories']['repo'])/'a.py'
    target.write_bytes(b'New content.\n')
    return {'status': 'PROPOSED', 'model_calls': 1}


ACCEPT = {'verdict': 'ACCEPT', 'sources': [], 'reference_sources': []}


def test_the_row_names_the_check_then_the_implementer_before_its_session_then_the_check_after(unit_run):
    run, said = unit_run
    seen = {}

    def worker(child, said):
        seen['when'] = list(said)  # what the row said when the model session started
        return writes(child, said)
    result = run([('worker', deployment())], worker, review=lambda *a: ACCEPT)
    assert result['verdict'] == 'VERIFIED'
    label = work_units.implementer_label('worker', deployment())
    assert label != 'worker' and not label.startswith('Implementer')  # the row already says the role
    targets = [t for t, _ in said]
    assert targets[0] == label and 'check before: conversation-diff' in targets
    assert seen['when'][-1][0] == label and 'working' in seen['when'][-1][1]  # before any model session row
    assert targets.index('check before: conversation-diff') < targets.index('check after: conversation-diff')
    assert targets[-1] == 'check after: conversation-diff'
    before = next(d for t, d in said if t == 'check before: conversation-diff')
    assert label in before and 'before the change' in before


def test_the_checks_before_applying_are_named_after_the_audit(unit_run):
    run, said = unit_run

    def review(*a):
        work_units.PROGRESS.get().audit()  # as conversation_tasks' audit does
        return ACCEPT
    assert run([('worker', deployment())], writes, review=review)['verdict'] == 'VERIFIED'
    targets = [t for t, _ in said]
    assert 'check after: conversation-diff' in targets[targets.index('audit'):]


def test_when_a_rung_fails_the_next_one_is_named(unit_run):
    run, said = unit_run
    second = {**deployment(), 'native_profile': {**deployment()['native_profile'], 'model': 'another-model'}}

    def worker(child, said):
        raise ValueError('the first route cannot start')
    run([('first', deployment()), ('second', second)], worker, conversation=False)
    labels = [work_units.implementer_label('first', deployment()), work_units.implementer_label('second', second)]
    assert labels[0] != labels[1]
    assert [t for t, _ in said if t in labels] == [labels[0], labels[0], labels[1]]  # rung, then its session; the next rung


def test_without_a_progress_the_unit_runs_exactly_as_before(unit_run):
    run, said = unit_run
    assert run([('worker', deployment())], writes, review=lambda *a: ACCEPT, progress=False)['verdict'] == 'VERIFIED'
    assert said == []


def test_a_progress_whose_publisher_fails_never_fails_the_work():
    def broken(target, detail):
        raise RuntimeError('screen gone')
    progress = work_units.Progress(broken)
    progress.rung('x'); progress.begin('before'); progress.running('a'); progress.sample(sample(99, 99))
    progress.working(); progress.audit()


ROW = dict(kind='tool-activity', id='effect', operation='implement', target='', state='running', duration_ms=None,
           detail='Tool request recorded; awaiting its result.', receipt='4 · '+'a'*64)


def test_describe_republishes_the_running_row_for_the_screen_only():
    events = []
    journal = ActivityJournal(object(), events.append)
    journal.rows['effect'] = activity_view(ROW)
    journal.describe('effect', 'check before: tests', 'Checking the project before the change: tests.')
    (event,) = events
    assert event['channel'] == 'activity' and event['activity']['state'] == 'running'
    assert event['activity']['target'] == 'check before: tests' and event['activity']['id'] == 'effect'
    assert activity_view(event['activity'])  # it still validates
    assert journal.rows['effect']['target'] == '' and journal.rows['effect']['detail'].startswith('Tool request')
    journal.rows['effect'] = {**journal.rows['effect'], 'state': 'succeeded'}
    journal.describe('effect', 'late', 'late')  # a finished tool is never described again
    journal.describe('unknown', 'x', 'x')
    assert len(events) == 1
    ActivityJournal(object(), None).describe('effect', 'x', 'x')  # no callback: nothing happens


def test_the_title_keeps_the_start_of_the_phrase_and_fits_80_columns(tmp_path, monkeypatch):
    from relay.console_output import ToolActivity
    from relay.native_console import WorkConsole
    from test_native_console import FakeBackend
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'state'))
    root = tmp_path/'project'; root.mkdir()

    async def scenario():
        app = WorkConsole(root, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            row = {**ROW, 'target': 'silent for 90 s: check before: ' + 'pytest-tests:tests/test_a_long_name.py'*2}
            app.assistant_activity({'channel': 'activity', 'activity': row})
            await pilot.pause()
            title = app.query_one(ToolActivity).title
            assert title.startswith('Delegate implementation silent for 90 s') and '…' in title
            assert 'Awaiting result' in title and len(title) <= 80
    asyncio.run(scenario())


def test_the_sandbox_question_names_no_tool_the_project_may_not_use():
    from relay.command_protection import BLOCKED_DURING
    texts = [permissions.sandbox_question(f'{code}: every failing test of this check failed. Not run: nobody asked.')[0]
             for code in (*permissions.BLOCKED, *work_units.SANDBOX_LIMITS, 'ISOLATION_TIMEOUT')]
    texts.append(permissions.sandbox_question(BLOCKED_DURING + ': a network connection. Output: x')[0])
    assert texts and not any('node --test' in text for text in texts)
