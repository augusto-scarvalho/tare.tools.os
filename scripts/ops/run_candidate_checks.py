"""Prepare only the admitted OS candidate files, then use the existing focused runner."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--targets-json',required=True)
    parser.add_argument('--runtime-paths-json',default='[]')
    parser.add_argument('tests',nargs='+')
    args=parser.parse_args()
    root=Path.cwd().resolve();targets=json.loads(args.targets_json)
    manifest='contracts/v1/harness_disposition_manifest.json'
    if (not isinstance(targets,list) or not targets or len(targets)>256
            or any(not isinstance(p,str) or not p or Path(p).is_absolute()
                or not (root/p).resolve().is_relative_to(root) or (root/p).is_dir() for p in targets)):
        raise ValueError('Choose literal admitted candidate files.')
    generated=any(p.startswith('relay/') and p.endswith('.py') for p in targets)
    if generated and manifest not in targets:
        raise ValueError('Relay edits require the disposition manifest in the admitted paths.')
    roots=json.loads(args.runtime_paths_json)
    if (not isinstance(roots,list) or len(roots)>8
            or any(not isinstance(p,str) or not Path(p).is_absolute() or not Path(p).is_dir() for p in roots)):
        raise ValueError('Choose existing absolute paired runtime paths.')
    if roots:
        os.environ['PYTHONPATH']=os.pathsep.join([str(root),*roots])
    print(json.dumps({'candidate_test_root':str(root),'paired_runtime_paths':roots}))
    if generated:subprocess.run([sys.executable,'-B','scripts/sync_disposition_manifest.py','.'],check=True)
    prepared=[p for p in targets if generated and (p==manifest or p.startswith('relay/'))]
    staged=[p for p in prepared if (root/p).exists() or subprocess.run(
        ['git','--literal-pathspecs','ls-files','--error-unmatch','--',p],stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL).returncode==0]
    if staged:subprocess.run(['git','--literal-pathspecs','add','-A','--',*staged],check=True)
    observed=subprocess.check_output(['git','diff','--cached','--name-only','-z']).decode('utf8').split('\0')
    print(json.dumps({'host_prepared_staged_paths':[p for p in observed if p]}))
    spec=importlib.util.spec_from_file_location('focused',root/'scripts/ops/run_focused_tests.py')
    focused=importlib.util.module_from_spec(spec);spec.loader.exec_module(focused)
    with tempfile.TemporaryDirectory(prefix='tare-candidate-check-') as folder:
        output=Path(folder)/'result'
        result=focused.run(args.tests,output)
        print((output/'pytest.log').read_text(encoding='utf8',errors='replace')[-12000:])
        print(json.dumps(result))
        return result['exit_code']


if __name__=='__main__':raise SystemExit(main())
