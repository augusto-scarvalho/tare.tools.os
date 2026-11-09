"""Each machine probes a vendor CLI release once: the kernel's verdicts persist in cli-qualification.json."""
from relay import cli_qualification
from relay.native_display import provider_limit_guidance
from relay.native_generation import deployment_profile, inference_client
from relay.native_model_recovery import CLI_PREFLIGHT_ERRORS
from test_native_cli import deployment


def test_verdicts_persist_per_endpoint_and_a_prove_forgets_only_the_failed_ones(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    agy = cli_qualification.Store('cli://agy')
    agy['1.5.3'] = {'status': 'failed', 'probe_code': 'CLI_INVALID_RESPONSE'}
    agy['1.5.4'] = {'status': 'ok'}
    cli_qualification.Store('cli://claude')['2.1.300'] = {'status': 'ok'}
    assert dict(cli_qualification.Store('cli://agy')) == {'1.5.3': {'status': 'failed', 'probe_code':
                                                                    'CLI_INVALID_RESPONSE'}, '1.5.4': {'status': 'ok'}}
    cli_qualification.forget_failed('cli://agy')
    assert dict(cli_qualification.Store('cli://agy')) == {'1.5.4': {'status': 'ok'}}
    assert dict(cli_qualification.Store('cli://claude')) == {'2.1.300': {'status': 'ok'}}


def test_every_vendor_cli_client_reads_and_writes_this_machines_verdicts(tmp_path, monkeypatch):
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path/'storage'))
    dep = deployment(tmp_path)
    client = inference_client(dep, deployment_profile(dep, 'fixture'), credential=lambda: None)
    assert isinstance(client.qualification, cli_qualification.Store) and client.qualification.endpoint == 'cli://claude'
    client.qualification['2.1.299'] = {'status': 'ok'}  # what a passed probe writes
    assert cli_qualification.Store('cli://claude')['2.1.299'] == {'status': 'ok'}


def test_a_failed_qualification_is_a_preflight_refusal_explained_to_the_user():
    assert {'CLI_QUALIFICATION_FAILED', 'CLI_QUALIFICATION_INTERRUPTED'} <= CLI_PREFLIGHT_ERRORS
    text = provider_limit_guidance({'status': 'BLOCKED', 'reason': 'CLI_QUALIFICATION_FAILED',
        'runtime_observation': {'deployment_id': 'agy-cli'},
        'qualification': {'version': '1.5.3', 'probe_code': 'CLI_INVALID_RESPONSE',
                          'detail': "invalid arguments: missing property 'text'"}})
    assert 'agy-cli 1.5.3' in text and 'CLI_INVALID_RESPONSE' in text and 'not sent' in text
    assert 'tare setup --prove' in text
