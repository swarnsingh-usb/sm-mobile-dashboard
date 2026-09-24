# Verification record

Verified locally 2026-09-24. Bank-host qualification remains pending.

Host clarification: the EC2 Mac is SSH-only with no GUI session. Documentation now uses local/no-start installation and foreground commands for the interactive pilot. The existing GUI LaunchAgent service implementation does not meet the unattended host requirement. No headless startup, reboot recovery, or signed iOS build has been qualified. The GitHub/local-worker update adds a foreground execution path suitable for SSH, but unattended startup and native SSH signing still need target-host qualification.

## Local checks

**54 tests passed.** See `test-results.txt` for the actual local run. The tests cover API authentication/CSRF, launch idempotency including uncertain upstream responses, pipeline scoping, local artifact access/path checks, Artifactory upload success/failure/checksum mismatch, secret redaction, failed commands, Bitrise restart cancellation, window expiry, exclusive lock release, runner registration/token handling, generated pipeline gates, iOS profile/temporary-keychain cleanup and restoration failure.

The nine added local-mode tests verify that AWS client creation is forbidden, local credentials refresh from disk, permissive files/symlinks/invalid schemas are rejected without exposing values, signing assets load by path, switching modes preserves configuration, the credential wizard keeps private fields out of output, local setup never registers/starts services, and SSH tests preserve the source checkout while selecting the same commit for both platforms. Actual temporary Git repositories exercise copying and commit selection; native compilation is stubbed. Failure records and the existing CI/protected-dev/window guards are also checked.

Additional checks passed: Python compilation, JavaScript syntax, shell syntax, Ruby helper syntax, and Python dependency consistency. The actual downloaded runner's help confirms that registration uses `CI_SERVER_TOKEN`; tokens do not appear in process arguments.

The self-extracting installer was tested in an empty scratch directory. All packaged source checksums matched. A nonempty destination was refused without overwrite. A deliberately corrupted payload was rejected before extraction. A separate clean-directory setup installed the isolated environment and all three pinned tools, and generated CI/IAM files with `--no-start`. It exited 2 as intended because this development Mac lacks the required JDK/Android SDK and qualified Ruby/Xcode settings. See `installer-test-results.txt`. No runner registration or background service was started by that test.

The revised installer was rechecked for extraction/checksums, nonempty-directory refusal, corruption detection, source-only ZIP contents, and forwarding `--local --no-start` to setup using a stub. See `local-installer-test-results.txt`. The earlier full dependency installation check was not repeated for this flag/credential change.

Browser checks passed against the explicit synthetic preview fixture: sign-in, default Android+iOS selection, launch, cancel, run/job details, log display, and APK download event. API tests verify download bytes and access restrictions. The synthetic preview server is not a build backend and must never be used as the deployed application.

Branding update: renamed the UI to Spend Management Mobile Dashboard and reused the mobile app's U.S. Bank logo/palette. Visually checked the sign-in page and authenticated dashboard with synthetic build details/logs; browser inspection confirmed the logo loads and the button/heading palette and title match. The local asset is included in the installer. This cosmetic update does not change build or service behavior.

These tests use fake GitLab/Secrets Manager/Artifactory responses. They do not demonstrate real bank integration or native builds. A browser check verifies the dashboard interaction with synthetic runs; no bank service is called.

To reproduce local tests: create a Python 3.10+ virtualenv, install `requirements-dev.txt`, and run `python -m pytest -q`. Native tools and cloud credentials are not required by these unit/integration-fixture tests.

## GitHub source and local worker update

On 2026-09-24 the new source checker read GitHub dev `87da4ea430ae` and native dependency branch `3bcb0eaeeb2b` using the work SSH identity from this developer laptop. The app checkout was fast-forwarded for review; native build/patch files did not change. EC2 networking and native builds were not tested.

The 21 added tests cover legacy config defaults, preserving GitLab settings/secrets while switching sources, SSH without source tokens, HTTPS helper host/repository scoping, frozen iOS dependency revisions across providers, GitHub credential prompts, source-origin validation, no GitLab runner installation/registration in local mode, dashboard credential setup, and the durable local worker. Real temporary Git repositories test fetching/pinning dev, per-platform checkout isolation, source advancement between platforms, fail-fast/cancel/window expiry, worker exclusivity, interruption guard, persisted UI history/logs/downloads and reuse of the same worker with GitLab source. Native compilation and external uploads remain mocked. Local mode forbids AWS initialization, and local-backend API tests forbid GitLab API construction.

Browser checks using `tests/preview.py --local` confirmed GitHub labeling, local run/job details, synthetic logs and artifact links, launch into the real local queue, and queued cancellation without a GitLab backend. The preview is explicitly synthetic and does not execute builds. Existing GitLab-backend tests continue to pass.

The packaged installer was refreshed and checked for source integrity, nonempty-directory refusal, corrupted-payload rejection, private-file exclusion, and forwarding `--local --no-start --source github`. See `github-installer-test-results.txt`. Pinned dependency installation was not repeated; dependencies are unchanged.

## Required live SSH test acceptance

- Local dependency/selected-source credentials and signing files work on the EC2 Mac; no Secrets Manager access is required.
- `./bento doctor` passes the actual native tool checks; Bitrise is drained/stopped for the window.
- `./bento run both` produces both native outputs from one fetched dev commit, or `./bento test-build both --repo /path/to/separate-checkout` uses a reviewed local dev commit.
- Validate/download both artifacts, inspect manifests and logs, confirm keychain/profile restoration and the unchanged source checkout, then resume the existing Bitrise baseline.
- A signed iOS build from the SSH session has been proven on the target host; local fixtures do not establish this.

## Additional live dashboard/runner acceptance

- Bank GitLab dev and the locked native dependency branch/commits are reachable.
- Scoped instance-role access works; secret schemas, CA trust and native tool versions are valid.
- The new runner is protected/tagged/locked to this project; generated pipeline passes GitLab CI lint on the bank version.
- Bitrise is drained/stopped for the exclusive window; its original configuration is preserved.
- Launch both-dev. Both jobs build the exact recorded SHA and explicit usbank/dev environment.
- Android APK package ID, version/build and signature are verified; iOS archive/export identity/signature/profile/entitlements are appropriate.
- Download both outputs and compare local SHA-256 with manifest. If Artifactory is enabled, confirm server checksum and access permissions.
- Verify failure/cancellation, keychain restoration, expired-window blocking, artifact survival and an approved SSH-only service/reboot recovery design.
- Resume Bitrise and run its known baseline to demonstrate coexistence has preserved its behavior.

Device installation, login, push, deep links, performance and production/store qualification follow as separate evidence.
