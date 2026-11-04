# CLI and lean MCP

Use the same Tare runtime from the TUI, a terminal or an MCP client. Tare owns
the inner loop, tools, history and limits; configured vendor CLIs provide
inference through their existing logins. See [vendor inference](VENDOR_CLI_INFERENCE.md).

## Start from another project

The `tare` command (installed from source or from a bundle; see the [README](../../README.md#getting-started))
works from any folder and keeps the caller's working directory. In PowerShell:

```powershell
$tare = 'tare'
& $tare profiles list
& $tare profiles show codex-cli
& $tare agent --root C:\path\to\your-project --deployment codex-cli --role explorer --no-context --instruction 'Explain the architecture with source locations.'
```

`--no-context` selects ordinary file/search tools when the project has no
SpecGraph index. Omit it to use structural context. An editable session can
authorize specific paths and one named verifier:

```powershell
& $tare agent --root C:\path\to\your-project --deployment claude-cli --role implementer --write-path src --write-path tests --no-context --max-model-calls 12 --max-seconds 300 --instruction 'Fix the reported failure and run the tests.' --verify-command python -m pytest -q
& $tare sessions --root C:\path\to\your-project --format json
& $tare profiles usage --session '<session path from the result>'
& $tare agent --session '<session path>' --resume --follow-up 'Explain the change.' --message-id followup-1
```

The existing CLI also exposes `plan`, `explore`, `run`, `sequence`, `review`,
`next` and `explain`; use `<command> --help` for their existing contracts.
Work execution retains its own preparation/admission requirements. Selecting
an arbitrary project for a conversation does not grant Work admission.

New native sessions can compact context more than once within their original
model-call and time limits. Summary calls consume the same total call budget.
The default summary ceiling is `max_model_calls - 1` (minimum zero), or 16 when
the call budget is explicitly unlimited; an explicit `context_policy.max_compactions`
overrides it. Bounded working source excerpts help retain the next edit's context.
Existing sessions preserve their saved policy and deadline, including the older
one-summary ceiling. Start a new session to use these recovery defaults; resuming
a blocked session does not grant it new budget.

Installed environments use `tare` instead of the source launcher. Install
the optional MCP dependency into the same Python environment as Tare:

```text
python -m pip install "tare-tools-os[mcp]"
```

For source development, install the checkout with `python -m pip install -e ".[mcp]"`.

## Profiles

Built-in names remain available. Local names resolve to
`TARE_STORAGE_ROOT/profiles/<name>.json`; `TARE_PROFILE_DIR` can select another
absolute directory. Without `TARE_STORAGE_ROOT`, a source install keeps its storage in
`.reconciliation` next to the clone.

```text
tare profiles import my-model /absolute/path/to/qualified-deployment.json
tare profiles show my-model
tare agent --deployment my-model --root /project --instruction "..."
```

Import validates an existing deployment and refuses to overwrite a name or
shadow a built-in. Profile inspection does not log in, contact a provider,
reveal auth paths/accounts, or change the default route. Invalid local files
are reported by `profiles list` and omitted from the model picker. CLI version
and executable presence are configuration checks, not availability guarantees.

## Delivery cost

For equivalent tasks, `tare profiles compare study.json` derives total delivery
cost from native session receipts, including failed attempts, corrections and
reviews. Add `--write-policy new-policy.json` to export a supported preference
for that task cohort, without another model call. See the
study format and activation procedure.

## MCP setup

The server uses the maintained v1 [official Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)
with a `<2` dependency boundary. It exposes stdio only: stdout carries protocol
messages, stderr carries diagnostics. No listener, remote auth or daemon is added.

Example `mcpServers` entry for a Windows client (adapt the enclosing format to
your client):

```json
{
  "mcpServers": {
    "tare": {
      "command": "C:\\path\\to\\tare.tools.os\\.venv\\Scripts\\tare.exe",
      "args": [
        "mcp", "--root", "C:\\path\\to\\your-project",
        "--deployment", "codex-cli", "--deployment", "agy-cli",
        "--no-context", "--max-model-calls", "12", "--max-seconds", "300"
      ]
    }
  }
}
```

Install the optional MCP dependency first (`python -m pip install -e ".[mcp]"` in the source checkout). If `tare`
is on the client's PATH, `"command": "tare"` is enough on any platform. Storage must be outside the project. The server reads
its profiles once at startup; restart it after intentional profile changes.

The default is read-only. Startup options `--write-path src --write-path tests`
grant scoped edits; `--verify 'unit=["python","-m","pytest","-q"]'` grants a
named command. Verifiers are real host processes: their side effects are not
restricted to the edit paths. Only configure commands you intend to authorize.
MCP callers cannot choose roots, executables, credentials, edit paths, shell
commands or verifiers. The server has no general terminal capability.

| Tool | Purpose |
| --- | --- |
| `tare_profiles` | Inspect enabled profiles, permissions, default limits and usage coverage; no model call. |
| `tare_run` | Run one task with an enabled profile; supply a stable `request_id`. |
| `tare_inspect` | Read a known request's saved result, usage and limits; no model call. |
| `tare_review` | Inspect recorded edits, checks and freshness; select an edit effect for its diff. No model call or test execution. |
| `tare_usage` | Inspect deduplicated usage and invocation attribution; missing pricing and acceptance remain unknown. No model call. |
| `tare_continue` | Resume or supply a follow-up with a stable `message_id`; preserve the session's limits and permissions. |
| `tare_cancel` | Request cancellation of this server's active task. Acknowledgement does not prove cleanup. |

Example tool arguments:

```json
{"request_id":"architecture-1","instruction":"Explain the architecture with file references.","profile":"codex-cli","max_model_calls":6,"max_seconds":180}
```

After an editable `tare_run` completes, call `tare_review` with the same
`request_id`. Its `report` contains edit metadata, recorded checks, their freshness
and unresolved effects. Add an `effect_id` from `report.edits` to request that
edit's diff; add `invocation_id` to narrow the review to a recorded invocation.
Unknown edit/turn IDs are refused. Diffs come from the saved edit snapshots,
excluding intervening external changes. Missing or corrupt snapshots remain
explicitly unavailable. A recorded PASS covers the verifier's observed files;
it is neither a new test run nor independent Work acceptance.

Call `tare_usage` with the same `request_id` for session totals and invocation
attribution in `report.recorded_sessions`. Repeated inspection does not spend
model calls, repeat tools or reset the original deadline. Both evidence tools
retain the original scope/profile and refuse active session writers. Each report
returns `authority: NONE` and `acceptance: unknown`.

Evidence responses are bounded to 32 KiB of JSON text. Oversized reports return
`error: response_too_large` and a saved-session reference instead of a partial
report presented as complete. Narrow review by invocation/edit, or use the
existing `tare review --session PATH` / `tare profiles usage --session PATH
--aggregate` CLI for the full report. Individual diff computation is bounded to
64 KiB of snapshot content. These ceilings apply to the new evidence tools;
`tare_inspect` retains its existing result-preview contract.

One execution runs at a time per server; another is refused with a busy error.
Configure the client's tool timeout to accommodate the selected server budget
and cleanup. A client-side timeout alone need not send an MCP cancellation;
use `tare_cancel` or the client's explicit cancel action if work should stop.
Duplicate start/message IDs return inspection without new model calls or
effects. Reusing an ID for different input is refused. After a lost response,
inspect the same ID. A new continuation ID explicitly authorizes another
invocation; incomplete or uncertain effects remain subject to Runtime recovery
rules. This MCP surface does not discard interrupted model requests automatically.

MCP request cancellation and graceful transport shutdown cancel the native
session and wait for the worker to finish. A force-killed host cannot provide
that acknowledgement. Saved outcomes remain inspectable after restart with
the same project, permissions and profile.

## Usage and limits

- Defaults: **16 model calls, 300 seconds**, no model retry, three stagnant
  turns. MCP startup can set finite call/time ceilings. Each request can lower
  its budget; unlimited profile values cannot remove server ceilings.
- Limits cover the **saved session**, including follow-ups. The original wall
  clock deadline continues while idle. Continuation never refreshes a budget.
  Start a new request ID when you intentionally need a new budget.
- Inspection returns recorded token totals, accounting coverage and known cost.
  Unknown measurements remain unknown. `profiles usage --session PATH` reads
  the same custody without a model call and refuses an active writer.
- Vendor CLI raw usage is not yet a normalized accounting contract. Subscription
  balances, reset times, hidden provider retries and total vendor-side cost are
  **unknown**. Tare call limits count Tare dispatches, not hidden transport work.
  CLI context estimates are estimates, not measured provider context.
- MCP results are bounded proposals, never a claim of commit, merge or Work
  admission. Output previews are truncated at 8,192 characters; the full native
  outcome and review records stay in the returned session directory.

During the live MCP pilot, Codex successfully read through the Tare tools but
declined an edit because it interpreted its CLI's read-only sandbox as a task
restriction. That run proposed a blocked result; it is not evidence of a
completed edit. The MCP adapter does not override vendor restrictions or treat
`PROPOSED` as acceptance. Use the recorded output and verifier results to assess
completion. The independent AGY pilot covers the editable MCP path.

## Deferred scope

The CLI retains complete existing Work/sequence flows. This lean MCP delivery
exposes native task sessions, not Work admission, arbitrary flow dispatch,
subscription-quota scraping, automatic profile switching, or remote hosting.
Add those only when a concrete task needs them.

### Usage across stages and attempts

```powershell
tare profiles usage --session C:\path\to\session --aggregate
tare profiles usage --session C:\path\to\first --session C:\path\to\second
tare profiles usage --train C:\path\to\train
```

A single `--session` without `--aggregate` retains the previous output. Aggregated
inspection reads existing receipts locally; it makes no model calls. `--train`
reads `WORK_RUN.json` and its recorded plan, implementation and audit custody,
including previous plan/audit attempts. It does not discover unrelated sessions.

Totals deduplicate `(session_id, call_id)`. Per-stage views may overlap and must
not be added together. Direct invocation `dispatch_usage` uses its saved journal
range, attributing a call to the invocation that requested it. Older Work attempt
records without journal ranges retain unknown per-invocation usage; their session
totals still include recorded failures, summaries and retries. Missing sessions,
missing counters and unpriced calls stay explicit. Empty currency totals are not
zero cost. A proposal or a model's `PASS` label is not independent acceptance.
Inspection refuses busy custody rather than reading an inconsistent active run.

### Bounded plan-review source recovery

New managed Work plan reviews can recover once when an otherwise accepting
proposal omits exactly one required citation (`PACKET.md` or
`EXECUTION_DESCRIPTOR.json`). Tare reads that admitted source and continues the
same session through an explicit host observation. Model, deadline and cumulative
call limits stay fixed. The corrected review still passes the existing source
and plan gates. This does not automatically accept implementation candidates.

`source-recovery.json` records the choice; `source-recovery-read.jsonl` retains
read intent/result in the same custody. Interrupted reads require reconciliation;
resume does not dispatch another read. Source changes and oversized context
refuse consumption. Native result text and aggregate usage inspection expose the
saved action. Existing sessions without this policy retain their behavior.

Set `TARE_PLAN_SOURCE_RECOVERY=0` in the launching environment to disable new
recovery dispatches. A read already completed under the enabled policy is still
reconciled and consumed under its original limits. Clearing the variable restores
the default for new plan sessions.
