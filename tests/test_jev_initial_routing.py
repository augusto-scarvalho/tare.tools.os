"""Offline initial routing: fake hosted transport, profiles and GPU observations only."""
import io
import json

import pytest

from relay import turn_engine as engine


@pytest.fixture
def routing(monkeypatch, tmp_path):
    import compute_plane.jev_choice_worker as worker
    monkeypatch.setenv('TARE_STORAGE_ROOT', str(tmp_path))
    monkeypatch.delenv('TARE_JEV_SHADOW', raising=False)
    monkeypatch.setattr('relay.environment.decides', lambda name: True)
    monkeypatch.setattr(engine, 'jev_key', lambda: 'fixture-key')
    monkeypatch.setattr(engine, 'local_judge', lambda: pytest.fail('no local classifier'))
    tiers = [['tiny', 'tiny-backup'], ['large'], ['specialist']]
    profiles = {name: {'id': name, 'native_profile': {
        'protocol': 'openai-chat', 'model': name, 'context_window': 32768,
        'output_tokens': 4096, 'source': 'Configured model card: ' + name,
        'endpoint': 'https://secret.invalid', 'parameters_json': 'PRIVATE'}}
        for tier in tiers for name in tier}
    down, calls = {}, []
    monkeypatch.setattr(engine, 'policy', lambda: {'implementer': tiers})
    monkeypatch.setattr(engine, 'available', lambda name: (None, down[name]) if name in down else (profiles[name], None))

    def assess(request, *, key, timeout, model):
        assert model == engine.HOSTED_JEV_MODEL
        assert key == 'fixture-key' and timeout <= 3
        calls.append(request)
        choice = 'tier_1' if 'complex' in request['state'] else next(iter(request['options']))
        return {'choice': choice, 'probabilities': {k: float(k == choice) for k in request['options']},
                'model_identity': {'model': engine.HOSTED_JEV_MODEL}, 'usage': {'input_tokens': 123}}
    monkeypatch.setattr(worker, 'assess', assess)
    return tiers, profiles, down, calls, worker, assess


@pytest.mark.parametrize('text,expected', [('simple typo', 'tiny'), ('complex refactor', 'large')])
def test_simple_and_complex_are_one_bounded_hosted_choice(routing, text, expected):
    tiers, profiles, down, calls, worker, assess = routing
    tier, name, chosen, skipped = engine.initial_choice(tiers, text)
    assert name == expected and len(calls) == 1
    assert calls[0]['state'] == text
    assert 'Configured model card' in calls[0]['options']['tier_0']
    assert 'PRIVATE' not in json.dumps(calls) and 'secret.invalid' not in json.dumps(calls)
    assert chosen['native_profile'] is profiles[expected]['native_profile']
    decision = chosen['native_auto_selection']['decision']
    assert decision['source'] == 'hosted_jev' and decision['selected_tier'] == tier
    assert decision['jev_model'] == engine.HOSTED_JEV_MODEL and decision['input_tokens'] == 123
    assert text not in json.dumps(decision) and 'fixture-key' not in json.dumps(decision)


@pytest.mark.parametrize('mode', ['disabled', 'missing', 'failed', 'timeout', 'abstain', 'incomplete',
                                  'nan', 'negative', 'sum', 'unknown', 'boolean'])
def test_unusable_advice_keeps_deterministic_policy(routing, monkeypatch, mode):
    tiers, profiles, down, calls, worker, assess = routing
    if mode == 'disabled':
        monkeypatch.setattr('relay.environment.decides', lambda name: False)
    elif mode == 'missing':
        monkeypatch.setattr(engine, 'jev_key', lambda: '')
    else:
        def bad(request, **kw):
            answer = assess(request, **kw)
            if mode == 'failed':
                raise RuntimeError('secret transport failure')
            if mode == 'timeout':
                raise TimeoutError()
            if mode == 'abstain':
                answer['choice'] = None
            elif mode == 'unknown':
                answer['choice'] = 'frontier-not-configured'
            else:
                probabilities = answer['probabilities']
                if mode == 'incomplete':
                    probabilities.pop('tier_2')
                else:
                    probabilities['tier_0'] = {'nan': float('nan'), 'negative': -1,
                                               'sum': .5, 'boolean': True}[mode]
            return answer
        monkeypatch.setattr(worker, 'assess', bad)
    chosen = engine.initial_choice(tiers, 'simple task')[2]
    assert chosen['id'] == 'tiny'
    assert chosen['native_auto_selection']['decision']['source'] == 'policy'
    assert len(calls) == (0 if mode in {'disabled', 'missing'} else 1)
    assert 'secret transport' not in json.dumps(chosen)


def test_all_judges_off_and_oversized_utf8_never_send_or_truncate(routing, monkeypatch):
    tiers, profiles, down, calls, worker, assess = routing
    monkeypatch.setenv('TARE_JEV_SHADOW', 'off')
    assert engine.initial_choice(tiers, 'complex task')[1] == 'tiny'
    monkeypatch.delenv('TARE_JEV_SHADOW')
    text = 'é' * 3100 + ' preserve this final requirement'
    chosen = engine.initial_choice(tiers, text)[2]
    assert chosen['native_auto_selection']['decision']['reason'] == 'input_bound'
    assert calls == []


def test_no_eligible_route_does_not_ask_jev(routing):
    tiers, profiles, down, calls, worker, assess = routing
    down.update({name: 'quota or policy blocked' for name in profiles})
    assert engine.initial_choice(tiers, 'complex task')[2] is None
    with pytest.raises(engine.UnschedulableError):
        engine.resolve_auto({'native_auto_routes': 'implementer'}, request='task')
    assert calls == []


def test_custom_tiers_and_route_order_are_not_quality_scores(routing):
    tiers, profiles, down, calls, worker, assess = routing
    custom = [['specialist'], ['tiny-backup', 'tiny'], ['large']]
    chosen = engine.initial_choice(custom, 'complex task')[2]
    assert chosen['id'] == 'tiny-backup'
    assert 'specialist' in calls[0]['options']['tier_0']
    assert 'large' in calls[0]['options']['tier_2']


@pytest.mark.parametrize('reason', ['local GPU busy (an image job holds or waits for it)',
                                    'local GPU reserved for foreign job', 'credential missing',
                                    'quota exhausted', 'machine policy', 'no local models in this chat'])
def test_ineligible_routes_are_not_options(routing, reason):
    tiers, profiles, down, calls, worker, assess = routing
    down.update(tiny=reason, **{'tiny-backup': reason})
    chosen = engine.initial_choice(tiers, 'simple task')[2]
    assert chosen['id'] == 'large' and 'tier_0' not in calls[0]['options']
    assert len(chosen['native_auto_selection']['decision']['exclusions']) == 2


def test_gpu_lease_acquired_during_assessment_is_revalidated(routing, monkeypatch):
    tiers, profiles, down, calls, worker, assess = routing
    def race(request, **kw):
        answer = assess(request, **kw)
        down.update({'tiny': engine.GPU_BUSY, 'tiny-backup': engine.GPU_BUSY})
        return answer
    monkeypatch.setattr(worker, 'assess', race)
    chosen = engine.initial_choice(tiers, 'simple task')[2]
    assert chosen['id'] == 'large' and len(calls) == 1
    assert chosen['native_auto_selection']['decision']['selected_tier'] == 1
    assert len(chosen['native_auto_selection']['decision']['exclusions']) == 2


@pytest.mark.parametrize('lease', [
    {'held': True, 'owner': {'kind': 'image'}},
    {'held': False, 'queue': [{'kind': 'heavy'}]},
    {'held': True, 'owner': {'kind': 'training'}},
    {'reservation': {'id': 'foreign', 'name': 'bench', 'holder': 'other'}},
])
def test_resident_model_does_not_grant_gpu(monkeypatch, lease):
    from urllib import request
    monkeypatch.delenv('TARE_GPU_RESERVATION_ID', raising=False)
    monkeypatch.setattr('relay.challenger.local', lambda d: True)
    status = {'current_model': 'tiny', 'gpu_coordination': lease}
    monkeypatch.setattr(request, 'urlopen', lambda *a, **kw: io.BytesIO(json.dumps(status).encode()))
    why = engine.local_gpu_busy({'native_profile': {'endpoint': 'http://fixture.test/v1'}})
    assert why is not None
    assert why.diagnostic['code'] in {'GPU_BUSY', 'GPU_RESERVED'}


@pytest.mark.parametrize('blocked', ['environment', 'quota', 'credential', 'local_opt_out', 'lease'])
def test_real_availability_filters_before_any_assessment(monkeypatch, blocked):
    from relay import native_catalog, native_generation
    profile = {'id': 'fixture', 'native_profile': {'protocol': 'openai-chat', 'model': 'fixture',
                                                 'endpoint': 'http://fixture.test/v1'}}
    if blocked == 'credential':
        profile['native_credential_env'] = 'FIXTURE_MISSING_KEY'
    monkeypatch.delenv('FIXTURE_MISSING_KEY', raising=False)
    monkeypatch.setattr(native_catalog, 'select', lambda name: profile)
    monkeypatch.setattr('relay.environment.allows', lambda d: (blocked != 'environment', 'fixture opt-out'))
    monkeypatch.setattr(engine, 'usage_limited', lambda d: 1e12 if blocked == 'quota' else None)
    monkeypatch.setattr(native_generation, 'vault_secret', lambda name: None)
    monkeypatch.setattr('relay.challenger.local', lambda d: True)
    monkeypatch.setattr(engine, 'avoid_local_gpu', lambda: blocked == 'local_opt_out')
    monkeypatch.setattr(engine, 'local_gpu_busy', lambda d: engine.Unavailable(engine.GPU_BUSY, 'GPU_BUSY')
                        if blocked == 'lease' else None)
    monkeypatch.setattr(engine, 'jev_key', lambda: pytest.fail('no eligible option must cost nothing'))
    assert engine.initial_choice([['fixture']], 'full task')[2] is None


def test_successor_ladder_never_makes_an_initial_assessment(routing, monkeypatch):
    tiers, profiles, down, calls, worker, assess = routing
    saved = engine.resolve_auto({'native_auto_routes': 'implementer'}, request='simple task')
    assert len(calls) == 1
    monkeypatch.setattr(engine, 'initial_choice', lambda *a, **kw: pytest.fail('successor reclassified'))
    successor = engine.next_auto(saved)
    assert successor['id'] == 'large'
    assert successor['native_profile'] is profiles['large']['native_profile']
    assert len(calls) == 1


@pytest.mark.parametrize('until,expected', [(1050, 1050), (999, None), (1e12, None),
                                          (float('inf'), None), (float('-inf'), None),
                                          (float('nan'), None)])
def test_persisted_quota_timestamps_are_finite_and_bounded(monkeypatch, tmp_path, until, expected):
    path = tmp_path/'usage-limits.json'
    path.write_text(json.dumps({'codex': {'until': until}}), encoding='utf-8')
    monkeypatch.setattr(engine, 'usage_limits_path', lambda: path)
    deployment = {'native_profile': {'protocol': 'openai-codex-responses'}}
    assert engine.usage_limited(deployment, now=1000) == expected
    if expected is None:
        # Advancing time must not manufacture another grace period for invalid metadata.
        assert engine.usage_limited(deployment, now=1000+engine.USAGE_LIMIT_DEFAULT_SECONDS+1) is None


@pytest.mark.parametrize('until', [1e12, float('inf'), float('-inf'), float('nan')])
def test_extreme_quota_observation_is_a_bounded_structured_exclusion(monkeypatch, until):
    from relay import native_catalog
    deployment = {'native_profile': {'protocol': 'openai-codex-responses'}}
    monkeypatch.setattr(native_catalog, 'select', lambda name: deployment)
    monkeypatch.setattr('relay.environment.allows', lambda d: (True, None))
    monkeypatch.setattr(engine, 'usage_limited', lambda d: until)
    chosen, why = engine.available('fixture')
    assert chosen is None and why.diagnostic['code'] == 'QUOTA_EXHAUSTED'
    assert 0 <= why.diagnostic['retry_after_seconds'] <= 86400


def test_last_suggested_tier_disappears_but_earlier_route_still_executes(routing, monkeypatch):
    tiers, profiles, down, calls, worker, assess = routing
    def race(request, **kw):
        calls.append(request)
        down['specialist'] = 'lease acquired during assessment'
        return {'choice': 'tier_2', 'probabilities': {'tier_0': 0, 'tier_1': 0, 'tier_2': 1},
                'model_identity': {'model': engine.HOSTED_JEV_MODEL}, 'usage': {'input_tokens': 123}}
    monkeypatch.setattr(worker, 'assess', race)
    chosen = engine.initial_choice(tiers, 'full task; preserve final requirement')[2]
    assert chosen['id'] == 'tiny' and len(calls) == 1
    assert calls[0]['state'] == 'full task; preserve final requirement'
    decision = chosen['native_auto_selection']['decision']
    assert decision['source'] == 'policy' and 'suggested_ladder_unavailable' in decision['reason']
    assert decision['selected_tier'] == 0


def test_single_eligible_tier_costs_no_assessment(routing):
    tiers, profiles, down, calls, worker, assess = routing
    down.update({'large': 'quota', 'specialist': 'policy'})
    chosen = engine.initial_choice(tiers, 'full task intact')[2]
    assert chosen['id'] == 'tiny' and calls == []
    assert chosen['native_auto_selection']['decision']['reason'] == 'single_eligible_tier'
