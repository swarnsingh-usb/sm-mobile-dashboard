# Credentials and signing

For initial tests, **AWS is optional**: see [LOCAL-TEST.md](LOCAL-TEST.md) for private local JSON credentials and signing file paths. `credential_source: local` makes no AWS calls. The schemas below also apply to the local file's named sections; local signing can use `p12_file`/`profile_files` or `keystore_file` instead of base64.

For the later managed setup use **AWS Secrets Manager**, authenticated by the EC2 instance profile. Region: **us-west-2**. `config.json` stores names/ARNs only. Do not paste secret values into chat, pipeline YAML, shell command arguments, or source control.

`generated/instance-role-policy.json` grants `secretsmanager:GetSecretValue` on the selected names. Replace `ACCOUNT_ID`, or use full ARNs in `config.json` and regenerate with `./bento generate`. A Secrets Manager ARN ends in a six-character random suffix; the generated name-based policy matches only that suffix. If the bank uses its own KMS key, add `kms:Decrypt` scoped to that key and the appropriate service/encryption-context conditions. No secret-write or IAM administration access is needed by the running application.

Create secrets through the AWS console or the bank's approved provisioning process. Each is a JSON **SecretString**. The names below are defaults and configurable. The examples contain placeholders, not usable credentials.

## bento/mac-ci/portal

```json
{"username":"engineer","password":"REPLACE_WITH_24_OR_MORE_RANDOM_CHARACTERS","session_key":"REPLACE_WITH_32_OR_MORE_RANDOM_CHARACTERS"}
```

This pilot has one shared internal login, with rate limiting, a signed HttpOnly session cookie and CSRF protection. It does not claim per-engineer audit identity. Use company SSO before broader deployment. Change the password to rotate login access; rotate the session key and restart the portal to invalidate all sessions.

## bento/mac-ci/gitlab

```json
{"token":"REPLACE_WITH_PROJECT_API_TOKEN"}
```

Project-scoped token with `api` access and permission to create pipelines on protected `dev`, read jobs/logs and repository contents, and cancel/retry pilot jobs. Prefer a bank-managed service account/project access token over a person's token. It also authenticates the native dependency branch to CocoaPods over HTTPS. Limit this identity to the mobile project. The API token is never placed in a browser response or repository URL.

## bento/mac-ci/runner

```json
{"token":"glrt-REPLACE_WITH_RUNNER_AUTHENTICATION_TOKEN"}
```

Create the project runner in GitLab first, with the settings in GITLAB.md. This is a runner authentication token, not the deprecated registration token. GitLab Runner persists/rotates it in `runtime/runner.toml`, mode 0600. This is an intentional exception to memory-only credential retrieval: normal runner operation needs that local file. Secrets Manager contains the initial registration token; if you reset the runner token, reconcile this file through the runbook rather than overwriting it at every startup.

## bento/mac-ci/dependencies

```json
{"npm_token":"REPLACE","maven_username":"REPLACE","maven_password":"REPLACE"}
```

Read access to the current npm and Android Maven repositories. Auth is passed through a temporary npm config and process-scoped Gradle properties. The current app already uses Artifactory **for dependencies**; making build-output uploads optional does not remove that dependency access requirement.

## bento/mac-ci/ios

```json
{
  "p12_base64":"BASE64_OF_EXPORTED_SIGNING_CERTIFICATE_AND_PRIVATE_KEY",
  "p12_password":"P12_PASSWORD",
  "team_id":"YOUR_APPLE_TEAM_ID",
  "signing_identity":"Apple Distribution: YOUR_APPROVED_IDENTITY",
  "profiles_base64":["BASE64_OF_MATCHING_MOBILEPROVISION_FILE"],
  "export_options":{
    "method":"release-testing",
    "destination":"export",
    "teamID":"YOUR_APPLE_TEAM_ID",
    "signingStyle":"manual",
    "provisioningProfiles":{
      "com.usbank.SpendManagement.staging":"EXACT_PROFILE_NAME_OR_UUID"
    }
  }
}
```

`release-testing` is an example for an appropriate ad-hoc profile, not an assumed entitlement. Set the export method supported by your installed Xcode and matched to your actual profile: e.g. `debugging`, `release-testing`, `enterprise`, or `app-store-connect`. Older Xcode versions use older method names; check `xcodebuild -help` on the Mac. App Store export is not direct device installation. A development/ad-hoc profile must include the intended test devices. An App Store Connect API key alone is not a signing certificate or profile.

Default target: `USBankSpendMgmtMobileApp-Stage`, bundle ID `com.usbank.SpendManagement.staging`, **dev environment**. The source branch and app environment are both dev; the existing staging identity is reused. If the actual successful Bitrise build uses another target, update `ios.scheme` and `ios.bundle_id` together with matching profiles. Additional app-extension bundle IDs must be present in `provisioningProfiles` and have corresponding included profiles.

The runner validates profile team/expiry and uses a temporary keychain; no default keychain is replaced. It temporarily extends the search list while holding the host lock and restores it afterward. Certificate import passwords are necessarily passed to Apple's local `security` tool; this is a trusted-host operation. Do not run untrusted code/users on this machine.

Secrets Manager values have a 65,536-byte limit. Check the size of the finished JSON; do not truncate profiles to fit. This pilot's bundled-signing schema is intended for the small number of profiles used by this app. If it exceeds the limit, extend the loader to separate profile secrets before setup.

## Optional Android signing secret

```json
{"keystore_base64":"BASE64_OF_EXISTING_KEYSTORE","store_password":"REPLACE","key_alias":"REPLACE","key_password":"REPLACE"}
```

Set `secrets.android` to its name. Without it, the existing `releaseStaging` debug signing configuration is preserved and the manifest says `repository-debug-key`. The application ID remains `com.usbank.spendmanagement`, which is also used by production; this is not a side-by-side install design. Do not generate or substitute a production signing key.

## Optional Artifactory upload secret

```json
{"token":"REPLACE_WITH_ARTIFACTORY_ACCESS_TOKEN"}
```

Grant deploy and read access to the chosen generic local repository/prefix. Exclude delete/overwrite permission to enforce retention/immutability on the server. Configure URL (ending `/artifactory`), repository key, and `secrets.artifactory` together. Local output retention stays enabled.

## Host trust boundary

An EC2 instance profile is a **host-level** trust boundary; different Unix accounts do not automatically receive different AWS permissions. Protected dev code and dependency install scripts are trusted in this pilot. Do not enable arbitrary branch/MR jobs on this persistent credential-bearing machine. SDK reads are cached for five minutes, and build signing/dependency reads force refresh. Neither the application nor tests create AWS secrets or roles automatically.

Sources: [AWS Secrets Manager retrieval](https://docs.aws.amazon.com/secretsmanager/latest/userguide/retrieving-secrets.html), [IAM permissions](https://docs.aws.amazon.com/secretsmanager/latest/userguide/auth-and-access_iam-policies.html), [GitLab runner security](https://docs.gitlab.com/runner/security/).
