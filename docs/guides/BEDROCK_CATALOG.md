# Amazon Bedrock internal catalog — 2026-09-07

Single source: the `bedrock` records of each model in `relay/round_table_roster.json`.
This document explains the selection; the live table comes from the roster:

```bash
python relay/round_table_roster.py --bedrock          # Markdown
python relay/round_table_roster.py --bedrock --json   # JSON for other scripts
python relay/round_table_roster.py                    # full projection, catalog at the end
```

Source evidence: the OS's own probe on 2026-09-07, one `converse` call
with `maxTokens=8` per model in `us-east-1`, token `AWS_BEARER_TOKEN_BEDROCK`
from the vault: 35 ids, 516 input tokens and 82 output tokens in total, plus the
account's official list of 122 models. The raw evidence of this run is not published.
The starting point was an earlier Gemini 3.8 diagnosis; it got the access list
right but four `modelId` values wrong (a `us.` prefix on models that exist only on-demand without a prefix): DeepSeek-V3.2, Devstral 2,
Mistral Large 3 and GLM 5. The roster uses the corrected, verified ids.

## Where Bedrock lives, and where it does not

- **Round Table: no Bedrock.** The three old fallbacks (`chair-anthropic-bedrock`,
  `chair-openai-bedrock`, `chair-xai-bedrock`) pointed to denied models and
  were removed. The roster validator now refuses any chair, mediator
  or scribe with runner `aider-bedrock`.
- **Role deployments: `relay/role_deployments.json`.** A table of its own,
  separate from the Round Table, projected by `python relay/round_table_roster.py --roles`.
  It holds the three Bedrock reserve explorers: `bedrock-opus-explorer`,
  `bedrock-sonnet-explorer`, `bedrock-glm-explorer`. A role deployment may only
  use a model with `access: verified` in the catalog.
- **Model catalog: the roster's `models`.** Shared by both tables;
  the `bedrock` records live there. Twelve verified, three marked as denied.

## Policy: free credit, reserve, never the primary route

Bedrock on this account runs on free credit. That removes the price argument
but not the quality one: no model reachable there matches Fable 5.1,
GPT-6 Astra or GLM 5.3, and the open models rank below the local Qwen3.8 27B.
So the policy is:

- **Serious work** (implementer, auditor, plan-auditor, chair, mediator):
  local Qwen and frontier models through CLI and OpenRouter. Bedrock is not a
  route, nor a routine fallback; OpenRouter already covers the reserve for these
  domains with better models.
- **Reserve with a role**, only where the free credit makes Bedrock strictly better
  than the alternatives: map and reduce for `research` beyond the local window or
  throughput (DeepSeek-V3.2, Nova Pro), and the Anthropic chair when the Claude plan
  runs out in the middle of a Round Table (Opus 4.6, previous generation, recorded
  as such in the receipt). Opus 4.6 also serves as a **repository explorer**:
  exploration is read-only and involves no judgment, and Opus is far above the
  local Qwen at it. Deployment `bedrock-opus-explorer`:

  ```bash
  python tare_ops.py agent --role explorer --deployment bedrock-opus-explorer --root ../repo --instruction "..."
  ```

  `tare-ops explore` (route `audit_local`) stays pinned to the local Qwen.
  Sonnet 4.6 (`bedrock-sonnet-explorer`) and GLM 5 (`bedrock-glm-explorer`)
  are equivalent reserve explorers; the three run in parallel with each other
  without errors. Bedrock quota is per model and per burst: chained sessions on the
  same model within a few minutes get `RateLimitError`. Parallel pattern and
  rules in [DELEGATION_POLICY.md](DELEGATION_POLICY.md) section 4b.
- Everything else is cataloged with `roles: []`: reachable, verified, no
  planned use. The full assessment is in
  BEDROCK_MODEL_ASSESSMENT_2026-09-07.md.

## What was left out and why

- **Gemma 3 27B, GLM 4.7 Flash, Ministral 8B/14B, Nova Lite/Micro, GPT-OSS 20B,
  Magistral Small, Llama 3.1/3.3 70B, Qwen3 32B:** the local Qwen on the GPU
  workstation covers low latency and zero cost for triage, autocomplete and
  exploration. Paying for that goes against the delegation policy. Qwen3 32B is the
  same domain as the local model.
- **Llama 4 Scout, Palmyra X5, Kimi K2 Thinking, GLM 4.7:** no gap that
  justifies them. Kimi K2.5 replaced K2 Thinking after the probe.
- **Nova Premier:** requires *Provisioned Throughput*, no on-demand.
- **Jamba 1.5, Claude 3.5:** legacy and end of life.
- **Image, video, embeddings, rerank** (Stability, Luma, TwelveLabs, Cohere,
  Titan): available on the account, but outside the scope of the text roster. If
  `research` gains semantic retrieval, `cohere.embed-v4:0` and
  `cohere.rerank-v3-5:0` are the candidates; record them here at that point.

## Denied on this account

Fable 5 and 5.1, Sonnet 5, Opus 5, Opus 4.7 and 4.8, Mythos, Grok 4.6 and 4.3,
GPT-5.6 Luna/Sol/Terra. All return `AccessDeniedException` because of a commercial
allowlist. Do not retry them routinely; if AWS grants access, change `access` to
`verified` in the roster with a new date and source.

## Reference prices

Table from the installed LiteLLM, `us-east-1`, USD per million tokens, checked
on 2026-09-07. Not reconciled with an invoice; Devstral 2 and Mistral Large 3 are
not in the table. What tare spends against is the models.dev reference price of the exact
wire id ([HTTP providers, Prices](HTTP_API_PROVIDERS.md#prices)), not this table.

| Model | Input | Output | Window |
|---|---:|---:|---:|
| Claude Sonnet 4.6 | 3.30 | 16.50 | 1 000 000 |
| Claude Opus 4.6 | 5.50 | 27.50 | 1 000 000 |
| Claude Haiku 4.5 | 1.10 | 5.50 | 200 000 |
| DeepSeek-R1 | 1.35 | 5.40 | 128 000 |
| DeepSeek-V3.2 | 0.62 | 1.85 | 163 840 |
| Llama 4 Maverick | 0.24 | 0.97 | 128 000 |
| Nova Pro | 0.80 | 3.20 | 300 000 |
| GPT-OSS 120B | 0.15 | 0.60 | 128 000 |
| GLM 5 | 1.00 | 3.20 | 200 000 |

## Rechecking access

No automatic inference. When needed, make one minimal call per model with
the vault token, recording date and result in the `checked`/`access` field:

```python
import boto3
client = boto3.client("bedrock-runtime", region_name="us-east-1")  # AWS_BEARER_TOKEN_BEDROCK in the environment
client.converse(modelId="us.anthropic.claude-sonnet-4-6",
                messages=[{"role": "user", "content": [{"text": "ping"}]}],
                inferenceConfig={"maxTokens": 8})
```

Every call is paid. Make them only on an explicit decision and note them in the changelog.
