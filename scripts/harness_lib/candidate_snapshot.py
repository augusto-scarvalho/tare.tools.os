"""The immutable, shippable Git tree used by governance.

The developer index may contain runtime receipts.  It is therefore not the
candidate authority.  A short-lived synthetic index contains precisely the
Candidate Plane and produces the one tree that every governance leg consumes.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from harness_lib import processes
from harness_lib.common import HarnessError, now


# Candidate Plane: staged delivery input. Evidence and provenance keep their
# base-head version so a receipt cannot rewrite the tree it describes.
EVIDENCE_PLANE_PREFIXES = (
    ".harness/runs/",
    ".harness/state/",
    ".harness/state-store/",
    ".harness/workflows/active/",
)
ARTIFACT_PLANE_REGISTRY = {
    ".harness/state/commit-transactions.jsonl": "evidence",
    ".harness/state/security-baseline.json": "authority_state",
    ".harness/state/security-exemptions.json": "authority_state",
    ".harness/state/tasks.json": "runtime_state",
    ".harness/state-store/trusted-verifiers.json": "authority_state",
    "release/checksums.sha256": "candidate",
    "release/harness-sbom.cdx.json": "candidate",
    "release/release-attestation.json": "derived_provenance",
    "release/release-signature.sha256": "evidence",
    "release/slsa-provenance.intoto.jsonl": "derived_provenance",
}
GIT_ENV_KEYS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
                "GIT_PREFIX", "GIT_CEILING_DIRECTORIES", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG_GLOBAL",
                "GIT_CONFIG_SYSTEM", "HARNESS_ROOT", "HARNESS_REAL_ROOT")


def is_evidence_path(path: str) -> bool:
    """True only for non-shippable runtime/proof material."""
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return any(normalized.startswith(prefix) for prefix in EVIDENCE_PLANE_PREFIXES)


def candidate_plane_classification(path: str) -> str:
    """Stable path classification used by tests and provenance consumers."""
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if normalized in ARTIFACT_PLANE_REGISTRY:
        return ARTIFACT_PLANE_REGISTRY[normalized]
    return "evidence" if is_evidence_path(normalized) else "candidate"


def resolve_scratch_root(kind: str, *, source_root: Path | None = None) -> Path:
    """Resolve and preflight an Evidence-Plane scratch root or fail closed."""
    configured = os.environ.get("HARNESS_SCRATCH_DIR")
    if configured:
        choices = [Path(configured)]  # an explicit operator choice must not silently fall back
    else:
        choices = []
        if source_root is not None:
            choices.append(source_root / ".harness" / "runs" / "candidate-scratch")
        choices.append(Path(tempfile.gettempdir()) / "harness-runtime-scratch")
    failures: list[str] = []
    for base in choices:
        target = base / kind
        try:
            target.mkdir(parents=True, exist_ok=True)
            fd, probe_name = tempfile.mkstemp(prefix=".write-probe-", dir=str(target))
            os.close(fd)
            Path(probe_name).unlink()
            return target
        except OSError as exc:
            failures.append(f"{target}: {exc}")
    raise HarnessError("UNWRITABLE_SCRATCH_FAILS_CLOSED: no writable runtime scratch root; "
                       + " | ".join(failures))


def _git(root: Path, *args: str, env: dict[str, str] | None = None, input: str | None = None):
    return processes.run_quiet(["git", *args], timeout=120, cwd=str(root), env=env,
                               input=input, capture_output=True, text=True)


def _require_git(root: Path, *args: str, env: dict[str, str] | None = None,
                 input: str | None = None) -> str:
    proc = _git(root, *args, env=env, input=input)
    if proc.returncode != 0:
        raise HarnessError(
            f"CandidateSnapshot git {' '.join(args[:2])} failed: "
            f"{(proc.stderr or proc.stdout or 'unknown error').strip()}"
        )
    return (proc.stdout or "").strip()


def _index_entries(root: Path, *, env: dict[str, str] | None = None) -> list[tuple[str, str, str]]:
    """Return stage-0 candidate index entries as ``(mode, blob, path)``."""
    raw = _require_git(root, "ls-files", "--stage", "-z", env=env)
    entries: list[tuple[str, str, str]] = []
    for record in raw.split("\0"):
        if not record:
            continue
        try:
            header, path = record.split("\t", 1)
            mode, blob, stage = header.split(" ", 2)
        except ValueError as exc:
            raise HarnessError("invalid Git index record while freezing candidate") from exc
        if stage != "0":
            raise HarnessError("cannot freeze a conflicted Git index")
        if not is_evidence_path(path):
            entries.append((mode, blob, path))
    return sorted(entries, key=lambda row: row[2])


def _index_info(entries: Iterable[tuple[str, str, str]]) -> str:
    return "".join(f"{mode} {blob}\t{path}\0" for mode, blob, path in entries)


def _candidate_index(root: Path, base_head: str) -> tuple[Path, list[tuple[str, str, str]]]:
    """Build ``baseHead`` overlaid only by Candidate Plane index entries."""
    index_dir = resolve_scratch_root("candidate-indices", source_root=root)
    fd, index_name = tempfile.mkstemp(prefix="harness-candidate-index-", dir=str(index_dir))
    os.close(fd)
    index = Path(index_name)
    index.unlink(missing_ok=True)
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = str(index)
    try:
        base = {path: (mode, blob, path) for mode, blob, path in _tree_entries(root, base_head)}
        staged = {path: (mode, blob, path) for mode, blob, path in _index_entries(root)}
        final: dict[str, tuple[str, str, str]] = dict(base)
        candidate_paths = {path for path in set(base) | set(staged)
                           if candidate_plane_classification(path) == "candidate"}
        for path in candidate_paths:
            if path in staged:
                final[path] = staged[path]
            else:
                final.pop(path, None)  # staged candidate deletion
        entries = sorted(final.values(), key=lambda row: row[2])
        _require_git(root, "read-tree", "--empty", env=env)
        if entries:
            _require_git(root, "update-index", "-z", "--index-info", env=env,
                         input=_index_info(entries))
        return index, entries
    except Exception:
        index.unlink(missing_ok=True)
        raise


def _tree_entries(root: Path, tree_id: str) -> list[tuple[str, str, str]]:
    raw = _require_git(root, "ls-tree", "-r", "-z", tree_id)
    rows: list[tuple[str, str, str]] = []
    for record in raw.split("\0"):
        if not record:
            continue
        try:
            header, path = record.split("\t", 1)
            mode, _kind, blob = header.split(" ", 2)
        except ValueError as exc:
            raise HarnessError("invalid Git tree record while verifying candidate") from exc
        rows.append((mode, blob, path))
    return sorted(rows, key=lambda row: row[2])


def _manifest_digest(entries: Iterable[tuple[str, str, str]]) -> str:
    """Fingerprint the exact plane represented by modes, object ids and paths."""
    payload = "".join(f"{mode} {blob}\t{path}\0" for mode, blob, path in entries)
    return hashlib.sha256(payload.encode("utf-8", errors="surrogateescape")).hexdigest()


def freeze_candidate_details(root: Path | str) -> tuple[str, str, str, str]:
    """Return ``(CandidateTreeId, CandidateFingerprint, ManifestDigest)``.

    Both hashes describe the same Candidate Plane.  The tree is Git's
    authoritative object; the SHA-256 digest is a portable manifest assertion.
    """
    source = Path(root).resolve()
    base_head = _require_git(source, "rev-parse", "--verify", "HEAD")
    index, entries = _candidate_index(source, base_head)
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = str(index)
    try:
        tree_id = _require_git(source, "write-tree", env=env)
    finally:
        index.unlink(missing_ok=True)
    if len(tree_id) not in (40, 64):
        raise HarnessError(f"CandidateSnapshot received invalid tree id {tree_id!r}")
    tree_entries = _tree_entries(source, tree_id)
    if entries != tree_entries:
        raise HarnessError("CANDIDATE_TREE_AND_FINGERPRINT_HAVE_CONSISTENT_PLANE_SEMANTICS failed")
    digest = _manifest_digest(entries)
    return tree_id, digest, digest, base_head


def freeze_candidate(root: Path | str) -> tuple[str, str, str]:
    """Compatibility view of the canonical four-field freeze result."""
    tree_id, fingerprint, digest, _base_head = freeze_candidate_details(root)
    return tree_id, fingerprint, digest


def current_candidate_tree(root: Path | str) -> str:
    """The current CandidateTreeId, never the raw developer-index tree."""
    return freeze_candidate(root)[0]


def current_candidate_fingerprint(root: Path | str) -> str:
    return freeze_candidate(root)[1]


def _readonly(path: Path) -> None:
    for current, dirs, files in os.walk(path, topdown=False):
        current_path = Path(current)
        for name in files:
            try:
                (current_path / name).chmod(stat.S_IREAD)
            except OSError:
                pass
        for name in dirs:
            try:
                (current_path / name).chmod(stat.S_IREAD | stat.S_IEXEC)
            except OSError:
                pass
    try:
        path.chmod(stat.S_IREAD | stat.S_IEXEC)
    except OSError:
        pass


def _writable(path: Path) -> None:
    for current, dirs, files in os.walk(path, topdown=False):
        current_path = Path(current)
        for name in files:
            try:
                (current_path / name).chmod(stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass
        for name in dirs:
            try:
                (current_path / name).chmod(stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            except OSError:
                pass
    try:
        path.chmod(stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    except OSError:
        pass


def _source_objects_dir(source: Path) -> Path:
    value = _require_git(source, "rev-parse", "--path-format=absolute", "--git-path", "objects")
    path = Path(value)
    return path if path.is_absolute() else (source / path).resolve()


def _materialize_execution_repository(source: Path, tree_id: str, commit_id: str,
                                      target: Path) -> None:
    """Create an independent repository checked out at exactly ``tree_id``.

    A temporary bundle transfers the immutable commit closure.  The resulting
    repository has no alternates, remote, shared refs, config, index, or path
    reference to the developer repository.
    """
    bundle_dir = resolve_scratch_root("object-bundles", source_root=source)
    fd, bundle_name = tempfile.mkstemp(prefix="harness-snapshot-", suffix=".bundle", dir=str(bundle_dir))
    os.close(fd)
    bundle = Path(bundle_name)
    bundle.unlink(missing_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="harness-object-stage-", dir=str(bundle_dir)))
    env = os.environ.copy()
    for key in GIT_ENV_KEYS:
        env.pop(key, None)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    try:
        # Name the dangling freeze commit in a temporary repository. Its
        # alternate is deleted before the final repository is cloned.
        _require_git(source, "init", "--quiet", str(stage), env=env)
        info = stage / ".git" / "objects" / "info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "alternates").write_text(str(_source_objects_dir(source)).replace("\\", "/") + "\n",
                                           encoding="utf-8", newline="\n")
        _require_git(stage, "update-ref", "refs/heads/harness-snapshot", commit_id, env=env)
        _require_git(stage, "bundle", "create", str(bundle), "refs/heads/harness-snapshot", env=env)
        shutil.rmtree(stage, ignore_errors=True)
        _require_git(source, "clone", "--quiet", str(bundle), str(target), env=env)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        bundle.unlink(missing_ok=True)
    _require_git(target, "remote", "remove", "origin", env=env)
    _require_git(target, "config", "--local", "core.autocrlf", "false", env=env)
    _require_git(target, "update-ref", "refs/heads/harness-snapshot", commit_id, env=env)
    _require_git(target, "symbolic-ref", "HEAD", "refs/heads/harness-snapshot", env=env)
    _require_git(target, "read-tree", "HEAD", env=env)
    _require_git(target, "checkout-index", "--all", "--force", env=env)
    _require_git(target, "update-index", "--refresh", env=env)
    got = _require_git(target, "rev-parse", "HEAD^{tree}", env=env)
    if got != tree_id:
        raise HarnessError(f"isolated repository tree mismatch: expected {tree_id}, got {got}")


@dataclass(frozen=True)
class CandidateSnapshot:
    snapshot_id: str
    candidate_tree_id: str
    candidate_fingerprint: str
    snapshot_path: Path
    created_at: str
    manifest_digest: str
    source_root: Path
    commit_id: str
    base_head: str

    @classmethod
    def create_from_index(cls, root: Path | str, *, parent: Path | str | None = None) -> "CandidateSnapshot":
        """Materialize the synthetic CandidateTreeId in an isolated Git worktree."""
        source = Path(root).resolve()
        tree_id, fingerprint, digest, parent_sha = freeze_candidate_details(source)
        identity = os.environ.copy()
        identity.update({
            "GIT_AUTHOR_NAME": identity.get("GIT_AUTHOR_NAME", "Harness Snapshot"),
            "GIT_AUTHOR_EMAIL": identity.get("GIT_AUTHOR_EMAIL", "snapshot@harness.invalid"),
            "GIT_COMMITTER_NAME": identity.get("GIT_COMMITTER_NAME", "Harness Snapshot"),
            "GIT_COMMITTER_EMAIL": identity.get("GIT_COMMITTER_EMAIL", "snapshot@harness.invalid"),
        })
        commit_id = _require_git(source, "commit-tree", tree_id, "-p", parent_sha, env=identity)
        base = Path(parent) if parent is not None else resolve_scratch_root("candidate-snapshots", source_root=source)
        base.mkdir(parents=True, exist_ok=True)
        created = now()
        snapshot_id = f"CS-{tree_id[:12]}-{digest[:12]}"
        snapshot_path = Path(tempfile.mkdtemp(prefix=f"{snapshot_id}-", dir=str(base)))
        snapshot_path.rmdir()
        try:
            _materialize_execution_repository(source, tree_id, commit_id, snapshot_path)
            snapshot = cls(snapshot_id, tree_id, fingerprint, snapshot_path, created,
                           digest, source, commit_id, parent_sha)
            snapshot.verify()
            _readonly(snapshot_path)
            return snapshot
        except Exception:
            _writable(snapshot_path)
            shutil.rmtree(snapshot_path, ignore_errors=True)
            raise

    def execution_env(self) -> dict[str, str]:
        """Child environment that cannot inherit a developer-repository override."""
        env = os.environ.copy()
        for key in GIT_ENV_KEYS:
            env.pop(key, None)
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["HARNESS_CANDIDATE_SNAPSHOT_ID"] = self.snapshot_id
        env["HARNESS_CANDIDATE_TREE_ID"] = self.candidate_tree_id
        return env

    def verify(self) -> None:
        materialized = _require_git(self.snapshot_path, "rev-parse", "HEAD^{tree}",
                                    env=self.execution_env())
        if materialized != self.candidate_tree_id:
            raise HarnessError(
                "CandidateSnapshot materialization mismatch: "
                f"expected {self.candidate_tree_id}, got {materialized}"
            )
        top = Path(_require_git(self.snapshot_path, "rev-parse", "--show-toplevel",
                                env=self.execution_env())).resolve()
        if top != self.snapshot_path.resolve():
            raise HarnessError("SNAPSHOT_PROCESS_CANNOT_FALL_BACK_TO_DEVELOPER_REPO failed")
        tree_entries = _tree_entries(self.snapshot_path, self.candidate_tree_id)
        if _manifest_digest(tree_entries) != self.manifest_digest:
            raise HarnessError("CandidateSnapshot manifest digest changed after creation")
        dirty = _git(self.snapshot_path, "diff-files", "--quiet", env=self.execution_env()).returncode
        cached = _git(self.snapshot_path, "diff-index", "--cached", "--quiet", "HEAD", "--",
                      env=self.execution_env()).returncode
        status = _require_git(self.snapshot_path, "status", "--porcelain", "--untracked-files=all",
                              env=self.execution_env())
        if dirty or cached or status:
            raise HarnessError("SNAPSHOT_MUTATED: canonical snapshot worktree/index is not clean: "
                               + status[:300])

    def fork(self, *, parent: Path | str | None = None, label: str = "leg") -> Path:
        """Return a disposable writable worktree derived from CandidateTreeId."""
        base = Path(parent) if parent is not None else resolve_scratch_root("candidate-forks", source_root=self.source_root)
        base.mkdir(parents=True, exist_ok=True)
        target = Path(tempfile.mkdtemp(prefix=f"{label}-{self.snapshot_id}-", dir=str(base)))
        target.rmdir()
        try:
            _materialize_execution_repository(self.source_root, self.candidate_tree_id,
                                              self.commit_id, target)
            got = _require_git(target, "rev-parse", "HEAD^{tree}", env=self.execution_env())
            if got != self.candidate_tree_id:
                raise HarnessError("CandidateSnapshot fork did not preserve candidate tree")
            return target
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise

    def cleanup_fork(self, path: Path | str) -> None:
        target = Path(path)
        _writable(target)
        shutil.rmtree(target, ignore_errors=True)

    def cleanup(self) -> None:
        self.cleanup_fork(self.snapshot_path)

    def metadata(self) -> dict[str, str]:
        return {
            "snapshotId": self.snapshot_id,
            "candidateTreeId": self.candidate_tree_id,
            "candidateFingerprint": self.candidate_fingerprint,
            "snapshotPath": str(self.snapshot_path),
            "createdAt": self.created_at,
            "manifestDigest": self.manifest_digest,
            "baseHead": self.base_head,
        }


def snapshot_metadata_json(snapshot: CandidateSnapshot) -> str:
    return json.dumps(snapshot.metadata(), sort_keys=True)


class SnapshotExecutor:
    """The only production process seam for a canonical snapshot."""

    def __init__(self, snapshot: CandidateSnapshot) -> None:
        self.snapshot = snapshot

    def run(self, argv: list[str], *, timeout: int, **kwargs):
        self.snapshot.verify()
        process_error: BaseException | None = None
        try:
            return processes.run_quiet(argv, timeout=timeout, cwd=str(self.snapshot.snapshot_path),
                                       env=self.snapshot.execution_env(), **kwargs)
        except BaseException as exc:
            process_error = exc
            raise
        finally:
            try:
                self.snapshot.verify()
            except HarnessError as drift:
                if process_error is not None:
                    raise HarnessError(
                        f"SNAPSHOT_MUTATED: {drift}; process failure retained as context: "
                        f"{type(process_error).__name__}: {process_error}"
                    ) from drift
                raise
