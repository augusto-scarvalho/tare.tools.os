"""A model on this machine or its network is local however its host is written; a hosted one never becomes local.
challenger.local decides without a DNS query (it runs on routing paths, offline)."""
import socket

import pytest

from relay import execution_scope, turn_engine
from relay.challenger import local


def dep(endpoint, **extra):
    return {'id': 'fixture', 'native_profile': {'protocol': 'openai-chat', 'model': 'm', 'endpoint': endpoint}, **extra}


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError('challenger.local must not resolve names')
    for name in ('getaddrinfo', 'gethostbyname', 'gethostbyname_ex', 'gethostbyaddr'):
        monkeypatch.setattr(socket, name, refuse)


@pytest.mark.parametrize('endpoint', [
    'http://127.0.0.1:8080/v1', 'http://10.0.0.5:8080', 'http://192.168.0.10:8080/v1', 'http://100.64.0.10:8080/v1',
    'http://[::1]:8080/v1', 'http://[fd00::1]:8080', 'http://localhost:8080/v1', 'http://aaaaa:8080/v1',
    'http://box.local:11434/v1', 'https://gpu.tailnet-name.ts.net/v1', 'http://models.internal/v1',
    'http://nas.lan:8080', 'http://pc.home.arpa:8080', 'http://app.localhost:8080',
    'http://AAAAA.:8080/v1', 'http://Box.Local.:11434/v1', 'http://GPU.Tailnet-Name.TS.NET./v1'])
def test_this_machine_and_its_network_are_local(endpoint):
    assert local(dep(endpoint))


@pytest.mark.parametrize('endpoint', [
    'http://8.8.8.8:8080/v1', 'https://api.openai.com/v1', 'https://openrouter.ai/api/v1',
    'https://generativelanguage.googleapis.com/v1beta/openai', 'https://bedrock-runtime.us-east-1.amazonaws.com',
    'https://myresource.openai.azure.com/openai/v1', 'https://llm.corp.example.com/v1', 'https://opencode.ai/inference/go/openai/v1',
    'https://chatgpt.com/backend-api/codex', 'https://models.example.com:8080/v1', 'https://api.local-models.com/v1',
    'cli://codex', 'cli://claude', 'http:///v1', 'x'])
def test_hosted_endpoints_and_vendor_clis_stay_hosted(endpoint):
    assert not local(dep(endpoint))  # conftest's gateway is http://127.0.0.1:8080


def test_the_configured_gateway_is_local_whatever_its_name(monkeypatch):
    monkeypatch.setenv('TARE_LOCAL_GATEWAY', 'http://Models.Example.com.:8080')
    for endpoint in ('http://models.example.com:8080/v1', 'https://MODELS.example.com./v1'):
        assert local(dep(endpoint))
    assert not local(dep('https://other.example.com/v1'))
    monkeypatch.setenv('TARE_LOCAL_GATEWAY', 'not a gateway')  # malformed: the name rules alone decide, no error
    assert not local(dep('http://models.example.com:8080/v1')) and local(dep('http://aaaaa:8080/v1'))


def test_the_explicit_accounting_decides_first():
    assert not local(dep('http://127.0.0.1:8080/v1', native_challenger_accounting='usd'))
    assert not local(dep('http://aaaaa:8080/v1', native_challenger_accounting='quota'))
    assert local(dep('https://api.openai.com/v1', native_challenger_accounting='local'))


def test_the_gpu_reservation_secret_goes_only_to_this_machines_own_gateways(monkeypatch, tmp_path):
    import json
    from compute_plane.gpu_lease_client import RESERVATION_HEADER
    from relay import environment
    from relay.challenger import client_headers
    store = tmp_path/'environment.json'  # tare setup saved a second gateway, by host name
    store.write_text(json.dumps({'schema': environment.SCHEMA, 'local_models': {'gateways': [
        {'origin': 'http://models.example.com:8080'}, {'origin': 'http://gpu2:8080'}]}}), encoding='utf-8')
    monkeypatch.setenv('TARE_ENVIRONMENT_STORE', str(store))
    monkeypatch.setenv('TARE_GPU_RESERVATION', 's3cret')
    monkeypatch.setenv('TARE_LOCAL_GATEWAY', 'http://Models.Example.com.:8080')
    secret = lambda endpoint: client_headers(dep(endpoint)).get(RESERVATION_HEADER) == 's3cret'
    for endpoint in ('http://models.example.com:8080/v1', 'http://gpu2:8080/v1', 'http://10.0.0.5:8080/v1',
                     'http://127.0.0.1:8080/v1', 'http://100.64.0.10:8080/v1', 'http://localhost:8080/v1'):
        assert secret(endpoint), endpoint
    # Local by the form of the name only, or the gateway's host on another port: the User-Agent, not the secret.
    for endpoint in ('http://box.internal:8080/v1', 'https://gpu.other-tailnet.ts.net/v1', 'http://aaaaa:8080/v1',
                     'http://models.example.com:9000/v1', 'https://gpu2/v1'):
        assert local(dep(endpoint)) and 'User-Agent' in client_headers(dep(endpoint)) and not secret(endpoint), endpoint
    assert client_headers(dep('https://api.openai.com/v1')) == {}
    monkeypatch.setenv('TARE_LOCAL_GATEWAY', 'http://192.168.0.10:8080')  # the gateway by IP, as today
    assert secret('http://192.168.0.10:8080/v1') and not secret('http://box.internal:8080/v1')


def test_local_only_admits_a_host_name_gateway_and_api_only_does_not(monkeypatch):
    monkeypatch.setenv('TARE_LOCAL_GATEWAY', 'http://models.example.com:8080')
    for endpoint in ('http://aaaaa:8080/v1', 'http://models.example.com:8080/v1'):
        assert execution_scope.kind(dep(endpoint)) == 'local'
        assert execution_scope.Scope(only='local').allows(dep(endpoint))[0]
        assert not execution_scope.Scope(only='api').allows(dep(endpoint))[0]
    assert not execution_scope.Scope(only='local').allows(dep('https://api.example.test/v1'))[0]


def test_leaving_the_gpu_to_others_skips_a_host_name_gateway(monkeypatch):
    monkeypatch.setenv(turn_engine.AVOID_LOCAL_GPU, '1')
    endpoints = {'gpu': 'http://aaaaa:8080/v1', 'cloud': 'https://api.example.test/v1'}
    monkeypatch.setattr('relay.native_catalog.select', lambda name: {**dep(endpoints[name]), 'id': name})
    monkeypatch.setattr('relay.native_catalog.resolve_host_mode', lambda value: value)
    monkeypatch.setattr(turn_engine, 'local_gpu_busy', lambda value: None)
    _, name, _, skipped = turn_engine.choose([['gpu'], ['cloud']])
    assert name == 'cloud' and skipped[0]['diagnostic']['code'] == 'LOCAL_GPU_DISABLED'
