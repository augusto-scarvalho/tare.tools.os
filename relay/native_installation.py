"""Small installation descriptor: code location and centralized mutable storage."""
import json
import os
from pathlib import Path


def descriptor_path():
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home()/'.config')))/'tare.tools'/'native-installation.json'


def settings():
    if os.environ.get('TARE_MANAGED_HOME'):
        return {}  # A managed release does not inherit a legacy development descriptor.
    file = descriptor_path()
    if not file.exists():
        return {}
    with file.open('rb') as stream:
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise ValueError('native_installation_descriptor_over_budget')
    value = json.loads(raw)
    if value.get('schema') != 'tare.tools/native-installation/1':
        raise ValueError('native_installation_descriptor_invalid')
    return value


def storage_root():
    configured = os.environ.get('TARE_STORAGE_ROOT') or settings().get('storage_root')
    if configured:
        root = Path(configured)
        if not root.is_absolute():
            raise ValueError('native_storage_absolute_root_required')
        return root
    source = Path(__file__).resolve().parents[1]
    if (source/'work-graph.json').is_file():
        return source.parent/'.reconciliation'
    raise ValueError('native_storage_not_configured: set TARE_STORAGE_ROOT or install a descriptor')


def assessment_policy(name):
    """An owner's assessment policy file (flows.json, default.json) in the installation's storage, under
    assessment-policies; None when it is absent or no storage is configured."""
    try:
        path = storage_root()/'assessment-policies'/name
    except ValueError:
        return None
    return path if path.is_file() else None


def default_storage():
    """The TUI's history folder when started without --storage-root: the configured storage, else the
    installation's workspace-state. tare serve reaches the TUI's per-project files through it."""
    try:
        return storage_root()
    except ValueError as exc:
        if 'native_storage_not_configured' not in str(exc):
            raise
        return descriptor_path().parent/'workspace-state'


def workspace_storage(project, preferred, fallback=None):
    """Choose external custody for a console workspace without moving existing records."""
    project = Path(project).resolve()
    if not project.is_dir():
        raise ValueError('Workspace folder is unavailable: '+str(project))
    preferred = Path(preferred).resolve()
    fallback = Path(fallback or descriptor_path().parent/'workspace-state').resolve()
    chosen = fallback if preferred.is_relative_to(project) else preferred
    if chosen.is_relative_to(project):
        raise ValueError('The workspace contains its history folder: '+str(chosen)+
                         '. Choose a smaller workspace or a history folder outside it.')
    return chosen


def version_label():
    """Describe the code actually loaded, including explicit development checkouts."""
    import importlib.metadata
    source = Path(__file__).resolve().parents[1]
    if (source/'pyproject.toml').is_file() and (source/'work-graph.json').is_file():
        import tomllib
        version = tomllib.loads((source/'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
        return 'tare.tools '+version+' · Development · '+str(source)
    version = importlib.metadata.version('tare-tools-os')
    release = os.environ.get('TARE_RELEASE_ID')
    return 'tare.tools '+version+(' · Release '+release[:12] if release else ' · Installed (unmanaged)')


def main(argv=None):
    """Installed entry, with explicit OS control root independent of package files."""
    import argparse
    import sys
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument('--os-root', type=Path)
    argv = list(sys.argv[1:] if argv is None else argv)
    boundary = argv.index('--verify-command') if '--verify-command' in argv else len(argv)
    args, remaining = parser.parse_known_args(argv[:boundary])
    remaining += argv[boundary:]
    from .execution_scope import console_option
    # A folder opens the same console as the explicit subcommand. Keep command
    # names unambiguous: use ./status for a folder named like a CLI command.
    if not remaining:
        remaining = ['console']
    elif console_option(remaining[0]) or remaining[0] in ('--root','--storage-root'):
        remaining = ['console',*remaining]
    elif remaining[0] == '--':
        remaining = ['console', '--root', *remaining[1:]]
    elif (remaining[0] in ('.', '..', '~') or remaining[0].startswith('~')
          or '/' in remaining[0] or '\\' in remaining[0]):
        remaining = ['console', '--root', *remaining]
    if remaining[:1] == ['research-synthesis']:
        from .research_synthesis import main as synthesize
        return synthesize(remaining[1:])
    selected = args.os_root or os.environ.get('TARE_OS_ROOT') or settings().get('os_root')
    if selected:
        from . import relay_mesh
        sys.modules['relay_mesh'] = relay_mesh
        root = Path(selected).resolve()
        graph_optional = remaining[:1] in (['models'], ['console'], ['agent'], ['sessions'], ['plan'], ['profiles'], ['mcp'], ['serve'], ['ci'], ['gpu'], ['fleet'], ['setup'],
                                           ['parallel-explore'], ['research-flow'], ['findings'], ['workflow'])
        if not (root/'work-graph.json').is_file() and not graph_optional:
            raise ValueError('native_os_control_root_missing')
        relay_mesh.rebind_root(root)
    from tare_ops import main as run
    return run(remaining)


def doctor(**options):
    """Compatibility entry for the read-only native diagnostic owner."""
    from .native_diagnostics import doctor as inspect
    return inspect(**options)
