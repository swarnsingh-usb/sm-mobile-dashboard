import json
import subprocess
from types import SimpleNamespace
from pathlib import Path

import yaml

from bento_ci import cli
from bento_ci.core import ROOT


def test_generated_pipeline_has_both_platforms_and_manual_protected_gate(tmp_path,monkeypatch):
    c=json.loads((ROOT/'config.example.json').read_text())
    monkeypatch.setattr(cli,'ROOT',tmp_path)
    cli.generate(c)
    pipeline=yaml.safe_load((tmp_path/'generated/gitlab-ci.yml').read_text())
    gate=pipeline['workflow']['rules'][0]['if']
    assert '$CI_COMMIT_REF_PROTECTED == "true"' in gate
    assert '$CI_COMMIT_BRANCH == "dev"' in gate
    assert 'push' not in gate
    for name in ('android','ios'):
        job=pipeline[name+'-dev']
        assert 'both-dev' in job['rules'][0]['if']
        assert job['resource_group']=='bento-mac-pilot'
        assert job['tags']==['bento-mac-pilot']
    assert (tmp_path/'generated/instance-role-policy.json').exists()


def test_shell_syntax():
    for path in (ROOT/'setup.sh',ROOT/'bento'):
        subprocess.run(['/bin/bash','-n',str(path)],check=True)


def test_signing_script_syntax():
    subprocess.run(['ruby','-c',str(ROOT/'scripts/ios-signing.rb')],check=True,capture_output=True)


def test_registration_uses_documented_token_environment_and_preserves_existing(tmp_path,monkeypatch):
    c=json.loads((ROOT/'config.example.json').read_text())
    monkeypatch.setattr(cli,'ROOT',tmp_path)
    (tmp_path/'runtime').mkdir()
    class Store:
        def __init__(self,c):pass
        def get(self,*a,**k):return {'token':'glrt-test-token'}
    monkeypatch.setattr(cli,'Secrets',Store)
    calls=[]
    def run(args,**kwargs):
        calls.append(args)
        assert kwargs['env']['CI_SERVER_TOKEN']=='glrt-test-token'
        assert 'glrt-test-token' not in args
        path=Path(args[args.index('--config')+1])
        path.write_text('concurrent = 5\n[[runners]]\n  token = "glrt-test-token"\n')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(cli.subprocess,'run',run)
    cli.register(c)
    output=tmp_path/'runtime/runner.toml'
    assert 'concurrent = 1' in output.read_text()
    assert output.stat().st_mode & 0o777 == 0o600
    assert not (tmp_path/'runtime/runner.pending.toml').exists()
    cli.register(c)
    assert len(calls)==1
