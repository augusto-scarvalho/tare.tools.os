import json
from pathlib import Path
import subprocess
import sys

import pytest
from relay import candidate_checks
import hashlib


def owner(tmp_path):
    (tmp_path/'pyproject.toml').write_text('[project]\nname="tare-tools-os"\n')
    script=tmp_path/'scripts/sync_disposition_manifest.py';script.parent.mkdir(parents=True)
    script.write_text('')
    return tmp_path


def test_owner_scope_rejects_omitted_generated_file_before_execution(tmp_path):
    root=owner(tmp_path)
    unit={'target_paths':['relay/a.py'],'test_paths':[]}
    with pytest.raises(ValueError,match='omits the OS generated disposition manifest'):
        candidate_checks.admit(root,unit)
    check=root/'tests/test_disposition_manifest_compliance.py';check.parent.mkdir();check.touch()
    unit['target_paths'].append(candidate_checks.MANIFEST)
    candidate_checks.admit(root,unit)
    assert unit['test_paths']==['tests/test_disposition_manifest_compliance.py']
    (root/'pyproject.toml').write_text('[project]\nname="other-project"\n')
    assert not candidate_checks.applies(root,unit)


def test_owned_check_prepares_only_admitted_files_and_uses_focused_runner(tmp_path):
    root=owner(tmp_path);source=Path(__file__).resolve().parents[1]
    (root/'relay').mkdir();(root/'relay/a.py').write_text('value = 1\n')
    manifest=root/candidate_checks.MANIFEST;manifest.parent.mkdir(parents=True)
    manifest.write_text('{}')
    (root/'outside.txt').write_text('Keep this out of the index')
    script=root/'scripts/sync_disposition_manifest.py'
    script.write_text("from pathlib import Path\nimport hashlib,json\nPath("+repr(candidate_checks.MANIFEST)+
        ").write_text(json.dumps({'digest':hashlib.sha256(Path('relay/a.py').read_bytes()).hexdigest()}))\n")
    (root/'scripts/ops').mkdir()
    for name in ('run_candidate_checks.py','run_focused_tests.py'):
        (root/'scripts/ops'/name).write_bytes((source/'scripts/ops'/name).read_bytes())
    tests=root/'tests';tests.mkdir()
    (tests/'test_contract.py').write_text("from pathlib import Path\nimport hashlib,json,subprocess\n"
        "def test_exact_candidate():\n"
        " assert json.loads(Path("+repr(candidate_checks.MANIFEST)+").read_text())['digest']==hashlib.sha256(Path('relay/a.py').read_bytes()).hexdigest()\n"
        " assert subprocess.check_output(['git','show',':relay/a.py'])==Path('relay/a.py').read_bytes().replace(b'\\r\\n',b'\\n')\n")
    def git(*args):return subprocess.run(['git',*args],cwd=root,check=True,capture_output=True,text=True)
    git('init');git('add','relay/a.py',candidate_checks.MANIFEST)
    git('-c','user.name=Fixture','-c','user.email=fixture@localhost','-c','commit.gpgsign=false','commit','-m','Base')
    (root/'relay/a.py').write_text('value = 2\n')
    unit={'target_paths':['relay/a.py',candidate_checks.MANIFEST,'tests/test_contract.py']}
    argv=candidate_checks.command(root,unit,sys.executable,['tests/test_contract.py'])
    result=subprocess.run(argv,cwd=root,capture_output=True,text=True)
    assert result.returncode==0,result.stdout+result.stderr
    assert '1 passed' in result.stdout and '"scratch_removed": true' in result.stdout
    expected={'relay/a.py',candidate_checks.MANIFEST}
    assert set(git('diff','--cached','--name-only').stdout.splitlines())==expected
    prepared=next(json.loads(s) for s in result.stdout.splitlines() if s.startswith('{"host_prepared_staged_paths"'))
    assert set(prepared['host_prepared_staged_paths'])==expected


def test_non_relay_candidate_uses_its_own_source_and_paired_kernel_without_staging(tmp_path):
    root=owner(tmp_path);source=Path(__file__).resolve().parents[1]
    (root/'relay').mkdir();(root/'relay/__init__.py').write_text('CANDIDATE = True\n')
    (root/'scripts/sync_disposition_manifest.py').write_text("raise AssertionError('No relay change was admitted')\n")
    (root/'scripts/ops').mkdir()
    for name in ('run_candidate_checks.py','run_focused_tests.py'):
        (root/'scripts/ops'/name).write_bytes((source/'scripts/ops'/name).read_bytes())
    import compute_plane
    expected=str(Path(compute_plane.__file__).resolve())
    (root/'tests').mkdir()
    (root/'tests/test_pair.py').write_text('import relay, compute_plane\n'
        'from compute_plane.managed_process import host_executable\n'
        'def test_pair():\n assert relay.CANDIDATE\n'
        ' assert str(__import__("pathlib").Path(compute_plane.__file__).resolve()) == '+repr(expected)+'\n')
    subprocess.run(['git','init'],cwd=root,check=True,capture_output=True)
    argv=candidate_checks.command(root,{'target_paths':['scripts/ci_suite.py']},sys.executable,['tests/test_pair.py'])
    result=subprocess.run(argv,cwd=root,capture_output=True,text=True)
    assert result.returncode==0,result.stdout+result.stderr
    assert '1 passed' in result.stdout and '"host_prepared_staged_paths": []' in result.stdout
    assert 'paired_runtime_paths' in result.stdout


@pytest.mark.parametrize('authored_change',[False,True])
def test_private_baseline_refresh_preserves_authored_fields_and_retained_code(tmp_path,authored_change):
    root=owner(tmp_path);(root/'relay').mkdir()
    a=root/'relay/a.py';b=root/'relay/b.py';a.write_bytes(b'a = 1\n');b.write_bytes(b'b = 1\n')
    manifest=root/candidate_checks.MANIFEST;manifest.parent.mkdir(parents=True)
    def row(name):
        raw=(root/'relay'/name).read_bytes()
        return {'name':name,'path':'tare.tools.os/relay/'+name,'rationale':'owned',
                'size_bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    original={'modules':[row('a.py'),row('b.py')],'total_modules':2}
    manifest.write_text(json.dumps(original,indent=2)+'\n',encoding='utf8',newline='\n')
    def git(*args):return subprocess.run(['git',*args],cwd=root,check=True,capture_output=True).stdout
    def commit():
        git('add','.');git('-c','user.name=Fixture','-c','user.email=fixture@localhost',
            '-c','commit.gpgsign=false','commit','-m','Fixture baseline')
    git('init');commit()
    a.write_bytes(b'a = 2\n');proposed={'modules':[row('a.py'),row('b.py')],'total_modules':2}
    if authored_change:proposed['modules'][0]['rationale']='candidate-authored change'
    manifest.write_text(json.dumps(proposed,indent=2)+'\n',encoding='utf8',newline='\n')
    patch=tmp_path.parent/(tmp_path.name+'-retained.diff');patch.write_bytes(git('diff','--binary','HEAD'))
    a.write_bytes(b'a = 1\n');manifest.write_text(json.dumps(original,indent=2)+'\n',encoding='utf8',newline='\n')
    b.write_bytes(b'b = 3\n');commit()  # Existing host sources moved, while their metadata stayed stale.
    base=git('rev-parse','HEAD').decode().strip()
    unit={'target_paths':['relay/a.py',candidate_checks.MANIFEST]}
    prepared,excluded,witness=candidate_checks.prepare_baseline(root,unit,base,retained_patch=patch)
    assert prepared!=base and excluded==([] if authored_change else [candidate_checks.MANIFEST]) and witness['modules']==['b.py']
    assert git('diff','--name-only',base,prepared).decode().strip()==candidate_checks.MANIFEST
    git('apply','--binary',*('--exclude='+p for p in excluded),str(patch))
    assert a.read_bytes().replace(b'\r\n',b'\n')==b'a = 2\n' and b.read_bytes()==b'b = 3\n'
    data=json.loads(manifest.read_text())
    assert data['modules'][0]['rationale']==('candidate-authored change' if authored_change else 'owned')
    assert data['modules'][1]['sha256']==hashlib.sha256(b.read_bytes()).hexdigest()
    assert witness['source_sha256']!=candidate_checks.source_digest(root)  # Concurrent/source edits cannot be applied unnoticed.
    data['modules'][0].update(size_bytes=len(a.read_bytes().replace(b'\r\n',b'\n')),
        sha256=hashlib.sha256(a.read_bytes().replace(b'\r\n',b'\n')).hexdigest())
    manifest.write_text(json.dumps(data,indent=2)+'\n',encoding='utf8',newline='\n')
    accepted=tmp_path.parent/(tmp_path.name+'-accepted.diff');accepted.write_bytes(git('diff','--binary',prepared))
    a.write_bytes(b'a = 1\n');manifest.write_text(json.dumps(original,indent=2)+'\n',encoding='utf8',newline='\n')
    commit()
    second_base=git('rev-parse','HEAD').decode().strip()
    _,second_excluded,_=candidate_checks.prepare_baseline(root,unit,second_base,retained_patch=accepted)
    assert second_excluded==([] if authored_change else [candidate_checks.MANIFEST])


@pytest.mark.parametrize('concurrent',[False,True])
def test_generated_baseline_lands_against_original_bytes_and_holds_real_concurrency(tmp_path,monkeypatch,concurrent):
    from relay import work_units,native_agent
    from relay.work_forms import record_plan
    from test_native_agent import deployment
    root=tmp_path/'project';root.mkdir();owner(root)
    source=Path(__file__).resolve().parents[1]
    (root/'relay').mkdir();a=root/'relay/a.py';b=root/'relay/b.py'
    a.write_bytes(b'a = 1\n');b.write_bytes(b'b = 1\n')
    manifest=root/candidate_checks.MANIFEST;manifest.parent.mkdir(parents=True)
    rows=[{'name':p.name,'path':'tare.tools.os/relay/'+p.name,'size_bytes':len(p.read_bytes()),
        'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in (a,b)]
    manifest.write_text(json.dumps({'modules':rows,'total_modules':2},indent=2)+'\n',encoding='utf8',newline='\n')
    (root/'scripts/sync_disposition_manifest.py').write_bytes((source/'scripts/sync_disposition_manifest.py').read_bytes())
    (root/'scripts/ops').mkdir()
    for name in ('run_candidate_checks.py','run_focused_tests.py'):
        (root/'scripts/ops'/name).write_bytes((source/'scripts/ops'/name).read_bytes())
    (root/'tests').mkdir()
    (root/'tests/test_value.py').write_text("from pathlib import Path\nimport json,hashlib\n"
        "def test_value():\n assert Path('relay/a.py').read_text()=='a = 2\\n'\n"
        " assert json.loads(Path("+repr(candidate_checks.MANIFEST)+").read_text())['modules'][0]['sha256']==hashlib.sha256(b'a = 2\\n').hexdigest()\n")
    b.write_bytes(b'b = 3\n')  # Legitimate host changes whose fingerprint was not refreshed.
    custody=tmp_path/'session';custody.mkdir()
    unit={'key':'change','title':'Set a to two','target_paths':['relay/a.py',candidate_checks.MANIFEST],
        'test_paths':['tests/test_value.py'],'criteria':['a equals two'],'verifiers':[]}
    config={'repositories':{'repo':str(root)},'writable':{'repo':unit['target_paths']},
        'conversation_flow':True,'verifiers':{}}
    record_plan(custody,config,{'units':[unit],'criteria':unit['criteria']})
    state={'config':config,'conversation_flow':True,'deployment':deployment(),
        'limits':{'max_model_calls':40},'deadline_utc':None}
    monkeypatch.setattr(work_units,'routes',lambda *a,**kw:iter([('fixture',deployment())]))
    def worker(config,*args,**kwargs):
        (Path(config['repositories']['repo'])/'relay/a.py').write_bytes(b'a = 2\n')
        return {'status':'PROPOSED','model_calls':1}
    monkeypatch.setattr(native_agent,'run_session',worker)
    def review(tree,*args):
        assert a.read_bytes()==b'a = 1\n' and json.loads(manifest.read_text())['modules'][1]['sha256']==rows[1]['sha256']
        if concurrent:b.write_bytes(b'b = 4\n')
        return {'verdict':'ACCEPT'}
    result=work_units.run_unit(custody,state,{'unit':'change'},lambda:False,review=review)
    assert bool(result.get('applied')) is (not concurrent),result
    assert a.read_bytes().replace(b'\r\n',b'\n')==(b'a = 1\n' if concurrent else b'a = 2\n')
    if concurrent:assert b.read_bytes()==b'b = 4\n'
    else:assert json.loads(manifest.read_text())['modules'][1]['sha256']==hashlib.sha256(b.read_bytes()).hexdigest()


@pytest.mark.parametrize('fault',[None,'authored','source','anchor','patch'])
def test_retained_candidate_requalifies_only_anchored_generated_drift_without_new_worker(tmp_path,monkeypatch,fault):
    from copy import deepcopy
    from relay import work_units,native_agent
    from relay.work_forms import record_plan
    from test_native_agent import deployment
    root=tmp_path/'project';root.mkdir();owner(root)
    (root/'relay').mkdir();a=root/'relay/a.py';b=root/'relay/b.py'
    a.write_bytes(b'a = 1\n');b.write_bytes(b'b = 1\n')
    manifest=root/candidate_checks.MANIFEST;manifest.parent.mkdir(parents=True)
    def refresh(tree):
        rows=[]
        for name in ('a.py','b.py'):
            raw=(tree/'relay'/name).read_bytes().replace(b'\r\n',b'\n')
            rows.append({'name':name,'path':'tare.tools.os/relay/'+name,'rationale':'owned',
                         'size_bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
        (tree/candidate_checks.MANIFEST).write_text(json.dumps({'modules':rows},indent=2)+'\n',encoding='utf8',newline='\n')
    refresh(root)
    def git(*args):return subprocess.check_output(['git',*args],cwd=root)
    git('init');git('add','.')
    git('-c','user.name=Fixture','-c','user.email=fixture@localhost','-c','commit.gpgsign=false','commit','-m','Base')
    custody=tmp_path/'session';custody.mkdir()
    unit={'key':'change','repository':'repo','title':'Set a to two',
          'target_paths':['relay/a.py',candidate_checks.MANIFEST],'test_paths':[],
          'criteria':['a equals two'],'verifiers':['check']}
    config={'repositories':{'repo':str(root)},'writable':{'repo':unit['target_paths']},
            'conversation_flow':True,'verifiers':{'check':{'repository':'repo','argv':['fixture']}}}
    record_plan(custody,config,{'units':[unit],'criteria':unit['criteria']})
    state={'config':config,'conversation_flow':True,'deployment':deployment(),
           'limits':{'max_model_calls':40},'deadline_utc':None}
    calls=[];reviews=[];rechecking=False
    monkeypatch.setattr(work_units,'routes',lambda *a,**kw:iter([('fixture',deployment())]))
    def worker(child,*args,**kwargs):
        assert not rechecking,'A saved candidate must not start another implementer'
        tree=Path(child['repositories']['repo']);calls.append('worker')
        (tree/'relay/a.py').write_bytes(b'a = 2\n');refresh(tree)
        return {'status':'PROPOSED','model_calls':1}
    monkeypatch.setattr(native_agent,'run_session',worker)
    def check(tree,commands):
        if not rechecking and (tree/'relay/a.py').read_bytes()==b'a = 2\n':
            return 'inconclusive','Host check environment stopped'
        return 'green','Fixture checks passed'
    monkeypatch.setattr(work_units,'run_checks',check)
    def review(tree,*args):
        reviews.append('review')
        assert (tree/'relay/a.py').read_bytes().replace(b'\r\n',b'\n')==b'a = 2\n'
        assert (tree/'relay/b.py').read_bytes().replace(b'\r\n',b'\n')==b'b = 3\n'
        return {'verdict':'ACCEPT'}
    first=work_units.run_unit(custody,state,{'unit':'change'},lambda:False,review=review)
    assert first['verdict']=='INCONCLUSIVE' and not first.get('applied')
    candidate={**first['candidate'],'review_only':True}
    patch=Path(candidate['patch']);patch_bytes=patch.read_bytes()
    b.write_bytes(b'b = 3\n');refresh(root)
    if fault=='authored':
        value=json.loads(manifest.read_bytes());value['modules'][0]['rationale']='changed contract'
        manifest.write_text(json.dumps(value,indent=2)+'\n',encoding='utf8',newline='\n')
    elif fault=='source':a.write_bytes(b'user edit\n')
    elif fault=='anchor':candidate['basis']['repo:'+candidate_checks.MANIFEST]='0'*64
    elif fault=='patch':patch.write_bytes(patch_bytes+b'\nchanged')
    original=deepcopy(candidate)
    rechecking=True
    result=work_units.run_unit(custody,state,{'unit':'change'},lambda:False,review=review,candidate=candidate)
    assert calls==['worker'] and candidate==original
    assert bool(result.get('applied')) is (fault is None),result
    assert reviews==(['review'] if fault is None else [])
    if fault is None:
        assert patch.read_bytes()==patch_bytes
        receipt=json.loads(Path(result['receipt']).read_bytes())
        witness=receipt['rungs'][0]['host_baseline_metadata']['retained_baseline_requalification']
        assert witness['authored_fields_preserved'] and witness['before_sha256']!=witness['after_sha256']
        assert receipt['rungs'][0]['model_calls']==0
    else:
        assert not result.get('applied')
        assert 'retained candidate' in result['reason']
        if fault!='patch':assert 'contracts/v1/harness_disposition_manifest.json' in result['reason']


def test_a_candidate_that_never_edits_the_manifest_requalifies_when_only_the_project_copy_moved(tmp_path,monkeypatch):
    from relay import work_units,native_agent
    from relay.work_forms import record_plan
    from test_native_agent import deployment
    root=tmp_path/'project';root.mkdir();owner(root)
    (root/'relay').mkdir();(root/'relay/b.py').write_bytes(b'b = 1\n');(root/'notes.txt').write_bytes(b'one\n')
    manifest=root/candidate_checks.MANIFEST;manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({'modules':[{'name':'b.py','sha256':'1'*64}]})+'\n',encoding='utf8',newline='\n')
    def git(*args):return subprocess.check_output(['git',*args],cwd=root)
    git('init');git('add','.')
    git('-c','user.name=Fixture','-c','user.email=fixture@localhost','-c','commit.gpgsign=false','commit','-m','Base')
    custody=tmp_path/'session';custody.mkdir()
    unit={'key':'change','repository':'repo','title':'Set notes to two',
          'target_paths':['notes.txt',candidate_checks.MANIFEST],'test_paths':[],
          'criteria':['notes say two'],'verifiers':['check']}
    config={'repositories':{'repo':str(root)},'writable':{'repo':unit['target_paths']},
            'conversation_flow':True,'verifiers':{'check':{'repository':'repo','argv':['fixture']}}}
    record_plan(custody,config,{'units':[unit],'criteria':unit['criteria']})
    state={'config':config,'conversation_flow':True,'deployment':deployment(),
           'limits':{'max_model_calls':40},'deadline_utc':None}
    calls,rechecking=[],[]
    monkeypatch.setattr(work_units,'routes',lambda *a,**kw:iter([('fixture',deployment())]))
    def worker(child,*args,**kwargs):
        assert not rechecking,'A saved candidate must not start another implementer'
        calls.append('worker');(Path(child['repositories']['repo'])/'notes.txt').write_bytes(b'two\n')
        return {'status':'PROPOSED','model_calls':1}
    monkeypatch.setattr(native_agent,'run_session',worker)
    monkeypatch.setattr(work_units,'run_checks',lambda tree,commands:(
        ('green','Fixture checks passed') if rechecking else ('inconclusive','Host check environment stopped')))
    first=work_units.run_unit(custody,state,{'unit':'change'},lambda:False,review=lambda *a:{'verdict':'ACCEPT'})
    assert first['verdict']=='INCONCLUSIVE' and not first.get('applied')
    candidate={**first['candidate'],'review_only':True}
    manifest.write_text(json.dumps({'modules':[{'name':'b.py','sha256':'2'*64}]})+'\n',encoding='utf8',newline='\n')
    rechecking.append(True)  # only the project's manifest moved; the candidate never touched it
    result=work_units.run_unit(custody,state,{'unit':'change'},lambda:False,review=lambda *a:{'verdict':'ACCEPT'},
                               candidate=candidate)
    assert calls==['worker'] and result.get('applied'),result
    assert (root/'notes.txt').read_bytes().replace(b'\r\n',b'\n')==b'two\n'
    receipt=json.loads(Path(result['receipt']).read_bytes())
    witness=receipt['rungs'][0]['retained_baseline_requalification']
    assert witness['kind']=='REQUALIFIED_UNTOUCHED_GENERATED_FILE'
