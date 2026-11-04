"""Bounded information-loss experiment, not a second FLOW recovery matrix."""
import asyncio
from copy import deepcopy
import hashlib

import pytest
from textual.widgets import Select, TextArea

from relay import console_demand, console_recovery, native_agent, relay_mesh
from relay.native_console import WorkConsole
from test_console_demand import Backend
from test_console_recovery import IDENTITY, request
from test_native_agent import Client, deployment, setup, steps


def owner_bytes(custody):
    return {str(p.relative_to(custody)): p.read_bytes()
            for p in custody.rglob('*') if p.is_file()}


@pytest.mark.parametrize('loss', ['presentation', 'missing_outcome', 'corrupt_outcome', 'other_invocation'])
def test_minimum_completed_session_seed_survives_only_projection_loss(setup, monkeypatch, loss):
    config, custody, root = setup
    # Real runtime + Kernel edit + verifier, deterministic provider; no copied repository.
    client = Client(steps=steps())
    result = native_agent.run_session(config, deployment(), custody,
        'Change value to 2 and verify it.', client=client, local_counter=client, invocation_id=IDENTITY)
    assert result['status'] == 'PROPOSED', result
    assert (root/'a.py').read_bytes() == b'value = 2\n'
    assert client.generations == 4
    control = root.parent/'control'
    control.mkdir()
    monkeypatch.setattr(relay_mesh, 'RELAY_ROOT', control)
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(root.parent/'state'))
    seed = request(root, custody)
    seed['snapshot']['selection']['prompt'] = 'Unsent follow-up'
    saved = console_demand.save(None, 0, root, control, 'execution_requested',
        seed['snapshot'], seed['events'][0]['detail'])
    before = console_recovery.observe(saved)
    assert before['state'] == 'RECOVERED' and before['result'] == result
    binding = deepcopy(saved['events'][0])
    identity_revision = (saved['id'], saved['revision'])
    receipts = {r['path']: r['sha256'] for r in before['receipts']}
    assert receipts == {str(custody/name): hashlib.sha256((custody/name).read_bytes()).hexdigest()
                        for name in ('native-agent.json', 'outcome.json')}

    # Only fixture custody is damaged. A plausible summary/commit cannot replace a source.
    if loss == 'missing_outcome':
        (custody/'outcome.json').unlink()
    elif loss == 'corrupt_outcome':
        (custody/'outcome.json').write_bytes(b'{')
    elif loss == 'other_invocation':
        saved['events'][0]['detail']['invocation_id'] = 'b'*32
    if loss != 'presentation':
        saved['snapshot']['last_result'] = {'status': 'DONE', 'output': result.get('output'),
                                           'candidate_commit': 'f'*40}
        observed_binding = deepcopy(saved['events'][0])
    else:
        observed_binding = binding

    durable = owner_bytes(custody)
    effect = (root/'a.py').read_bytes()
    calls, generations = len(client.calls), client.generations

    def forbidden(*args, **kwargs):
        pytest.fail('Reconstruction attempted execution/provider access')

    monkeypatch.setattr(client, 'post', forbidden)
    monkeypatch.setattr(native_agent, 'run_session', forbidden)
    after = console_recovery.observe(saved)
    assert saved['events'][0] == observed_binding
    assert (saved['id'], saved['revision']) == identity_revision
    if loss != 'presentation':
        assert after['state'] == 'UNKNOWN' and after['result'] is None
        assert console_recovery.pending(saved) is not None
    else:
        assert after == before  # Exact result, binding and byte-hash provenance.

        async def reopen():
            backend = Backend()
            app = WorkConsole(root, backend=backend)
            async with app.run_test(size=(140, 52)) as pilot:
                await pilot.pause()
                await app.list_demands().wait()
                app.query_one('#saved-demand', Select).value = saved['id']
                app.open_demand()
                await app.workers.wait_for_complete()
                assert app.last_result == result
                assert app.query_one('#prompt', TextArea).text == 'Unsent follow-up'
                assert app.demand_id == saved['id']
                assert app.demand_revision == saved['revision'] + 1
                assert app.demand_state['events'][0] == binding
                event = app.demand_state['events'][-1]
                assert event['kind'] == 'session_reconciled'
                assert event['detail']['receipts'] == before['receipts']
                assert event['detail']['request_event'] == 0
                # Historical PROPOSED is neither a new approval nor demand DONE.
                assert app.reviewed_plan is None and app.reviewed_preparation is None
                assert app.reviewed_revision is None and app.reviewed_decision is None
                assert 'not Work settlement or a new approval' in console_demand.guidance(app.demand_state)
                revision = app.demand_revision
                await app.reconcile_demand().wait()
                assert app.demand_revision == revision
                assert not backend.calls and app.process is None
                reread = console_demand.read(saved['id'], root, control)
                assert reread['events'] == app.demand_state['events']
        asyncio.run(reopen())

    assert len(client.calls) == calls and client.generations == generations
    assert (root/'a.py').read_bytes() == effect
    assert owner_bytes(custody) == durable  # Includes journal: no repeated effect or owner rewrite.
