import hashlib
import base64
import json
import os
import re
import stat
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import boto3
import requests
from botocore.config import Config as AWSConfig

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = {
    'both-dev': 'Android + iOS · dev',
    'android-dev': 'Android · dev',
    'ios-dev': 'iOS · dev',
    'validate-dev': 'Code checks · dev',
}


class SafeError(Exception):
    """Messages intentionally safe for build logs and HTTP responses."""


def https_url(value):
    p = urlsplit(value)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise SafeError('A plain HTTPS URL without credentials, query, or fragment is required.')
    return value.rstrip('/')


def load_config(path=None):
    path = Path(path or ROOT / 'config.json')
    if not path.exists():
        raise SafeError('Run ./bento configure first.')
    c = json.loads(path.read_text())
    c['_config_dir'] = str(path.resolve().parent)
    if c.get('credential_source', 'aws') not in ('aws', 'local'):
        raise SafeError('credential_source must be aws or local.')
    c['gitlab_url'] = https_url(c['gitlab_url'])
    if c['branch'] != 'dev':
        raise SafeError('This pilot is restricted to the dev branch.')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)+', c['project_path']):
        raise SafeError('Invalid GitLab project path.')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', c['runner_tag']):
        raise SafeError('Invalid runner tag.')
    for url in c['npm_registries']:
        https_url(url)
    https_url(c['ios']['native_dependencies_url'])
    a = c['artifactory']
    if bool(a['url']) != bool(a['repository']):
        raise SafeError('Set both Artifactory URL and repository, or leave both empty for local storage.')
    if a['url']:
        a['url'] = https_url(a['url'])
        if not c['secrets']['artifactory']:
            raise SafeError('Enable secrets.artifactory when configuring artifact uploads.')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', a['repository']):
            raise SafeError('Invalid Artifactory repository key.')
    if any(x in ('', '.', '..') for x in a['prefix'].split('/')):
        raise SafeError('Invalid artifact prefix.')
    p = urlsplit(c['public_url'])
    if p.username or p.password or p.query or p.fragment or p.path not in ('', '/'):
        raise SafeError('public_url must be an origin without a path.')
    if p.scheme != 'https' and not (p.scheme == 'http' and p.hostname in ('localhost', '127.0.0.1')):
        raise SafeError('Non-local access requires HTTPS; keep the app behind a TLS proxy.')
    return c


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, indent=2)
            f.write('\n')
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


class Secrets:
    def __init__(self, c):
        self.c = c
        self.cache = {}
        self.lock = threading.Lock()
        self.source = c.get('credential_source', 'aws')
        if self.source not in ('aws', 'local'):
            raise SafeError('credential_source must be aws or local.')
        # Local mode must never create an AWS client or query EC2 credentials.
        self.client = None
        if self.source == 'aws':
            self.client = boto3.client('secretsmanager', region_name=c['aws_region'], verify=c['ca_bundle'] or True,
                config=AWSConfig(connect_timeout=5, read_timeout=15, retries={'max_attempts': 2}))

    def local_path(self):
        path = Path(self.c.get('local_secrets_file', 'local-secrets.json')).expanduser()
        return path if path.is_absolute() else Path(self.c.get('_config_dir', ROOT)) / path

    def local_value(self, name):
        path = self.local_path()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('Local credentials must be owned by this user and private.')
            value = json.load(f)[name]
        def encode_asset(filename):
            asset = Path(filename).expanduser()
            if not asset.is_absolute():
                asset = path.parent / asset
            return base64.b64encode(asset.read_bytes()).decode()
        if name == 'ios':
            if value.get('p12_file'):
                value['p12_base64'] = encode_asset(value['p12_file'])
            if value.get('profile_files'):
                value['profiles_base64'] = [encode_asset(p) for p in value['profile_files']]
        if name == 'android' and value.get('keystore_file'):
            value['keystore_base64'] = encode_asset(value['keystore_file'])
        return value

    def get(self, name, fresh=False):
        ref = self.c['secrets'].get(name)
        if not ref and self.source == 'aws':
            raise SafeError(f'Secrets Manager reference is not configured: {name}')
        with self.lock:
            old = self.cache.get(name)
            if self.source == 'aws' and old and not fresh and time.monotonic() - old[0] < 300:
                return old[1]
            try:
                value = self.local_value(name) if self.source == 'local' else json.loads(self.client.get_secret_value(SecretId=ref)['SecretString'])
                if not isinstance(value, dict):
                    raise ValueError()
                required = {
                    'portal': ('username', 'password', 'session_key'),
                    'gitlab': ('token',), 'runner': ('token',),
                    'dependencies': ('npm_token', 'maven_username', 'maven_password'),
                    'ios': ('p12_base64', 'p12_password', 'team_id', 'signing_identity'),
                    'android': ('keystore_base64', 'store_password', 'key_alias', 'key_password'),
                    'artifactory': ('token',),
                }.get(name, ())
                if any(not isinstance(value.get(k), str) or not value[k] for k in required):
                    raise ValueError()
                if name == 'ios' and (not isinstance(value.get('profiles_base64'), list) or not value['profiles_base64'] or not isinstance(value.get('export_options'), dict)):
                    raise ValueError()
            except Exception:
                if self.source == 'local':
                    raise SafeError(f'Cannot read local credential {name}. Run ./bento credentials; check the JSON, asset paths, ownership and chmod 600 on local-secrets.json.') from None
                raise SafeError(f'Cannot read JSON secret {name}. Check the instance role, region, network, and secret schema.') from None
            self.cache[name] = (time.monotonic(), value)
            return value


class GitLab:
    def __init__(self, c, secrets):
        self.c, self.secrets = c, secrets
        self.project = quote(c['project_path'], safe='')

    def call(self, method, path, *, raw=False, **kwargs):
        token = self.secrets.get('gitlab')['token']
        try:
            r = requests.request(method, self.c['gitlab_url'] + '/api/v4/' + path,
                headers={'PRIVATE-TOKEN': token}, verify=self.c['ca_bundle'] or True,
                timeout=(10, 45), allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise SafeError('GitLab connection failed. For a launch, check build history before trying again.') from None
        if not 200 <= r.status_code < 300:
            raise SafeError(f'GitLab returned HTTP {r.status_code}. Check permissions, pipeline configuration, and protected dev access.')
        if raw:
            return r.content
        try:
            return r.json()
        except ValueError:
            raise SafeError('GitLab returned an unexpected response.') from None

    def project_call(self, method, path='', **kwargs):
        return self.call(method, f'projects/{self.project}' + path, **kwargs)

    def launch(self, workflow, request_id):
        if workflow not in WORKFLOWS:
            raise SafeError('Unknown workflow.')
        return self.project_call('POST', '/pipeline', json={'ref': 'dev', 'variables': [
            {'key': 'BENTO_PORTAL', 'value': '1'},
            {'key': 'BENTO_WORKFLOW', 'value': workflow},
            {'key': 'BENTO_REQUEST_ID', 'value': request_id},
        ]})

    def pipeline(self, ident):
        if not str(ident).isdigit():
            raise SafeError('Invalid pipeline ID.')
        p = self.project_call('GET', f'/pipelines/{ident}')
        variables = self.project_call('GET', f'/pipelines/{ident}/variables')
        v = {x['key']: x['value'] for x in variables}
        if p['ref'] != 'dev' or v.get('BENTO_PORTAL') != '1':
            raise SafeError('This pipeline is outside the pilot.')
        p['workflow'] = v.get('BENTO_WORKFLOW', 'unknown')
        return p


def artifact_dir(root, pipeline, job, platform):
    if not str(pipeline).isdigit() or not str(job).isdigit() or platform not in ('android', 'ios'):
        raise SafeError('Invalid artifact coordinates.')
    return Path(root) / 'data' / 'artifacts' / str(pipeline) / str(job) / platform
