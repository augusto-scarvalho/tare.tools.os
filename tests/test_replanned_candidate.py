"""A revised review must preserve completed work without inheriting its approval."""
from copy import deepcopy
import hashlib
import pytest
from relay import conversation_tasks as tasks


@pytest.fixture
def previous(tmp_path):
    (tmp_path/'write.py').write_text('original')
    (tmp_path/'dependency.py').write_text('before')
    config={'repositories':{'repo':str(tmp_path)}}
    targets={'repo':['write.py','dependency.py']}
    delivery={'applied':False,'verdict':'NOT_REVIEWED','audit':{'verdict':'INCONCLUSIVE'},
        'files':['write.py'],'candidate':{'patch':'unchanged-patch','sha256':'unchanged-hash',
            'targets':targets,'basis':tasks.source_state(config,targets)}}
    (tmp_path/'dependency.py').write_text('after')
    observed=hashlib.sha256((tmp_path/'dependency.py').read_bytes()).hexdigest()
    plan={'unit':{'repository':'repo','target_paths':['write.py']},
        'sources':[{'repository':'repo','path':'dependency.py','sha256':observed}]}
    return tmp_path,config,plan,delivery


def test_revised_plan_can_requalify_only_its_read_dependencies(previous):
    root,config,plan,delivery=previous; old=deepcopy(delivery)
    revised=tasks.replanned_delivery(config,plan,delivery)
    assert delivery==old
    assert revised['candidate']['basis']==tasks.source_state(config,delivery['candidate']['targets'])
    assert revised['candidate']['patch']==old['candidate']['patch']
    assert revised['candidate']['sha256']==old['candidate']['sha256']
    assert revised['candidate']['replanned_dependencies']['repo:dependency.py']=={
        'before':old['candidate']['basis']['repo:dependency.py'],
        'after':revised['candidate']['basis']['repo:dependency.py']}
    assert revised['applied'] is False and revised['audit']['verdict']=='INCONCLUSIVE'


@pytest.mark.parametrize('fault',['write_changed','missing_proof','wrong_proof','new_write_scope','removed_dependency'])
def test_changed_outputs_or_unreconciled_dependencies_cannot_be_rebased(previous,fault):
    root,config,plan,delivery=previous
    if fault=='write_changed':(root/'write.py').write_text('user edit')
    elif fault=='missing_proof':plan['sources']=[]
    elif fault=='wrong_proof':plan['sources'][0]['sha256']='0'*64
    elif fault=='new_write_scope':plan['unit']['target_paths'].append('dependency.py')
    elif fault=='removed_dependency':
        (root/'dependency.py').unlink(); plan['sources']=[]
    with pytest.raises(ValueError,match='retained candidate'):
        tasks.replanned_delivery(config,plan,delivery)


from test_conversation_tasks import host


def test_revised_goal_reuses_prior_candidate_for_review_without_an_implementer(host,monkeypatch):
    from test_conversation_tasks import intake,planned,effect
    from relay import work_units
    tools,control,*_=host
    intake(host); planned(host)
    value=tasks.load(tools.root); task=tasks.current(value)
    targets={task['plan']['unit']['repository']:['a.py']}
    task['prior_delivery']={'verdict':'NOT_REVIEWED','applied':False,'audit':{'verdict':'INCONCLUSIVE'},
        'files':['a.py'],'candidate':{'patch':'retained','sha256':'a'*64,'targets':targets,
            'basis':tasks.source_state(tools.state['config'],targets)}}
    task['delivery']={'verdict':'INCONCLUSIVE','reason':'preparation stopped'}
    tasks.save(tools.root,value)
    calls=[]
    def reuse(*args,**kwargs):
        calls.append(kwargs['candidate'])
        return {'verdict':'NOT_REVIEWED','candidate':kwargs['candidate'],'files':['a.py']}
    monkeypatch.setattr(work_units,'run_unit',reuse)
    result=tools.execute(effect('implement',{}),control)
    assert result.status=='succeeded'
    assert len(calls)==1 and calls[0]['review_only'] is True
    saved=tasks.current(tasks.load(tools.root))['delivery']
    assert saved['candidate']['patch']=='retained'
