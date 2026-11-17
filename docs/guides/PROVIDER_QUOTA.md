# Provider quota and credit

`tare quota` shows what each provider says you have left: the windows of each subscription and the credit of each
API account. It asks the providers directly and makes no model call, so it spends nothing.

```powershell
tare quota            # ask every provider now (about ten seconds)
tare quota --cached   # the last readings, without asking again
tare quota --json     # the same readings as JSON
tare quota --set openai-api 25   # state an API balance in dollars; tare counts its own spend down from it
tare quota --set anthropic-api 100 --renews 14   # a credit that returns each month (day 14) and does not carry over
tare quota --set bedrock 300 --expires 2027-03-31   # a credit that lapses at the end of that day and never renews
```

A known balance is also a guard: while a stated balance is counted down to zero, or OpenRouter's own reading (under
an hour old) shows none, that account's routes are left out instead of being tried and refused, and the line says
`its routes are left out while nothing is left`. A new statement or reading with credit brings them back at once. An
account with no known balance is never left out. tare counts only its own use, and it leaves the routes out even if
the key could still bill a card beyond the stated credit: state a new balance to keep using it.

A refused call is a guard too. Only OpenRouter and OpenCode return what a call cost, and only OpenRouter reports a
balance; every other provider returns token counts, which tare values at a tariff or a reference price. So when a
provider refuses a call for lack of credit (HTTP 402, OpenAI's `insufficient_quota`, Anthropic's "Your credit balance
is too low"), tare ends the session with `API_CREDIT_SPENT`, leaves that account's routes out for half an hour and
then tries again; the account's line shows `A refused call left this account out until HH:MM.` A passing rate limit
is not taken for it.

In the console, `/usage` opens on its first row, **Provider quota and credit**. It shows the saved readings at once
and asks every provider again in the background (no model call, about 15 seconds); the row changes when the new
reading lands. Reopen `/usage` to read again.

`/quota` opens the same text on a screen of its own, **Quota, credit and cost**, and under it the fields to state what
the providers do not report: for each API account the reading lists, `Balance, US$`, `Renews on day` and `or lapses on
(YYYY-MM-DD)` (what `--set`, `--renews` and `--expires` take), and `Energy price, US$ per kWh` (what `tare setup
--energy-price` holds). **Save** writes the accounts whose Balance is filled and nothing else, because stating a balance
restarts the count; one wrong field (a day or a date without a balance, both a day and a date, a past date) saves
nothing and the status line says which account and why. **Read again** asks the providers again. The screen shows and
asks for no key; keys stay in `/setup`.

```text
Subscriptions
  Claude subscription · plan max_20x
    week, all models                  57% left   resets Sun 15 Nov 17:59
    week, Fable                       62% left   resets Sun 15 Nov 17:59
  Codex subscription · plan prolite
    week                             none left   resets Thu 12 Nov 18:21
    2 free limit resets available
  AGY subscription · plan Google AI Pro
    Gemini Models, week               49% left   resets Fri 13 Nov 22:20
    Gemini Models, 5 h               100% left   resets 13:13
    Claude and GPT models, week      100% left   resets Tue 17 Nov 08:13
    Gemini 4 Argon, week             none left   resets Sat 14 Nov 01:57
  OpenCode subscription · plan go: not reported. OpenCode publishes no remaining allowance; a reply only says what it used.

API credit
  OpenRouter: US$ 5.23 left of US$ 20.00
  OpenAI API: not reported. This provider has no balance endpoint for an API key.
```

Under each bucket one more line says what 1% of that window bought on this account, measured from tare's own use
(`no measure yet` until it can): `about US$ 46 of API-equivalent use per 1% (firm, measured 11 to 16 Nov)`. See
[What 1% bought](#what-1-bought).

## Where each reading comes from

| Provider | Source | What it reports |
| --- | --- | --- |
| Claude subscription | `claude -p /usage` | Percent used of each window. A model with its own quota (Fable) has its own line. |
| Codex subscription | `codex app-server`, request `account/rateLimits/read` | Percent used and reset time of each window, per limit id; the plan; free limit resets. |
| AGY subscription | `agy -p /quota` and `/credits` | Fraction left of the weekly and 5-hour windows of each model group, and AI credits. |
| OpenRouter | `GET https://openrouter.ai/api/v1/credits` | Dollars bought and dollars used. |

Each subscription's name carries the plan the account is on, which tare reads instead of asking:

| Provider | Where the plan shows | Read |
| --- | --- | --- |
| Claude subscription | `planTier` of `claude auth status` (else `subscriptionType`); a local answer | Each reading. Only the plan is kept: the answer also holds the account's email and organisation. |
| Codex subscription | `planType` of the same `account/rateLimits/read` | Each reading. |
| AGY subscription | The header line of the TUI's first screen, `<account email> (Google AI Pro)`; no AGY command reports it | Once per AGY executable file (a new AGY version reads again), kept in the quota cache. tare opens the TUI for a few seconds and sends nothing: no key, no text, no model call. Only the plan name is kept, never the email. If AGY asks to trust the folder, tare answers nothing and shows no plan; it tries again after a day. |
| OpenCode subscription | `usage.opencode.billing_source` of the last reply of the subscription route (`go`, `go-plus`; `balance`, `byok` and `free` are not plans) | When a reply is recorded (`provider-spend.jsonl`); no plan is shown until one was. |

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
  OpenCode subscription · plan go: not reported. OpenCode publishes no remaining allowance; a reply only says what it used.
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

## Reading the Anthropic API credit from the Console

Anthropic has no API for the balance or expiry of the monthly API credit of a Max or Ultra plan; only the Console's
Billing page shows them. `tare quota --sync` reads that page once through your browser and states the credit for
`anthropic-api` with its exact window (granted, expires, what was granted), so the real cycle is used, not a day you
guessed. tare then counts its own spend down from the balance read, as for any stated balance.

- **Needs.** Claude Code signed in, and the Claude in Chrome extension connected in a browser that is signed in to the
  Console. Only read-only browser tools are allowed: nothing is clicked or typed.
- **Costs.** One Haiku call on the Claude subscription (about a cent of quota, about half a minute) and a tab that opens
  and closes. `tare quota --sync` is the consent for that one reading.
- **Kept.** The model sees the whole Billing page; tare parses only the credits section and saves only the stated
  numbers (`<storage>/serve/provider-balance.json`) and the attempt (`<storage>/serve/console-credit.json`).
- **Fails.** The last statement stays. `tare quota` shows a failed last attempt as a note under the Claude API account.
  A credit past its expiry counts as lapsed until the next reading.
- **Automatic.** Off until you turn it on: `tare setup --console-credit on` (`off` turns it off). When on, the console
  reads the credit at start when the last attempt is over 12 hours old (one hour after a failed one), never at each
  demand.
- **Anthropic's terms.** Yours to weigh before turning it on; this is what they say, not legal advice (read on
  2026-11-14). The Commercial Terms, which govern the Console, have no clause on automated reading of its pages. The
  Consumer Terms, which govern the Claude plan the extension runs on, forbid access "through automated or non-human
  means" except with an API key "or where we otherwise explicitly permit it", and forbid scraping. No text says that
  Anthropic's own Claude in Chrome reading your own signed-in page is that permission, and none says it is forbidden;
  its documentation describes acting in sites you are signed in to, on your behalf. tare reads one section of one
  page of your own account, clicks nothing, and never works around a sign-in page or a block.
- **Without it.** `tare quota --set anthropic-api AMOUNT --renews DAY [GRANT]` stays the way to state the credit by hand.

## What 1% bought

A plan's cost per task depends on how much API-equivalent value its meter delivers, and the vendors change that
without notice. tare measures it on your own account and never announces a number without a state. It changes no
routing.

- **Readings.** Each `tare quota` appends the buckets of every subscription it read to
  `<storage>/serve/plan-readings.json` (time, account, bucket, remaining, reset) and drops what is older than 7 days. A
  failed reading adds nothing.
- **tare's own use.** A call on a Claude, Codex or AGY route is valued at the reference price (models.dev, see
  HTTP_API_PROVIDERS.md) of its model on the vendor's own API, cache reads and writes at their own rates, and kept in
  `provider-spend.jsonl` as a row with `equivalent` and no `usd`. It is never money: it does not count in
  `spent()`, a declared balance or a spending cap. A call whose model has no reference price, or whose CLI reported no
  token counts, has no value and does not count. Calls that never reach the ledger (side calls such as reviews and web
  research) are missing too; that only makes the estimate more cautious. A bucket scoped to some models (Claude's
  `week, Fable`, AGY's groups) counts only the calls of those models.
- **The estimate.** Use outside tare only ever adds to the meter, so between two readings of one window
  `rate <= (meter movement + one meter step) / tare's dollars` and every span gives an upper bound. The estimate is the
  smallest bound over spans of the last 7 days that hold at least US$ 5 of tare's use (older spans are never used, so a
  changed quota moves it within a week; a span ending at 100% is no bound, as the meter stops counting). It is shown
  as the inverse: the dollars 1% bought, at least that much.
  The meter step is the smallest non-zero step in the series, else the source's own (1% for Claude and Codex, 0.01%
  for AGY).
- **States.** `none`: no span reaches US$ 5 (`no measure yet`). `weak`: shown as `at least US$ X`; one span, or
  outside use may have loosened it. `firm`: shown as `about US$ X`; the best span covers at least US$ 50 and each of
  its halves, a span of its own, gives a bound within 25% of it.
- **Where.** `tare quota --json` has it as `consumption` on each bucket: `state`, `usd_per_percent`, `measured_from`,
  `measured_to` (the span the figure comes from) and `covered_usd`.

## What a local model's energy cost

A local model is not free: its GPU draws power. tare measures that and shows it; nothing routes by it. It measures
from the day you set a price (or declared watts): until then a local endpoint gets no request for it.

- **Measured.** The node samples the card's power (`nvidia-smi`) every 5 seconds and keeps a day in memory. After
  each invocation that made new calls on a local route, tare asks the node (`GET /v1/gpu/power`, 2 seconds, one
  request) for the average watts between the invocation's start and end, and adds one row to `provider-spend.jsonl`
  for the account `local`: `energy_wh = watts * seconds / 3600`, the model, and `source` (`measured`, or `declared`).
  The span is the whole invocation, so tool time and preparation count at the card's draw then, and a GPU shared with
  an image job or another agent counts their draw too. No reading and no declared watts: no row. A failure never
  touches the call. The node says where its readings for the span start: when they cover only its end (a session
  longer than the node's record, or a node restarted meanwhile, which empties it), tare counts only that part, keeps
  the rest as `unmeasured_seconds`, and `tare quota` says the total is partial. The average of the end is never
  stretched over the start.
- **Declared.** `tare setup --local-watts W` states the GPU's watts under load. It is used only when the node gives no
  reading (an old node, no `nvidia-smi`, a span with no sample). It is applied to the whole invocation, idle time
  included, so the figure is then an upper bound and the line says how much of the energy came from it. `off` clears
  it.
- **The price.** `tare setup --energy-price US$_PER_KWH` states your electricity price (`off` clears it). Rows hold
  watt-hours, never dollars: the cost is computed when `tare quota` shows it, at the price set then.
- **Shown.** `local: 0.41 kWh in 7 days, about US$ 0.07 at US$ 0.18/kWh (GPU only)`; without a price the kWh and the
  command to set one; with no row, nothing. "GPU only" stays: `nvidia-smi` measures the card, not the machine.
  `tare quota --json` has it as `energy` on the account `local` (`kind: local`): `days`, `kwh`, `declared_kwh`,
  `price_kwh`, `usd`.
- **Never money spent.** The row has `energy_wh` and no `usd`: `spent()`, a declared balance and a spending cap do not
  see it.

## The saved readings

Each `tare quota` saves its readings in `<storage>/serve/provider-quota.json` (schema
`tare.tools/provider-quota/1`): one entry per account with `status` (`OBSERVED`, `UNAVAILABLE` or `NOT_REPORTED`),
`source`, `observed_at`, and either `buckets` (each with `label`, `scope`, `window`, `remaining` as a fraction and
`resets_at` as an epoch) or `credit` (`remaining` and `total` in dollars). No credential is saved.
