import json
import os
import signal
import subprocess
import sys
import time

import pytest

from bento_ci import build
from bento_ci.build import Commands, accept_hermes_checksum, android_configuration, apply_android_version, configure_android_environment, configure_gradle_distribution, configure_maven_downloads, publish, tool_env
from bento_ci.core import ROOT, SafeError, atomic_json, digest, load_config
from bento_ci.locking import check_window, host_lock, open_window


@pytest.fixture
def config():
    return json.loads((ROOT/'config.example.json').read_text())


@pytest.mark.parametrize(('variant', 'task', 'apk_path', 'mapping_path'), [
    ('stage', ':app:assembleSpendmanagementReleaseStaging',
     'android/app/build/outputs/apk/spendmanagement/releaseStaging',
     'android/app/build/outputs/mapping/spendmanagementReleaseStaging/mapping.txt'),
    ('developer', ':app:assembleDeveloperReleaseStaging',
     'android/app/build/outputs/apk/developer/releaseStaging',
     'android/app/build/outputs/mapping/developerReleaseStaging/mapping.txt'),
])
def test_android_configuration_selects_bitrise_variant(config, variant, task, apk_path, mapping_path):
    selected = android_configuration(config, variant)
    assert selected['task'] == task
    assert selected['environment'] == 'stage'
    assert selected['package_id'] == 'com.usbank.spendmanagement'
    assert selected['apk_path'] == apk_path
    assert selected['mapping_path'] == mapping_path


def test_android_environment_matches_bitrise_overrides(config, tmp_path):
    class Store:
        def get(self, name, **kwargs):
            assert name == 'android_environment'
            assert kwargs['required_fields'] == ('ssl_certificate', 'ssl_certificate_backup')
            return {'ssl_certificate': 'primary-pin', 'ssl_certificate_backup': 'backup-pin'}

    class Command:
        sensitive = set()

    (tmp_path / '.env').write_text('ENABLE_SSL_PINNING=false\n')
    configure_android_environment(Store(), Command(), tmp_path,
                                  android_configuration(config, 'developer'))
    lines = (tmp_path / '.env').read_text().splitlines()
    assert lines[-7:] == [
        'ENABLE_SSL_PINNING=true',
        'ENABLE_JAILBREAK_ROOT_DETECTION=true',
        'SSL_CERTIFICATE=primary-pin',
        'SSL_CERTIFICATE_BACKUP=backup-pin',
        'ENABLE_SCREENSHOTS=true',
        'REACT_APP_FEATUREFLAG_BIZ1476_ENABLE_WEBVIEW_LOGIN=false',
        'HIDE_BENTO_LOGIN_FOOTER_LINK=false',
    ]
    assert Command.sensitive == {'primary-pin', 'backup-pin'}


def test_android_versioning_matches_bitrise_formula(config, tmp_path, monkeypatch):
    monkeypatch.setattr(build, 'ROOT', tmp_path)
    first = build.allocate_android_version(config)
    second = build.allocate_android_version(config)
    assert first == {'build_number': 309, 'version_code': 409, 'version_name': '2.0.309'}
    assert second == {'build_number': 310, 'version_code': 410, 'version_name': '2.0.310'}
    gradle = tmp_path / 'repo/android/app/build.gradle'
    gradle.parent.mkdir(parents=True)
    gradle.write_text('android {\n  defaultConfig {\n    versionCode = 408\n    versionName = "2.1.0"\n  }\n}\n')
    apply_android_version(tmp_path / 'repo', first)
    assert 'versionCode = 409' in gradle.read_text()
    assert 'versionName = "2.0.309"' in gradle.read_text()


def test_android_gradle_distribution_uses_local_pinned_archive(tmp_path):
    properties = tmp_path / 'android/gradle/wrapper/gradle-wrapper.properties'
    properties.parent.mkdir(parents=True)
    properties.write_text(
        'distributionUrl=https\\://services.gradle.org/distributions/gradle-8.14.3-bin.zip\n')
    archive = tmp_path / 'cache/gradle-8.14.3-bin.zip'
    archive.parent.mkdir()
    archive.touch()
    configure_gradle_distribution(tmp_path, archive)
    updated = properties.read_text()
    assert f'distributionUrl={archive.resolve().as_uri().replace(":", "\\\\:", 1)}' in updated
    assert 'distributionSha256Sum=bd71102213493060956ec229d946beee57158dbd89d0e62b91bca0fa2c5f3531' in updated


def test_android_gradle_distribution_reuses_valid_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(build, 'ROOT', tmp_path)
    archive = tmp_path / 'data/cache/gradle/gradle-8.14.3-bin.zip'
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b'qualified archive')
    monkeypatch.setattr(build, 'digest', lambda path: build.GRADLE_DISTRIBUTION_SHA256)
    monkeypatch.setattr(build, 'download_gradle_distribution',
                        lambda *args: pytest.fail('valid cache should not be downloaded again'))
    assert build.cached_gradle_distribution({}, None) == archive


def test_android_gradle_distribution_rejects_corrupt_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(build, 'ROOT', tmp_path)
    archive = tmp_path / 'data/cache/gradle/gradle-8.14.3-bin.zip'
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b'corrupt archive')
    with pytest.raises(SafeError, match='failed checksum validation'):
        build.cached_gradle_distribution({}, None)


def test_android_jitpack_cache_reuses_qualified_files(config, tmp_path, monkeypatch):
    monkeypatch.setattr(build, 'ROOT', tmp_path)
    repository = tmp_path / 'data/cache/jitpack'
    for relative in build.JITPACK_FILES:
        artifact = repository / relative
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b'qualified artifact')
    monkeypatch.setattr(build, 'digest', lambda path: build.JITPACK_FILES[str(path.relative_to(repository))])
    monkeypatch.setattr(build.requests, 'get', lambda *args, **kwargs: pytest.fail('qualified cache should be reused'))
    assert build.cached_jitpack_repository(config) == repository


def test_android_jitpack_cache_rejects_corrupt_file(config, tmp_path, monkeypatch):
    monkeypatch.setattr(build, 'ROOT', tmp_path)
    relative = next(iter(build.JITPACK_FILES))
    artifact = tmp_path / 'data/cache/jitpack' / relative
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b'corrupt artifact')
    with pytest.raises(SafeError, match='failed checksum validation'):
        build.cached_jitpack_repository(config)


def test_android_builds_with_repository_debug_signing(config, tmp_path, monkeypatch):
    sdk = tmp_path / 'sdk'
    for relative in ('platforms/android-36/android.jar', 'build-tools/36.0.0/aapt',
                     'ndk/27.0.12077973/source.properties'):
        path = sdk / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    config['toolchain']['android_sdk'] = str(sdk)
    config['toolchain']['java_home'] = '/jdk-17'
    config['secrets']['android'] = ''
    repo = tmp_path / 'repo'
    properties = repo / 'android/gradle/wrapper/gradle-wrapper.properties'
    properties.parent.mkdir(parents=True)
    properties.write_text(
        'distributionUrl=https\\://services.gradle.org/distributions/gradle-8.14.3-bin.zip\n')
    apk = repo / 'android/app/build/outputs/apk/spendmanagement/releaseStaging/app.apk'
    apk.parent.mkdir(parents=True)
    apk.write_bytes(b'apk')
    output = tmp_path / 'output'
    output.mkdir()
    commands = []

    class Command:
        env = {'JAVA_HOME': '/jdk-17', 'PATH': '/usr/bin'}
        sensitive = set()

        def run(self, args, *positional, **kwargs):
            commands.append([str(value) for value in args])
            if str(args[0]) == 'java':
                return 'openjdk version "17.0.2"'
            if 'aapt' in str(args[0]):
                return "package: name='com.usbank.spendmanagement' versionCode='409' versionName='2.0.309'"
            return ''

    archive = tmp_path / 'cached-gradle.zip'
    archive.touch()
    monkeypatch.setattr(build, 'cached_gradle_distribution', lambda *args: archive)
    jitpack = tmp_path / 'jitpack'
    jitpack.mkdir()
    monkeypatch.setattr(build, 'cached_jitpack_repository', lambda *args: jitpack)
    result = build.android(config, None, Command(), repo, tmp_path, output,
                           android_configuration(config, 'stage'), {'build_number': 309})
    assert any(':app:assembleSpendmanagementReleaseStaging' in command for command in commands)
    assert result['signing'] == 'repository-debug-key'
    assert result['apps'][0]['version'] == '2.0.309'
    assert (output / 'app.apk').read_bytes() == b'apk'


def test_no_bitrise_or_tls_bypass_in_child_environment(config,monkeypatch):
    monkeypatch.setenv('BITRISEIO_GIT_BRANCH_DEST','production')
    monkeypatch.setenv('NODE_TLS_REJECT_UNAUTHORIZED','0')
    monkeypatch.setenv('GIT_SSL_NO_VERIFY','1')
    monkeypatch.setenv('AWS_ACCESS_KEY_ID','should-not-propagate')
    monkeypatch.setenv('JAVA_TOOL_OPTIONS','-Djavax.net.ssl.trustStore=/unapproved')
    monkeypatch.setenv('JAVA_OPTS','-Djavax.net.ssl.trustStore=/unapproved')
    monkeypatch.setenv('GRADLE_OPTS','-Djavax.net.ssl.trustStore=/unapproved')
    e=tool_env(config)
    assert 'BITRISEIO_GIT_BRANCH_DEST' not in e
    assert 'NODE_TLS_REJECT_UNAUTHORIZED' not in e
    assert 'GIT_SSL_NO_VERIFY' not in e
    assert 'AWS_ACCESS_KEY_ID' not in e
    assert 'JAVA_TOOL_OPTIONS' not in e
    assert 'JAVA_OPTS' not in e
    assert 'GRADLE_OPTS' not in e
    assert e['npm_config_strict_ssl']=='true'


def test_rvm_ruby_uses_matching_gem_environment(config,tmp_path):
    ruby_home=tmp_path/'rvm/rubies/ruby-3.2.1'
    gem_home=tmp_path/'rvm/gems/ruby-3.2.1'
    (ruby_home/'bin').mkdir(parents=True)
    gem_home.mkdir(parents=True)
    config['toolchain']['ruby_bin']=str(ruby_home/'bin')
    e=tool_env(config)
    assert e['MY_RUBY_HOME']==str(ruby_home)
    assert e['GEM_HOME']==str(gem_home)
    assert e['GEM_PATH']==os.pathsep.join((str(gem_home),str(gem_home)+'@global'))


def test_maven_downloads_use_approved_host_scoped_credentials(config,tmp_path):
    cmd=Commands({'GRADLE_USER_HOME': str(tmp_path/'gradle')})
    credentials={'maven_username':'build-user','maven_password':'stale-password','npm_token':'private-token'}
    configure_maven_downloads(config,credentials,cmd,tmp_path)
    assert cmd.env['ENTERPRISE_REPOSITORY']==config['maven_repository']
    assert 'build-user' not in cmd.env['ENTERPRISE_REPOSITORY']
    curl_home=tmp_path/'curl'
    assert cmd.env['CURL_HOME']==str(curl_home)
    assert (curl_home.stat().st_mode&0o777)==0o700
    assert ((curl_home/'netrc').stat().st_mode&0o777)==0o600
    assert 'machine artifactory.us.bank-dns.com' in (curl_home/'netrc').read_text()
    assert 'private-token' not in (curl_home/'.curlrc').read_text()
    init_script=(tmp_path/'gradle/init.gradle').read_text()
    assert 'ENTERPRISE_REPOSITORY' in init_script
    assert 'stale-password' not in init_script
    assert ((tmp_path/'gradle/init.gradle').stat().st_mode&0o777)==0o600


def test_only_hermes_mirror_checksum_may_change():
    before='PODS:\n  - hermes-engine (0.81.4)\nSPEC CHECKSUMS:\n  hermes-engine: '+'a'*40+'\n'
    after=before.replace('a'*40,'b'*40)
    accept_hermes_checksum(before,after)
    with pytest.raises(SafeError,match='more than'):
        accept_hermes_checksum(before,after.replace('0.81.4','0.81.5'))


def test_log_redaction_and_subprocess_failure(monkeypatch,capsys):
    monkeypatch.setattr('bento_ci.build.bitrise_processes',lambda:[])
    cmd=Commands(os.environ.copy(),['top-secret-value'])
    with pytest.raises(SafeError):
        cmd.run([sys.executable,'-c','print("top-secret-value");raise SystemExit(7)'])
    output=capsys.readouterr().out
    assert 'top-secret-value' not in output
    assert '[REDACTED]' in output


def test_bitrise_restart_aborts_child(monkeypatch):
    monkeypatch.setattr('bento_ci.build.bitrise_processes',lambda:['bitrise-den-agent'])
    cmd=Commands(os.environ.copy())
    start=time.monotonic()
    with pytest.raises(SafeError,match='Bitrise restarted'):
        cmd.run([sys.executable,'-c','import time;time.sleep(45)'])
    assert time.monotonic()-start<15


def test_window_expiry_and_agent_detection(tmp_path,monkeypatch):
    monkeypatch.setattr('bento_ci.locking.bitrise_processes',lambda:[])
    with pytest.raises(SafeError):check_window(tmp_path)
    open_window(tmp_path,1);check_window(tmp_path)
    monkeypatch.setattr('bento_ci.locking.bitrise_processes',lambda:['agent'])
    with pytest.raises(SafeError):check_window(tmp_path)
    with pytest.raises(SafeError):open_window(tmp_path,1)
    monkeypatch.setattr('bento_ci.locking.bitrise_processes',lambda:[])
    atomic_json(tmp_path/'data/pilot-window.json',{'expires_at':time.time()-1})
    with pytest.raises(SafeError):check_window(tmp_path)


def test_host_lock_is_exclusive_and_released(tmp_path,monkeypatch):
    monkeypatch.setattr('bento_ci.locking.bitrise_processes',lambda:[])
    open_window(tmp_path,1)
    script='import fcntl,sys;f=open(sys.argv[1],"a");fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)'
    with host_lock(tmp_path):
        r=subprocess.run([sys.executable,'-c',script,str(tmp_path/'data/host.lock')],capture_output=True)
        assert r.returncode!=0
    r=subprocess.run([sys.executable,'-c',script,str(tmp_path/'data/host.lock')],capture_output=True)
    assert r.returncode==0


class Store:
    def get(self,*args,**kwargs):return {'token':'private-upload-token'}


def test_local_only_and_artifactory_failure_preserves_files(config,tmp_path,monkeypatch):
    (tmp_path/'app.apk').write_bytes(b'app')
    m={'sha':'a'*40,'pipeline_id':'1','job_id':'2','platform':'android'}
    publish(config,Store(),tmp_path,m)
    assert json.loads((tmp_path/'manifest.json').read_text())['storage']=='local'
    assert m['files'][0]['sha256']==digest(tmp_path/'app.apk')
    config['artifactory'].update(url='https://artifacts.example/artifactory',repository='mobile')
    class Response: status_code=403
    monkeypatch.setattr('bento_ci.build.requests.put',lambda *a,**k:Response())
    with pytest.raises(SafeError,match='403'):publish(config,Store(),tmp_path,m)
    assert (tmp_path/'app.apk').read_bytes()==b'app'
    assert json.loads((tmp_path/'manifest.json').read_text())['upload_status']=='failed'


def test_artifactory_checksum_mismatch_fails(config,tmp_path,monkeypatch):
    (tmp_path/'app.ipa').write_bytes(b'ipa')
    config['artifactory'].update(url='https://artifacts.example/artifactory',repository='mobile')
    class Response:
        status_code=201
    class Metadata:
        status_code=200
        def json(self):return {'checksums':{'sha256':'wrong'}}
    monkeypatch.setattr('bento_ci.build.requests.put',lambda *a,**k:Response())
    monkeypatch.setattr('bento_ci.build.requests.get',lambda *a,**k:Metadata())
    with pytest.raises(SafeError,match='checksum'):publish(config,Store(),tmp_path,{'sha':'a'*40,'pipeline_id':'1','job_id':'2','platform':'ios'})


def test_artifactory_success_paths_and_manifest(config,tmp_path,monkeypatch):
    (tmp_path/'app.apk').write_bytes(b'apk')
    config['artifactory'].update(url='https://artifacts.example/artifactory',repository='mobile')
    seen=[]
    class Response: status_code=201
    class Metadata:
        status_code=200
        def json(self):return {'checksums':{'sha256':digest(tmp_path/'app.apk')}}
    def put(url,**kwargs):
        seen.append(url);assert kwargs['allow_redirects'] is False;return Response()
    monkeypatch.setattr('bento_ci.build.requests.put',put)
    monkeypatch.setattr('bento_ci.build.requests.get',lambda *a,**k:Metadata())
    m={'sha':'a'*40,'pipeline_id':'11','job_id':'22','platform':'android'}
    publish(config,Store(),tmp_path,m)
    assert m['upload_status']=='complete'
    assert '/11/22/android/app.apk' in seen[0]
    assert seen[1].endswith('/manifest.json')


def test_config_rejects_bad_origins_and_partial_storage(config,tmp_path):
    p=tmp_path/'config.json'
    config['public_url']='http://0.0.0.0:8765';atomic_json(p,config)
    with pytest.raises(SafeError):load_config(p)
    config['public_url']='http://localhost:8765'
    config['artifactory']['url']='https://artifacts.example/artifactory';atomic_json(p,config)
    with pytest.raises(SafeError):load_config(p)
