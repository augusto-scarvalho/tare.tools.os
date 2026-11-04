"""Install explicit integration pins in disposable CI clones, keeping gitlinks.

Private repositories use the runner's existing Git credentials. Windows uses
the same read-only deploy-key aliases as this repository's satellite checkout.
No token is copied from the parent checkout or printed by this helper.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

OWNERS = {"tare.tools.kernel", "tare.tools.specgraph", "tare.tools.agent-runtime", "tare.tools.backlog-graph"}


def validate_pins(document):
    pins = document["repositories"]
    if set(pins) != OWNERS or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value) for value in pins.values()):
        raise ValueError("Four exact integration commit SHAs are required")
    return pins


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pins", type=Path, default=Path(__file__).resolve().with_name("specgraph_ci_dependencies.json"))
    args = parser.parse_args()
    pins = validate_pins(json.loads(args.pins.read_text(encoding="utf-8")))
    parent = Path(tempfile.mkdtemp(prefix="specgraph-os-ci-", dir=os.environ.get("RUNNER_TEMP")))
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
    if os.name == "nt":
        env["GIT_SSH_COMMAND"] = "C:/Windows/System32/OpenSSH/ssh.exe -F C:/ProgramData/tare-tools/ci/ssh_config"
    installed = []
    for owner, commit in sorted(pins.items()):
        root = parent / owner
        root.mkdir()
        url = "https://github.com/augusto-scarvalho/" + owner + ".git"
        if os.name == "nt" and owner != "tare.tools.backlog-graph":
            alias = owner.removeprefix("tare.tools.")
            url = f"git@tare-os-ci-{alias}:augusto-scarvalho/{owner}.git"
        subprocess.run(["git", "init", "-q", str(root)], check=True, env=env)
        subprocess.run(["git", "-C", str(root), "fetch", "--depth=1", "--no-tags", url, commit], check=True, env=env)
        subprocess.run(["git", "-C", str(root), "checkout", "--detach", "FETCH_HEAD"], check=True, env=env)
        actual = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], env=env, text=True).strip()
        if actual != commit:
            raise ValueError("Fetched integration revision mismatch")
        installed.extend(["-e", str(root)])
    flags = ["--break-system-packages"] if os.name != "nt" and sys.prefix == sys.base_prefix else []
    subprocess.run([sys.executable, "-m", "pip", "install", *flags, *installed], check=True, env=env)
    print(json.dumps({"status": "INSTALLED", "repositories": pins, "clones": str(parent),
                      "scope": "CI job only; committed satellite gitlinks unchanged"}))


if __name__ == "__main__":
    main()
