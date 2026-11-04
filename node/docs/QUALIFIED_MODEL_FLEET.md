# Qualified local model fleet

The canonical text and vision surface is an OpenAI-compatible API on
`http://127.0.0.1:8080/v1`. The request's `model` field selects a role-qualified
artifact. The RTX 3090 keeps exactly one generation model resident: a route change
stops the previous private backend before loading the next one. Port 8081 remains the
independent embedding service.

## Catalog and fleet

The **catalog**, [`tare_node/config/qualified_model_fleet.json`](../tare_node/config/qualified_model_fleet.json),
is the models tare.tools qualified: a model enters it with a `promoted` or bounded `qualified_role` decision,
explicit evidence, intended roles and claim limits (the records stay in tare.tools.local-labs). It ships with the
package and updates with it; qualifying a model is a pull request here.

The **fleet** is the node owner's: `~/.config/tare-node/fleet.json`, which updates never touch.

```json
{"default": "qwen38-gsq",
 "models": ["qwen38-gsq", "hauhaucs", "gemma-vision"],
 "off": ["qwen36-moe"],
 "custom": {"my-model": {"artifact": {"path": "/srv/models/mine.gguf"}, "runtime": {"args": ["-c", "32768"]}}}}
```

- `models` are catalog ids: the gateway serves the catalog's cards for them, so corrections reach the node.
- `custom` are the owner's own models: a card needs only `artifact.path`; `runtime.kind` defaults to `llama`,
  `runtime.binary` to the catalog's binary for that kind, `runtime.args` to none. Any of them may be the default.
- Catalog paths beginning with `~/` use the service user's home and `@TARE_NODE_PACKAGE@/` the installed package
  (the qwen38 chat template ships there); custom fleet cards use absolute paths.
  Alternative layouts can supply a catalog through `tare-node-gateway --config`.
- The gateway lists the catalog picks first, then the owner's models. Role aliases (`coding`, `vision`…) follow
  the catalog while their model is in the fleet.
- The first start writes the fleet: the catalog models whose weights are on the node, and the catalog default.
- A catalog model whose weights reach the node later (you downloaded it, or an update qualified one you have)
  joins the fleet within a minute, unless you turned it off: `off` lists the catalog models you took out, which
  `PUT /v1/fleet` keeps (serving one again takes it off the list). Editing by hand, put it in `off` yourself.
- A catalog id the catalog later drops is skipped, not fatal (`fleet_missing` in `/v1/fleet/status`).

Change it from tare (`tare fleet`, or `/fleet` in the console), with `PUT /v1/fleet` and the node token (it serves
the new fleet at once and stops a resident model the fleet dropped), or by editing the file and restarting the
gateway. `GET /v1/fleet` shows the catalog (with `on_node`: whether the weights are here), the fleet, what is
served and the default.

## Agent CLI

The discovery CLI is maintained in tare.tools.local-labs. Run these commands from that checkout:

```powershell
python tools/agents/modelctl.py list
python tools/agents/modelctl.py recommend coding
python tools/agents/modelctl.py show hauhaucs
python tools/agents/modelctl.py example gemma-vision
python tools/agents/modelctl.py status
python tools/agents/modelctl.py chat qwen38-gsq "Reply only: OK"
python tools/agents/modelctl.py chat qwen36-moe "Reply only: OK" --no-thinking
```

Every discovery command supports stable model ids. `list`, `show`, `recommend` and
`status` also support `--json` for agent automation. Clients that already speak the
OpenAI API only need:

```text
OPENAI_BASE_URL=http://127.0.0.1:8080/v1
model=qwen38-gsq | qwen36-moe | fable-tc | hauhaucs | gemma-vision | muse-vision
```

Aliases such as `coding`, `math`, `vision` and `vision-hard` are accepted, but durable
automation should record the canonical model id returned by `modelctl recommend`.

### Inspect a model card by alias

`show` resolves either an alias or a canonical model id. The human-readable form is
suited to interactive inspection; `--json` is the stable form for agents and scripts:

```powershell
python tools/agents/modelctl.py show coding
python tools/agents/modelctl.py show coding --json
python tools/agents/modelctl.py show hauhaucs --json
```

The returned card includes the canonical `id`, display name, qualification class,
intended and excluded roles, modalities, summary, known limits, quantization, evidence
paths, the originally requested alias, artifact path and SHA-256, and the public API
base. Runtime launch arguments remain an operator concern in the machine-readable
registry rather than part of the public agent card.

For example, both `coding` and `hauhaucs` resolve to the role-qualified HauhauCS
artifact. Agents should use it for coding, low-friction tool use and constrained
summaries, but must not reinterpret that bounded qualification as a broad-default or
bounded-math promotion.

## Qualified routes and boundaries

| Route | Qualified role | Important boundary |
|---|---|---|
| `qwen38-gsq` | general, long context, agent/tools, coding baseline | The catalog default (`default`, `general`); its card excludes `fleet-count-binding`. |
| `qwen36-moe` | capped open-loop throughput and concurrency | Configuration-specific legacy deploy evidence, not a broad quality/default claim. |
| `fable-tc` | concise ordinary QA, Portuguese, math, writing | Frozen serving context is 8,192; not a thinking-mode route. |
| `hauhaucs` | coding, low-friction, agent/tools | Role-qualified, not promoted as the broad default. |
| `gemma-vision` | screenshots, OCR, UI debugging | Bounded synthetic visual-coding qualification, not universal VQA. |
| `muse-vision` | hard VQA and visual reasoning | Specialist role only; overall model remains HOLD and DFlash remains rejected. |

`muse-vision` uses its frozen architecture-specific build. The gateway, rather than
the homogeneous native llama-server router, is intentional: it permits different
qualified binaries while retaining a single public endpoint and a one-resident-model
VRAM invariant.

## Operational lifecycle

The public gateway runs as the existing `llm-inference.service`. Its child server is
loopback-only on port 18080. Current installation and update steps are in the
[Node README](../README.md#install-on-the-node-with-the-owner).

The first request after changing models includes unload and cold-load latency. Requests
are serialized across a switch so the old and new large artifacts cannot overlap in
VRAM. A model-load failure returns HTTP 503 and never silently falls through to a
different model.

`lmctl mode check serve` recognizes the private 18080 child only when the public 8080
endpoint proves the `qualified-model-gateway` identity. An orphan process on 18080 still
fails the mode lock as drift.
