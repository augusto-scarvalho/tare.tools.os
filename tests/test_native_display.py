"""CLI presentation must preserve automation and distinguish evidence from claims."""
import contextlib
import io
import json

import pytest

from relay import native_agent
from relay.native_display import conversation_result, render_result


def test_recorded_checks_distinguish_failure_absence_and_later_edits():
    result = {'status': 'PROPOSED', 'output': 'The model claims everything passed.',
              'capability_observations': {'effects': 2, 'verifications': {
                  'current': {'returncode': 0, 'effects': 2},
                  'failed': {'returncode': 1, 'effects': 2},
                  'old': {'returncode': 0, 'effects': 1},
                  'unknown': {'effects': 2}}},
              'tokens': {'total_tokens': {'known_sum': 100, 'unknown_calls': 1}}}
    output = render_result(result)
    assert 'current: PASS\n' in output and 'failed: FAIL\n' in output
    assert 'old: PASS (before later edits or freshness unknown)' in output
    assert 'unknown: UNKNOWN' in output and '100 (partial)' in output
    assert 'None recorded this invocation.' in render_result({'status': 'PROPOSED'})
    assert 'freshness unknown' in render_result({'status': 'PROPOSED',
        'capability_observations': {'verifications': {'old': {'returncode': 0}}}})


@pytest.mark.parametrize('tty,flag,as_json', [(True, None, False), (False, None, True),
                                           (True, 'json', True), (False, 'text', False)])
def test_cli_auto_and_explicit_formats_preserve_the_result(monkeypatch, tmp_path, tty, flag, as_json):
    class Terminal(io.StringIO):
        def isatty(self):
            return tty

    result = {'status': 'PROPOSED', 'output': 'Useful answer', 'session': "C:/space and 'quote/session"}
    monkeypatch.setattr(native_agent, 'run_session', lambda *a, **kw: result)
    output = Terminal()
    session = tmp_path/'existing-session'; session.mkdir()
    (session/'native-agent.json').write_text(json.dumps({'config': {}}))
    args = ['--resume', '--session', str(session)] + (['--format', flag] if flag else [])
    with contextlib.redirect_stdout(output):
        assert native_agent.main(args) == 0
    if as_json:
        assert json.loads(output.getvalue()) == result
    else:
        assert 'Useful answer' in output.getvalue()
        assert '--resume --interactive --format text --session' in output.getvalue()


def test_text_error_keeps_failure_exit_code_and_does_not_invent_session(capsys):
    assert native_agent.main(['--format', 'text', '--resume']) == 2
    output = capsys.readouterr().out
    assert 'Status: BLOCKED' in output and 'native_agent_resume_requires_session' in output
    assert 'Continue' not in output


@pytest.mark.parametrize('output_format', ['text', 'json'])
def test_stream_presentation_preserves_json_and_keeps_reasoning_out_of_text(monkeypatch, capsys, tmp_path, output_format):
    def run(*args, on_progress=None, **kwargs):
        on_progress({'call_id': '1', 'channel': 'reasoning', 'text': 'internal reasoning'})
        on_progress({'call_id': '1', 'channel': 'text', 'text': 'Provisional response'})
        return {'status': 'PROPOSED', 'output': 'Final response'}

    monkeypatch.setattr(native_agent, 'run_session', run)
    assert native_agent.main(['--stream', '--format', output_format, '--instruction', 'hello',
                              '--session', str(tmp_path/'session')]) == 0
    output = capsys.readouterr()
    assert 'internal reasoning' not in output.err
    if output_format == 'text':
        assert '[Working]' in output.err and 'Provisional response' in output.err
        assert 'Final response' in output.out
        assert 'call_id' not in output.err
    else:
        assert json.loads(output.out)['output'] == 'Final response'
        assert [json.loads(line)['channel'] for line in output.err.splitlines()] == ['reasoning', 'text']


@pytest.mark.parametrize('contributor', [False, True])
def test_redirected_model_listing_is_machine_readable(capsys, monkeypatch, contributor):
    from relay.native_catalog import LOCAL
    from relay import environment
    monkeypatch.setattr(environment, 'read', lambda: {'model_data_use': {'meta_contributor': contributor}})
    assert native_agent.main(['--list-models']) == 0
    result = json.loads(capsys.readouterr().out)
    expected = set(LOCAL)
    if not contributor:
        expected.remove('openrouter-muse-spark-1-3-contributor')
    assert {row['deployment'] for row in result['models']} == expected
    assert result['availability'] == 'not_checked'


def test_unreadable_history_is_visible_without_changing_the_result_or_journal(tmp_path):
    journal = tmp_path/'journal.jsonl'
    journal.write_text('incomplete history\n')
    output = render_result({'status': 'PROPOSED', 'output': 'Answer', 'session': str(tmp_path)})
    assert 'Answer' in output and 'Earlier check history unavailable.' in output
    assert journal.read_text() == 'incomplete history\n'


def test_unfinished_invocation_refusal_has_actionable_recovery_guidance():
    result = {'status': 'RECONCILIATION_REQUIRED', 'reason': 'UNFINISHED_INVOCATION',
              'session': 'preserved-session-reference'}

    details = render_result(result)
    notice = conversation_result(result)['notice']

    for display in (details, notice):
        assert 'UNFINISHED_INVOCATION' in display
        assert 'This operation has no confirmed outcome.' in display
        assert 'Resume checks saved tool results and edit snapshots' in display
        assert 'external command without proof of completion is not repeated' in display
    assert 'Session: preserved-session-reference' in details
    assert 'Continue' not in details


def test_failed_claude_rate_limit_observation_has_honest_recovery_copy():
    result = {'status': 'BLOCKED', 'reason': 'MODEL_FAILURE',
              'failure_code': 'CLI_GENERATION_FAILED',
              'provider_rate_limit_observed': True}
    details = render_result(result)
    notice = conversation_result(result)['notice']
    for output in (details, notice):
        assert 'Claude reported an API rate limit' in output
        assert 'final cause and remote usage are unknown' in output
        assert 'Review saved changes' in output
        assert 'Continue task' in output
        assert 'recorded usage and existing limits are preserved' in output
        assert 'quota exhausted' not in output.lower()
    assert 'Failure: CLI_GENERATION_FAILED' in details
    assert 'CLI_GENERATION_FAILED' in conversation_result(result)['details']
    for changed in ({**result, 'provider_rate_limit_observed': False},
                    {**result, 'failure_code': 'CLI_USAGE_BUDGET_REACHED'},
                    {**result, 'status': 'PROPOSED'}):
        assert 'Claude reported an API rate limit' not in conversation_result(changed)['notice']


def test_http_429_points_to_explicit_continuation_without_claiming_balance():
    value = {'status': 'BLOCKED', 'reason': 'MODEL_FAILURE', 'failure_code': 'HTTP_429'}
    notice = conversation_result(value)['notice']
    assert 'HTTP 429' in notice and 'Continue task' in notice
    assert 'quota exhausted' not in notice.lower()


def test_a_spent_account_says_until_when_and_that_its_routes_are_skipped():
    import time
    until = time.mktime((2026, 10, 2, 17, 0, 0, 0, 0, -1))
    value = {'status': 'BLOCKED', 'reason': 'MODEL_FAILURE', 'failure_code': 'CLI_USAGE_LIMIT',
             'usage_limit': {'account': 'claude', 'until': until, 'resets_at': until, 'kind': 'five_hour'}}
    notice = conversation_result(value)['notice']
    assert 'claude account' in notice and 'until 17:00' in notice and 'skip its routes' in notice


def test_cancellation_does_not_receive_unfinished_invocation_guidance():
    notice = conversation_result({'status': 'CANCELLED',
                                  'reason': 'UNFINISHED_INVOCATION'})['notice']
    assert notice.startswith('Cancelled. Earlier changes may still be present;')
    assert 'Execution and cleanup are unconfirmed.' not in notice


def test_terminal_tests_are_separate_from_named_checks_and_model_claims():
    result = {'status': 'PROPOSED', 'output': '12 tests passed',
              'capability_observations': {'terminal': [
                  {'operation': 'terminal_start', 'status': 'succeeded'},
                  {'operation': 'terminal_read', 'status': 'succeeded'}]}}
    display = conversation_result(result)
    assert 'No named checks recorded this turn.' in display['notice']
    assert 'Terminal activity recorded.' in display['notice']
    assert 'Inspect command output' in display['notice']
    assert 'Recorded named checks:' in display['details']
    assert 'terminal commands are not named checks' in display['details']
    assert 'PASS' not in display['details']  # Starting/reading a command is not verification success.
    assert 'Terminal activity recorded.' not in conversation_result({'status': 'PROPOSED'})['notice']


@pytest.mark.parametrize('remaining,deadline,expected', [
    (2, None, 'Resume discards the incomplete response'),
    (0, None, 'model-call limit is exhausted'),
    (2, 10, 'session deadline has expired'),
])
def test_recovery_guidance_matches_cleanup_and_live_limits(monkeypatch, remaining, deadline, expected):
    monkeypatch.setattr('relay.native_display.time.time', lambda: 20)
    result = {'status': 'RECONCILIATION_REQUIRED', 'reason': 'UNFINISHED_INVOCATION',
              'model_recovery': {'transport_eligible': True, 'available': True,
                  'remaining_model_calls': remaining, 'deadline_utc': deadline}}
    notice = conversation_result(result)['notice']
    assert 'local cleanup is confirmed' in notice and expected in notice
    assert 'Remote completion and usage remain unknown' in notice
    if remaining == 0 or deadline is not None:
        assert 'Resume discards' not in notice


@pytest.mark.parametrize('code,meaning', [
    ('GPU_STATUS_UNKNOWN', 'the local model is unavailable'),
    ('CLI_USAGE_LIMIT', 'the quota or usage limit is exhausted'),
    ('OTHER_FAILURE', '(OTHER_FAILURE)'),
])
def test_generic_failure_uses_only_recorded_reason_and_preserves_evidence(code, meaning):
    result = {'status': 'FAILED', 'reason': code,
              'failure_code': 'RECORDED_FAILURE',
              'session': 'saved-evidence', 'output': 'Earlier answer',
              'source_changes': [{'repository': 'project', 'path': 'a.py', 'current_sha256': 'new'}]}
    view = conversation_result(result)
    assert meaning in view['notice']
    assert 'open Execution details before continuing' in view['notice']
    if code != 'OTHER_FAILURE':
        assert code not in view['notice']
    assert code in view['details'] and 'saved-evidence' in view['details']
    assert 'a.py' in view['details'] and view['body'] == 'Earlier answer'
    assert result['reason'] == code and result['failure_code'] == 'RECORDED_FAILURE'


def test_generic_failure_without_reason_does_not_invent_a_cause():
    view = conversation_result({'status': 'FAILED'})
    assert view['notice'] == 'Execution failed; open Execution details before continuing.'


def test_changed_turn_still_warns_when_no_checks_ran():
    view = conversation_result({'status': 'PROPOSED', 'source_changes': [
        {'repository': 'project', 'path': 'a.py', 'current_sha256': 'new'}]})
    assert 'No named checks recorded this turn.' in view['notice']
    assert 'Earlier checks do not verify' in view['notice']
