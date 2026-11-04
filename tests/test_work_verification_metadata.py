"""Verifier-generated UID sidecars stay in the private candidate, never the project patch."""
import json
from pathlib import Path
import subprocess
import sys

from relay import native_agent, work_units
from relay.challenger_workspace import changes
from relay.work_forms import record_plan
from test_native_agent import deployment


def repository(root):
    root.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True).stdout
    git('init'); git('config', 'user.name', 'Fixture'); git('config', 'user.email', 'fixture@example.invalid')
    (root/'main.gd').write_text('old\n')
    (root/'kept.gd').write_text('existing\n')
    (root/'kept.gd.uid').write_text('uid://old\n')
    git('add', '--all'); git('commit', '-m', 'Fixture')
    return git('rev-parse', 'HEAD').decode().strip()


def test_metadata_baseline_keeps_source_and_existing_uid_changes_visible(tmp_path):
    tree=tmp_path/'private';base=repository(tree)
    (tree/'main.gd.uid').write_text('uid://new\n')
    (tree/'main.gd').write_text('changed source\n')
    (tree/'kept.gd.uid').write_text('uid://changed\n')
    (tree/'other.bin').write_bytes(b'unplanned')
    newer,generated=work_units.imported_uid_baseline(tree,base,['main.gd.uid'])
    assert newer!=base and list(generated)==['main.gd.uid']
    assert set(changes(tree,newer))=={'main.gd','kept.gd.uid','other.bin'}
    (tree/'invalid.gd.uid').write_text('not a UID\n')
    same,generated=work_units.imported_uid_baseline(tree,newer,['invalid.gd.uid'])
    assert same==newer and not generated


def test_host_import_metadata_does_not_break_or_expand_reviewed_application(tmp_path,monkeypatch):
    project=tmp_path/'project';repository(project)
    custody=tmp_path/'session';custody.mkdir()
    checks={
        'godot-import':{'repository':'repo','argv':[sys.executable,'-I','-c',
            "from pathlib import Path; p=Path('main.gd.uid'); p.write_text('uid://generated\\n') if not p.exists() else None"]},
        'tests':{'repository':'repo','argv':[sys.executable,'-I','-c',
            "from pathlib import Path; assert Path('main.gd').read_text()=='fixed\\n'"]}}
    config={'repositories':{'repo':str(project)},'writable':{'repo':['main.gd']},
        'verifiers':checks,'conversation_flow':True}
    unit={'key':'repair','title':'Fix script','target_paths':['main.gd'],'criteria':['Script passes'],
        'verifiers':list(checks)}
    record_plan(custody,config,{'units':[unit],'criteria':unit['criteria']})
    state={'config':config,'conversation_flow':True,'deployment':deployment(),
        'limits':{'max_model_calls':30},'deadline_utc':None}
    monkeypatch.setattr(work_units,'routes',lambda *a,**kw:iter([('fixture',deployment())]))
    def implement(config,*args,**kwargs):
        tree=Path(config['repositories']['repo'])
        assert (tree/'main.gd.uid').read_text()=='uid://generated\n'
        (tree/'main.gd').write_text('fixed\n')
        return {'status':'PROPOSED','model_calls':1}
    monkeypatch.setattr(native_agent,'run_session',implement)
    reviewed=[]
    def audit(tree,dep,unit,changed,checks):
        reviewed.append(sorted(changed))
        assert '$ tests (exit 0)' in checks['after']
        return {'verdict':'ACCEPT'}
    result=work_units.run_unit(custody,state,{'unit':'repair'},lambda:False,review=audit)
    assert result.get('applied'),result
    assert reviewed==[['main.gd']] and (project/'main.gd').read_text()=='fixed\n'
    assert not (project/'main.gd.uid').exists()
    row=json.loads(Path(result['receipt']).read_text())['rungs'][0]
    assert list(row['verification_metadata'])==['main.gd.uid']
