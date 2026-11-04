"""Vendor model profiles expanded from explicit catalogues (Codex's own models cache, AGY's list).

No login, inference, credential discovery or implicit model fallback.
"""
from copy import deepcopy
import json

from .model_choices import EFFORTS
from .native_generation import deployment_profile

CODEX_NATIVE_BATCH_QUALIFIED = {'gpt-5.6-sol', 'gpt-6-sol', 'gpt-6.1-sol'}


def parse(slug):
    """(vendor, line, version) of a model slug: gpt-6.1-sol is ('gpt', 'sol', (6, 1))."""
    parts = slug.split('-')
    return parts[0], '-'.join(p for p in parts[1:] if not p.replace('.', '').isdigit()), tuple(
        int(n) for p in parts[1:] if p.replace('.', '').isdigit() for n in p.split('.'))


def older(slugs):
    """The models a vendor still lists that a newer version of the same line replaces (gpt-6-sol beside
    gpt-6.1-sol, gpt-5.6-luna beside gpt-6-luna). Versions move per line: a line without a newer version, such as
    gpt-6-astra beside gpt-6.1-sol, is never older."""
    parsed = {s: parse(s) for s in slugs if parse(s)[2]}
    return {s for s, (vendor, line, version) in parsed.items()
            if any(v == vendor and l == line and n > version for v, l, n in parsed.values())}


def newest(slugs, slug):
    """The newest model of `slug`'s line among `slugs` (gpt-6.1-sol for gpt-6-sol), or `slug` itself."""
    vendor, line, _ = parse(slug)
    same = [s for s in slugs if parse(s)[:2] == (vendor, line) and parse(s)[2]]
    return max(same, key=lambda s: parse(s)[2], default=slug)


def expand(profiles, codex_models=(), agy_models=()):
    result = {}
    claude = profiles.get('claude-cli')
    if claude and claude['native_profile']['endpoint'] == 'cli://claude':
        # Opus 5 / 4.8 and Sonnet 5 are retired: the 5.5 models are better and cheaper.
        models = ['claude-fable-5-1']
        version = claude.get('native_cli', {}).get('version', '')
        release = tuple(int(p) for p in version.split('.') if p.isdigit())
        if release >= (2, 1, 280):
            # Saved versions are transport/usage references, not the installed
            # executable's version. Kernel checks Haiku's actual 2.1.293 floor.
            models += ['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5']
        for model in models:
            value = deepcopy(claude)
            value['id'] = name = model+'-cli'
            value['model'] = value['native_profile']['model'] = model
            effort = {'claude-opus-5-5': 'medium', 'claude-haiku-5-5': 'low'}.get(model, 'high')
            value['native_profile']['parameters_json'] = json.dumps({'effort': effort})
            value['native_profile']['source'] = ('Claude Code '+claude['native_cli']['version']+
                ' model-config; explicit model and effort; account availability unchecked')
            value['native_profile'].pop('expected_response_model', None)
            if value['native_cli']['version'] == '2.1.280':
                value['native_usage_contract'] = {
                    'layout': 'claude-cli-exclusive',
                    'source': 'Claude Code 2.1.280 result.usage; one admitted API message; '
                              'https://platform.claude.com/docs/en/build-with-claude/prompt-caching',
                }
            result[name] = value
    for route in ('codex-cli', 'codex-native'):
        base = profiles.get(route)
        if not base:
            continue
        for record in codex_models:
            model = record.get('slug', '')
            if record.get('visibility') != 'list' or not model.startswith('gpt-'):
                continue
            levels = [r['effort'] for r in record.get('supported_reasoning_levels', []) if r.get('effort') in EFFORTS]
            if not levels:
                continue
            value = deepcopy(base)
            same = value['native_profile']['model'] == model
            name = route if same else route+'-'+model.replace('.', '-')
            if not same:
                value['id'] = name
                value['model'] = value['native_profile']['model'] = model
                value['native_profile'].pop('expected_response_model', None)
                effort = record.get('default_reasoning_level', 'medium')
                if effort not in levels:
                    effort = levels[0]
                params = json.loads(value['native_profile'].get('parameters_json', '{}'))
                if route == 'codex-cli':
                    params['effort'] = effort
                else:
                    params.setdefault('reasoning', {})['effort'] = effort
                    if model in CODEX_NATIVE_BATCH_QUALIFIED:
                        # This route completed two independent TUI reads in one
                        # response. Keep other generated routes on their base policy.
                        params['parallel_tool_calls'] = True
                value['native_profile']['parameters_json'] = json.dumps(params)
                # Preserve conservative transport limits; never enlarge from a label alone.
                value['native_profile']['context_window'] = min(value['native_profile']['context_window'], record['context_window'])
                value['native_profile']['source'] = 'Explicit local Codex model catalogue; account availability unchecked'
            if route == 'codex-cli':
                value['native_cli']['model_catalog'] = deepcopy(record)
            else:
                value['native_reasoning_levels'] = levels
            result[name] = value
    base = profiles.get('agy-cli')
    if base:
        value = deepcopy(base)
        model = value['native_profile']['model']
        family = model.rsplit('-', 1)[0]
        mapping = {e: family+'-'+e for e in EFFORTS[:3] if family+'-'+e in agy_models}
        if model in mapping.values():
            value['native_reasoning_models'] = mapping
            result['agy-cli'] = value
    for value in result.values():
        deployment_profile(value, value['native_profile']['model'])
    return result
