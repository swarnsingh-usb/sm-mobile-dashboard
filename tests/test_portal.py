import copy
import json
import time
from pathlib import Path

import pytest

from bento_ci.core import ROOT, SafeError, artifact_dir, atomic_json
from bento_ci.server import create_app


class Store:
    def get(self, name, fresh=False):
        return {'username':'engineer', 'password':'test-password-with-at-least-24-characters', 'session_key':'k'*40}


class GitLab:
    def __init__(self): self.launches=0; self.fail=False
    def launch(self, workflow, key):
        self.launches += 1
        if self.fail: raise SafeError('Upstream result unknown')
        return {'id':101,'status':'pending','ref':'dev'}
    def pipeline(self, ident):
        if ident != 101: raise SafeError('Outside pilot')
        return {'id':101,'ref':'dev'}
    def project_call(self, method, path, **kwargs):
        if path == '/pipelines':
            return [{'id':101,'name':'Bento Mac / both-dev'}, {'id':102,'name':'Other pipeline'}]
        if path == '/pipelines/101/jobs': return []
        if path == '/jobs/7': return {'id':7,'pipeline':{'id':102}}
        return {'ok':True}


@pytest.fixture
def portal(tmp_path, monkeypatch):
    monkeypatch.setattr('bento_ci.locking.bitrise_processes', lambda: [])
    config=json.loads((ROOT/'config.example.json').read_text())
    config['public_url']='http://localhost'
    gl=GitLab()
    app=create_app(config, Store(), gl, root=tmp_path)
    app.testing=True
    client=app.test_client()
    return client,gl,tmp_path


def login(client):
    r=client.post('/api/login',json={'username':'engineer','password':'test-password-with-at-least-24-characters'},headers={'Origin':'http://localhost'})
    assert r.status_code==200
    return {'Origin':'http://localhost','X-CSRF-Token':r.json['csrf']}


def test_auth_origin_and_csrf(portal):
    c,_,_=portal
    assert c.get('/api/builds').status_code==401
    assert c.get('/',headers={'Host':'attacker.example'}).status_code==400
    headers=login(c)
    assert c.post('/api/logout',headers={'Origin':'https://attacker.example'}).status_code==403
    assert c.post('/api/logout',headers={'Origin':'http://localhost'}).status_code==403
    assert c.get('/api/builds').json==[{'id':101,'name':'Bento Mac / both-dev'}]
    assert c.post('/api/logout',headers=headers).status_code==200
    assert c.get('/api/builds').status_code==401


def test_window_and_idempotent_launch(portal):
    c,gl,root=portal; headers=login(c)
    payload={'workflow':'both-dev','request_id':'a'*36}
    assert c.post('/api/builds',json=payload,headers=headers).status_code==400
    assert gl.launches==0
    atomic_json(root/'data/pilot-window.json',{'expires_at':time.time()+60})
    assert c.post('/api/builds',json=payload,headers=headers).status_code==201
    assert c.post('/api/builds',json=payload,headers=headers).status_code==200
    assert gl.launches==1
    payload['workflow']='ios-dev'
    assert c.post('/api/builds',json=payload,headers=headers).status_code==400
    assert gl.launches==1


def test_uncertain_launch_not_repeated(portal):
    c,gl,root=portal;headers=login(c);gl.fail=True
    atomic_json(root/'data/pilot-window.json',{'expires_at':time.time()+60})
    body={'workflow':'both-dev','request_id':'b'*36}
    assert c.post('/api/builds',json=body,headers=headers).status_code==400
    assert c.post('/api/builds',json=body,headers=headers).status_code==409
    assert gl.launches==1


def test_only_pilot_jobs_can_be_read(portal):
    c,_,_=portal;login(c)
    assert c.get('/api/jobs/7/log').status_code==400
    assert c.post('/api/builds/102/cancel',json={},headers=login(c)).status_code==400


def test_local_download_survives_upstream_outage(portal):
    c,gl,root=portal;login(c)
    folder=artifact_dir(root,101,6,'android');folder.mkdir(parents=True)
    (folder/'app.apk').write_bytes(b'APK-test')
    atomic_json(folder/'manifest.json', {'project':'BENTO/bento.mobileapp','pipeline_id':'101','job_id':'6','platform':'android','files':[{'name':'app.apk'}]})
    gl.fail=True
    assert c.get('/api/artifacts/101/6/android/app.apk').data==b'APK-test'
    assert c.get('/api/artifacts/101/6/android/manifest.json').status_code==400
    assert len(c.get('/api/local-artifacts').json)==1
    assert c.get('/api/artifacts/101/6/../../config.json').status_code!=200


def test_symlink_download_rejected(portal):
    c,_,root=portal;login(c)
    folder=artifact_dir(root,101,6,'ios');folder.mkdir(parents=True)
    outside=root/'sensitive';outside.write_text('secret')
    (folder/'app.ipa').symlink_to(outside)
    atomic_json(folder/'manifest.json',{'project':'BENTO/bento.mobileapp','files':[{'name':'app.ipa'}]})
    assert c.get('/api/artifacts/101/6/ios/app.ipa').status_code==400
