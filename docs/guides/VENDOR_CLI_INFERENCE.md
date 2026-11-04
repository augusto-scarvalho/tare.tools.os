# Vendor CLI inference, with Tare owning the agent

The same native session and Work flows can use AGY, Codex or Claude through an
official CLI login. Tare sends the conversation and available tool schemas,
receives a JSON proposal, validates and executes its tools, records their
results, and decides whether to invoke the model again. Each CLI invocation is
fresh and has no task tools. No vendor SDK runs the Tare workflow.

`TUI -> OS flows/session -> Runtime NativeSession -> Kernel adapter -> vendor CLI -> LLM`

The adapter uses official configuration and extension interfaces. CLI-managed
authentication remains inside the vendor client. Tare does not extract AGY or
Claude OAuth credentials. Plugins are local to an invocation; none are installed
globally.

## Start a conversation

The [model picker](MODEL_PICKER.md) now groups models and connections and provides
reasoning preferences for new conversations. `/model` and `/reasoning` open it;
the custom profile field below remains available for explicit deployment files.

Open the TUI (`tare`), press **F2**, expand **Execution options**, and set
**Custom model profile** to one of the profiles Tare writes when it finds the
matching CLI installed (for example during `/setup`):

```text
<storage>\profiles\agy-cli.json
<storage>\profiles\codex-cli.json
<storage>\profiles\claude-cli.json
```

`<storage>` is Tare's storage folder: `TARE_STORAGE_ROOT` when set, `.reconciliation`
next to the clone for a source install, `%LOCALAPPDATA%\tare.tools\product\state` for
an installed product. `TARE_PROFILE_DIR` selects another profile folder.

Start a new conversation. The project selection controls the repository Tare
can access; invocation configuration files remain temporary. Codex uses their
stable parent as its working directory so the environment context does not
change on every call. Native task tools remain disabled.
The default model remains unchanged. Existing sessions retain their saved
profile. The same file can be selected with `--native-deployment` for native
sessions and prepared Work execution, including sequences of dependent Works.

Continue a task in its existing conversation using **Send message** and
**Resume**. Earlier messages and tool results remain in order; new criteria and
source-change notices are appended. CLI catalogs and formatter schemas also
remain stable when the last budgeted call requires delivery: its action
constraint is appended after history and checked by the host before effects.
Codex edit decisions use the stable `observed` reference; the host resolves the
exact source version and still checks it against disk before writing.

This preserves eligibility for provider prompt caching. Provider expiration,
eviction and routing can still cause misses. Read cache counters in the saved
usage receipt; raw token totals do not establish monetary cost or a subscription
quota discount. Switching model/provider or required context compaction starts
a different prefix. Normal runs retain full history; experimental output elision
is off by default. Do not restart exploration merely to apply a localized change.

For repeated Codex calls, prefer the existing [native Responses connection](NATIVE_CODEX_INFERENCE.md).
Its cache key stays stable within the Tare session. The qualified CLI currently
assigns a new key to each fresh invocation even when its text prefix is stable;
cross-invocation cache reuse has not been established. The model picker lists
the native connection first for a new choice; saved conversations and explicit
connection preferences retain their selected route.

| Route | Qualified CLI | Selected model | Effort |
| --- | --- | --- | --- |
| AGY | 1.2.7 | `gemini-3.8-flash-high` | high |
| Codex | 0.154.0 | `gpt-6-astra` | low |
| Claude | 2.1.278 | `claude-opus-5` | low |

Codex, Claude and AGY updates no longer require editing a version pin. Their saved
version is a reference: the adapter checks the actual installed version and,
for newer releases, the required public switches before inference. Response,
model and tool-boundary checks still apply on every call. The receipt records
both versions. Codex queries the installed feature inventory with `features list`
in a disposable home, disables optional features including new ones, and verifies
the effective controls before inference. Only the existing two isolation controls
remain explicitly enabled; the model catalog exposes no task tools. Feature
discovery uses no provider calls and is reused for an unchanged executable version
within a client. A real missing/incompatible control can still block with
`CLI_CAPABILITY_UNAVAILABLE`; changing the version number alone does not.

If an earlier conversation stopped with `CLI_VERSION_CHANGED`, reopen it and
use **Resume** (`/resume`). With the updated Runtime/Kernel/OS composition, this
authorizes one new attempt only when the failed call was recorded as not sent.
The original failure, prior effects, history and cumulative budgets remain.
Uncertain dispatched calls and tool effects do not use this recovery path.

Live development TUI checks on 2026-09-25 passed at 80x24 with Claude 2.1.282
(Opus 5.5) and AGY 1.2.11, using the unchanged 2.1.280/1.2.7 profiles. The reported
blocked Claude session resumed in place; no file tools ran in any of these checks.

On 2026-09-26, the development TUI also completed two real turns with Codex
0.157.0 and the unchanged 0.154.0 profile, recalling the first user message on
the second turn without file effects. A separate local fake-provider check
confirmed the real CLI request advertised no tools. Required-switch removal,
ineffective feature overrides, new features, obsolete settings and successive
version changes have deterministic regressions in Kernel's
`tests/test_native_cli_codex.py`.

Login and renewal use the official client. API-key
fallback environment variables are removed; the adapter does not purchase
credits or substitute another model when the subscription cannot serve one.

## Deployment contract

Keep executable paths and deployment files local. This example uses Claude;
AGY uses `cli://agy`, its qualified executable/version/model and high effort.

```json
{
  "id": "claude-cli",
  "runner": "native-http",
  "model": "claude-opus-5",
  "native_profile": {
    "protocol": "vendor-cli",
    "endpoint": "cli://claude",
    "model": "claude-opus-5",
    "source": "Claude CLI 2.1.278 qualified controls",
    "context_window": 200000,
    "output_tokens": 16384,
    "parameters_json": "{\"effort\":\"low\"}"
  },
  "native_counter_protocol": "utf8-envelope-estimate-v1",
  "native_cli": {
    "executable": "C:/absolute/path/to/claude.exe",
    "version": "2.1.278"
  },
  "native_execution_limits": {
    "max_model_calls": 16,
    "max_seconds": 300,
    "request_seconds": 120
  }
}
```

`native-http` is the existing host selector; `vendor-cli` chooses the process
transport. Codex additionally requires `native_cli.model_catalog`, the exact
selected record from its local model catalog. The adapter overrides that
record's tool capabilities; it does not trust the user's tool defaults.
Prepared Works also require the top-level model's matching `runtime.native`
entry in the OS roster. Saved CLI deployments do not pin a subscription account:
the official client's current login is used for each invocation.

## How the boundary is enforced

- **AGY:** a custom agent excludes default components and declares no tools.
  `--add-dir` makes the invocation-local agent discoverable. A plugin terminates
  after `PostInvocation`, denies `PreToolUse`, and records the pre/post/stop
  sequence. A missing boundary receipt or tool event rejects the response.
  The CLI's initialization event lists its global tool registry even when the
  custom agent excludes those tools; that list alone is not qualification.
- **Codex:** `exec --ignore-user-config --ephemeral`, a controlled model catalog,
  replacement instructions and explicit feature/tool controls remove task
  tools, skills, plugins and agent delegation. Qualification inspected both
  top-level tools and `additional_tools` in the serialized request. The JSONL
  decoder accepts one completed turn and rejects tool events and failed turns.
- **Claude:** empty tools, strict empty MCP, empty setting sources, disabled
  hooks/plugins/slash commands and replacement system instructions. The decoder
  requires empty tools/MCP, one assistant inference and a successful result.
  `--bare` is not used because it would disable the installed subscription login.

Sources: [AGY plugins](https://antigravity.google/docs/plugins),
[AGY hooks](https://antigravity.google/docs/hooks),
[AGY agents](https://antigravity.google/docs/subagents?tab=cli),
[Claude CLI reference](https://code.claude.com/docs/en/cli-reference),
[Codex configuration](https://developers.openai.com/codex/config-reference),
[Codex 0.154.0 tool construction](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/core/src/tools/spec_plan.rs).

## Limits and evidence

- Each completed proposal returns to Tare. CLI authentication, hidden framing
  and transport retries remain vendor behavior. Backend request counts are
  unknown, not equated to the number of CLI invocations.
- Preflight hashes/counts the submitted public input. UTF-8 estimation adds a
  4,096-unit reserve for hidden context; this is not an exact vendor token count
  or guaranteed upper bound. CLI output is buffered before validation; partial
  output never authorizes effects. Image input is not qualified on these routes.
- Raw vendor usage is retained. Normalized CLI usage/cost is currently unknown;
  it must not be displayed or interpreted as zero. Codex JSONL does not attest
  the actual backend model: its model identity is request-pinned, unverified.
  AGY and Claude report the matched model.
- Cancellation and finite per-invocation deadlines use the existing managed
  process containment. Local cleanup is verified; remote inference completion
  after an interruption remains unknown. Workflow “unlimited” does not remove
  the finite CLI invocation timeout.
- Failed, truncated, malformed, tool-bearing or extra-invocation responses are
  rejected. Claude qualification used low effort and one assistant message;
  another output shape fails closed rather than executing a partial proposal.

Qualification artifacts, including unsuccessful probes, are retained in the
registered `vendor-inference-20260920` experiment lot. See the current acceptance
status in VENDOR_INFERENCE_PLAN.md. The separate
[direct Codex HTTP route](NATIVE_CODEX_INFERENCE.md) is not the CLI proof.

External frontends through a Tare CLI/MCP remain a separate
follow-up. Usage normalization, additional model/version
qualification and richer progress streaming are also deferred.
