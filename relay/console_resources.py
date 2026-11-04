"""Bounded, inert project skill inventory and hash-checked UTF-8 previews."""

from contextlib import ExitStack, contextmanager
import hashlib
from itertools import islice
import json
import os
from pathlib import Path
import stat

_MAX_ENTRIES = 128
_MAX_BYTES = 32 * 1024
_LOCATIONS = ((".agents", "skills"), ("skills",))


def _windows_open(path: Path, directory: bool) -> int:
    """Pin a component without following reparse points or permitting replacement."""
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                       wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    info = kernel.GetFileInformationByHandleEx
    info.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    info.restype = wintypes.BOOL
    # OPEN_EXISTING, OPEN_REPARSE_POINT, BACKUP_SEMANTICS; share READ only.
    handle = create(str(path), 0x80000000, 1, None, 3, 0x02200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attributes = (wintypes.DWORD * 2)()  # FILE_ATTRIBUTE_TAG_INFO
        if not info(handle, 9, attributes, ctypes.sizeof(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())
        if attributes[0] & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("Linked skill path component")
        if bool(attributes[0] & stat.FILE_ATTRIBUTE_DIRECTORY) != directory:
            raise ValueError("Wrong skill path component type")
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        close(handle)
        raise
    return fd  # The CRT descriptor now owns the handle.


@contextmanager
def _pinned(path: Path, *, directory: bool = False):
    """Open every component, retaining parents until the operation finishes."""
    if ".." in path.parts:
        raise ValueError("Parent traversal is not allowed")
    path = path.absolute()  # Not resolve(): links must be rejected, not followed.
    if path.anchor.startswith("\\\\") or path.anchor.startswith("//"):
        raise ValueError("Network roots are not allowed")
    if os.name == "nt" and any(
            p.rstrip(" .") != p or Path(p).is_reserved() or ":" in p
            for p in path.parts[1:]):
        raise ValueError("Noncanonical Windows path component")
    with ExitStack() as stack:
        parent = None
        current = Path(path.anchor)
        components = (path.anchor, *path.parts[1:])
        for index, component in enumerate(components):
            is_directory = index < len(components) - 1 or directory
            if index:
                current /= component
            if os.name == "nt":
                fd = _windows_open(current, is_directory)
            else:
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if is_directory:
                    flags |= os.O_DIRECTORY
                fd = os.open(component, flags, dir_fd=parent)
            stack.callback(os.close, fd)
            mode = os.fstat(fd).st_mode
            if not (stat.S_ISDIR(mode) if is_directory else stat.S_ISREG(mode)):
                raise ValueError("Not a regular skill path")
            parent = fd
        yield fd, path


def _skill_parts(path: str) -> tuple[str, ...]:
    # Accept only canonical inventory paths, also on Windows (no ADS or drives).
    if not isinstance(path, str) or any(c in path for c in ("\\", ":", "\x00")):
        raise ValueError("Invalid skill path")
    parts = tuple(path.split("/"))
    if (any(p in ("", ".", "..") or p.rstrip(" .") != p for p in parts)
            or parts[-1] != "SKILL.md"
            or parts[:-2] not in _LOCATIONS):
        raise ValueError("Not an immediate project skill")
    return parts


def _read_skill(root: Path, path: str) -> tuple[str, str, int]:
    try:
        parts = _skill_parts(path)
        with _pinned(Path(root).joinpath(*parts)) as (fd, _):
            if os.fstat(fd).st_size > _MAX_BYTES:
                raise ValueError("Skill exceeds 32 KiB")
            with os.fdopen(os.dup(fd), "rb") as stream:
                data = stream.read(_MAX_BYTES + 1)
            if len(data) > _MAX_BYTES:
                raise ValueError("Skill exceeds 32 KiB")
            text = data.decode("utf-8", errors="strict")
            return text, hashlib.sha256(data).hexdigest(), len(data)
    except (OSError, UnicodeError) as exc:
        raise ValueError("Skill is missing, linked, unreadable or invalid UTF-8") from exc


def _scalar(value: str) -> str:
    """Read only single-line strings, never YAML tags, aliases or containers."""
    value = value.strip()
    if not value or value[0] in "!&*|>[{":
        return ""
    if value.startswith('"'):
        try:
            result = json.loads(value)
            return result if isinstance(result, str) else ""
        except ValueError:
            return ""
    if value.startswith("'"):
        return value[1:-1].replace("''", "'") if value.endswith("'") else ""
    return value.split(" #", 1)[0].strip()


def _metadata(text: str, fallback: str) -> tuple[str, str]:
    lines = text.splitlines()
    fields = {}
    body = lines
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, len(lines))
                    if lines[i].strip() in ("---", "...")), None)
        if end is not None:
            body = lines[end + 1:]
            for line in lines[1:end]:
                key, separator, value = line.partition(":")
                if separator and key in ("name", "description"):
                    fields[key] = _scalar(value)
    prose = next((line.strip() for line in body
                  if line.strip() and not line.lstrip().startswith("#")
                  and line.strip() not in ("---", "...")), "")
    return fields.get("name") or fallback, fields.get("description") or prose


def discover_skills(root: Path) -> list[dict]:
    """Inspect at most 128 entries per location; return valid skills sorted by path.

    Only the bounded prefix supplied by each directory iterator is inspected;
    sorting the returned inventory does not require an unbounded directory scan.
    """
    inventory = []
    for location in _LOCATIONS:
        try:
            with _pinned(Path(root).joinpath(*location), directory=True) as (fd, path):
                with os.scandir(path if os.name == "nt" else fd) as entries:
                    names = sorted(entry.name for entry in islice(entries, _MAX_ENTRIES))
                for name in names:
                    relative = "/".join((*location, name, "SKILL.md"))
                    try:
                        text, digest, size = _read_skill(root, relative)
                    except ValueError:
                        continue
                    title, description = _metadata(text, name)
                    inventory.append({"path": relative, "name": title,
                                      "description": description, "sha256": digest,
                                      "size_bytes": size})
        except (OSError, ValueError):
            continue
    return sorted(inventory, key=lambda skill: skill["path"])


def preview_skill(root: Path, path: str, expected_sha256: str) -> str:
    """Return exact UTF-8 text only if the current bytes match the inventory hash.

    Invalid paths, links, missing/nonregular files, oversized or malformed content,
    and stale inventory hashes all raise ValueError. No skill code is executed.
    """
    text, digest, _ = _read_skill(root, path)
    if digest != expected_sha256:
        raise ValueError("Skill changed since discovery")
    return text
