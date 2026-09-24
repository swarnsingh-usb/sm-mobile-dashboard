"""Durable manual dev runs for the SSH-only Mac; no hosted CI API required."""
import contextlib
import datetime
import fcntl
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from . import source
from .core import ROOT, WORKFLOWS, SafeError, Secrets, atomic_json, load_config
from .locking import check_window, host_lock


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def identity(c):
    return {'source_provider': source.provider(c), 'source_url': source.clone_url(c),
            'native_url': source.native_url(c), 'project': c['project_path']}


class LocalRuns:
    def __init__(self, c, root=ROOT):
        self.c, self.root = c, Path(root)
        self.directory = self.root / 'data/local-runs'
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def folder(self, ident):
        if not str(ident).isdigit():
            raise SafeError('Invalid run ID.')
        return self.directory / str(ident)

    def pipeline(self, ident):
        try:
            row = json.loads((self.folder(ident) / 'run.json').read_text())
        except (OSError, ValueError):
            raise SafeError('Unknown local run.') from None
        if row['project'] != self.c['project_path']:
            raise SafeError('Run is outside this project.')
        if self.canceled(ident) and row['status'] in ('pending', 'running'):
            row['cancel_requested'] = True
            if row['status'] == 'pending':
                row['status'] = 'canceled'
                for job in row['jobs']:
                    job['status'] = 'canceled'
        return row

    def save(self, row):
        atomic_json(self.folder(row['id']) / 'run.json', row)

    def builds(self):
        paths = sorted(self.directory.glob('[0-9]*/run.json'), key=lambda p: int(p.parent.name), reverse=True)
        return [self.pipeline(p.parent.name) for p in paths]

    def launch(self, workflow, request_id):
        if workflow not in WORKFLOWS:
            raise SafeError('Unknown workflow.')
        check_window(self.root)
        # Exclusive mkdir is the ID allocator across concurrent web requests.
        ident = int(time.time() * 1000)
        while True:
            try:
                folder = self.folder(ident)
                folder.mkdir(mode=0o700)
                break
            except FileExistsError:
                ident += 1
        targets = ['android', 'ios'] if workflow == 'both-dev' else [workflow.removesuffix('-dev')]
        jobs = [{'id': ident * 10 + i, 'name': target, 'status': 'pending', 'pipeline': {'id': ident}}
                for i, target in enumerate(['checkout', *targets])]
        row = dict(identity(self.c), id=ident, ref='dev', sha='', status='pending',
                   name='Bento Mac / ' + workflow, workflow=workflow, request_id=request_id,
                   created_at=now(), jobs=jobs, source_repository=source.repository(self.c), backend='local')
        self.save(row)
        return row

    def canceled(self, ident):
        return (self.folder(ident) / 'cancel').exists()

    def cancel(self, ident):
        row = self.pipeline(ident)
        if row['status'] in ('pending', 'running'):
            (self.folder(ident) / 'cancel').touch(mode=0o600)
            row['cancel_requested'] = True
        return self.pipeline(ident)

    def job(self, ident):
        row = self.pipeline(int(ident) // 10)
        for job in row['jobs']:
            if job['id'] == int(ident):
                return row, job
        raise SafeError('Unknown local job.')

    def log(self, ident):
        row, job = self.job(ident)
        path = self.folder(row['id']) / (job['name'] + '.log')
        if not path.exists():
            return b'Job has not started yet.'
        with path.open('rb') as stream:
            stream.seek(max(0, path.stat().st_size - 512_000))
            return stream.read()


@contextlib.contextmanager
def worker_slot(root):
    path = Path(root) / 'data/local-worker.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        path.chmod(0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SafeError('The local worker is already running. Launch from the dashboard, or stop the idle worker before ./bento run.') from None
        try:
            if (Path(root) / 'data/local-worker-active.json').exists():
                raise SafeError('An interrupted worker needs inspection. See docs/GITHUB.md recovery before starting more builds.')
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def process_run(runs, row, c):
    from .build import Commands, Tee, execute_build, tool_env
    ident = row['id']
    marker = runs.root / 'data/local-worker-active.json'
    job = row['jobs'][0]
    def cancellation():
        return runs.canceled(ident)
    def checkpoint():
        if cancellation():
            raise KeyboardInterrupt()
    @contextlib.contextmanager
    def job_log(current):
        current['status'] = 'running'
        runs.save(row)
        path = runs.folder(ident) / (current['name'] + '.log')
        with path.open('x') as log:
            path.chmod(0o600)
            with contextlib.redirect_stdout(Tee(sys.stdout, log)), contextlib.redirect_stderr(Tee(sys.stderr, log)):
                try:
                    yield
                except BaseException as error:
                    print(str(error) if isinstance(error, SafeError) else type(error).__name__, flush=True)
                    raise
        current['status'] = 'success'
        runs.save(row)
    try:
        checkpoint()
        if any(row.get(k) != value for k, value in identity(c).items()):
            raise SafeError('Source configuration changed after this run was queued. Launch a new run after reviewing the source.')
        # One slot for source resolution and both platforms; all use one pinned dev SHA.
        with host_lock(runs.root):
            checkpoint()
            atomic_json(marker, {'id': ident, 'pid': os.getpid()})
            row.update(status='running', started_at=now())
            runs.save(row)
            with tempfile.TemporaryDirectory(prefix='remote-run-', dir=runs.root / 'runtime') as tmp:
                temp = Path(tmp)
                cmd = Commands(tool_env(c, runs.root), canceled=cancellation)
                checkout = temp / 'source'
                with job_log(job):
                    source.authenticate(c, Secrets(c), cmd, temp)
                    cmd.run(['git', 'clone', '--no-checkout', '--single-branch', '--branch', 'dev', '--',
                             source.clone_url(c), checkout], label='Fetch configured dev source')
                    sha = cmd.run(['git', 'rev-parse', '--verify', 'refs/remotes/origin/dev^{commit}'], checkout, capture=True).strip()
                    if not re.fullmatch(r'[0-9a-f]{40,64}', sha):
                        raise SafeError('Source did not resolve to a commit.')
                    row['sha'] = sha
                    runs.save(row)
                    print(f'Pinned {source.provider(c)} dev commit: {sha}', flush=True)
                for job in row['jobs'][1:]:
                    checkpoint()
                    check_window(runs.root)
                    with job_log(job), tempfile.TemporaryDirectory(prefix=job['name'] + '-', dir=temp) as working:
                        repo = Path(working) / 'app'
                        cmd.run(['git', 'clone', '--no-local', '--no-checkout', '--', checkout, repo], capture=True,
                                label='Create disposable platform checkout')
                        cmd.run(['git', 'checkout', '--detach', sha], repo, capture=True, label='Select pinned dev commit')
                        execute_build(c, job['name'], repo, sha, str(ident), str(job['id']),
                                      source='local-worker', canceled=cancellation)
                    checkpoint()
                row['status'] = 'success'
    except BaseException as error:
        row['status'] = 'canceled' if isinstance(error, KeyboardInterrupt) else 'failed'
        if job['status'] != 'success':
            job['status'] = row['status']
        row['error'] = str(error) if isinstance(error, SafeError) else type(error).__name__
        print(f'Run {ident}: {row["error"]}', flush=True)
        # A dashboard cancellation stops only this run; SSH interruption stops the worker.
        if isinstance(error, KeyboardInterrupt) and not cancellation():
            raise
    finally:
        for item in row['jobs']:
            if item['status'] == 'pending':
                item['status'] = 'skipped'
        row['finished_at'] = now()
        runs.save(row)
        marker.unlink(missing_ok=True)
    return row


def worker(once=False, workflow=None):
    from .build import build_signals
    c = load_config()
    if c.get('build_backend', 'gitlab') != 'local':
        raise SafeError('Select the local backend first: ./bento source github (or gitlab --backend local).')
    (ROOT / 'runtime').mkdir(parents=True, exist_ok=True)
    with build_signals(), worker_slot(ROOT):
        runs = LocalRuns(c)
        if workflow:
            row = runs.launch(workflow, 'terminal')
            result = process_run(runs, row, c)
            if result['status'] != 'success':
                raise SafeError(f'Run {row["id"]} {result["status"]}. See data/local-runs/{row["id"]}/ for logs.')
            return
        print('Local worker ready. Keep this SSH session open; Ctrl-C stops it safely.', flush=True)
        while True:
            pending = [r for r in reversed(runs.builds()) if r['status'] == 'pending']
            if pending:
                process_run(runs, pending[0], load_config())
            elif once:
                return
            else:
                time.sleep(2)
