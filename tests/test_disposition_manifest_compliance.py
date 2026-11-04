import hashlib
import io
import json
import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = str(REPO_ROOT / "contracts" / "v1" / "harness_disposition_manifest.json")
HARNESS_DIR = str(REPO_ROOT.parent / "tare.tools.harness" / "scripts" / "harness_lib")
RELAY_DIR = str(REPO_ROOT / "relay")


def content_fingerprint(content):
    return len(content), hashlib.sha256(content).hexdigest()


def git_index_contents(relative_paths):
    # One Git process for all modules: hundreds of `git show` launches can
    # exceed the test deadline on Windows before checking the complete manifest.
    result = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=REPO_ROOT,
        input="".join(f":{path}\n" for path in relative_paths).encode("utf-8"),
        capture_output=True,
        check=True,
        timeout=15,
    )
    stream = io.BytesIO(result.stdout)
    contents = {}
    for path in relative_paths:
        header = stream.readline().split()
        assert len(header) == 3 and header[1] == b"blob", f"index blob missing: {path}"
        size = int(header[2])
        contents[path] = stream.read(size)
        assert len(contents[path]) == size and stream.read(1) == b"\n", f"incomplete index blob: {path}"
    assert not stream.read(), "unexpected Git batch output"
    return contents


def test_relay_modules_are_pinned_to_lf_checkouts():
    attributes = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "relay/*.py text eol=lf" in attributes.splitlines()


def test_disposition_manifest_structure_and_completeness():
    """ISS-OPENAI-R1-01: prove canonical manifest completeness and integrity."""

    assert os.path.exists(MANIFEST_PATH), "canonical manifest must exist in contracts/v1"
    with open(MANIFEST_PATH, "r", encoding="utf-8") as fp:
        manifest = json.load(fp)

    assert manifest["version"] == "1.0.0"
    modules = manifest["modules"]
    # Exact module membership is checked below; a fixed count breaks unrelated merges.
    assert manifest["total_modules"] == len(modules)

    names = [module["name"] for module in modules]
    paths = [module["path"] for module in modules]
    assert len(names) == len(set(names)), "manifest module names must be unique"
    assert len(paths) == len(set(paths)), "manifest module paths must be unique"

    if os.path.exists(HARNESS_DIR):
        harness_fs = {
            name
            for name in os.listdir(HARNESS_DIR)
            if name.endswith(".py") and name != "__init__.py"
        }
        manifest_harness = {
            module["name"] for module in modules if "harness_lib" in module["path"]
        }
        assert harness_fs == manifest_harness, (
            f"harness filesystem/manifest difference: {harness_fs ^ manifest_harness}"
        )

    relay_fs = {name for name in os.listdir(RELAY_DIR) if name.endswith(".py")}
    manifest_relay = {
        module["name"] for module in modules if "relay" in module["path"]
    }
    assert relay_fs == manifest_relay, (
        f"relay filesystem/manifest difference: {relay_fs ^ manifest_relay}"
    )

    # This repository owns the complete integrity contract for relay modules.
    # Harness entries describe a sibling repository and are checked there.
    relay_modules = [
        module
        for module in modules
        if module["path"].startswith("tare.tools.os/relay/")
    ]
    index_contents = git_index_contents([f"relay/{module['name']}" for module in relay_modules])
    for module in relay_modules:
        path = os.path.join(RELAY_DIR, module["name"])
        assert os.path.exists(path), f"relay module missing: {module['name']}"
        index_content = index_contents[f"relay/{module['name']}"]
        expected = (module["size_bytes"], module["sha256"])
        assert expected == content_fingerprint(index_content), (
            f"manifest mismatch for relay module {module['name']}"
        )
        assert Path(path).read_bytes().replace(b"\r\n", b"\n") == index_content, (
            f"worktree mismatch for relay module {module['name']}"
        )


def test_anti_resurrection_barrier_falsifier():
    """ISS-OPENAI-R1-02: prove the frozen harness cannot be reimported."""

    active_dirs = [str(REPO_ROOT / "relay"), str(REPO_ROOT / "scripts")]
    forbidden_tokens = [
        "import tare.tools.harness",
        "from tare.tools.harness",
        "importlib.import_module('tare.tools.harness",
        'importlib.import_module("tare.tools.harness',
        "sys.path.append('C:\\projects\\tare.tools.harness",
        'sys.path.append("C:\\projects\\tare.tools.harness',
        "sys.path.insert(0, 'C:\\projects\\tare.tools.harness",
        'sys.path.insert(0, "C:\\projects\\tare.tools.harness',
    ]

    violations = []
    for directory in active_dirs:
        if not os.path.exists(directory):
            continue
        for root, _, files in os.walk(directory):
            for filename in files:
                if not filename.endswith(".py") or filename == __file__:
                    continue
                path = os.path.join(root, filename)
                with open(path, "r", encoding="utf-8", errors="ignore") as fp:
                    for line_number, line in enumerate(fp, start=1):
                        for token in forbidden_tokens:
                            if token in line and not line.strip().startswith("#"):
                                violations.append(
                                    f"{path}:{line_number}: {line.strip()}"
                                )

    assert not violations, f"frozen-harness resurrection violations: {violations}"
