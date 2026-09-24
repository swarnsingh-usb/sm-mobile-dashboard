import functools
import hmac
import json
import logging
import re
import secrets as random_secrets
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, quote

from flask import Flask, jsonify, redirect, render_template, request, send_file, session
from werkzeug.security import check_password_hash, generate_password_hash

from .core import ROOT, WORKFLOWS, GitLab, SafeError, Secrets, artifact_dir, load_config
from .locking import check_window
from . import source as sources
from .local_runner import LocalRuns


def create_app(config=None, secret_store=None, gitlab=None, root=ROOT):
    c = config or load_config()
    root = Path(root)
    (root / 'data').mkdir(parents=True, exist_ok=True)
    store = secret_store or Secrets(c)
    credentials = store.get('portal')
    if len(credentials.get('password', '')) < 24 or len(credentials.get('session_key', '')) < 32:
        raise SafeError('Portal secret needs a password of at least 24 characters and a session_key of at least 32.')
    app = Flask(__name__, template_folder=str(ROOT / 'templates'), static_folder=str(ROOT / 'static'))
    app.config.update(SECRET_KEY=credentials['session_key'], MAX_CONTENT_LENGTH=8192,
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict',
        SESSION_COOKIE_SECURE=c['public_url'].startswith('https:'), PERMANENT_SESSION_LIFETIME=28800)
    local = LocalRuns(c, root) if c.get('build_backend', 'gitlab') == 'local' else None
    gl = None if local else (gitlab or GitLab(c, store))
    rate = {}
    rate_lock = threading.Lock()
    dbpath = root / 'data/requests.sqlite3'
    with sqlite3.connect(dbpath) as db:
        db.execute('CREATE TABLE IF NOT EXISTS launches (request_id TEXT PRIMARY KEY, workflow TEXT, result TEXT)')
    dbpath.chmod(0o600)

    def authenticated(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            if not session.get('user'):
                return jsonify(error='Sign in to continue.'), 401
            return fn(*args, **kwargs)
        return inner

    @app.before_request
    def protect():
        expected = urlsplit(c['public_url']).netloc
        if request.host != expected:
            return jsonify(error='Unexpected Host header.'), 400
        if request.method == 'POST':
            if request.headers.get('Origin') != c['public_url'].rstrip('/'):
                return jsonify(error='Request origin rejected.'), 403
            if request.path != '/api/login' and not hmac.compare_digest(
                    request.headers.get('X-CSRF-Token', ''), session.get('csrf', '!')):
                return jsonify(error='Request token rejected.'), 403

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.errorhandler(SafeError)
    def safe_error(e):
        return jsonify(error=str(e)), 400

    @app.errorhandler(Exception)
    def internal_error(e):
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return jsonify(error=e.name), e.code
        # Do not write request bodies or SDK exception payloads (which may include secrets).
        logging.error('Portal request failed: %s', type(e).__name__)
        return jsonify(error='The request failed. Check service connectivity and configuration.'), 502

    @app.get('/')
    def index():
        return render_template('index.html')

    @app.post('/api/login')
    def login():
        now = time.monotonic()
        key = request.remote_addr
        with rate_lock:
            history = [t for t in rate.get(key, []) if now - t < 60]
            if len(history) >= 10:
                return jsonify(error='Too many sign-in attempts. Wait one minute.'), 429
            history.append(now)
            rate[key] = history
        supplied = request.get_json() or {}
        cred = store.get('portal', fresh=True)
        if not hmac.compare_digest(str(supplied.get('username', '')).encode(), str(cred.get('username', 'engineer')).encode()) or not hmac.compare_digest(
                str(supplied.get('password', '')).encode(), str(cred['password']).encode()):
            return jsonify(error='Invalid username or password.'), 401
        session.clear()
        session.update(user=cred.get('username', 'engineer'), csrf=random_secrets.token_urlsafe(32))
        session.permanent = True
        return jsonify(csrf=session['csrf'])

    @app.post('/api/logout')
    @authenticated
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get('/api/meta')
    @authenticated
    def meta():
        try:
            window = check_window(root)
            ready = True
            reason = ''
        except SafeError as e:
            window, ready, reason = {}, False, str(e)
        return jsonify(project=sources.repository(c), source_provider=sources.provider(c),
            backend=c.get('build_backend', 'gitlab'), branch='dev', workflows=WORKFLOWS,
            csrf=session['csrf'], storage='Artifactory + Mac' if c['artifactory']['url'] else 'Mac disk',
            window=window, ready=ready, reason=reason)

    @app.get('/api/builds')
    @authenticated
    def builds():
        if local:
            return jsonify(local.builds()[:50])
        rows = gl.project_call('GET', '/pipelines', params={'ref': 'dev', 'per_page': 50, 'order_by': 'id', 'sort': 'desc'})
        # Pipeline names are set by the generated YAML. Verify the marker again for actions.
        return jsonify([r for r in rows if (r.get('name') or '').startswith('Bento Mac / ')])

    @app.post('/api/builds')
    @authenticated
    def launch():
        check_window(root)
        data = request.get_json() or {}
        key = data.get('request_id', '')
        workflow = data.get('workflow')
        if workflow not in WORKFLOWS or not re.fullmatch(r'[a-f0-9-]{36}', key):
            raise SafeError('Choose a supported workflow and supply a valid request ID.')
        with sqlite3.connect(dbpath) as db:
            try:
                db.execute('INSERT INTO launches VALUES (?, ?, NULL)', (key, workflow))
            except sqlite3.IntegrityError:
                old = db.execute('SELECT workflow,result FROM launches WHERE request_id=?', (key,)).fetchone()
                if old[0] != workflow:
                    raise SafeError('Request ID was already used for a different workflow.')
                if old[1]:
                    return jsonify(json.loads(old[1]))
                return jsonify(error='This launch is pending or its result is unknown. Check build history before launching another.'), 409
        result = (local or gl).launch(workflow, key)
        with sqlite3.connect(dbpath) as db:
            db.execute('UPDATE launches SET result=? WHERE request_id=?', (json.dumps(result), key))
        return jsonify(result), 201

    @app.get('/api/builds/<int:ident>')
    @authenticated
    def detail(ident):
        p = (local or gl).pipeline(ident)
        jobs = p['jobs'] if local else gl.project_call('GET', f'/pipelines/{ident}/jobs', params={'per_page': 100, 'include_retried': 'true'})
        for job in jobs:
            job['artifacts_local'] = local_manifests(ident, job['id'])
            job['retry_allowed'] = not bool(local)
        return jsonify(pipeline=p, jobs=jobs)

    def local_manifests(pipeline, job):
        values = []
        for platform in ('android', 'ios'):
            folder = artifact_dir(root, pipeline, job, platform)
            if (folder / 'manifest.json').exists():
                m = json.loads((folder / 'manifest.json').read_text())
                for entry in m['files']:
                    entry['download'] = f'/api/artifacts/{pipeline}/{job}/{platform}/' + quote(entry['name'], safe='')
                values.append(m)
        return values

    def pilot_job(job_id):
        job = gl.project_call('GET', f'/jobs/{job_id}')
        gl.pipeline(job['pipeline']['id'])
        return job

    @app.get('/api/jobs/<int:ident>/log')
    @authenticated
    def log(ident):
        if local:
            return app.response_class(local.log(ident), mimetype='text/plain')
        pilot_job(ident)
        trace = gl.project_call('GET', f'/jobs/{ident}/trace', raw=True)
        # Full trace remains available in GitLab; keep browser refreshes bounded.
        return app.response_class(trace[-512_000:], mimetype='text/plain')

    @app.post('/api/builds/<int:ident>/cancel')
    @authenticated
    def cancel(ident):
        if local:
            return jsonify(local.cancel(ident))
        gl.pipeline(ident)
        return jsonify(gl.project_call('POST', f'/pipelines/{ident}/cancel'))

    @app.post('/api/jobs/<int:ident>/retry')
    @authenticated
    def retry(ident):
        if local:
            raise SafeError('Launch a new workflow for a fresh dev commit. Local jobs are never silently retried.')
        check_window(root)
        pilot_job(ident)
        return jsonify(gl.project_call('POST', f'/jobs/{ident}/retry'))

    @app.get('/api/artifacts/<int:pipeline>/<int:job>/<platform>/<name>')
    @authenticated
    def artifact(pipeline, job, platform, name):
        # Local copies stay accessible even if GitLab is temporarily offline.
        folder = artifact_dir(root, pipeline, job, platform)
        manifest = json.loads((folder / 'manifest.json').read_text())
        if manifest['project'] != c['project_path'] or name not in [f['name'] for f in manifest['files']]:
            raise SafeError('Unknown artifact.')
        target = (folder / name).resolve()
        if target.parent != folder.resolve() or target.is_symlink():
            raise SafeError('Invalid artifact path.')
        return send_file(target, as_attachment=True, download_name=name)

    @app.get('/api/local-artifacts')
    @authenticated
    def local_artifacts():
        items = []
        for path in sorted((root / 'data/artifacts').glob('*/*/*/manifest.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:100]:
            m = json.loads(path.read_text())
            if m.get('project') == c['project_path']:
                items.extend(local_manifests(m['pipeline_id'], m['job_id']))
        unique = {(m['pipeline_id'], m['job_id'], m['platform']): m for m in items}
        return jsonify(list(unique.values()))
    return app


def serve():
    from waitress import serve as run
    c = load_config()
    run(create_app(c), host='127.0.0.1', port=c['port'], threads=8, clear_untrusted_proxy_headers=True)
