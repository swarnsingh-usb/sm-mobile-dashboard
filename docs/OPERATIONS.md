# Operations and coexistence

## First pilot uses an exclusive window

Both systems remain installed through the planned October 9 transition. This package does **not** claim that independent Bitrise/GitLab schedulers can safely execute simultaneously on this one host.

Drain Bitrise, temporarily stop its agent, prevent its supervisor from restarting it, and open a pilot window. No Bitrise stop/start command is guessed or run automatically: identify its actual service and queue controls on this Mac. Process detection recognizes the normal `bitrise-den-agent`, `bitrise-agent`, and Bitrise CLI executable names, but it is an additional guard, not proof that an unknown/custom supervisor is disabled.

The new system uses an OS file lock for its jobs, checks the window after acquiring it, and monitors recognized Bitrise processes during native commands. If Bitrise restarts, it terminates the new command and fails the job. There is still a detection interval, so this guard is not a substitute for draining/stopping the old scheduler first. Host-wide signing/search-list or cleanup changes must never overlap.

After window closure/expiry, existing jobs may continue. Wait for all pilot jobs to finish, verify that no cleanup/recovery is pending, and then resume Bitrise. To support two live queues later, add an admission mechanism observed by **both** systems before resource use, including Bitrise checkout/setup/cleanup. A lock added only to the new scripts cannot guarantee that.

## Common commands

This host is SSH-only. For the initial build use LOCAL-TEST.md; for an interactive dashboard pilot use the foreground `./bento serve` and `./bento runner` commands in separate SSH sessions. The `services` commands are GUI LaunchAgent management and are **not usable on this host**. Headless automatic startup and recovery remain to be implemented and qualified.

```bash
./bento doctor --online
./bento open-window --minutes 240
./bento close-window
./bento recover-signing
```

Drain jobs before stopping or restarting the foreground processes. This does not pause/unregister Bitrise or change its automatic triggers. `recover-signing` needs an exclusive pilot window and restores the saved user keychain search list after a hard interruption. Hard-killed jobs may leave job directories in `runtime/`; inspect and remove only those belonging to finished jobs after signing recovery.

## Access

Initial access is localhost or an SSH tunnel. For team access, put a bank-managed HTTPS reverse proxy in front of `127.0.0.1:8765`, preserve the configured public Host header, and set `public_url` to the exact HTTPS origin. Example nginx server fragment:

```nginx
server {
    listen 443 ssl;
    server_name BUILDS_INTERNAL_HOST;
    ssl_certificate /approved/path/server.crt;
    ssl_certificate_key /approved/path/server.key;
    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
    }
}
```

Replace the host/cert paths and use your existing approved gateway if available. Restrict network access to company/VPN sources. Never expose the app's plain HTTP listener. The pilot login is shared; company SSO and per-user authorization/audit are required before broader rollout.

## Storage and Artifactory

Without Artifactory: completed APK, IPA, archive, symbols and manifest stay under `data/artifacts`, on disk, not RAM. Downloads still work through the authenticated local-artifact API when GitLab is unavailable. Files survive an ordinary process restart but not disk loss, deletion, or Bitrise reimaging; arrange backups/retention with the bank.

No automatic artifact deletion is configured. Monitor disk and archive/remove old runs intentionally. The build script checks 40 GiB free before starting; adjust only after measuring actual peak usage. Logs are held by GitLab under its retention policy. Portal/runner operational logs in `data/*.log` also need bank-managed rotation.

When ready, edit the three Artifactory fields and restart the portal after draining jobs. The next builds still keep local copies and also upload to:

`<repo>/<prefix>/usbank/dev/<commit>/<pipeline>/<job>/<platform>/`

Binary SHA-256 is supplied on upload and checked against the storage API. Manifests are uploaded last. Configure server-side no-overwrite permissions. A configured upload failure fails the pipeline and retains local files with `upload_status=failed`; it never silently reports local-only success. Existing local builds are not automatically uploaded. No automatic retry of uncertain uploads or pipeline creation is performed.

## Credentials, cancellation and recovery

The dashboard never sends service tokens to the browser. Build output redacts known secret values, but arbitrary code can deliberately transform or exfiltrate credentials: trusted protected dev and trusted dependencies remain essential. Do not print environment dumps or enable shell tracing.

Cancellation terminates the active child process group, waits up to ten seconds, and force-kills if necessary before cleanup. iOS signing cleanup restores the pre-build keychain search list, deletes only profiles it added, and removes the temporary keychain. A journal persists until restoration succeeds. After power loss/SIGKILL, run signing recovery before another build. Mac toolchain/provisioning must be tested on the actual host.

## Retirement

October 9 is the planned transition date, not an automatic action. Qualify both platforms, installation behavior and failure recovery first. Explicitly transfer automatic triggers/distribution, then retire Bitrise through a separate reviewed change.
