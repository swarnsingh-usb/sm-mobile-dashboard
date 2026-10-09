import base64
import contextlib
import datetime
import json
import os
import plistlib
import re
import secrets as random_secrets
import shutil
import signal
import sys
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests

from .core import ROOT, SafeError, Secrets, artifact_dir, atomic_json, digest, load_config
from .locking import bitrise_processes, host_lock
from . import source as sources


class Commands:
    def __init__(self, env, sensitive=(), canceled=None, monitor_bitrise=True):
        self.env = env
        self.sensitive = set(str(s) for s in sensitive if s)
        self.process = None
        self.canceled = canceled or (lambda: False)
        self.monitor_bitrise = monitor_bitrise

    def redact(self, text):
        for value in sorted(self.sensitive, key=len, reverse=True):
            for form in (value, quote(value, safe='')):
                text = text.replace(form, '[REDACTED]')
        return re.sub(r'(https?://)[^\s/@]+:[^\s/@]+@', r'\1[REDACTED]@', text)

    def run(self, args, cwd=None, capture=False, label=None):
        if self.canceled():
            raise KeyboardInterrupt()
        print('\n→ ' + (label or Path(str(args[0])).name), flush=True)
        self.process = subprocess.Popen([str(a) for a in args], cwd=cwd, env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors='replace',
            start_new_session=True)
        p = self.process
        out = []
        collision = threading.Event()
        canceled = threading.Event()
        done = threading.Event()

        def monitor():
            while not done.wait(2):
                try:
                    running = self.monitor_bitrise and bool(bitrise_processes())
                except Exception:
                    running = True
                if running or self.canceled():
                    (collision if running else canceled).set()
                    try:
                        os.killpg(p.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    if not done.wait(10):
                        try:
                            os.killpg(p.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    return
        watch = threading.Thread(target=monitor, daemon=True)
        watch.start()
        try:
            for line in p.stdout:
                if capture:
                    out.append(line)
                else:
                    print(self.redact(line), end='', flush=True)
            rc = p.wait()
        except BaseException:
            try:
                os.killpg(p.pid, signal.SIGTERM)
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
            except ProcessLookupError:
                pass
            raise
        finally:
            done.set()
            watch.join(timeout=3)
            self.process = None
        if canceled.is_set():
            raise KeyboardInterrupt()
        if collision.is_set():
            raise SafeError('Bitrise restarted during the pilot; the new build was stopped. Re-establish an exclusive window.')
        if rc:
            # Captured output may contain signing secrets; never echo it on errors.
            raise SafeError(f'{label or Path(str(args[0])).name} failed with exit code {rc}.')
        return ''.join(out)


def tool_env(c, root=ROOT, native=True):
    env = os.environ.copy()
    # Never inherit Bitrise environment selection, trace mode, or TLS bypasses.
    for key in list(env):
        if key.startswith(('BITRISE', 'AWS_ACCESS_KEY', 'AWS_SECRET_ACCESS', 'AWS_SESSION_TOKEN')) or key in (
                'NODE_TLS_REJECT_UNAUTHORIZED', 'NPM_CONFIG_STRICT_SSL', 'YARN_STRICT_SSL',
                'GIT_SSL_NO_VERIFY', 'COREPACK_INTEGRITY_KEYS', 'USE_PUBLIC_REPOS',
                'JAVA_TOOL_OPTIONS', 'JDK_JAVA_OPTIONS', '_JAVA_OPTIONS', 'JAVA_OPTS', 'GRADLE_OPTS'):
            env.pop(key, None)
    t = c['toolchain']
    paths = [str(Path(root) / 'tools/node/bin'), str(Path(root) / 'tools/yarn/bin')]
    if t['ruby_bin']:
        paths.append(t['ruby_bin'])
    paths += ['/opt/homebrew/bin', '/usr/local/bin', env.get('PATH', '/usr/bin:/bin')]
    env.update(PATH=os.pathsep.join(paths), CI='true', AWS_REGION=c['aws_region'],
        NODE_OPTIONS='--max-old-space-size=8192',
        GIT_TERMINAL_PROMPT='0', npm_config_strict_ssl='true')
    if native:
        env.update(ANDROID_HOME=t['android_sdk'], ANDROID_SDK_ROOT=t['android_sdk'],
                   DEVELOPER_DIR=t['developer_dir'])
    if native and t['java_home']:
        env['JAVA_HOME'] = t['java_home']
    if t['ruby_bin']:
        ruby_home = Path(t['ruby_bin']).resolve().parent
        if ruby_home.parent.name == 'rubies':
            rvm_root = ruby_home.parent.parent
            gem_home = rvm_root / 'gems' / ruby_home.name
            if gem_home.is_dir():
                env.update(GEM_HOME=str(gem_home), GEM_PATH=os.pathsep.join((str(gem_home), str(gem_home) + '@global')),
                           MY_RUBY_HOME=str(ruby_home))
    if c['ca_bundle']:
        for key in ('NODE_EXTRA_CA_CERTS', 'SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE', 'GIT_SSL_CAINFO'):
            env[key] = c['ca_bundle']
    return env


def private_file(path, data):
    path = Path(path)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    path.chmod(0o600)
    return path


def configure_maven_downloads(c, dependencies, cmd, temp):
    repository = c['maven_repository']
    host = urlsplit(repository).hostname
    username = dependencies['maven_username']
    password = dependencies['npm_token']
    if any(re.search(r'\s', value) for value in (username, password)):
        raise SafeError('Maven credentials cannot contain whitespace.')
    curl_home = Path(temp) / 'curl'
    curl_home.mkdir(mode=0o700)
    netrc = private_file(curl_home / 'netrc', f'machine {host}\nlogin {username}\npassword {password}\n')
    private_file(curl_home / '.curlrc', f'netrc-file = "{netrc}"\n')
    cmd.env.update(ENTERPRISE_REPOSITORY=repository, CURL_HOME=str(curl_home))
    gradle_home = Path(cmd.env['GRADLE_USER_HOME'])
    gradle_home.mkdir(mode=0o700)
    private_file(gradle_home / 'init.gradle', '''
def enterpriseRepository = System.getenv('ENTERPRISE_REPOSITORY')
def enterpriseUsername = System.getenv('ORG_GRADLE_PROJECT_artifactory_username')
def enterprisePassword = System.getenv('ORG_GRADLE_PROJECT_artifactory_password')
def localMavenRepository = System.getenv('LOCAL_MAVEN_REPOSITORY')
def addLocalMavenRepository = { repositories ->
    if (localMavenRepository) {
        repositories.maven {
            url = localMavenRepository
            metadataSources {
                mavenPom()
                artifact()
            }
            content {
                includeGroup('com.github.yalantis')
                includeGroup('com.github.zacharee')
                includeGroup('com.github.ybq')
            }
        }
    }
}
def addEnterpriseRepository = { repositories ->
    repositories.maven {
        url = enterpriseRepository
        credentials {
            username = enterpriseUsername
            password = enterprisePassword
        }
    }
}
beforeSettings { settings ->
    addLocalMavenRepository(settings.pluginManagement.repositories)
    addEnterpriseRepository(settings.pluginManagement.repositories)
    addLocalMavenRepository(settings.dependencyResolutionManagement.repositories)
    addEnterpriseRepository(settings.dependencyResolutionManagement.repositories)
}
allprojects {
    addLocalMavenRepository(buildscript.repositories)
    addEnterpriseRepository(buildscript.repositories)
    addLocalMavenRepository(repositories)
    addEnterpriseRepository(repositories)
}
'''.lstrip())


def accept_hermes_checksum(before, after):
    pattern = r'(?m)^  hermes-engine: ([0-9a-f]{40})$'
    old = re.findall(pattern, before)
    new = re.findall(pattern, after)
    if len(old) != 1 or len(new) != 1 or re.sub(pattern, f'  hermes-engine: {new[0]}', before) != after:
        raise SafeError('CocoaPods changed more than the expected Hermes mirror checksum.')


def prepare(c, secret_store, cmd, repo, temp, native=True, environment='dev'):
    fields = ('npm_token', 'maven_username', 'maven_password') if native else ('npm_token',)
    dependencies = secret_store.get('dependencies', fresh=True, required_fields=fields)
    for key in fields:
        if not dependencies.get(key) or '\n' in dependencies[key] or '\r' in dependencies[key]:
            raise SafeError(f'Missing or invalid dependency secret field: {key}')
    cmd.sensitive.update(dependencies[k] for k in fields)
    npm = ['always-auth=true', 'strict-ssl=true']
    for registry in c['npm_registries']:
        p = urlsplit(registry)
        npm.append(f'//{p.netloc}{p.path.rstrip("/")}/:_authToken={dependencies["npm_token"]}')
    if c['ca_bundle']:
        npm.append('cafile=' + c['ca_bundle'])
    cmd.env.update(NPM_CONFIG_USERCONFIG=str(private_file(temp / 'npmrc', '\n'.join(npm) + '\n')),
        GRADLE_USER_HOME=str(temp / 'gradle'), YARN_CACHE_FOLDER=str(ROOT / 'data/cache/yarn'))
    if native:
        cmd.env.update(ORG_GRADLE_PROJECT_artifactory_username=dependencies['maven_username'],
                       ORG_GRADLE_PROJECT_artifactory_password=dependencies['maven_password'])
        configure_maven_downloads(c, dependencies, cmd, temp)
    node = cmd.run(['node', '--version'], capture=True).strip()
    yarn = cmd.run(['yarn', '--version'], capture=True).strip()
    if node != 'v20.19.4' or yarn != '1.22.22' or (repo / '.nvmrc').read_text().strip() != '20.19.4':
        raise SafeError('Node/Yarn baseline changed or is missing. Expected Node 20.19.4 and Yarn 1.22.22.')
    cmd.env['NODE_BINARY'] = shutil.which('node', path=cmd.env['PATH'])
    lock_before = digest(repo / 'yarn.lock')
    cmd.run(['yarn', 'install', '--frozen-lockfile', '--non-interactive'], repo, label='Install locked dependencies')
    # Preserve the repository patch sequence but make failures stop the job.
    for script in ('install-android-fixes.sh', 'install-ios-fixes.sh', 'install-patches.sh', 'patch-vulnerability-fix.sh'):
        cmd.run(['/bin/bash', '-e', str(repo / 'scripts' / script)], repo, label=script)
    if digest(repo / 'yarn.lock') != lock_before:
        raise SafeError('Dependency installation changed yarn.lock.')
    if not re.fullmatch(r'[a-z][a-z0-9-]*', environment):
        raise SafeError('Invalid build environment.')
    expected_environment = repo / 'environments' / environment / 'usbank.env'
    if not expected_environment.is_file():
        raise SafeError(f'Missing usbank / {environment} environment configuration.')
    cmd.run(['/bin/bash', '-e', 'scripts/prebuild.sh', '-b', 'usbank', '-e', environment], repo,
            label=f'Select usbank / {environment} environment')
    if (repo / '.env').read_bytes() != expected_environment.read_bytes():
        raise SafeError(f'Environment output differs from the {environment} configuration.')
    # Protect against xcode shell phases that otherwise source a stale developer-local Node path.
    private_file(repo / 'ios/.xcode.env.local', 'export NODE_BINARY=' + __import__('shlex').quote(cmd.env['NODE_BINARY']) + '\n')
    return {'node': node, 'yarn': yarn}


def android_configuration(c, variant='stage'):
    config = dict(c['android'])
    variants = config.pop('variants', {})
    selected = variants.get(variant)
    if not isinstance(selected, dict):
        raise SafeError('Unknown Android build variant.')
    config.update(selected)
    if any(not isinstance(config.get(key), str) or not config[key]
           for key in ('task', 'package_id', 'environment', 'apk_path', 'mapping_path', 'label')):
        raise SafeError('Android build configuration is incomplete.')
    if not isinstance(config.get('webview_login'), str):
        raise SafeError('Android webview login configuration is invalid.')
    if not re.fullmatch(r':app:assemble[A-Za-z0-9]+', config['task']):
        raise SafeError('Invalid Android Gradle task.')
    for key in ('apk_path', 'mapping_path'):
        path = Path(config[key])
        if path.is_absolute() or '..' in path.parts:
            raise SafeError('Invalid Android artifact path.')
    return config


def configure_android_environment(secret_store, cmd, repo, android_config):
    pins = secret_store.get('android_environment', fresh=True,
                            required_fields=('ssl_certificate', 'ssl_certificate_backup'))
    for value in pins.values():
        if not isinstance(value, str) or not value or any(character in value for character in ('\n', '\r', '\0')):
            raise SafeError('Android environment credentials contain an invalid certificate value.')
        cmd.sensitive.add(value)
    overrides = {
        'ENABLE_SSL_PINNING': 'true',
        'ENABLE_JAILBREAK_ROOT_DETECTION': 'true',
        'SSL_CERTIFICATE': pins['ssl_certificate'],
        'SSL_CERTIFICATE_BACKUP': pins['ssl_certificate_backup'],
        'ENABLE_SCREENSHOTS': 'true',
        'REACT_APP_FEATUREFLAG_BIZ1476_ENABLE_WEBVIEW_LOGIN': android_config['webview_login'],
        'HIDE_BENTO_LOGIN_FOOTER_LINK': 'false',
    }
    with (repo / '.env').open('a') as environment:
        environment.write('\n' + ''.join(f'{key}={value}\n' for key, value in overrides.items()))


def allocate_android_version(c):
    versioning = c['android'].get('versioning', {})
    try:
        initial = int(versioning['initial_build_number'])
        offset = int(versioning['code_offset'])
    except (KeyError, TypeError, ValueError):
        raise SafeError('Android versioning configuration is incomplete.') from None
    prefix = versioning.get('name_prefix')
    if initial < 1 or offset < 0 or not isinstance(prefix, str) or not re.fullmatch(r'\d+\.\d+', prefix):
        raise SafeError('Android versioning configuration is invalid.')
    state_path = ROOT / 'data/android-build-number.json'
    last = initial
    if state_path.exists():
        try:
            last = int(json.loads(state_path.read_text())['last_build_number'])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise SafeError('Android build-number state is invalid; inspect it before another build.') from None
        if last < initial:
            raise SafeError('Android build-number state is behind the configured baseline.')
    build_number = last + 1
    version_code = build_number + offset
    if version_code > 2_100_000_000:
        raise SafeError('Android versionCode exceeds the Google Play limit.')
    atomic_json(state_path, {'last_build_number': build_number})
    return {'build_number': build_number, 'version_code': version_code,
            'version_name': f'{prefix}.{build_number}'}


def apply_android_version(repo, version):
    path = repo / 'android/app/build.gradle'
    text = path.read_text()
    code_pattern = r'(?m)^(\s*)versionCode\s*=\s*\d+\s*$'
    name_pattern = r'(?m)^(\s*)versionName\s*=\s*"[^"]+"\s*$'
    if len(re.findall(code_pattern, text)) != 1 or len(re.findall(name_pattern, text)) != 1:
        raise SafeError('Expected one Android versionCode and versionName assignment.')
    text = re.sub(code_pattern, rf'\g<1>versionCode = {version["version_code"]}', text)
    text = re.sub(name_pattern, rf'\g<1>versionName = "{version["version_name"]}"', text)
    path.write_text(text)


def configure_java_trust(c, cmd, temp):
    if sys.platform == 'darwin':
        cmd.env['JAVA_TOOL_OPTIONS'] = ('-Djavax.net.ssl.trustStoreType=KeychainStore '
                                        '-Djavax.net.ssl.trustStore=NONE')
        return
    java_home = Path(cmd.env.get('JAVA_HOME', ''))
    default_store = java_home / 'lib/security/cacerts'
    keytool = java_home / 'bin/keytool'
    if not default_store.is_file() or not keytool.is_file():
        raise SafeError('JDK 17 truststore or keytool is missing.')
    truststore = private_file(temp / 'java-cacerts', default_store.read_bytes())
    if c['ca_bundle']:
        try:
            bundle = Path(c['ca_bundle']).read_text()
        except OSError:
            raise SafeError('Configured CA bundle cannot be read.') from None
        certificates = re.findall(r'-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----',
                                  bundle, flags=re.S)
        if not certificates:
            raise SafeError('Configured CA bundle contains no PEM certificates.')
        for index, certificate in enumerate(certificates):
            cert = private_file(temp / f'bank-ca-{index}.pem', certificate + '\n')
            cmd.run([keytool, '-importcert', '-noprompt', '-storepass', 'changeit',
                     '-keystore', truststore, '-alias', f'bento-bank-ca-{index}', '-file', cert],
                    capture=True, label='Add approved CA to Java truststore')
    cmd.env['JAVA_TOOL_OPTIONS'] = ('-Djavax.net.ssl.trustStore=' + str(truststore) +
                                    ' -Djavax.net.ssl.trustStorePassword=changeit')


GRADLE_DISTRIBUTION_NAME = 'gradle-8.14.3-bin.zip'
GRADLE_DISTRIBUTION_SHA256 = 'bd71102213493060956ec229d946beee57158dbd89d0e62b91bca0fa2c5f3531'
GRADLE_DISTRIBUTION_URL = 'https://downloads.gradle.org/distributions/' + GRADLE_DISTRIBUTION_NAME
JITPACK_FILES = {
    'com/github/yalantis/ucrop/2.2.11/ucrop-2.2.11.pom':
        '1e070fcc04b929e8b0f847463240550a1639e4bc2e7110b0a57f4f1366247580',
    'com/github/yalantis/ucrop/2.2.11/ucrop-2.2.11.aar':
        'c12db23784aaa954d8998106cf5c1cfee2993c62f7601c790da8e21d4c51a0a9',
    'com/github/zacharee/AndroidPdfViewer/4.0.1/AndroidPdfViewer-4.0.1.pom':
        'a9006febc8a96e30dcab3d5fdedc08985717ab62a6233eccc34efc492a9053ec',
    'com/github/zacharee/AndroidPdfViewer/4.0.1/AndroidPdfViewer-4.0.1.aar':
        '6465b8febdbf75172890accd2dd19e45aeab9c2ff328bc11bd8db292a74454dc',
    'com/github/ybq/Android-SpinKit/1.4.0/Android-SpinKit-1.4.0.pom':
        'cee6e4a9e11e87fafa6957117c3f1eec80a6b0f2fef06e1b6cf50716481b9dbe',
    'com/github/ybq/Android-SpinKit/1.4.0/Android-SpinKit-1.4.0.aar':
        '3d1bb6b05feb0c62f92d174b0d32a611da238a22a929fe1db2b0f7c4fb58ede2',
}


def cached_jitpack_repository(c):
    repository = ROOT / 'data/cache/jitpack'
    repository.mkdir(parents=True, exist_ok=True, mode=0o700)
    for relative, expected in JITPACK_FILES.items():
        artifact = repository / relative
        if artifact.exists():
            if not artifact.is_file() or artifact.is_symlink() or digest(artifact) != expected:
                raise SafeError('Cached JitPack artifact failed checksum validation; inspect and remove it manually.')
            continue
        artifact.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(dir=artifact.parent, prefix=artifact.name + '.', suffix='.part')
        os.close(fd)
        temporary = Path(temporary)
        try:
            with requests.get('https://jitpack.io/' + relative, stream=True,
                              verify=c['ca_bundle'] or True, timeout=(15, 120)) as response:
                if response.status_code != 200 or urlsplit(response.url).hostname not in ('jitpack.io', 'www.jitpack.io'):
                    raise SafeError('Could not download a qualified JitPack artifact.')
                size = 0
                with temporary.open('wb') as output:
                    for chunk in response.iter_content(64 * 1024):
                        size += len(chunk)
                        if size > 5 * 1024 * 1024:
                            raise SafeError('JitPack artifact exceeded the qualified size limit.')
                        output.write(chunk)
            if digest(temporary) != expected:
                raise SafeError('Downloaded JitPack artifact failed checksum validation.')
            temporary.chmod(0o600)
            os.replace(temporary, artifact)
        except requests.RequestException:
            raise SafeError('Could not download a qualified JitPack artifact.') from None
        finally:
            temporary.unlink(missing_ok=True)
    return repository


def download_gradle_distribution(c, destination):
    chunk_size = 4 * 1024 * 1024
    maximum_size = 300 * 1024 * 1024
    offset = 0
    total = None
    print('\n→ Download pinned Gradle distribution')
    with destination.open('wb') as output:
        while total is None or offset < total:
            requested_end = offset + chunk_size - 1
            for attempt in range(4):
                try:
                    with requests.get(
                            GRADLE_DISTRIBUTION_URL,
                            headers={'Range': f'bytes={offset}-{requested_end}'},
                            stream=True,
                            verify=c['ca_bundle'] or True,
                            timeout=(15, 120)) as response:
                        content_range = re.fullmatch(
                            r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                        if response.status_code != 206 or not content_range:
                            raise ValueError('server did not honor the range request')
                        start, end, response_total = map(int, content_range.groups())
                        if start != offset or end > requested_end or response_total > maximum_size:
                            raise ValueError('server returned unexpected archive bounds')
                        if total is not None and response_total != total:
                            raise ValueError('server changed the archive size')
                        written = 0
                        for chunk in response.iter_content(64 * 1024):
                            output.write(chunk)
                            written += len(chunk)
                        if written != end - start + 1:
                            raise ValueError('server returned an incomplete range')
                        total = response_total
                        offset += written
                        break
                except (requests.RequestException, ValueError):
                    output.seek(offset)
                    output.truncate()
                    if attempt == 3:
                        raise SafeError('Could not download a complete Gradle distribution range.') from None
                    time.sleep(2 ** attempt)


def cached_gradle_distribution(c, _cmd):
    directory = ROOT / 'data/cache/gradle'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive = directory / GRADLE_DISTRIBUTION_NAME
    if archive.exists():
        if not archive.is_file() or archive.is_symlink() or digest(archive) != GRADLE_DISTRIBUTION_SHA256:
            raise SafeError('Cached Gradle distribution failed checksum validation; inspect and remove it manually.')
        return archive
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=GRADLE_DISTRIBUTION_NAME + '.', suffix='.part')
    os.close(fd)
    temporary = Path(temporary)
    try:
        download_gradle_distribution(c, temporary)
        if digest(temporary) != GRADLE_DISTRIBUTION_SHA256:
            raise SafeError('Downloaded Gradle distribution failed checksum validation.')
        temporary.chmod(0o600)
        os.replace(temporary, archive)
    finally:
        temporary.unlink(missing_ok=True)
    return archive


def configure_gradle_distribution(repo, archive):
    properties = repo / 'android/gradle/wrapper/gradle-wrapper.properties'
    text = properties.read_text()
    source = 'distributionUrl=https\\://services.gradle.org/distributions/gradle-8.14.3-bin.zip'
    local = 'distributionUrl=' + archive.resolve().as_uri().replace(':', '\\:', 1)
    if text.count(source) != 1:
        raise SafeError('Gradle wrapper distribution changed; qualify its direct URL and checksum.')
    if 'distributionSha256Sum=' in text:
        if f'distributionSha256Sum={GRADLE_DISTRIBUTION_SHA256}' not in text:
            raise SafeError('Gradle wrapper checksum differs from the qualified value.')
    else:
        text += f'distributionSha256Sum={GRADLE_DISTRIBUTION_SHA256}\n'
    properties.write_text(text.replace(source, local))


def android(c, secret_store, cmd, repo, temp, output, android_config, version):
    sdk = Path(c['toolchain']['android_sdk'])
    for needed in ('platforms/android-36/android.jar', 'build-tools/36.0.0/aapt', 'ndk/27.0.12077973/source.properties'):
        if not (sdk / needed).exists():
            raise SafeError(f'Missing Android SDK component: {needed}. See ./bento doctor.')
    configure_java_trust(c, cmd, temp)
    java = cmd.run(['java', '-version'], capture=True, label='Check Java')
    if not re.search(r'version "17[.\"]', java):
        raise SafeError('Android requires Java 17. Set toolchain.java_home to the existing JDK 17.')
    cmd.env['PATH'] = str(Path(cmd.env['JAVA_HOME']) / 'bin') + os.pathsep + cmd.env['PATH'] if cmd.env.get('JAVA_HOME') else cmd.env['PATH']
    init = []
    signing = 'repository-debug-key'
    if c['secrets'].get('android'):
        s = secret_store.get('android', fresh=True)
        for k in ('store_password', 'key_alias', 'key_password'):
            cmd.sensitive.add(s[k])
        private_file(temp / 'android.keystore', base64.b64decode(s['keystore_base64'], validate=True))
        cmd.env.update(BENTO_KEYSTORE=str(temp / 'android.keystore'), BENTO_STORE_PASSWORD=s['store_password'],
                       BENTO_KEY_ALIAS=s['key_alias'], BENTO_KEY_PASSWORD=s['key_password'])
        init = ['-I', str(ROOT / 'scripts/android-signing.gradle')]
        signing = 'local-file-key' if c.get('credential_source') == 'local' else 'secrets-manager-key'
    cmd.env['LOCAL_MAVEN_REPOSITORY'] = cached_jitpack_repository(c).resolve().as_uri()
    configure_gradle_distribution(repo, cached_gradle_distribution(c, cmd))
    cmd.run(['/bin/bash', './gradlew', '--no-daemon', '--console=plain', *init, android_config['task']],
        repo / 'android', label=f'Build Android {android_config["label"]} APK')
    apks = list((repo / android_config['apk_path']).glob('*.apk'))
    if not apks:
        raise SafeError('Gradle completed but the expected APK was not found.')
    metadata = []
    for apk in apks:
        info = cmd.run([sdk / 'build-tools/36.0.0/aapt', 'dump', 'badging', apk], capture=True, label='Verify Android identity')
        m = re.search(r"package: name='([^']+)' versionCode='([^']+)' versionName='([^']+)'", info)
        if not m or m[1] != android_config['package_id']:
            raise SafeError('APK application ID did not match the configured identity.')
        cmd.run([sdk / 'build-tools/36.0.0/apksigner', 'verify', apk], capture=True, label='Verify APK signature')
        shutil.copy2(apk, output / apk.name)
        metadata.append({'file': apk.name, 'app_id': m[1], 'build_number': m[2], 'version': m[3]})
    mapping = repo / android_config['mapping_path']
    if mapping.exists():
        shutil.copy2(mapping, output / 'mapping.txt')
    return {'signing': signing, 'apps': metadata, 'java': java.splitlines()[0],
        'bitrise_build_number': version['build_number']}


def ios_configuration(c, variant=None):
    config = dict(c['ios'])
    variants = config.pop('variants', {})
    if variant:
        selected = variants.get(variant)
        if not isinstance(selected, dict):
            raise SafeError('Unknown iOS build variant.')
        config.update(selected)
    if any(not isinstance(config.get(key), str) or not config[key]
           for key in ('workspace', 'scheme', 'bundle_id')):
        raise SafeError('iOS build configuration is incomplete.')
    return config


@contextlib.contextmanager
def ios_signing(c, store, cmd, temp, ios_config):
    s = store.get('ios', fresh=True)
    for k in ('p12_password', 'team_id', 'signing_identity'):
        if not s.get(k):
            raise SafeError(f'iOS secret needs {k}.')
    cmd.sensitive.add(s['p12_password'])
    keypass = random_secrets.token_urlsafe(32)
    cmd.sensitive.add(keypass)
    keychain = temp / 'signing.keychain-db'
    p12 = private_file(temp / 'signing.p12', base64.b64decode(s['p12_base64'], validate=True))
    exports = dict(s.get('export_options') or {})
    if not isinstance(exports, dict) or exports.get('teamID') != s['team_id']:
        raise SafeError('iOS export_options must contain the matching teamID and the approved method/provisioningProfiles.')
    # This milestone always exports locally; user-supplied options cannot request a store upload.
    if exports.get('destination', 'export') != 'export':
        raise SafeError('Milestone 1 only supports export_options.destination=export, never upload.')
    exports.update(destination='export', signingStyle='manual', manageAppVersionAndBuildNumber=False)
    profiles = []
    installed = []
    old_search = cmd.run(['security', 'list-keychains', '-d', 'user'], capture=True, label='Read keychain search list')
    old_keys = __import__('shlex').split(old_search)
    journal = ROOT / 'data/signing-recovery.json'
    if journal.exists():
        raise SafeError('An interrupted signing session needs recovery: ./bento recover-signing.')
    atomic_json(journal, {'keychains': old_keys, 'temporary_keychain': str(keychain), 'profiles': []})
    try:
        cmd.run(['security', 'create-keychain', '-p', keypass, keychain], capture=True, label='Create temporary signing keychain')
        cmd.run(['security', 'set-keychain-settings', '-lut', '21600', keychain], capture=True)
        cmd.run(['security', 'unlock-keychain', '-p', keypass, keychain], capture=True)
        cmd.run(['security', 'import', p12, '-k', keychain, '-P', s['p12_password'], '-T', '/usr/bin/codesign', '-T', '/usr/bin/security'], capture=True, label='Import signing identity')
        cmd.run(['security', 'set-key-partition-list', '-S', 'apple-tool:,apple:,codesign:', '-s', '-k', keypass, keychain], capture=True)
        cmd.run(['security', 'list-keychains', '-d', 'user', '-s', keychain, *old_keys], capture=True)
        for i, encoded in enumerate(s.get('profiles_base64', [])):
            profile = private_file(temp / f'profile-{i}.mobileprovision', base64.b64decode(encoded, validate=True))
            decoded = cmd.run(['security', 'cms', '-D', '-i', profile], capture=True, label='Validate provisioning profile')
            data = plistlib.loads(decoded.encode())
            uid = data['UUID']
            if not re.fullmatch(r'[A-Fa-f0-9-]{36}', uid):
                raise SafeError('Invalid provisioning profile UUID.')
            if s['team_id'] not in data.get('TeamIdentifier', []):
                raise SafeError('Provisioning profile belongs to another Apple team.')
            if data['ExpirationDate'] <= datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None):
                raise SafeError('Provisioning profile has expired.')
            profiles.append(data)
            # Xcode 16+ uses this directory; retain legacy location compatibility.
            for directory in (Path.home() / 'Library/Developer/Xcode/UserData/Provisioning Profiles',
                              Path.home() / 'Library/MobileDevice/Provisioning Profiles'):
                directory.mkdir(parents=True, exist_ok=True)
                destination = directory / f'{uid}.mobileprovision'
                if destination.exists():
                    if destination.read_bytes() != profile.read_bytes():
                        raise SafeError('A different provisioning profile already occupies this UUID; refusing to overwrite it.')
                else:
                    installed.append(str(destination))
                    atomic_json(journal, {'keychains': old_keys, 'temporary_keychain': str(keychain), 'profiles': installed})
                    private_file(destination, profile.read_bytes())
        app_profile = exports.get('provisioningProfiles', {}).get(ios_config['bundle_id'])
        if not app_profile or not any(app_profile in (p['Name'], p['UUID']) for p in profiles):
            raise SafeError('export_options.provisioningProfiles must map the app ID to an included profile name or UUID.')
        export_file = private_file(temp / 'ExportOptions.plist', plistlib.dumps(exports))
        yield s, keychain, app_profile, export_file
    finally:
        # Cleanup must run even if Bitrise restarts. Do not use Commands' build monitor here.
        result = subprocess.run(['security', 'list-keychains', '-d', 'user', '-s', *old_keys], capture_output=True)
        deleted = subprocess.run(['security', 'delete-keychain', str(keychain)], capture_output=True)
        for name in installed:
            Path(name).unlink(missing_ok=True)
        if result.returncode == 0 and (deleted.returncode == 0 or not keychain.exists()):
            journal.unlink(missing_ok=True)
        else:
            raise SafeError('Signing cleanup failed. Run ./bento recover-signing before another build or resuming Bitrise.')


def ios(c, store, cmd, repo, temp, output, ios_config):
    xcode = cmd.run(['xcodebuild', '-version'], capture=True, label='Check Xcode')
    if not c['toolchain']['expected_xcode'] or c['toolchain']['expected_xcode'] not in xcode.splitlines()[0]:
        raise SafeError('Set toolchain.expected_xcode to the qualified existing Xcode version reported by ./bento doctor.')
    ruby = cmd.run(['ruby', '--version'], capture=True)
    pod = c['toolchain']['pod_bin'] or shutil.which('pod', path=cmd.env['PATH'])
    if not pod:
        raise SafeError('CocoaPods is missing. Set toolchain.pod_bin to the existing Bitrise-qualified executable.')
    pod_version = cmd.run([pod, '--version'], capture=True).strip()
    if pod_version != c['toolchain']['expected_pod']:
        raise SafeError('CocoaPods version differs from toolchain.expected_pod.')
    # Rewrite only the known vendor URL in this disposable checkout, in both lockfile and Podfile.
    old = 'git@github.com:BentoInc/bento.mobileapp.git'
    new = sources.native_url(c)
    for file in (repo / 'ios/Podfile', repo / 'ios/Podfile.lock'):
        file.write_text(file.read_text().replace(old, new))
    lock = repo / 'ios/Podfile.lock'
    lock.write_text(re.sub(r'^PODFILE CHECKSUM: [0-9a-f]+$',
        'PODFILE CHECKSUM: ' + __import__('hashlib').sha1((repo / 'ios/Podfile').read_bytes()).hexdigest(),
        lock.read_text(), flags=re.M))
    sources.authenticate(c, store, cmd, temp)
    cmd.run(['git', 'ls-remote', '--exit-code', new, 'refs/heads/auth-26-06-cocoapods'], capture=True,
            label='Check iOS native dependency branch')
    # The original boost patch accidentally depends on a relative path; prepare() fails if it cannot apply.
    lock_before = lock.read_text()
    cmd.run([pod, 'install'], repo / 'ios', label='Materialize locked CocoaPods dependencies')
    accept_hermes_checksum(lock_before, lock.read_text())
    locked_pods = digest(lock)
    cmd.run([pod, 'install', '--deployment'], repo / 'ios', label='Install locked CocoaPods dependencies')
    if digest(lock) != locked_pods:
        raise SafeError('CocoaPods changed the adjusted lockfile; review dependency resolution before continuing.')
    with ios_signing(c, store, cmd, temp, ios_config) as (s, keychain, profile, export_file):
        signing_file = private_file(temp / 'target-signing.json', json.dumps({
            'team': s['team_id'], 'identity': s['signing_identity'],
            'profiles': s['export_options']['provisioningProfiles'], 'bundle_id': ios_config['bundle_id']}))
        cmd.run(['ruby', ROOT / 'scripts/ios-signing.rb', repo / 'ios/BentoMobileApp.xcodeproj', signing_file], repo,
                label='Configure signing only on app targets in the disposable checkout')
        archive = temp / 'app.xcarchive'
        cmd.run(['xcodebuild', '-workspace', repo / ios_config['workspace'], '-scheme', ios_config['scheme'],
            '-configuration', 'Release', '-destination', 'generic/platform=iOS', '-derivedDataPath', temp / 'DerivedData',
            '-archivePath', archive, 'archive',
            'OTHER_CODE_SIGN_FLAGS=--keychain ' + __import__('shlex').quote(str(keychain))], repo, label='Archive signed iOS dev build')
        info = plistlib.loads((archive / 'Info.plist').read_bytes())['ApplicationProperties']
        if info['CFBundleIdentifier'] != ios_config['bundle_id']:
            raise SafeError('Archived iOS bundle ID does not match configured identity.')
        cmd.run(['xcodebuild', '-exportArchive', '-archivePath', archive, '-exportPath', temp / 'export',
                 '-exportOptionsPlist', export_file], repo, label='Export signed IPA')
        ipas = list((temp / 'export').glob('*.ipa'))
        if len(ipas) != 1:
            raise SafeError('Expected one exported IPA.')
        # Verify exported app signature without changing installation or submitting to Apple.
        cmd.run(['codesign', '--verify', '--deep', '--strict', archive / 'Products' / info['ApplicationPath']], capture=True, label='Verify iOS archive signature')
        cmd.run(['ditto', '-x', '-k', ipas[0], temp / 'export-check'], capture=True, label='Inspect exported IPA')
        apps = list((temp / 'export-check/Payload').glob('*.app'))
        if len(apps) != 1 or plistlib.loads((apps[0] / 'Info.plist').read_bytes())['CFBundleIdentifier'] != ios_config['bundle_id']:
            raise SafeError('Exported IPA identity did not match.')
        cmd.run(['codesign', '--verify', '--deep', '--strict', apps[0]], capture=True, label='Verify exported IPA signature')
        shutil.copy2(ipas[0], output / ipas[0].name)
        if (archive / 'dSYMs').exists():
            shutil.make_archive(str(output / 'dSYMs'), 'zip', archive, 'dSYMs')
        cmd.run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', archive, output / 'xcarchive.zip'],
                capture=True, label='Retain Xcode archive')
    return {'app_id': info['CFBundleIdentifier'], 'scheme': ios_config['scheme'],
        'version': info.get('CFBundleShortVersionString'),
        'build_number': info.get('CFBundleVersion'), 'xcode': xcode.strip(), 'ruby': ruby.strip(), 'cocoapods': pod_version,
        'signing': 'local-file-key' if c.get('credential_source') == 'local' else 'secrets-manager-key',
        'export_method': s['export_options'].get('method')}


def publish(c, store, directory, manifest):
    files = []
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.name != 'manifest.json':
            files.append({'name': path.name, 'bytes': path.stat().st_size, 'sha256': digest(path)})
    manifest['files'] = files
    manifest['storage'] = 'local'
    manifest['upload_status'] = 'disabled'
    atomic_json(directory / 'manifest.json', manifest)
    a = c['artifactory']
    if not a['url']:
        print('Artifacts retained on the Mac; Artifactory is not configured.', flush=True)
        return
    token = store.get('artifactory', fresh=True)['token']
    prefix = '/'.join([a['prefix'], 'usbank', 'dev', manifest['sha'], str(manifest['pipeline_id']), str(manifest['job_id']), manifest['platform']])
    manifest['upload_status'] = 'uploading'
    atomic_json(directory / 'manifest.json', manifest)
    try:
        for entry in files:
            url = a['url'] + '/' + quote(a['repository'], safe='') + '/' + quote(prefix + '/' + entry['name'], safe='/')
            with (directory / entry['name']).open('rb') as body:
                r = requests.put(url, data=body, headers={'Authorization': 'Bearer ' + token,
                    'X-Checksum-Sha256': entry['sha256']}, verify=c['ca_bundle'] or True, timeout=(10, 300), allow_redirects=False)
            if r.status_code not in (200, 201):
                raise SafeError(f'Artifactory upload failed (HTTP {r.status_code}); local artifacts retained.')
            # Verify the checksum reported by Artifactory, not only the HTTP success.
            remote = requests.get(a['url'] + '/api/storage/' + quote(a['repository'], safe='') + '/' + quote(prefix + '/' + entry['name'], safe='/'),
                headers={'Authorization': 'Bearer ' + token}, verify=c['ca_bundle'] or True, timeout=(10, 60), allow_redirects=False)
            if remote.status_code != 200 or remote.json().get('checksums', {}).get('sha256') != entry['sha256']:
                raise SafeError('Artifactory checksum verification failed; local artifacts retained.')
            entry['url'] = url
        manifest.update(storage='local+artifactory', upload_status='complete')
        manifest_url = a['url'] + '/' + quote(a['repository'], safe='') + '/' + quote(prefix + '/manifest.json', safe='/')
        payload = json.dumps(manifest, indent=2).encode()
        r = requests.put(manifest_url, data=payload, headers={'Authorization': 'Bearer ' + token,
            'X-Checksum-Sha256': __import__('hashlib').sha256(payload).hexdigest()},
            verify=c['ca_bundle'] or True, timeout=(10, 60), allow_redirects=False)
        if r.status_code not in (200, 201):
            raise SafeError('Artifactory manifest upload failed; local artifacts retained.')
    except Exception as e:
        manifest['upload_status'] = 'failed'
        atomic_json(directory / 'manifest.json', manifest)
        if isinstance(e, SafeError):
            raise
        raise SafeError('Artifactory request failed; local artifacts retained. Check connectivity and permissions.') from None
    atomic_json(directory / 'manifest.json', manifest)


@contextlib.contextmanager
def build_signals():
    def terminate(*_):
        raise KeyboardInterrupt()
    previous = {sig: signal.signal(sig, terminate) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def execute_build(c, platform, repo, sha, pipeline, job, source='gitlab', canceled=None, branch='dev',
                  android_variant=None, ios_variant=None):
    """Caller owns the host lock; repo must be a disposable checkout."""
    if shutil.disk_usage(ROOT).free < c['minimum_free_gb'] * 1024 ** 3:
        raise SafeError('Insufficient free disk space; archive old artifacts or expand storage.')
    store = Secrets(c)
    with tempfile.TemporaryDirectory(prefix='job-', dir=ROOT / 'runtime') as tmp:
        if (ROOT / 'data/signing-recovery.json').exists():
            raise SafeError('Signing state needs ./bento recover-signing before another build.')
        temp = Path(tmp)
        cmd = Commands(tool_env(c, native=platform != 'validate'), canceled=canceled)
        cmd.env['PATH'] = str(Path(cmd.env['JAVA_HOME']) / 'bin') + os.pathsep + cmd.env['PATH'] if cmd.env.get('JAVA_HOME') else cmd.env['PATH']
        try:
            android_config = android_configuration(c, android_variant or 'stage') if platform == 'android' else None
            ios_config = ios_configuration(c, ios_variant) if platform == 'ios' else None
            environment = (android_config or ios_config or {}).get('environment', 'dev')
            tools = prepare(c, store, cmd, repo, temp, native=platform != 'validate', environment=environment)
            if platform == 'validate':
                for action in ('typescript', 'lint', 'test'):
                    cmd.run(['yarn', action, *(['--runInBand', '--ci'] if action == 'test' else [])], repo)
                return
            android_version = None
            if platform == 'android':
                configure_android_environment(store, cmd, repo, android_config)
                android_version = allocate_android_version(c)
                apply_android_version(repo, android_version)
            output = artifact_dir(ROOT, pipeline, job, platform)
            output.mkdir(parents=True, exist_ok=False)
            meta = android(c, store, cmd, repo, temp, output, android_config, android_version) if platform == 'android' else \
                ios(c, store, cmd, repo, temp, output, ios_config)
            manifest = {'project': c['project_path'], 'branch': branch, 'environment': environment, 'brand': 'usbank',
                'sha': sha, 'pipeline_id': pipeline, 'job_id': job, 'platform': platform, 'tools': tools,
                'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'native': meta,
                'distribution': 'none', 'qualification': 'native-build-only', 'source': source,
                'source_provider': sources.provider(c), 'source_repository': sources.repository(c)}
            publish(c, store, output, manifest)
            print('Native build complete. Device installation and functional qualification are separate checks.', flush=True)
        finally:
            for file in (repo / '.env', repo / 'ios/.xcode.env.local'):
                file.unlink(missing_ok=True)


def build(platform):
    if platform not in ('android', 'ios', 'validate'):
        raise SafeError('Unknown build platform.')
    c = load_config()
    if sources.provider(c) != 'gitlab':
        raise SafeError('GitLab CI jobs require source_provider gitlab.')
    if os.environ.get('CI_PROJECT_PATH') != c['project_path'] or os.environ.get('CI_COMMIT_BRANCH') != 'dev':
        raise SafeError('Build must run as a GitLab job for the configured project dev branch.')
    if os.environ.get('CI_COMMIT_REF_PROTECTED') != 'true':
        raise SafeError('Protect dev and restrict this runner to protected refs before running native builds.')
    repo = Path(os.environ['CI_PROJECT_DIR']).resolve()
    sha = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    if sha != os.environ.get('CI_COMMIT_SHA'):
        raise SafeError('Checkout commit does not match the pipeline commit.')
    with build_signals(), host_lock(ROOT):
        execute_build(c, platform, repo, sha, os.environ['CI_PIPELINE_ID'], os.environ['CI_JOB_ID'])


class Tee:
    """Copy already-redacted console output to a private test log."""
    def __init__(self, terminal, log):
        self.terminal, self.log = terminal, log

    def write(self, value):
        self.terminal.write(value)
        self.log.write(value)
        self.flush()
        return len(value)

    def flush(self):
        self.terminal.flush()
        self.log.flush()


def local_build(platform, source_path):
    if platform not in ('both', 'android', 'ios', 'validate'):
        raise SafeError('Choose both, android, ios, or validate.')
    if not source_path:
        raise SafeError('Supply --repo /path/to/a/separate/dev/checkout, or use ./bento run both to fetch the configured source.')
    c = load_config()
    source_repo = Path(source_path).expanduser().resolve()
    def git_read(*args):
        try:
            return subprocess.check_output(['git', '-C', str(source_repo), *args], text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError):
            raise SafeError('The supplied checkout must contain a local dev branch and the configured source origin.') from None
    url = git_read('remote', 'get-url', 'origin')
    if url not in sources.allowed_origins(c):
        raise SafeError('Use a separate clone whose origin is the configured source project, without credentials embedded in its URL.')
    sha = git_read('rev-parse', '--verify', 'refs/heads/dev^{commit}')
    selected = ('android', 'ios') if platform == 'both' else (platform,)
    print(f'Testing local dev commit {sha}. Only committed files are used; this command does not fetch.')
    with build_signals(), host_lock(ROOT):
        # Numeric coordinates remain compatible with the local-download API. Millisecond
        # IDs distinguish these runs from ordinary GitLab IDs; source is explicit in metadata.
        ident = str(int(time.time() * 1000))
        run = ROOT / 'data/test-runs' / ident
        if run.exists() or (ROOT / 'data/artifacts' / ident).exists():
            raise SafeError('Test run ID already exists; retry the command.')
        run.mkdir(parents=True, mode=0o700)
        record = {'id': ident, 'source': 'local', 'sha': sha, 'platforms': list(selected), 'status': 'running', 'jobs': {},
            'source_provider': sources.provider(c), 'source_repository': sources.repository(c)}
        atomic_json(run / 'run.json', record)
        for index, target in enumerate(selected, 1):
            job = str(index)
            record['jobs'][target] = 'running'
            atomic_json(run / 'run.json', record)
            try:
                # Recheck before each platform; expiry prevents new starts as it does in CI.
                from .locking import check_window
                check_window(ROOT)
                with (run / (target + '.log')).open('x') as log, tempfile.TemporaryDirectory(prefix='local-checkout-', dir=ROOT / 'runtime') as tmp:
                    (run / (target + '.log')).chmod(0o600)
                    with contextlib.redirect_stdout(Tee(sys.stdout, log)), contextlib.redirect_stderr(Tee(sys.stderr, log)):
                        repo = Path(tmp) / 'app'
                        cmd = Commands(tool_env(c))
                        cmd.run(['git', 'clone', '--no-local', '--no-checkout', '--', source_repo, repo], capture=True, label='Copy committed source into a disposable checkout')
                        cmd.run(['git', 'checkout', '--detach', sha], repo, capture=True, label='Select the pinned dev commit')
                        execute_build(c, target, repo, sha, ident, job, source='local')
                record['jobs'][target] = 'success'
                atomic_json(run / 'run.json', record)
            except BaseException as error:
                record['jobs'][target] = 'canceled' if isinstance(error, KeyboardInterrupt) else 'failed'
                record['status'] = record['jobs'][target]
                record['error'] = str(error) if isinstance(error, SafeError) else type(error).__name__
                atomic_json(run / 'run.json', record)
                print(f'Test stopped. Logs and status: {run}', flush=True)
                raise
        record['status'] = 'success'
        atomic_json(run / 'run.json', record)
        print(f'Test complete. Logs: {run}\nArtifacts: {ROOT / "data/artifacts" / ident}', flush=True)
