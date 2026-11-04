"""Real native conversation transports scoped rules and enforces saved observations."""
import hashlib
import json
import pytest
from relay import native_agent
from relay.project_permissions import permissions_view
from test_native_agent import Client, run, setup


@pytest.mark.parametrize('change',['change','delete','appear'])
def test_model_receives_scoped_rules_before_edit_and_resume_checks_them(setup,change):
    config,custody,root=setup
    config['writable']={'repo':['src']}
    (root/'src').mkdir();(root/'src/value.txt').write_bytes(b'one')
    rules=root/'src/AGENTS.md'
    if change!='appear': rules.write_text('Scoped: keep strings lowercase.',encoding='utf-8')
    client=Client(steps=[('read',{'repository':'repo','path':'src/value.txt'}),
        ('edit',{'repository':'repo','path':'src/value.txt','sha256':hashlib.sha256(b'one').hexdigest(),
                 'search':'one','replace':'two'}),('verify',{'name':'unit'}),('tare_deliver',{'summary':'Edited under scoped guidance.'})])
    result=run(config,custody,client)
    assert result['status']=='PROPOSED' and (root/'src/value.txt').read_bytes()==b'two'
    requests=[body for path,body in client.calls if path.endswith('/completions')]
    scoped=[json.loads(m['content'])['scoped_project_instructions'] for m in requests[1]['messages']
            if m['role']=='tool' and 'scoped_project_instructions' in json.loads(m['content'])]
    assert scoped and scoped[0][0]['path']=='src/AGENTS.md'
    assert scoped[0][0]['text']==(None if change=='appear' else 'Scoped: keep strings lowercase.')
    view=permissions_view(root.parent/'preferences',root,root.parent/'control',session=custody)
    assert 'src/AGENTS.md' in view['instructions_text'] and 'Match the saved observations' in view['instructions_text']
    resumed=Client(steps=[('tare_deliver',{'summary':'Continued.'})])
    native_agent.run_session(None,None,custody,resume=True,client=resumed,local_counter=resumed)
    if change=='delete': rules.unlink()
    else: rules.write_text('Different rules',encoding='utf-8')
    view=permissions_view(root.parent/'preferences',root,root.parent/'control',session=custody)
    assert 'Changed or unavailable' in view['instructions_text']
    assert 'instruction_source_changed' not in view['instructions_text']
    assert 'start a new conversation' in view['instructions_text']
    rejected=Client()
    with pytest.raises(ValueError):
        native_agent.run_session(None,None,custody,resume=True,refresh=True,client=rejected)
    assert not rejected.calls
    assert (root/'src/value.txt').read_bytes()==b'two'
