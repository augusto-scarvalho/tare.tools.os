#!/usr/bin/env python3
"""
bump_satellite.py — Mechanized CLI for Satellite Federation Management, Audit & Bumping (ADR-049)

Usage:
  python scripts/ops/bump_satellite.py status
  python scripts/ops/bump_satellite.py bump <satellite_name> [--ref <tag/sha/branch>] [--auto-test] [--auto-commit]
  python scripts/ops/bump_satellite.py sync-all
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

REPO_ROOT = Path(__file__).resolve().parents[2]
SATELLITES_DIR = REPO_ROOT / "satellites"
README_FILES = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "README.pt-BR.md",
)

CANONICAL_SATELLITES = {
    "tare.tools.kernel": "https://github.com/augusto-scarvalho/tare.tools.kernel.git",
    "tare.tools.agent-runtime": "https://github.com/augusto-scarvalho/tare.tools.agent-runtime.git",
    "tare.tools.specgraph": "https://github.com/augusto-scarvalho/tare.tools.specgraph.git",
    "tare.tools.backlog-graph": "https://github.com/augusto-scarvalho/tare.tools.backlog-graph.git",
    "tare.tools.library": "https://github.com/augusto-scarvalho/tare.tools.library.git",
}


def run_cmd(cmd: list, cwd: Path) -> Tuple[int, str, str]:
    """Runs a shell command and returns (returncode, stdout, stderr)."""
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          encoding='utf-8', errors='replace')
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def get_current_pinned_sha(sat_name: str) -> Optional[str]:
    """Gets the currently pinned commit SHA for a submodule via git submodule status."""
    code, out, _ = run_cmd(["git", "submodule", "status", f"satellites/{sat_name}"], REPO_ROOT)
    if code != 0 or not out:
        return None
    parts = out.split()
    if parts:
        return parts[0].lstrip("+-U")
    return None


def get_remote_head_sha(sat_name: str) -> Optional[str]:
    """Gets the latest commit SHA from remote origin/main."""
    sat_path = SATELLITES_DIR / sat_name
    if not sat_path.exists():
        return None
    run_cmd(["git", "fetch", "origin"], sat_path)
    code, out, _ = run_cmd(["git", "rev-parse", "origin/main"], sat_path)
    if code == 0 and out:
        return out
    return None


def cmd_status():
    """Prints status of all 6 canonical satellites."""
    print("\n" + "=" * 80)
    print(" 🛰️ TARE.TOOLS SATELLITE FEDERATION STATUS (ADR-049)")
    print("=" * 80)
    print(f"{'Satellite':<26} | {'Pinned SHA':<12} | {'Remote main':<12} | {'Status'}")
    print("-" * 80)

    for sat_name in CANONICAL_SATELLITES.keys():
        sat_path = SATELLITES_DIR / sat_name
        if not sat_path.exists():
            print(f"{sat_name:<26} | {'MISSING':<12} | {'-':<12} | ❌ Directory missing")
            continue

        pinned_sha = get_current_pinned_sha(sat_name) or "UNKNOWN"
        pinned_short = pinned_sha[:7] if pinned_sha != "UNKNOWN" else "UNKNOWN"

        remote_sha = get_remote_head_sha(sat_name)
        remote_short = remote_sha[:7] if remote_sha else "UNKNOWN"

        if remote_short == "UNKNOWN":
            status = "⚠️ Remote unreachable"
        elif pinned_short == remote_short:
            status = "✅ Up to date (Pinned)"
        else:
            status = f"🔄 Update available ({pinned_short} -> {remote_short})"

        print(f"{sat_name:<26} | {pinned_short:<12} | {remote_short:<12} | {status}")

    print("-" * 80 + "\n")


def update_readme_pin(sat_name: str, new_short_sha: str):
    """Updates the pinned SHA in every maintained README if present."""
    pattern = rf"({re.escape(sat_name)}/\s*\(@\s*)[a-f0-9]+(\))"

    for readme_file in README_FILES:
        if not readme_file.exists():
            continue
        content = readme_file.read_text(encoding="utf-8")
        if re.search(pattern, content):
            new_content = re.sub(pattern, rf"\g<1>{new_short_sha}\2", content)
            readme_file.write_text(new_content, encoding="utf-8")
            print(
                f"[README] Updated {readme_file.name}: "
                f"{sat_name} pinned SHA to @ {new_short_sha}"
            )


def cmd_bump(sat_name: str, target_ref: str = "origin/main", auto_test: bool = True, auto_commit: bool = False):
    """Bumps a specific satellite to a target commit/ref, runs tests, and optionally commits."""
    if sat_name not in CANONICAL_SATELLITES:
        print(f"❌ Error: '{sat_name}' is not a canonical satellite repository.")
        print(f"Authorized satellites: {list(CANONICAL_SATELLITES.keys())}")
        sys.exit(1)

    sat_path = SATELLITES_DIR / sat_name
    if not sat_path.exists():
        print(f"❌ Error: Submodule folder '{sat_path}' does not exist. Run 'git submodule update --init --recursive'")
        sys.exit(1)

    old_pinned = get_current_pinned_sha(sat_name) or "UNKNOWN"
    old_short = old_pinned[:7]

    print(f"\n[BUMP] Fetching and updating '{sat_name}' from {old_short} -> {target_ref}...")
    
    # 1. Fetch
    code, _, err = run_cmd(["git", "fetch", "origin"], sat_path)
    if code != 0:
        print(f"❌ Failed to fetch from remote: {err}")
        sys.exit(1)

    # 2. Checkout
    code, _, err = run_cmd(["git", "checkout", target_ref], sat_path)
    if code != 0:
        print(f"❌ Failed to checkout '{target_ref}': {err}")
        sys.exit(1)

    code, new_sha, _ = run_cmd(["git", "rev-parse", "HEAD"], sat_path)
    new_short = new_sha[:7]

    print(f"[BUMP] Checked out {sat_name} @ {new_short}")

    # 3. Update README
    update_readme_pin(sat_name, new_short)

    # The federation gate reads committed gitlinks, so commit before testing.
    if auto_commit:
        print(f"\n[COMMIT] Staging and committing bump for {sat_name}...")
        code, out, err = run_cmd(
            [
                "git",
                "add",
                f"satellites/{sat_name}",
                "README.md",
                "README.pt-BR.md",
            ],
            REPO_ROOT,
        )
        if code != 0:
            print(f"[COMMIT] Staging failed: {out} {err}")
            sys.exit(1)
        commit_msg = f"chore(deps): bump {sat_name} from {old_short} to @ {new_short}"
        code, out, err = run_cmd(["git", "commit", "-m", commit_msg], REPO_ROOT)
        if code == 0:
            print(f"✅ Committed: {commit_msg}")
        else:
            print(f"[COMMIT] Commit failed; candidate retained for repair: {out} {err}")
            sys.exit(1)

    if auto_test:
        code, committed, err = run_cmd(['git', 'rev-parse', f'HEAD:satellites/{sat_name}'], REPO_ROOT)
        if code != 0:
            print(f"[TEST] Cannot read committed pin: {err}")
            sys.exit(1)
        if committed != new_sha:
            print('[TEST] Pending: commit the gitlink and README updates, then run '
                  'python -m pytest tests/test_federation_submodules.py')
        else:
            print('\n[TEST] Checking committed satellite federation...')
            code, out, err = run_cmd([sys.executable, '-m', 'pytest', 'tests/test_federation_submodules.py'], REPO_ROOT)
            if code != 0:
                print(f'[TEST] Quality gate FAILED; candidate retained for repair:\n{out}\n{err}')
                sys.exit(1)
            print('[TEST] Committed federation checks passed.')

    print(f"\n🎉 Successfully bumped '{sat_name}' to @ {new_short}!\n")


def main():
    parser = argparse.ArgumentParser(description="Satellite Federation Manager (ADR-049)")
    subparsers = parser.add_subparsers(dest="command")

    # Status
    subparsers.add_parser("status", help="Inspect status of all 6 satellites")

    # Bump
    bump_parser = subparsers.add_parser("bump", help="Bump a satellite submodule to target ref")
    bump_parser.add_argument("satellite", help="Name of the satellite (e.g. tare.tools.specgraph)")
    bump_parser.add_argument("--ref", default="origin/main", help="Target git ref, branch, or tag (default: origin/main)")
    bump_parser.add_argument("--no-test", action="store_true", help="Skip running integration tests")
    bump_parser.add_argument("--commit", action="store_true", help="Automatically commit the bump")

    args = parser.parse_args()

    if args.command == "status" or not args.command:
        cmd_status()
    elif args.command == "bump":
        cmd_bump(args.satellite, target_ref=args.ref, auto_test=not args.no_test, auto_commit=args.commit)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
