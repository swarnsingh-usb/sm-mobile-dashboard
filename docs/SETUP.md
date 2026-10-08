# Mac setup and prerequisites

For work-Mac code validation, use [WORK-MAC.md](WORK-MAC.md) and `bash setup.sh --work-mac`. That path checks only validation prerequisites. Later, `./bento native-tools` collects the existing native tool paths for actual APK/IPA builds.

For the first SSH build without AWS or GitLab runner setup, follow [LOCAL-TEST.md](LOCAL-TEST.md). The role, Secrets Manager connectivity and service setup below apply to the later AWS-backed dashboard/runner path.

The actual host has no GUI session. Always use `--no-start` during installation; the existing GUI LaunchAgent service installer does not meet this host's requirements. The service/reboot notes below describe that implementation's limitation, not an instruction to enable a desktop. Use foreground commands only for the interactive pilot; unattended headless service deployment is not yet ready.

1. Use a stable directory excluded from Bitrise cleanup/reimaging. Inspect the actual Bitrise agent cleanup policy before choosing it; no installer can preserve files across a host reimage. Leave existing Bitrise directories/services/configurations untouched.
2. Ensure the trusted pilot macOS user can reach GitLab, AWS Secrets Manager (us-west-2), Artifactory npm/Maven, required public package sources, and the mirrored native dependency branch. The installer additionally downloads from PyPI, nodejs.org, registry.npmjs.org, and gitlab-runner-downloads.s3.amazonaws.com. Use approved proxy/mirror access if the bank restricts them; do not disable TLS.
3. Attach the scoped EC2 instance profile through your approved AWS process. Verify IMDS access and AWS network routing. A custom CA bundle must include ordinary public roots as well as bank roots. Java's trust store must trust the bank certificates too; setting a Node/Python CA path does not configure Java.
4. Put secret JSON values in Secrets Manager. See SECRETS.md.
5. Run the one-file installer in an empty directory, or run `bash setup.sh` from an extracted copy.
6. Review `config.json`; configure the existing Xcode path/version, Ruby/CocoaPods executable/version, Android SDK path and JDK home. `config.json` contains references, not secret values. It is private and ignored by source control.
7. Run `./bento doctor --online`. Missing/failed checks exit nonzero. This checks connectivity and tools, not a native build.
8. Connect GitLab and install the generated pipeline through the normal change process, then open a pilot window and run both platforms.

If outbound package downloads pass through a bank TLS-inspection proxy, set `PIP_CERT=/approved/full-ca-bundle.pem` and `REQUESTS_CA_BUNDLE=/approved/full-ca-bundle.pem` before invoking the installer. The initial Python dependency installation happens before the wizard can read your configured CA path. Do not use `--trusted-host`, `--insecure`, or TLS verification bypasses.

## Native tools intentionally reused

| Tool | Required baseline |
|---|---|
| Node / Yarn | Installed privately: 20.19.4 / 1.22.22 |
| Java | Existing JDK 17; set java_home explicitly |
| Android SDK | platform android-36, build-tools 36.0.0, NDK 27.0.12077973 |
| CMake | App declares 4.1.2; validate installed availability with SDK manager and the real Gradle run |
| Gradle | Project wrapper 8.14.3; downloaded into job-private Gradle state |
| Xcode | Existing qualified installation; exact `expected_xcode` is a required setup value |
| Ruby | Existing qualified Ruby >=3.2.1; includes xcodeproj used for target-only signing settings |
| CocoaPods | Existing qualified version; initial config is 1.16.2, verify against the successful Bitrise baseline |

This package never runs `xcode-select --switch`, changes global npm settings, replaces signing identities, upgrades the shared SDK, or deletes global caches. If native components are missing, provision them through the same process used for the working Bitrise Mac and rerun doctor. Automatically changing the shared toolchain while Bitrise still depends on it would defeat the coexistence requirement.

## AWS roles and services

The portal and build jobs use the Python AWS SDK's standard credential chain; with no explicit credentials configured it retrieves the EC2 instance role. Remove inherited personal AWS profiles/credentials from this service user's environment. The build subprocess environment strips explicit AWS access-key variables. The runner's own persistent token is in a mode-0600 config under runtime/.

The installer names its LaunchAgents using a hash of its directory. It does not use or overwrite `gitlab-runner.plist` or any Bitrise plist. Installation is rerunnable via `bash setup.sh`; it preserves existing config, runner registration and artifacts. Starting services restarts this package's portal/runner, so only do it after its jobs drain.

After reboot the GUI user must be signed in. Arrange the session policy with the bank; do not enable automatic login as a hidden installer side effect. Verify recovery with an actual reboot before calling the host ready for unattended work.

Current GitLab guidance: [macOS runner installation](https://docs.gitlab.com/runner/install/osx/). The package pins GitLab Runner 19.4.1. Check the bank's server/runner compatibility policy before rollout; change the pinned version in `install_tools` if the bank requires another qualified version and rebuild/retest the package.
