"""Project skill inventory and preview boundaries; all fixtures stay in tmp_path."""

import hashlib
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from relay import console_resources as resources


def _write(root, relative, data=b"# Heading\n\nUseful prose.\n"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def test_both_locations_metadata_and_exact_preview(tmp_path):
    data = ('---\r\nname: "Café helper"\r\n'
            "description: 'It''s useful: yes'\r\n---\r\n# Heading\r\nBody.\r\n").encode()
    _write(tmp_path, ".agents/skills/zeta/SKILL.md", data)
    _write(tmp_path, "skills/alpha/SKILL.md")
    _write(tmp_path, "skills/alpha/nested/SKILL.md")
    _write(tmp_path, "elsewhere/ignored/SKILL.md")
    _write(tmp_path, "skills/SKILL.md")
    (tmp_path / "skills/empty").mkdir()
    inventory = resources.discover_skills(tmp_path)
    assert inventory == [
        {"path": ".agents/skills/zeta/SKILL.md", "name": "Café helper",
         "description": "It's useful: yes", "sha256": _digest(data),
         "size_bytes": len(data)},
        {"path": "skills/alpha/SKILL.md", "name": "alpha",
         "description": "Useful prose.",
         "sha256": _digest(b"# Heading\n\nUseful prose.\n"), "size_bytes": 25},
    ]
    assert resources.preview_skill(tmp_path, inventory[0]["path"],
                                   inventory[0]["sha256"]) == data.decode()


@pytest.mark.parametrize("frontmatter, name, description", [
    ("name: Plain # comment\ndescription: A plain description", "Plain", "A plain description"),
    ("name: !include outside\ndescription: *reference", "demo", "Fallback prose."),
    ("name: [not, a, scalar]\ndescription: |\n  multiline", "demo", "Fallback prose."),
    ("name: ''", "demo", "Fallback prose."),
])
def test_single_line_metadata_is_inert(tmp_path, frontmatter, name, description):
    _write(tmp_path, "skills/demo/SKILL.md",
           f"---\n{frontmatter}\n---\n# Title\n\nFallback prose.\n".encode())
    [skill] = resources.discover_skills(tmp_path)
    assert (skill["name"], skill["description"]) == (name, description)


def test_bounded_iteration_and_sorted_output(tmp_path, monkeypatch):
    names = [f"skill-{i:03}" for i in reversed(range(130))]
    for location in ("skills", ".agents/skills"):
        for name in names:
            _write(tmp_path, f"{location}/{name}/SKILL.md", b"ok")
    counts = []

    class BoundedEntries:
        def __enter__(self):
            self.count = 0
            counts.append(self)
            return self

        def __exit__(self, *args):
            pass

        def __iter__(self):
            return self

        def __next__(self):
            assert self.count < 128, "enumerated beyond directory budget"
            name = names[self.count]
            self.count += 1
            return SimpleNamespace(name=name)

    with monkeypatch.context() as patch:
        patch.setattr(resources.os, "scandir", lambda _: BoundedEntries())
        first = resources.discover_skills(tmp_path)
        assert first == resources.discover_skills(tmp_path)
    paths = [skill["path"] for skill in first]
    assert len(first) == 256
    assert paths == sorted(paths)
    assert all(entry.count == 128 for entry in counts)
    assert not any("skill-000/" in path or "skill-001/" in path for path in paths)


def test_invalid_entries_count_toward_directory_budget(tmp_path, monkeypatch):
    (tmp_path / "skills").mkdir()
    _write(tmp_path, "skills/valid/SKILL.md")
    real_scandir = os.scandir

    class Entries:
        def __enter__(self):
            return iter([SimpleNamespace(name=f"missing-{i}") for i in range(128)]
                        + [SimpleNamespace(name="valid")])

        def __exit__(self, *args):
            pass

    with monkeypatch.context() as patch:
        patch.setattr(resources.os, "scandir", lambda _: Entries())
        assert resources.discover_skills(tmp_path) == []
    assert os.scandir is real_scandir


def test_changed_and_missing_preview_rejected(tmp_path):
    path = _write(tmp_path, "skills/demo/SKILL.md", b"original")
    [skill] = resources.discover_skills(tmp_path)
    path.write_bytes(b"modified")  # Same byte count: timestamps/size are insufficient.
    with pytest.raises(ValueError, match="changed"):
        resources.preview_skill(tmp_path, skill["path"], skill["sha256"])
    path.unlink()
    with pytest.raises(ValueError):
        resources.preview_skill(tmp_path, skill["path"], skill["sha256"])


@pytest.mark.parametrize("path", [
    "../outside/SKILL.md", "skills/../SKILL.md", "skills/.. /SKILL.md",
    "skills/./SKILL.md", "/skills/demo/SKILL.md", "skills//demo/SKILL.md",
    "skills/demo/nested/SKILL.md", "other/demo/SKILL.md", "skills/SKILL.md",
    "C:/outside/SKILL.md", "skills\\demo\\SKILL.md", "skills/demo/SKILL.md:stream",
    "skills/demo/SKILL.md\x00", "", "skills/demo/SKILL.md/",
    "//server/share/SKILL.md", "skills/demo./SKILL.md",
])
def test_non_inventory_paths_rejected(tmp_path, path):
    _write(tmp_path, "skills/demo/SKILL.md", b"ok")
    with pytest.raises(ValueError):
        resources.preview_skill(tmp_path, path, _digest(b"ok"))


def test_absolute_outside_path_rejected(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = _write(tmp_path, "outside/SKILL.md", b"outside")
    with pytest.raises(ValueError):
        resources.preview_skill(root, outside.as_posix(), _digest(b"outside"))
    assert resources.discover_skills(root) == []


@pytest.mark.parametrize("data", [b"x" * (32768 + 1), b"\xff", b"truncated\xe2\x82"],
                         ids=["oversized", "invalid-leading-byte", "truncated-utf8"])
def test_oversized_or_invalid_utf8_omitted_and_preview_rejected(tmp_path, data):
    _write(tmp_path, "skills/bad/SKILL.md", data)
    assert resources.discover_skills(tmp_path) == []
    with pytest.raises(ValueError):
        resources.preview_skill(tmp_path, "skills/bad/SKILL.md", _digest(data))


def test_exact_size_limit_and_empty_file(tmp_path):
    data = ("é" * 16384).encode()
    _write(tmp_path, "skills/limit/SKILL.md", data)
    _write(tmp_path, "skills/empty/SKILL.md", b"")
    inventory = resources.discover_skills(tmp_path)
    assert [s["size_bytes"] for s in inventory] == [0, 32768]
    assert resources.preview_skill(tmp_path, "skills/limit/SKILL.md", _digest(data)) == data.decode()
    assert resources.preview_skill(tmp_path, "skills/empty/SKILL.md", _digest(b"")) == ""


def test_nonregular_skill_rejected(tmp_path):
    (tmp_path / "skills/demo/SKILL.md").mkdir(parents=True)
    assert resources.discover_skills(tmp_path) == []
    with pytest.raises(ValueError):
        resources.preview_skill(tmp_path, "skills/demo/SKILL.md", _digest(b""))


@pytest.mark.parametrize("component", ["root", "ancestor", ".agents", ".agents/skills",
                                       ".agents/skills/demo", ".agents/skills/demo/SKILL.md"])
def test_symlink_components_rejected(tmp_path, component):
    project = tmp_path / "project"
    path = _write(project, ".agents/skills/demo/SKILL.md", b"ok")
    relative = ".agents/skills/demo/SKILL.md"
    if component in ("root", "ancestor"):
        link = tmp_path / "link"
        target = project if component == "root" else tmp_path
        root = link if component == "root" else link / "project"
    else:
        link = project / component
        target = tmp_path / "relocated"
        link.rename(target)
        root = project
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Symlinks unavailable: {exc}")
    assert resources.discover_skills(root) == []
    with pytest.raises(ValueError):
        resources.preview_skill(root, relative, _digest(b"ok"))


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
def test_junction_component_rejected(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    target = tmp_path / "outside"
    _write(target, "demo/SKILL.md", b"ok")
    junction = project / "skills"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(target)],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        pytest.skip(f"Junction creation unavailable: {result.stderr}")
    try:
        assert resources.discover_skills(project) == []
        with pytest.raises(ValueError):
            resources.preview_skill(project, "skills/demo/SKILL.md", _digest(b"ok"))
    finally:
        junction.rmdir()
