# Shared GPU for text and image generation

One GPU serves text models, image jobs and training. The model gateway listens on port 8080 and ComfyUI on 8188;
both coordinate GPU use for **browser jobs and API image jobs** alike, and training takes the same lease. This is
serving integration, not a model-quality promotion.

## Execution contract

The managed text gateway and managed ComfyUI process share an exclusive Linux
file lock. A text request holds it through generation, streaming, counting or
profile observation. ComfyUI holds it through workflow execution and model
cleanup. The browser server remains available while a job waits.

An image job acquires the lock, asks the loopback gateway to release its resident
text backend, and starts only after confirmed backend exit. Before releasing the
lock, ComfyUI discards GPU caches and unloads its image models. The next text
request loads the requested qualified text model. Switching includes cold-load
latency; there is no fallback to a paid provider.

The gateway checks the image lease nonce before releasing its backend. The
nonce is local coordination evidence and does not appear in public status.
The lock does not expire while its owning process is alive. Failed ComfyUI
cleanup retains the lock and refuses further jobs until the managed process
is restarted. An unconfirmed text-backend stop also prevents a model switch.

The ComfyUI launcher runs upstream `main.py` with its normal arguments and
initialization. It installs the execution wrapper before the worker starts,
checks that custom-node initialization did not replace it, and verifies hashes
of the reviewed ComfyUI integration sources. A ComfyUI update therefore requires
review before this managed launcher starts. Arbitrary unmanaged GPU processes
and custom-node background GPU workers are outside this admission protocol.

Not yet qualified: live cancellation and failure recovery, a real reboot, and the
quality of the image models.

## Access and installed assets

On the Tailscale network:

- ComfyUI browser: `http://<node-tailnet-address>:8188`
- Model gateway: `http://<node-tailnet-address>:8080/v1`

The reviewed ComfyUI version is 0.36.0, source revision
`7a0b5eede3f9721c8faab290689893f36edc6d66`. Its Python environment is
`~/.venvs/comfyui/bin/python`.

Image models are the node owner's choice (for example Illustrious XL, Juggernaut XL,
Z-Image and Krea, plus supporting encoders/VAEs). Browser workflows retain their chosen
assets. Some models require explicit compatible workflows; inventory is not proof
that every model works or meets a quality threshold.

## Training commands

### Renting the GPU: `gpu` (any agent or person)

One command in the GPU node's WSL wraps the lease below (`~/.local/bin/gpu`; from the other machine,
`ssh gpu-node "wsl -e bash -lc 'gpu status'"`):

```bash
gpu status                                   # holder, reason, how long, running jobs, who is waiting
gpu run --task cohort-26 -- ./cohort.sh      # supervised job; the task name learns its usual duration
gpu run --task flux-batch --kill-after 2h -- python batch.py
gpu hold --reason "debug flux workflow" 30m  # interactive shell holding the GPU; exit it to give it back
```

- `run` has no deadline. With `--task`, the median of that task's last ten successful runs (lease wait
  excluded, `~/.local/state/tare-gpu-training/runs.jsonl`) becomes its estimate, and `status` flags a run past
  1.5x of it as late; with no history it says "no estimate". Only `--kill-after` stops a job.
- `hold` needs a terminal (`ssh -t`) and a duration; five minutes before it ends it warns, then it stops the
  shell and its children and only then releases the lease. Agents without a terminal use `run`.
- A `gpu run` inside a hold or another job reuses the held lease and keeps its own task name, limits and log.
- Idle watch: every 30 s the supervisor reads board power; after ten minutes averaging under 80 W (2x the idle
  floor measured on an RTX 3090) `status` shows the job as idle. A failed reading is "unknown", never idle. It
  only acts with `--idle-release 15m`, because 30 s samples miss short recurring bursts.
- While an image job (these, ComfyUI) holds or waits for the lease, the gateway answers text requests with
  `503 gpu_busy` within about 5 s instead of queueing them for the route timeout, and callers such as tare
  escalate elsewhere. Text behind text still queues. `qwen38-gsq` unloads after 10 idle minutes.
- How long text waits (contract `gpu-lease/1`, the kernel's `docs/GPU_LEASE_CONTRACT.md`): `/v1/fleet/status`
  gives `gpu_coordination.eta_seconds`, the time the holding `gpu run` job has left of its estimate plus the
  estimates of the image jobs waiting (each ticket carries its task's `expected_seconds`); null when one of
  them has no estimate (ComfyUI, a task without history, a run already late). Every `503` refusal carries
  `Retry-After` and `error.retry_after_seconds`: that estimate as it is, from 5 s up to 24 h (a 12-hour
  training run says 12 h), 60 s without one. `gpu status` shows each job's time left and when text gets the GPU. What tare does with it:
  tare.tools.os `docs/guides/SHARED_GPU.md`.
- Text keeps the lease for an idle grace after each request (`--text-idle-grace`, 20 s), so an agent's
  back-to-back calls are never split: an image or training job arriving mid-turn waits until text has been idle
  that long (`gpu status` shows the text owner with `idle_since`). Once a job has waited `--image-max-wait`
  (300 s) the grace ends after the running request, and the gateway logs a warning.

### Reserving the GPU from another machine (contract `gpu-lease/2`)

A bench or a long run elsewhere can hold the GPU for the text models by name, without ssh (from tare:
`tare gpu hold <name> 2h` or `tare gpu run <name> 2h -- <command>`). One reservation at a time, at most 4 h:

- `POST /v1/gpu/reservations` with `Authorization: Bearer <node token>` and
  `{"name", "holder", "max_seconds", "ttl_seconds" (15-600, default 60), "wait_seconds" (0-120, default 30)}`
  → **201** `{"id", "secret", "fence", "expires_at", "hard_until"}`; **409** `gpu_reserved` when one is active;
  **503** `gpu_busy` with `Retry-After` when a job keeps the GPU past `wait_seconds` (a reservation never cuts a
  running training job). The token lives in `~/.local/state/tare-qualified-models/api-token` (0600, created by
  `tare-node-install --apply`; `--gpu-api-token-file` elsewhere); without it reservations are off (**404**).
- `POST /v1/gpu/reservations/<id>/renew` and `DELETE /v1/gpu/reservations/<id>`, both with `{"secret", "fence"}`;
  **404** when it is over, **409** `stale_fence` for an old holder. A holder that stops renewing loses it after
  `ttl_seconds`; the gateway checks every second.
- While it lasts, only requests carrying its secret in `X-Tare-GPU-Reservation` reach the GPU (and they never give
  way to waiting image jobs); everything else, text or image, gets **503** `gpu_reserved` with `Retry-After` = the
  time left, and training jobs wait in the queue. `/v1/fleet/status` shows `gpu_coordination.reservation` (name,
  holder, fence, time left; never the secret) and `eta_seconds`; `gpu status` prints it.
- It is the text idle grace stretched to the reservation's length: no new lock. It lives in the gateway process, so
  a gateway restart ends it; the fence number grows across restarts (`gpu-fence` in the state directory).

The reservation deadline is the earlier of its renewed TTL and its original maximum duration. Renewal never
extends that maximum. Expiry or release prevents new use of the old reservation; an already admitted request
keeps the physical lock until it finishes. Revocation does not interrupt a running GPU operation.
If a remote holder disappears, missing renewals expire its reservation; local cancellation alone is not a remote
release acknowledgement.

Admission rechecks the current holder while serialized, so an earlier observation of a free GPU cannot replace
a reservation acquired meanwhile. With a state directory, the next fence is committed before taking the GPU:
temporary file, file fsync, atomic replacement and directory fsync. Storage failure returns **503**
`fence_state_unavailable`; invalid counter content returns **503** `fence_state_invalid`, without a grant or a
held GPU lock. Restore storage access or a known valid counter; do not delete/reset `gpu-fence` to hide the error.
A failed attempt may consume a counter value; gaps are allowed. After restart, old IDs return **404** and an
old fence/secret cannot change the successor (**409**).

### Reusable entrypoint

In the GPU node's WSL shell as the service user, use the same command for any training script:

```bash
gpu-run ./train.sh
gpu-run ./another-training.sh --max_train_epochs 12
gpu-run -- python train.py --config experiment.toml
gpu-run --gpu-wait 32400 ./another-training.sh
```

`.sh` commands run through Bash, including scripts without executable permission.
Other commands use the current environment, so activate the intended Python
environment first when invoking Python/Accelerate directly. Arguments and exit
codes are preserved. `tare-node-install --apply` links `~/.local/bin/gpu` and
`~/.local/bin/gpu-run` to the installed package; call them by that path if the shell
lacks that PATH entry. No changes to Accelerate or musubi-tuner are required.

For a Bash script that should also be protected when launched directly, copy the
shared guard shipped as `tare_node/gpu_training_guard.sh` to
`~/.local/share/tare-gpu-training/guard.sh` and source it near the top, before
environment activation or training:

```bash
source "$HOME/.local/share/tare-gpu-training/guard.sh" || exit $?
```

The guard re-executes the whole script under the supervisor. A protected command
inside an already protected job reuses its parent reservation after checking the
kernel lock, nonce, owner PID and process ancestry. An environment flag alone is
insufficient. This avoids recursive acquisition while keeping one owner through
training, samples, checkpoint conversion and descendant cleanup. Nested work is
part of the same supervised job; the lease is not a scheduler for its children.

New scripts need either `gpu-run script.sh` or the shared guard; arbitrary
unmanaged processes cannot be intercepted by this cooperative protocol.

### Low-level launcher and lifetime

Run musubi-tuner under the same lease, **on the GPU node in WSL as the service user**:

```bash
gpu-run -- \
  bash ./train.sh
```

The wrapper defaults to the serving lock at
`~/.local/state/tare-qualified-models/gpu.lock` and loopback gateway on 8080.
Running an ordinary training script without `gpu-run` or the shared guard still
bypasses coordination.

- `--gpu-wait 3600` is the default acquisition limit in seconds. Increase it
  explicitly for queued training, for example `--gpu-wait 32400` for nine hours.
  It must remain finite and positive. This deadline does **not** expire an acquired
  lease; no heartbeat is needed during a five-to-eight-hour training run.
- The wrapper acquires `kind=image`, authenticates its nonce with the gateway,
  and starts the command only after the gateway confirms its text backend exited.
  It holds the lease for the entire command, including sample generation and
  checkpoint conversion. It does not alter training arguments or outputs.
- SIGINT, SIGTERM and SIGHUP request shutdown of the child process group and
  detached descendants. `--stop-grace 30` allows cleanup before SIGKILL escalation.
  A Linux child subreaper tracks and reaps orphaned workers. The wrapper releases
  the lease only after observing that every descendant exited; elapsed grace
  alone is not evidence. Unconfirmed cleanup leaves the supervisor holding the
  lease and reporting the problem.
- Normal child exit codes are preserved; signal deaths use `128 + signal`.
  Acquisition timeout returns 124, coordination failure 125, and a missing
  executable 127. Diagnostics go to stderr; child input/output is inherited.
- Gateway requests and ComfyUI jobs do not preempt training. Their existing
  **600-second** acquisition limits remain in effect: they wait, then report a
  timeout if training is still running. Retrying later is explicit. A second
  training wrapper likewise waits up to its own limit and never starts concurrently.
  The next admitted text request reloads its model after training releases the GPU.

Process-exit evidence covers workers supervised by this wrapper. It is not a
GPU sandbox: unrelated processes and external services remain outside the lease.
Send signals to the wrapper, rather than force-killing it with SIGKILL. SIGKILL
cannot be handled; terminating the supervisor itself can drop its lock while
workers survive. Do not delete the lock file or forcibly stop the supervisor to
bypass an unconfirmed cleanup. Inspect and stop the remaining training workers.

## Image API

The configured gateway advertises the `comfyui` workflow engine and two SDXL
presets through `/v1/models`. Image cards are explicitly **unassessed** for model
quality. `/v1/images/models` also reports installed checkpoint, diffusion-model,
VAE and encoder assets without loading them.

Submit a preset job with `POST /v1/images/jobs`:

```json
{"model":"juggernaut-xl","prompt":"A blue teapot on a white table.","width":512,"height":512,"steps":20}
```

Use `illustrious-xl` for the other preset, or provide
`{"model":"comfyui","workflow":{...}}` with a ComfyUI API workflow to select
other installed models and nodes explicitly. A queued acknowledgement is not a
completed image. Preserve its returned `id`:

- `GET /v1/images/jobs/{id}` reports observed queue/history state and output links.
- `GET /v1/images/jobs/{id}/outputs/{index}` retrieves that job's recorded image.
- `POST /v1/images/jobs/{id}/cancel` with `{}` requests cancellation of that job
  only; it does not attest completed cancellation or GPU cleanup.

An uncertain submission reports its attempted ID so callers can inspect it
without blindly submitting the work twice. Missing history after a backend
restart is `UNKNOWN`; this is not a durable ComfyUI job database.

## Deployment and checks

Installation, rollback and automatic updates are described in the
[README](../README.md#install-on-the-node-with-the-owner). The gateway runs as
`llm-inference.service`; [`comfyui-gateway.conf`](../ops/comfyui-gateway.conf),
installed as `/etc/systemd/system/comfyui.service.d/zzzz-shared-gpu-order.conf`,
starts the gateway with ComfyUI and orders ComfyUI after it. The gateway listens
without preloading a model.

Windows mirrored WSL networking has a separate Hyper-V firewall. The scoped
[`allow_shared_gpu_tailscale.ps1`](../ops/allow_shared_gpu_tailscale.ps1)
rule permits TCP 8080 and 8188 from Tailscale addresses without changing the
global inbound policy. Windows firewall rules are kept. Mirrored networking
does not need a Windows port proxy for these WSL listeners.

Do not add same-port loopback port proxies (`netsh interface portproxy`) for 8080
or 8188 while using mirrored networking: a wildcard rule forwards to itself when the
WSL service is absent, and Windows can accumulate tens of thousands of established
sockets until local HTTP clients fail with `WinError 10055`. To diagnose, inspect
`netsh interface portproxy show all`, `Get-NetTCPConnection | Group-Object State`
and the actual WSL listeners with `wsl -d Ubuntu-24.04 -- ss -ltnp`. A missing
application listener should be diagnosed in its service.
