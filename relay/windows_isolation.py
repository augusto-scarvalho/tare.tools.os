"""Explicit host composition of an MXC launcher with existing command mediation."""
from pathlib import Path

from compute_plane.codex_sandbox import CodexSandboxProfile
from compute_plane.mediated_tools import validate_command_confinement
from compute_plane.windows_mxc import WindowsMxcProfile


def windows_isolation_factory(*, backend, policy, cwd, environment, seconds,
                              mediation_factory, codex_executable=None,
                              codex_sha256=None, verification_seconds=None, staged_effects=False,
                              unbounded_verification=False):
    """Return a native_agent command_mediation_factory; no execution or new authority.

    The host supplies the existing assessment/authorization/receipt mediator.
    Each binding gets one launcher and the existing capabilities own its lifetime.
    There is no backend fallback, CLI discovery or model-selected configuration.
    """
    if backend not in ('mxc', 'codex'):
        raise ValueError('windows_isolation_backend_required')
    if type(staged_effects) is not bool:
        raise ValueError('staged_effects_must_be_bool')
    if type(unbounded_verification) is not bool:
        raise ValueError('unbounded_verification_must_be_bool')
    if not callable(mediation_factory):
        raise ValueError('command_mediation_factory_required')
    environment, seconds = validate_command_confinement(lambda: None, environment, seconds)
    if backend == 'mxc' and (codex_executable is not None or codex_sha256 is not None):
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
        else:
            launcher = CodexSandboxProfile(policy, executable=codex_executable,
                expected_executable_sha256=codex_sha256, cwd=str(root), environment=environment)
        mediator = mediation_factory(**session)
        if not callable(getattr(mediator, 'execute', None)):
            raise ValueError('command_mediation_unavailable')
        capabilities.bind_command_confinement(launcher, environment, seconds,
            **({'verification_seconds':verification_seconds} if verification_seconds is not None else {}),
            **({'unbounded_verification':True} if unbounded_verification else {}),
            **({'staged_effects':True} if staged_effects else {}))
        return mediator

    return bind
