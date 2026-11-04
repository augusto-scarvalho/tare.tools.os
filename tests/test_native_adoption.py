"""Public defaults exercise native ports; no Aider realization may be imported."""
import hashlib
import json
import sys
from pathlib import Path

import pytest

from relay import agent, native_agent, native_work_host, work_run, relay_mesh
from test_work_run import os_root, NativeFixtureClient, evidence
from test_native_agent import setup, Client, deployment


@pytest.mark.parametrize('name,model', [('bedrock-sonnet','claude-sonnet-4.6'), ('bedrock-glm','glm-5')])
def test_restored_bedrock_catalog_role_and_roster_share_exact_native_profile(name, model):
    from relay.native_catalog import select
    root=Path(__file__).resolve().parents[1]/'relay'
    dep=select(name)
    roles=json.loads((root/'role_deployments.json').read_text())['deployments']
    role=next(r for r in roles if r['id']==name+'-explorer')
    assert role['native_profile']==dep['native_profile'] and role['runner']=='native-http'
    assert set(role['roles'])=={'explorer','implementer'} and role['kind']=='reserve'
    roster=json.loads((root/'round_table_roster.json').read_text())
    assert roster['models'][model]['runtime']['native']==dep['native_profile']['model']
    assert role.get('native_counter_protocol')==dep.get('native_counter_protocol')
    assert dep['model']==model


@pytest.mark.parametrize('spend_limit', [False, True])
def test_default_work_uses_native_factory_and_exact_settlement(os_root, monkeypatch, spend_limit):
    from compute_plane import native_models
    _, repo = os_root
    profile = deployment('bedrock-converse')
    profile['model'] = 'qwen-3.8-27b'
    if spend_limit:
        from dataclasses import asdict
        from test_native_spend import tariff
        profile['native_usage_tariff']=asdict(tariff())
        profile['native_spend_policy']={'max_amount':'0','currency':'USD'}
    roster = relay_mesh.RELAY_DIR/'round_table_roster.json'
    value = json.loads(roster.read_bytes())
    value['models']['qwen-3.8-27b']['runtime']['native'] = 'fixture'
    roster.write_text(json.dumps(value))
    client = NativeFixtureClient()
    class HttpFixture:
        def __new__(cls, *a, **kw):
            return client
    monkeypatch.setattr(native_models, 'NativeHttpClient', HttpFixture)
    result = work_run.run_work('work-one', repository_root=repo, native_deployment=profile,
        plan_auditor=lambda *a: {'verdict': 'ACCEPT', 'findings': []}, evidence_factory=evidence)
    if spend_limit:
        assert result['status'] != 'SETTLED' and not client.generations
        return
    assert result['status'] == 'SETTLED', result
    assert result['execution'] == 'native' and len(client.generations) == 5
    assert (repo/'src/value.txt').read_text() == 'strict\n'


def test_native_plan_audit_reads_packet_and_descriptor(setup, tmp_path, monkeypatch):
    from compute_plane import native_models
    # Work projection is qualified by test_work_plan_context; this test isolates native review reads.
    monkeypatch.setattr(native_work_host, 'plan_context', lambda *args, repository_root: 'PLAN_CONTEXT.json')
    _, _, repo = setup
    train = tmp_path/'train'; train.mkdir()
    for name in ('PACKET.md', 'EXECUTION_DESCRIPTOR.json'):
        (train/name).write_text('bounded fixture')
    refs = [{'repository': 'train', 'path': name, 'sha256': hashlib.sha256((train/name).read_bytes()).hexdigest()}
            for name in ('PACKET.md', 'EXECUTION_DESCRIPTOR.json')]
    review = {'summary': 'The bounded fixture is coherent.', 'sources': refs, 'remaining': [],
              'verdict': 'ACCEPT', 'findings': []}
    client = Client(steps=[*[('read', {k:s[k] for k in ('repository','path')}) for s in refs],
                           ('tare_deliver', review)])
    class HttpFixture:
        def __new__(cls, *a, **kw):
            return client
    monkeypatch.setattr(native_models, 'NativeHttpClient', HttpFixture)
    result = native_work_host.plan_audit(deployment(), train, 'repo', repo, tmp_path/'audit')
    assert result == review and client.generations == 3


def test_legacy_resume_is_explicitly_refused_without_import(tmp_path, monkeypatch, capsys):
    (tmp_path/'session.json').write_text('{}')
    monkeypatch.setattr(native_agent, 'main', lambda *a: (_ for _ in ()).throw(AssertionError('wrong host')))
    assert agent.main(['--session', str(tmp_path), '--resume']) == 2
    assert 'LEGACY_SESSION_REQUIRES_ARCHIVED_HOST' in capsys.readouterr().err
