"""Offline cohort qualification tests; fixtures are not real node observations."""
import copy
import importlib.util
import json
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('cohort',
    Path(__file__).resolve().parents[1]/'scripts/ops/qualify_product_cohort.py')
cohort = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cohort)


def packet():
    rows = []
    for node, current in [('LAPTOP-01', 'b'*64), ('WORKSTATION-01', 'c'*64)]:
        previous = 'a'*64
        rows.append({'node': node, 'installation': '1'*64, 'status': {
            'current': current, 'previous': previous,
            'packages': [{'name': 'tare.tools.os', 'commit': 'd'*40, 'version': 'fixture'}],
            'last_transition': {'operation': 'update', 'from': previous, 'to': current,
                                'qualification': 'releases/'+current[:16]+'/ready.json',
                                'catalog_trial': {'path': 'releases/'+current[:16]+'/catalog-trial.json',
                                                  'sha256': '9'*64}}},
            'transition_status': {'schema': 'tare.tools/transition-status/1',
                'completed': True, 'reasons': ['qualified_rotation_complete'],
                'current': current, 'previous': previous, 'serve': 'selected',
                'server': {'installation': '1'*64, 'release': current,
                           'pid': 7}}})
    return {'schema': cohort.SCHEMA, 'expected_nodes': ['LAPTOP-01', 'WORKSTATION-01'],
            'expected_sources': {'tare.tools.os': 'd'*40}, 'observations': rows}


def test_equal_local_ids_and_different_bundles_are_valid():
    p = packet()
    before = copy.deepcopy(p)
    assert cohort.qualify(p)['observation'] == 'PASS'
    assert p == before


@pytest.mark.parametrize('fault', ['missing', 'unreachable', 'source', 'extra_source', 'duplicate_package',
    'schema', 'identity', 'release', 'pair', 'receipt', 'truthy', 'partial', 'duplicate_node',
    'missing_trial', 'trial_digest', 'trial_path', 'installation_shape'])
def test_bad_sibling_never_hides_good_node(fault):
    p = packet()
    row = p['observations'][1]
    t = row['transition_status']
    if fault == 'missing': p['observations'].pop()
    elif fault == 'unreachable': row.pop('status')
    elif fault == 'source': row['status']['packages'][0]['commit'] = 'e'*40
    elif fault == 'extra_source': row['status']['packages'].append({'name': 'extra', 'commit': 'e'*40})
    elif fault == 'duplicate_package': row['status']['packages'] *= 2
    elif fault == 'schema': t['schema'] = 'wrong'
    elif fault == 'identity': t['server']['installation'] = 'foreign'
    elif fault == 'installation_shape': row['installation'] = t['server']['installation'] = 'short'
    elif fault == 'release': t['server']['release'] = 'f'*64
    elif fault == 'pair': t['previous'] = 'f'*64
    elif fault == 'receipt': row['status']['last_transition']['operation'] = 'rollback'
    elif fault == 'missing_trial': row['status']['last_transition'].pop('catalog_trial')
    elif fault == 'trial_digest': row['status']['last_transition']['catalog_trial']['sha256'] = 'short'
    elif fault == 'trial_path': row['status']['last_transition']['catalog_trial']['path'] = '../../outside'
    elif fault == 'truthy': t['completed'] = 1
    elif fault == 'partial': t.pop('reasons')
    elif fault == 'duplicate_node': p['observations'].append({**row, 'node': 'workstation-01'})
    result = cohort.qualify(p)
    assert result['observation'] == 'PARTIAL'
    assert [n['observation'] for n in result['nodes']] == ['PASS', 'REJECTED']
    assert all(0 < len(n['reasons']) <= 10 for n in result['nodes'])


@pytest.mark.parametrize('raw', [b'{', b'[]', b'['*2000+b'0'+b']'*2000,
    b' '* (cohort.MAX_BYTES+1), b'{"schema":1,"schema":2}', b'\xff'],
    ids=['malformed', 'array', 'deep', 'oversized', 'duplicate-key', 'invalid-utf8'])
def test_bad_packet_rejected(raw):
    assert cohort.qualify(raw)['observation'] == 'REJECTED'


@pytest.mark.parametrize('fault', ['nodes', 'sources', 'limit', 'schema', 'no_success'])
def test_declarations_fail_closed(fault):
    p = packet()
    if fault == 'nodes': p['expected_nodes'].append('workstation-01')
    elif fault == 'sources': p['expected_sources']['tare.tools.os'] = 'short'
    elif fault == 'limit': p['expected_nodes'] = ['node'+str(i) for i in range(cohort.MAX_NODES+1)]
    elif fault == 'schema': p['schema'] = 'wrong'
    else: p['observations'] = []
    assert cohort.qualify(p)['observation'] == 'REJECTED'


def test_duplicate_json_package_key_rejected():
    raw = json.dumps(packet()).replace('"expected_sources": {',
        '"expected_sources": {"tare.tools.os":"'+ 'e'*40+'",')
    assert cohort.qualify(raw)['observation'] == 'REJECTED'


def test_cli_stdout_only(tmp_path, capsys):
    path = tmp_path/'packet.json'
    path.write_text(json.dumps(packet()), encoding='utf-8')
    before = path.read_bytes()
    assert cohort.main([str(path)]) == 0
    assert json.loads(capsys.readouterr().out)['authority'] == 'NONE'
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]
