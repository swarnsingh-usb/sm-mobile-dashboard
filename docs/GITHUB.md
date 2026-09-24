# GitHub source on the SSH-only EC2 Mac

Use **BentoInc/bento.mobileapp on GitHub** while bank GitLab DNS is blocked. The dashboard repository (`swarnsingh-usb/sm-mobile-dashboard`) is a different repository. GitHub mode uses a local Mac worker for manual dev builds, history, logs, cancellation and downloads. It needs no GitHub Actions, GitLab API, runner registration or pipeline commit.

GitLab settings and credentials remain saved. Source selection (`source_provider`), orchestration (`build_backend`) and credential storage (`credential_source`) are independent. Existing installations retain their GitLab behavior until explicitly switched. There is no automatic fallback on source failure.

## Update your existing installation

After any active dashboard/worker builds finish, run in the dashboard directory on EC2:

```bash
git pull --ff-only
./bento configure --local
./bento source github
./bento check-source
```

If setup was interrupted before creating `config.json` or the Python environment, use this after pulling:

```bash
bash setup.sh --local --no-start --source github
./bento check-source
```

Setup preserves existing tool paths and GitLab settings. GitHub mode skips GitLab prompts, runner installation, registration and service startup. Missing native prerequisites still cause exit 2 after installation; correct them and rerun `./bento doctor`.

`check-source` reads `dev` and `auth-26-06-cocoapods` from the selected remote. It needs no build window and makes no AWS requests in local credential mode. Run it on the **EC2 Mac** as the build user. Branch access alone does not prove every locked native dependency is fetchable or buildable.

## GitHub authentication

SSH is the default, using the Mac user's SSH configuration/agent with noninteractive authentication and strict host verification. The key must have access to **BentoInc/bento.mobileapp**, including any required organization SSO authorization. Dashboard repository access alone is insufficient. The package does not extract Bitrise credentials.

To select an existing private key explicitly:

```bash
./bento source github --ssh-key ~/.ssh/YOUR_WORK_KEY
./bento check-source
```

An explicit key ignores other SSH configuration identities, avoiding accidental personal-account selection. For an encrypted key, load it into your approved SSH agent and run the worker in that environment. No key contents are stored in configuration.

If host verification fails, connect interactively using `ssh -T git@github.com` and verify GitHub's published fingerprint before accepting a new key. For an explicit identity, include `-F /dev/null -i ~/.ssh/YOUR_WORK_KEY -o IdentitiesOnly=yes`. A successful GitHub SSH greeting exits 1; that is normal. Follow [GitHub's SSH guidance](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/testing-your-ssh-connection). Test app access using the same identity:

```bash
git ls-remote git@github.com:BentoInc/bento.mobileapp.git refs/heads/dev refs/heads/auth-26-06-cocoapods
```

Optional HTTPS authentication:

```bash
./bento source github --transport https
./bento credentials
./bento check-source
```

The wizard then asks for `github.token`, an approved repository-read token. A temporary [Git credential helper](https://git-scm.com/docs/gitcredentials) scopes it to the configured repository; URLs never contain tokens. GitLab credentials are preserved and not read in GitHub mode. Return to SSH with `./bento source github --transport ssh`.

## First Android and iOS build

```bash
./bento credentials
./bento doctor --online --build-only
```

With GitHub SSH, the wizard needs npm/Maven credentials and existing iOS signing assets only. AWS Secrets Manager can wait. Android keeps its configured signing behavior. A terminal build needs no dashboard login.

**The app still downloads private npm/Maven dependencies from `artifactory.us.bank-dns.com`.** If that host is blocked too, the Cloud admin must restore access. Keep certificate verification enabled. Optional artifact *uploads* can stay disabled independently.

Drain Bitrise and temporarily stop its agent through your existing procedure, preventing auto-restart during the pilot. Keep its installation and configuration. Then:

```bash
./bento open-window --minutes 240
./bento run both
```

This fetches remote `dev`, pins one SHA, and builds Android then iOS in separate disposable checkouts. No manual app clone is needed. Private iOS CocoaPods dependencies use the chosen provider, preserving locked commits and deployment-mode installation. The first failure stops the run; later jobs are skipped. `./bento run android` or `./bento run ios` starts a new attempt at current remote dev.

Logs/status remain in `data/local-runs/<run-id>/`; binaries/manifests remain in `data/artifacts/<run-id>/<job-id>/<platform>/`. Records include provider, repository and SHA. The existing Artifactory uploader is used only when configured. No store, TestFlight or Firebase release is made.

The older `./bento test-build both --repo /path/to/separate/checkout` also accepts the selected GitHub or GitLab origin. It uses committed **local** dev without fetching. Those logs stay under `data/test-runs/` rather than remote-run history; their artifacts appear in the dashboard's local artifact list.

## Dashboard without GitLab

Set the local login once:

```bash
./bento portal-credentials
```

Keep these foreground commands open in **two separate SSH sessions**, both in the dashboard directory:

```bash
# Session 1
./bento serve
# Session 2
./bento worker
```

From your laptop, use your normal EC2 SSH destination:

```bash
ssh -N -L 8765:127.0.0.1:8765 MAC_USER@MAC_ADDRESS
```

Open `http://localhost:8765` on your laptop. Keep `public_url` at `http://localhost:8765` for this tunnel. The Mac needs no GUI. Launch workflows while the pilot window is open. Queued runs wait for the worker; it serializes all jobs, saves redacted logs and exposes native artifacts.

**Cancel run** requests cancellation; active child commands check about every two seconds, then cleanup runs before the worker proceeds. Use **Run workflow** for a new attempt, which resolves current dev again. Local jobs are never silently retried. Either use the worker/dashboard or `./bento run`; a second worker or terminal run is refused while the first worker is active.

These foreground SSH sessions are an interactive pilot, not reboot-persistent hosting. No service or GitLab runner starts automatically. An approved terminal multiplexer can preserve sessions; unattended startup remains unqualified.

When finished, `./bento close-window` prevents new starts. Cancel queued work, allow active jobs and signing cleanup to finish, and stop the idle worker before resuming Bitrise. Window closure does not kill a running platform but blocks subsequent starts. The existing process guard aborts a pilot if the recognized Bitrise agent restarts.

## Switch back to GitLab

After all active/pending runs finish or are canceled, stop the idle dashboard and worker. Once GitLab networking, CA trust and repository-read credentials are ready:

```bash
./bento source gitlab
./bento credentials
./bento check-source
```

This keeps the **same local Mac worker**, switching app and native dependency sources to the saved GitLab URLs. Restart `serve` and `worker`. Build/signing/storage behavior stays the same. Verify a real build and the mirrored native branch/locked commits before calling the swap qualified.

The original GitLab pipeline integration remains available through `./bento source gitlab --backend gitlab`. It needs the protected runner, API token and pipeline in [GITLAB.md](GITLAB.md), and uses `./bento runner` instead of `worker`. Its unattended headless service requirement remains unresolved. Automatic webhooks and PR builds are outside this manual dev milestone.

## Interrupted worker recovery

Normal cancellation, Ctrl-C, SIGTERM and SSH hangup record a result and attempt process/signing cleanup. SIGKILL or host crashes cannot clean up. If `data/local-worker-active.json` remains, new workers refuse to start.

1. Keep Bitrise stopped. Inspect the recorded run ID/PID, logs and running native/Git processes. Ensure the interrupted build and its children have stopped; a PID alone does not establish identity after reboot.
2. Open a pilot window and run `./bento recover-signing` if `data/signing-recovery.json` exists. Confirm restoration before proceeding.
3. Record that run as `failed` in `data/local-runs/<id>/run.json`, mark running jobs failed and pending jobs skipped, and add an explanatory `error`. Partial outputs do not prove success.
4. After inspection and cleanup, remove only `data/local-worker-active.json`. Restart the worker and launch a new run. Logs/artifacts remain; interrupted runs are not automatically replayed.

An authorized operator must inspect an unclean interruption; the package cannot establish orphan-process safety after a host crash.
