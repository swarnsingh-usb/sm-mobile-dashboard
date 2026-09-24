import argparse
import base64
import hashlib
import getpass
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import quote

import requests

from .core import ROOT, GitLab, SafeError, Secrets, atomic_json, digest, load_config
from .locking import bitrise_processes, open_window
from . import source as sources


def ask(label, default=''):
    answer = input(f'{label} [{default}]: ').strip()
    return answer or default


def configure(local=False, source=None):
    path = ROOT / 'config.json'
    if path.exists():
        if local or source:
            c = json.loads(path.read_text())
            if local:
                c['credential_source'] = 'local'
            c.setdefault('local_secrets_file', 'local-secrets.json')
            if source:
                sources.defaults(c)
                c['source_provider'] = source
                c['build_backend'] = 'local'
            atomic_json(path, c)
        print('Existing tool paths and GitLab settings preserved.' + (f' Source selected: {source}.' if source else ''))
        if local:
            print('Credential source set to local; AWS is not used.')
        return load_config()
    c = json.loads((ROOT / 'config.example.json').read_text())
    if not sys.stdin.isatty():
        raise SafeError('Copy config.example.json to config.json and edit it, or run setup interactively.')
    c['credential_source'] = 'local' if local else 'aws'
    sources.defaults(c)
    if source:
        c['source_provider'] = source
        c['build_backend'] = 'local'
    print('Local credential mode: run ./bento credentials after setup.' if local else
          'Configuration contains secret references only. Put secret values in AWS Secrets Manager, not here.')
    c['aws_region'] = ask('AWS region', c['aws_region'])
    if sources.provider(c) == 'gitlab':
        c['gitlab_url'] = ask('GitLab base URL', c['gitlab_url'])
        c['project_path'] = ask('GitLab project path', c['project_path'])
    else:
        c['github']['repository'] = ask('GitHub app repository', c['github']['repository'])
        c['github']['ssh_key'] = ask('GitHub SSH private key path (empty uses your SSH configuration)')
    c['ca_bundle'] = ask('Full PEM CA bundle path, if required by the bank')
    c['toolchain']['android_sdk'] = ask('Existing Android SDK directory', os.environ.get('ANDROID_HOME', c['toolchain']['android_sdk']))
    try:
        java = subprocess.check_output(['/usr/libexec/java_home', '-v', '17'], stderr=subprocess.DEVNULL, text=True).strip()
    except subprocess.CalledProcessError:
        java = ''
    c['toolchain']['java_home'] = ask('Existing JDK 17 home', java)
    c['toolchain']['developer_dir'] = ask('Existing Xcode Developer directory', c['toolchain']['developer_dir'])
    try:
        version = subprocess.check_output(['xcodebuild', '-version'], env={**os.environ, 'DEVELOPER_DIR': c['toolchain']['developer_dir']}, text=True, stderr=subprocess.DEVNULL).splitlines()[0].removeprefix('Xcode ')
    except Exception:
        version = ''
    c['toolchain']['expected_xcode'] = ask('Qualified existing Xcode version', version)
    pod = shutil.which('pod') or ''
    c['toolchain']['pod_bin'] = ask('Existing qualified CocoaPods executable', pod)
    c['toolchain']['ruby_bin'] = ask('Directory containing qualified Ruby (optional)', '')
    c['toolchain']['expected_pod'] = ask('Qualified CocoaPods version', c['toolchain']['expected_pod'])
    c['ios']['native_dependencies_url'] = c['gitlab_url'] + '/' + c['project_path'] + '.git'
    if not local:
        names = ['portal', 'dependencies', 'ios']
        if sources.credential_name(c): names.append(sources.credential_name(c))
        if c['build_backend'] == 'gitlab': names.append('runner')
        for name in names:
            c['secrets'][name] = ask(f'Secrets Manager name/ARN for {name}', c['secrets'][name])
        c['secrets']['android'] = ask('Optional Android signing secret; empty preserves repository dev signing')
    c['artifactory']['url'] = ask('Optional artifact storage URL, ending /artifactory; empty means Mac disk')
    if c['artifactory']['url']:
        c['artifactory']['repository'] = ask('Artifactory generic local repository key')
        c['secrets']['artifactory'] = 'artifactory' if local else ask('Artifactory upload token secret', 'bento/mac-ci/artifactory')
    atomic_json(path, c)
    return load_config()


def select_source(name, backend=None, ssh_key=None, transport=None):
    if name not in ('github', 'gitlab'):
        raise SafeError('Choose ./bento source github or ./bento source gitlab.')
    path = ROOT / 'config.json'
    c = load_config()
    c.pop('_config_dir', None)
    c['source_provider'] = name
    c['build_backend'] = backend or ('local' if name == 'github' else c['build_backend'])
    if ssh_key is not None: c['github']['ssh_key'] = str(Path(ssh_key).expanduser()) if ssh_key else ''
    if transport is not None: c['github']['transport'] = transport
    sources.validate(c)
    atomic_json(path, c)
    print(f'Source: {sources.clone_url(c)}; build backend: {c["build_backend"]}.')
    print('GitLab settings and all credentials retained. Restart the idle dashboard/worker to apply this choice.')


def check_source(c):
    from .build import Commands, tool_env
    with tempfile.TemporaryDirectory(dir=ROOT / 'runtime') as tmp:
        sources.check_remote(c, Secrets(c), Commands(tool_env(c), monitor_bitrise=False), Path(tmp))


def portal_credentials(c):
    if c.get('credential_source') != 'local' or not sys.stdin.isatty():
        raise SafeError('Use ./bento portal-credentials in an interactive terminal with local credential mode.')
    path = Secrets(c).local_path()
    if path.exists() and (path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077):
        raise SafeError('Local credential file must be owned by this user, chmod 600, and not a symlink.')
    values = json.loads(path.read_text()) if path.exists() else {}
    old = values.get('portal', {})
    user = ask('Dashboard username', old.get('username', 'engineer'))
    password = getpass.getpass('Dashboard password (at least 24 characters; Enter keeps existing): ') or old.get('password', '')
    if not user or len(password) < 24:
        raise SafeError('Choose a username and a password of at least 24 characters.')
    import secrets
    session_key = old.get('session_key', '')
    values['portal'] = dict(username=user, password=password,
                           session_key=session_key if len(session_key) >= 32 else secrets.token_urlsafe(48))
    atomic_json(path, values)
    print('Dashboard login saved privately. Restart the dashboard if it is running.')


def credentials(c):
    if c.get('credential_source') != 'local':
        raise SafeError('First select local mode with ./bento configure --local.')
    if not sys.stdin.isatty():
        raise SafeError('Run ./bento credentials in an interactive SSH terminal.')
    store = Secrets(c)
    path = store.local_path()
    if path.exists():
        # Preserve any optional portal, runner, Android and upload credentials.
        if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
            raise SafeError('Local credential file must be owned by this user, chmod 600, and not a symlink.')
        values = json.loads(path.read_text())
    else:
        values = {}
    print('Credentials stay in a private local JSON file. Hidden prompts accept Enter to keep an existing value.')
    sections = [('dependencies', ('npm_token', 'maven_username', 'maven_password'))]
    source_credential = sources.credential_name(c)
    if source_credential: sections.append((source_credential, ('token',)))
    for name, fields in sections:
        value = values.setdefault(name, {})
        for field in fields:
            value[field] = getpass.getpass(f'{name}.{field}: ') or value.get(field, '')
    ios = values.setdefault('ios', {})
    ios['p12_file'] = ask('Signing certificate/private key .p12 path', ios.get('p12_file', ''))
    ios['p12_password'] = getpass.getpass('P12 password: ') or ios.get('p12_password', '')
    ios['profile_files'] = [p.strip() for p in ask('Provisioning profile paths (comma-separated)', ','.join(ios.get('profile_files', []))).split(',') if p.strip()]
    ios['team_id'] = ask('Apple team ID', ios.get('team_id', ''))
    ios['signing_identity'] = ask('Exact existing signing certificate identity', ios.get('signing_identity', ''))
    exports_path = ask('Existing approved ExportOptions.plist path (Enter keeps existing options)')
    if exports_path:
        ios['export_options'] = plistlib.loads(Path(exports_path).expanduser().read_bytes())
    elif not ios.get('export_options'):
        method = ask('Export method supported by your Xcode and profiles (e.g. release-testing)')
        profile = ask('Exact profile name or UUID for ' + c['ios']['bundle_id'])
        ios['export_options'] = {'method': method, 'destination': 'export', 'teamID': ios['team_id'],
            'signingStyle': 'manual', 'provisioningProfiles': {c['ios']['bundle_id']: profile}}
    atomic_json(path, values)
    for name in ['dependencies', 'ios'] + ([source_credential] if source_credential else []):
        store.get(name, fresh=True)
    print('Local build credentials saved with mode 0600. No AWS service was contacted. See docs/LOCAL-TEST.md.')


def download(url, target):
    target = Path(target)
    with requests.get(url, timeout=(15, 180), stream=True) as r:
        r.raise_for_status()
        with target.open('wb') as f:
            for chunk in r.iter_content(1024 * 1024):
                f.write(chunk)


def install_tools():
    if platform.system() != 'Darwin':
        raise SafeError('Tool installation is for macOS only.')
    if (ROOT / 'config.json').exists():
        ca = load_config()['ca_bundle']
        if ca:
            os.environ['REQUESTS_CA_BUNDLE'] = ca
    arch = 'arm64' if platform.machine() == 'arm64' else 'x64'
    tools = ROOT / 'tools'
    tools.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=tools) as work:
        temp = Path(work)
        if not (tools / 'node/bin/node').exists():
            name = f'node-v20.19.4-darwin-{arch}.tar.gz'
            download('https://nodejs.org/dist/v20.19.4/' + name, temp / name)
            download('https://nodejs.org/dist/v20.19.4/SHASUMS256.txt', temp / 'SHA256SUMS')
            expected = next(line.split()[0] for line in (temp / 'SHA256SUMS').read_text().splitlines() if line.split()[-1] == name)
            if digest(temp / name) != expected:
                raise SafeError('Node download checksum failed.')
            subprocess.run(['/usr/bin/tar', '-xzf', str(temp / name), '-C', str(temp)], check=True)
            shutil.move(temp / name.removesuffix('.tar.gz'), tools / 'node')
        if not (tools / 'yarn/bin/yarn').exists():
            r = requests.get('https://registry.npmjs.org/yarn/1.22.22', timeout=30)
            r.raise_for_status()
            dist = r.json()['dist']
            download('https://registry.npmjs.org/yarn/-/yarn-1.22.22.tgz', temp / 'yarn.tgz')
            actual = 'sha512-' + base64.b64encode(hashlib.sha512((temp / 'yarn.tgz').read_bytes()).digest()).decode()
            if actual != dist['integrity']:
                raise SafeError('Yarn download integrity check failed.')
            subprocess.run(['/usr/bin/tar', '-xzf', str(temp / 'yarn.tgz'), '-C', str(temp)], check=True)
            shutil.move(temp / 'package', tools / 'yarn')
        need_runner = not (ROOT / 'config.json').exists() or load_config()['build_backend'] == 'gitlab'
        if need_runner and not (tools / 'gitlab-runner').exists():
            version = 'v19.4.1'
            name = 'gitlab-runner-darwin-' + ('arm64' if arch == 'arm64' else 'amd64')
            base = 'https://gitlab-runner-downloads.s3.amazonaws.com/' + version
            download(base + '/binaries/' + name, temp / name)
            download(base + '/release.sha256', temp / 'release.sha256')
            expected = next(line.split()[0] for line in (temp / 'release.sha256').read_text().splitlines() if line.split()[-1] == 'binaries/' + name)
            if digest(temp / name) != expected:
                raise SafeError('GitLab runner download checksum failed.')
            shutil.move(temp / name, tools / 'gitlab-runner')
            (tools / 'gitlab-runner').chmod(0o700)
    print('Private Node 20.19.4 and Yarn 1.22.22 ready.' + (' GitLab Runner 19.4.1 ready.' if need_runner else ' Local backend needs no GitLab Runner.'))


def generate(c):
    import shlex
    command = shlex.quote(str(ROOT / 'bento'))
    text = '''# Merge this configuration into the app repository; do not overwrite existing pipelines.
# First milestone: explicit manual/API launches on protected dev only.
workflow:
  name: 'Bento Mac / $BENTO_WORKFLOW'
  rules:
    - if: '$CI_COMMIT_BRANCH == "dev" && $CI_COMMIT_REF_PROTECTED == "true" && $BENTO_PORTAL == "1" && ($CI_PIPELINE_SOURCE == "api" || $CI_PIPELINE_SOURCE == "web")'
    - when: never

stages: [build]
variables:
  BENTO_WORKFLOW: 'both-dev'
  GIT_STRATEGY: clone
  GIT_DEPTH: '20'
  GIT_SUBMODULE_STRATEGY: none
  FF_ENABLE_JOB_CLEANUP: 'true'

'''
    for name in ('android', 'ios', 'validate'):
        condition = f'$BENTO_WORKFLOW == "{name}-dev"'
        if name != 'validate':
            condition += ' || $BENTO_WORKFLOW == "both-dev"'
        text += f'''{name}-dev:
  stage: build
  tags: [{c['runner_tag']}]
  resource_group: bento-mac-pilot
  timeout: 3h
  interruptible: false
  script:
    - {json.dumps(command + ' build ' + name)}
  rules:
    - if: '{condition}'

'''
    directory = ROOT / 'generated'
    directory.mkdir(exist_ok=True)
    (directory / 'gitlab-ci.yml').write_text(text)
    refs = [v for v in c['secrets'].values() if v]
    # Exact ARNs cannot be fabricated; names receive a documented account-ID placeholder.
    resources = [r if r.startswith('arn:') else f"arn:aws:secretsmanager:{c['aws_region']}:ACCOUNT_ID:secret:{r}-??????" for r in refs]
    atomic_json(directory / 'instance-role-policy.json', {'Version': '2012-10-17', 'Statement': [
        {'Effect': 'Allow', 'Action': ['secretsmanager:GetSecretValue'], 'Resource': resources}]})
    print('Generated pipeline and least-privilege secret-read policy in generated/.')


def register(c):
    config = ROOT / 'runtime/runner.toml'
    if config.exists():
        if '[[runners]]' not in config.read_text():
            raise SafeError('Existing runner config is incomplete. Inspect and move it aside before registering; it was not overwritten.')
        print('Existing private runner configuration preserved (including token rotation).')
        return
    s = Secrets(c).get('runner')
    token = s.get('token', '')
    if not token.startswith('glrt-'):
        raise SafeError('Runner secret requires a modern glrt- runner authentication token.')
    env = os.environ.copy()
    env['CI_SERVER_TOKEN'] = token
    pending = ROOT / 'runtime/runner.pending.toml'
    if pending.exists():
        raise SafeError('A pending runner registration exists. Inspect it before retrying.')
    args = [str(ROOT / 'tools/gitlab-runner'), 'register', '--non-interactive', '--config', str(pending),
        '--url', c['gitlab_url'], '--executor', 'shell', '--shell', 'bash', '--name', 'bento-mac-pilot',
        '--builds-dir', str(ROOT / 'data/checkouts'), '--cache-dir', str(ROOT / 'data/runner-cache')]
    if c['ca_bundle']:
        args += ['--tls-ca-file', c['ca_bundle']]
    result = subprocess.run(args, env=env, capture_output=True, text=True)
    if result.returncode:
        pending.unlink(missing_ok=True)
        raise SafeError('Runner registration failed. Check the authentication token, GitLab trust, and network. Raw output withheld to protect the token.')
    content = pending.read_text()
    content = re.sub(r'^concurrent\s*=.*$', 'concurrent = 1', content, flags=re.M)
    content = content.replace('[[runners]]', '[[runners]]\n  limit = 1\n  request_concurrency = 1', 1)
    pending.write_text(content)
    pending.chmod(0o600)
    pending.replace(config)
    print('Runner registered in runtime/runner.toml. Existing GitLab/Bitrise services were not modified.')


def runner():
    c = load_config()
    register(c)
    from .build import tool_env
    os.execve(str(ROOT / 'tools/gitlab-runner'), [str(ROOT / 'tools/gitlab-runner'), 'run',
        '--config', str(ROOT / 'runtime/runner.toml'), '--working-directory', str(ROOT / 'data/runner')], tool_env(c))


def service(action):
    uid = os.getuid()
    domain = f'gui/{uid}'
    if subprocess.run(['launchctl', 'print', domain], capture_output=True).returncode:
        raise SafeError('No graphical user session. Sign in through the Mac desktop and run ./bento services start there; GitLab Runner uses LaunchAgents.')
    suffix = hashlib.sha256(str(ROOT).encode()).hexdigest()[:8]
    directory = Path.home() / 'Library/LaunchAgents'
    directory.mkdir(parents=True, exist_ok=True)
    for kind in ('portal', 'runner'):
        label = f'com.bento.mac-ci.{suffix}.{kind}'
        path = directory / f'{label}.plist'
        if action == 'stop':
            subprocess.run(['launchctl', 'bootout', domain + '/' + label], capture_output=True)
            continue
        if action == 'status':
            r = subprocess.run(['launchctl', 'print', domain + '/' + label], capture_output=True)
            print(f'{kind}: ' + ('loaded' if r.returncode == 0 else 'not loaded'))
            continue
        payload = {'Label': label, 'ProgramArguments': [str(ROOT / 'bento'), 'serve' if kind == 'portal' else 'runner'],
            'WorkingDirectory': str(ROOT), 'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 30,
            'ProcessType': 'Background', 'EnvironmentVariables': {'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'},
            'StandardOutPath': str(ROOT / f'data/{kind}.log'), 'StandardErrorPath': str(ROOT / f'data/{kind}.error.log')}
        path.write_bytes(plistlib.dumps(payload))
        path.chmod(0o600)
        subprocess.run(['launchctl', 'bootout', domain + '/' + label], capture_output=True)
        result = subprocess.run(['launchctl', 'bootstrap', domain, str(path)], capture_output=True)
        if result.returncode:
            raise SafeError(f'Could not load {kind} LaunchAgent. Check the logged-in GUI session.')
    print('Only this installation’s Bento services were affected. Bitrise was not changed.')


def doctor(c, online=False, build_only=False):
    from .build import tool_env
    failures = []
    env = tool_env(c)
    if env.get('JAVA_HOME'):
        env['PATH'] = str(Path(env['JAVA_HOME']) / 'bin') + os.pathsep + env['PATH']
    checks = [('Node', ['node', '--version']), ('Yarn', ['yarn', '--version']), ('Java', ['java', '-version']),
        ('Xcode', ['xcodebuild', '-version']), ('Ruby', ['ruby', '--version']),
        ('CocoaPods', [c['toolchain']['pod_bin'] or 'pod', '--version'])]
    for name, command in checks:
        try:
            r = subprocess.run(command, env=env, capture_output=True, text=True, timeout=45)
            text = (r.stdout or r.stderr).strip().splitlines()
            print(f'{name}: ' + (text[0] if text else 'no output'))
            if r.returncode:
                failures.append(name)
            elif name == 'Node' and text[0] != 'v20.19.4': failures.append('Node version')
            elif name == 'Yarn' and text[0] != '1.22.22': failures.append('Yarn version')
            elif name == 'Java' and not re.search(r'version "17[.\"]', text[0]): failures.append('Java 17 required')
            elif name == 'Xcode' and (not c['toolchain']['expected_xcode'] or text[0] != 'Xcode ' + c['toolchain']['expected_xcode']): failures.append('qualified Xcode version')
            elif name == 'CocoaPods' and text[0] != c['toolchain']['expected_pod']: failures.append('CocoaPods version')
            elif name == 'Ruby':
                match = re.search(r'ruby (\d+)\.(\d+)\.(\d+)', text[0])
                if not match or tuple(map(int,match.groups())) < (3,2,1): failures.append('Ruby >= 3.2.1 required')
        except (OSError, subprocess.TimeoutExpired):
            failures.append(name)
            print(f'{name}: missing or timed out')
    for part in ('platforms/android-36/android.jar', 'build-tools/36.0.0/aapt', 'ndk/27.0.12077973/source.properties'):
        ok = (Path(c['toolchain']['android_sdk']) / part).exists()
        print(f'Android {part}: ' + ('present' if ok else 'MISSING'))
        if not ok:
            failures.append(part)
    free = shutil.disk_usage(ROOT).free / 1024 ** 3
    print(f'Disk free: {free:.1f} GiB (minimum {c["minimum_free_gb"]})')
    if free < c['minimum_free_gb']:
        failures.append('disk capacity')
    print('Bitrise: ' + ('running; pilot builds remain blocked until drained/stopped' if bitrise_processes() else 'no recognized agent/CLI process detected'))
    if (ROOT / 'data/signing-recovery.json').exists():
        failures.append('signing recovery')
    if online:
        store = Secrets(c)
        names = ['dependencies', 'ios']
        if sources.credential_name(c): names.append(sources.credential_name(c))
        if not build_only: names.append('portal')
        if not build_only and c.get('build_backend', 'gitlab') == 'gitlab': names.append('runner')
        for name in names:
            try:
                store.get(name, fresh=True)
                print(f'Secret {name}: readable (values withheld)')
            except SafeError:
                failures.append('secret ' + name)
                print(f'Secret {name}: unavailable')
        try:
            if c.get('build_backend', 'gitlab') == 'local' or build_only:
                check_source(c)
            else:
                gl = GitLab(c, store)
                b = gl.project_call('GET', '/repository/branches/dev')
                print('GitLab dev: ' + b['commit']['id'][:12])
                if not build_only and not b.get('protected'):
                    failures.append('dev is not protected')
                gl.project_call('GET', '/repository/branches/auth-26-06-cocoapods')
                print('Native dependency branch: present')
        except SafeError as e:
            failures.append('source access/dependency branch')
            print(str(e))
    print('Artifactory: ' + ('configured; actual write/checksum verified during build' if c['artifactory']['url'] else 'disabled; artifacts retained on Mac disk'))
    print('Preflight result: ' + ('BLOCKED: ' + ', '.join(failures) if failures else 'Local checks passed; native builds still require live qualification.'))
    return bool(failures)


def recover():
    if bitrise_processes():
        raise SafeError('Drain and stop Bitrise before recovering shared keychain state.')
    from .locking import host_lock
    with host_lock(ROOT):
        p = ROOT / 'data/signing-recovery.json'
        if not p.exists():
            print('No pending signing recovery.')
            return
        s = json.loads(p.read_text())
        subprocess.run(['security', 'list-keychains', '-d', 'user', '-s', *s['keychains']], check=True, capture_output=True)
        subprocess.run(['security', 'delete-keychain', s['temporary_keychain']], capture_output=True)
        for profile in s['profiles']:
            Path(profile).unlink(missing_ok=True)
        p.unlink()
        print('Previous keychain search list restored; temporary profiles removed.')


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description='Bento Mac CI')
    p.add_argument('command', choices=['setup','configure','source','check-source','portal-credentials','run','worker','credentials','test-build','tools','generate','register','services','doctor','serve','runner','build','open-window','close-window','recover-signing','install-pipeline'])
    p.add_argument('argument', nargs='?')
    p.add_argument('--online', action='store_true')
    p.add_argument('--no-start', action='store_true')
    p.add_argument('--local', action='store_true', help='Read credentials locally instead of AWS; setup leaves services stopped')
    p.add_argument('--source', choices=['github', 'gitlab'])
    p.add_argument('--backend', choices=['local', 'gitlab'])
    p.add_argument('--transport', choices=['ssh', 'https'])
    p.add_argument('--ssh-key', help='Optional GitHub private key path; never the key contents')
    p.add_argument('--once', action='store_true', help='Worker drains current pending runs and exits')
    p.add_argument('--repo', help='Separate configured app checkout containing the local dev branch')
    p.add_argument('--build-only', action='store_true', help='Skip portal/runner credential and protected-branch checks in doctor')
    p.add_argument('--minutes', type=int, default=120)
    args = p.parse_args()
    for name in ('data', 'runtime', 'generated', 'data/runner'):
        (ROOT / name).mkdir(parents=True, exist_ok=True)
    try:
        if args.command == 'setup':
            c = configure(local=args.local, **({'source': args.source} if args.source else {}))
            install_tools()
            generate(c)
            blocked = doctor(c)
            if not args.no_start and not args.local and c.get('build_backend', 'gitlab') == 'gitlab':
                try:
                    register(c)
                    service('start')
                except SafeError as e:
                    print('Service startup pending: ' + str(e))
                    blocked = True
            if c.get('build_backend', 'gitlab') == 'local':
                print('Package installed. Follow docs/GITHUB.md for source checks, credentials, ./bento run both and the dashboard worker.')
            else:
                print('Package installed. ' + ('Run ./bento credentials, then follow docs/LOCAL-TEST.md for SSH builds.' if args.local else
                    'Read README.md and docs/SECRETS.md. Add generated/gitlab-ci.yml to GitLab dev before a dashboard run.'))
            if blocked:
                print('Native build prerequisites are missing; resolve the items above and run ./bento doctor.')
                return 2
        elif args.command == 'configure': configure(local=args.local, source=args.source)
        elif args.command == 'source': select_source(args.argument, args.backend, args.ssh_key, args.transport)
        elif args.command == 'check-source': check_source(load_config())
        elif args.command == 'portal-credentials': portal_credentials(load_config())
        elif args.command in ('run', 'worker'):
            from .local_runner import worker
            workflow = (args.argument or 'both').removesuffix('-dev') + '-dev' if args.command == 'run' else None
            worker(once=args.once, workflow=workflow)
        elif args.command == 'credentials': credentials(load_config())
        elif args.command == 'test-build':
            from .build import local_build
            local_build(args.argument or 'both', args.repo)
        elif args.command == 'tools': install_tools()
        elif args.command == 'generate': generate(load_config())
        elif args.command == 'register': register(load_config())
        elif args.command == 'services': service(args.argument or 'status')
        elif args.command == 'doctor': return 2 if doctor(load_config(), args.online, args.build_only) else 0
        elif args.command == 'serve':
            from .server import serve
            serve()
        elif args.command == 'runner': runner()
        elif args.command == 'build':
            from .build import build
            build(args.argument)
        elif args.command == 'open-window':
            open_window(ROOT, args.minutes)
            print(f'Pilot window opened for {args.minutes} minutes. Keep Bitrise stopped until all new jobs finish; ./bento close-window prevents further starts.')
        elif args.command == 'close-window':
            (ROOT / 'data/pilot-window.json').unlink(missing_ok=True)
            print('New starts disabled. Running jobs continue; wait for them to finish before restarting Bitrise.')
        elif args.command == 'recover-signing': recover()
        elif args.command == 'install-pipeline':
            generate(load_config())
            if not args.argument:
                raise SafeError('Supply the path to a separate GitLab app checkout.')
            destination = Path(args.argument).resolve() / '.gitlab-ci.yml'
            if destination.exists():
                raise SafeError('Existing .gitlab-ci.yml preserved. Merge generated/gitlab-ci.yml manually; see docs/GITLAB.md.')
            shutil.copyfile(ROOT / 'generated/gitlab-ci.yml', destination)
            print('Pipeline copied. Review, commit, and push it through the bank’s normal GitLab process.')
    except KeyboardInterrupt:
        print('Canceled; temporary build credentials are being cleaned up.', file=sys.stderr)
        return 130
    except SafeError as e:
        print(str(e), file=sys.stderr)
        return 1
    except Exception as e:
        print(f'Operation failed ({type(e).__name__}). Check configuration and connectivity; secret-bearing exception details withheld.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
