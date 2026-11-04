"""Deterministic packet diagnostics and declared evidence checks; never execute code."""
from collections import Counter
import json
import sys

from relay.audit_cases import checksum, json_bytes, require
from relay.audit_context import symbols_for


def included_symbols(packet, source):
    raw = packet.get('source_archive', {}).get(source['id'], source['text'])
    symbols = symbols_for(raw, source['path'])
    if 'segments' not in source:
        return symbols
    lines = {n for segment in source['segments'] for n in range(
        segment['start_line'], segment['start_line'] + len(segment['text'].splitlines()))}
    return [s for s in symbols if set(range(s.start_line, s.end_line + 1)) <= lines]


def validate_map(packet):
    """Check declared sufficiency and test claims, not semantic completeness or execution."""
    view = packet['input']
    mapping = view['context']['review_map']
    require(isinstance(mapping, dict) and set(mapping) == {o['id'] for o in view['obligations']},
            'preflight_obligation_mapping_required')
    sources = {s['id']: s for s in view['sources'] if s['kind'] == 'source'}
    evidence = {e['id']: e for e in view['evidence']}
    targets = {r['id']: r['target'] for r in packet['repositories']}
    current_hashes = {r['id'] + ':' + s['path']: s['sha256'] for r in packet['repositories']
                      for s in r['sources'] if s['revision'] == r['target'] and s['sha256'] is not None}

    def source_for(ref, obligation):
        require(isinstance(ref, str) and ref in sources, 'preflight_source_missing')
        source = sources[ref]
        require(obligation in source['obligation_ids'], 'preflight_unlinked_source')
        require(source['revision'] == targets[source['repository']], 'preflight_historical_evidence')
        return source

    def symbol_for(item, obligation):
        require(isinstance(item, dict), 'preflight_symbol_mapping_required')
        source = source_for(item.get('source_ref'), obligation)
        require(isinstance(item.get('symbol'), str) and sum(
            s.name == item['symbol'] for s in included_symbols(packet, source)) == 1,
            'preflight_symbol_missing_or_ambiguous')
        return source

    for obligation, entry in mapping.items():
        require(isinstance(entry, dict) and set(entry) == {'contract_refs', 'dependencies', 'tests'},
                'preflight_mapping_shape')
        require(isinstance(entry['contract_refs'], list) and entry['contract_refs'], 'preflight_contract_required')
        for ref in entry['contract_refs']:
            source_for(ref, obligation)
        require(isinstance(entry['dependencies'], list), 'preflight_dependencies_required')
        for item in entry['dependencies']:
            symbol_for(item, obligation)
        require(isinstance(entry['tests'], list) and entry['tests'], 'preflight_tests_required')
        for item in entry['tests']:
            source = symbol_for(item, obligation)
            receipt = evidence.get(item.get('receipt_ref'))
            require(receipt is not None and obligation in receipt['obligation_ids'], 'preflight_receipt_missing')
            require(checksum(receipt['text'].encode('utf-8')) == receipt['sha256'], 'preflight_receipt_bytes_changed')
            claim = json.loads(receipt['text'])
            require(isinstance(claim, dict) and claim.get('executor') == 'implementer'
                    and type(claim.get('exit_code')) is int and claim['exit_code'] == 0
                    and isinstance(claim.get('command'), list) and claim['command']
                    and all(isinstance(a, str) and a for a in claim['command']), 'preflight_test_command_required')
            hashes = claim.get('source_sha256')
            require(isinstance(hashes, dict) and all(hashes.get(k) == v for k, v in current_hashes.items()),
                    'preflight_stale_test_sources')
            tests = claim.get('tests')
            require(isinstance(tests, dict), 'preflight_named_test_results_required')
            prefix = source['path'] + '::' + item['symbol'].replace('.', '::')
            nodeids = item.get('nodeids', [prefix])
            require(isinstance(nodeids, list) and nodeids and all(
                isinstance(n, str) and (n == prefix or n.startswith(prefix + '['))
                and tests.get(n) == 'passed' for n in nodeids), 'preflight_test_not_passed_or_mismatched')


def inspect(packet):
    """Explain bytes and missing context using the owner's static references only."""
    from relay.audit_packet import budget
    view = packet['input']
    sources = [s for s in view['sources'] if s['kind'] == 'source']
    current = {r['id']: r['target'] for r in packet['repositories']}
    available, selected = set(), []
    for source in sources:
        if source['revision'] != current[source['repository']]:
            continue
        module = source['path'].removesuffix('.py').replace('/', '.')
        symbols = included_symbols(packet, source)
        available.add(module)
        available.update(module + '.' + s.name for s in symbols)
        if any(source['id'] in change['context_refs'] for change in view['changes']):
            selected.extend((source['id'], s) for s in symbols
                            if 'selected_symbols' not in source or s.name in source['selected_symbols'])
    unresolved = sorted({(ref, dep) for ref, symbol in selected for dep in symbol.static_dependencies
                         if dep.split('.')[0] not in sys.stdlib_module_names and dep not in available})
    errors = []
    if 'review_map' not in view['context']:
        errors.append('preflight_obligation_mapping_required')
    else:
        try:
            validate_map(packet)
        except (ValueError, TypeError, KeyError, RecursionError) as error:
            errors.append(str(error))
    sizes = [{'id': s['id'], 'kind': s['kind'], 'bytes': len(json_bytes(s))} for s in view['sources']]
    texts = [s['text'] if 'segments' not in s else ''.join(x['text'] for x in s['segments']) for s in sources]
    counts = Counter(texts)
    return {'authority': 'NONE', 'readiness': 'DECLARED_INPUTS_CHECKED' if not errors else 'NEEDS_PREPARATION',
            'errors': errors, **budget(packet), 'sources': sorted(sizes, key=lambda s: -s['bytes']),
            'repeated_source_text_bytes': sum((n - 1) * len(t.encode('utf-8')) for t, n in counts.items()),
            'unresolved_static_references': [{'source_ref': ref, 'reference': dep} for ref, dep in unresolved],
            'limits': 'Static references are navigation, not a complete call graph. Test receipts are implementer claims. '
                      'Byte counts do not bound output, reasoning, billable cost or subscription quota.'}
