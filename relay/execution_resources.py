"""Operator-selected ceilings, scoped to one invocation and its delegated roles."""
import math
import re
from functools import wraps

from .native_limits import UNLIMITED_STAGNANT_TURNS

RESOURCES = ('calls', 'time', 'money', 'compactions', 'stagnation')
LEGACY = ('calls', 'time', 'compactions', 'stagnation')
ALIASES = {'turns': 'calls', 'budget': 'money', 'cost': 'money', 'usd': 'money',
           'seconds': 'time', 'duration': 'time', 'compaction': 'compactions',
           'no-progress': 'stagnation'}
HELP = ('Remove selected execution ceilings for this instance and its workers: '
        'calls time money compactions stagnation (stagnation loosens the no-progress pause to '
        f'{UNLIMITED_STAGNANT_TURNS} replies; --max-stagnant-turns unlimited removes it). No list means all; '
        'use spaces or commas. Provider limits, request timeouts and permissions still apply.')


def parse(value):
    if value is True:
        return RESOURCES
    if value is False or value is None:
        return ()
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, (tuple, list)):
        raise ValueError('Invalid unlimited resources. Choose: '+', '.join(RESOURCES)+'.')
    result = set()
    for item in values:
        if not isinstance(item, str):
            raise ValueError('Unlimited resources must be names: '+', '.join(RESOURCES)+'.')
        if not item.strip():
            result.update(RESOURCES)
        for name in filter(None, re.split(r'[\s,;]+', item.lower())):
            name = ALIASES.get(name, name)
            if name == 'all':
                result.update(RESOURCES)
            elif name in RESOURCES:
                result.add(name)
            else:
                raise ValueError('Unknown unlimited resource '+repr(name)+'. Choose: '+', '.join(RESOURCES)+'.')
    return tuple(name for name in RESOURCES if name in result)


def selected(name, value=None):
    if value is None:
        from .execution_scope import current
        value = current().unlimited
    return name in parse(value)


def add_argument(parser):
    parser.add_argument('--unlimited', action='append', nargs='?', const='all', default=[],
                        metavar='RESOURCE[,RESOURCE...]', help=HELP)


def cli(function):
    """Keep explicit operator authorization alive across every stage of a command."""
    @wraps(function)
    def run(argv=None, *args, **kwargs):
        import argparse
        import sys
        from dataclasses import replace
        from .native_limits import cli_limit
        from .execution_scope import Scope, activated, current
        values = normalize_arguments(list(sys.argv[1:] if argv is None else argv))
        boundary = next((i for i, v in enumerate(values) if v in ('--', '--verify-command')), len(values))
        parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
        add_argument(parser)
        explicit = {'max-model-calls': 'calls', 'max-seconds': 'time',
                    'max-stagnant-turns': 'stagnation', 'max-compactions': 'compactions'}
        for flag in explicit:
            parser.add_argument('--'+flag, type=cli_limit(float), default=argparse.SUPPRESS)
        options, _ = parser.parse_known_args(values[:boundary])
        try:
            policy = current().intersect(Scope(unlimited=parse(options.unlimited)))
            bounded = {resource for flag, resource in explicit.items()
                       if hasattr(options, flag.replace('-', '_')) and getattr(options, flag.replace('-', '_')) is not None}
            policy = replace(policy, unlimited=tuple(r for r in policy.unlimited if r not in bounded))
        except ValueError as error:
            parser.error(str(error))
        with activated(policy):
            return function(values, *args, **kwargs)
    return run


def normalize_arguments(values):
    """Gather a resource list without eating a following project path or another flag."""
    from pathlib import Path
    result = []
    index = 0
    while index < len(values):
        value = values[index]; index += 1
        if value in ('--', '--verify-command'):
            result.extend(values[index-1:])
            break
        if value != '--unlimited':
            result.append(value)
            continue
        names = []
        while index < len(values) and not values[index].startswith('-'):
            part = values[index]
            try:
                parse(part)
            except ValueError:
                # A path belongs to the console positional argument. A misspelled
                # resource is retained as a value, so validation can explain it.
                if '/' in part or '\\' in part or part in ('.', '..', '~') or Path(part).is_dir():
                    break
                names.append(part); index += 1
                break
            names.append(part); index += 1
        result.append('--unlimited='+(','.join(names) if names else 'all'))
    return result


def limits(values, resources=None):
    result = dict(values)
    for resource, field in [('calls', 'max_model_calls'), ('time', 'max_seconds')]:
        if selected(resource, resources):
            result[field] = None
    if selected('stagnation', resources):  # looser, not gone; an explicit --max-stagnant-turns still wins
        result['max_stagnant_turns'] = UNLIMITED_STAGNANT_TURNS
    return result


def ceiling(resource, *values):
    """Min of real enclosing ceilings; JSON uses null, never Infinity."""
    candidates = [v for v in values if v is not None and math.isfinite(v)]
    return min(candidates) if candidates else None


def child_calls(default, remaining=None, *, reserve=0, share=1):
    local = None if selected('calls') else default
    available = None if remaining is None else (remaining-reserve)//share
    return ceiling('calls', local, available)


def child_seconds(default, remaining=math.inf, *, reserve=0):
    local = None if selected('time') else default
    return ceiling('time', local, remaining-reserve)


def context(policy):
    return {**policy, **({'max_compactions': None} if selected('compactions') else {})}


def spend_capped():
    return not selected('money')


def known_calls(value):
    if value is None:
        raise ValueError('Shared model-call usage is unknown. Preserve the receipts and reconcile it before using a finite call budget.')
    return value


def calls_used(row, session):
    """An unbounded child has no reserved count; its journal still counts requests."""
    value = row.get('calls_used', row.get('calls_reserved'))
    if value is not None:
        return value
    from pathlib import Path
    from compute_plane.native_journal import NativeJournal
    path = Path(session)/'journal.jsonl'
    return sum(r.event()['kind'] in {'model_requested', 'context_summary_requested'}
               for r in NativeJournal(path).snapshot().records) if path.is_file() else 0
