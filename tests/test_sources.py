import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bento_ci import build, cli, core, source
from bento_ci.core import ROOT, SafeError, Secrets, atomic_json


@pytest.fixture
def config(tmp_path, monkeypatch):
    c = json.loads((ROOT / 'config.example.json').read_text())
    c.update(source_provider='github', build_backend='local', credential_source='local', _config_dir=str(tmp_path))
    source.defaults(c)
    monkeypatch.setattr(core.boto3, 'client', lambda *a, **k: pytest.fail('AWS must not be contacted'))
    return c


def test_switch_preserves_gitlab_and_credentials(config, tmp_path, monkeypatch):
    config['source_provider'] = 'gitlab'
    config.pop('github')
    config.pop('build_backend')
    config.pop('source_provider')
    atomic_json(tmp_path / 'config.json', config)
    atomic_json(tmp_path / 'local-secrets.json', {'gitlab': {'token': 'keep-private'}})
    monkeypatch.setattr(cli, 'ROOT', tmp_path)
    monkeypatch.setattr(cli, 'load_config', lambda: core.load_config(tmp_path / 'config.json'))
    old = cli.load_config()
    assert old['build_backend'] == 'gitlab'
    cli.select_source('github')
    gh = cli.load_config()
    assert source.clone_url(gh) == 'git@github.com:BentoInc/bento.mobileapp.git'
    assert gh['build_backend'] == 'local'
    for field in ('gitlab_url', 'project_path', 'ios', 'toolchain'):
        assert gh[field] == old[field]
    cli.select_source('gitlab')
    gl = cli.load_config()
    assert gl['build_backend'] == 'local'  # Same Mac worker after the source swap.
    assert source.native_url(gl) == old['ios']['native_dependencies_url']
    assert Secrets(gl).get('gitlab')['token'] == 'keep-private'
    cli.select_source('gitlab', backend='gitlab')
    assert cli.load_config()['build_backend'] == 'gitlab'


@pytest.mark.parametrize('field,value', [('source_provider', 'other'), ('build_backend', 'gitlab')])
def test_invalid_source_backend_rejected(config, field, value):
    config[field] = value
    with pytest.raises(SafeError): source.validate(config)


def test_ssh_uses_existing_auth_no_token_and_enforces_host_verification(config, tmp_path):
    class Store:
        def get(self, *a): pytest.fail('SSH must not read any token')
    cmd = build.Commands({})
    source.authenticate(config, Store(), cmd, tmp_path)
    assert 'BatchMode=yes' in cmd.env['GIT_SSH_COMMAND']
    assert 'StrictHostKeyChecking=yes' in cmd.env['GIT_SSH_COMMAND']
    assert 'BENTO_GIT_TOKEN' not in cmd.env
    key = tmp_path / 'key with spaces'
    key.write_text('test-placeholder')
    config['github']['ssh_key'] = str(key)
    source.authenticate(config, Store(), cmd, tmp_path)
    assert "'" + str(key) + "'" in cmd.env['GIT_SSH_COMMAND']


@pytest.mark.parametrize('provider', ['github', 'gitlab'])
def test_https_helper_releases_token_only_to_configured_repo(config, tmp_path, provider):
    config['source_provider'] = provider
    config['github']['transport'] = 'https'
    token = 'private-token-value'
    atomic_json(tmp_path / 'local-secrets.json', {provider: {'token': token}})
    cmd = build.Commands(os.environ.copy())
    source.authenticate(config, Secrets(config), cmd, tmp_path)
    host = 'github.com' if provider == 'github' else 'gitlab.us.bank-dns.com'
    repo = source.repository(config)
    helper = tmp_path / 'git-credential-bento.py'
    def read(hostname, path, protocol='https'):
        return subprocess.check_output([sys.executable, str(helper), 'get'], env=cmd.env,
            input=f'protocol={protocol}\nhost={hostname}\npath={path}\n\n', text=True)
    assert token in read(host, repo + '.git')
    assert read('attacker.example', repo + '.git') == ''
    assert read(host, 'OtherOrg/private.git') == ''
    assert read(host, repo + '.git', 'http') == ''
    assert token not in helper.read_text()
    assert token not in cmd.redact(token)


@pytest.mark.parametrize('provider,transport', [('github', 'ssh'), ('github', 'https'), ('gitlab', 'https')])
def test_native_dependency_rewrite_preserves_locked_commit(config, tmp_path, provider, transport):
    config['source_provider'] = provider
    config['github']['transport'] = transport
    config['toolchain'].update(expected_xcode='26.0', pod_bin='/fake/pod')
    atomic_json(tmp_path / 'local-secrets.json', {provider: {'token': 'test-private-token'}})
    ios = tmp_path / 'app/ios'
    ios.mkdir(parents=True)
    old = 'git@github.com:BentoInc/bento.mobileapp.git'
    (ios / 'Podfile').write_text(f"pod 'AuthCore', :git => '{old}', :branch => 'auth-26-06-cocoapods'\n")
    revision = '3bcb0eaeeb2bb98db63676b6025c25a694209e15'
    (ios / 'Podfile.lock').write_text(f'  :git: {old}\n  :commit: {revision}\nPODFILE CHECKSUM: ' + 'a' * 40 + '\n')
    class StopAtCompilation(Exception): pass
    class Cmd(build.Commands):
        def run(self, args, *a, **kwargs):
            if args == ['xcodebuild', '-version']: return 'Xcode 26.0\n'
            if args == ['ruby', '--version']: return 'ruby 3.2.1'
            if args == ['/fake/pod', '--version']: return '1.16.2'
            if args[:2] == ['git', 'ls-remote']:
                assert source.native_url(config) in args
                return revision
            assert args == ['/fake/pod', 'install', '--deployment']
            raise StopAtCompilation()
    with pytest.raises(StopAtCompilation):
        build.ios(config, Secrets(config), Cmd({}), ios.parent, tmp_path, tmp_path / 'out')
    assert source.native_url(config) in (ios / 'Podfile').read_text()
    assert ':commit: ' + revision in (ios / 'Podfile.lock').read_text()
    if provider == 'github':
        assert 'gitlab.us.bank-dns.com' not in (ios / 'Podfile').read_text()


def test_github_credential_wizard_skips_gitlab_and_keeps_saved_values(config, tmp_path, monkeypatch):
    (tmp_path / 'cert').write_bytes(b'cert')
    (tmp_path / 'profile').write_bytes(b'profile')
    atomic_json(tmp_path / 'local-secrets.json', {
        'gitlab': {'token': 'untouched-gitlab'},
        'ios': {'p12_file': 'cert', 'p12_password': 'pass', 'profile_files': ['profile'],
                'team_id': 'TEAM', 'signing_identity': 'Apple Distribution', 'export_options': {'method': 'release-testing'}}})
    monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(cli, 'ask', lambda label, default='': default)
    prompts = []
    def hidden(label):
        prompts.append(label)
        return '' if label == 'P12 password: ' else 'private-value'
    monkeypatch.setattr(cli.getpass, 'getpass', hidden)
    cli.credentials(config)
    assert not any('gitlab' in p or 'github' in p for p in prompts)
    assert Secrets(config).get('gitlab')['token'] == 'untouched-gitlab'


def test_github_local_checkout_allowed_wrong_origin_rejected(config, tmp_path, monkeypatch):
    from test_local import make_repository, configure_test_host
    repo = tmp_path / 'app'
    make_repository(repo, config)
    configure_test_host(tmp_path, config, monkeypatch)
    with pytest.raises(SafeError, match='configured source'):
        build.local_build('both', repo)
    subprocess.run(['git', '-C', str(repo), 'remote', 'set-url', 'origin', source.clone_url(config)], check=True)
    seen = []
    monkeypatch.setattr(build, 'execute_build', lambda *a, **k: seen.append(a[1]))
    build.local_build('both', repo)
    assert seen == ['android', 'ios']


def test_github_setup_does_not_install_or_register_gitlab_runner(config, tmp_path, monkeypatch):
    atomic_json(tmp_path / 'config.json', config)
    for name in ('tools/node/bin/node', 'tools/yarn/bin/yarn'):
        path = tmp_path / name
        path.parent.mkdir(parents=True)
        path.touch()
    monkeypatch.setattr(cli, 'ROOT', tmp_path)
    monkeypatch.setattr(cli, 'load_config', lambda: config)
    monkeypatch.setattr(cli.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(cli, 'download', lambda *a: pytest.fail('No GitLab download in local mode'))
    cli.install_tools()
    assert not (tmp_path / 'tools/gitlab-runner').exists()
    monkeypatch.setattr(cli.sys, 'argv', ['bento', 'setup', '--source', 'github', '--local', '--no-start'])
    def configure(local=False, source=None):
        assert local and source == 'github'
        return config
    monkeypatch.setattr(cli, 'configure', configure)
    monkeypatch.setattr(cli, 'doctor', lambda *a: False)
    monkeypatch.setattr(cli, 'register', lambda *a: pytest.fail('No GitLab registration'))
    monkeypatch.setattr(cli, 'service', lambda *a: pytest.fail('No service startup'))
    assert cli.main() == 0


def test_portal_credential_setup_preserves_source_secrets(config, tmp_path, monkeypatch, capsys):
    atomic_json(tmp_path / 'local-secrets.json', {'gitlab': {'token': 'private-preserved'}})
    monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(cli, 'ask', lambda *a: 'engineer')
    monkeypatch.setattr(cli.getpass, 'getpass', lambda *a: 'private-dashboard-password-at-least-24')
    cli.portal_credentials(config)
    portal = Secrets(config).get('portal')
    assert len(portal['session_key']) >= 32
    assert Secrets(config).get('gitlab')['token'] == 'private-preserved'
    assert 'private-dashboard' not in capsys.readouterr().out
