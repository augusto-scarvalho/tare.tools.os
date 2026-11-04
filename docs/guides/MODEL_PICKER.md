# Model and reasoning selection

Open **Model** above the composer, use `/model`, or choose **Choose model and
reasoning** in Settings. `/reasoning` opens the same panel on its reasoning
control. Both commands participate in slash completion.

Search by model, provider or connection. Recent models appear first, once each.
Select a model with the mouse or arrow keys, adjust **Reasoning**, and choose
**Connection** when more than one route exists. Enter in search/the model list
or **Apply** saves the choice; Escape cancels. Selecting a row with the mouse
does not apply it. The connection remains visible as a label for single-route
models. `local-qwen` and `qwen38` share one picker entry.

## What changes

Choices apply to **new conversations/executions**. An active conversation keeps
its saved model and reasoning, including when resumed. The composer displays
the active session's configuration; after applying a future preference, the
status line identifies the next conversation's model. Use `/new` to start with
that preference. No model call occurs while browsing or applying settings.

The picker remembers a connection and reasoning choice per model, up to twelve
recent models, and the last applied selection. Preferences live under the
external console storage directory, in `models.json`. Full selections are
immutable, content-addressed JSON files in `model-selections/`. Existing vendor
profiles, CLI user settings and saved session descriptors are not rewritten by
the picker. Custom profile paths remain available under Execution options.

**Default** resolves to the selected connection's configured effort, shown in
the control. Only registered levels are offered. A connection change resets an
unsupported effort to Default before Apply. Local and Bedrock profiles retain
their existing qualified parameters; their reasoning control is fixed to the
profile default in this delivery.

Codex Direct and Codex CLI are connections under the same model. CLI catalogues
may expose agent modes as well as reasoning: `ultra` and Claude `ultracode` are
excluded. Tare keeps the agent loop, tools and flows. No automatic connection,
account, model or paid API fallback is introduced.

## Additional vendor models

`scripts/configure_vendor_models.py` expands existing local connections using
an explicit local Codex catalogue and the documented Claude IDs:
`claude-opus-5-5`, `claude-sonnet-5-5`, `claude-fable-5-1`, and
`claude-haiku-5-5`. Existing base profiles stay available. Codex entries marked hidden are excluded; each CLI variant retains
its own complete model catalogue record. Direct variants retain the selected
connection's account binding and conservative context/output limits.

The script does not log in, contact a provider or purchase credits. It defaults
to preview and needs `--apply` to write. Changed base profiles are backed up;
conflicting custom names are refused. Example, with absolute operator paths:

```text
python scripts/configure_vendor_models.py --profiles /storage/profiles --codex-catalog /codex/models_cache.json --backup /evidence/profiles-before --apply
```

An optional `--agy-models` accepts a JSON array of exact IDs observed from
`agy models`. AGY effort-specific IDs are grouped into one model and a reasoning
selection changes both the requested ID and effort together. Missing tiers are
not invented. Installed CLI version checks remain in force.

Listed means **configured**, not that login, quota or account access was checked.
Provider errors remain visible. Adding a model is not a benchmark or proof of
every reasoning level's live behavior. See [vendor inference](VENDOR_CLI_INFERENCE.md)
for transport boundaries and usage-accounting limitations.

### Gemini 4 Argon

AGY discovery offers `agy-gemini-4-argon-cli`, shown as **Gemini 4 Argon**.
The exact IDs from `agy models` on 2026-10-17 support **low**, **medium** and
**high**; **Default** resolves to **high**. Selecting a reasoning level changes
both the model ID and effort together.

Operator policy places Argon alongside Astra and Fable for frontier planning,
audit and the highest implementation escalation tier. It shares the
`google/gemini` lineage with Gemini Flash, so it cannot independently audit
that family. The profile retains conservative AGY transport limits; this
registration does not establish comparative quality, price or available quota.

### Claude Haiku 5.5 candidate

Claude Code 2.1.293 or later is required. Discovery offers
`claude-haiku-5-5-cli`; the picker labels it **Claude Haiku 5.5** and resolves
**Default** to **low**. The role policy includes it as a reserve explorer.
For a bounded implementation trial, choose **high** and start a new conversation.
The existing conversation keeps its saved effort. Haiku is not automatically
selected as an implementer or assigned a Round Table chair by this addition.

The profile keeps a conservative 200k context and 16k output budget even though
the API supports 1M context and 128k output. Low effort is intended for short,
bounded exploration; check source coverage and early stopping before expanding
the task. High-effort implementation needs independent behavioral verification.
Validate final protocol delivery as well as edits and passing checks before
promoting this candidate. A blocked final response does not establish delivery.
The profile retains the qualified 2.1.280 transport/usage reference; Kernel checks
the actual executable against Haiku's 2.1.293 minimum before inference.

API reference prices are USD 0.10 input / 0.50 output per million tokens for
prompts up to 100,000 tokens, and USD 0.50 / 2.50 above that threshold. Prompt
length includes cache reads and writes. These prices are not the subscription's
quota consumption or incremental bill. See the official
[Haiku prompting guide](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-haiku-5-5)
and [pricing](https://platform.claude.com/docs/en/about-claude/pricing).

Sources: [Claude model configuration](https://code.claude.com/docs/en/model-config),
[Codex configuration](https://developers.openai.com/codex/config-reference), and
[AGY headless selection](https://antigravity.google/docs/cli/headless/).
