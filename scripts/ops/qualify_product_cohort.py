"""Bounded, read-only cohort observation. See PRODUCT_INSTALLATION.md for packet v1."""
import argparse
import json
import re
import sys

MAX_BYTES = 262144
MAX_NODES = 16
MAX_PACKAGES = 32
SCHEMA = 'tare.tools/product-cohort/1'


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= 128 and value == value.strip()


def _hex(value, size):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{%d}' % size, value) is not None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_json_key')
        result[key] = value
    return result


def _result(nodes, reasons=()):
    passed = sum(row['observation'] == 'PASS' for row in nodes)
    overall = 'PASS' if nodes and passed == len(nodes) and not reasons else ('PARTIAL' if passed else 'REJECTED')
    return {'schema': SCHEMA, 'authority': 'NONE', 'observation': overall,
            'reasons': list(reasons), 'nodes': nodes}


def _node(row, sources):
    reasons = []
    status, transition = row.get('status'), row.get('transition_status')
    installation = row.get('installation')
    if not _hex(installation, 64):
        reasons.append('installation_invalid')
    if not isinstance(status, dict) or not isinstance(transition, dict):
        return ['evidence_missing_or_malformed']
    current, previous = status.get('current'), status.get('previous')
    if not _hex(current, 64) or not _hex(previous, 64) or current == previous:
        reasons.append('status_pair_invalid')
    packages = status.get('packages')
    found = {}
    valid = isinstance(packages, list) and 0 < len(packages) <= MAX_PACKAGES
    if valid:
        for package in packages:
            if not isinstance(package, dict):
                valid = False
                break
            name, commit = package.get('name'), package.get('commit')
            if not _text(name) or not _hex(commit, 40) or name.casefold() in found:
                valid = False
                break
            found[name.casefold()] = (name, commit)
    if not valid or {name: commit for name, commit in found.values()} != sources:
        reasons.append('source_inputs_mismatch')
    receipt = status.get('last_transition')
    trial = receipt.get('catalog_trial') if isinstance(receipt, dict) else None
    if (not isinstance(receipt, dict) or receipt.get('operation') != 'update'
            or receipt.get('from') != previous or receipt.get('to') != current
            or receipt.get('qualification') != 'releases/'+str(current)[:16]+'/ready.json'
            or not isinstance(trial, dict) or not _hex(trial.get('sha256'), 64)
            or trial.get('path') != 'releases/'+str(current)[:16]+'/catalog-trial.json'):
        reasons.append('receipt_mismatch')
    if transition.get('schema') != 'tare.tools/transition-status/1':
        reasons.append('transition_schema_invalid')
    if (transition.get('completed') is not True
            or transition.get('reasons') != ['qualified_rotation_complete']
            or transition.get('serve') != 'selected'):
        reasons.append('rotation_not_qualified')
    if transition.get('current') != current or transition.get('previous') != previous:
        reasons.append('transition_pair_mismatch')
    server = transition.get('server')
    if (not isinstance(server, dict) or server.get('installation') != installation
            or server.get('release') != current or type(server.get('pid')) is not int
            or server.get('pid', 0) <= 0):
        reasons.append('server_identity_mismatch')
    return reasons


def qualify(packet):
    """Return observation only from a dict or UTF-8 JSON bytes/string; never perform effects."""
    try:
        if isinstance(packet, dict):
            packet = json.dumps(packet, allow_nan=False)
        if isinstance(packet, str):
            packet = packet.encode('utf-8')
        if not isinstance(packet, bytes) or len(packet) > MAX_BYTES:
            raise ValueError('packet_size_or_type')
        packet = json.loads(packet, object_pairs_hook=_unique,
                            parse_constant=lambda value: (_ for _ in ()).throw(ValueError('constant')))
        if not isinstance(packet, dict) or packet.get('schema') != SCHEMA:
            raise ValueError('packet_schema')
        nodes, sources, rows = (packet.get(key) for key in ('expected_nodes', 'expected_sources', 'observations'))
        if (not isinstance(nodes, list) or not 1 <= len(nodes) <= MAX_NODES
                or not all(_text(n) for n in nodes)
                or len({n.casefold() for n in nodes}) != len(nodes)):
            raise ValueError('expected_nodes_invalid')
        if (not isinstance(sources, dict) or not 1 <= len(sources) <= MAX_PACKAGES
                or not all(_text(k) and _hex(v, 40) for k, v in sources.items())
                or len({k.casefold() for k in sources}) != len(sources)):
            raise ValueError('expected_sources_invalid')
        if not isinstance(rows, list) or len(rows) > MAX_NODES:
            raise ValueError('observations_invalid')
        grouped = {n.casefold(): [] for n in nodes}
        packet_reasons = []
        for row in rows:
            if not isinstance(row, dict) or not _text(row.get('node')):
                packet_reasons = ['unexpected_or_malformed_observation']
            elif row['node'].casefold() not in grouped:
                packet_reasons = ['unexpected_or_malformed_observation']
            else:
                grouped[row['node'].casefold()].append(row)
        results = []
        for node in nodes:
            evidence = grouped[node.casefold()]
            reasons = (['observation_missing'] if not evidence else
                       ['duplicate_node_observation'] if len(evidence) != 1 else _node(evidence[0], sources))
            results.append({'node': node, 'installation': evidence[0].get('installation')
                            if len(evidence) == 1 and _hex(evidence[0].get('installation'), 64) else None,
                            'observation': 'REJECTED' if reasons else 'PASS',
                            'reasons': reasons or ['qualified_rotation_complete']})
        return _result(results, packet_reasons)
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        return _result([], ['packet_invalid'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('packet', nargs='?', default='-', help='JSON path, or - for stdin')
    args = parser.parse_args(argv)
    try:
        if args.packet == '-':
            raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        else:
            with open(args.packet, 'rb') as stream:
                raw = stream.read(MAX_BYTES + 1)
        result = qualify(raw)
    except OSError:
        result = _result([], ['packet_unavailable'])
    print(json.dumps(result, sort_keys=True))
    return {'PASS': 0, 'PARTIAL': 1, 'REJECTED': 2}[result['observation']]


if __name__ == '__main__':
    sys.exit(main())
