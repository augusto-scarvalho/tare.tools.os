# HTTP providers — OS 0.1.95

Requires `tare-kernel>=0.9.50,<0.10`. No additional SDK is required.

| Explicit profile | Model | Protocol | Endpoint | Credential |
|---|---|---|---|---|
| openai-api | gpt-6.1-sol | openai-responses | https://api.openai.com/v1 | OPENAI_API_KEY |
| gemini-api | gemini-4-argon | openai-chat | https://generativelanguage.googleapis.com/v1beta/openai | GEMINI_API_KEY |
| claude-api | claude-haiku-5-5 | anthropic-messages | https://api.anthropic.com/v1 | ANTHROPIC_API_KEY |

## Models in the selector

In **Settings** (`F2`), click **Choose model and reasoning…**, search for the
model name and choose the **Claude API**,
**OpenAI API** or **Gemini API** connection. All models from one vendor use the same
key registered in `/setup`; you do not need to register it again.

| Vendor | Model in the TUI | ID sent to the API | Explicit profile |
|---|---|---|---|
| Anthropic | Claude Haiku 5.5 | claude-haiku-5-5 | claude-api |
| Anthropic | Claude Sonnet 5.5 | claude-sonnet-5-5 | claude-api-sonnet-5-5 |
| Anthropic | Claude Opus 5.5 | claude-opus-5-5 | claude-api-opus-5-5 |
| Anthropic | Claude Fable 5.1 | claude-fable-5-1 | claude-api-fable-5-1 |
| Anthropic | Claude Fable 5.2 | claude-fable-5-2 | claude-api-fable-5-2 |
| OpenAI | GPT-6.1 Sol | gpt-6.1-sol | openai-api |
| OpenAI | GPT-6 Astra | gpt-6-astra | openai-api-gpt-6-astra |
| OpenAI | GPT-6.1 Terra | gpt-6.1-terra | openai-api-gpt-6-1-terra |
| OpenAI | GPT-6 Luna | gpt-6-luna | openai-api-gpt-6-luna |
| OpenAI | GPT-6.1 Sol Pro | gpt-6.1-sol | openai-api-gpt-6-1-sol-pro |
| OpenAI | GPT-6 Astra Pro | gpt-6-astra | openai-api-gpt-6-astra-pro |
| Google | Gemini 3.8 Flash | gemini-3.8-flash | gemini-api-3-8-flash |
| Google | Gemini 4 Argon | gemini-4-argon | gemini-api |
| Google | Gemini 4 Carbon | gemini-4-carbon | gemini-api-4-carbon |
| Google | Gemini 4 Hydrogen | gemini-4-hydrogen | gemini-api-4-hydrogen |

IDs confirmed in the catalogs of the three APIs on 2026-10-23. The OpenAI catalog
exposes **GPT-6 Luna**, with no `gpt-6.1-luna` ID. The Google Live/TTS models are
not part of this conversation and implementation selector.

Fable 5.2, GPT-6.1 Terra, Gemini 4 Carbon and Gemini 4 Hydrogen were added from the same catalogs on
2026-11-13. They can be chosen and pinned to a role, but no automatic role picks them until they are qualified
through tare (`"qualified": false` in `relay/role_deployments.json`). Gemini 4 Hydrogen's model page also lists
`xhigh`; this compatibility protocol offers low to high. Like the other first-party API profiles, they carry no price.

**Pro** uses the same model with `reasoning.mode: "pro"` in the Responses API,
as described in the [OpenAI documentation](https://developers.openai.com/api/docs/guides/reasoning#reasoning-mode).
It is independent of the effort level and can use more tokens and take longer.
The Pro options have their own preferences in the TUI; changing the effort keeps the mode.
A listed model does not prove inference or access to every mode on the account.

The three original profiles still point to Sol 6.1, Haiku 5.5 and Argon.
Saved conversations keep their profiles and histories. These additional models do not
change the automatic role policies. Claude API uses the provider's default thinking
and effort; OpenAI and Gemini expose the levels Tare already supports.

The context limit of 131072 and the output limit of 8192 are conservative Tare ceilings,
**not** provider maximums. Other parameters supported by ModelProfile
remain configurable in your own profiles; reasoning must follow the protocol and
not reuse another provider's format indiscriminately.

## Keys and connection

In `/setup` → **Connections**, use **Add API key** next to the provider; if a key
is already saved, the button reads **Change key**. The status shows whether the key is
saved in the vault, configured outside Tare or missing. The field is masked,
cleared on save/cancel, and writes only to the existing keyring, namespace
`universal-agent-harness` (compatibility). No key goes into the setup JSON,
history or export. Environment variables take precedence over the vault:
remove the override to use the saved key. Unsaved Use/Do not use decisions
are kept while you edit the key. Authenticate in the header still
uses the same panel. Saving a key does not save or change the routing choices.

**Check connection** only does GET `/models`, with Authorization: Bearer for all three providers and,
for Claude, `anthropic-version: 2023-06-01`. It does not generate or retry.
A listed model does not prove inference. `native_anthropic_workspace` lets you set
`anthropic-workspace-id` for multi-workspace keys.

## Sessions, usage and cache

Text and tools share the client composition and authentication. Usage for OpenAI:
responses-inclusive; Claude: anthropic-exclusive; Gemini: chat-inclusive, only
for the compatible counters reported; missing values stay unknown.
A prior estimated count is not observed usage or proof of billing.

Claude Messages runs **without streaming**: the console's public entry accepts its
`--stream` preference but does not apply it to this protocol. The response arrives complete.
Gemini Argon has implicit cache, with no explicit cache configuration. Claude uses
automatic `cache_control` by default; `cache_system` remains available in your own profiles.
OpenAI uses the Kernel's per-session cache affinity. History must be preserved,
without rebuilding or summarizing it to force cache. No hit or saving is guaranteed.

Codex/ChatGPT is a separate connection with its own login: there is no login swap
and no fallback to the API. Saved connection preferences are still respected.
The selector offers the reasoning levels each profile declares: OpenAI sends
`reasoning.effort`; Gemini sends `reasoning_effort`. The presets are not added to the automatic policy. No paid probe runs when
opening/configuring. The reply test is under **Advanced**: **Check test cost**
shows the cost and **Run reply test** requires an explicit operator action.
Gemini HTTP uses exactly `gemini-4-argon`, never an AGY low/medium/high ID.

## Verification

The offline suite uses loopback and a simulated vault; no commercial call, download
or real credential is needed. The public dogfood also uses loopback and an isolated
area of the Windows vault, without replacing the operator's credentials.

Provider references: [OpenAI Responses/GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol),
[Gemini OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai),
[Gemini Argon](https://ai.google.dev/gemini-api/docs/models/gemini-4-argon),
[Gemini Carbon](https://ai.google.dev/gemini-api/docs/models/gemini-4-carbon),
[Gemini Hydrogen](https://ai.google.dev/gemini-api/docs/models/gemini-4-hydrogen),
[GPT-6.1 Terra](https://developers.openai.com/api/docs/models/gpt-6.1-terra),
[Claude auth](https://platform.claude.com/docs/en/manage-claude/authentication) and
[Claude cache](https://platform.claude.com/docs/en/build-with-claude/prompt-caching).
