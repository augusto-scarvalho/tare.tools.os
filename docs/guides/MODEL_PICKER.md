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

A first-party model's name and levels come from its line in `relay/model_lines.json` (Claude Fable, GPT Terra,
Gemini Argon, ...), found from the model id whatever the connection: `claude-fable-5-3` is **Claude Fable 5.3**,
`gpt-6.2-terra` **GPT-6.2 Terra**, AGY's `claude-opus-5-5-high` **Claude Opus 5.5**, with no entry per version.
Where the vendor lists levels per model (Codex's catalogue, AGY's ids), its list wins over the line's. An id of no
known line keeps its raw name (`GPT-` prefixed under OpenAI connections) and only its profile's levels.

The line also gives such a model its author when the roster has no record for it, so its work can be audited by
another line (see [the relay mesh](RELAY_MESH.md#local-operator-trust-boundary)). That holds only on the vendor's
own CLI or API, or AGY: a reseller's id stays of unknown author even when spelled like a line. No automatic role
picks a model for that: automatic roles come from `relay/role_deployments.json` rows, which name roster records.

Codex Direct and Codex CLI are connections under the same model. CLI catalogues
may expose agent modes as well as reasoning: `ultra` and Claude `ultracode` are
excluded. Tare keeps the agent loop, tools and flows. No automatic connection,
account, model or paid API fallback is introduced.

## Additional vendor models

`scripts/configure_vendor_models.py` expands existing local connections using
an explicit local Codex catalogue and the documented Claude IDs:
`claude-opus-5-5`, `claude-sonnet-5-5`, `claude-fable-5-1`, `claude-fable-5-2`
and `claude-haiku-5-5`. Existing base profiles stay available. Codex entries marked hidden are excluded; each CLI variant retains
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

### Claude Code: a new version without a template

Claude Code lists no models, so each Claude model has a template in `relay/native_deployments/vendor_cli_templates.json`.
Discovery also asks the installed Claude Code which model id each family alias (`fable`, `opus`, `sonnet`, `haiku`)
resolves to: one start per alias against a local recorder that refuses every request, so no token is spent. The
reading is kept in discovery's cache (`cli-discovery.json`, key `claude_aliases`) for that executable file and is
taken again only when the file changes (a Claude Code update); a reading that fails is kept as empty until then and
the templates alone are used. An id that is a newer version of a line the templates have (`claude-fable-5-3` beside
`claude-fable-5-2-cli`) gets the profile `<id>-cli`, copied from the newest template of that line, with the id and
model replaced and a `source` that says it came from the alias reading (account availability unchecked). It is
managed like any discovered profile: retired when a later reading no longer resolves to it, never touched once you
edit it. Its role row is then derived as for any newer version (see
[shared GPU](SHARED_GPU.md)): pin only until proven, which the TUI does when it arrives (below).

### AGY: a new version without a template

AGY lists its models for the signed-in account (`agy models`, no model call). Discovery runs it through the located
executable and keeps the ids in the same cache (`cli-discovery.json`, key `agy_models`). It reads again when the
executable file changes or the last attempt is a day old, since the list depends on the account too; a read that fails,
times out or lists no id keeps the last ids (and counts as the attempt, so it is not repeated for a day). Ids are grouped
by base (the id without its effort suffix). A base that is a newer version of a line than every AGY template of that
line (`gemini-4.1-argon` beside `agy-gemini-4-argon-cli`, `claude-opus-5-6` beside `agy-claude-opus-5-5-cli`) gets
the profile `agy-<base>-cli` (dots become hyphens), copied from that line's newest template: `model` is the listed id
at the template's effort (the nearest listed one when AGY does not list it, and the `source` says which), and
`native_reasoning_models` is rebuilt from the listed ids for the efforts the template maps only (never an effort it
lacked). It is retired only when a later successful read no longer lists the base, and never touched once you edit it.
An older or equal version, an id with no known line (`gpt-oss-120b-medium`) and a line with no AGY template change
nothing. Its role row is derived as for any newer version: pin only until proven.

### API: the same version on the vendor's API

A vendor's own CLI is the whitelist for its API. After the CLIs are read, discovery takes the ids they list in the
same run (Codex's catalogue slugs, the ids Claude Code's aliases resolve to, AGY's Gemini bases without the effort
suffix) and keeps those that are a newer version of a line than every bundled API profile of it (`openai-api`,
`claude-api*`, `gemini-api*`, Pro variants included in the comparison). Only for a vendor with such an id, whose key
is stored (environment or vault) and whose provider `tare setup` leaves in, discovery sends one `GET <endpoint>/models`
(Anthropic: `x-api-key` and `anthropic-version`, `limit=1000`; OpenAI and the Gemini OpenAI-compatible endpoint:
`Authorization: Bearer`) to the host of the bundled profile's endpoint, over https, 15 seconds, no redirect. The key
is never in a URL, a log, an error text or the cache; discovery's cache (key `api_models`) keeps the ids, the read
date and the last attempt, and reads again only when the attempt is a day old (a read that fails or lists no id keeps
the last ids and counts as the attempt). The API's list never decides which models exist, it only confirms an id the
CLI listed. A confirmed id gets `claude-api-<id without claude->`, `openai-api-<id>` or `gemini-api-<id without
gemini->` (dots become hyphens), copied from the line's newest bundled API profile that is not Pro, with the id and
both model fields replaced and a `source` that says it mirrors the id the CLI lists, confirmed on the API's list on
that date; limits and parameters are the sibling's and no price is added. It is retired when the CLI no longer lists
the id or a successful read no longer has it, and never touched once you edit it. Its role row is derived as for any
newer version (the non-Pro sibling's row, since only that sibling has one): pin only until proven.

### A new version is proven when it arrives

The TUI proves such a route itself, so a new version does not wait for you to find it in `/routes`. When it starts
in a terminal, right after discovery and the price reading, it looks for **arrivals**: routes with a derived role row
(a newer version of a known line on its vendor's own CLI or API) that have no proof record at all, that can run now
and that the routing profile leaves on for at least one of the row's roles. A route proven, failed or declined
before is never tried or asked again by itself; `p` in `/routes` > "Models per role" and `tare setup --prove` still
prove it again.

- **On a subscription** (a route that counts against a plan, such as Claude Code, Codex or AGY) the proof runs with
  no question, one route after another. The status line names the route and its cost line first (one minimal call and
  one short task of at most 6 calls, on that plan's account). A notice then gives the outcome: passed (the route is
  used automatically where its line is ticked in `/routes` and is now its line's newest version), or failed with the
  reason (it stays pin only). This spends the plan's allowance without asking, once per new model version.
- **On a pay-per-token route** nothing is called without a yes. The Prove dialog of `/routes` opens once, before the
  subscription proofs start, and lists each such route with its cost line. Prove runs the same proofs. Cancel or Esc
  records `{"ok": null, "reason": "not confirmed"}` under `proofs` in the setup, so the next start does not ask
  again. A declined route counts as not proven everywhere.

To keep tare from proving anything by itself, start it with `TARE_CLI_DISCOVERY=0` (which also turns discovery off),
or switch the line off in `/routes` for every role its row has. Closing the TUI starts no further proof, but it
waits for the one already running to finish (its task stops at 5 minutes). `tare serve`, `tare setup` and any entry
with nobody present prove nothing by themselves.

Two TUIs opened together on the same setup prove a route once: the proof reserves the route under the setup's
lock before its call, and the other window calls nothing. A reservation left by a tare that died mid-proof lapses
after 30 minutes.

### Gemini 4 Argon

AGY discovery offers `agy-gemini-4-argon-cli`, shown as **Gemini 4 Argon**.
The exact IDs from `agy models` on 2026-10-17 support **low**, **medium** and
**high**; **Default** resolves to **high**. Selecting a reasoning level changes
both the model ID and effort together.

Its line is in the middling class, with Sol and Opus: it reviews plans and
code, and it is an implementer of tare auto's first two floors (at least cheap, at least middling). It shares the
`google/gemini` lineage with Gemini Flash, so it cannot independently audit
that family. The profile retains conservative AGY transport limits; this
registration does not establish comparative quality, price or available quota.

### Fable 5.2, GPT-6.1 Terra and Gemini 4 Hydrogen

Discovery offers `claude-fable-5-2-cli` (**Claude Fable 5.2**, Default **high**; Claude Code 2.1.325 lists it),
`codex-cli-gpt-6-1-terra` (**GPT-6.1 Terra**, from Codex's own model list) and `agy-gemini-4-hydrogen-cli`
(**Gemini 4 Hydrogen**, low, medium and high from `agy models` on 2026-11-13; AGY's `xhigh` is left out because
the Kernel takes AGY efforts up to high). Gemini 4 Carbon has no CLI route: AGY 1.5.8 does not list it; it is on
the Gemini API ([HTTP providers](HTTP_API_PROVIDERS.md)). These routes can be chosen and pinned to a role; no
automatic role picks them until they are qualified through tare (`"qualified": false` in
`relay/role_deployments.json`). Hydrogen shares the `google/gemini` lineage, Fable 5.2 `anthropic/fable` and
Terra `openai/gpt`, so none reviews work of its own line.

### Claude Opus 5.5 and Sonnet 5.5 on AGY

AGY also serves Anthropic's Claude Opus 5.5 and Claude Sonnet 5.5 (`agy models` on 2026-11-13 lists
`claude-opus-5-5-low|medium|high` and `claude-sonnet-5-5-low|medium|high`). Discovery offers
`agy-claude-opus-5-5-cli` and `agy-claude-sonnet-5-5-cli`; the picker shows each as one more connection, **AGY CLI**,
of **Claude Opus 5.5** and **Claude Sonnet 5.5**, under Anthropic, with low, medium and high (Default **high**). A
level selects AGY's id for it. They spend AGY's allowance (its "Claude and GPT models" group in `tare quota`), not the
Claude subscription's, so they are a fallback when that one is spent. Their author is Anthropic: Opus on AGY shares
`anthropic/opus` with Opus on Claude Code and cannot review its work (nor Sonnet Sonnet's), while it can review GPT
or Gemini work. A pin names the connection, which is Google's: `google/agy-claude-opus-5-5-cli@high`. They have the
roles of `claude-opus-5-5-cli` and `claude-sonnet-5-5-cli` with `"qualified": false`: chosen or pinned only, never
by an automatic role. One minimal call each answered on 2026-11-14 (AGY 1.5.8, high), and AGY reported the
requested model.

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
