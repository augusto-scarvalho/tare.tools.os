"""Named local deployments and redacted inspection; never resolves credentials."""
import argparse
import json
import os
from pathlib import Path
import re
import sys

from .native_generation import load_deployment, MAX_DEPLOYMENT_BYTES
from .native_limits import resolve


def directory():
    from .native_installation import storage_root
    root = Path(os.environ['TARE_PROFILE_DIR']) if 'TARE_PROFILE_DIR' in os.environ else storage_root()/'profiles'
    if not root.is_absolute():
        raise ValueError('profile_directory_must_be_absolute')
    from compute_plane.native_journal import regular
    return regular(root)


def local_path(name):
    from .native_catalog import LOCAL
    if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', name):
        raise ValueError('invalid_profile_name')
    if name in LOCAL or name == 'auto':
        raise ValueError('reserved_profile_name')
    from compute_plane.native_journal import regular
    return regular(directory()/(name+'.json'))


def names():
    root = directory()
    if not root.exists():
        return []
    result = []
    for path in root.glob('*.json'):
        try:
            local_path(path.stem)
        except ValueError:
            continue
        result.append(path.stem)
        if len(result) > 128:
            raise ValueError('too_many_local_profiles')
    return sorted(result)


def describe(name, deployment=None):
    from .native_catalog import select
    value = deployment if deployment is not None else select(name)
    profile = value['native_profile']
    cli = value.get('native_cli', {})
    from compute_plane.native_usage import deployment_contract
    contract = deployment_contract(value) or {}
    from .native_generation import model_context_policy
    return {'deployment': name, 'model': profile['model'], 'protocol': profile['protocol'],
            'context_window': profile['context_window'], 'output_tokens': profile['output_tokens'],
            'compaction_policy': {**model_context_policy(value),
                'source': value.get('native_context_policy', {}).get('source', 'Tare default; not a model recommendation')},
            'context_counting': value.get('native_counter_protocol', 'server-template-tokenize'),
            'cli_version': cli.get('version'),
            'executable_present': Path(cli['executable']).is_file() if cli else None,
            'effective_limits': resolve(value), 'availability': 'not_checked',
            'usage_accounting': 'contract_configured' if contract else 'unknown',
            'cost_accounting': ('provider_reported' if contract.get('layout') in {'openrouter-inclusive', 'openrouter-responses-inclusive',
                                                                               'opencode-exclusive'}
                else 'tariff_configured' if value.get('native_usage_tariff') else 'unknown'),
            'cost_estimate': 'client_reported' if contract.get('layout') == 'claude-cli-exclusive' else 'unknown',
            'subscription_quota': 'unknown'}


def inventory():
    from .native_catalog import LOCAL
    rows = []
    for name in sorted(set(LOCAL) | set(names())):
        try:
            rows.append(describe(name))
        except (ValueError, OSError, KeyError, TypeError):
            rows.append({'deployment': name, 'availability': 'invalid_profile'})
    return rows


def install(name, source):
    """Create-only import; updates remain explicit file edits, never silent overwrite."""
    target = local_path(name)
    value = load_deployment(source)
    raw = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n'
    if len(raw.encode('utf-8')) > MAX_DEPLOYMENT_BYTES:
        raise ValueError('native_deployment_file_over_budget')
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('x', encoding='utf-8') as stream:
        stream.write(raw)
    return describe(name, value)


def _usage_json(path, limit):
    """Strictly decode a custody file under this owner's explicit byte budget."""
    from compute_plane.native_journal import read_json
    return read_json(path, max_bytes=limit)


def usage(session):
    """Read recorded session totals; do not infer vendor subscription balances."""
    from compute_plane.native_journal import custody_lock, regular
    from compute_plane.native_usage import MAX_USAGE_BYTES
    root = regular(session)
    from .native_continuity import chain
    paths = chain(root)
    from .delegation import children
    child_count = sum(len(children(path)) for path in paths)
    delegated = any((path/'planning').is_dir() or (path/'units').is_dir() for path in paths)
    if len(paths) > 1 or child_count or delegated:
        from .execution_usage_sources import execution_usage
        totals = execution_usage(sessions=paths)
        sessions = totals['recorded_sessions']
        counts = {stage: len({(e['session_id'], e['source']) for e in sessions if e['stage'] == stage})
                  for stage in ('exploration', 'planning', 'implementation')}
        with custody_lock(root):
            state = _usage_json(root/'native-agent.json', 1024*1024)
        return {**totals, 'session': str(root), 'session_id': state['session_id'],
                'last_runtime_observation': (state.get('invocations') or [{}])[-1].get('runtime_observation'),
                'model_calls': totals['generation_intents'],
                'effective_limits': state.get('continuation', {}).get('original_limits', state['limits']),
                'deadline_utc': state['deadline_utc'],
                'provider_sessions': len({(e['session_id'], e['source']) for e in sessions}),
                **{stage+'_sessions': number for stage, number in counts.items()}}
    with custody_lock(root):
        state = _usage_json(root/'native-agent.json', 1024*1024)
        report = _usage_json(root/'usage.json', MAX_USAGE_BYTES) if (root/'usage.json').exists() else None
        if report is not None:
            from compute_plane.native_usage import enrich_provider_costs
            report = enrich_provider_costs(report, state.get('deployment', {}))
        outcome = _usage_json(root/'outcome.json', 4*1024*1024) if (root/'outcome.json').exists() else {}
        return {'session': str(root), 'session_id': state['session_id'],
                'last_runtime_observation': outcome.get('runtime_observation'),
                'effective_limits': state.get('limits'), 'deadline_utc': state.get('deadline_utc'),
                'last_outcome_status': outcome.get('status'), 'model_calls': outcome.get('model_calls'),
                'tokens': report.get('tokens') if report else None,
                'coverage': report.get('coverage') if report else None,
                'known_cost_by_currency': report.get('known_cost_by_currency') if report else None,
                'unpriced_or_partial_calls': report.get('unpriced_or_partial_calls') if report else None,
                'provider_estimates_by_currency': report.get('provider_estimates_by_currency') if report else None,
                'subscription_quota': 'unknown'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('list', help='List configured models, limits and accounting coverage; no network calls')
    prices = sub.add_parser('prices', help='Snapshot provider prices and allocation gaps; no model calls')
    prices.add_argument('names', nargs='*')
    prices.add_argument('--live', action='store_true', help='Refresh public OpenRouter and AWS price catalogs')
    prices.add_argument('--output', type=Path, help='Create a cost snapshot for study.costs')
    show = sub.add_parser('show', help='Inspect a named profile without exposing auth or endpoint details')
    show.add_argument('name')
    route = sub.add_parser('route', help='Choose OpenRouter provider endpoints and precision for new selections; no inference')
    route.add_argument('name')
    route.add_argument('--provider', action='append', default=[], help='Exact endpoint slug; repeat for an allowed list')
    route.add_argument('--quantization', action='append', default=[], help='Endpoint precision; omit to leave it unfiltered')
    route.add_argument('--allow-fallbacks', action='store_true', help='Allow recovery within the named endpoint list; the new endpoint may have a cold cache')
    route.add_argument('--reset', action='store_true', help='Restore the shipped route for new selections')
    add = sub.add_parser('import', help='Validate and save a deployment JSON under a new local name')
    add.add_argument('name')
    add.add_argument('source', type=Path)
    comparison = sub.add_parser('compare', help='Compare complete delivery trials from receipts; no model call')
    comparison.add_argument('study', type=Path)
    comparison.add_argument('--write-policy', type=Path, help='Save a new cohort-specific preference only when evidence supports a change')
    totals = sub.add_parser('usage', help='Read session usage and saved limits; missing measurements remain unknown')
    selection = totals.add_mutually_exclusive_group(required=True)
    selection.add_argument('--session', type=Path, action='append', help='Repeat to aggregate saved sessions')
    selection.add_argument('--train', type=Path, help='Train directory containing WORK_RUN.json')
    totals.add_argument('--aggregate', action='store_true', help='Show call-deduplicated stage accounting for a single session')
    totals.add_argument('--costs', type=Path, help='Value saved usage with a price/allocation snapshot')
    args = parser.parse_args(argv)
    try:
        if args.action == 'prices':
            from .provider_prices import snapshot
            result = snapshot(args.names, live=args.live)
            if args.output:
                with args.output.open('x', encoding='utf-8') as stream:
                    json.dump(result, stream, ensure_ascii=False, indent=2)
        elif args.action == 'compare':
            from .delivery_comparison import compare, write_policy
            result = write_policy(args.study, args.write_policy) if args.write_policy else compare(args.study)
        elif args.action == 'route':
            from .openrouter import route_command
            result = route_command(args)
        elif args.action == 'usage' and (args.train or args.aggregate or args.costs or len(args.session or []) > 1):
            from .execution_usage_sources import execution_usage
            result = execution_usage(sessions=args.session or (), train=args.train, costs=args.costs)
        else:
            result = (usage(args.session[0]) if args.action == 'usage' else
                  {'profiles': inventory()} if args.action == 'list' else
                  describe(args.name) if args.action == 'show' else install(args.name, args.source))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        if args.action == 'route':
            print('OpenRouter route: '+str(exc), file=sys.stderr)
            return 2
        if args.action == 'compare':
            print('Delivery comparison failed: '+str(exc), file=sys.stderr)
            return 2
        if args.action == 'prices' or args.action == 'usage' and args.costs:
            print('Cost accounting failed: '+str(exc), file=sys.stderr)
            return 2
        print('Profile operation failed: '+type(exc).__name__+'. Check the name and deployment file.', file=sys.stderr)
        return 2
