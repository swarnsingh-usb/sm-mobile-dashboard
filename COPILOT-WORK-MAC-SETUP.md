# Copilot: install the Spend Management Mobile Dashboard on my work Mac

When the user asks you to follow this file, carry out the setup and first validation below. Do the work and check the result; do not stop after describing a plan. Use the existing scripts and continue routine setup and troubleshooting without repeated confirmation. Ask only for missing information, credentials entered privately, or an action that needs the user's account or operating-system interaction.

## Goal and scope

Install everything required for the first **Code checks · dev** run in a fresh dashboard folder on the user's work Mac. Use the bank's GitLab as the mobile app source, a local Mac worker, and local credentials. Run validation, then make its result and logs available in the dashboard.

This first run installs locked JavaScript dependencies and the repository's patches, selects the `usbank/dev` environment, and executes TypeScript, lint and Jest. It does not build an APK/IPA or distribute an app. Full Android/iOS builds are the next phase, after their native tools and signing inputs are available.

Read [docs/WORK-MAC.md](docs/WORK-MAC.md) before execution. Use [docs/SETUP.md](docs/SETUP.md) for native tool requirements only if native builds are requested later. Inspect the current scripts if their behavior differs from this guide; report the difference instead of guessing.

## 1. Establish the installation folder

- Run on the user's work Mac, as their normal user, without `sudo` for the dashboard installer.
- If this is already the dashboard clone, use this folder. Otherwise use the fresh folder below, or the user's chosen location. Keep build checkouts, temporary files, caches and results inside the dashboard installation.
- Dashboard source: `git@github.com:swarnsingh-usb/sm-mobile-dashboard.git`.
- Setup branch: `codex/work-mac-validation`.
- Mobile app source: GitLab host `https://gitlab.us.bank-dns.com`, project `BENTO/bento.mobileapp`, branch `dev`; confirm any user-provided replacement values.
- GitLab app access and GitHub dashboard access are separate. Use the user's existing work identities; do not change their global Git/SSH configuration or assume their personal GitHub identity has access.

For a new installation:

```bash
mkdir -p ~/MobileBuildPilot
cd ~/MobileBuildPilot
git clone --branch codex/work-mac-validation git@github.com:swarnsingh-usb/sm-mobile-dashboard.git
cd sm-mobile-dashboard
```

Run subsequent commands from that dashboard folder. If the destination already exists, inspect its origin, branch and working-tree state. Reuse the correct clone and preserve local edits, configuration, credentials and run history. Never reset or overwrite an unrelated folder. If the correct branch is clean and needs updating, use a fast-forward-only pull. Confirm `bento_ci/cli.py` contains `--work-mac` before setup.

## 2. Check prerequisites and collect only missing inputs

Check macOS, Git availability and free disk space. The package requires **40 GiB free** for its current build guard. Reuse existing Git and macOS Command Line Tools; full Xcode is not required for code validation. Git operations must work without an interactive login prompt.

The installer needs Python 3.10+ and creates a private `.venv`. If suitable Python is missing and Homebrew is already installed, the script installs Python 3.12 through that Homebrew. If neither is available, help the user install Python through their company's supported software-installation path, then resume. An explicit interpreter can be selected through `BENTO_PYTHON` using its real executable path. Do not replace the system Python or install packages into it.

The script installs private Node **20.19.4** and Yarn **1.22.22** under `tools/`; do not independently upgrade global Node/Yarn. Package downloads need access to PyPI, nodejs.org and registry.npmjs.org. App dependencies additionally need the bank's Artifactory npm registries.

Identify the GitLab access method the work Mac already uses:

- **SSH:** reuse the SSH configuration/agent. A passphrase-protected key must be loaded in the user's agent and the host must already be trusted. The wizard accepts an optional key path; prefer the existing SSH configuration when it contains custom routing or ports.
- **Saved HTTPS:** reuse the user's Git credential helper. Browser login alone is insufficient; a Git fetch must work without prompting.
- **Token:** if the existing access methods are unavailable, let the user enter an authorized repository-read token in the hidden wizard prompt.

Have the user enter their **npm Artifactory token** privately during setup. GitLab repository access does not supply private npm access. Maven credentials and iOS signing files are unnecessary for this first validation.

If bank TLS inspection requires a CA bundle, obtain its existing approved full PEM path. Set `PIP_CERT` and `REQUESTS_CA_BUNDLE` to that path in the setup terminal **before** invoking the script; the initial Python downloads precede the wizard. Enter the same full bundle path when the wizard asks. Never disable TLS verification.

## 3. Run the single setup script

```bash
bash setup.sh --work-mac
```

Run this in a **user-visible interactive terminal**. The wizard checks for a real terminal, and passwords/tokens use hidden prompts. Let the user type secrets there; never request them in Copilot chat, pipe them into the command, embed them in URLs, or read/print `local-secrets.json`. If your terminal tool cannot accept interactive user input, give the user this exact command in the current folder and resume after they complete it.

The wizard asks for GitLab host/project, SSH/saved HTTPS/token access, optional bank CA bundle, npm token, and a dashboard username/password. The dashboard password needs at least 24 characters. Setup checks the required tools, disk space, local credentials and access to GitLab `dev`.

Expected configuration after successful setup:

| Setting | Expected value |
| --- | --- |
| `source_provider` | `gitlab` |
| `build_backend` | `local` |
| `credential_source` | `local` |
| `default_workflow` | `validate-dev` |
| Dashboard address | `http://localhost:8765` |

Check configuration fields without printing secrets. Setup returns nonzero if blocked. Resolve the reported cause and rerun the same script; it preserves existing settings and credentials. Do not label setup successful based only on the folder or virtual environment existing.

To repair source authentication after setup, use the one matching the user's access:

```bash
# Existing SSH configuration/agent
./bento source gitlab --backend local --transport ssh

# Saved HTTPS credentials
./bento source gitlab --backend local --transport https --auth existing

# Privately supplied token
./bento source gitlab --backend local --transport https --auth token
./bento credentials --validation
```

Then verify:

```bash
./bento doctor --online --validation
```

Do not use plain `./bento doctor` as the acceptance check for this phase: it checks full native-build prerequisites. The validation preflight reads the npm credential and checks source access; the real npm install happens during the run below.

## 4. Run the first real validation

```bash
./bento open-window --minutes 240
./bento run validate
```

Execute sequentially; stop and investigate if either command fails. The window enables build starts for four hours. On a work Mac without a Bitrise agent there is nothing to stop; if an agent is detected, report that blocker and coordinate an exclusive window with the user instead of disabling the guard or stopping an unrelated service.

The run fetches current GitLab `dev`, records one commit SHA and works in a disposable checkout. Preserve the user's other mobile-app checkouts. Follow execution until completion or a specific blocker; do not report success when the run is merely queued or started.

Read the resulting `data/local-runs/<run-id>/run.json`, `checkout.log` and `validate.log`. The command returns nonzero on failure. On success it prints the run ID and **Code checks passed**. This validation produces logs/results, not native binaries.

If validation fails:

- Identify the first failing step and its redacted error. Distinguish source authentication, DNS/TLS, npm authorization, dependency/patch failures, and TypeScript/lint/test failures.
- Fix dashboard setup/configuration issues within this clone and rerun when the cause is resolved. Every run receives a new ID.
- Do not weaken checks, alter the lockfile, set `USE_PUBLIC_REPOS`, or change mobile-app code merely to manufacture a green result. Report an actual app-code failure with its commit and useful error details.
- If access or credentials require the user/admin, state the exact missing input and the command to resume. Keep existing results available.

## 5. Start the dashboard and show the result

After the terminal run, start the dashboard in a terminal that can remain open:

```bash
./bento serve
```

Open **http://localhost:8765** on this same Mac. No SSH tunnel is needed. Let the user sign in with their chosen credentials, then select the recorded run and its **validate** job. Confirm the displayed status/logs match the recorded result. If browser interaction is unavailable, provide the URL, run ID and log locations and state that UI verification remains pending.

For subsequent runs launched from the UI, keep `./bento worker` running in another terminal from the same dashboard folder, choose **Code checks · dev**, and click **Run workflow** while a build window is open. Do not run `./bento run validate` concurrently with the worker; only one executor is allowed.

These processes run in the foreground. Identify the terminal(s) left running. After validation finishes, use `./bento close-window` to prevent unintended new starts; reopen a window when the user wants another run. Keep the dashboard available for inspecting the result. Window closure does not cancel an active job.

## Completion report

Give the user a concise report containing:

- Installation folder and dashboard source branch/commit.
- Setup/preflight outcome and selected GitLab access method, without secret values.
- App commit SHA, validation run ID, final status and any failing step.
- Dashboard URL and whether its result/log display was verified.
- Any required next action, plus the exact resume command if blocked.

Report success only for work actually verified. A passing code-validation run does not establish APK/IPA compilation, signing, device behavior, store distribution or EC2 readiness. Do not provision AWS, register a GitLab runner, install background services or start native builds as part of this initial validation setup.

## Later, when the user requests native builds

Follow [docs/SETUP.md](docs/SETUP.md) to locate or arrange the required existing native tools. Use `./bento native-tools` to save their paths, then `./bento credentials` to collect Maven and iOS signing inputs privately. Run `./bento doctor --online --build-only` and resolve failures before opening a new window and running `./bento run both`. Record the resulting artifacts and manifests separately from code-validation evidence.
