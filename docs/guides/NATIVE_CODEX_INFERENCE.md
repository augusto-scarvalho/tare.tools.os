# Codex inference with the Tare agent loop

Status: local read/edit/check and TUI qualification passed, 2026-09-20.
Not published. The broader vendor goal remains active.

The route is `TUI -> OS native session -> Runtime NativeSession -> Kernel
Responses adapter -> Codex inference`. NativeSession decides when to call the
model and dispatches its completed function calls through NativeCapabilities.
The transport has no tool runner, agent SDK or vendor CLI subprocess.

## Explicit deployment

For new `gpt-6.1-sol` conversations, prefer **Codex Direct** in the model picker
or the existing local `codex-native-gpt-6-1-sol.json` profile. The native adapter
derives one opaque cache key per Tare session and keeps complete earlier input,
tool results and encrypted reasoning in order on follow-up. Cache counters
remain observations; a stable prefix/key does not guarantee a provider hit or
establish a subscription discount. Explicit connection choices and saved
conversations keep their original profile.

Use a local deployment JSON through the TUI's custom profile field or the native
agent's `--native-deployment` option. Start a new conversation after changing
the profile. Saved conversations retain their original profile and account.

The deployment uses `runner: native-http` and:

- `native_profile.protocol: openai-codex-responses`
- `native_profile.endpoint: https://chatgpt.com/backend-api/codex`
- An explicitly selected model, context window and reasoning parameters.
- `native_streaming: true`
- `native_counter_protocol: utf8-envelope-estimate-v1`
- `native_codex_auth: {"path": "<absolute existing auth.json path>",
  "account_id": "<selected account identifier>"}`
- `native_usage_contract: {"layout": "responses-inclusive", "source":
  "<usage qualification evidence>"}`

Keep the deployment local: it contains an account identifier and login-file
location. It does not contain tokens. The default local-model route is unchanged.

Save the deployment as a profile, for example `codex-native.json`, in
`<storage>\profiles\` (`<storage>` is Tare's storage folder: `TARE_STORAGE_ROOT`
when set, `.reconciliation` next to the clone for a source install,
`%LOCALAPPDATA%\tare.tools\product\state` for an installed product), or import it
with `tare profiles import codex-native <file>`.

Open the TUI (`tare`), press **F2**, expand **Execution options**, and enter
this path in **Custom model profile** before starting a new conversation.
The recorded profile selected the `gpt-6-astra` model and configured `high` effort.
The bounded live checks explicitly used `low` effort; they did not change the
Codex configuration or the TUI's default route.

## Recorded qualification

The 2026-10-17 bounded GPT-6.1 Sol TUI trial completed an incremental workflow
executor and a targeted-execution revision. Both correction variants passed the
same 37 frozen tests. A completed response dispatched seven admitted tools;
grouped read/edit/check decisions stayed inside NativeCapabilities. Generated
GPT-6.1 Sol profiles may therefore enable `parallel_tool_calls`, as the earlier
qualified Sol profiles do. Existing explicit profiles and saved sessions keep
their selected settings.

Continuing the conversation and starting a fresh one each took four correction
calls. Reported input was 77,940 versus 34,504 tokens, with zero reported cache
reads and writes in both. This qualifies grouped tool dispatch and the observed
delivery, not cache hits, quota savings, general cost superiority or native PLAN
Replan continuity. Interrupted diagnostic attempts remain in the private record.

The registered `vendor-inference-20260920` lot contains the live inference,
NativeSession and WorkConsole evidence, including failed attempts. Both
successful tasks read a source file, changed its value, ran a real Python
verifier and proposed delivery in four inference calls. Resume did not change
the journal or repeat effects. The WorkConsole check used Textual's terminal
pilot with the real backend subprocess and saved `codex-tui.svg`; it was not a
manual visual review. No vendor agent CLI ran either task.

The affected suites passed: 100 Kernel tests and 92 OS tests. Coverage includes
opaque replay, incomplete streams, request admission, account binding, expired
login, unknown usage, cancellation and existing HTTP/Bedrock behavior.

## Semantics and limits

- The existing ChatGPT login is read for each request. Account changes, expiry
  and malformed login fail before dispatch. Refresh and OS-keyring reading are
  not implemented; renewing the login remains an authentication-only operation
  in the official client. No API-key fallback occurs.
- Credentials are sent only to the fixed subscription endpoint, in headers
  supplied to an isolated HTTP worker through an in-memory pipe. Redirects,
  retries and hosted tools are disabled. Headers do not enter model context or
  journals. This endpoint is a service interface, not a compatibility guarantee
  for arbitrary third-party clients.
- The complete request is converted before estimation and admission hashing.
  UTF-8 size is an explicit estimate, not a measured token count. `output_tokens`
  reserves context; Codex's service controls output length, so this is not a
  server-enforced generation cap.
- Completed provider output items, function IDs and encrypted reasoning are
  replayed verbatim. Streaming deltas remain provisional. A truncated stream
  cannot authorize tool execution; local cancellation preserves uncertainty
  about remote completion. Resume does not retry uncertain effects.
- Tools explicitly use non-strict Responses schemas to preserve optional
  arguments. Tare still validates every tool call before execution. See the
  [function-calling documentation](https://developers.openai.com/api/docs/guides/function-calling).
- Usage is per request, with cached input included in input totals and reasoning
  included in output totals. Missing counters remain unknown. No API tariff is
  inferred for subscription use.

This direct route is separate from the three qualified CLI adapters described
in [VENDOR_CLI_INFERENCE.md](VENDOR_CLI_INFERENCE.md). The AGY adapter uses the
official client and invocation-local extensions; it does not extract personal
OAuth credentials. Current delivery status is recorded in
the vendor-inference plan.
