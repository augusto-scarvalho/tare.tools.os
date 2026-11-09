"""Write tare's OpenCode deployment profiles for the operator's model list from OpenCode's public model list.

python scripts/ops/opencode_profiles.py --plan sub|api [--models saved-list.json] [--date YYYY-MM-DD]

OpenCode is two providers in tare. `sub` is the subscription (Go, Go Plus, ...: a plan with a per-model allowance;
relay/native_deployments/opencode-sub-<id>.json, pin `opencode-sub/...`). `api` is pay-per-token from the account
balance (opencode-api-<id>.json, pin `opencode-api/...`). Only the models MODELS names for that plan get profiles;
a profile of the plan that is no longer named, and its role row, are removed. A named model that OpenCode does not
serve with tools on a family the Kernel speaks is reported as waiting.

A model's author is the roster record whose native runtime is its OpenCode id; a record without one gets it, and a
model with none gets a new record from its canonical vendor. A new profile gets an implementer and explorer reserve
row marked `qualified: false`: an exact pin reaches it, automatic selection does not until the mark is flipped after
a live qualification. An edited row is kept. Review the diff: a profile is metadata admission, not measured quality.
No inference, no credentials.
"""
import argparse
import json
import sys
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT/'relay'/'native_deployments'
ROSTER = ROOT/'relay'/'round_table_roster.json'
ROLES = ROOT/'relay'/'role_deployments.json'
LISTS = {'sub': 'https://opencode.ai/inference/go/v1/models', 'api': 'https://opencode.ai/inference/v1/models'}
# The operator's selection (2026-11-09). A model may be named under both plans: the API one is then a paid reserve.
MODELS = {'sub': ('claude-haiku-5-5', 'deepseek-v4.1-flash', 'gemini-4-argon', 'glm-5.3-flash', 'gpt-6-luna',
                  'grok-4.7', 'kimi-k3', 'mimo-v2.6-flash'),
          'api': ('claude-fable-5-2', 'claude-sonnet-5-5', 'gemini-3.8-flash', 'gpt-6-astra', 'gpt-6.1-sol',
                  'mistral-large-4', 'muse-ember-1.0')}
PROTOCOLS = {'chat-completions': 'openai-chat', 'responses': 'openai-responses', 'messages': 'anthropic-messages'}
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
OUTPUT_CAP = 16384  # the convention of the other API profiles; the provider maximum stays in its list
# canonical_id prefix -> roster vendor and bias domain of that family (one author identity across gateways)
VENDORS = {'deepseek': ('DeepSeek', 'deepseek/deepseek'), 'zhipuai': ('Z.ai', 'zai/glm-family'),
           'xai': ('xAI', 'xai/grok'), 'meta': ('Meta', 'meta/muse-family'), 'openai': ('OpenAI', 'openai/gpt'),
           'tencent': ('Tencent', 'tencent/hy-family'), 'moonshotai': ('Moonshot AI', 'kimi/kimi-k3'),
           'xiaomi': ('Xiaomi', 'xiaomi/mimo-family'), 'minimax': ('MiniMax', 'minimax/m3'),
           'meituan': ('Meituan', 'meituan/longcat-family'), 'stepfun': ('StepFun', 'stepfun/step-family'),
           'alibaba': ('Qwen', 'qwen/qwen-family'), 'google': ('Google', 'google/gemini'),
           'mistral': ('Mistral AI', 'mistral/mistral'), 'mistralai': ('Mistral AI', 'mistral/mistral')}
PREFIX = {'sub': 'opencode-sub-', 'api': 'opencode-api-'}


def author(row):
    vendor = (row.get('canonical_id') or '').split('/', 1)[0]
    if vendor == 'anthropic':
        family = next((f for f in ('haiku', 'sonnet', 'opus', 'fable') if f in row['id']), None)
        if family is None:
            raise SystemExit(f"{row['id']}: Anthropic family unknown; add its roster record by hand")
        return 'Anthropic', 'anthropic/' + family
    if vendor not in VENDORS:
        raise SystemExit(f"{row['id']}: vendor {vendor!r} has no roster family yet; add its roster record by hand")
    return VENDORS[vendor]


def roster_key(models, row):
    """The roster record of this OpenCode model id, adding the alias or the record when missing."""
    wire = row['id']
    found = [key for key, model in models.items() if (model.get('runtime') or {}).get('native') == wire]
    if found:
        return found[0]
    if wire in models:
        runtime = models[wire].setdefault('runtime', {})
        if runtime.get('native', wire) != wire:  # another gateway's model id: its identity is not ours to move
            raise SystemExit(f"{wire}: roster record already serves {runtime['native']!r}; add a record by hand")
        runtime.update({'native': wire, 'native-http': wire})
        return wire
    vendor, domain = author(row)
    models[wire] = {'display_name': row['name'], 'vendor': vendor, 'bias_domain': domain,
                    'runtime': {'native': wire, 'native-http': wire}}
    return wire


def parameters(protocol, levels):
    if 'high' not in levels:
        return {}  # the provider's default reasoning
    return {'openai-chat': {'reasoning_effort': 'high'}, 'openai-responses': {'reasoning': {'effort': 'high'}},
            'anthropic-messages': {'output_config': {'effort': 'high'}}}[protocol]


def profile(row, family, api, key, plan, today):
    protocol, wire = PROTOCOLS[family], row['id']
    reasoning = row['capabilities'].get('reasoning') or {}
    levels = [e for e in reasoning.get('effort') or [] if e in EFFORTS]
    name = PREFIX[plan] + wire.replace('.', '-')
    source = LISTS[plan] + ', read ' + today
    return name, {
        'id': name, 'runner': 'native-http', 'model': key,
        'native_credential_env': 'OPENCODE_API_KEY', 'native_counter_protocol': 'utf8-envelope-estimate-v1',
        'native_profile': {
            'protocol': protocol, 'model': wire, 'endpoint': api['base_url'],
            'context_window': row['limit']['context'], 'output_tokens': min(row['limit']['output'], OUTPUT_CAP),
            'parameters_json': json.dumps(parameters(protocol, levels), sort_keys=True),
            'session_header': 'x-opencode-session',
            'source': 'OpenCode model list ' + source + '; metadata admission, not measured delivery quality.'},
        'native_usage_contract': {'layout': 'opencode-exclusive',
                                  'source': 'https://opencode.ai/docs/console/api/inference (usage.opencode.tokens, cost)'},
        'native_reasoning_levels': levels,
        'native_opencode': {'plan': plan, 'label': row['name'], 'family': family,
                            'tool_choice': (api.get('capabilities') or {}).get('tools', {}).get('choice', []),
                            **({'data_use': 'meta-contributor'} if wire.endswith('-contributor') else {}),
                            'source': LISTS[plan], 'observed_at': today}}


def role_row(value, plan):
    family = value['native_opencode']['family']
    return {'id': value['id'], 'model': value['model'],
            'provider': 'OpenCode Subscription' if plan == 'sub' else 'OpenCode API',
            'transport': 'API', 'runner': 'native-http',
            'quota_domain': 'opencode-sub' if plan == 'sub' else 'opencode-balance',
            'endpoint_domain': 'opencode-' + family, 'effort': None, 'roles': ['implementer', 'explorer'],
            'kind': 'reserve', 'cost_tier': 'flat' if plan == 'sub' else 'metered', 'qualified': False,
            'native_catalog': value['id'], 'note': value['native_profile']['source']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--plan', choices=sorted(LISTS), required=True)
    parser.add_argument('--models', type=Path, help='a saved copy of the list instead of fetching it')
    parser.add_argument('--date', default=date.today().isoformat())
    args = parser.parse_args(argv)
    if args.models:
        listing = json.loads(args.models.read_text(encoding='utf-8'))
    else:
        request = urllib.request.Request(LISTS[args.plan], headers={'User-Agent': 'tare-opencode-profiles/1'})
        with urllib.request.urlopen(request, timeout=30) as reply:
            listing = json.load(reply)
    roster = json.loads(ROSTER.read_text(encoding='utf-8'))
    roles = json.loads(ROLES.read_text(encoding='utf-8'))
    listed = {row.get('native_catalog', row['id']) for row in roles['deployments']}
    rows = {row['id']: row for row in listing['data']}
    written, waiting = [], {}
    for wire in MODELS[args.plan]:
        row = rows.get(wire)
        if row is None:
            waiting[wire] = 'not in the list'
            continue
        (family, api), = row['api'].items()
        if family not in PROTOCOLS or not row['capabilities'].get('tools', {}).get('supported'):
            waiting[wire] = family + ' API without a Kernel codec' if family not in PROTOCOLS else 'no tool support'
            continue
        name, value = profile(row, family, api, roster_key(roster['models'], row), args.plan, args.date)
        (PROFILES/(name + '.json')).write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
        written.append(name)
        if name not in listed:
            roles['deployments'].append(role_row(value, args.plan))
    if not written:  # the wrong plan's list, or an empty reply: never take it as "remove every profile"
        raise SystemExit(json.dumps({'error': 'no named model is in this list; nothing changed', 'waiting': waiting}))
    removed = sorted(p.stem for p in PROFILES.glob(PREFIX[args.plan] + '*.json') if p.stem not in written)
    for name in removed:
        (PROFILES/(name + '.json')).unlink()
    roles['deployments'] = [r for r in roles['deployments'] if r.get('native_catalog', r['id']) not in removed]
    ROSTER.write_text(json.dumps(roster, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    ROLES.write_text(json.dumps(roles, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(json.dumps({'written': written, 'removed': removed, 'waiting': waiting}, indent=2))


if __name__ == '__main__':
    sys.exit(main())
