"""Both Work flows and interactive sessions select the same CLI inference boundary."""
import json
import pytest
from relay.native_generation import deployment_profile, inference_client


def deployment(tmp_path):
    return {'id':'claude-cli','runner':'native-http',
        'native_counter_protocol':'utf8-envelope-estimate-v1',
        'native_cli':{'executable':str(tmp_path/'claude.exe'),'version':'2.1.278'},
        'native_profile':{'protocol':'vendor-cli','model':'fixture','context_window':100000,
                          'output_tokens':4096,'source':'fixture','endpoint':'cli://claude'}}


@pytest.mark.parametrize('fault',['credential','counter','stream','relative','version','usage'])
def test_explicit_cli_boundary_rejects_incompatible_configuration(tmp_path,fault):
    dep=deployment(tmp_path)
    if fault=='credential':dep['native_credential_env']='API_KEY'
    if fault=='counter':dep['native_counter_endpoint']='http://elsewhere'
    if fault=='stream':dep['native_streaming']=True
    if fault=='relative':dep['native_cli']['executable']='claude.exe'
    if fault=='version':dep['native_cli']['version']=''
    if fault=='usage':dep['native_usage_contract']={'layout':'chat-inclusive','source':'wrong'}
    with pytest.raises(ValueError):deployment_profile(dep,'fixture')


def test_factory_never_resolves_api_credentials(tmp_path):
    dep=deployment(tmp_path);profile=deployment_profile(dep,'fixture')
    client=inference_client(dep,profile,credential=lambda:pytest.fail('API credential fallback'))
    assert client.endpoint=='cli://claude' and client.version=='2.1.278'


@pytest.mark.parametrize('endpoint,version,layout,accepted', [
    ('cli://claude', '2.1.280', 'claude-cli-exclusive', True),
    ('cli://claude', '2.1.278', 'claude-cli-exclusive', False),
    ('cli://agy', '1.2.7', 'claude-cli-exclusive', False),
    ('cli://claude', '2.1.280', 'chat-inclusive', False),
    ('http://fixture', '2.1.280', 'claude-cli-exclusive', False),
])
def test_claude_usage_contract_is_explicit_and_transport_qualified(tmp_path, endpoint, version, layout, accepted):
    dep = deployment(tmp_path)
    dep['native_cli']['version'] = version
    dep['native_profile']['endpoint'] = endpoint
    dep['native_usage_contract'] = {'layout': layout, 'source': 'qualified fixture'}
    if endpoint.startswith('http:'):
        dep.pop('native_cli')
        dep['native_profile']['protocol'] = 'openai-chat'
    if accepted:
        assert deployment_profile(dep, 'fixture').protocol == 'vendor-cli'
    else:
        with pytest.raises(ValueError):
            deployment_profile(dep, 'fixture')


from test_work_resume import host
from test_work_run import os_root


def test_work_host_routes_cli_through_shared_factory_and_keeps_authority(host):
    from relay.native_work_host import create_runner, finish_runner
    from compute_plane.native_cli import VendorCliClient
    dep=deployment(host[0]);dep['model']='qwen-3.8-27b'
    runner=create_runner(dep,'work-one',host[0]/'cli-work',unlimited=True)
    try:
        assert isinstance(runner.settings['client'],VendorCliClient)
        assert runner.settings['control'].binding.work_id=='work-one'
        assert runner.settings['profile'].protocol=='vendor-cli'
    finally:
        finish_runner(runner)
