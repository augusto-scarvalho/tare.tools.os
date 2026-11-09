"""Canonical temporary inference sets. Parsing performs no I/O or inference."""
import re

LEVELS = ('low', 'medium', 'high', 'xhigh', 'max', 'default')
PROVIDERS = {'openai': 'openai', 'anthropic': 'anthropic', 'claude': 'anthropic',
             'google': 'google', 'gemini': 'google', 'openrouter': 'openrouter',
             'opencode-sub': 'opencode-sub', 'opencode-api': 'opencode-api',
             'bedrock': 'bedrock', 'aws': 'bedrock', 'local': 'local', 'api': 'api',
             'ninfer': 'ninfer', 'strata': 'strata'}


def parse(raw):
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError('An explicit inference allowlist cannot be empty.')
    entries = []
    for part in re.split('[,;]', raw.lower()):
        part = part.strip()
        if part in LEVELS and entries and entries[-1][2]:
            provider, model, levels = entries.pop()
            entries.append((provider, model, tuple(sorted(set((*levels, part))))))
            continue
        match = re.fullmatch(r'([a-z0-9-]+)(?:/([a-z0-9][a-z0-9._:/-]*))?(?:@(low|medium|high|xhigh|max|default))?', part)
        if not match or match[1] not in PROVIDERS:
            raise ValueError('Invalid inference selection: '+part)
        entries.append((PROVIDERS[match[1]], match[2] or '', (match[3],) if match[3] else ()))
    merged = {}
    for provider, model, levels in entries:
        key = (provider, model)
        if key not in merged:
            merged[key] = set(levels) if levels else None
        elif not levels:
            merged[key] = None
        elif merged[key] is not None:
            merged[key].update(levels)
    return tuple(sorted((p, m, tuple(sorted(levels)) if levels is not None else ())
                        for (p, m), levels in merged.items()))


def exact(raw):
    """A role pin names one connection, never a model-name search or set."""
    if not isinstance(raw, str) or not re.fullmatch(r'[a-z0-9-]+/[a-z0-9][a-z0-9._:-]*@(low|medium|high|xhigh|max|default)', raw.lower()):
        raise ValueError('Use provider/deployment@reasoning; consult tare models --role ROLE.')
    entries = parse(raw)
    if len(entries) != 1:
        raise ValueError('Choose one exact connection; consult tare models.')
    provider, deployment, levels = entries[0]
    return provider+'/'+deployment+'@'+levels[0]


def canonical(raw):
    return ';'.join(p+('/'+m if m else '')+('@'+','.join(levels) if levels else '')
                    for p, m, levels in parse(raw))


def identities(name, deployment):
    from .model_choices import describe
    item = describe(name, deployment)
    route = item['route']
    profile = deployment['native_profile']
    group = item['group'].lower()
    provider = {'aws bedrock': 'bedrock', 'local': {'qwen38-gsq': 'ninfer',
                'swift27b': 'ninfer', 'swift-next': 'strata'}.get(profile['model'], 'local')}.get(group, group)
    if deployment.get('native_opencode'):
        from .opencode import provider as opencode_provider
        provider = opencode_provider(deployment)
    models = {name.lower(), profile['model'].lower(), item['key'].lower(),
              str((deployment.get('native_model_choice') or {}).get('connection','')).lower(),
              str(deployment.get('model', '')).lower()}
    # Friendly names are aliases of these registered models, never wire IDs.
    aliases = {'qwen38-gsq': {'qwen3.8-gsq'}, 'swift-next': {'qwen-next'},
               'claude-opus-5-5': {'opus'}}
    models.update(aliases.get(item['key'], set()))
    if (deployment.get('native_openrouter') or deployment.get('native_opencode')) and not route['efforts']:
        route = {**route, 'efforts': ['default'], 'default_effort': 'default'}
    return provider, models, route


def matches(raw, name, deployment, *, effort=None):
    provider, models, route = identities(name, deployment)
    actual = route['default_effort'] if effort is None else effort
    return any(p == provider and (not m or m in models) and
               (not levels or actual in levels and actual in route['efforts'])
               for p, m, levels in parse(raw))


def validate_catalog(raw, deployments):
    """Every entry must name a registered model and supported reasoning, not a typo."""
    for p, m, levels in parse(raw):
        found = False
        for name, value in deployments:
            provider, models, route = identities(name, value)
            if p == provider and (not m or m in models):
                if not levels or set(levels) <= set(route['efforts']):
                    found = True
                    break
        if not found:
            raise ValueError('Unknown or unsupported inference selection: '+p+'/'+m+'@'+','.join(levels))
