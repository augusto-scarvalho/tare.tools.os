"""Write tare's OpenCode deployment profiles from OpenCode's public model list; no inference, no credentials.

python scripts/ops/opencode_profiles.py --plan go [--models saved-list.json] [--date YYYY-MM-DD]

Each model OpenCode serves with tools on a family the Kernel speaks gets relay/native_deployments/opencode-go-<id>.json
(Console models: opencode-<id>.json). Its author identity comes from the roster record whose native runtime is the
OpenCode model id; a record without one gets it, and a model with none gets a new record from its canonical vendor.
Free models are left out (OpenCode refuses them to other clients). A model without a row in
relay/role_deployments.json gets one as an implementer and explorer reserve marked `qualified: false`: an exact pin
reaches it, automatic selection does not until a live qualification flips the mark. An edited row is kept. Rerun after OpenCode changes its list and review the diff: a profile
is metadata admission, not measured delivery quality.
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
LISTS = {'go': 'https://opencode.ai/inference/go/v1/models', 'console': 'https://opencode.ai/inference/v1/models'}
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
           'mistralai': ('Mistral AI', 'mistral/mistral')}


def author(row):
    vendor = row['canonical_id'].split('/', 1)[0]
    if vendor == 'anthropic':
        family = next((f for f in ('haiku', 'sonnet', 'opus', 'fable') if f in row['id']), None)
        if family is None:
            raise SystemExit(f"{row['id']}: Anthropic family unknown; add its roster record by hand")
        return 'Anthropic', 'anthropic/' + family
    if vendor not in VENDORS:
        raise SystemExit(f"{row['id']}: vendor {vendor!r} has no roster family yet; add it to VENDORS")
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
    name = ('opencode-go-' if plan == 'go' else 'opencode-') + wire.replace('.', '-')
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
    return {'id': value['id'], 'model': value['model'], 'provider': 'OpenCode Go' if plan == 'go' else 'OpenCode',
            'transport': 'API', 'runner': 'native-http',
            'quota_domain': 'opencode-go' if plan == 'go' else 'opencode-balance',
            'endpoint_domain': 'opencode-' + family, 'effort': None, 'roles': ['implementer', 'explorer'],
            'kind': 'reserve', 'cost_tier': 'flat' if plan == 'go' else 'metered', 'qualified': False,
            'native_catalog': value['id'],
            'note': value['native_profile']['source']}


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
    written = []
    for row in listing['data']:
        (family, api), = row['api'].items()
        if (family not in PROTOCOLS or not row['capabilities'].get('tools', {}).get('supported')
                or row['id'].endswith('-free') or not row.get('canonical_id')):
            continue
        name, value = profile(row, family, api, roster_key(roster['models'], row), args.plan, args.date)
        (PROFILES/(name + '.json')).write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
        written.append(name)
        if name not in listed:
            roles['deployments'].append(role_row(value, args.plan))
    ROSTER.write_text(json.dumps(roster, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    ROLES.write_text(json.dumps(roles, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    prefix = 'opencode-go-' if args.plan == 'go' else 'opencode-'
    stale = sorted(p.stem for p in PROFILES.glob(prefix + '*.json') if p.stem not in written
                   and (args.plan == 'go' or not p.stem.startswith('opencode-go-')))
    print(json.dumps({'written': written, 'no_longer_listed': stale}, indent=2))


if __name__ == '__main__':
    sys.exit(main())
