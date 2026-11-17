"""The provider's own count for the organization, read with an admin key the owner stored apart (tare quota)."""
import time
from io import StringIO
from pathlib import Path

import pytest

from relay import environment, provider_quota

DAY = 86400
NOW = (time.time() // DAY) * DAY + 3600  # an hour into a UTC day
ADMIN = 'sk-fixture-admin-key'


def iso(epoch):
    return time.strftime('%Y-%m-%dT00:00:00Z', time.gmtime(epoch))


def test_the_two_reports_are_read_as_dollars_per_utc_day():
    today = (NOW // DAY) * DAY
    anthropic = {'data': [{'starting_at': iso(today), 'ending_at': iso(today + DAY), 'results': [
        {'amount': '123.78912', 'currency': 'USD', 'cost_type': 'tokens'}, {'amount': '76.21088', 'currency': 'USD'}]},
        {'starting_at': iso(today - DAY), 'ending_at': iso(today), 'results': []}], 'has_more': False, 'next_page': None}
    assert provider_quota.anthropic_days(anthropic) == [(today, pytest.approx(2.0)), (today - DAY, 0)]  # cents
    openai = {'object': 'page', 'data': [{'object': 'bucket', 'start_time': today, 'end_time': today + DAY, 'results': [
        {'object': 'organization.costs.result', 'amount': {'value': 0.13, 'currency': 'usd'}, 'line_item': None}]}]}
    assert provider_quota.openai_days(openai) == [(today, 0.13)]


@pytest.fixture
def client(monkeypatch):
    """A stand-in for the one GET: it records the request and answers `answer` (a body, or an exception to raise)."""
    import json
    seen = {'requests': []}

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, size):
            return json.dumps(seen['answer']).encode()

    class Opener:
        def open(self, request, timeout):
            seen['requests'].append((request.full_url, dict(request.header_items()), timeout))
            if isinstance(seen['answer'], Exception):
                raise seen['answer']
            return Reply()

    def build(*handlers):
        seen['handlers'] = [type(h).__name__ for h in handlers]
        return Opener()

    monkeypatch.setattr(provider_quota, 'build_opener', build)
    monkeypatch.setattr(provider_quota, 'secret', lambda name: ADMIN if name in seen.get('stored', ()) else None)
    return seen


def test_no_admin_key_asks_nothing(client):
    assert provider_quota.provider_count('anthropic-api', NOW) is None and provider_quota.provider_count('openai-api', NOW) is None
    assert client['requests'] == []


def test_the_stored_admin_key_reads_the_providers_own_count_and_goes_nowhere_else(client):
    today = (NOW // DAY) * DAY
    client['stored'] = ('ANTHROPIC_ADMIN_KEY', 'OPENAI_ADMIN_KEY')
    client['answer'] = {'data': [{'starting_at': iso(today - 2*DAY), 'results': [{'amount': '150', 'currency': 'USD'}]},
                                 {'starting_at': iso(today - 10*DAY), 'results': [{'amount': '250', 'currency': 'USD'}]}],
                        'has_more': False}
    note = provider_quota.provider_count('anthropic-api', NOW)
    assert note == ("Anthropic's own count for the organization: 7 days US$ 1.50, 30 days US$ 4.00 "
                    '(UTC days, every key of the organization)')
    url, headers, timeout = client['requests'][0]
    assert url == 'https://api.anthropic.com/v1/organizations/cost_report?limit=31&starting_at=' + iso(today - 29*DAY)
    assert headers['Authorization'] == 'Bearer ' + ADMIN and headers['Anthropic-version'] == '2026-10-21' and timeout == 20
    assert client['handlers'] == ['NoRedirect']  # the key is never carried to another address
    client['answer'] = {'data': [{'start_time': today, 'results': [{'amount': {'value': 0.5, 'currency': 'usd'}}]}], 'has_more': True}
    note = provider_quota.provider_count('openai-api', NOW)
    assert note == ("OpenAI's own count for the organization: 7 days US$ 0.50, 30 days US$ 0.50 or more: the report has "
                    'further pages (UTC days, every key of the organization)')
    url, headers, _ = client['requests'][1]
    assert url == f'https://api.openai.com/v1/organization/costs?bucket_width=1d&limit=31&start_time={int(today - 29*DAY)}'
    assert headers['Authorization'] == 'Bearer ' + ADMIN and 'Anthropic-version' not in headers


def test_a_refused_key_or_a_broken_answer_is_one_note_without_the_key_or_the_providers_text(client):
    from urllib.error import HTTPError
    client['stored'] = ('ANTHROPIC_ADMIN_KEY',)
    client['answer'] = HTTPError('https://api.anthropic.com/x', 401, 'echo ' + ADMIN, {}, None)
    note = provider_quota.provider_count('anthropic-api', NOW)
    assert note == ("ANTHROPIC_ADMIN_KEY did not read Anthropic's cost report: HTTP 401. It takes an admin key of the "
                    'organization, not an ordinary API key.')
    client['answer'] = {'error': {'message': 'echo ' + ADMIN}}
    note = provider_quota.provider_count('anthropic-api', NOW)
    assert note.startswith("ANTHROPIC_ADMIN_KEY did not read Anthropic's cost report: KeyError.") and ADMIN not in note


def test_the_count_is_a_note_under_the_account_before_tares_own_use(client, monkeypatch, tmp_path):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path))
    monkeypatch.setattr('relay.cli_discovery.locate', lambda endpoint: None)
    monkeypatch.setattr(provider_quota, 'secret', lambda name: ADMIN if name == 'OPENAI_ADMIN_KEY' else None)  # an admin key alone
    client['answer'] = {'data': [], 'has_more': False}
    report = provider_quota.refresh(NOW)
    account, = [a for a in report['accounts'] if a['account'] == 'openai-api']
    assert account['notes'] == ["OpenAI's own count for the organization: 7 days US$ 0.00, 30 days US$ 0.00 "
                                '(UTC days, every key of the organization)']
    assert ADMIN not in provider_quota.render(report, NOW) and ADMIN not in (tmp_path/'serve'/'provider-quota.json').read_text(encoding='utf-8')


def test_an_admin_key_is_stored_in_the_vault_and_is_no_provider_of_model_calls(monkeypatch):
    import keyring
    kept = {}
    monkeypatch.setattr(keyring, 'set_password', lambda service, name, secret: kept.update({name: secret}))
    monkeypatch.delenv('ANTHROPIC_ADMIN_KEY', raising=False)
    assert environment.store_credential({}, 'ANTHROPIC_ADMIN_KEY', StringIO(ADMIN + '\n')) == {}  # the setup marks nothing
    assert kept == {'ANTHROPIC_ADMIN_KEY': ADMIN}
    # No profile or table of routes names an admin variable: no model call can carry one.
    names = [report[0] for report in provider_quota.ADMIN_REPORTS.values()]
    for path in (Path(provider_quota.__file__).parent).rglob('*.json'):
        text = path.read_text(encoding='utf-8', errors='replace')
        assert not any(name in text for name in names), path
