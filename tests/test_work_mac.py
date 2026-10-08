import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from bento_ci import build, cli, core, source
from bento_ci.core import ROOT, SafeError, Secrets, atomic_json


@pytest.fixture
def config(tmp_path, monkeypatch):
    c = json.loads((ROOT / 'config.example.json').read_text())
    c.update(source_provider='gitlab', build_backend='local', credential_source='local',
             minimum_free_gb=0, _config_dir=str(tmp_path))
    c['gitlab'] = dict(transport='ssh', auth='existing', ssh_key='')
    source.defaults(c)
    monkeypatch.setattr(core.boto3, 'client', lambda *a, **k: pytest.fail('No AWS in work Mac mode'))
    return c


@pytest.mark.parametrize('access', ['ssh', 'https', 'token'])
def test_fresh_work_mac_setup_selects_local_validation_and_only_required_credentials(tmp_path, monkeypatch, access, capsys):
    (tmp_path / 'config.example.json').write_bytes((ROOT / 'config.example.json').read_bytes())
    monkeypatch.setattr(cli, 'ROOT', tmp_path)
    monkeypatch.setattr(cli, 'load_config', lambda: core.load_config(tmp_path / 'config.json'))
    monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(cli.sys, 'argv', ['bento', 'setup', '--work-mac'])
    prompts = []
    def ask(label, default=''):
        prompts.append(label)
        return access if label.startswith('GitLab access:') else default
    hidden = []
    def password(label):
        hidden.append(label)
        return 'fixture-private-value-at-least-24-characters'
    monkeypatch.setattr(cli, 'ask', ask)
    monkeypatch.setattr(cli.getpass, 'getpass', password)
    monkeypatch.setattr(cli, 'install_tools', lambda: None)
    def forbidden(*a, **k): pytest.fail('Work Mac setup must not invoke native tools, AWS templates or services')
    for name in ('configure_native_tools', 'generate', 'register', 'service'):
        monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr(core.boto3, 'client', forbidden)
    checked = []
    def doctor(c, online=False, build_only=False, validation=False):
        assert validation
        assert c['build_backend'] == 'local'
        checked.append(online)
        if online:
            store = Secrets(c)
            store.get('dependencies', required_fields=('npm_token',))
            store.get('portal')
            if access == 'token': store.get('gitlab')
        return False
    monkeypatch.setattr(cli, 'doctor', doctor)
    assert cli.main() == 0
    c = cli.load_config()
    assert c['credential_source'] == 'local'
    assert c['default_workflow'] == 'validate-dev'
    assert c['gitlab']['transport'] == ('ssh' if access == 'ssh' else 'https')
    assert c['gitlab']['auth'] == ('token' if access == 'token' else 'existing')
    assert checked == [False, True]
    secrets_path = tmp_path / 'local-secrets.json'
    saved = json.loads(secrets_path.read_text())
    assert set(saved) == ({'dependencies', 'portal', 'gitlab'} if access == 'token' else {'dependencies', 'portal'})
    assert set(saved['dependencies']) == {'npm_token'}
    assert secrets_path.stat().st_mode & 0o777 == 0o600
    assert all('maven' not in p and 'P12' not in p for p in hidden)
    assert all('AWS' not in p and 'Xcode' not in p for p in prompts)
    assert 'fixture-private' not in capsys.readouterr().out
    # Re-running setup keeps configured access, native paths and existing credentials.
    c['toolchain']['java_home'] = '/previous/java'
    atomic_json(tmp_path / 'config.json', c)
    monkeypatch.setattr(cli.getpass, 'getpass', lambda *a: '')
    assert cli.main() == 0
    assert cli.load_config()['toolchain']['java_home'] == '/previous/java'
    assert json.loads(secrets_path.read_text()) == saved


@pytest.mark.parametrize('online_blocked', [False, True])
def test_work_mac_setup_stops_when_prerequisites_fail(config, monkeypatch, online_blocked, capsys):
    monkeypatch.setattr(cli, 'configure', lambda **k: config)
    monkeypatch.setattr(cli, 'install_tools', lambda: None)
    monkeypatch.setattr(cli, 'doctor', lambda c, online=False, **k: online == online_blocked)
    calls = []
    monkeypatch.setattr(cli, 'credentials', lambda *a, **k: calls.append('credentials'))
    monkeypatch.setattr(cli, 'portal_credentials', lambda *a: calls.append('portal'))
    assert cli.setup_work_mac() == 2
    assert calls == (['credentials', 'portal'] if online_blocked else [])
    assert 'setup complete' not in capsys.readouterr().out


def test_gitlab_ssh_uses_existing_agent_and_rewrites_native_url(config, tmp_path):
    class NoSecrets:
        def get(self, *a, **k): pytest.fail('Existing GitLab auth must not read a token')
    cmd = build.Commands({})
    source.authenticate(config, NoSecrets(), cmd, tmp_path)
    assert 'BatchMode=yes' in cmd.env['GIT_SSH_COMMAND']
    assert 'StrictHostKeyChecking=yes' in cmd.env['GIT_SSH_COMMAND']
    assert source.clone_url(config) == 'git@gitlab.us.bank-dns.com:BENTO/bento.mobileapp.git'
    assert source.native_url(config) == source.clone_url(config)
    assert 'BENTO_GIT_TOKEN' not in cmd.env


def test_gitlab_https_reuses_real_configured_credential_helper(config, tmp_path):
    config['gitlab']['transport'] = 'https'
    git_config = tmp_path / 'gitconfig'
    git_config.write_text('[credential]\n\thelper = "!f() { echo username=fixture; echo password=fixture-secret; }; f"\n')
    env = {**os.environ, 'GIT_CONFIG_GLOBAL': str(git_config), 'GIT_CONFIG_NOSYSTEM': '1'}
    cmd = build.Commands(env)
    source.authenticate(config, Secrets(config), cmd, tmp_path)
    result = subprocess.run(['git', 'credential', 'fill'], cwd=tmp_path, env=cmd.env, text=True,
                            input='url=' + source.clone_url(config) + '\n\n', capture_output=True, check=True)
    assert 'password=fixture-secret' in result.stdout
    assert cmd.env['GIT_TERMINAL_PROMPT'] == '0'
    assert 'BENTO_GIT_TOKEN' not in cmd.env
    assert not list(tmp_path.glob('git-credential-bento.py'))


def test_validation_doctor_skips_native_tools_signing_and_native_branch(config, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, 'ROOT', tmp_path)
    monkeypatch.setattr(cli, 'bitrise_processes', lambda: [])
    monkeypatch.delenv('DEVELOPER_DIR', raising=False)
    (tmp_path / 'runtime').mkdir()
    atomic_json(tmp_path / 'local-secrets.json', {'dependencies': {'npm_token': 'fixture-secret'}})
    commands = []
    versions = {'git': 'git version 2.50.0', 'node': 'v20.19.4', 'yarn': '1.22.22'}
    def run(args, **kwargs):
        commands.append(args)
        assert args[0] in versions, 'Validation unexpectedly invoked native tooling'
        assert 'DEVELOPER_DIR' not in kwargs['env'], 'Git must work with Command Line Tools and no full Xcode'
        return SimpleNamespace(returncode=0, stdout=versions[args[0]], stderr='')
    monkeypatch.setattr(cli.subprocess, 'run', run)
    branches = []
    def remote(self, args, **kwargs):
        assert args[:2] == ['git', 'ls-remote']
        branches.append(args[-1])
        return 'a' * 40 + '\t' + args[-1] + '\n'
    monkeypatch.setattr(build.Commands, 'run', remote)
    assert cli.doctor(config, online=True, build_only=True, validation=True) is False
    assert branches == ['refs/heads/dev']
    assert [args[0] for args in commands] == ['git', 'node', 'yarn']


def test_aws_cached_validation_secret_still_requires_native_fields_and_expires(config, monkeypatch):
    config['credential_source'] = 'aws'
    reads = []
    def fetch(**kwargs):
        reads.append(True)
        return {'SecretString': json.dumps({'npm_token': 'fixture-secret'})}
    monkeypatch.setattr(core.boto3, 'client', lambda *a, **k: SimpleNamespace(get_secret_value=fetch))
    clock = [100.0]
    monkeypatch.setattr(core.time, 'monotonic', lambda: clock[0])
    store = Secrets(config)
    store.get('dependencies', required_fields=('npm_token',))
    clock[0] = 200.0
    store.get('dependencies', required_fields=('npm_token',))
    with pytest.raises(SafeError, match='dependencies'):
        store.get('dependencies')
    assert len(reads) == 1
    clock[0] = 401.0
    store.get('dependencies', required_fields=('npm_token',))
    assert len(reads) == 2


@pytest.mark.parametrize('failure', [None, 'typescript', 'lint', 'test'])
def test_validation_executes_code_checks_with_only_npm_and_propagates_failure(config, tmp_path, monkeypatch, failure):
    monkeypatch.setattr(build, 'ROOT', tmp_path)
    (tmp_path / 'runtime').mkdir()
    repo = tmp_path / 'app'
    (repo / 'ios').mkdir(parents=True)
    (repo / 'environments/dev').mkdir(parents=True)
    (repo / '.nvmrc').write_text('20.19.4\n')
    (repo / 'yarn.lock').write_text('locked fixture')
    (repo / 'environments/dev/usbank.env').write_text('ENV=dev\n')
    atomic_json(tmp_path / 'local-secrets.json', {'dependencies': {'npm_token': 'fixture-secret'}})
    steps = []
    def run(self, args, cwd=None, **kwargs):
        if args == ['node', '--version']: return 'v20.19.4'
        if args == ['yarn', '--version']: return '1.22.22'
        if args[0] == '/bin/bash':
            if 'scripts/prebuild.sh' in args: (repo / '.env').write_text('ENV=dev\n')
            return ''
        assert args[0] == 'yarn'
        steps.append(args[1])
        assert 'fixture-secret' in Path(self.env['NPM_CONFIG_USERCONFIG']).read_text()
        if args[1] == failure: raise SafeError('Fixture code check failed')
        return ''
    monkeypatch.setattr(build.Commands, 'run', run)
    monkeypatch.setattr(build.shutil, 'which', lambda *a, **k: '/fixture/node')
    if failure:
        with pytest.raises(SafeError, match='Fixture code check failed'):
            build.execute_build(config, 'validate', repo, 'a' * 40, '1', '1')
    else:
        build.execute_build(config, 'validate', repo, 'a' * 40, '1', '1')
    expected = ['install', 'typescript', 'lint', 'test']
    assert steps == (expected[:expected.index(failure) + 1] if failure else expected)
    assert not (repo / '.env').exists()
    assert not (tmp_path / 'data/artifacts').exists()
    assert not list((tmp_path / 'runtime').iterdir())
    # A code-only credential file cannot accidentally satisfy native build requirements.
    with pytest.raises(SafeError, match='dependencies'):
        Secrets(config).get('dependencies')
