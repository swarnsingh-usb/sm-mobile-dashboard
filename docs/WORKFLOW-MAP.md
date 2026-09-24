# Workflow mapping for milestone 1

The checked-in Bitrise YAML has drifted: Bento brand names conflict with current `usbank` prebuild validation, and some steps hard-code stage. Therefore this package is an explicit translation of the current native app targets, not an interpreter of every historical Bitrise step.

| Existing intent | New pilot behavior |
|---|---|
| Android staging-style release build | `android-dev`: `usbank` + **dev environment**, `assembleSpendmanagementReleaseStaging` |
| iOS staging app archive | `ios-dev`: `usbank` + **dev environment**, `USBankSpendMgmtMobileApp-Stage` archive/export |
| Build both | `both-dev`: separate platform jobs, same source SHA, sequential host use |
| PR validation | `validate-dev`: manual trusted dev checks first; automatic MR triggers deferred |
| Bitrise artifact upload/install page | Local authenticated downloads; optional Artifactory upload and links |
| Production/store deployment | Not enabled in milestone 1 |

Native app version/build numbers are preserved from source in this milestone, recorded in manifests, and never uploaded to stores. That avoids colliding with Bitrise's release number allocation. Establish a shared monotonic allocator before either new workflow performs store/tester distribution.

Existing dependency patches are retained in order, with fail-fast execution and frozen Yarn installation. The standalone validation workflow runs existing TypeScript, ESLint and Jest commands without weakening failures. The native build workflows do not implicitly claim those checks passed; select validation separately.

The Android default key is the repository's current debug key. It is signed, but not a qualified production artifact. The iOS profile/export method determines where the IPA can be installed; downloaded IPA alone is not TestFlight distribution.
