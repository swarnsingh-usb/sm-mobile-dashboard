# Connect the GitLab project

Project: `https://gitlab.us.bank-dns.com/BENTO/bento.mobileapp`.

## Runner

Create a **project runner** in GitLab's CI/CD runner settings:

- Tag: `bento-mac-pilot` (must match config.json).
- Protected: enabled.
- Run untagged jobs: disabled.
- Locked to this project: enabled.
- Maximum timeout: at least 3 hours for initial cold native builds.
- Do not enable ordinary feature/MR branch execution on this persistent host.

Store its `glrt-` authentication token in the configured runner secret, then `./bento register`. The local `runtime/runner.toml` has one concurrent job and one runner limit. GitLab's resource group serializes the platform jobs too. Neither setting coordinates with Bitrise: the exclusive pilot window is still mandatory.

The API token's user must be permitted to run pipelines on protected dev. The bank must permit that role to set pipeline variables through the API (`BENTO_PORTAL`, `BENTO_WORKFLOW`, `BENTO_REQUEST_ID`). If instance policy prohibits pipeline variables, adapt the trigger contract to GitLab CI inputs with the bank's version before enabling the UI. Do not broaden permissions globally as a workaround.

## Pipeline

`./bento generate` renders the absolute installation path into `generated/gitlab-ci.yml`. Keep the installation directory stable. The generated runner script invokes that host-installed, reviewed build code instead of downloading a script from an arbitrary branch.

For an app checkout with no existing CI file:

```bash
./bento install-pipeline /path/to/separate/bento.mobileapp-checkout
```

Review, commit and push using the bank's normal process. This command deliberately refuses to overwrite an existing `.gitlab-ci.yml`. If one exists, merge the three jobs, the workflow rule and name, variables, and build stage carefully. `workflow` is project-wide: do not paste over existing rules for unrelated pipelines. Existing pipelines may need their own preserved naming/default behavior. Use GitLab's CI lint on the final combined file.

The first milestone accepts only explicitly marked API/web pipelines on protected dev. No mirrored push or PR event starts a pilot native build automatically. Manual launch through GitLab's UI is possible with `BENTO_PORTAL=1` and the desired `BENTO_WORKFLOW`. The dashboard supplies both. The workflow names are `both-dev`, `android-dev`, `ios-dev`, `validate-dev`.

## Branches and exact source

GitLab creates a pipeline for the current dev commit. The Mac checks out that pipeline's exact SHA; the build script verifies it before work. Retrying a job uses that pipeline's source; launching again may use a newer dev commit. Pipeline and job IDs make local/Artifactory locations distinct on retries.

For iOS, mirror `auth-26-06-cocoapods` **and the commits referenced by Podfile.lock**. The helper rewrites the old GitHub repository URL to the configured bank GitLab URL in the disposable Podfile/lockfile only. It retains locked revisions, adjusts the Podfile checksum, and uses `pod install --deployment`. Missing native assets/locked commits must be fixed in the mirror, not worked around with an unconstrained pod update. Other public pod sources/download URLs must remain reachable or be migrated explicitly.

GitHub PR mirroring/integration remains the bank's responsibility. Enable branch/MR triggers only after the manual dev build and shared-host admission design are qualified.

Sources: [Pipeline API](https://docs.gitlab.com/api/pipelines/), [Runner registration](https://docs.gitlab.com/runner/register/), [Workflow rules](https://docs.gitlab.com/ci/yaml/workflow/).
