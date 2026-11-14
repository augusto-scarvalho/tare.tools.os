"""Explicit host composition of an MXC launcher (or, on Linux, bubblewrap's) with existing command mediation."""
from pathlib import Path

from compute_plane.codex_sandbox import CodexSandboxProfile
from compute_plane.mediated_tools import validate_command_confinement
from compute_plane.windows_mxc import WindowsMxcProfile


GIB = 1024**3


def job_limits(base, total=None):
    """A sandboxed command's Windows job limits (processes, bytes for one process, bytes for its whole tree), sized by
    the machine instead of fixed: the tree may use a quarter of the machine's memory, at least 2 GiB and at most 4 GiB,
    and one process at most 2 GiB (the Kernel's WindowsJob accepts no more of either). With the launcher's fixed
    256 MiB a process and 512 MiB a tree, Godot's import of a small project, which peaks at 644 MiB, died in the
    sandbox (measured 2026-11-14). The same machine gives the same limits every time, so a check's verdict does not
    depend on what else is open. The process count stays the launcher's. `total`: the machine's memory in bytes."""
    if total is None:
        import ctypes
        class Status(ctypes.Structure):
            _fields_ = [('length', ctypes.c_uint32), ('load', ctypes.c_uint32), *((name, ctypes.c_uint64) for name in (
                'total', 'available', 'total_page', 'available_page', 'total_virtual', 'available_virtual', 'extended'))]
        status = Status(length=ctypes.sizeof(Status))
        try:
            total = status.total if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)) else 0
        except (AttributeError, OSError):
            total = 0  # unknown: the floor
    tree = min(4*GIB, max(2*GIB, total//4))
    return base[0], min(2*GIB, tree), tree


def windows_isolation_factory(*, backend, policy, cwd, environment, seconds,
                              mediation_factory, codex_executable=None,
                              codex_sha256=None, verification_seconds=None, staged_effects=False,
                              unbounded_verification=False):
    """Return a native_agent command_mediation_factory; no execution or new authority.

    The host supplies the existing assessment/authorization/receipt mediator.
    Each binding gets one launcher and the existing capabilities own its lifetime.
    There is no backend fallback, CLI discovery or model-selected configuration.
    """
    if backend not in ('mxc', 'codex', 'bwrap'):
        raise ValueError('windows_isolation_backend_required')
    if type(staged_effects) is not bool:
        raise ValueError('staged_effects_must_be_bool')
    if type(unbounded_verification) is not bool:
        raise ValueError('unbounded_verification_must_be_bool')
    if not callable(mediation_factory):
        raise ValueError('command_mediation_factory_required')
    environment, seconds = validate_command_confinement(lambda: None, environment, seconds)
    if backend != 'codex' and (codex_executable is not None or codex_sha256 is not None):
        raise ValueError('native_mxc_does_not_use_codex')
    if backend == 'codex' and (not codex_executable or not codex_sha256):
        raise ValueError('admitted_codex_executable_required')
    root = Path(cwd).resolve(strict=True)

    def bind(**session):
        capabilities = session['capabilities']
        repositories = session['config'].get('repositories')
        if (not isinstance(repositories, dict) or not repositories
                or any(Path(value).resolve(strict=True) != root for value in repositories.values())
                or getattr(capabilities, 'terminal', None) is not None):
            raise ValueError('windows_isolation_repository_or_terminal_mismatch')
        if backend == 'mxc':
            launcher = WindowsMxcProfile(policy, cwd=str(root), environment=environment)
        elif backend == 'bwrap':
            from compute_plane.linux_bwrap import LinuxBwrapProfile
            launcher = LinuxBwrapProfile(policy, cwd=str(root), environment=environment)
        else:
            launcher = CodexSandboxProfile(policy, executable=codex_executable,
                expected_executable_sha256=codex_sha256, cwd=str(root), environment=environment)
        if backend in ('mxc', 'codex'):
            launcher.job_limits = job_limits(launcher.job_limits)
        mediator = mediation_factory(**session)
        if not callable(getattr(mediator, 'execute', None)):
            raise ValueError('command_mediation_unavailable')
        if backend == 'bwrap':
            execute = mediator.execute

            def kept(facts, effect):
                # A check refused before it runs (bwrap_scope_changed, bwrap_denied_path_changed, an OSError from
                # its mediation or from Popen) raises; the Kernel then keeps only the error's type once the check
                # started, and an OSError's type always, so the mediator keeps why, for its host's receipt.
                mediator.launch_refused = None
                try:
                    return execute(facts, effect)
                except (ValueError, OSError) as error:
                    mediator.launch_refused = str(error)
                    raise
            mediator.execute = kept
        capabilities.bind_command_confinement(launcher, environment, seconds,
            **({'verification_seconds':verification_seconds} if verification_seconds is not None else {}),
            **({'unbounded_verification':True} if unbounded_verification else {}),
            **({'staged_effects':True} if staged_effects else {}))
        return mediator

    return bind
