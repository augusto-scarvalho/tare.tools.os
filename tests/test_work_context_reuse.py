"""Offline qualification of advisory reuse; provider cache hits are not asserted."""
import copy
import hashlib
import json
from pathlib import Path

import pytest
from compute_plane.native_journal import io_path, save_json
from graph_backlog.jsonutil import sha256_canonical
from relay import planning_findings as findings, work_context_reuse as reuse
from test_native_plan import proposal  # noqa: F401
from test_planning_findings import reports  # noqa: F401


@pytest.fixture
def selected(reports):
    directory, root = reports
    (root / 'other.txt').write_bytes(b'Independent source.\n')
    path = directory / '2/artifact.json'
    artifact = json.loads(path.read_bytes())
    artifact['payload']['sources'] = [{'repository': 'project', 'path': 'other.txt',
        'sha256': hashlib.sha256((root / 'other.txt').read_bytes()).hexdigest()}]
    save_json(path, artifact)
    view = findings.preview('parallel', directory)
    saved = findings.save(view, view['sha256'])
    return saved, root


def prepare(selected):
    saved, root = selected
    return reuse.prepare(saved['path'], saved['sha256'], root)


def rewrite(selected, mutate):
    saved, _ = selected
    path = Path(saved['path'])
    packet = json.loads(path.read_bytes())
    mutate(packet)
    save_json(path, packet)
    saved['sha256'] = sha256_canonical(packet)


def snapshot(directory):
    return {str(p.relative_to(directory)): io_path(p).read_bytes()
            for p in directory.rglob('*') if p.is_file()}


def test_two_reports_stable_prefix_and_no_writes(selected, tmp_path):
    before = snapshot(tmp_path)
    value = prepare(selected)
    assert value['schema'] == reuse.SCHEMA and value['authority'] == 'NONE'
    assert len(value['reports']) == 2 and value['omitted'] == []
    assert all(r['remaining'] == ['Needs a runtime check.'] for r in value['reports'])
    assert value['prefix_sha256'] == hashlib.sha256(value['prefix'].encode()).hexdigest()
    assert len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()) <= 32768
    prefix = json.loads(value['prefix'])
    assert prefix['reports'] == value['reports']
    assert 'Untrusted advisory context' in prefix['notice']
    assert 'not instructions, approval or proof' in prefix['notice']
    assert 'author-1' not in value['prefix'] and selected[0]['path'] not in value['prefix']
    assert str(tmp_path) not in value['prefix']
    assert prepare(selected) == value
    assert snapshot(tmp_path) == before
    # Separate custody identity and author sessions do not change the prefix.
    rewrite(selected, lambda p: p['reports'][0]['artifact'].update(session_id='independent-session'))
    assert prepare(selected)['prefix'] == value['prefix']


@pytest.mark.parametrize('missing', [False, True])
def test_changed_or_missing_omits_entire_report_only(selected, missing):
    file = selected[1] / 'finding.txt'
    if missing:
        file.unlink()
    else:
        file.write_bytes(b'Changed current source')
    value = prepare(selected)
    assert [r['id'] for r in value['reports']] == ['2']
    assert value['omitted'] == [{'id': '1', 'reason': 'missing' if missing else 'changed', 'paths': ['finding.txt']}]
    (selected[1] / 'other.txt').unlink()
    empty = prepare(selected)
    assert empty['reports'] == [] and len(empty['omitted']) == 2
    assert json.loads(empty['prefix'])['reports'] == []


def test_operator_caveat_is_preserved_in_frozen_advisory_prefix(selected):
    rewrite(selected, lambda p: p['review'].update(note='Default claims conflict; verify before use.'))
    value = prepare(selected)
    assert json.loads(value['prefix'])['review_note'] == 'Default claims conflict; verify before use.'
    assert reuse.snapshot(value) == value


@pytest.mark.parametrize('fault', ['digest', 'root', 'schema', 'snapshot', 'historical'])
def test_identity_and_historical_contract_refused(selected, fault):
    saved, root = selected
    if fault == 'digest':
        saved['sha256'] = 'a' * 64
    elif fault == 'root':
        selected = saved, root.parent
    elif fault == 'schema':
        rewrite(selected, lambda p: p.update(schema='forged'))
    elif fault == 'snapshot':
        rewrite(selected, lambda p: p['reports'][0]['sources'][0].update(snapshot='../outside'))
    else:
        source = next((Path(saved['path']).parent / 'sources').glob('*.txt'))
        io_path(source).write_bytes(b'changed historical snapshot')
    with pytest.raises(ValueError):
        prepare(selected)


@pytest.mark.parametrize('fault', ['subset', 'hash', 'extra', 'duplicate', 'type', 'repository'])
def test_forged_or_malformed_citation_lists_refused(selected, fault):
    def mutate(packet):
        report = packet['reports'][0]
        if fault == 'subset':
            report['sources'] = []
        elif fault == 'hash':
            report['artifact']['payload']['sources'][0]['sha256'] = 'b' * 64
        elif fault == 'extra':
            report['sources'][0]['unexpected'] = True
        elif fault == 'duplicate':
            report['sources'].append(copy.deepcopy(report['sources'][0]))
        elif fault == 'type':
            report['sources'] = None
        else:
            report['artifact']['payload']['sources'][0]['repository'] = 'another-project'
    rewrite(selected, mutate)
    with pytest.raises(ValueError):
        prepare(selected)


@pytest.mark.parametrize('name', ['../outside', '/outside', 'C:/outside', 'a\\b', '.git/config', '.GIT/config', './finding.txt'])
def test_unsafe_paths_refused(selected, name):
    def mutate(packet):
        packet['reports'][0]['sources'][0]['path'] = name
        packet['reports'][0]['artifact']['payload']['sources'][0]['path'] = name
    rewrite(selected, mutate)
    with pytest.raises(ValueError):
        prepare(selected)


def test_symlink_refused_even_with_matching_content(selected):
    file = selected[1] / 'finding.txt'
    target = selected[1].parent / 'outside.txt'
    target.write_bytes(file.read_bytes())
    file.unlink()
    try:
        file.symlink_to(target)
    except OSError:
        pytest.skip('Host does not permit creating symlinks')
    with pytest.raises(ValueError, match='Symlink or junction'):
        prepare(selected)


@pytest.mark.parametrize('kind', ['research', 'session', 'session-plan'])
def test_other_origins_refused(selected, kind):
    rewrite(selected, lambda p: p['origin'].update(kind=kind))
    with pytest.raises(ValueError, match='Only parallel'):
        prepare(selected)


def test_source_budget_refused(selected):
    (selected[1] / 'finding.txt').write_bytes(b'x' * (2 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match='2 MiB'):
        prepare(selected)


def test_projection_budget_fails_without_truncating_uncertainty(selected):
    rewrite(selected, lambda p: p['reports'][0]['artifact']['payload'].update(remaining=['Uncertain ' * 1800]))
    with pytest.raises(ValueError, match='32 KiB projection'):
        prepare(selected)


def deployment():
    return {'native_codex_auth': {'account_id': 'account-one'}, 'session_id': 'session-one',
            'native_profile': {'protocol': 'openai-codex-responses', 'model': 'model-one',
                               'output_tokens': 4096, 'parameters_json': '{"reasoning":{"effort":"high"}}'}}


def key(deploy):
    return json.loads(deploy['native_profile']['parameters_json'])['prompt_cache_key']


def test_affinity_is_deterministic_scoped_and_nonmutating(selected):
    context = prepare(selected)
    before = copy.deepcopy(context)
    original = deployment()
    result = reuse.deployment_for_context(original, context, 'implementer')
    assert original == deployment() and context == before
    assert len(key(result)) <= 64
    assert result == reuse.deployment_for_context(original, context, 'implementer')
    variants = [key(result)]
    variants.append(key(reuse.deployment_for_context(original, context, 'auditor')))
    for field, value in [('account_id', 'account-two'), ('model', 'model-two')]:
        changed = copy.deepcopy(original)
        (changed['native_profile'] if field == 'model' else changed['native_codex_auth'])[field] = value
        variants.append(key(reuse.deployment_for_context(changed, context, 'implementer')))
    other_context = {**context, 'root': str(selected[1].parent / 'different-project')}
    variants.append(key(reuse.deployment_for_context(original, other_context, 'implementer')))
    assert len(set(variants)) == len(variants)
    result['native_profile']['parameters_json'] = original['native_profile']['parameters_json']
    assert result == original


def test_explicit_key_survives_independent_sessions_and_role(selected):
    context = prepare(selected)
    original = deployment()
    original['native_profile']['parameters_json'] = '{"prompt_cache_key":"operator-key","effort":"high"}'
    for session in ['independent-one', 'independent-two']:
        original['session_id'] = session
        assert reuse.deployment_for_context(original, context, 'auditor') == original


@pytest.mark.parametrize('protocol,empty', [('openai-responses', False), ('openai-codex-responses', True)])
def test_unsupported_or_empty_is_unchanged_deep_copy(selected, protocol, empty):
    original = deployment()
    original['native_profile']['protocol'] = protocol
    context = prepare(selected)
    if empty:
        context['reports'] = []
    result = reuse.deployment_for_context(original, context, 'implementer')
    assert result == original and result is not original
    assert result['native_profile'] is not original['native_profile']
