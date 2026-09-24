"""Local browser test fixture only. Never contacts AWS/GitLab or launches native builds."""
import json
import tempfile
import time
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from bento_ci.core import ROOT, atomic_json
from bento_ci.server import create_app

class Store:
    def get(self,*args,**kwargs):
        return {'username':'demo','password':'local-preview-only-password-12345','session_key':'synthetic-local-session-key-for-tests-only'}

class GitLab:
    def __init__(self):
        self.rows=[{'id':1042,'name':'Bento Mac / both-dev','status':'success','ref':'dev','sha':'a81bb52e72c6f2705488030f8641ea90d47fedf2','created_at':'2026-09-24T15:30:00Z'},
            {'id':1041,'name':'Bento Mac / android-dev','status':'failed','ref':'dev','sha':'a81bb52e72c6f2705488030f8641ea90d47fedf2','created_at':'2026-09-24T14:15:00Z'}]
    def launch(self,workflow,key):
        row={**self.rows[0],'id':1043,'name':'Bento Mac / '+workflow,'status':'pending'};self.rows.insert(0,row);return row
    def pipeline(self,ident):
        return next(x for x in self.rows if x['id']==ident)
    def project_call(self,method,path,**kwargs):
        if path=='/pipelines':return self.rows
        if path.endswith('/jobs'):
            pid=int(path.split('/')[2]);return [{'id':2111,'name':'android-dev','status':'success','pipeline':{'id':pid}}, {'id':2112,'name':'ios-dev','status':'success','pipeline':{'id':pid}}]
        if path.endswith('/trace'):return b'SYNTHETIC PREVIEW - no native build ran.\n\nCheckout: a81bb52e72c6\nEnvironment: usbank / dev\nDependencies resolved\nBuild complete\nArtifacts saved on Mac disk\n'
        if '/jobs/' in path:return {'id':2111,'pipeline':{'id':1042}}
        if path.endswith('/cancel'):
            self.rows[0]['status']='canceled';return self.rows[0]
        return {}

if __name__=='__main__':
    config=json.loads((ROOT/'config.example.json').read_text());config['public_url']='http://localhost:8876';config['port']=8876
    import bento_ci.locking
    bento_ci.locking.bitrise_processes=lambda:[]
    with tempfile.TemporaryDirectory(prefix='bento-preview-') as directory:
        root=Path(directory)
        atomic_json(root/'data/pilot-window.json',{'expires_at':time.time()+7200})
        for platform,job,name in [('android','2111','spendmanagement-dev.apk'),('ios','2112','SpendManagement-dev.ipa')]:
            folder=root/'data/artifacts/1042'/job/platform;folder.mkdir(parents=True)
            (folder/name).write_bytes(b'Synthetic artifact for browser tests only.')
            atomic_json(folder/'manifest.json',{'project':'BENTO/bento.mobileapp','pipeline_id':'1042','job_id':job,'platform':platform,
                'sha':'a81bb52e72c6f2705488030f8641ea90d47fedf2','environment':'dev','storage':'local','upload_status':'disabled',
                'native':{'signing':'synthetic-test'},'files':[{'name':name,'bytes':40,'sha256':'demo'}]})
        from waitress import serve
        serve(create_app(config,Store(),GitLab(),root),host='127.0.0.1',port=8876)
