import json
import os
import signal
import subprocess
import sys
import time

import pytest

from bento_ci.build import Commands, publish, tool_env
from bento_ci.core import ROOT, SafeError, atomic_json, digest, load_config
from bento_ci.locking import check_window, host_lock, open_window


@pytest.fixture
def config():
    return json.loads((ROOT/'config.example.json').read_text())


def test_no_bitrise_or_tls_bypass_in_child_environment(config,monkeypatch):
    monkeypatch.setenv('BITRISEIO_GIT_BRANCH_DEST','production')
    monkeypatch.setenv('NODE_TLS_REJECT_UNAUTHORIZED','0')
    monkeypatch.setenv('GIT_SSL_NO_VERIFY','1')
    monkeypatch.setenv('AWS_ACCESS_KEY_ID','should-not-propagate')
    e=tool_env(config)
    assert 'BITRISEIO_GIT_BRANCH_DEST' not in e
    assert 'NODE_TLS_REJECT_UNAUTHORIZED' not in e
    assert 'GIT_SSL_NO_VERIFY' not in e
    assert 'AWS_ACCESS_KEY_ID' not in e
    assert e['npm_config_strict_ssl']=='true'


def test_log_redaction_and_subprocess_failure(monkeypatch,capsys):
    monkeypatch.setattr('bento_ci.build.bitrise_processes',lambda:[])
    cmd=Commands(os.environ.copy(),['top-secret-value'])
    with pytest.raises(SafeError):
        cmd.run([sys.executable,'-c','print("top-secret-value");raise SystemExit(7)'])
    output=capsys.readouterr().out
    assert 'top-secret-value' not in output
    assert '[REDACTED]' in output


def test_bitrise_restart_aborts_child(monkeypatch):
    monkeypatch.setattr('bento_ci.build.bitrise_processes',lambda:['bitrise-den-agent'])
    cmd=Commands(os.environ.copy())
    start=time.monotonic()
    with pytest.raises(SafeError,match='Bitrise restarted'):
        cmd.run([sys.executable,'-c','import time;time.sleep(45)'])
    assert time.monotonic()-start<15


def test_window_expiry_and_agent_detection(tmp_path,monkeypatch):
    monkeypatch.setattr('bento_ci.locking.bitrise_processes',lambda:[])
    with pytest.raises(SafeError):check_window(tmp_path)
    open_window(tmp_path,1);check_window(tmp_path)
    monkeypatch.setattr('bento_ci.locking.bitrise_processes',lambda:['agent'])
    with pytest.raises(SafeError):check_window(tmp_path)
    with pytest.raises(SafeError):open_window(tmp_path,1)
    monkeypatch.setattr('bento_ci.locking.bitrise_processes',lambda:[])
    atomic_json(tmp_path/'data/pilot-window.json',{'expires_at':time.time()-1})
    with pytest.raises(SafeError):check_window(tmp_path)


def test_host_lock_is_exclusive_and_released(tmp_path,monkeypatch):
    monkeypatch.setattr('bento_ci.locking.bitrise_processes',lambda:[])
    open_window(tmp_path,1)
    script='import fcntl,sys;f=open(sys.argv[1],"a");fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)'
    with host_lock(tmp_path):
        r=subprocess.run([sys.executable,'-c',script,str(tmp_path/'data/host.lock')],capture_output=True)
        assert r.returncode!=0
    r=subprocess.run([sys.executable,'-c',script,str(tmp_path/'data/host.lock')],capture_output=True)
    assert r.returncode==0


class Store:
    def get(self,*args,**kwargs):return {'token':'private-upload-token'}


def test_local_only_and_artifactory_failure_preserves_files(config,tmp_path,monkeypatch):
    (tmp_path/'app.apk').write_bytes(b'app')
    m={'sha':'a'*40,'pipeline_id':'1','job_id':'2','platform':'android'}
    publish(config,Store(),tmp_path,m)
    assert json.loads((tmp_path/'manifest.json').read_text())['storage']=='local'
    assert m['files'][0]['sha256']==digest(tmp_path/'app.apk')
    config['artifactory'].update(url='https://artifacts.example/artifactory',repository='mobile')
    class Response: status_code=403
    monkeypatch.setattr('bento_ci.build.requests.put',lambda *a,**k:Response())
    with pytest.raises(SafeError,match='403'):publish(config,Store(),tmp_path,m)
    assert (tmp_path/'app.apk').read_bytes()==b'app'
    assert json.loads((tmp_path/'manifest.json').read_text())['upload_status']=='failed'


def test_artifactory_checksum_mismatch_fails(config,tmp_path,monkeypatch):
    (tmp_path/'app.ipa').write_bytes(b'ipa')
    config['artifactory'].update(url='https://artifacts.example/artifactory',repository='mobile')
    class Response:
        status_code=201
    class Metadata:
        status_code=200
        def json(self):return {'checksums':{'sha256':'wrong'}}
    monkeypatch.setattr('bento_ci.build.requests.put',lambda *a,**k:Response())
    monkeypatch.setattr('bento_ci.build.requests.get',lambda *a,**k:Metadata())
    with pytest.raises(SafeError,match='checksum'):publish(config,Store(),tmp_path,{'sha':'a'*40,'pipeline_id':'1','job_id':'2','platform':'ios'})


def test_artifactory_success_paths_and_manifest(config,tmp_path,monkeypatch):
    (tmp_path/'app.apk').write_bytes(b'apk')
    config['artifactory'].update(url='https://artifacts.example/artifactory',repository='mobile')
    seen=[]
    class Response: status_code=201
    class Metadata:
        status_code=200
        def json(self):return {'checksums':{'sha256':digest(tmp_path/'app.apk')}}
    def put(url,**kwargs):
        seen.append(url);assert kwargs['allow_redirects'] is False;return Response()
    monkeypatch.setattr('bento_ci.build.requests.put',put)
    monkeypatch.setattr('bento_ci.build.requests.get',lambda *a,**k:Metadata())
    m={'sha':'a'*40,'pipeline_id':'11','job_id':'22','platform':'android'}
    publish(config,Store(),tmp_path,m)
    assert m['upload_status']=='complete'
    assert '/11/22/android/app.apk' in seen[0]
    assert seen[1].endswith('/manifest.json')


def test_config_rejects_bad_origins_and_partial_storage(config,tmp_path):
    p=tmp_path/'config.json'
    config['public_url']='http://0.0.0.0:8765';atomic_json(p,config)
    with pytest.raises(SafeError):load_config(p)
    config['public_url']='http://localhost:8765'
    config['artifactory']['url']='https://artifacts.example/artifactory';atomic_json(p,config)
    with pytest.raises(SafeError):load_config(p)
