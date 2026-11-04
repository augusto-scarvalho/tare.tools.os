"""FLOW-05: internal summaries and instruction metadata stay out of answers/previews."""
import asyncio
import json

from relay.console_output import Transcript, observe_process
from relay.native_activity import ActivityJournal, full_output, output_preview
from relay.native_console import WorkConsole
from test_native_console import FakeBackend

AGENTS_BODY = '# AGENTS.md\n' + 'Swarm governance text. ' * 400


def instructions():
    return [{'path': 'tare.tools.os/AGENTS.md', 'repository': 'projects', 'sha256': 'a' * 64, 'text': AGENTS_BODY},
            {'path': 'tare.tools.os/relay/AGENTS.md', 'repository': 'projects', 'sha256': None, 'text': None}]


def test_preview_omits_only_scoped_instruction_metadata():
    edit = {'status': 'APPLIED', 'path': 'tare.tools.os/relay/x.py', 'sha256': 'b' * 64,
            'effects': {'replacements': 1}, 'scoped_project_instructions': instructions()}
    preview = output_preview(edit)
    assert 'Swarm governance' not in preview and len(preview) < 1000
    assert 'APPLIED' in preview and 'replacements' in preview and 'b' * 64 in preview
    assert 'tare.tools.os/AGENTS.md' in preview and 'Full output retains them' in preview
    assert edit['scoped_project_instructions'][0]['text'] == AGENTS_BODY  # input not mutated
    failure = {'error_type': 'StaleSha', 'reason': 'sha256 mismatch', 'scoped_project_instructions': instructions()}
    preview = output_preview(failure)
    assert 'StaleSha' in preview and 'sha256 mismatch' in preview and 'Swarm governance' not in preview
    read = {'text': '1: source line', 'sha256': 'c' * 64, 'scoped_project_instructions': instructions()}
    assert '1: source line' in output_preview(read) and 'Swarm governance' not in output_preview(read)
    terminal = {'status': 'exited', 'returncode': 1, 'output': {'stderr': {'text': 'Traceback boom'}},
                'scoped_project_instructions': instructions()}
    preview = output_preview(terminal)
    assert 'Exit code: 1' in preview and 'Traceback boom' in preview and 'Swarm governance' not in preview


def test_preview_without_loaded_instructions_is_compatible():
    plain = {'status': 'ok', 'items': [1, 2]}
    assert output_preview(plain) == json.dumps(plain, ensure_ascii=False, indent=2)
    assert output_preview([1, 2]) == json.dumps([1, 2], ensure_ascii=False, indent=2)
    empty = {'status': 'ok', 'scoped_project_instructions': [{'path': 'x/AGENTS.md', 'text': None}]}
    assert output_preview(empty) == json.dumps({'status': 'ok'}, ensure_ascii=False, indent=2)


def test_activity_journal_keeps_exact_output_for_full_output():
    class Record:
        def __init__(self, sequence, event):
            self.sequence, self._event, self.sha256 = sequence, event, 'd' * 64
        def event(self):
            return self._event

    class Journal:
        def __init__(self):
            self.events = []
        def append(self, event):
            self.events.append(event)
            return Record(len(self.events), event)

    rows, journal = [], Journal()
    observed = ActivityJournal(journal, rows.append)
    result = {'status': 'APPLIED', 'path': 'x.py', 'scoped_project_instructions': instructions()}
    exact = json.dumps(result)
    observed.append({'kind': 'effect_requested', 'effect_id': 'e1',
                     'tool_call': {'name': 'edit', 'arguments_json': json.dumps({'path': 'x.py'})}})
    observed.append({'kind': 'effect_returned',
                     'result': {'effect_id': 'e1', 'status': 'succeeded', 'output_json': exact}})
    assert observed.observation_failures == 0
    detail = rows[-1]['activity']['detail']
    assert rows[-1]['activity']['state'] == 'succeeded' and 'APPLIED' in detail
    assert 'Swarm governance' not in detail and 'Full output retains them' in detail
    assert journal.events[-1]['result']['output_json'] == exact  # journal/Full output source is exact
    assert json.loads(exact)['scoped_project_instructions'][0]['text'] == AGENTS_BODY


def test_summary_purpose_stream_is_forwarded_with_purpose():
    async def scenario():
        class Process:
            def __init__(self):
                self.stdout = asyncio.StreamReader(); self.stderr = asyncio.StreamReader()
            async def wait(self): return 0
        process, events, diagnostics = Process(), [], []
        process.stdout.feed_data(b'{"status":"PROPOSED"}'); process.stdout.feed_eof()
        values = [{'channel': 'text', 'call_id': 's', 'purpose': 'summary', 'text': 'digest'},
                  {'channel': 'text', 'call_id': 'm', 'purpose': 'model', 'text': 'Answer'}]
        process.stderr.feed_data(('\n'.join(map(json.dumps, values)) + '\n').encode()); process.stderr.feed_eof()
        await observe_process(process, events.append, diagnostics.append)
        assert [e.get('purpose') for e in events] == ['summary', 'model'] and not diagnostics
    asyncio.run(scenario())


def test_summary_stream_is_internal_progress_and_answers_still_stream(tmp_path):
    async def scenario():
        app = WorkConsole(tmp_path, backend=FakeBackend(), initial_mode='assistant')
        async with app.run_test(size=(100, 30)) as pilot:
            await app.workers.wait_for_complete(); await pilot.pause()
            output = app.query_one('#output', Transcript)
            before = len(output.items)
            for part in ('SUMMARY_SECRET one ', 'SUMMARY_SECRET two'):
                output.stream({'channel': 'text', 'call_id': 's1', 'purpose': 'summary', 'text': part})
            await pilot.pause()
            assert output.live is None and 'SUMMARY_SECRET' not in output.live_text
            added = output.items[before:]
            assert len(added) == 1 and 'Compacting' in str(added[0].title)
            output.stream({'channel': 'text', 'call_id': 'm1', 'purpose': 'model', 'text': 'Real answer'})
            assert output.live is not None and output.live_text == 'Real answer'
            output.finish_stream()
            output.stream({'channel': 'text', 'call_id': 'legacy', 'text': 'Legacy answer'})
            assert output.live is not None and output.live_text == 'Legacy answer'
            output.finish_stream(); await pilot.pause()
    asyncio.run(scenario())


def test_long_instruction_metadata_follows_and_cannot_hide_error():
    paths = [{'path': ('deep/' * 500) + str(index) + '/AGENTS.md', 'text': AGENTS_BODY}
             for index in range(40)]
    preview = output_preview({'error_type': 'EDIT_FAILED_SENTINEL', 'reason': 'replacement missed',
                              'scoped_project_instructions': paths})
    assert preview.startswith('{')
    assert 'EDIT_FAILED_SENTINEL' in preview[:8000]
    assert preview.index('EDIT_FAILED_SENTINEL') < preview.index('Scoped project instructions omitted')
    assert len(preview) < 8000
    assert 'Swarm governance' not in preview


def test_transcript_summary_state_initializes_and_clear_resets(monkeypatch):
    transcript = Transcript()
    assert transcript.summary_key is None
    transcript.summary_key = 'summary-call'
    class Welcome:
        display = False
    welcome = Welcome()
    monkeypatch.setattr(transcript, 'query_one', lambda *args, **kwargs: welcome)
    monkeypatch.setattr(transcript, 'anchor', lambda: None)
    transcript.clear()
    assert transcript.summary_key is None


def test_real_full_output_retains_instruction_metadata(tmp_path):
    from compute_plane.native_journal import NativeJournal, save_json
    project = tmp_path / 'project'
    project.mkdir()
    custody = tmp_path / 'custody'
    custody.mkdir()
    journal = NativeJournal(custody / 'journal.jsonl')
    journal.append({'kind': 'effect_requested', 'effect_id': 'effect-1',
                    'tool_call': {'name': 'read', 'arguments_json': json.dumps({'path': 'x.py'})}})
    exact = {'error_type': 'EDIT_FAILED_SENTINEL', 'scoped_project_instructions': instructions()}
    record = journal.append({'kind': 'effect_returned', 'result': {'effect_id': 'effect-1',
                             'status': 'failed', 'output_json': json.dumps(exact)}})
    save_json(custody / 'native-agent.json', {'config': {'repositories': {'projects': str(project)}}})
    value = {'kind': 'tool-activity', 'id': 'effect-1', 'operation': 'read', 'state': 'failed',
             'target': 'x.py', 'detail': output_preview(exact), 'duration_ms': 1,
             'receipt': f'{record.sequence} · {record.sha256}', 'truncated': False}
    loaded = json.loads(full_output(str(custody), str(project), value))
    assert loaded == exact
    assert loaded['scoped_project_instructions'][0]['text'] == AGENTS_BODY
