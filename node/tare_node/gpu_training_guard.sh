# Source this once, before activating environments or starting training:
# source "$HOME/.local/share/tare-gpu-training/guard.sh"
# The shared launcher reserves the GPU for the entire script, including samples
# and conversion. A verified parent lease avoids recursive acquisition.

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    echo 'Source the GPU training guard from a Bash training script.' >&2
    exit 2
fi

if "$HOME/.local/bin/gpu-run" --check-inherited; then
    : # The outer supervisor already owns the verified lease.
else
    tare_gpu_guard_status=$?
    if [[ "$tare_gpu_guard_status" != 1 ]]; then
        exit "$tare_gpu_guard_status"
    fi
    exec "$HOME/.local/bin/gpu-run" -- bash "$0" "$@"
fi
