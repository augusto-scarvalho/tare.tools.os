#!/usr/bin/env bash
# Two more tare.tools.os runners in the node's WSL (aaaaa-os-linux-2, -3), copies of the original one.
# The registration token replaces __TOKEN__ on the way in (docs/operations/INFRASTRUCTURE_CI_RUNNERS_NODE_AAAAA.md, item 5);
# it never lands on disk. Runs as root: sed ... | ssh node "wsl -d Ubuntu-24.04 -u root -- bash -s".
token='__TOKEN__'
src=/opt/actions-runners/tare.tools.os
for n in 2 3; do
  dir=/opt/actions-runners/tare.tools.os-$n
  if [ -f "$dir/.runner" ]; then echo "skip $dir (already configured)"; continue; fi
  rm -rf "$dir"  # a partial copy from an attempt that never registered
  mkdir -p "$dir/_work/_tool/tare-ci"
  # bin and externals are absolute links to the original runner's version folders: copy the folders themselves,
  # or the new runner runs the original's binaries and finds its registration.
  cp -a "$(readlink -f "$src/bin")" "$dir/bin"
  cp -a "$(readlink -f "$src/externals")" "$dir/externals"
  cp -a "$src/config.sh" "$src/run.sh" "$src/runsvc.sh" "$src/svc.sh" "$src/env.sh" "$src/safe_sleep.sh" \
        "$src/run-helper.sh.template" "$src/.env" "$src/.path" "$dir/"
  # The CI's learned test map, so the first runs here are scoped like the original runner's; each learns on its own.
  cp -a "$src"/_work/_tool/tare-ci/test-map-linux-wsl2*.json "$dir/_work/_tool/tare-ci/" 2>/dev/null
  chown -R augus:augus "$dir"
  cd "$dir" || exit 1
  sudo -u augus ./config.sh --unattended --url https://github.com/augusto-scarvalho/tare.tools.os --token "$token" \
      --name "aaaaa-os-linux-$n" --labels rtx3090,cuda,wsl2,aaaaa --work _work 2>&1 | tail -4 | sed "s/^/  [$n] /"
  if [ -f .runner ]; then
    ./svc.sh install augus 2>&1 | tail -1 | sed "s/^/  [$n] /"
    ./svc.sh start 2>&1 | grep -i 'active' | sed "s/^/  [$n] /"
  else
    echo "  [$n] not configured: no service installed"
  fi
done
systemctl list-units --plain --no-pager 'actions.runner.*tare.tools.os*' | grep service
