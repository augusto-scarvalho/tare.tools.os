"""Compose repository audit evidence with Kernel capabilities; no new agent loop."""
from pathlib import Path

from relay.audit_cases import checksum, json_bytes, require
from relay.relay_io import write_json_atomic

DEFAULT_TIMEOUT = 240


def configuration(value):
    from urllib.parse import urlsplit
    if value is None:  # Explicit compatibility profile for sealed packet-only cases.
        return None
    if isinstance(value, dict) and value.get('transport') == 'native-cli':
        require(set(value) <= {'transport', 'index_states'}, 'invalid_native_audit_config')
        states = value.get('index_states', {})
        require(isinstance(states, dict) and all(isinstance(k, str) and isinstance(v, str)
                and Path(v).is_absolute() for k, v in states.items()), 'explicit_index_states_required')
        return {'transport': 'native-cli', 'index_states': states}
    require(isinstance(value, dict) and set(value) <= {'api_base', 'timeout'}, 'invalid_exploration_config')
    from relay.native_catalog import local_gateway  # this machine's model gateway, never a fixed host
    config = {'api_base': local_gateway() + '/v1', 'timeout': DEFAULT_TIMEOUT, **value}
    address = urlsplit(config['api_base'])
    require(address.scheme in {'http', 'https'} and address.hostname and not address.username
            and not address.password and not address.query and not address.fragment,
            'invalid_explorer_endpoint')
    require(type(config['timeout']) is int and 0 < config['timeout'] <= 900, 'invalid_explorer_timeout')
    return config


def prepare_access(packet, run, case_directory, config, *, timeout=300):
    from compute_plane.audit_sources import repository_refs
    root = Path(run) / 'exploration'
    root.mkdir()
    manifest = {'schema': 'tare.tools/audit-exploration/2',
        'repositories': repository_refs(packet['repositories']),
        'packet_sha256': packet['sha256'], 'objective': packet['input']['intent'],
        'obligation_ids': [item['id'] for item in packet['input']['obligations']],
        'config': config, 'lock_path': str(root / '.explorer.lock'),
        'cancel_path': str(Path(run) / 'cancel.request')}
    if config.get('transport') == 'native-cli':
        import time
        require(set(config['index_states']) <= {repo['id'] for repo in manifest['repositories']},
                'index_repository_not_selected')
        manifest['expires_at'] = time.time() + timeout
    path = root / 'manifest.json'
    write_json_atomic(path, manifest)
    return path


def independent_prompt(packet, manifest, *, user_decision=None, previous_evidence=()):
    import json
    access = json.loads(Path(manifest).read_text(encoding='utf-8'))
    if access['config'].get('transport') == 'native-cli':
        import shlex
        from relay.native_audit_cli import command_prefix
        instructions = 'CURRENT EVIDENCE CLI: ' + shlex.join(command_prefix(manifest))
    else:
        raise ValueError('RETIRED_AIDER_AUDIT_ACCESS: historical cases require their archived host')
    direction = {'user_decision': user_decision, 'previous_evidence_locations': [
        {key: source[key] for key in ('id', 'repository', 'path', 'start_line')}
        for source in previous_evidence]}
    # The frontier receives navigation references, not another copy of source
    # files/diffs. It obtains exact code through the live read capability.
    recommendation = {**packet['input'], 'sources': [
        {key: value for key, value in source.items() if key not in {'text', 'segments'}}
        for source in packet['input']['sources']]}
    return (instructions + ' Prior evidence locations are navigation only; read sources '
        'again for IDs citable in this attempt.\nHOST CONTINUITY (final artifacts only): '
        + json_bytes(direction).decode() + '\nLIVE REPOSITORY REFERENCES: '
        + json_bytes(access['repositories']).decode()
        + '\nIMPLEMENTER RECOMMENDATION AND CURRENT OBJECTIVE:\n').encode() + json_bytes(recommendation)


def collect_access(manifest_path):
    """Read only terminal explorer artifacts and exact evidence, never its chat."""
    import json
    config = json.loads(Path(manifest_path).read_text(encoding='utf-8'))['config']
    if config.get('transport') == 'native-cli':
        from relay.native_audit_access import collect
        return collect(manifest_path)
    raise ValueError('RETIRED_AIDER_AUDIT_ACCESS: historical cases require their archived host')
