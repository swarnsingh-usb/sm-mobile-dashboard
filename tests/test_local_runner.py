import copy
import json
import os
import subprocess
import sys
import time

import pytest

from bento_ci import build, core, local_runner, source
from bento_ci.core import ROOT, SafeError, atomic_json
from bento_ci.locking import open_window
from bento_ci.server import create_app
from test_portal import Store, login


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    c = json.loads((ROOT / 'config.example.json').read_text())
    c.update(source_provider='github', build_backend='local', credential_source='local', public_url='http://localhost')
    source.defaults(c)
    monkeypatch.setattr(core.boto3, 'client', lambda *a, **k: pytest.fail('No AWS'))
    monkeypatch.setattr('bento_ci.server.GitLab', lambda *a, **k: pytest.fail('No GitLab API'))
    monkeypatch.setattr(build, 'ROOT', tmp_path)
    monkeypatch.setattr(build, 'bitrise_processes', lambda: [])
    monkeypatch.setattr('bento_ci.locking.bitrise_processes', lambda: [])
    (tmp_path / 'runtime').mkdir()
    app_repo = tmp_path / 'remote'
    app_repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(app_repo), *args], text=True, stderr=subprocess.DEVNULL).strip()
    git('init', '-b', 'dev')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.invalid')
    (app_repo / 'tracked.txt').write_text('first commit')
    git('add', '.')
    git('commit', '-m', 'fixture')
    sha = git('rev-parse', 'HEAD')
    real_run = build.Commands.run
    def run(self, args, *a, **kwargs):
        # Only substitute the remote transport; actual Git cloning/checkout still runs.
        args = [str(app_repo) if str(arg) == source.clone_url(c) else arg for arg in args]
        return real_run(self, args, *a, **kwargs)
    monkeypatch.setattr(build.Commands, 'run', run)
    open_window(tmp_path, 5)
    client = create_app(c, Store(), root=tmp_path).test_client()
    headers = login(client)
    return c, tmp_path, client, headers, local_runner.LocalRuns(c, tmp_path), sha, git


def test_dashboard_launch_worker_logs_artifacts_and_pinned_commit(fixture, monkeypatch):
    c, root, client, headers, runs, sha, git = fixture
    body = dict(workflow='both-dev', request_id='a' * 36)
    launched = client.post('/api/builds', json=body, headers=headers)
    assert launched.status_code == 201
    row = launched.json
    assert client.post('/api/builds', json=body, headers=headers).json['id'] == row['id']
    assert len(runs.builds()) == 1
    assert client.get('/api/meta').json['source_provider'] == 'github'
    seen = []
    def execute(config, platform, repo, commit, pipeline, job, **kwargs):
        assert commit == sha
        assert (repo / 'tracked.txt').read_text() == 'first commit'
        assert not (repo / 'platform-dirty').exists()
        (repo / 'platform-dirty').touch()
        seen.append(platform)
        # Advance remote dev after Android: iOS must retain the fetched SHA.
        if platform == 'android':
            (root / 'remote/tracked.txt').write_text('new commit')
            git('commit', '-am', 'advance dev while Android builds')
        build.Commands(os.environ.copy(), ['private-value']).run(['/bin/echo', 'private-value'])
        folder = core.artifact_dir(root, pipeline, job, platform)
        folder.mkdir(parents=True)
        (folder / 'app.bin').write_bytes(b'test artifact')
        atomic_json(folder / 'manifest.json', {'project': c['project_path'], 'pipeline_id': pipeline,
            'job_id': job, 'platform': platform, 'files': [{'name': 'app.bin'}]})
    monkeypatch.setattr(build, 'execute_build', execute)
    with local_runner.worker_slot(root):
        local_runner.process_run(runs, row, c)
    assert seen == ['android', 'ios']
    detail = client.get(f'/api/builds/{row["id"]}').json
    assert detail['pipeline']['status'] == 'success'
    assert detail['pipeline']['sha'] == sha
    for job in detail['jobs'][1:]:
        log = client.get(f'/api/jobs/{job["id"]}/log').data
        assert b'[REDACTED]' in log and b'private-value' not in log
        link = job['artifacts_local'][0]['files'][0]['download']
        assert client.get(link).data == b'test artifact'
        assert job['retry_allowed'] is False
    assert not (root / 'data/local-worker-active.json').exists()
    # Data survives recreating the app/backend, without contacting any hosted CI API.
    fresh = create_app(c, Store(), root=root).test_client()
    login(fresh)
    assert fresh.get('/api/builds').json[0]['status'] == 'success'


@pytest.mark.parametrize('condition', ['failure', 'cancel', 'expiry'])
def test_failed_canceled_or_expired_run_never_starts_second_platform(fixture, monkeypatch, condition):
    c, root, client, headers, runs, _, _ = fixture
    row = runs.launch('both-dev', 'terminal')
    seen = []
    def execute(*a, **k):
        seen.append(a[1])
        if condition == 'failure': raise SafeError('Native fixture failed')
        if condition == 'cancel':
            client.post(f'/api/builds/{row["id"]}/cancel', json={}, headers=headers)
        if condition == 'expiry': atomic_json(root / 'data/pilot-window.json', {'expires_at': 0})
    monkeypatch.setattr(build, 'execute_build', execute)
    local_runner.process_run(runs, row, c)
    assert seen == ['android']
    assert runs.pipeline(row['id'])['status'] == ('canceled' if condition == 'cancel' else 'failed')
    assert row['jobs'][2]['status'] in ('skipped', 'failed')
    assert not (root / 'data/local-worker-active.json').exists()


def test_queued_cancel_source_change_and_closed_window(fixture, monkeypatch):
    c, root, client, headers, runs, _, _ = fixture
    monkeypatch.setattr(build, 'execute_build', lambda *a, **k: pytest.fail('Must not build'))
    row = runs.launch('both-dev', 'terminal')
    runs.cancel(row['id'])
    assert runs.pipeline(row['id'])['status'] == 'canceled'
    local_runner.process_run(runs, row, c)
    assert row['status'] == 'canceled'
    row = runs.launch('ios-dev', 'terminal')
    changed = copy.deepcopy(c)
    changed['source_provider'] = 'gitlab'
    local_runner.process_run(runs, row, changed)
    assert row['status'] == 'failed' and 'Source configuration changed' in row['error']
    (root / 'data/pilot-window.json').unlink()
    assert client.post('/api/builds', json={'workflow': 'both-dev', 'request_id': 'b' * 36}, headers=headers).status_code == 400


def test_worker_exclusivity_crash_recovery_guard_and_unknown_job(fixture):
    _, root, client, _, runs, _, _ = fixture
    script = 'import fcntl,sys;f=open(sys.argv[1],"a");fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)'
    with local_runner.worker_slot(root):
        r = subprocess.run([sys.executable, '-c', script, str(root / 'data/local-worker.lock')], capture_output=True)
        assert r.returncode != 0
    atomic_json(root / 'data/local-worker-active.json', {'id': 1, 'pid': 0})
    with pytest.raises(SafeError, match='interrupted worker'):
        with local_runner.worker_slot(root): pass
    assert client.get('/api/jobs/12345/log').status_code == 400


def test_cancellation_terminates_running_subprocess(monkeypatch):
    monkeypatch.setattr(build, 'bitrise_processes', lambda: [])
    started = time.monotonic()
    cmd = build.Commands(os.environ.copy(), canceled=lambda: time.monotonic() - started > .1)
    with pytest.raises(KeyboardInterrupt):
        cmd.run([sys.executable, '-c', 'import time;time.sleep(45)'])
    assert time.monotonic() - started < 15


def test_gitlab_source_uses_same_local_worker_without_pipeline_api(fixture, monkeypatch):
    c, root, _, _, runs, sha, _ = fixture
    c.update(source_provider='gitlab', _config_dir=str(root))
    atomic_json(root / 'local-secrets.json', {'gitlab': {'token': 'private-fixture-token'}})
    seen = []
    monkeypatch.setattr(build, 'execute_build', lambda *a, **k: seen.append((a[1], a[3])))
    row = runs.launch('both-dev', 'terminal')
    local_runner.process_run(runs, row, c)
    assert seen == [('android', sha), ('ios', sha)]
    assert row['status'] == 'success' and row['source_provider'] == 'gitlab'


@pytest.mark.parametrize('fail', [False, True])
def test_work_mac_validation_dashboard_records_result_without_signing_or_native_jobs(fixture, monkeypatch, fail):
    c, root, _, _, _, sha, _ = fixture
    c.update(source_provider='gitlab', default_workflow='validate-dev', _config_dir=str(root))
    c['gitlab'] = dict(transport='ssh', auth='existing', ssh_key='')
    client = create_app(c, Store(), root=root).test_client()
    headers = login(client)
    assert client.get('/api/meta').json['default_workflow'] == 'validate-dev'
    launched = client.post('/api/builds', json={'workflow': 'validate-dev', 'request_id': 'c' * 36}, headers=headers)
    assert launched.status_code == 201
    runs = local_runner.LocalRuns(c, root)
    row = runs.pipeline(launched.json['id'])
    def execute(c, platform, repo, commit, *a, **k):
        assert platform == 'validate' and commit == sha
        assert (repo / 'tracked.txt').read_text() == 'first commit'
        print('fixture validation log')
        if fail: raise SafeError('Fixture lint failed')
    monkeypatch.setattr(build, 'execute_build', execute)
    local_runner.process_run(runs, row, c)
    detail = client.get(f'/api/builds/{row["id"]}').json
    assert detail['pipeline']['status'] == ('failed' if fail else 'success')
    assert [j['name'] for j in detail['jobs']] == ['checkout', 'validate']
    job = detail['jobs'][1]
    assert b'fixture validation log' in client.get(f'/api/jobs/{job["id"]}/log').data
    assert job['artifacts_local'] == []
