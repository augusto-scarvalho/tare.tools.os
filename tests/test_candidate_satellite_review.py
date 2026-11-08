"""Satellite sources reach the independent auditor through a real candidate snapshot.

These scenarios build real submodule repositories, so they live apart from the other review recovery tests:
each file has its own check time cap.
"""
from pathlib import Path

import pytest

from relay import conversation_review
from test_conversation_review_recovery import paired_packages, reviewed  # noqa: F401
from test_conversation_tasks import host  # noqa: F401


@pytest.fixture(scope='module')
def satellite_repositories(tmp_path_factory):
    """Build real submodule repositories once; each review gets an isolated copy."""
    from relay.challenger import git
    base=tmp_path_factory.mktemp('satellite-repositories')
    repositories={}
    satellites=['satellites/tare.tools.kernel','satellites/tare.tools.agent-runtime']
    for kind in ('regular','symlink'):
        dependency=base/(kind+'-dependency');dependency.mkdir()
        git(dependency,'init')
        (dependency/'source.py').write_bytes(b'pinned = True\n')
        if kind=='symlink':
            blob=git(dependency,'hash-object','-w','source.py').decode().strip()
            git(dependency,'update-index','--add','--cacheinfo','120000,'+blob+',link')
        else:
            git(dependency,'add','.')
        git(dependency,'-c','user.name=Fixture','-c','user.email=fixture@localhost',
            '-c','commit.gpgsign=false','commit','-m','Dependency')
        root=base/kind;root.mkdir()
        git(root,'init')
        (root/'a.py').write_bytes(b'value = 1\n')
        for path in satellites:
            git(root,'-c','protocol.file.allow=always','submodule','add',str(dependency),path)
        git(root,'add','.')
        git(root,'-c','user.name=Fixture','-c','user.email=fixture@localhost',
            '-c','commit.gpgsign=false','commit','-m','Pinned satellites')
        repositories[kind]=(root,git(dependency,'rev-parse','HEAD').decode().strip())
    return repositories


@pytest.mark.parametrize('scenario',['initialized','deinitialized','neither','one-package','missing-pin','inventory-error',
    'symlink','deadline','inventory-deadline','checkout-error'])
@pytest.mark.parametrize('package_present', [True, False])
def test_satellite_sources_through_review(reviewed,paired_packages,satellite_repositories,tmp_path,monkeypatch,scenario,package_present):
    """Real gitlinks select sources before dispatch; missing coverage is not a finding."""
    import shutil
    import subprocess
    from relay import challenger_workspace as workspace
    from relay.challenger import git
    host,run,_=reviewed
    tools,_,root,*_=host
    source,pin=satellite_repositories['symlink' if scenario=='symlink' else 'regular']
    # Relative submodule gitdirs survive copying; never mutate the shared source.
    (root/'.git').rename(tmp_path/'original-review-git')
    shutil.copytree(source,root,dirs_exist_ok=True)
    (root/'a.py').write_bytes(b'value = 2\n')
    satellites=['satellites/tare.tools.kernel','satellites/tare.tools.agent-runtime']
    if scenario in {'deinitialized','neither','one-package'}:
        git(root,'submodule','deinit','-f','--',*satellites)
    tree=tmp_path/'candidate-satellites'
    original_offline=workspace._offline_git
    population_failure=scenario in {'deadline','inventory-deadline','checkout-error'}
    if population_failure:
        def interrupted(path,*args,**kwargs):
            target=('checkout-index' if scenario in {'deadline','checkout-error'} else 'cat-file')
            if target in args:
                if scenario=='checkout-error': raise ValueError('injected checkout failure')
                raise subprocess.TimeoutExpired(target,300)
            return original_offline(path,*args,**kwargs)
        monkeypatch.setattr(workspace,'_offline_git',interrupted)
    workspace.snapshot(root,tree)
    monkeypatch.setattr(workspace,'_offline_git',original_offline)
    if not package_present:
        shutil.rmtree(paired_packages/'compute_plane-1.0.dist-info')
    if scenario=='missing-pin':
        shutil.rmtree(tree/satellites[0]);(tree/satellites[0]).mkdir()
    if scenario in {'neither','one-package'}:
        for module in (('compute_plane','tare_tools_agent_runtime') if scenario=='neither' else ('compute_plane',)):
            shutil.rmtree(paired_packages/(module+'-1.0.dist-info'),ignore_errors=True)
    if scenario=='inventory-error':
        def unavailable(*a,**kw):raise ValueError('local inventory unreadable')
        monkeypatch.setattr(workspace,'submodule_inventory',unavailable)
    fallback=scenario in {'missing-pin','symlink','deadline','inventory-deadline','checkout-error'}
    # A missing first pin with no matching package retains the valid sibling pin;
    # an unrelated runtime root must not switch this context to runtime fallback.
    expected_branch=('unavailable-pins' if scenario=='inventory-error' else
        'candidate-pins' if scenario=='missing-pin' and not package_present else
        'paired-runtime' if scenario in {'deinitialized','neither','one-package'} or fallback else 'candidate-pins')
    context=conversation_review.satellite_context(tools.state['config'],run.task['plan']['unit'],tree)
    assert context['branch']==expected_branch
    missing=[module+': matching installation metadata missing or ambiguous'
        for module in (('compute_plane','tare_tools_agent_runtime') if scenario=='neither' else ('compute_plane',))]
    named_gap='Missing local pinned source '+satellites[0]+' at '+pin
    expected_remaining=(missing if scenario in {'neither','one-package'} else
        [missing[0]] if scenario=='deinitialized' and not package_present else
        [named_gap] if fallback and not package_present else
        ['Local submodule inventory unavailable: local inventory unreadable'] if scenario=='inventory-error' else [])
    assert context['remaining']==expected_remaining
    assert conversation_review.apply_satellite_obligations(
        {'verdict':'ACCEPT','findings':[],'remaining':[]},context)=={
            'verdict':'INCONCLUSIVE' if expected_remaining else 'ACCEPT',
            'findings':[],'remaining':expected_remaining}
    def inspect(config,packet,instruction):
        assert packet['satellite_sources']==context
        assert config['writable']=={} and set(config['capabilities'])=={'read','list','search'}
        assert config['repositories']['reference-1']
        aliases=[]
        for row in context['sources']:
            if 'module' in row:
                assert row['satellite'] in satellites
                assert row['module'] in {'compute_plane','tare_tools_agent_runtime'}
                if 'root' in row:
                    aliases.append(row['alias'])
                    assert row['version']=='1.0' and row['distribution']==row['module']
                    assert Path(row['root']).is_absolute() and Path(row['root']).parent==paired_packages
                    assert config['repositories'][row['alias']]==row['root']
                    assert row['alias'] in instruction
                else:assert row['missing'] in expected_remaining
            else:
                assert row['commit']==pin
        assert len(aliases)==len(set(aliases)) and not set(aliases)&{'reference-1','candidate','review'}
    result=run('ACCEPT',with_findings=False,inspect=inspect,
        runtime_citations=True,candidate_tree=tree)
    assert result['verdict']==('INCONCLUSIVE' if expected_remaining else 'ACCEPT')
    assert result['findings']==[] and result['remaining']==expected_remaining
    runtime=[s for s in result['reference_sources'] if 'runtime' in s]
    expected_runtime=sum('root' in row for row in context['sources'])
    assert len(runtime)==expected_runtime
    if fallback and package_present:
        assert context['branch']=='paired-runtime'
        assert any(row.get('alias','').startswith('runtime-') for row in context['sources'])
        assert runtime and named_gap not in result['remaining']
    if fallback and not package_present:
        assert named_gap in result['remaining']
    assert conversation_review.reference_sources_current(tools.state['config'],result['reference_sources'])
    if runtime:
        citation=runtime[0]
        (Path(citation['folder'])/citation['path']).write_bytes(b'# stale after review\n')
        assert not conversation_review.reference_sources_current(tools.state['config'],runtime)
