# Provider quota and credit

`tare quota` shows what each provider says you have left: the windows of each subscription and the credit of each
API account. It asks the providers directly and makes no model call, so it spends nothing.

```powershell
tare quota            # ask every provider now (about ten seconds)
tare quota --cached   # the last readings, without asking again
tare quota --json     # the same readings as JSON
tare quota --set openai-api 25   # state an API balance in dollars; tare counts its own spend down from it
```

In the console, `/usage` opens on its first row, **Provider quota and credit**. It shows the saved readings at once
and asks every provider again in the background (no model call, about 15 seconds); the row changes when the new
reading lands. Reopen `/usage` to read again.

```text
Subscriptions
  Claude subscription
    week, all models                  57% left   resets Sun 15 Nov 17:59
    week, Fable                       62% left   resets Sun 15 Nov 17:59
  Codex subscription · prolite
    week                             none left   resets Thu 12 Nov 18:21
    2 free limit resets available
  AGY subscription
    Gemini Models, week               49% left   resets Fri 13 Nov 22:20
    Gemini Models, 5 h               100% left   resets 13:13
    Claude and GPT models, week      100% left   resets Tue 17 Nov 08:13
    Gemini 4 Argon, week             none left   resets Sat 14 Nov 01:57
  OpenCode subscription: not reported. OpenCode publishes no remaining allowance; a reply only says what it used.

API credit
  OpenRouter: US$ 5.23 left of US$ 20.00
  OpenAI API: not reported. This provider has no balance endpoint for an API key.
```

## Where each reading comes from

| Provider | Source | What it reports |
| --- | --- | --- |
| Claude subscription | `claude -p /usage` | Percent used of each window. A model with its own quota (Fable) has its own line. |
| Codex subscription | `codex app-server`, request `account/rateLimits/read` | Percent used and reset time of each window, per limit id; the plan; free limit resets. |
| AGY subscription | `agy -p /quota` and `/credits` | Fraction left of the weekly and 5-hour windows of each model group, and AI credits. |
| OpenRouter | `GET https://openrouter.ai/api/v1/credits` | Dollars bought and dollars used. |

A window covers the models its line names. On Claude, a Fable call needs room in both the Fable window and the
all-models window. On AGY, the Gemini models, the third-party models (Claude Opus, Claude Sonnet, GPT-OSS) and
Gemini 4 Argon each have their own windows; on a Pro plan Argon's weekly quota is separate from the Gemini group.

Times are shown in this machine's time zone. Claude prints its reset as text ("Nov 15, 5:59pm"); tare reads it as
the next such time and keeps the original words in the JSON (`resets`).

## What tare does with a reading

A window read at zero takes the routes it covers out of every selection until that window resets, exactly as a
failed call would have, but before any call is made. tare shows the route as out of usage until that time
(`QUOTA_EXHAUSTED` in a selection receipt).

- Only zero counts. A window with anything left changes nothing, however little.
- Only the routes the window covers leave. With AGY's Gemini 4 Argon quota spent, the other Gemini models and the
  Claude and GPT models through AGY stay. With Claude's Fable window spent, Opus, Sonnet and Haiku stay. A spent
  Codex window leaves both the Codex CLI and the Codex native routes.
- A window tare cannot place (a new group, a Codex limit id that names no model) is shown and never acted on.
- A zero without a reset time holds for half an hour from the reading.

The readings are taken when the console or `tare serve` starts, right after the vendor CLIs are discovered, and
whenever you run `tare quota`. If you buy credits or use one of Codex's free limit resets, run `tare quota`: a
route that a reading had taken out is back at once. A limit that a failed call recorded still waits for the reset
time that call reported.

## What is not reported

- **OpenCode** has no endpoint for the remaining allowance or balance (its documentation, read on 2026-11-10). Each
  reply only says how much of the allowance it used. tare shows the two facts it has instead:

  ```text
  OpenCode subscription: not reported. OpenCode publishes no remaining allowance; a reply only says what it used.
      A refused call left this account out until 21:05.
      Allowance tare's own sessions used: US$ 0.38 in the last 5 h, US$ 1.88 in 7 days, US$ 1.88 in 30 days.
  ```

  The first line appears when OpenCode refused a call for a spent plan and gave the time to try again. The second
  adds up what each reply said it used; it is not a share of what is left. OpenCode's documentation gives limits
  per model, but the refusal observed on 2026-11-09 stopped every paid model of the plan at once, so tare does not
  turn these dollars into a percentage. It counts tare's sessions only.
- **OpenAI, Anthropic, Gemini and AWS Bedrock** have no balance endpoint for an ordinary API key.
- **Kimi** has no reader yet.

These appear as "not reported" when their credential is set, never as a guess.

## A stated balance for an API without a balance endpoint

For `openai-api`, `anthropic-api`, `gemini-api`, `bedrock` and `opencode-api` you can state the balance yourself:

```powershell
tare quota --set anthropic-api 18.40
```

```text
API credit
  Claude API: US$ 17.92 left of US$ 18.40, stated Tue 10 Nov 09:12, less tare's own use since
```

tare then subtracts the dollars its own sessions record on that account from that moment on. Every session adds
the charge of each of its calls to `<storage>/serve/provider-spend.jsonl` when it ends; the stated balances are in
`<storage>/serve/provider-balance.json`.

- It counts tare's use only. If the same key is used elsewhere, or you add credit, state the balance again.
- The API routes carry no tariff of their own: their calls are valued at the models.dev reference price
  ([HTTP providers, Prices](HTTP_API_PROVIDERS.md#prices)), which can be wrong, and the balance is only as exact as
  it. The row then says how much of the use since the statement was valued that way.
- A call whose cost is unknown (no price for that model) adds nothing, so the figure can only be too high.
- Single text calls outside a session (a flow assessment, a title) are not added yet.
- Routing does not act on a stated balance: at zero the row says "none left" and the provider's own refusal is
  what stops the route.

A reading that fails (the CLI is signed out, the provider does not answer in time) is shown as "no reading" with
the reason, and the other providers are still read.

Any subscription that a refused call left out says so under its windows ("A refused call left this account out
until ..."), because that limit holds even when a later reading shows room.

## The saved readings

Each `tare quota` saves its readings in `<storage>/serve/provider-quota.json` (schema
`tare.tools/provider-quota/1`): one entry per account with `status` (`OBSERVED`, `UNAVAILABLE` or `NOT_REPORTED`),
`source`, `observed_at`, and either `buckets` (each with `label`, `scope`, `window`, `remaining` as a fraction and
`resets_at` as an epoch) or `credit` (`remaining` and `total` in dollars). No credential is saved.
