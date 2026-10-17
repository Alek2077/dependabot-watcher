# Development, release and dogfooding

This is Watcher's maintainer workflow. It separates proving a change in development from delivering that change through the same installation/update path a user receives. Use the [validation workflow](validation-workflow.md) for test procedures and the [validation record](validation.md) for historical evidence; neither a passing test nor a merged PR establishes a successful user update.

Keep distributed documentation, agent instructions and runtime defaults usable without the maintainer's private development environment. Document real Watcher prerequisites and contributor conventions, but do not require private harness files, workflow wrappers, reviewer arrangements or workstation paths. Examples and historical evidence must not become implicit user requirements; resolve operating conventions from the user's authorized configuration and repository instructions.

## Keep the environments distinct

| Environment | Purpose | Update boundary |
| --- | --- | --- |
| Development checkout and isolated test installations | Implement changes, build candidates, run diagnostics and reproduce failures | Developer-built artifacts and controlled fixtures are appropriate here. |
| Persistent dogfooding installation | Exercise acquisition, updates and ordinary maintenance with realistic settings, scheduler and history | Use the documented user installation/update procedure against an identified remote revision or published version. |

Do not replace dogfooding executables with loose development output, install a wheel from a maintainer's working directory, register an ad hoc private native build or patch saved state merely to deliver a fix. An exception requires the owner's explicit approval before the change and must be recorded as an assisted/development update, not evidence that the normal update path works. Ordinary authorization to fix code or test an isolated build is not that exception. An explicitly supplied packaged Windows prerelease following the documented channel below is not such a shortcut; record its private origin rather than claiming public package acquisition.

An agent may perform both roles, but it must keep their installations, profiles and authority separate. A user-side agent or independent session is useful for testing discoverability; a particular model, IDE or additional window is not part of the product contract.

## Development to delivery

1. Implement in the development checkout. Run proportionate deterministic, native/platform, installed-artifact and visual checks, and the applicable independent review. Use isolated test profiles; do not use the dogfooding profile as a fixture.
2. Resolve findings and verify the actual final source. Update relevant documentation and submit a focused contribution with validation evidence through the repository's contribution process.
3. Identify the merged remote commit and intended distribution channel. A merged PR is available source, not automatically a release or an updated installation.
4. Prepare the deliverable supported by that channel. Bind Python and native artifacts to their source commit/version, validate their contents and record hashes. Verify the public/user-facing acquisition instructions are usable with the tester's actual access. Missing build, authentication or distribution prerequisites are delivery blockers, not permission to substitute a local developer build.
5. Update the persistent dogfooding installation using the user procedure below. Verify installed identity, settings/history retention, native registration and scheduler behavior before continuing ordinary use.
6. Resume the user's existing workflow. Record product friction and assisted interventions accurately. Do not restart setup or erase history to turn an update/usability test into a fresh-install success.

The implementation agent owns development validation and release preparation. The agent operating the dogfooding installation follows user-facing instructions rather than private developer shortcuts. The owner provides direction, necessary access and action approvals—not repetitive development QA or manual command execution by default.

## The installation/update channel must actually exist

### Current private prerelease: reviewed GitHub source

The [README installation](../README.md#install-and-set-up) and [update instructions](../README.md#update-reset-uninstall-or-roll-back) currently support a clean checkout of an exact reviewed GitHub tag or commit, installed by `uv` or `pipx`. “Pinned source” means selecting that exact revision, not following moving `main` and not using the active development checkout. Acquire it independently from GitHub; verify the revision and clean state before installation. Existing private repository access remains a prerequisite until publication.

Use the README's source reinstall/update instructions from that clean, pinned acquisition with the installer that owns the existing tool. This is a supported source update, not a PyPI release. The README's Python 3.14 command is an example with an explicit interpreter selection, not an instruction to silently migrate an installation using another supported interpreter. Establish the current installer and interpreter first; preserve them or obtain approval for a deliberate migration rather than assuming a reinstall preserves them automatically.

The Windows interface is a separate component. Start with the [user acquisition and selection instructions](setup.md#windows-component-for-wsl-supervision). Until a public package is available, the documented channel is an explicitly supplied private prerelease ZIP, not an agent searching for a maintainer's build. The maintainer supplies a package tied to the selected source revision using the [native packaging contract](../native/windows/README.md#package-a-verified-build); verify backend compatibility, supplied provenance and checksums, extract the complete archive into a new versioned directory, and deliberately reselect it with the intended profile and state. Record the supplied-package origin; integrity hashes alone do not authenticate the publisher. If no suitable package is supplied, report the prerequisite rather than substituting loose SDK/build-cache output. Users of a packaged component should not need the maintainer's SDK/build cache.

### Packaged releases when available

Only use the README's PyPI/versioned-package commands after the selected version is actually published. A prerelease can support dogfooding without being a stable public release, but it still needs a real, documented acquisition and update path. An unreleased source version string is not proof that a package exists.

The current [release workflow](../.github/workflows/release.yml) builds Python distributions and a self-contained Windows archive. Manual workflow dispatch builds validation artifacts only. A matching `v*` tag can proceed to PyPI publication after both builds succeed and the `pypi` environment permits it; GitHub publication depends on successful PyPI publication. Tagging and publishing are external release actions requiring explicit authorization. Merging a normal PR does not perform them. Do not assume this workflow creates a separate private prerelease channel or marks a GitHub release as prerelease; inspect and deliberately change the publishing configuration if that channel is needed.

Hosted build/startup failure is not a successful release. Permission to merge despite unavailable CI does not establish hosted build, package availability or release acceptance, or waive release checks.

Before package publication, maintainers must verify functioning hosted CI and a successful hosted release rehearsal for the selected candidate. This is an acceptance requirement, not an additional automatic gate enforced by the tag-triggered workflow; retain the run identity and source revision with release evidence.

## Updating without losing the ongoing trial

Before updating, locate the actual installed command, native component selection, configuration, state and scheduler registration. Record the current and intended commit/version and check for active investigation or action work. Defer replacement of either backend or native binaries until active work has reached a verified terminal state; use supported recovery if it cannot finish normally, never kill an approved action to replace binaries. Prevent new scheduled work during replacement using supported scheduler controls, close the application normally, and restore the intended schedule after verification. If these conditions cannot be established, report the update as blocked rather than replacing a running installation.

Keep settings, selected targets, saved decisions, conversations, audit history and scheduler identity across updates unless a documented migration or separately approved reset requires otherwise. Never assume an older backend can read newer journals. Check compatibility before rollback and preserve state rather than silently discarding incompatible records.

After updating, verify that the command resolves to the intended installed tool rather than the development import path; version alone may be insufficient while several source revisions share a prerelease version. Verify the selected native component, saved-profile loading, queue/outcome visibility and native scheduler readback. Do not approve a new merge, invoke paid inference or synchronize a checkout merely to prove the update; those keep their own scope and consent requirements.

An unchanged normal-use trial continues from its saved state. This does not waive applicable fresh-install or fresh-session milestone checkpoints in the validation workflow: run those separately with isolated profiles, preserving the continuing trial. Fresh acquisition, update preservation and ongoing usability are separate evidence claims; label which were exercised. Developer assistance does not erase the usefulness of later usability observations, but it must not be counted as an unassisted update.

## Storage and cleanup

Installation tools choose the supported user-level tool location; settings, durable state and the Windows component have separate platform-appropriate locations. That separation is normal, not several competing Watcher installations. Show the actual resolved paths when explaining an update, distinguishing the source checkout from the installed runtime.

Use persistent locations for a continuing dogfooding workspace and its configuration/state. Do not rely on `/tmp` surviving a restart, and do not remove a workspace retained for an ongoing user/agent trial under routine test cleanup. Temporary build/acquisition files may be disposable only when the installed product no longer depends on them and they are not the retained agent workspace. Preserve useful session continuity; do not require a reinstall simply because a disposable acquisition directory disappeared.

Keep update evidence tied to the selected revision, channel and installed result. Significant sanitized acceptance evidence belongs in the validation record; instructions belong in the workflow. Private working notes are optional and are not a prerequisite for contributing or operating Watcher.

Read the sections relevant to the task rather than loading every related document. Update active guidance in its owning section instead of appending competing instructions; cross-reference rather than repeat it elsewhere. Add historical entries for significant acceptance evidence or decisions, not routine command transcripts. Preserve valid history and distinguish it from current requirements. Improve navigation or separate material by purpose when retrieval becomes difficult, not merely to meet a word-count target.
