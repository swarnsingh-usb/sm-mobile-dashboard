# Workflow mapping for milestone 1

The checked-in Bitrise YAML has drifted: Bento brand names conflict with current `usbank` prebuild validation, and some steps hard-code stage. Therefore this package is an explicit translation of the current native app targets, not an interpreter of every historical Bitrise step.

| Existing intent | New pilot behavior |
|---|---|
| Standard Android Stage build | `android-stage`: `usbank` + **stage environment**, `assembleSpendmanagementReleaseStaging` |
| Developer Android build | `android-developer`: `usbank` + **stage environment**, `assembleDeveloperReleaseStaging` |
| iOS staging app archive | `ios-dev`: `usbank` + **stage environment**, `USBankSpendMgmtMobileApp-Stage` archive/export |
| iOS Developer app archive | `ios-developer`: `usbank` + **stage environment**, `USBankSpendMgmtMobileApp-Developer` archive/export |
| Build both | `both-dev`: separate platform jobs, same source SHA, sequential host use |
| PR validation | `validate-dev`: manual trusted dev checks first; automatic MR triggers deferred |
| Bitrise artifact upload/install page | Local authenticated downloads; optional Artifactory upload and links |
| Production/store deployment | Not enabled in milestone 1 |

Android uses Bitrise's `versionCode = build number + 100` and `versionName = 2.0.<build number>` formula. Its private local counter starts after the source baseline and is recorded in manifests; it is not coordinated with Bitrise, so Firebase or store distribution remains disabled until both systems share one allocator.

Existing dependency patches are retained in order, with fail-fast execution and frozen Yarn installation. The standalone validation workflow runs existing TypeScript, ESLint and Jest commands without weakening failures. The native build workflows do not implicitly claim those checks passed; select validation separately.

Both current Android workflows use the repository's debug key because their explicit Bitrise signing steps are disabled. The APKs are signed but are not qualified production artifacts. The iOS profile/export method determines where the IPA can be installed; downloaded IPA alone is not TestFlight distribution.
