"""Command-level qualification of the pinned Runtime dogfood, never vendor CLIs.

No mocks of Kernel/SpecGraph. Only disposable Runtime copies may be edited;
source pins, tracked bytes and OS coordinator/Work state are checked at teardown.
"""
from __future__ import annotations

import ast
from hashlib import sha256
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import sysconfig

import pytest


ROOT = Path(__file__).resolve().parents[1]
OWNERS = {name: ROOT / "satellites" / ("tare.tools." + name)
          for name in ("kernel", "specgraph", "agent-runtime")}
MANIFESTS = {
    "claude": (".claude/settings.json", "hooks"),
    "codex": (".codex/hooks.json", "hooks"),
    "agy": (".agents/hooks.json", "tare-specgraph-first"),
}
EDIT = "src/tare_tools_agent_runtime/runtime.py"


def process(argv, *, cwd=ROOT, env=None, raw=None):
    result = subprocess.run(argv, cwd=cwd, env=env, input=raw, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=15, check=False,
                            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
    assert len(result.stdout) <= 65536, "command output exceeds qualification budget"
    return result


def git(root, *args):
    # Source inventory is setup evidence, not context delivered by a hook.
    # Keep every tracked path even when the pinned evidence corpus grows.
    result = subprocess.run(["git", "-C", str(root), *args], cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=15, check=False,
                            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
    assert result.returncode == 0, result.stderr.decode(errors="replace")[:300]
    return result.stdout


def tracked(root):
    return [item.decode("utf-8") for item in git(root, "ls-files", "-z").split(b"\0") if item]


def fingerprints(paths):
    result = {}
    for path in paths:
        native = os.path.abspath(path)
        if os.name == "nt" and not native.startswith("\\\\?\\"):
            native = "\\\\?\\UNC\\" + native[2:] if native.startswith("\\\\") else "\\\\?\\" + native
        result[str(path)] = sha256(Path(native).read_bytes()).hexdigest()
    return result


def snapshot(root):
    return fingerprints(sorted(path for path in root.rglob("*") if path.is_file()))


@pytest.fixture(scope="module")
def pinned_sources():
    # The coordinator database is a record, absent from a clone without them: then there is nothing to change.
    source_paths = [p for p in (ROOT / "work-graph.json", ROOT / "relay/history/coordinator.db") if p.exists()]
    pins = {}
    for name, owner in OWNERS.items():
        pin = git(ROOT, "ls-tree", "HEAD", "satellites/" + owner.name).decode().split()[2]
        assert git(owner, "rev-parse", "HEAD").decode().strip() == pin
        assert not git(owner, "status", "--porcelain"), f"dirty pinned owner: {name}"
        pins[name] = pin
        source_paths.extend(owner / name for name in tracked(owner))
    before = fingerprints(source_paths)
    yield pins
    assert fingerprints(source_paths) == before, "source checkout or canonical OS state changed"
    for name, owner in OWNERS.items():
        assert git(owner, "rev-parse", "HEAD").decode().strip() == pins[name]
        assert not git(owner, "status", "--porcelain"), f"owner changed during qualification: {name}"


@pytest.fixture
def dogfood(tmp_path, pinned_sources):
    root = tmp_path / "runtime"
    for name in tracked(OWNERS["agent-runtime"]):
        source = OWNERS["agent-runtime"] / name
        assert not source.is_symlink()
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    # Explicit import roots override editable installs pointing at sibling repos.
    env = {key: value for key, value in os.environ.items() if key.upper() in {
        "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
        "LOCALAPPDATA", "APPDATA", "USERPROFILE", "LANG", "SYSTEMDRIVE",
    }}
    env.update(PYTHONPATH=os.pathsep.join(map(str, (ROOT, OWNERS["kernel"],
               OWNERS["specgraph"] / "src", OWNERS["agent-runtime"] / "src"))),
               PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
               TARE_SPECGRAPH_STATE_DIR=str(tmp_path / "recovery-state"))
    binaries = {name: shutil.which(name, path=env.get("PATH")) for name in ("tare-hook", "specgraph")}
    assert all(binaries.values()), "installed tare-hook and specgraph entrypoints required; no fallback"
    for binary in binaries.values():
        assert Path(binary).parent.resolve() == Path(sysconfig.get_path("scripts")).resolve(), \
            "entrypoint is shadowed by another Python installation"
    return root, env, binaries


def command_json(command, dogfood, *, raw=None, expected=0):
    root, env, _ = dogfood
    result = process(command, cwd=root, env=env, raw=raw)
    assert result.returncode == expected, result.stderr.decode(errors="replace")[:400]
    return json.loads(result.stdout)


def spec(dogfood, *args, expected=0):
    root, _, binaries = dogfood
    return command_json([binaries["specgraph"], "--root", str(root), *args, "--format", "json"],
                        dogfood, expected=expected)


def initialize_cache(dogfood):
    root, env, binaries = dogfood
    result = process([binaries["specgraph"], "--root", str(root), "sync-cache"], cwd=root, env=env)
    assert result.returncode == 0, result.stderr.decode(errors="replace")[:400]
    assert spec(dogfood, "check-stale")["status"] == "FRESH"


def hook_pair(dogfood, vendor, event, payload):
    root, _, binaries = dogfood
    manifest, namespace = MANIFESTS[vendor]
    config = json.loads((root / manifest).read_bytes())
    entry = config[namespace][event][0]
    if isinstance(payload, dict):
        tool = payload["toolCall"]["name"] if vendor == "agy" else payload["tool_name"]
        assert tool in entry["matcher"].split("|"), "test tool not intercepted by checked-in manifest"
    configured = shlex.split(entry["hooks"][0]["command"])
    assert configured[0] == "tare-hook" and configured[1] == vendor
    raw = json.dumps(payload).encode() if isinstance(payload, dict) else payload
    direct = command_json([binaries["tare-hook"], *configured[1:]], dogfood, raw=raw)
    facade = command_json([sys.executable, "-B", "-m", "tare_ops", "hook", *configured[1:]], dogfood, raw=raw)
    assert facade == direct, "OS facade changed the owner's native hook result"
    return direct


def payload(vendor, root, *, event="PreToolUse", edit=False, command="rg AgentRuntime src"):
    if vendor == "agy":
        return {"workspacePaths": [str(root)], "toolCall": {
            "name": "write_to_file" if edit else "run_command",
            "args": {"TargetFile": str(root / EDIT)} if edit else {"CommandLine": command}}}
    if edit and vendor == "codex":
        tool, arguments = "apply_patch", {"command": f"*** Begin Patch\n*** Update File: {EDIT}\n*** End Patch"}
    elif edit:
        tool, arguments = "Edit", {"file_path": str(root / EDIT)}
    else:
        tool, arguments = "Bash", {"command": command}
    return {"hook_event_name": event, "cwd": str(root), "tool_name": tool, "tool_input": arguments}


def advisory(output, vendor, event):
    if vendor == "agy":
        if event == "PostToolUse":
            assert output == {}, "Antigravity has no post-tool feedback channel in this contract"
            return ""
        assert output["decision"] == "allow"
        return output["reason"]
    specific = output["hookSpecificOutput"]
    assert specific["hookEventName"] == event
    assert "permissionDecision" not in specific, "advisory became permission authority"
    return specific["additionalContext"]


def test_children_import_exact_pinned_owners(dogfood):
    script = ("import json, compute_plane, specgraph, tare_tools_agent_runtime; "
              "print(json.dumps([m.__file__ for m in (compute_plane,specgraph,tare_tools_agent_runtime)]))")
    loaded = command_json([sys.executable, "-B", "-c", script], dogfood)
    for filename, owner in zip(loaded, OWNERS.values()):
        assert Path(filename).resolve().is_relative_to(owner.resolve()), filename


@pytest.mark.parametrize("vendor", MANIFESTS)
def test_real_search_denial_and_resolver_pass_are_identical_through_facade(dogfood, vendor):
    root, _, _ = dogfood
    before = snapshot(root)
    denied = hook_pair(dogfood, vendor, "PreToolUse", payload(vendor, root))
    if vendor == "agy":
        assert denied["decision"] == "deny"
    else:
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "specgraph resolve" in json.dumps(denied)
    passed = hook_pair(dogfood, vendor, "PreToolUse", payload(
        vendor, root, command="specgraph resolve AgentRuntime --format json"))
    assert passed == ({"decision": "allow", "reason": ""} if vendor == "agy" else {})
    assert snapshot(root) == before


@pytest.mark.parametrize("vendor", MANIFESTS)
@pytest.mark.parametrize("raw", [b"not-json", b"[]", b"{" + b"x" * 65536], ids=["invalid-json", "wrong-shape", "oversized"])
def test_real_invalid_pre_tool_is_closed_and_bounded(dogfood, vendor, raw):
    output = hook_pair(dogfood, vendor, "PreToolUse", raw)
    if vendor == "agy":
        assert output["decision"] == "deny"
    else:
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert len(json.dumps(output).encode()) < 2048


def test_real_resolve_checks_source_reuses_fallback_and_keeps_telemetry_private(dogfood):
    root, env, _ = dogfood
    initialize_cache(dogfood)  # explicit setup, never initiated by a hook
    before = snapshot(root)
    # The legacy AST cache and resolve's published index are separate stores.
    structural = spec(dogfood, "search", "AgentRuntime", "--limit", "3")
    assert structural["results"]
    for match in structural["results"]:
        source = (root / match["file_path"]).resolve()
        assert source.is_relative_to(root.resolve())
        assert "AgentRuntime" in match["name"]
        assert any(isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name == match["name"].rsplit(".", 1)[-1]
                   and (node.lineno, node.end_lineno) == (match["start_line"], match["end_line"])
                   for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))))
    for query, expected_source in (("tactical turn progression", "traditional"), ("AgentRuntime", "specgraph")):
        if expected_source == "specgraph":
            published = spec(dogfood, "index")  # explicit setup, never a hook side effect
            assert published["status"] == "READY" and published["index_integrity"] == "PASS"
        report = spec(dogfood, "resolve", query, "--limit", "3")
        assert report["status"] == "HIT" and report["source"] == expected_source
        assert 0 < report["count"] <= 3
        if expected_source == "traditional":
            assert report["primary_status"] == "UNAVAILABLE"  # no index was published yet
            assert report["recovery_action"] == "INDEXED_FALLBACK_EVIDENCE"
        else:
            assert report["recovery_action"] == "READ_PUBLISHED_SNAPSHOT"
            assert report["snapshot"]["results_from_snapshot"] is True
        for match in report["results"]:
            source = (root / match["file_path"]).resolve()
            assert source.is_relative_to(root.resolve())
            content = source.read_text(encoding="utf-8")
            if expected_source == "specgraph":
                assert query in match["name"]
                # Qualified names can match a parent class, not the first line.
                assert any(
                    isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == match["name"].rsplit(".", 1)[-1]
                    and (node.lineno, node.end_lineno) == (match["start_line"], match["end_line"])
                    for node in ast.walk(ast.parse(content))
                )
            else:
                assert query in content.splitlines()[match["line"] - 1]
        repeated = spec(dogfood, "resolve", query, "--limit", "3")
        if expected_source == "traditional":
            assert repeated["source"] == "learned" and repeated["recovery_action"] == "REUSED_VALIDATED"
        else:
            assert repeated["source"] == "specgraph" and repeated["primary_status"] == "HIT"
    assert snapshot(root) == before
    state = Path(env["TARE_SPECGRAPH_STATE_DIR"])
    assert not state.is_relative_to(root)
    telemetry_dir = Path(spec(dogfood, "telemetry")["state_directory"])
    assert telemetry_dir.resolve().is_relative_to(state.resolve())
    telemetry = (telemetry_dir / "search-events.jsonl").read_text(encoding="utf-8")
    assert "AgentRuntime" not in telemetry and "tactical turn progression" not in telemetry
    events = list(map(json.loads, telemetry.splitlines()))
    assert len(events) == 4
    for event in events:
        assert len(event["query_sha256"]) == 64
        assert not {"query", "excerpt", "file_path", "results"} & event.keys()


@pytest.mark.parametrize("vendor", MANIFESTS)
def test_real_edit_impact_and_staleness_are_advisory_without_reindex(dogfood, vendor):
    root, _, _ = dogfood
    initialize_cache(dogfood)
    cache = root / ".specgraph/cache.json"
    before = snapshot(root)
    cache_stamp = cache.stat().st_mtime_ns
    pre = hook_pair(dogfood, vendor, "PreToolUse", payload(vendor, root, edit=True))
    text = advisory(pre, vendor, "PreToolUse")
    assert '"status":"HIT"' in text and "SpecGraph pre-edit impact" in text
    assert snapshot(root) == before
    target = root / EDIT
    target.write_bytes(target.read_bytes() + b"\n# Disposable dogfood edit; never applied to the owner.\n")
    after_edit = snapshot(root)
    post = hook_pair(dogfood, vendor, "PostToolUse", payload(vendor, root, event="PostToolUse", edit=True))
    text = advisory(post, vendor, "PostToolUse")
    if vendor != "agy":
        assert '"status":"STALE"' in text and EDIT in text
    # Also observe the owner result where the vendor cannot return advisory text.
    stale = spec(dogfood, "staleness", "--changed-path", EDIT)
    assert stale["status"] == "STALE" and stale["observed_changed_paths"] == [EDIT]
    assert snapshot(root) == after_edit
    assert cache.stat().st_mtime_ns == cache_stamp
    assert sha256(cache.read_bytes()).hexdigest() == before[str(cache)]


@pytest.mark.parametrize("vendor", MANIFESTS)
def test_uninitialized_cache_reports_unknown_impact_without_blocking_edit(dogfood, vendor):
    root, _, _ = dogfood
    before = snapshot(root)
    output = hook_pair(dogfood, vendor, "PreToolUse", payload(vendor, root, edit=True))
    text = advisory(output, vendor, "PreToolUse")
    assert '"status":"UNAVAILABLE"' in text
    assert snapshot(root) == before and not (root / ".specgraph").exists()
