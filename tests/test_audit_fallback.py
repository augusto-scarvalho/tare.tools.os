"""Round Table transport reuse without model changes or credential artifacts."""
import json
import os
import sys
from uuid import uuid4

import pytest

from relay.audit_fallback import openrouter_driver
from relay import round_table_engine


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY', 'fixture-gateway-token')
    driver = openrouter_driver([sys.executable], 'claude-fable-5-1')
    assert driver.gateway['deployment_id'] == 'chair-anthropic-openrouter'
    return driver


def test_fallback_pins_same_model_and_keeps_restricted_tools(gateway, tmp_path):
    command = gateway.build_packet_command(model='claude-fable-5-1', session=str(uuid4()),
        resume=True, schema={'type': 'object'}, effort='xhigh')
    assert command[command.index('--model') + 1] == 'anthropic/claude-fable-5.1'
    assert '--resume' in command and '--strict-mcp-config' in command
    assert command[command.index('--tools') + 1] == ''
    assert 'fixture-gateway-token' not in json.dumps(command) + json.dumps(gateway.gateway)
    with pytest.raises(ValueError, match='gateway_model_mismatch'):
        gateway.build_packet_command(model='claude-fable-5', session=str(uuid4()),
            resume=False, schema={}, effort='high')


def test_gateway_auth_is_child_only_and_overrides_subscription(gateway, tmp_path, monkeypatch):
    monkeypatch.setenv('CLAUDE_CODE_OAUTH_TOKEN', 'fixture-subscription')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'fixture-direct-api')
    source = tmp_path / 'probe.py'
    source.write_text("import os,json\nprint(json.dumps({"
        "'gateway':os.environ.get('ANTHROPIC_BASE_URL'),"
        "'token_ok':os.environ.get('ANTHROPIC_AUTH_TOKEN')=='fixture-gateway-token',"
        "'api_empty':os.environ.get('ANTHROPIC_API_KEY')=='',"
        "'oauth_absent':'CLAUDE_CODE_OAUTH_TOKEN' not in os.environ}))", encoding='utf-8')
    code, raw = gateway.run([sys.executable, str(source)], root=tmp_path, prompt=b'', timeout=5)
    assert code == 0
    assert json.loads(raw) == {'gateway': 'https://openrouter.ai/api', 'token_ok': True,
                               'api_empty': True, 'oauth_absent': True}
    assert os.environ['CLAUDE_CODE_OAUTH_TOKEN'] == 'fixture-subscription'
    assert os.environ['ANTHROPIC_API_KEY'] == 'fixture-direct-api'


def test_unknown_auditor_cannot_silently_change_model():
    with pytest.raises(ValueError, match='not_qualified'):
        openrouter_driver([sys.executable], 'unknown-model')


def test_glm_uses_its_roster_deployment_and_requires_matching_gateway(monkeypatch):
    from relay.audit_fallback import supports_packet_identity
    monkeypatch.setenv('OPENROUTER_API_KEY', 'fixture-token')
    driver = openrouter_driver([sys.executable], 'openrouter/z-ai/glm-5.3')
    identity = {'runtime_key': 'aider-openrouter', 'runtime_model': 'openrouter/z-ai/glm-5.3'}
    assert driver.gateway['deployment_id'] == 'chair-glm-openrouter'
    assert supports_packet_identity(driver, identity)
    assert driver.wire_model(identity['runtime_model']) == 'z-ai/glm-5.3'
    assert not supports_packet_identity(object(), identity)
    driver.gateway['wire_model'] = 'anthropic/claude-fable-5'
    assert not supports_packet_identity(driver, identity)
