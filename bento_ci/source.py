"""Source selection is independent of credential storage and build execution."""
import re
import shlex
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .core import SafeError


def defaults(c):
    c.setdefault('source_provider', 'gitlab')
    c.setdefault('build_backend', 'gitlab')
    github = c.setdefault('github', {})
    github.setdefault('repository', 'BentoInc/bento.mobileapp')
    github.setdefault('transport', 'ssh')
    github.setdefault('ssh_key', '')
    gitlab = c.setdefault('gitlab', {})
    gitlab.setdefault('transport', 'https')
    gitlab.setdefault('auth', 'token')
    gitlab.setdefault('ssh_key', '')
    c['secrets'].setdefault('github', 'bento/mac-ci/github')
    return c


def validate(c):
    defaults(c)
    if c['source_provider'] not in ('github', 'gitlab') or c['build_backend'] not in ('local', 'gitlab'):
        raise SafeError('Source must be github or gitlab; build backend must be local or gitlab.')
    if c['source_provider'] == 'github' and c['build_backend'] != 'local':
        raise SafeError('GitHub source requires the local build backend.')
    gh = c['github']
    if not re.fullmatch(r'[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+', gh['repository']) or gh['repository'].split('/')[-1] in ('.', '..'):
        raise SafeError('GitHub repository must be OWNER/REPOSITORY, without a URL or .git suffix.')
    if gh['repository'].endswith('.git') or gh['transport'] not in ('ssh', 'https'):
        raise SafeError('Use a GitHub repository without .git and transport ssh or https.')
    if not isinstance(gh['ssh_key'], str) or any(ch in gh['ssh_key'] for ch in '\r\n\x00'):
        raise SafeError('Invalid GitHub SSH key path.')
    gl = c['gitlab']
    if gl['transport'] not in ('ssh', 'https') or gl['auth'] not in ('existing', 'token'):
        raise SafeError('GitLab transport must be ssh or https; auth must be existing or token.')
    if not isinstance(gl['ssh_key'], str) or any(ch in gl['ssh_key'] for ch in '\r\n\x00'):
        raise SafeError('Invalid GitLab SSH key path.')
    if urlsplit(c['ios']['native_dependencies_url']).netloc != urlsplit(c['gitlab_url']).netloc:
        raise SafeError('GitLab native dependency URL must use the configured GitLab host.')


def provider(c):
    return c.get('source_provider', 'gitlab')


def repository(c):
    return c.get('github', {}).get('repository', 'BentoInc/bento.mobileapp') if provider(c) == 'github' else c['project_path']


def clone_url(c):
    if provider(c) == 'github':
        prefix = 'git@github.com:' if c['github']['transport'] == 'ssh' else 'https://github.com/'
        return prefix + repository(c) + '.git'
    url = c['gitlab_url'] + '/' + c['project_path'] + '.git'
    return gitlab_url(c, url)


def gitlab_url(c, url):
    if c.get('gitlab', {}).get('transport', 'https') == 'ssh':
        parsed = urlsplit(url)
        return 'git@' + parsed.hostname + ':' + parsed.path.lstrip('/')
    return url


def native_url(c):
    return clone_url(c) if provider(c) == 'github' else gitlab_url(c, c['ios']['native_dependencies_url'])


def allowed_origins(c):
    host = 'github.com' if provider(c) == 'github' else urlsplit(c['gitlab_url']).netloc
    base = 'https://github.com' if provider(c) == 'github' else c['gitlab_url']
    path = repository(c)
    return (base + '/' + path, base + '/' + path + '.git',
            'git@' + host + ':' + path + '.git', 'ssh://git@' + host + '/' + path + '.git')


def credential_name(c):
    if provider(c) == 'github' and c['github']['transport'] == 'ssh':
        return None
    if provider(c) == 'gitlab':
        gl = c.get('gitlab', {})
        if gl.get('transport') == 'ssh' or gl.get('auth') == 'existing':
            return None
    return provider(c)


def authenticate(c, store, cmd, temp):
    """Configure a job-only Git environment; never edit global Git or SSH config."""
    name = credential_name(c)
    if name is None:
        settings = c.get(provider(c), {})
        if settings.get('transport', 'https') == 'https':
            # Reuse the work Mac's configured credential helper without copying its secrets.
            cmd.env.update(GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never')
            return
        args = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=15']
        key = settings.get('ssh_key', '')
        if key:
            path = Path(key).expanduser()
            if not path.is_file():
                raise SafeError('Configured source SSH key does not exist on this Mac.')
            args += ['-F', '/dev/null', '-o', 'IdentitiesOnly=yes', '-i', str(path)]
        cmd.env['GIT_SSH_COMMAND'] = shlex.join(args)
        return
    token = store.get(name)['token']
    if any(ch in token for ch in '\r\n\x00'):
        raise SafeError('Invalid source token.')
    cmd.sensitive.add(token)
    urls = [urlsplit(clone_url(c)), urlsplit(native_url(c))]
    # Git supplies the repository path when credential.useHttpPath is true.
    # Scope to both configured repositories; never hand a token to another host/path.
    helper = Path(temp) / 'git-credential-bento.py'
    helper.write_text('''import os,sys
d=dict(line.rstrip('\\n').split('=',1) for line in sys.stdin if '=' in line)
if sys.argv[1]=='get' and d.get('protocol')=='https' and d.get('host')==os.environ['BENTO_GIT_HOST'] and d.get('path','').removesuffix('.git') in os.environ['BENTO_GIT_PATHS'].split('\\n'):
 print('username='+os.environ['BENTO_GIT_USER'])
 print('password='+os.environ['BENTO_GIT_TOKEN'])
''')
    helper.chmod(0o600)
    cmd.env.update(BENTO_GIT_TOKEN=token, BENTO_GIT_HOST=urls[0].netloc,
        BENTO_GIT_PATHS='\n'.join(u.path.lstrip('/').removesuffix('.git') for u in urls),
        BENTO_GIT_USER='x-access-token' if name == 'github' else 'oauth2',
        GIT_CONFIG_COUNT='4', GIT_CONFIG_KEY_0='credential.helper', GIT_CONFIG_VALUE_0='',
        GIT_CONFIG_KEY_1='credential.helper', GIT_CONFIG_VALUE_1='!' + shlex.join([sys.executable, str(helper)]),
        GIT_CONFIG_KEY_2='credential.useHttpPath', GIT_CONFIG_VALUE_2='true',
        GIT_CONFIG_KEY_3='http.followRedirects', GIT_CONFIG_VALUE_3='false')


def check_remote(c, store, cmd, temp, native=True):
    authenticate(c, store, cmd, temp)
    branches = [(clone_url(c), 'dev')]
    if native:
        branches.append((native_url(c), 'auth-26-06-cocoapods'))
    for url, branch in branches:
        output = cmd.run(['git', 'ls-remote', '--exit-code', url, 'refs/heads/' + branch],
                         capture=True, label='Check source branch ' + branch)
        if not re.fullmatch(r'[0-9a-f]{40,64}\s+refs/heads/' + re.escape(branch), output.strip()):
            raise SafeError('Source did not return the expected branch.')
        print(f'{provider(c)} {branch}: {output.split()[0][:12]}')
