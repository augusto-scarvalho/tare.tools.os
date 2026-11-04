"""Refresh size/sha256 of every relay module in the disposition manifest (contracts/v1); add new ones given on argv
as name=summary; drop those whose file is gone. Deterministic from the files, so a merge conflict in the manifest is resolved by running it on the
merged tree (tare ci shepherd does). Usage: python scripts/sync_disposition_manifest.py REPO [name=summary ...]"""
import hashlib
import json
import sys
from pathlib import Path

repo = Path(sys.argv[1])
path = repo/'contracts'/'v1'/'harness_disposition_manifest.json'
manifest = json.loads(path.read_text(encoding='utf-8'))
known = {m['name'] for m in manifest['modules'] if m['path'].startswith('tare.tools.os/relay/')}
for item in sys.argv[2:]:
    name, _, summary = item.partition('=')
    if name not in known:
        manifest['modules'].append({'name': name, 'path': 'tare.tools.os/relay/' + name, 'summary': summary,
            'disposition': 'RETAIN_CORE', 'target_repository': 'tare.tools.os',
            'rationale': 'OS owns bounded exploration composition, operator settings and shared delivery accounting.',
            'size_bytes': 0, 'sha256': ''})
gone = [m['name'] for m in manifest['modules']
        if m['path'].startswith('tare.tools.os/relay/') and not (repo/'relay'/m['name']).is_file()]
manifest['modules'] = [m for m in manifest['modules']
                       if not (m['path'].startswith('tare.tools.os/relay/') and m['name'] in gone)]
changed = []
for module in manifest['modules']:
    if not module['path'].startswith('tare.tools.os/relay/'):
        continue
    raw = (repo/'relay'/module['name']).read_bytes().replace(b'\r\n', b'\n')
    value = (len(raw), hashlib.sha256(raw).hexdigest())
    if value != (module['size_bytes'], module['sha256']):
        module['size_bytes'], module['sha256'] = value
        changed.append(module['name'])
manifest['total_modules'] = len(manifest['modules'])
path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
print(manifest['total_modules'], changed, 'gone:', gone)
