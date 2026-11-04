"""tare fleet: which local models the GPU node serves, the owner's choice. The node's catalog holds the models
tare.tools qualified (shown first); the fleet picks from it, sets the default (tare auto's local model) and adds
the owner's own models, which need only their weights' path. Changes go to the node at once (GET/PUT /v1/fleet,
the node token of `tare gpu`); `/fleet` in the console does the same."""
import argparse
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def view(origin):
    """{'catalog': [card + on_node], 'fleet': {default, models, custom}, 'served', 'default', 'missing'}."""
    with urlopen(origin + '/v1/fleet', timeout=10) as response:
        return json.load(response)


def save(origin, token, fleet):
    """PUT the owner's fleet; the node serves it at once. ValueError with the node's reason when it refuses."""
    request = Request(origin + '/v1/fleet', data=json.dumps(fleet).encode(), method='PUT',
                      headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
    try:
        with urlopen(request, timeout=60) as response:
            return json.load(response)
    except HTTPError as exc:
        try:
            reason = json.load(exc)['error']['message']
        except (ValueError, KeyError, TypeError):
            reason = f'HTTP {exc.code}'
        raise ValueError(f'The node kept its fleet: {reason}') from None


def remember(origin):
    """After the fleet changed: this machine's setup asks the node again, so the picker shows what it serves now,
    tare auto runs its default and an own model gets its generic profile."""
    from .environment import apply, read, refresh_gateway
    value = read()
    gateways = (value.get('local_models') or {}).get('gateways') or []
    if 'error' not in value and gateways and gateways[0]['origin'].rstrip('/') == origin:
        apply(refresh_gateway(value))


def change(origin, token, fleet):
    """Save the fleet on the node and in this machine's view of it; the node's answer (see `view`)."""
    value = save(origin, token, fleet)
    remember(origin)
    return value


def own_line(name, card):
    """An own model as one line: name, weights, then the backend's arguments."""
    return ' '.join([name, card['artifact']['path'], *map(str, (card.get('runtime') or {}).get('args') or [])])


def own_models(text, current=None):
    """{name: card} from one line per own model ('name /path/to/weights.gguf -c 32768 ...'); a model kept from
    `current` keeps what the line cannot say (its backend, its binary)."""
    models, current = {}, current or {}
    for line in text.splitlines():
        words = line.split()
        if not words:
            continue
        if len(words) < 2 or not words[1].startswith('/'):
            raise ValueError(f'"{line.strip()}": a name, then the absolute path of its weights on the node')
        card = dict(current.get(words[0]) or {})
        card['artifact'] = {**(card.get('artifact') or {}), 'path': words[1]}
        card['runtime'] = {**(card.get('runtime') or {}), 'args': words[2:]}
        models[words[0]] = card
    return models


def describe(value):
    fleet = value.get('fleet') or {}
    picked, default = set(fleet.get('models') or []), value.get('default')
    lines = ['Catalog (qualified by tare.tools):']
    for card in value.get('catalog') or []:
        mark = '*' if card['id'] == default else 'x' if card['id'] in picked else ' '
        roles = ', '.join(card.get('qualified_for') or [])
        lines.append(f"  [{mark}] {card['id']:<14} {card.get('display_name') or ''}" + (f' · {roles}' if roles else '')
                     + ('' if card.get('on_node') else ' · not on the node'))
    custom = fleet.get('custom') or {}
    if custom:
        lines.append('Your own models:')
        lines += [f"  [{'*' if name == default else 'x'}] {own_line(name, card)}" for name, card in custom.items()]
    if value.get('missing'):
        lines.append('No longer in the catalog (skipped): ' + ', '.join(value['missing']))
    lines.append(f'Default (tare auto): {default}.  [x] served, [*] default.')
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='tare fleet', description=__doc__)
    sub = parser.add_subparsers(dest='action')
    sub.add_parser('show', help='the catalog and your fleet (the default)').add_argument('--json', action='store_true')
    sub.add_parser('add', help='serve catalog models').add_argument('ids', nargs='+')
    sub.add_parser('remove', help='stop serving models (catalog or your own)').add_argument('ids', nargs='+')
    sub.add_parser('default', help="the model tare auto uses on this node").add_argument('id')
    own = sub.add_parser('own', help='add or replace one of your own models')
    own.add_argument('name')
    own.add_argument('path', help='absolute path of its weights on the node')
    own.add_argument('args', nargs=argparse.REMAINDER, help="-- the backend's arguments, e.g. -- -c 32768")
    args = parser.parse_args(argv)
    from .gpu import node_token
    from .native_catalog import local_gateway
    origin = local_gateway()
    try:
        value = view(origin)
    except (OSError, ValueError) as exc:
        print(f'The model gateway at {origin} did not show its fleet: {type(exc).__name__}.')
        return 1
    if args.action in (None, 'show'):
        print(json.dumps(value, indent=1) if getattr(args, 'json', False) else describe(value))
        return 0
    fleet = {'default': None, 'models': [], 'custom': {}, **(value.get('fleet') or {})}
    fleet['models'] = [m for m in fleet['models'] if m not in value.get('missing', [])]
    if args.action == 'add':
        fleet['models'] += [m for m in args.ids if m not in fleet['models']]
    elif args.action == 'remove':
        fleet['models'] = [m for m in fleet['models'] if m not in args.ids]
        fleet['custom'] = {n: c for n, c in fleet['custom'].items() if n not in args.ids}
        if fleet['default'] in args.ids:
            fleet['default'] = next(iter(fleet['models'] + list(fleet['custom'])), None)
    elif args.action == 'default':
        if args.id not in fleet['models'] and args.id not in fleet['custom']:
            fleet['models'].append(args.id)
        fleet['default'] = args.id
    try:
        if args.action == 'own':
            line = ' '.join([args.name, args.path, *[a for a in args.args if a != '--']])
            fleet['custom'].update(own_models(line, fleet['custom']))
        print(describe(change(origin, node_token(), fleet)))
    except (OSError, ValueError) as exc:
        print(exc)
        return 1
    return 0
