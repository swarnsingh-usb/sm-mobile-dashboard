# Spend Management Mobile Dashboard — EC2 Mac pilot

An internal build dashboard and GitLab runner package for `https://gitlab.us.bank-dns.com/BENTO/bento.mobileapp`, branch **dev**, AWS **us-west-2 (Oregon)**. It builds Android and iOS on the existing EC2 Mac and keeps outputs on its disk. Artifactory is optional until the repository is known.

**Host constraint: SSH/command line only; no GUI session.** Use the direct-build path below for the first test. View the web dashboard in your laptop's browser through an SSH tunnel. The current `services` implementation uses GUI LaunchAgents and does not meet this host's unattended-service requirement; do not use it on this host. Headless startup/reboot recovery and actual iOS signing still need qualification.

**Milestone 1:** launch `both-dev`, produce a signed APK and a signed IPA from the same GitLab commit, follow logs, and download both from the dashboard. A successful native build is not proof of installation, login, push notifications, or store qualification. Neither platform is submitted to a store or tester service by this package.

## Install from GitHub over SSH

This repository contains the full application. After SSHing into the EC2 Mac, clone it into a stable directory outside Bitrise cleanup paths and run the setup script from its root:

```bash
git clone git@github.com:swarnsingh-usb/sm-mobile-dashboard.git ~/sm-mobile-dashboard
cd ~/sm-mobile-dashboard
bash setup.sh --local --no-start
./bento credentials
```

The Mac needs GitHub repository access to clone this source. Its GitHub credentials are separate from the GitLab credentials used to build the mobile app. `--local` defers AWS Secrets Manager; `--no-start` leaves the GUI-dependent service installer unused. Missing native prerequisites are reported by setup; resolve them and run `./bento doctor` before testing. The credential wizard is interactive and saves credentials only on the Mac, outside source control.

For the app checkout, exclusive Bitrise window, and `./bento test-build both` command, follow [LOCAL-TEST.md](docs/LOCAL-TEST.md). This dashboard repository is separate from the GitLab mobile-app repository.

## Alternative: single-file installer

**Testing first without AWS?** Use the [SSH/local-credentials guide](docs/LOCAL-TEST.md). Copy the installer to the Mac, create an empty directory, and run `bash ~/bento-mac-ci-setup.sh --local --no-start`. Then `./bento credentials` collects local credentials and `./bento test-build both --repo /path/to/gitlab-dev-checkout` runs the builds without configuring GitLab CI or dashboard services. Native tools, dependency access and iOS signing are still required. The original AWS/service setup follows below.

Copy `bento-mac-ci-setup.sh` onto the EC2 Mac. In the empty directory you created for this system, run:

```bash
bash /path/to/bento-mac-ci-setup.sh --local --no-start
```

The single-file installer extracts this entire package and runs a setup wizard. It refuses to overwrite a nonempty directory. To extract and review without installing anything:

```bash
bash /path/to/bento-mac-ci-setup.sh --extract-only
```

Run as a dedicated, trusted non-root macOS build user through SSH. No desktop browser is needed on the Mac. The initial direct-build command does not install a service. GitLab's supported macOS service uses a GUI LaunchAgent, so the package's existing service installer is not the unattended deployment path for this SSH-only host. See [LOCAL-TEST.md](docs/LOCAL-TEST.md).

Setup creates a private Python environment, downloads checksum-verified Node 20.19.4, Yarn 1.22.22 and GitLab Runner 19.4.1, and generates pipeline/IAM templates. With `--local --no-start`, it leaves registration and services untouched and uses local credentials. It uses an existing Python 3.10+, or installs Python 3.12 through an existing Homebrew installation if necessary. No Homebrew auto-upgrade runs.

It reuses the existing JDK 17, Android SDK 36/NDK 27.0.12077973, Xcode and Ruby/CocoaPods. It does **not** install/upgrade Xcode, change the global selected Xcode, accept licenses, replace Bitrise tools, create AWS resources, grant permissions, or push application code. Those require your bank's approved inputs. See [SETUP.md](docs/SETUP.md). Setup exits with code 2 when installed but blocked; rerun `bash setup.sh --local --no-start` after resolving the listed items.

## Required bank-side inputs

1. An EC2 instance role that can read only the configured AWS Secrets Manager secrets, plus `kms:Decrypt` for a customer-managed key if used. Do not run `aws configure` with long-lived access keys.
2. Five JSON secrets: portal login, GitLab API access, GitLab runner authentication, dependency repository credentials, and iOS signing. See [SECRETS.md](docs/SECRETS.md). Android managed signing is optional; the default preserves the repository's dev/debug signing.
3. A project-scoped, protected GitLab runner tagged `bento-mac-pilot`, locked to this project with untagged jobs disabled. The `dev` branch must be protected. See [GITLAB.md](docs/GITLAB.md).
4. Review and commit `generated/gitlab-ci.yml` as the application's `.gitlab-ci.yml` on `dev`, or merge it with existing GitLab configuration. The installer cannot infer or override existing bank pipelines.
5. The mirrored `auth-26-06-cocoapods` branch, private dependency access, and a complete trusted CA bundle where needed.

## Later interactive dashboard pilot

For the first direct build, follow LOCAL-TEST.md. For a later interactive dashboard pilot after setup, credentials and committing the pipeline, check `./bento doctor --online`. Run the following foreground commands in two separate SSH sessions, each from the installation directory:

```bash
# SSH session 1
./bento serve
# SSH session 2
./bento runner
```

Keep both sessions open. These foreground commands are not a reboot-persistent service or proof of headless iOS signing. Automatic startup/recovery remains unfinished; do not use the GUI-dependent `./bento services start` on this host.

Drain Bitrise's queue and temporarily stop its Mac agent through your existing operating procedure. Disable any agent auto-restart for the pilot window. Keep its installation and configuration intact. Then:

```bash
./bento open-window --minutes 240
```

On your laptop, open another terminal and create an SSH tunnel:

```bash
ssh -N -L 8765:127.0.0.1:8765 MAC_USER@MAC_ADDRESS
```

Open `http://localhost:8765` in your laptop's browser and sign in using the portal credential. The application binds only to loopback on the Mac; the Mac needs no browser or GUI. For a shared internal URL, configure a bank-managed HTTPS reverse proxy and update `public_url`; do not expose port 8765 directly. See [OPERATIONS.md](docs/OPERATIONS.md).

Choose **Android + iOS · dev**, then **Run workflow**. The jobs run sequentially on the Mac and use fresh GitLab checkouts. Their logs remain in GitLab; files and manifests appear under `data/artifacts/<pipeline>/<job>/<platform>/` and are downloadable in the portal.

After all new jobs finish:

```bash
./bento close-window
```

Check GitLab for running/pending pilot jobs, cancel pending jobs if needed, and resume Bitrise through your normal procedure. Window expiry/closure prevents new build starts; it does not kill a running job. **Do not resume Bitrise until running pilot jobs have finished.** No cutover or removal occurs automatically on October 9.

## What is included

| Component | Included behavior |
|---|---|
| Dashboard | Sign-in, dev workflow selection, recent pipelines, jobs, updating logs, cancel, retry, local downloads and Artifactory links |
| Workflows | `both-dev`, `android-dev`, `ios-dev`, `validate-dev` |
| Android | Explicit usbank/dev config; releaseStaging APK; current debug key or optional Secrets Manager key; package/signature verification |
| iOS | Existing staging app identity with dev config; locked pods; temporary certificate keychain/profiles; archive, IPA and dSYMs; signature checks |
| Credentials | Local private JSON for initial tests, or AWS SDK/instance role; temporary build credentials removed on normal exit/cancellation |
| Storage | Local files by pipeline/job/platform; optional checksum-verified Artifactory binary uploads; failed upload remains a failed job |
| Coexistence | Manual exclusive window, new-system host lock, recognized Bitrise process guard, no existing Bitrise service changes |
| Recovery | iOS keychain/profile cleanup journal; old artifacts retained; independent local download endpoint |

This is a **bounded pilot**, not full Bitrise workflow parity. Production, all legacy brand workflows, automatic branch/MR triggers, TestFlight/Firebase distribution, shared release numbering, SSO and a shared two-scheduler admission controller are later steps.

## Repository baseline and verification limits

Code was derived from local `bento.mobileapp` dev commit `a81bb52e72c6f2705488030f8641ea90d47fedf2` (2026-09-14). A fresh GitHub fetch on 2026-09-24 returned `Repository not found`. The bank GitLab checkout, EC2 host, IAM, signing assets, native compilation, and Artifactory service were **not accessed or qualified here**. The first real native builds must run in the bank environment.

See [VERIFICATION.md](docs/VERIFICATION.md) for local tests and the live acceptance checklist. See [WORKFLOW-MAP.md](docs/WORKFLOW-MAP.md) for current versus migrated workflow behavior.
