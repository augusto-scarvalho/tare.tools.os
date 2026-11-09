"""Setup observations distinguish keys, sign-in and an actual model reply."""
import json

from .web_search import CREDENTIALS, PROVIDERS as SEARCH_PROVIDERS

PROVIDERS = {'OPENAI_API_KEY': ('OpenAI API', 'openai-api'),
             'ANTHROPIC_API_KEY': ('Claude API', 'claude-api'),
             'GEMINI_API_KEY': ('Gemini API', 'gemini-api'),
             'AWS_BEARER_TOKEN_BEDROCK': ('Amazon Bedrock API', 'bedrock-opus'),
             'OPENROUTER_API_KEY': ('OpenRouter API', 'openrouter-deepseek-v4-1-flash'),
             'OPENCODE_API_KEY': ('OpenCode API (Go and Console)', 'opencode-go-glm-5-3'),
             'TYPESAFE_API_KEY': ('Jev online (TypeSafe)', None)}
PROVIDERS.update({p['key']: (p['label'], None) for p in SEARCH_PROVIDERS.values()})
CLI_LABELS = {'cli://codex': 'Codex CLI · ChatGPT subscription',
              'cli://claude': 'Claude Code · Claude subscription', 'cli://agy': 'AGY CLI · Gemini subscription'}


def key_status(where):
    return {'vault': 'Key saved in the secure vault · connection not checked',
            'env': 'Key configured outside Tare · connection not checked',
            'absent': 'No API key configured', 'missing': 'No API key configured'}.get(
                where, 'Key status unavailable · refresh to try again')


def credential_where(found, env):
    if env == 'TYPESAFE_API_KEY':
        return (found.get('decision_models') or {}).get('jev', 'absent')
    category = 'search_providers' if env in CREDENTIALS else 'api_providers'
    return (found.get(category) or {}).get(env, 'absent')


def can_check(env):
    # Search providers have no common free authentication probe. Saving or
    # opening setup must never silently spend a search credit.
    return env != 'TYPESAFE_API_KEY' and env not in CREDENTIALS


def credential_target(env):
    from .console_connection import Target
    if env in CREDENTIALS:
        value = SEARCH_PROVIDERS[CREDENTIALS[env]]
        return Target(env, value['label'], value['endpoint'], 'search-api', False, env)
    if env == 'TYPESAFE_API_KEY':
        from compute_plane.jev_choice_worker import BASE_URL
        from .action_model_assessment import JEV_MODEL as MODEL
        return Target(env, 'Jev online · '+MODEL, BASE_URL, 'jev', False, env)
    from .native_catalog import LOCAL, select
    from .native_profiles import names
    for name in sorted(set(LOCAL) | set(names())):
        try:
            value = select(name)
            if value.get('native_credential_env') == env:
                p = value['native_profile']
                return Target(env, p['model'], p['endpoint'], p['protocol'], False, env)
        except (ValueError, OSError, KeyError, TypeError):
            continue
    return None


def cli_status(endpoint, executable):
    if not executable:
        return 'Not installed · install this CLI to use its subscription'
    from .cli_discovery import LOGIN, cache_file, fingerprint, logged_in
    login = logged_in(endpoint, executable)
    if login is False:
        return 'Installed · sign-in needed. Run '+LOGIN[endpoint][1]
    try:
        cached = json.loads(cache_file().read_text(encoding='utf-8'))
        proof = cached.get(endpoint, {}) if isinstance(cached,dict) else {}
        tested = isinstance(proof,dict) and proof.get('fingerprint') == fingerprint(executable)
    except (OSError, ValueError, TypeError):
        tested = False
    state = 'Installed and signed in' if login is True else 'Installed · sign-in not verified'
    return state + (' · reply test passed '+str(proof.get('checked_at', 'previously'))
                    if tested else ' · reply not tested')


def local_helper_status():
    from .environment import decides
    from .turn_engine import avoid_local_gpu, local_judge
    if not decides('local_judge'):
        return 'Off in your saved settings · no API key needed'
    if avoid_local_gpu():
        return 'GPU reserved for other work · no API key needed'
    return ('Ready: Qwen3.8 is loaded and idle · no API key needed' if local_judge() else
            'Not available now: needs Qwen3.8 already loaded and idle · no API key needed')
