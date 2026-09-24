import base64
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from bento_ci import build as builds, cli, core
from bento_ci.core import ROOT, SafeError, Secrets, atomic_json
from bento_ci.locking import open_window


@pytest.fixture
def local_config(tmp_path, monkeypatch):
    c = json.loads((ROOT / 'config.example.json').read_text())
    c.update(credential_source='local', _config_dir=str(tmp_path), minimum_free_gb=0)
    def no_aws(*args, **kwargs):
        raise AssertionError('Local mode must never initialize AWS')
    monkeypatch.setattr(core.boto3, 'client', no_aws)
    return c


def test_local_secrets_never_contact_aws_and_refresh_edits(local_config, tmp_path):
    path = tmp_path / 'local-secrets.json'
    atomic_json(path, {'gitlab': {'token': 'first-private-token'}})
    store = Secrets(local_config)
    assert store.get('gitlab')['token'] == 'first-private-token'
    atomic_json(path, {'gitlab': {'token': 'rotated-private-token'}})
    assert store.get('gitlab')['token'] == 'rotated-private-token'


def test_local_credentials_reject_readable_files_symlinks_and_invalid_schema(local_config, tmp_path):
    path = tmp_path / 'local-secrets.json'
    atomic_json(path, {'gitlab': {'token': 'do-not-leak'}})
    path.chmod(0o644)
    with pytest.raises(SafeError, match='Cannot read local credential') as error:
        Secrets(local_config).get('gitlab')
    assert 'do-not-leak' not in str(error.value)
    path.rename(tmp_path / 'real.json')
    path.symlink_to(tmp_path / 'real.json')
    with pytest.raises(SafeError):
        Secrets(local_config).get('gitlab')
    path.unlink()
    atomic_json(path, {'gitlab': {'token': ''}})
    with pytest.raises(SafeError):
        Secrets(local_config).get('gitlab')


def test_local_signing_files_expand_without_base64_manual_input(local_config, tmp_path):
    (tmp_path / 'signing.p12').write_bytes(b'certificate')
    (tmp_path / 'stage.mobileprovision').write_bytes(b'profile')
    atomic_json(tmp_path / 'local-secrets.json', {'ios': {
        'p12_file': 'signing.p12', 'profile_files': ['stage.mobileprovision'], 'p12_password': 'password',
        'team_id': 'TEAM', 'signing_identity': 'identity', 'export_options': {}}})
    result = Secrets(local_config).get('ios')
    assert base64.b64decode(result['p12_base64']) == b'certificate'
    assert base64.b64decode(result['profiles_base64'][0]) == b'profile'
    assert 'p12_base64' not in json.loads((tmp_path / 'local-secrets.json').read_text())['ios']


def test_local_mode_switch_preserves_configuration_and_secrets(local_config, tmp_path, monkeypatch):
    path = tmp_path / 'config.json'
    local_config['credential_source'] = 'aws'
    atomic_json(path, local_config)
    atomic_json(tmp_path / 'local-secrets.json', {'gitlab': {'token': 'retain-this'}})
    monkeypatch.setattr(cli, 'ROOT', tmp_path)
    monkeypatch.setattr(cli, 'load_config', lambda: core.load_config(path))
    cli.configure(local=True)
    updated = core.load_config(path)
    assert updated['credential_source'] == 'local'
    assert updated['toolchain'] == local_config['toolchain']
    assert Secrets(updated).get('gitlab')['token'] == 'retain-this'


def test_credentials_wizard_preserves_optional_fields_and_keeps_secrets_private(local_config, tmp_path, monkeypatch, capsys):
    (tmp_path / 'cert.p12').write_bytes(b'certificate')
    (tmp_path / 'profile.mobileprovision').write_bytes(b'profile')
    exports = tmp_path / 'ExportOptions.plist'
    exports.write_bytes(plistlib.dumps({'method': 'release-testing', 'teamID': 'TEAM',
        'provisioningProfiles': {local_config['ios']['bundle_id']: 'PROFILE'}}))
    path = tmp_path / 'local-secrets.json'
    atomic_json(path, {'portal': {'username': 'preserved-user'}})
    values = iter(['cert.p12', 'profile.mobileprovision', 'TEAM', 'Apple Distribution: Existing', str(exports)])
    monkeypatch.setattr(cli, 'ask', lambda *args: next(values))
    monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: True)
    private = iter(['private-npm', 'private-maven-user', 'private-maven-pass', 'private-gitlab', 'private-p12-pass'])
    monkeypatch.setattr(cli.getpass, 'getpass', lambda *args: next(private))
    cli.credentials(local_config)
    saved = json.loads(path.read_text())
    assert saved['portal']['username'] == 'preserved-user'
    assert saved['gitlab']['token'] == 'private-gitlab'
    assert Secrets(local_config).get('ios')['p12_base64']
    assert path.stat().st_mode & 0o777 == 0o600
    assert 'private-gitlab' not in capsys.readouterr().out


def test_local_setup_never_registers_or_starts_services(local_config, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, 'ROOT', tmp_path)
    monkeypatch.setattr(sys, 'argv', ['bento', 'setup', '--local'])
    def configure(local=False):
        assert local is True
        return local_config
    def forbidden(*args):
        raise AssertionError('Local setup must not register/start services')
    monkeypatch.setattr(cli, 'configure', configure)
    monkeypatch.setattr(cli, 'install_tools', lambda: None)
    monkeypatch.setattr(cli, 'doctor', lambda *a: False)
    monkeypatch.setattr(cli, 'register', forbidden)
    monkeypatch.setattr(cli, 'service', forbidden)
    assert cli.main() == 0


def make_repository(path, config):
    path.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(path), *args], text=True, stderr=subprocess.DEVNULL).strip()
    git('init', '-b', 'dev')
    git('config', 'user.email', 'test@example.invalid')
    git('config', 'user.name', 'Local test')
    git('remote', 'add', 'origin', config['gitlab_url'] + '/' + config['project_path'] + '.git')
    (path / 'tracked.txt').write_text('committed')
    git('add', '.')
    git('commit', '-m', 'fixture')
    sha = git('rev-parse', 'HEAD')
    (path / 'tracked.txt').write_text('uncommitted-user-work')
    (path / '.env').write_text('user-env-must-survive')
    return sha


def configure_test_host(root, config, monkeypatch):
    (root / 'runtime').mkdir()
    monkeypatch.setattr(builds, 'ROOT', root)
    monkeypatch.setattr(builds, 'load_config', lambda: config)
    monkeypatch.setattr(builds, 'bitrise_processes', lambda: [])
    monkeypatch.setattr('bento_ci.locking.bitrise_processes', lambda: [])
    open_window(root, 5)


def test_ssh_both_uses_same_commit_and_disposable_copies(local_config, tmp_path, monkeypatch):
    source = tmp_path / 'source with spaces'
    sha = make_repository(source, local_config)
    configure_test_host(tmp_path, local_config, monkeypatch)
    seen = []
    def execute(c, platform, repo, commit, pipeline, job, source):
        assert source == 'local'
        assert commit == sha
        assert (repo / 'tracked.txt').read_text() == 'committed'
        assert not (repo / '.env').exists()
        assert not (repo / 'previous-platform').exists()
        (repo / 'previous-platform').touch()
        seen.append((platform, str(repo), pipeline))
        # Exercise actual subprocess redaction and the persistent log tee.
        builds.Commands(os.environ.copy(), ['test-secret']).run(['/bin/echo', 'test-secret'])
    monkeypatch.setattr(builds, 'execute_build', execute)
    builds.local_build('both', str(source))
    assert [r[0] for r in seen] == ['android', 'ios']
    assert seen[0][1] != seen[1][1]
    assert seen[0][2] == seen[1][2]
    assert not Path(seen[0][1]).exists()
    assert (source / 'tracked.txt').read_text() == 'uncommitted-user-work'
    assert (source / '.env').read_text() == 'user-env-must-survive'
    run = next((tmp_path / 'data/test-runs').iterdir())
    assert json.loads((run / 'run.json').read_text())['status'] == 'success'
    assert 'test-secret' not in (run / 'android.log').read_text()
    assert '[REDACTED]' in (run / 'ios.log').read_text()
    assert (run / 'android.log').stat().st_mode & 0o777 == 0o600


def test_ssh_failure_preserved_and_no_second_platform_started(local_config, tmp_path, monkeypatch):
    repo = tmp_path / 'source'
    make_repository(repo, local_config)
    configure_test_host(tmp_path, local_config, monkeypatch)
    seen = []
    def execute(*args, **kwargs):
        seen.append(args[1])
        raise SafeError('Synthetic native failure')
    monkeypatch.setattr(builds, 'execute_build', execute)
    with pytest.raises(SafeError, match='Synthetic native failure'):
        builds.local_build('both', str(repo))
    assert seen == ['android']
    run = next((tmp_path / 'data/test-runs').iterdir())
    status = json.loads((run / 'run.json').read_text())
    assert status['status'] == 'failed'
    assert status['error'] == 'Synthetic native failure'
    assert not list((tmp_path / 'runtime').iterdir())


def test_ssh_requires_window_and_gitlab_source_and_does_not_bypass_ci_gate(local_config, tmp_path, monkeypatch):
    repo = tmp_path / 'source'
    make_repository(repo, local_config)
    configure_test_host(tmp_path, local_config, monkeypatch)
    (tmp_path / 'data/pilot-window.json').unlink()
    with pytest.raises(SafeError, match='No active pilot window'):
        builds.local_build('android', str(repo))
    subprocess.run(['git', '-C', str(repo), 'remote', 'set-url', 'origin', 'https://github.com/example/repo.git'], check=True)
    with pytest.raises(SafeError, match='separate clone'):
        builds.local_build('android', str(repo))
    monkeypatch.setenv('CI_PROJECT_PATH', local_config['project_path'])
    monkeypatch.setenv('CI_COMMIT_BRANCH', 'dev')
    monkeypatch.delenv('CI_COMMIT_REF_PROTECTED', raising=False)
    with pytest.raises(SafeError, match='Protect dev'):
        builds.build('android')
