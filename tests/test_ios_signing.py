import base64
import datetime
import json
import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from bento_ci import build
from bento_ci.core import ROOT, SafeError


@pytest.fixture
def setup(tmp_path,monkeypatch):
    config=json.loads((ROOT/'config.example.json').read_text())
    monkeypatch.setattr(build,'ROOT',tmp_path)
    fake_home=tmp_path/'home';fake_home.mkdir()
    monkeypatch.setattr(build.Path,'home',lambda:fake_home)
    secret={'p12_password':'private-p12-password','p12_base64':base64.b64encode(b'fake-p12').decode(),
        'team_id':'TESTTEAM','signing_identity':'Test identity',
        'profiles_base64':[base64.b64encode(b'fake-profile').decode()],
        'export_options':{'teamID':'TESTTEAM','method':'release-testing','provisioningProfiles':{'com.usbank.SpendManagement.staging':'Test Profile'}}}
    profile={'UUID':'12345678-1234-1234-1234-123456789012','Name':'Test Profile','TeamIdentifier':['TESTTEAM'],
        'ExpirationDate':datetime.datetime.now()+datetime.timedelta(days=7)}
    calls=[]
    class Store:
        def get(self,*a,**k):return secret
    class Commands:
        sensitive=set()
        def run(self,args,**kwargs):
            calls.append([str(a) for a in args])
            if args[1:3]==['list-keychains','-d'] and '-s' not in args:return '"/Users/test/Library/Keychains/login.keychain-db"\n'
            if args[1:3]==['cms','-D']:return plistlib.dumps(profile).decode()
            return ''
    cleaned=[]
    def cleanup(args,**kwargs):
        cleaned.append(args)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(build.subprocess,'run',cleanup)
    temp=tmp_path/'temp';temp.mkdir()
    return config,Store(),Commands(),temp,secret,profile,calls,cleaned,fake_home


def test_cleanup_after_archive_failure(setup):
    c,store,cmd,temp,secret,profile,calls,cleaned,home=setup
    with pytest.raises(RuntimeError):
        with build.ios_signing(c,store,cmd,temp) as (_,_,_,export):
            options=plistlib.loads(export.read_bytes())
            assert options['destination']=='export'
            assert options['manageAppVersionAndBuildNumber'] is False
            assert len(list(home.rglob('*.mobileprovision')))==2
            raise RuntimeError('archive failed')
    assert not list(home.rglob('*.mobileprovision'))
    assert any(x[1]=='delete-keychain' for x in cleaned)
    assert cleaned[0][-1]=='/Users/test/Library/Keychains/login.keychain-db'
    assert not (temp.parent/'data/signing-recovery.json').exists()


def test_existing_profile_not_removed(setup):
    c,store,cmd,temp,s,profile,calls,cleaned,home=setup
    existing=home/'Library/MobileDevice/Provisioning Profiles'/f'{profile["UUID"]}.mobileprovision'
    existing.parent.mkdir(parents=True);existing.write_bytes(b'fake-profile')
    with build.ios_signing(c,store,cmd,temp):pass
    assert existing.read_bytes()==b'fake-profile'


def test_refuses_store_upload_before_keychain_changes(setup):
    c,store,cmd,temp,s,profile,calls,cleaned,home=setup
    s['export_options']['destination']='upload'
    with pytest.raises(SafeError,match='never upload'):
        with build.ios_signing(c,store,cmd,temp):pass
    assert calls==[]


def test_expired_profile_fails_and_cleans_keychain(setup):
    c,store,cmd,temp,s,profile,calls,cleaned,home=setup
    profile['ExpirationDate']=datetime.datetime.now()-datetime.timedelta(days=1)
    with pytest.raises(SafeError,match='expired'):
        with build.ios_signing(c,store,cmd,temp):pass
    assert any(x[1]=='delete-keychain' for x in cleaned)


def test_restore_failure_keeps_journal(setup,monkeypatch):
    c,store,cmd,temp,s,profile,calls,cleaned,home=setup
    monkeypatch.setattr(build.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=1))
    with pytest.raises(SafeError,match='Signing cleanup failed'):
        with build.ios_signing(c,store,cmd,temp):pass
    assert (temp.parent/'data/signing-recovery.json').exists()
