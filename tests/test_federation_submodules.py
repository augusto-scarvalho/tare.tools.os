#!/usr/bin/env python3
"""
test_federation_submodules.py — Automated Verification of Satellite Federation & ADR-049 Governance
"""
import os
import re
import subprocess
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GITMODULES_PATH = REPO_ROOT / ".gitmodules"
SATELLITES_DIR = REPO_ROOT / "satellites"

CANONICAL_SATELLITES = {
    "tare.tools.kernel": "https://github.com/augusto-scarvalho/tare.tools.kernel.git",
    "tare.tools.agent-runtime": "https://github.com/augusto-scarvalho/tare.tools.agent-runtime.git",
    "tare.tools.specgraph": "https://github.com/augusto-scarvalho/tare.tools.specgraph.git",
    "tare.tools.backlog-graph": "https://github.com/augusto-scarvalho/tare.tools.backlog-graph.git",
    "tare.tools.library": "https://github.com/augusto-scarvalho/tare.tools.library.git",
}


def test_gitmodules_file_exists_and_contains_canonical_urls():
    """Validates that .gitmodules exists and only registers canonical, authorized satellite repositories."""
    assert GITMODULES_PATH.exists(), ".gitmodules file must exist at repo root"
    content = GITMODULES_PATH.read_text(encoding="utf-8")

    for sat_name, expected_url in CANONICAL_SATELLITES.items():
        assert f'path = satellites/{sat_name}' in content, f"Missing path for {sat_name} in .gitmodules"
        assert expected_url in content, f"URL for {sat_name} in .gitmodules does not match canonical: {expected_url}"


def test_all_six_satellite_directories_exist():
    """Validates that all satellite submodule folders exist under satellites/."""
    assert SATELLITES_DIR.exists(), "satellites/ directory must exist"
    
    for sat_name in CANONICAL_SATELLITES.keys():
        sat_path = SATELLITES_DIR / sat_name
        assert sat_path.exists(), f"Satellite directory {sat_path} does not exist"
        assert sat_path.is_dir(), f"Satellite path {sat_path} must be a directory"


def test_submodule_git_pinning_status():
    """Checks git submodule status to ensure all submodules are pinned to valid commit SHAs."""
    cmd = ["git", "-c", "safe.directory=*", "submodule", "status"]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    
    assert proc.returncode == 0, f"git submodule status failed: {proc.stderr}"
    output = proc.stdout.strip()
    
    found_satellites = 0
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        # Format: [-/+]<sha> satellites/<name> ...
        parts = line.split()
        assert len(parts) >= 2, f"Unexpected submodule status format: {line}"
        
        sha = parts[0].lstrip("+-U")
        path = parts[1]
        
        sat_name = Path(path).name
        if sat_name in CANONICAL_SATELLITES:
            found_satellites += 1
            assert len(sha) >= 7, f"Invalid commit SHA '{sha}' for satellite {sat_name}"

    assert found_satellites == len(CANONICAL_SATELLITES), (
        f"Expected {len(CANONICAL_SATELLITES)} canonical satellites in git submodule status, found {found_satellites}"
    )


@pytest.mark.parametrize("readme_name", ["README.md", "README.pt-BR.md"])
def test_readme_contains_pinned_satellite_references(readme_name):
    """Validates that maintained READMEs document the exact committed gitlinks."""
    readme_path = REPO_ROOT / readme_name
    assert readme_path.exists(), f"{readme_name} must exist"
    readme_content = readme_path.read_text(encoding="utf-8")

    for sat_name in CANONICAL_SATELLITES.keys():
        proc = subprocess.run(
            ["git", "rev-parse", "--short=7", f"HEAD:satellites/{sat_name}"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"Could not resolve gitlink for {sat_name}: {proc.stderr}"
        expected_pin = proc.stdout.strip()
        pattern = rf"{re.escape(sat_name)}/\s*\(@\s*{expected_pin}\)"
        assert re.search(pattern, readme_content), (
            f"{readme_name} must pin {sat_name} at @ {expected_pin}"
        )
