import subprocess
import shutil
from pathlib import Path


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def test_post_commit_pushes_work_branch_but_not_protected_branch(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    hook = Path(__file__).parents[1] / ".githooks" / "post-commit"
    isolated_hooks = tmp_path / "hooks"
    isolated_hooks.mkdir()
    isolated_post_commit = isolated_hooks / "post-commit"
    shutil.copy2(hook, isolated_post_commit)
    isolated_post_commit.chmod(0o755)

    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "init", str(work))
    _git(work, "config", "user.name", "Hook Smoke")
    _git(work, "config", "user.email", "hook@example.invalid")
    _git(work, "config", "core.hooksPath", str(isolated_hooks))
    _git(work, "remote", "add", "origin", str(remote))

    _git(work, "switch", "-c", "feat/hook-smoke")
    (work / "probe.txt").write_text("feature\n", encoding="utf-8")
    _git(work, "add", "probe.txt")
    _git(work, "commit", "-m", "feature")
    assert _git(remote, "rev-parse", "refs/heads/feat/hook-smoke").stdout.strip() == _git(
        work, "rev-parse", "HEAD"
    ).stdout.strip()

    _git(work, "switch", "-c", "main")
    (work / "probe.txt").write_text("main\n", encoding="utf-8")
    _git(work, "commit", "-am", "main")
    assert _git(remote, "show-ref", "--verify", "refs/heads/main", check=False).returncode != 0
