"""The energy a local model's calls used: measured by the node (or declared), kept in watt-hours, shown in dollars."""
import io
import json
import urllib.error

import pytest

from relay import environment, provider_quota, provider_spend
from test_provider_quota import NOW, providers, usage  # noqa: F401  (the quota fixture)

LOCAL = {'id': 'qwen38-gsq', 'native_profile': {'model': 'Qwen38-GSQ', 'endpoint': 'http://127.0.0.1:8080/v1'}}
CLOUD = {'id': 'openai-api', 'native_credential_env': 'OPENAI_API_KEY',
         'native_profile': {'model': 'gpt-6.1-sol', 'endpoint': 'https://api.openai.com/v1'}}


class Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def node(monkeypatch, providers):
    """The node's /v1/gpu/power: set node.answer to a dict, or to an exception to raise. A price is set, as on a
    machine that measures."""
    environment.save(environment.set_local_energy({}, 'price_kwh', '0.18'))
    class Node:
        answer, asked = {'watts': 300.0, 'samples': 7}, []

    def urlopen(url, timeout=None):
        Node.asked.append((url, timeout))
        if isinstance(Node.answer, BaseException):
            raise Node.answer
        return Reply(Node.answer if isinstance(Node.answer, bytes) else json.dumps(Node.answer).encode())
    Node.asked = []
    monkeypatch.setattr('urllib.request.urlopen', urlopen)
    return Node


def ledger():
    path = provider_spend.folder()/'provider-spend.jsonl'
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []


def post(tmp_path, deployment=LOCAL, seconds=36, calls=1):
    session = tmp_path/'session'
    session.mkdir(exist_ok=True)
    return provider_spend.post(deployment, usage(*[None]*calls), session, now=NOW + seconds, since=NOW)


def test_measured_watts_times_the_span_is_the_energy_of_the_call(providers, node):
    assert post(providers) == 0  # no money row
    (row,) = ledger()
    assert row['account'] == 'local' and row['source'] == 'measured' and row['model'] == 'qwen38-gsq'
    assert row['energy_wh'] == pytest.approx(300 * 36 / 3600) == pytest.approx(3.0) and row['seconds'] == 36
    url, timeout = node.asked[0]
    assert url == f'http://127.0.0.1:8080/v1/gpu/power?since={NOW}&until={NOW + 36}' and timeout == 2


def test_declared_watts_count_only_when_the_node_gives_none(providers, node):
    environment.save(environment.set_local_energy({}, 'watts', '200'))
    node.answer = {'watts': None, 'samples': 0}  # an old node answers 404 (an error), a new one with no sample this
    post(providers, calls=1)
    node.answer = urllib.error.HTTPError('u', 404, 'not found', {}, None)
    post(providers, calls=2)
    node.answer = {'watts': 450, 'samples': 3}  # measured beats declared
    post(providers, calls=3)
    assert [(r['source'], round(r['energy_wh'], 3)) for r in ledger()] == [
        ('declared', 2.0), ('declared', 2.0), ('measured', 4.5)]


@pytest.mark.parametrize('answer', [{'watts': None, 'samples': 0}, urllib.error.URLError('down'), TimeoutError(),
                                    b'not json', {'watts': 'x'}, {'watts': float('nan')}, {'watts': -5}, {}, RuntimeError()])
def test_no_reading_and_no_declaration_records_nothing_and_the_call_is_unchanged(providers, node, answer):
    node.answer = answer
    assert post(providers) == 0 and ledger() == []
    assert json.loads((providers/'session'/'spend-posted.json').read_text())['calls'] == 1  # the marker still moves


def test_nothing_is_asked_or_recorded_until_a_price_or_watts_is_set(providers, node):
    environment.save({})
    assert post(providers) == 0 and node.asked == [] and ledger() == []


def test_a_failing_node_never_fails_the_post(providers, node, monkeypatch):
    environment.save(environment.set_local_energy({}, 'watts', '200'))
    node.answer = ConnectionResetError()
    assert post(providers) == 0 and ledger()[0]['source'] == 'declared'
    monkeypatch.setattr(provider_spend, 'record_energy', lambda *args: 1 / 0)  # whatever else goes wrong
    assert post(providers, calls=2) == 0


def test_only_new_calls_of_a_local_route_with_a_known_start_count(providers, node):
    post(providers, calls=2)
    post(providers, calls=2)  # the resumed session's cumulative report: nothing new
    assert len(ledger()) == 1
    asked = len(node.asked)
    other = providers/'other'
    other.mkdir()
    assert provider_spend.post(CLOUD, usage('0.5'), other, now=NOW) == 1  # a paid API route
    assert len(node.asked) == asked and [r['account'] for r in ledger()] == ['local', 'openai-api']
    third = providers/'third'
    third.mkdir()
    provider_spend.post(LOCAL, usage(None), third, now=NOW + 5)  # no known start: no span
    assert len(ledger()) == 2


def test_an_energy_row_is_in_no_spend_balance_or_cap(providers, node):
    provider_spend.declare('openai-api', 10, now=NOW - 60)
    post(providers)
    assert provider_spend.spent('local', 0) == 0 and provider_spend.spent('openai-api', 0) == 0
    assert provider_spend.remaining('openai-api') == {'currency': 'USD', 'total': 10.0, 'declared_at': NOW - 60,
                                                      'remaining': 10.0}
    assert provider_spend.equivalents('local', 0) == [] and provider_spend.last_plan('local') is None
    assert provider_spend.spent('local', 0, 'energy_wh') == pytest.approx(3.0)  # only asked for by name


def test_tare_quota_says_the_energy_of_seven_days_with_and_without_a_price(providers, node, capsys):
    assert 'local' not in {a['account'] for a in provider_quota.refresh(now=NOW)['accounts']}  # no row: nothing
    assert 'Local models' not in provider_quota.render(provider_quota.refresh(now=NOW), now=NOW)
    node.answer = {'watts': 400, 'samples': 720}
    for hour in range(5):  # 5 x (400 W for 1 h) = 2 kWh
        (providers/f's{hour}').mkdir()
        provider_spend.post(LOCAL, usage(None), providers/f's{hour}', now=NOW + 3600 * (hour + 1), since=NOW + 3600 * hour)
    now = NOW + 6 * 3600
    environment.save(environment.set_local_energy({}, 'watts', '350'))  # the price cleared since: the kWh stay
    text = provider_quota.render(provider_quota.refresh(now=now), now=now)
    assert 'Local models\n  local: 2.00 kWh in 7 days (GPU only); tare setup --energy-price US$_PER_KWH' in text
    environment.save(environment.set_local_energy({}, 'price_kwh', '0.18'))
    report = provider_quota.refresh(now=now)
    text = provider_quota.render(report, now=now)
    assert '  local: 2.00 kWh in 7 days, about US$ 0.36 at US$ 0.18/kWh (GPU only)' in text
    (local,) = [a for a in report['accounts'] if a['account'] == 'local']
    assert local['kind'] == 'local' and local['energy'] == {
        'days': 7, 'kwh': 2.0, 'declared_kwh': 0.0, 'price_kwh': 0.18, 'usd': 0.36, 'scope': 'GPU only'}
    assert provider_quota.main(['--cached', '--json']) == 0
    assert [a['energy']['usd'] for a in json.loads(capsys.readouterr().out)['accounts'] if a['account'] == 'local'] == [0.36]
    # A row older than 7 days is not in it; the price read now prices the stored energy.
    later = provider_quota.refresh(now=NOW + 8 * 86400 + 5 * 3600)
    assert 'local' not in {a['account'] for a in later['accounts']}


def test_declared_energy_is_said_to_be_declared(providers, node):
    environment.save(environment.set_local_energy({}, 'watts', '1000'))
    node.answer = {'watts': None, 'samples': 0}
    post(providers, seconds=3600)
    text = provider_quota.render(provider_quota.refresh(now=NOW + 3600), now=NOW + 3600)
    assert '1.00 kWh in 7 days' in text and '1.00 kWh of it at the watts you declared' in text


def test_the_setup_values_parse_clear_and_reject_nonsense(providers, capsys):
    value = environment.set_local_energy({}, 'price_kwh', '0.18')
    value = environment.set_local_energy(value, 'watts', ' 350 ')
    assert value['local_energy'] == {'price_kwh': 0.18, 'watts': 350.0}
    assert environment.set_local_energy(value, 'price_kwh', 'OFF')['local_energy'] == {'watts': 350.0}
    assert 'local_energy' not in environment.set_local_energy(environment.set_local_energy(value, 'watts', 'off'),
                                                              'price_kwh', 'off')
    for key, bad in (('price_kwh', 'free'), ('price_kwh', '0'), ('price_kwh', '-1'), ('price_kwh', 'nan'),
                     ('price_kwh', 'inf'), ('price_kwh', '1000'), ('watts', ''), ('watts', '99999')):
        with pytest.raises(ValueError):
            environment.set_local_energy(value, key, bad)
    assert environment.main(['--energy-price', '0.18', '--local-watts', '350']) == 0
    assert environment.read()['local_energy'] == {'price_kwh': 0.18, 'watts': 350.0}
    assert 'Local energy: US$ 0.18 per kWh, 350 W under load' in capsys.readouterr().out
    assert environment.main(['--energy-price', 'nonsense']) == 1 and 'takes a number above zero' in capsys.readouterr().err
    assert environment.main(['--energy-price', 'off']) == 0 and environment.read()['local_energy'] == {'watts': 350.0}
    assert provider_spend.local_energy() == {'watts': 350.0}
