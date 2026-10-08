# First validation on your work Mac

Use a fresh dashboard clone on the Mac that already reaches bank GitLab. Run the setup script once, then launch **Code checks · dev**. It fetches GitLab `dev` into a disposable checkout, installs locked npm dependencies and the app's patches, selects `usbank/dev`, and runs TypeScript, lint and Jest. Logs and results stay in the dashboard folder.

Validation does not compile Android/iOS, produce an APK/IPA, or upload to TestFlight, Firebase or a store. Native builds are a separate next step below.

## Clone and set up

The dashboard source is on GitHub; the mobile app source is on bank GitLab. Your Mac needs access to both, plus the private npm registry and public setup/package downloads. Have your npm Artifactory token ready. Validation needs no Maven credentials, iOS signing assets, AWS account or GitLab Runner.

```bash
mkdir -p ~/MobileBuildPilot
cd ~/MobileBuildPilot
git clone --branch codex/work-mac-validation git@github.com:swarnsingh-usb/sm-mobile-dashboard.git
cd sm-mobile-dashboard
bash setup.sh --work-mac
```

The `codex/work-mac-validation` branch contains this setup path. Use your work GitHub SSH identity when cloning if you have separate personal/work accounts.

Setup installs its Python environment, Node 20.19.4 and Yarn 1.22.22 inside the clone. It requires Python 3.10+; if missing, the existing installer can use an installed Homebrew to install Python 3.12. It prompts for:

1. GitLab host and app project, defaulting to `https://gitlab.us.bank-dns.com` and `BENTO/bento.mobileapp`.
2. Your existing GitLab access method: `ssh` for your SSH configuration/agent, `https` for your saved Git credential helper, or `token` to supply a repository-read token privately.
3. A full bank/public CA bundle, if your network requires it.
4. The private npm registry token, entered with a hidden prompt.
5. A dashboard username and password (at least 24 characters).

Setup checks Git, the pinned Node/Yarn versions, 40 GiB free space, local credentials and access to GitLab `dev`. It leaves native tool paths and signing for later. A successful setup confirms prerequisites; npm access and code checks are exercised by the validation run itself.

For SSH, use a key already authorized for the app repository and load any passphrase-protected key into your normal SSH agent. The host must already be trusted in your SSH configuration. A custom SSH port can be configured there. For saved HTTPS authentication, make sure a normal Git fetch works without prompting; being signed into GitLab in a browser alone is insufficient. Access to only the dashboard repository is also insufficient.

If the initial package download needs your bank CA, set `PIP_CERT` and `REQUESTS_CA_BUNDLE` to the approved full PEM bundle before running setup. This download happens before the configuration prompts. Keep TLS verification enabled.

Setup returns a nonzero exit code and names the missing prerequisites when blocked. Fix those items and rerun the same script; existing configuration and secrets are preserved. For a configured clone, these commands adjust GitLab authentication:

```bash
# Existing SSH configuration/agent
./bento source gitlab --backend local --transport ssh

# Or saved HTTPS Git credentials
./bento source gitlab --backend local --transport https --auth existing

# Or a token supplied privately to the wizard
./bento source gitlab --backend local --transport https --auth token
./bento credentials --validation
```

Do not paste credentials into command arguments, Git URLs or chat. Local credentials are stored in the ignored, owner-only `local-secrets.json`; existing Git credentials are used through Git itself, not copied into that file.

## Run validation in the terminal

From the dashboard folder:

```bash
./bento open-window --minutes 240
./bento run validate
```

The run pins the current remote `dev` commit and leaves your other app checkouts unchanged. The first failed step stops the run and returns a nonzero exit code. Successful output says **Code checks passed**, with the run ID and log folder. Results are recorded under `data/local-runs/<run-id>/`, including `run.json`, `checkout.log` and `validate.log`.

`open-window` enables starts for four hours; it does not start a build itself. This safeguard also applies on a laptop. If Bitrise is not installed/running on this Mac, there is no agent to stop. Any recognized active Bitrise process still blocks starts.

To inspect the run in your browser:

```bash
./bento serve
```

Open **http://localhost:8765**, sign in with the credentials you chose, select the run, then select the **validate** job to read its logs. No SSH tunnel is needed on your own Mac. Keep this terminal open; Ctrl-C stops the dashboard.

## Launch validation from the dashboard instead

Open the build window, then keep `./bento serve` running in one terminal and `./bento worker` in a second terminal, both in this clone. Open **http://localhost:8765**, choose **Code checks · dev** (the work-Mac default), and click **Run workflow**. The worker processes queued runs and the dashboard shows the result/logs.

Use either the terminal `./bento run validate` or the dashboard's worker for a run; the package permits only one active worker. Setup does not install background services or start native builds automatically.

When finished, wait for the run to finish, run `./bento close-window`, and stop the idle worker/dashboard with Ctrl-C. Window closure prevents new starts; it does not cancel an active run.

## After validation: actual Android/iOS builds

Run `./bento native-tools` to configure the existing Xcode, JDK 17, Android SDK/NDK, Ruby and CocoaPods locations described in [SETUP.md](SETUP.md). Then run:

```bash
./bento credentials
./bento doctor --online --build-only
./bento open-window --minutes 240
./bento run both
```

The full credential wizard collects Maven access and iOS signing assets. The full preflight also checks native tools and the `auth-26-06-cocoapods` dependency branch. Use `android` or `ios` instead of `both` to launch an individual platform after its prerequisites are ready. Native outputs remain on Mac disk by default. Work-Mac success still needs separate networking, signing and unattended-operation verification on the eventual EC2 Mac.
