"""Provider quota readings: the parsers run on the shapes each provider returned on 2026-11-10 (account data removed)."""
import json
from datetime import datetime

import pytest

from relay import provider_quota

NOW = datetime(2026, 11, 10, 8, 0).timestamp()
CLAUDE = ('You are currently using your subscription to power your Claude Code usage\n\n'
          'Current session: 12% used · resets 5pm\n'
          'Current week (all models): 39% used · resets Nov 15, 5:59pm\n'
          'Current week (Fable): 100% used · resets Nov 15, 6pm\n\n'
          "What's contributing to your usage?\n  99%  Stop sessions you no longer need\n")
CODEX = {'ordinaryUsageAllowed': False,
         'rateLimits': {'limitId': 'codex', 'primary': {'usedPercent': 100, 'windowDurationMins': 10080,
                                                        'resetsAt': 1794518478}, 'secondary': None},
         'rateLimitsByLimitId': {
             'codex': {'limitId': 'codex', 'planType': 'prolite', 'secondary': None, 'primary': {
                 'usedPercent': 100, 'windowDurationMins': 10080, 'resetsAt': 1794518478}},
             'premium': {'limitId': 'premium', 'planType': 'prolite', 'primary': {
                 'usedPercent': 25.5, 'windowDurationMins': 300, 'resetsAt': 1794300000}, 'secondary': None}},
         'rateLimitResetCredits': {'availableCount': 2, 'credits': []}}
AGY = {'description': 'Within each group, models share a weekly limit and a 5-hour limit.', 'groups': [
    {'name': 'Gemini Models', 'description': 'Models within this group: Gemini Flash, Gemini Pro', 'buckets': [
        {'id': 'gemini-weekly', 'window': 'weekly', 'remaining_fraction': 0.5160037875175476,
         'reset_time': '2026-11-14T01:20:02Z'},
        {'id': 'gemini-5h', 'window': '5h', 'remaining_fraction': 0.9988325238227844,
         'reset_time': '2026-11-10T09:32:32Z'}]},
    {'name': 'Claude and GPT models', 'description': 'Models within this group: Claude Opus, Claude Sonnet, GPT-OSS',
     'buckets': [{'id': '3p-weekly', 'window': 'weekly', 'remaining_fraction': 1, 'reset_time': '2026-11-17T05:05:27Z'}]},
    {'name': 'Gemini 4 Argon', 'description': 'For paid Pro plans, Argon quota is separate.', 'buckets': [
        {'id': 'gemini-argon-wtu-weekly', 'window': 'weekly', 'remaining_fraction': 0,
         'reset_time': '2026-11-14T04:57:36Z'}]}]}


def test_claude_usage_text_becomes_one_window_per_model_scope():
    session, week, fable = provider_quota.parse_claude(CLAUDE, NOW)
    assert (session['id'], session['scope'], session['remaining']) == ('session', None, 0.88)
    assert (week['id'], week['label'], week['scope'], week['remaining']) == ('week', 'week, all models', None, 0.61)
    assert (fable['id'], fable['scope'], fable['remaining']) == ('week:fable', 'fable', 0.0)
    # The reset is the next such local time; the provider's own words are kept beside it.
    assert datetime.fromtimestamp(week['resets_at']).strftime('%m-%d %H:%M') == '11-15 17:59'
    assert datetime.fromtimestamp(fable['resets_at']).strftime('%m-%d %H:%M') == '11-15 18:00'
    assert datetime.fromtimestamp(session['resets_at']).strftime('%m-%d %H:%M') == '11-10 17:00'
    assert week['resets'] == 'Nov 15, 5:59pm'
    assert provider_quota.parse_claude('Claude Code is using an API key.', NOW) == []


@pytest.mark.parametrize('text,expected', [('5pm', '11-10 17:00'), ('7am', '11-11 07:00'), ('Jan 2, 9:30am', '01-02 09:30'),
                                           ('soon', None), ('Foo 2, 9am', None), ('Feb 31, 9am', None)])
def test_claude_reset_is_the_next_such_time_or_unknown(text, expected):
    value = provider_quota.claude_reset(text, NOW)
    assert (value and datetime.fromtimestamp(value).strftime('%m-%d %H:%M')) == expected
    if text.startswith('Jan'):
        assert datetime.fromtimestamp(value).year == 2027


def test_claude_reset_honours_the_zone_the_cli_names():
    zoneinfo = pytest.importorskip('zoneinfo')
    try:
        zone = zoneinfo.ZoneInfo('America/Sao_Paulo')
    except zoneinfo.ZoneInfoNotFoundError:
        pytest.skip('no zone database on this machine')
    line = 'Current week (Fable): 34% used · resets Nov 15, 5:59pm (America/Sao_Paulo)'  # as the CLI printed it
    bucket, = provider_quota.parse_claude(line, NOW)
    assert bucket['resets_at'] == datetime(2026, 11, 15, 17, 59, tzinfo=zone).timestamp()
    assert bucket['resets'] == 'Nov 15, 5:59pm (America/Sao_Paulo)' and bucket['remaining'] == 0.66


def test_codex_limits_keep_each_limit_id_window_and_reset_credits():
    buckets, notes, plan = provider_quota.parse_codex(CODEX)
    assert [(b['id'], b['label'], b['scope'], b['remaining'], b['resets_at']) for b in buckets] == [
        ('codex:week', 'week', None, 0.0, 1794518478), ('premium:5 h', '5 h, premium', 'premium', 0.745, 1794300000)]
    assert notes == ['2 free limit resets available'] and plan == 'prolite'
    single = {'rateLimits': CODEX['rateLimits']}  # an older server without the per-id map
    assert [b['id'] for b in provider_quota.parse_codex(single)[0]] == ['codex:week']


def test_agy_groups_keep_their_models_and_the_separate_argon_quota():
    buckets, notes = provider_quota.parse_agy(AGY, {'remaining_credits': 0, 'upgrade_uri': 'https://example.invalid'})
    assert [(b['id'], b['label'], b['remaining']) for b in buckets] == [
        ('gemini-weekly', 'Gemini Models, week', 0.516), ('gemini-5h', 'Gemini Models, 5 h', 0.9988),
        ('3p-weekly', 'Claude and GPT models, week', 1.0), ('gemini-argon-wtu-weekly', 'Gemini 4 Argon, week', 0.0)]
    assert buckets[2]['scope'] == 'Claude and GPT models' and 'Claude Opus' in buckets[2]['models']
    assert buckets[3]['resets_at'] == datetime.fromisoformat('2026-11-14T04:57:36+00:00').timestamp()
    assert notes == ['AI credits: 0'] and provider_quota.parse_agy(AGY)[1] == []


def test_openrouter_credit_is_what_was_bought_minus_what_was_used():
    assert provider_quota.parse_openrouter({'data': {'total_credits': 20, 'total_usage': 14.770221788}}) == {
        'currency': 'USD', 'total': 20, 'remaining': 5.229778}
    with pytest.raises(ValueError):
        provider_quota.parse_openrouter({'data': {}})


@pytest.fixture
def providers(monkeypatch, tmp_path):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path))
    monkeypatch.setattr('relay.cli_discovery.locate', lambda endpoint: endpoint.rsplit('/', 1)[-1] + '.exe')
    monkeypatch.setattr(provider_quota, 'secret', lambda name: 'sk-fixture-secret' if name in {
        'OPENROUTER_API_KEY', 'OPENCODE_API_KEY', 'OPENAI_API_KEY'} else None)
    monkeypatch.setattr(provider_quota, 'codex_limits', lambda executable: CODEX)

    def run(executable, *arguments, timeout=90):
        if executable == 'claude.exe':
            assert arguments == ('-p', '/usage', '--output-format', 'json', '--setting-sources', '',
                                 '--no-session-persistence')
            return json.dumps({'result': CLAUDE, 'total_cost_usd': 0})
        if arguments[1] == '/credits':
            raise OSError('credits panel unavailable')
        return json.dumps({'status': 'SUCCESS', 'command': {'name': 'usage', 'data': AGY}})
    monkeypatch.setattr(provider_quota, 'run', run)

    def openrouter(key, now):
        raise TimeoutError('no answer')
    monkeypatch.setattr(provider_quota, 'read_openrouter', openrouter)
    return tmp_path


def test_refresh_reads_every_provider_and_one_failure_hides_no_other(providers):
    report = provider_quota.refresh(now=NOW)
    accounts = {a['account']: a for a in report['accounts']}
    assert list(accounts) == ['claude', 'codex', 'agy', 'openrouter', 'opencode-sub', 'openai-api']
    assert {a: accounts[a]['status'] for a in accounts} == {
        'claude': 'OBSERVED', 'codex': 'OBSERVED', 'agy': 'OBSERVED', 'openrouter': 'UNAVAILABLE',
        'opencode-sub': 'NOT_REPORTED', 'openai-api': 'NOT_REPORTED'}
    assert accounts['agy']['notes'] == [] and len(accounts['agy']['buckets']) == 4  # quota stands without /credits
    assert accounts['openrouter']['reason'] == 'TimeoutError: no answer' and 'sk-fixture-secret' not in json.dumps(report)
    assert provider_quota.cached() == report and (providers/'serve'/'provider-quota.json').is_file()


def test_the_table_says_what_is_left_when_it_resets_and_what_is_not_reported(providers, capsys):
    assert provider_quota.main(['--cached']) == 1 and 'No reading yet' in capsys.readouterr().err
    text = provider_quota.render(provider_quota.refresh(now=NOW), now=NOW)
    assert 'week, Fable' in text and 'none left' in text and 'Gemini 4 Argon, week' in text
    line = next(row for row in text.splitlines() if 'week, all models' in row)
    assert '61% left' in line and 'resets Sun 15 Nov 17:59' in line
    assert 'Codex subscription · prolite' in text and '2 free limit resets available' in text
    assert 'OpenRouter: no reading. TimeoutError: no answer' in text
    assert 'OpenAI API: not reported.' in text and 'no model call was made' in text
    assert provider_quota.main(['--cached', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['schema'] == 'tare.tools/provider-quota/1'


def test_tare_quota_is_a_registered_command():
    import tare_ops
    assert 'relay.provider_quota' in open(tare_ops.__file__, encoding='utf-8').read()
