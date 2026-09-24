# First build over SSH, without AWS Secrets Manager

This path runs Android and iOS builds directly from the Mac terminal. It needs no AWS secrets, EC2 role, GitLab runner registration, pipeline commit, or dashboard login. It still needs access to the private dependency repositories and the existing iOS signing certificate/private key and provisioning profiles. Use the approved credentials you already have; this package does not extract them from Bitrise.

## 1. Copy the installer from your laptop

**If using GitHub instead of copying the installer:** after SSHing into the Mac, run the following and then continue at step 2:

```bash
git clone git@github.com:swarnsingh-usb/sm-mobile-dashboard.git ~/sm-mobile-dashboard
cd ~/sm-mobile-dashboard
bash setup.sh --local --no-start
```

Use `~/sm-mobile-dashboard` wherever this guide later shows `~/bento-buildroom`. The Mac needs access to this dashboard repository on GitHub and separately to the app source on GitLab. Run `setup.sh` from the clone; the self-extracting installer is only for an empty destination directory.

**If copying the single-file installer directly:**

Replace `MAC_USER@MAC_HOST` with your normal SSH destination. If SSH needs a key, add the same `-i /path/to/key.pem` option to both commands. An existing SSH alias works too.

```bash
scp /Users/swarn/Desktop/SM/output/mac-ci/bento-mac-ci-setup.sh MAC_USER@MAC_HOST:~/
ssh MAC_USER@MAC_HOST
```

On the Mac, use an empty directory outside all Bitrise cleanup/reimaging paths:

```bash
mkdir -p ~/bento-buildroom
cd ~/bento-buildroom
bash ~/bento-mac-ci-setup.sh --local --no-start
```

The setup wizard asks for existing tool locations and any bank CA bundle. `--local` selects local credential files and leaves services stopped. `--no-start` makes that intent explicit. The credential loader creates no AWS SDK client and makes no AWS credential or Secrets Manager requests. The installer still downloads public tools and packages. Native tools are reused, not upgraded. Missing native tools are reported with exit code 2; the package is still installed. Correct `config.json` and rerun `./bento doctor` after fixing prerequisites.

Already extracted/installed? Run `bash setup.sh --local --no-start`. To change an existing installation's credential source alone, run `./bento configure --local`; other settings and credentials are preserved.

## 2. Enter credentials on the Mac

```bash
./bento credentials
```

The interactive wizard collects npm/Maven access, a GitLab token, and iOS signing inputs. Passwords and tokens use hidden prompts. It writes `local-secrets.json` beside `config.json` with owner-only permissions (0600). Certificate/profile inputs are file paths on the Mac; you do not need to base64-encode them. Relative paths are relative to the credential file, not the app checkout. The credentials and signing files are excluded from the packaged installer and ignored by source control.

For the direct test, GitLab repository-read access is sufficient for the mirrored CocoaPods branch. The later dashboard needs the broader project API access described in SECRETS.md. No runner or portal credential is needed yet.

For iOS, provide the same approved certificate **including its private key**, the P12 password, matching provisioning profiles, Apple team ID, exact signing identity and export settings used for this app. If you have a working `ExportOptions.plist`, supply its path; otherwise the wizard asks for the export method and app profile name. For app extensions, supply all profiles and use an export plist mapping every bundle ID. Set the export method supported by the installed Xcode and matching these profiles. Nothing is sent to Apple or a store.

Local schema (placeholders only):

```json
{
  "dependencies": {"npm_token": "REPLACE", "maven_username": "REPLACE", "maven_password": "REPLACE"},
  "gitlab": {"token": "REPLACE"},
  "ios": {
    "p12_file": "/private/path/signing.p12",
    "p12_password": "REPLACE",
    "profile_files": ["/private/path/stage.mobileprovision"],
    "team_id": "REPLACE",
    "signing_identity": "Apple Distribution: YOUR_EXISTING_IDENTITY",
    "export_options": {
      "method": "release-testing",
      "destination": "export",
      "teamID": "REPLACE",
      "signingStyle": "manual",
      "provisioningProfiles": {"com.usbank.SpendManagement.staging": "EXACT_PROFILE_NAME_OR_UUID"}
    }
  }
}
```

This is plaintext on the Mac's disk, protected by file permissions; it is temporary local storage, not a vault. Keep signing assets in a private directory and restrict the P12/keystore to their owner. Do not put secrets in commands, chat, or the app repository. Local mode requires the JSON to be owned by the build user with no group/other permissions and refuses symlinks. The wizard preserves optional fields on reruns. Android defaults to the repository's dev/debug key; see SECRETS.md for optional managed signing.

## 3. Make a separate GitLab source checkout

```bash
git clone --branch dev https://gitlab.us.bank-dns.com/BENTO/bento.mobileapp.git ~/mobileapp-pilot-source
```

Use your approved Git authentication when prompted; do not embed tokens in the URL. If required, set `GIT_SSL_CAINFO` to the complete trusted bank/public CA bundle before cloning. An existing separate GitLab clone also works. The command below builds its **committed local `dev` branch**. It does not fetch automatically or include uncommitted changes. To refresh that source later, use your normal reviewed Git update procedure before testing. Do not point it at Bitrise's working directory.

## 4. Run the first native build

Check tools, drain Bitrise's queue and temporarily stop its agent through the existing operating procedure. Prevent automatic agent restart during the window; leave its installation intact.

```bash
cd ~/bento-buildroom
./bento doctor
./bento open-window --minutes 240
./bento test-build both --repo ~/mobileapp-pilot-source
```

Android and iOS use the same commit, sequentially, each in a disposable copy. The new system's lock, Bitrise process guard, temporary signing keychain and cleanup apply here too. No CI variables are forged and the GitLab job protections remain in place. A window expiring during Android prevents the subsequent iOS start; it does not kill the running Android job. Open a sufficient window (maximum 480 minutes) or rerun an individual platform after renewing it.

The first failure stops the command and returns a nonzero status; it never labels the other platform successful. Run `./bento test-build android --repo ~/mobileapp-pilot-source` or `ios` to retry one platform. Every attempt gets a new run ID. Keep SSH connected while it runs, or use an already approved terminal multiplexer such as tmux on the Mac. An interrupted session may require `./bento recover-signing` before another build.

Artifacts and redacted logs stay on **Mac disk**:

- `data/artifacts/<run-id>/<job-id>/android/` — APK, mapping when generated, manifest/checksums.
- `data/artifacts/<run-id>/<job-id>/ios/` — IPA, Xcode archive, dSYMs, manifest/checksums.
- `data/test-runs/<run-id>/` — platform logs and `run.json`, including commit and result.

`source: local` distinguishes these manifests from GitLab pipelines. When enabled later, the dashboard's local-download list can show these files, but SSH test runs/logs are not GitLab pipelines and do not appear in its pipeline history. Artifactory uploads remain off by default; if configured, this command uses the same uploader as CI.

Run `./bento close-window` after tests finish. If a signing recovery journal remains, complete recovery before resuming Bitrise. Do not restart Bitrise while a pilot build or signing cleanup is still active.

You can copy results back using your SSH connection, for example:

```bash
scp -r MAC_USER@MAC_HOST:~/bento-buildroom/data/artifacts ./bento-build-artifacts
```

The target Mac is **SSH-only, with no GUI session**. The terminal build command does not install a LaunchAgent or invoke graphical applications. It uses Xcode's command-line tools and explicitly unlocks a temporary signing keychain. Successful noninteractive iOS signing must still be proven in the actual SSH session; do not infer it from local tests. The package's existing `services` command requires a GUI session and must not be used on this host. [GitLab's supported macOS service mode](https://docs.gitlab.com/runner/install/osx/) has that requirement; unattended headless operation needs a revised and qualified service design, not instructions to open an unavailable desktop.

## 5. Enable the UI or switch to AWS later

The dashboard/runner also support the local provider. Add `portal` and `runner` objects to `local-secrets.json`, using the JSON schemas in SECRETS.md, and provide an API-capable GitLab token. Follow GITLAB.md to register the protected runner and install the generated pipeline. For an interactive pilot, run `./bento serve` and `./bento runner` in separate SSH sessions and keep them open. View the dashboard in your laptop's browser using the SSH tunnel shown in README.md. These foreground commands do not provide unattended startup/reboot recovery or prove headless signing. Local credential section names are fixed (`portal`, `runner`, `gitlab`, `dependencies`, `ios`, optional `android`/`artifactory`); AWS reference names in `config.json` do not rename them.

For optional Android signing, enable `secrets.android` with a nonempty label and add an `android` object containing `keystore_file`, `store_password`, `key_alias`, and `key_password`. For optional artifact uploads, configure the URL/repository, set `secrets.artifactory` to a nonempty label, and add an `artifactory` object with `token`.

To move to AWS Secrets Manager, provision the instance role/secrets using SECRETS.md, convert file inputs to its documented base64 schema, set `credential_source` to `aws` in `config.json`, and confirm the secret references. Restart this package's services after its builds drain. Verify `./bento doctor --online` and a real build before removing the local credential file/signing assets. The application never silently falls back between providers.
