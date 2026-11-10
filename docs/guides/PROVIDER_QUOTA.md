# Provider quota and credit

`tare quota` shows what each provider says you have left: the windows of each subscription and the credit of each
API account. It asks the providers directly and makes no model call, so it spends nothing.

```powershell
tare quota            # ask every provider now (about ten seconds)
tare quota --cached   # the last readings, without asking again
tare quota --json     # the same readings as JSON
```

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

## What is not reported

- **OpenCode** has no endpoint for the remaining allowance or balance (its documentation, read on 2026-11-10). Each
  reply only says how much of the allowance it used.
- **OpenAI, Anthropic, Gemini and AWS Bedrock** have no balance endpoint for an ordinary API key.
- **Kimi** has no reader yet.

These appear as "not reported" when their credential is set, never as a guess.

A reading that fails (the CLI is signed out, the provider does not answer in time) is shown as "no reading" with
the reason, and the other providers are still read.

## The saved readings

Each `tare quota` saves its readings in `<storage>/serve/provider-quota.json` (schema
`tare.tools/provider-quota/1`): one entry per account with `status` (`OBSERVED`, `UNAVAILABLE` or `NOT_REPORTED`),
`source`, `observed_at`, and either `buckets` (each with `label`, `scope`, `window`, `remaining` as a fraction and
`resets_at` as an epoch) or `credit` (`remaining` and `total` in dollars). No credential is saved.
