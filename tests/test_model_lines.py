"""Labels and reasoning levels come from relay/model_lines.json, one row per model line, never one per version."""
from copy import deepcopy
import json

from relay import model_choices, native_catalog

ALL = ['low', 'medium', 'high', 'xhigh', 'max']
LMH = ['low', 'medium', 'high']
# What the picker showed for every shipped deployment and CLI template before model_lines.json (key, label, group,
# connection, efforts, default effort): printed by this file's __main__ run with relay/ at origin/main 8628d8cd. The
# two agy-claude rows came later, with their templates: Anthropic's models, under Anthropic, through AGY.
BEFORE = {
    'agy-claude-opus-5-5-cli': ['claude-opus-5-5', 'Claude Opus 5.5', 'Anthropic', 'AGY CLI', ['low', 'medium', 'high'], 'high'],
    'agy-claude-sonnet-5-5-cli': ['claude-sonnet-5-5', 'Claude Sonnet 5.5', 'Anthropic', 'AGY CLI', ['low', 'medium', 'high'], 'high'],
    'agy-cli':['gemini-3.8-flash', 'Gemini 3.8 Flash', 'Google', 'AGY CLI', ['low', 'medium', 'high'], 'high'],
    'agy-gemini-4-argon-cli': ['gemini-4-argon', 'Gemini 4 Argon', 'Google', 'AGY CLI', ['low', 'medium', 'high'], 'high'],
    'agy-gemini-4-hydrogen-cli': ['gemini-4-hydrogen', 'Gemini 4 Hydrogen', 'Google', 'AGY CLI', ['low', 'medium', 'high'], 'high'],
    'bedrock-glm': ['zai.glm-5', 'GLM-5', 'AWS Bedrock', 'AWS Bedrock', ['high'], 'high'],
    'bedrock-opus': ['us.anthropic.claude-opus-4-6-v1', 'Claude Opus 4.6', 'AWS Bedrock', 'AWS Bedrock', [], ''],
    'bedrock-sonnet': ['us.anthropic.claude-sonnet-4-6', 'Claude Sonnet 4.6', 'AWS Bedrock', 'AWS Bedrock', [], ''],
    'claude-api': ['claude-haiku-5-5', 'Claude Haiku 5.5', 'Anthropic', 'Claude API', ['low', 'medium', 'high', 'xhigh', 'max'], ''],
    'claude-api-fable-5-1': ['claude-fable-5-1', 'Claude Fable 5.1', 'Anthropic', 'Claude API', ['low', 'medium', 'high', 'xhigh', 'max'], ''],
    'claude-api-fable-5-2': ['claude-fable-5-2', 'Claude Fable 5.2', 'Anthropic', 'Claude API', ['low', 'medium', 'high', 'xhigh', 'max'], ''],
    'claude-api-opus-5-5': ['claude-opus-5-5', 'Claude Opus 5.5', 'Anthropic', 'Claude API', ['low', 'medium', 'high', 'xhigh', 'max'], ''],
    'claude-api-sonnet-5-5': ['claude-sonnet-5-5', 'Claude Sonnet 5.5', 'Anthropic', 'Claude API', ['low', 'medium', 'high', 'xhigh', 'max'], ''],
    'claude-cli': ['claude-opus-5-5', 'Claude Opus 5.5', 'Anthropic', 'Claude CLI', ['low', 'medium', 'high', 'xhigh', 'max'], 'low'],
    'claude-fable-5-1-cli': ['claude-fable-5-1', 'Claude Fable 5.1', 'Anthropic', 'Claude CLI', ['low', 'medium', 'high', 'xhigh', 'max'], 'high'],
    'claude-fable-5-2-cli': ['claude-fable-5-2', 'Claude Fable 5.2', 'Anthropic', 'Claude CLI', ['low', 'medium', 'high', 'xhigh', 'max'], 'high'],
    'claude-haiku-5-5-cli': ['claude-haiku-5-5', 'Claude Haiku 5.5', 'Anthropic', 'Claude CLI', ['low', 'medium', 'high', 'xhigh', 'max'], 'low'],
    'claude-opus-5-5-cli': ['claude-opus-5-5', 'Claude Opus 5.5', 'Anthropic', 'Claude CLI', ['low', 'medium', 'high', 'xhigh', 'max'], 'medium'],
    'claude-sonnet-5-5-cli': ['claude-sonnet-5-5', 'Claude Sonnet 5.5', 'Anthropic', 'Claude CLI', ['low', 'medium', 'high', 'xhigh', 'max'], 'high'],
    'codex-cli': ['gpt-6-astra', 'GPT-6 Astra', 'OpenAI', 'Codex CLI', ['low', 'medium', 'high', 'xhigh', 'max'], 'low'],
    'codex-cli-gpt-5-5': ['gpt-5.5', 'GPT-5.5', 'OpenAI', 'Codex CLI', ['low', 'medium', 'high', 'xhigh'], 'medium'],
    'codex-cli-gpt-5-6-luna': ['gpt-5.6-luna', 'GPT-5.6 Luna', 'OpenAI', 'Codex CLI', ['low', 'medium', 'high', 'xhigh', 'max'], 'medium'],
    'codex-cli-gpt-5-6-sol': ['gpt-5.6-sol', 'GPT-5.6 Sol', 'OpenAI', 'Codex CLI', ['low', 'medium', 'high', 'xhigh', 'max'], 'low'],
    'codex-cli-gpt-5-6-terra': ['gpt-5.6-terra', 'GPT-5.6 Terra', 'OpenAI', 'Codex CLI', ['low', 'medium', 'high', 'xhigh', 'max'], 'medium'],
    'codex-cli-gpt-6-1-luna': ['gpt-6.1-luna', 'GPT-6.1 Luna', 'OpenAI', 'Codex CLI', ['low', 'medium', 'high', 'xhigh', 'max'], 'medium'],
    'codex-cli-gpt-6-1-sol': ['gpt-6.1-sol', 'GPT-6.1 Sol', 'OpenAI', 'Codex CLI', ['low', 'medium', 'high', 'xhigh', 'max'], 'low'],
    'codex-cli-gpt-6-luna': ['gpt-6-luna', 'GPT-6 Luna', 'OpenAI', 'Codex CLI', ['low', 'medium', 'high', 'xhigh', 'max'], 'medium'],
    'fable-tc': ['fable-tc', 'Fable-TC', 'Local', 'Local GPU', [], ''],
    'gemini-api': ['gemini-4-argon', 'Gemini 4 Argon', 'Google', 'Gemini API', ['low', 'medium', 'high'], 'medium'],
    'gemini-api-3-8-flash': ['gemini-3.8-flash', 'Gemini 3.8 Flash', 'Google', 'Gemini API', ['low', 'medium', 'high'], 'medium'],
    'gemini-api-4-carbon': ['gemini-4-carbon', 'Gemini 4 Carbon', 'Google', 'Gemini API', ['low', 'medium', 'high'], 'medium'],
    'gemini-api-4-hydrogen': ['gemini-4-hydrogen', 'Gemini 4 Hydrogen', 'Google', 'Gemini API', ['low', 'medium', 'high'], 'medium'],
    'hauhaucs': ['hauhaucs', 'HauhauCS · Qwen3.8', 'Local', 'Local GPU', [], ''],
    'local-auto': ['local-auto', 'Local auto', 'Local', 'Local GPU · host-aware', ['low'], 'low'],
    'openai-api': ['gpt-6.1-sol', 'GPT-6.1 Sol', 'OpenAI', 'OpenAI API', ['low', 'medium', 'high', 'xhigh', 'max'], 'medium'],
    'openai-api-gpt-6-1-sol-pro': ['gpt-6.1-sol::pro', 'GPT-6.1 Sol Pro', 'OpenAI', 'OpenAI API · Pro mode', ['low', 'medium', 'high', 'xhigh', 'max'], 'medium'],
    'openai-api-gpt-6-1-terra': ['gpt-6.1-terra', 'GPT-6.1 Terra', 'OpenAI', 'OpenAI API', ['low', 'medium', 'high', 'xhigh', 'max'], 'medium'],
    'openai-api-gpt-6-astra': ['gpt-6-astra', 'GPT-6 Astra', 'OpenAI', 'OpenAI API', ['low', 'medium', 'high', 'xhigh', 'max'], 'medium'],
    'openai-api-gpt-6-astra-pro': ['gpt-6-astra::pro', 'GPT-6 Astra Pro', 'OpenAI', 'OpenAI API · Pro mode', ['low', 'medium', 'high', 'xhigh', 'max'], 'medium'],
    'openai-api-gpt-6-luna': ['gpt-6-luna', 'GPT-6 Luna', 'OpenAI', 'OpenAI API', ['low', 'medium', 'high', 'xhigh', 'max'], 'medium'],
    'opencode-sub-claude-haiku-5-5': ['claude-haiku-5-5', 'Claude Haiku 5.5', 'OpenCode Subscription', 'OpenCode Subscription', ['low', 'medium', 'high', 'xhigh', 'max'], 'high'],
    'opencode-sub-deepseek-v4-1-flash': ['deepseek-v4.1-flash', 'DeepSeek V4.1 Flash', 'OpenCode Subscription', 'OpenCode Subscription', ['low', 'high', 'max'], 'high'],
    'opencode-sub-gemini-4-argon': ['gemini-4-argon', 'Gemini 4 Argon', 'OpenCode Subscription', 'OpenCode Subscription', ['low', 'medium', 'high'], 'high'],
    'opencode-sub-glm-5-3-flash': ['glm-5.3-flash', 'GLM-5.3-Flash', 'OpenCode Subscription', 'OpenCode Subscription', ['low', 'high', 'max'], 'high'],
    'opencode-sub-gpt-6-luna': ['gpt-6-luna', 'GPT-6 Luna', 'OpenCode Subscription', 'OpenCode Subscription', ['low', 'medium', 'high', 'xhigh', 'max'], 'high'],
    'opencode-sub-grok-4-7': ['grok-4.7', 'Grok 4.7', 'OpenCode Subscription', 'OpenCode Subscription', ['low', 'medium', 'high', 'xhigh'], 'high'],
    'opencode-sub-kimi-k3': ['kimi-k3', 'Kimi K3', 'OpenCode Subscription', 'OpenCode Subscription', ['max'], ''],
    'opencode-sub-mimo-v2-6-flash': ['mimo-v2.6-flash', 'MiMo-V2.6-Flash', 'OpenCode Subscription', 'OpenCode Subscription', [], ''],
    'openrouter-deepseek-v4-1-flash': ['deepseek/deepseek-v4.1-flash', 'DeepSeek V4.1 Flash', 'OpenRouter', 'OpenRouter API', ['low', 'high', 'max'], 'high'],
    'openrouter-deepseek-v4-1-flash-1017': ['deepseek/deepseek-v4.1-flash-1017', 'DeepSeek V4.1 Flash 1017', 'OpenRouter', 'OpenRouter API', ['low', 'high', 'max'], 'high'],
    'openrouter-glm-5-3': ['z-ai/glm-5.3', 'GLM 5.3', 'OpenRouter', 'OpenRouter API', ['low', 'high', 'max'], 'high'],
    'openrouter-glm-5-3-flash': ['z-ai/glm-5.3-flash', 'GLM 5.3 Flash', 'OpenRouter', 'OpenRouter API', ['high'], 'high'],
    'openrouter-grok-4-7': ['x-ai/grok-4.7', 'Grok 4.7', 'OpenRouter', 'OpenRouter API', ['high'], 'high'],
    'openrouter-kimi-k3-1-pro': ['moonshotai/kimi-k3.1-pro', 'Kimi K3.1 Pro', 'OpenRouter', 'OpenRouter API', ['high'], 'high'],
    'openrouter-ling-3-1-flash': ['inclusionai/ling-3.1-flash', 'Ling 3.1 Flash', 'OpenRouter', 'OpenRouter API', [], ''],
    'openrouter-mimo-v2-6-pro': ['xiaomi/mimo-v2.6-pro', 'MiMo V2.6 Pro', 'OpenRouter', 'OpenRouter API', [], ''],
    'openrouter-mistral-large-4': ['mistralai/mistral-large-4-0', 'Mistral Large 4', 'OpenRouter', 'OpenRouter API', ['high'], 'high'],
    'openrouter-muse-spark-1-3-contributor': ['meta/muse-spark-1.3-contributor', 'Muse Spark 1.3 Contributor', 'OpenRouter', 'OpenRouter API', ['high'], 'high'],
    'openrouter-muse-wonder': ['meta/muse-wonder', 'Muse Wonder', 'OpenRouter', 'OpenRouter API', ['high'], 'high'],
    'openrouter-qwen3-8-omni-flash': ['qwen/qwen3.8-omni-flash', 'Qwen3.8 Omni Flash', 'OpenRouter', 'OpenRouter API', [], ''],
    'openrouter-step-5-preview': ['stepfun/step-5-preview', 'Step 5 Preview', 'OpenRouter', 'OpenRouter API', ['high'], 'high'],
    'qwen36-moe': ['qwen36-moe', 'Qwen3.6 MoE', 'Local', 'Local GPU', [], ''],
    'qwen38-gsq': ['qwen38-gsq', 'Qwen3.8 27B GSQ · NInfer', 'Local', 'Local GPU', ['low', 'medium', 'xhigh'], 'low'],
    'swift-next': ['swift-next', 'Swift 1.5 Flash-Next', 'Local', 'Local GPU', ['low', 'medium', 'xhigh'], 'low'],
    'swift27b': ['swift27b', 'Swift 1.5 27B · NInfer', 'Local', 'Local GPU', ['xhigh'], 'xhigh'],
    'tare-auto': ['tare-auto', 'tare auto', 'tare', 'local → cheap → intermediate', [], ''],
}
# Through shipped connections (key, label, group, connection, efforts): versions that do not exist yet, and AGY's
# Claude ids, labelled from the table alone; AGY's own ids give its efforts, the Codex template's catalogue Codex's.
# AGY's Claude ids are grouped under their author, Anthropic (they were under Google, AGY's vendor, until the
# agy-claude templates).
NEW = {
    ('claude-cli', 'claude-fable-5-3'): ['claude-fable-5-3', 'Claude Fable 5.3', 'Anthropic', 'Claude CLI', ALL],
    ('claude-api', 'claude-fable-5-3'): ['claude-fable-5-3', 'Claude Fable 5.3', 'Anthropic', 'Claude API', ALL],
    ('codex-cli', 'gpt-6.2-terra'): ['gpt-6.2-terra', 'GPT-6.2 Terra', 'OpenAI', 'Codex CLI', ALL],
    ('openai-api', 'gpt-6.2-terra'): ['gpt-6.2-terra', 'GPT-6.2 Terra', 'OpenAI', 'OpenAI API', ALL],
    ('gemini-api', 'gemini-4.1-argon'): ['gemini-4.1-argon', 'Gemini 4.1 Argon', 'Google', 'Gemini API', LMH],
    ('agy-cli', 'gemini-4.1-argon-low', 'gemini-4.1-argon-medium', 'gemini-4.1-argon-high'):
        ['gemini-4.1-argon', 'Gemini 4.1 Argon', 'Google', 'AGY CLI', LMH],
    ('agy-cli', 'claude-opus-5-5-low', 'claude-opus-5-5-medium', 'claude-opus-5-5-high'):
        ['claude-opus-5-5', 'Claude Opus 5.5', 'Anthropic', 'AGY CLI', LMH],
    ('agy-cli', 'claude-sonnet-5-5-low', 'claude-sonnet-5-5-medium', 'claude-sonnet-5-5-high'):
        ['claude-sonnet-5-5', 'Claude Sonnet 5.5', 'Anthropic', 'AGY CLI', LMH],
}
# Ids with no line (an unknown line, a non-chat model, a dated snapshot, AGY's other vendors) fall back as an unknown
# model did before model_lines.json: these rows were printed by the same __main__ run at origin/main 8628d8cd.
UNKNOWN = {
    ('claude-cli', 'claude-mythos-1'): ['claude-mythos-1', 'claude-mythos-1', 'Anthropic', 'Claude CLI', ['low']],
    ('claude-api', 'claude-mythos-1'): ['claude-mythos-1', 'claude-mythos-1', 'Anthropic', 'Claude API', []],
    ('claude-api', 'claude-haiku-4-5-20251001'):
        ['claude-haiku-4-5-20251001', 'claude-haiku-4-5-20251001', 'Anthropic', 'Claude API', []],
    ('codex-cli', 'gpt-7-nova'): ['gpt-7-nova', 'GPT-7-nova', 'OpenAI', 'Codex CLI', ALL],
    ('openai-api', 'gpt-7-nova'): ['gpt-7-nova', 'GPT-7-nova', 'OpenAI', 'OpenAI API', ALL],
    ('openai-api', 'gpt-realtime-2.1'): ['gpt-realtime-2.1', 'GPT-realtime-2.1', 'OpenAI', 'OpenAI API', ALL],
    ('openai-api', 'gpt-image-2.5-flare'): ['gpt-image-2.5-flare', 'GPT-image-2.5-flare', 'OpenAI', 'OpenAI API', ALL],
    ('gemini-api', 'gemini-5-neon'): ['gemini-5-neon', 'gemini-5-neon', 'Google', 'Gemini API', LMH],
    ('gemini-api', 'gemini-3.8-flash-tts'): ['gemini-3.8-flash-tts', 'gemini-3.8-flash-tts', 'Google', 'Gemini API', LMH],
    ('agy-cli', 'gemini-5-neon-low', 'gemini-5-neon-medium', 'gemini-5-neon-high'):
        ['gemini-5-neon', 'Gemini 5-neon', 'Google', 'AGY CLI', LMH],
    ('agy-cli', 'gpt-oss-120b-medium'): ['gpt-oss-120b', 'gpt-oss-120b', 'Google', 'AGY CLI', ['medium']],
    ('agy-cli', 'gemini-pro-orchestrator-g1-pro'):
        ['gemini-pro-orchestrator-g1-pro', 'Gemini pro-orchestrator-g1 Pro', 'Google', 'AGY CLI', ['high']],
}


def shipped():
    return {name: value for name, value, _ in native_catalog.offline_deployments(include_unconfigured=True)}


def shown():
    """What the picker shows for every shipped deployment and CLI template."""
    return {name: [item['key'], item['label'], item['group'], item['route']['label'], item['route']['efforts'],
                   item['route']['default_effort']]
            for name, value in shipped().items() for item in [model_choices.describe(name, value)]}


def moved(deployments, name, *models):
    """A shipped deployment moved to other model ids, as discovery would write it, as the picker shows it. AGY takes
    every id it lists for the model: one per effort, or one with none."""
    value = deepcopy(deployments[name])
    value['native_profile']['model'] = models[-1]
    if name == 'agy-cli':
        value['native_reasoning_models'] = {m.rpartition('-')[2]: m for m in models
                                            if m.endswith(('-low', '-medium', '-high'))}
        if value['native_reasoning_models']:
            value['native_profile']['parameters_json'] = json.dumps({'effort': models[-1].rpartition('-')[2]})
        else:
            del value['native_reasoning_models']
    item = model_choices.describe(name, value)
    return [item['key'], item['label'], item['group'], item['route']['label'], item['route']['efforts']]


def test_every_shipped_label_and_effort_list_is_unchanged():
    assert shown() == BEFORE


def test_a_new_version_of_a_known_line_needs_no_entry_and_an_unknown_line_falls_back_as_before():
    deployments = shipped()
    assert {key: moved(deployments, *key) for key in NEW} == NEW
    assert {key: moved(deployments, *key) for key in UNKNOWN} == UNKNOWN


def test_the_line_comes_from_the_model_never_the_cli():
    from relay.execution_team import actor_label
    from relay.vendor_models import line
    fable, terra, argon, opus = map(line, ('claude-fable-5-3', 'gpt-6.2-terra', 'gemini-4.1-argon-high',
                                           'claude-opus-5-5-high'))
    assert (fable['vendor'], fable['bias_domain'], fable['claude_code_alias']) == ('Anthropic', 'anthropic/fable', 'fable')
    assert (terra['vendor'], terra['bias_domain'], terra['label']) == ('OpenAI', 'openai/gpt', 'GPT-6.2 Terra')
    assert (argon['vendor'], argon['bias_domain'], argon['label']) == ('Google', 'google/gemini', 'Gemini 4.1 Argon')
    assert (opus['vendor'], opus['bias_domain'], opus['label']) == ('Anthropic', 'anthropic/opus', 'Claude Opus 5.5')
    assert 'claude_code_alias' not in terra
    for model in ('gpt-7-nova', 'claude-mythos-1', 'gemini-5-neon', 'gpt-realtime-2.1', 'gemini-3.8-flash-tts',
                  'gpt-image-2.5-flare', 'gpt-oss-120b-medium', 'gemini-pro-orchestrator-g1-pro',
                  'claude-haiku-4-5-20251001', 'claude-opus', 'gpt-5.5'):
        assert line(model) is None and model_choices.model_label(model) is None
    assert actor_label({'role': 'auditor', 'model': 'claude-opus-5-5-high', 'reasoning': 'high'}) == \
        'Auditor · Claude Opus 5.5 · high'
    line('claude-fable-5-3')['efforts'].append('ultra')  # the caller's copy, never the table
    assert line('claude-fable-5-3')['efforts'] == ALL


def test_every_first_party_model_tare_ships_has_a_line_spelled_as_the_roster_spells_it():
    from relay.vendor_models import line
    deployments = shipped()
    first_party = {deployments[name]['native_profile']['model'] for name, row in shown().items()
                   if row[2] in ('Anthropic', 'OpenAI', 'Google')}
    assert sorted(m for m in first_party if not line(m)) == ['gpt-5.5']  # a Codex template with no line name
    roster = json.loads(native_catalog.roster_path().read_text(encoding='utf-8'))['models']
    for record in roster.values():
        for model in (record.get('runtime') or {}).values():
            if line(model):
                assert (line(model)['vendor'], line(model)['bias_domain']) == (record['vendor'], record['bias_domain'])


if __name__ == '__main__':
    # Prints BEFORE, then the NEW and UNKNOWN rows, as the checked-out relay/ shows them, with empty scratch storage.
    import os
    import sys
    import tempfile
    sys.stdout.reconfigure(encoding='utf-8')
    scratch = tempfile.mkdtemp()
    os.environ.update(TARE_STORAGE_ROOT=scratch, TARE_PROFILE_DIR=scratch, CODEX_HOME=scratch,
                      TARE_LOCAL_GATEWAY='http://127.0.0.1:8080',
                      TARE_ENVIRONMENT_STORE=os.path.join(scratch, 'environment.json'))
    for name, row in shown().items():
        print(f'    {name!r}: {row!r},')
    deployments = shipped()
    for key in (*NEW, *UNKNOWN):
        print(f'    {key!r}: {moved(deployments, *key)!r},')
