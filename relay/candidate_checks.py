"""The OS repository's existing generated-manifest contract, compiled before execution."""
from pathlib import Path
import json
import tomllib
import hashlib
import re

MANIFEST='contracts/v1/harness_disposition_manifest.json'
RUNNER='scripts/ops/run_candidate_checks.py'


class ScopeRepair(ValueError):
    """The declared repository contract cannot be executed inside this plan's paths."""


def native_project(root):
    root=Path(root)
    try:
        project=tomllib.loads((root/'pyproject.toml').read_text(encoding='utf8'))['project']['name']
    except (OSError,ValueError,KeyError):return False
    return project=='tare-tools-os' and (root/'scripts/sync_disposition_manifest.py').is_file()


def applies(root, unit):
    return native_project(root) and any(p.startswith('relay/') and p.endswith('.py') for p in unit['target_paths'])


def admit(root, unit):
    if not applies(root,unit):return
    if MANIFEST not in unit['target_paths']:
        raise ScopeRepair('The executable plan omits the OS generated disposition manifest. '
            'Repair only the saved plan scope: include '+MANIFEST+' in target_paths; preserve all criteria and the candidate.')
    test='tests/test_disposition_manifest_compliance.py'
    if (Path(root)/test).is_file() and test not in unit['test_paths']:
        unit['test_paths'].append(test)


def command(root, unit, python, tests):
    if not native_project(root) or not tests:return None
    if applies(root,unit) and MANIFEST not in unit['target_paths']:return None
    if not (Path(root)/RUNNER).is_file():
        raise ValueError('The OS generated-check runner is missing; no test command was substituted.')
    # The project interpreter supplies pytest; the running product supplies its
    # paired satellites. Never import the host's relay instead of the candidate.
    import importlib.util
    roots=[]
    for name in ('compute_plane','tare_tools_agent_runtime','specgraph','graph_backlog'):
        spec=importlib.util.find_spec(name)
        if spec is None or not spec.origin:raise ValueError('Missing paired test dependency: '+name)
        path=str(Path(spec.origin).resolve().parent.parent)
        if path not in roots:roots.append(path)
    return [python,'-B',RUNNER,'--targets-json',json.dumps(unit['target_paths']),
        '--runtime-paths-json',json.dumps(roots),*tests]


def source_digest(root):
    root=Path(root).resolve();relay=(root/'relay').resolve();sources=[]
    if not relay.is_relative_to(root):raise ValueError('Generated source redirects outside the project.')
    for path in sorted(relay.rglob('*.py')):
        if not path.resolve().is_relative_to(relay.resolve()):
            raise ValueError('Generated source redirects outside the relay root.')
        raw=path.read_bytes().replace(b'\r\n',b'\n')
        sources.append((path.relative_to(root).as_posix(),hashlib.sha256(raw).hexdigest()))
    return hashlib.sha256(json.dumps(sources,separators=(',',':')).encode()).hexdigest()


def authored_manifest(value):
    return {**value,'modules':[{k:v for k,v in row.items() if k not in {'size_bytes','sha256'}}
                               for row in value['modules']]}


def retained_baseline(root,unit,candidate,observed):
    """Prove only generated fingerprint drift against the hash-anchored original Git blob.

    This does not change the saved candidate or approve it; fresh checks and review still run.
    Missing anchors, changed authored fields and all other changed dependencies fail closed.
    """
    from .challenger import git
    repository=unit['repository'];key=repository+':'+MANIFEST
    changed={k for k in observed if observed[k]!=candidate['basis'].get(k)}
    if changed!={key} or MANIFEST not in unit['target_paths']:
        return None
    patch=Path(candidate['patch']).read_bytes()
    marker=('diff --git a/'+MANIFEST+' b/'+MANIFEST+'\n').encode()
    if patch.count(marker)==0:
        # The candidate never edits the generated manifest: only the project's copy moved under it. Its patch still
        # applies, and the fresh checks and review run against the project as it is now.
        try:
            if hashlib.sha256((Path(root)/MANIFEST).read_bytes()).hexdigest()!=observed[key]:return None
        except OSError:
            return None
        return {'kind':'REQUALIFIED_UNTOUCHED_GENERATED_FILE','files':[MANIFEST],
            'before_sha256':candidate['basis'][key],'after_sha256':observed[key]}
    if patch.count(marker)!=1 or not applies(root,unit):return None
    section=patch.split(marker,1)[1].split(b'\ndiff --git ',1)[0]
    match=re.search(rb'^index ([0-9a-f]{7,64})\.\.[0-9a-f]{7,64}(?: 100644)?$',section,re.M)
    if match is None:return None
    anchor=match[1].decode()
    try:
        if not 0<int(git(root,'cat-file','-s',anchor))<=2*1024*1024:return None
        original=git(root,'cat-file','blob',anchor)
        # Git stores LF; a Windows source receipt may have captured CRLF bytes.
        if candidate['basis'][key] not in {hashlib.sha256(original).hexdigest(),
                hashlib.sha256(original.replace(b'\n',b'\r\n')).hexdigest()}:
            return None
        current=(Path(root)/MANIFEST).read_bytes()
        if hashlib.sha256(current).hexdigest()!=observed[key]:return None
        if authored_manifest(json.loads(original))!=authored_manifest(json.loads(current)):return None
    except (OSError,ValueError,KeyError,TypeError):
        return None
    return {'kind':'REQUALIFIED_GENERATED_BASELINE','files':[MANIFEST],
        'original_blob':anchor,'before_sha256':candidate['basis'][key],
        'after_sha256':observed[key],'authored_fields_preserved':True}


def prepare_baseline(tree, unit, base, *, retained_patch=None):
    """Refresh derived fingerprints in the private baseline; preserve all authored manifest fields."""
    from .challenger import git
    tree=Path(tree)
    if not applies(tree,unit) or MANIFEST not in unit['target_paths']:return base,[],None
    path=tree/MANIFEST
    original=json.loads(path.read_text(encoding='utf8'))
    excluded=[]
    if retained_patch is not None:
        marker=('diff --git a/'+MANIFEST+' b/'+MANIFEST).encode()
        with Path(retained_patch).open('rb') as stream:
            contains=any(line.rstrip(b'\r\n')==marker for line in stream)
        if contains:
            git(tree,'apply','--cached','--include='+MANIFEST,str(retained_patch))
            try:proposed=json.loads(git(tree,'show',':'+MANIFEST))
            finally:git(tree,'reset','-q','HEAD','--',MANIFEST)
            # Only a purely derived patch may be excluded. New module records
            # and other authored changes must remain in the literal patch;
            # git apply will reject any real conflict with the refreshed base.
            if authored_manifest(original)==authored_manifest(proposed):excluded=[MANIFEST]
    changed=[]
    relay=(tree/'relay').resolve()
    if not relay.is_relative_to(tree.resolve()):raise ValueError('Generated source redirects outside the candidate.')
    for module in original['modules']:
        if not module['path'].startswith('tare.tools.os/relay/'):continue
        source=(relay/module['name']).resolve()
        if not source.is_relative_to(relay) or not source.is_file():
            raise ValueError('Generated source is missing or outside the admitted relay root.')
        raw=source.read_bytes().replace(b'\r\n',b'\n')
        size,fingerprint=len(raw),hashlib.sha256(raw).hexdigest()
        if (module['size_bytes'],module['sha256'])!=(size,fingerprint):
            changed.append(module['name']);module.update(size_bytes=size,sha256=fingerprint)
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    prepared=base
    if changed:
        path.write_text(json.dumps(original,indent=2,ensure_ascii=False)+'\n',encoding='utf8',newline='\n')
        git(tree,'add','--',MANIFEST)
        git(tree,'-c','user.name=tare verifier','-c','user.email=verifier@localhost',
            '-c','core.hooksPath=','-c','commit.gpgsign=false','commit','--only',
            '-m','Prepare existing generated manifest baseline','--',MANIFEST)
        prepared=git(tree,'rev-parse','HEAD').decode().strip()
    return prepared,excluded,{'kind':'GENERATED_BASELINE_FINGERPRINTS','files':[MANIFEST],
        'modules':changed,'base_before':base,'base_after':prepared,'manifest_before_sha256':before,
        'manifest_after_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'source_sha256':source_digest(tree),
        'authored_fields_preserved':True,'retained_generated_patch_excluded':bool(excluded)}
