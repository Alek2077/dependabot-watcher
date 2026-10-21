# Windows review component

This directory contains the native review source. The self-contained Windows x64 companion is available in the [experimental alpha releases](https://github.com/Alek2077/dependabot-watcher/releases); publication does not establish broader platform acceptance. Python remains the portable evidence, job, permission, execution and recovery engine. This component supplies one aggregate window, tray access and Windows notifications; it does not implement GitHub policy.

The current sources preserve the tested development assembly name and
diagnostics. The development CLI can select an existing component with
`review --native-component PATH` and subsequently reuse its saved fingerprint.
It launches the installed Python module in isolated mode with the chosen private
WSL profile. The window validates and retains one exact launch definition;
different launch bindings or component fingerprints cannot overwrite a live
window's manifest or silently activate an older binary. Component identity binds
both the executable launcher and its sibling application DLL: self-contained
.NET launchers can remain byte-identical when application code changes. Legacy
EXE-only registrations require deliberate reselection with `--native-component`;
old immutable profiles remain historical and are not rewritten. This is not
code signing or a guarantee about every bundled dependency. Process submission is
not proof of a visible window. Normal startup does not fall back to synthetic
recommendations. Installed-wheel read-only queue/feedback/decision/reopen plumbing
has been exercised on Windows/WSL. Installed development checks also cover cold
notification opening, quiet scheduled initiation and same-session live feedback;
one exact approved merge and subsequent same-conversation validation have also
completed. One subsequent real automatic post-merge assessment and separately approved
local-completion run passed. Reliable foreground focus, fresh-user component
distribution/setup and broader recovery acceptance remain open. See validation for exact snapshot boundaries.
Synthetic notification controls are not offered in a normal profile. The
development `notification test` command instead exercises the normal selected
component without changing queue decisions or production reminder state.

Deliberate component registration is saved before process submission. If that
write fails, no native child starts; a saved selection is not evidence that a
launch succeeded. The rejected browser review/server/assets are not part of
this component or the Python distribution.

## Build boundary

The project uses C#/WPF on .NET 10 and Windows App SDK 2.5.1, with self-contained
Windows x64 deployment. Contributors need the Windows build tools; end users
download the built component rather than install an SDK. The basic Python
CLI remains usable without its runtime; the current WSL scheduled AI supervisor
requires the component, although the review window need not stay visible.

Restore and build `src/WatcherNativeProof.csproj` using a compatible Windows
.NET SDK. A cached development build can use `--no-restore`. Build output and
private test/launch state must not be committed. Development tool caches are
not application runtime requirements.

After self-contained publishing, run `complete-publish.ps1 -PublishedDirectory PUBLISH_DIRECTORY -AssetsFile src/obj/project.assets.json` before testing or packaging. Windows App SDK 2.5.1 can omit its Insights resource DLL. The helper takes that one resource from the exact runtime MSIX resolved by the build, or verifies an existing matching file; it never substitutes a system DLL or another application's copy. Use `-PackageCache PATH` when the build's restored NuGet cache is mounted elsewhere. The release workflow runs this step and its controlled contracts; the packager independently refuses a missing resource.

### Package a verified build

Build and package from a complete repository checkout. The Python source distribution includes only selected native sources, not the full native build scripts or test suite; it is not a standalone Windows build workspace. The packaged Windows component is required for the current WSL supervisor even when its window stays hidden; basic CLI use does not require it.

Maintainers can create the separate Windows x64 archive without copying SDKs or caches into it:

```bash
python native/windows/package.py --published-directory PUBLISH_DIRECTORY --assets-file PROJECT_ASSETS_JSON --output-directory OUTPUT_DIRECTORY --version VERSION --source-commit FULL_VERIFIED_COMMIT --preview
# Remove --preview after checking the selected build and its source provenance.
```

The default output is `.7z`, using 7-Zip with LZMA2/BCJ, level 7, a 32 MiB dictionary, two threads and solid compression, without encryption. Maintainers need `7zz` or `7z` on PATH, or `--seven-zip PATH` (for example the installed Windows `7z.exe`). Use `--archive-format zip` for the compatible ZIP/Deflate level 6 alternative. The packager tests and separately extracts each 7z candidate, verifies every extracted file against the staged input, and publishes it only after verification; it never replaces an existing archive. Staging and verification require temporary disk space for two uncompressed copies plus the archive.

The assets file is the build's resolved `obj/project.assets.json`; when packaging a Windows build from WSL, `--package-cache` selects that build's mounted NuGet cache. The packager includes runtime dependencies, project license, resolved dependency notices and a per-file SHA-256 manifest. The window shows that manifest's component version in its title and footer; an unpackaged build without usable version metadata shows “development build.” It excludes debug symbols and refuses recognized private state, logs and linked files. Keep the selected build and cache immutable during packaging; identity/change checks are not isolation against a concurrent same-user writer. The source commit is supplied by the maintainer: packaging does not independently prove build provenance, sign the executable, register it or publish a release. Integrity hashes are not publisher authentication.

The release workflow is configured to build and check this component on Windows alongside the Python distributions. A manual workflow run rehearses the builds without publishing; a version-tag push can proceed to publication only after both builds succeed. That hosted path includes the Windows x64 archive, checksums and build attestations, and a [successful hosted rehearsal](../../docs/validation.md#hosted-ci-and-release-build-verification--2026-10-21) has verified that build and acquisition path. The published 0.4.0a1 and 0.4.0a2 alphas retain their locally built packages under explicitly approved exceptions; they have no GitHub-hosted build attestations and are not replaced by rehearsal artifacts. The archive is unsigned, and extraction and component selection remain explicit user steps.

For `.7z` packages, use File Explorer’s **Extract All** on Windows 11 24H2 or newer; older Windows versions require [7-Zip](https://www.7-zip.org/) or another compatible extractor. The published `0.4.0a1` ZIP remains supported. Verify the supplied checksum before extraction.

For a supplied package, extract the complete archive into a stable user-owned directory and retain the runtime files beside `WatcherNativeProof.exe`. From WSL, select that executable with `dependabot-watcher review --native-component PATH`, using the intended configuration and state. Do not replace a registered build in place: extract an update into a new versioned directory and deliberately reselect it. The basic Python CLI does not require this archive; the current WSL scheduled AI supervisor does, even when its window remains hidden. A complete guided install/update path and signed release distribution remain separate acceptance work; users must not install build tools merely to run a packaged component.

## Communication and lifetime

### Notification link and diagnostics

The development notification route uses Windows App SDK toasts, not the
short-lived balloon adapter. A private current-user Windows lookup resolves an
opaque profile ID to a saved normal launch definition. A click revalidates that
definition and executable before opening the aggregate queue; it carries no
paths, commands, permission decision or merge authority. A different profile
cannot replace a live window's backend. The sender can exit without requiring
the review window to stay open.

With a deliberately selected component, use the same configuration and state
arguments as `review`:

```bash
dependabot-watcher notification test --config CONFIG --state-dir STATE
dependabot-watcher notification status --config CONFIG --state-dir STATE
dependabot-watcher notification remove --config CONFIG --state-dir STATE
```

The first test may select `--native-component PATH`. Test uses no model or GitHub
access and does not reset notification deduplication. Status reports Windows
settings/history, not human receipt. Remove clears only this profile's lookup
and notices, leaving the saved queue, component selection and schedules intact;
a later explicitly configured desktop run can register it again. Turn desktop
delivery off or disable/remove the schedule to stop later notices.

Configured WSL desktop delivery uses the selected component and refuses to
silently fall back to the transient balloon when it is unavailable or changed.
Production notice content comes from the exact current saved run through the
bound backend, not a caller-selected report path or notification payload.
Installed development tests have exercised visible durable notices, cold queue
opening and same-owner tray reopening with saved feedback. Cold starts have also
opened behind another application; reliable foreground focus remains an open
operating issue. This is not component distribution or scheduled-supervisor
acceptance. Contract tests live separately under `tests/` and load the built
assembly and its registration-free SDK context without starting the application.
They round-trip actual SDK-generated toast/button arguments, but do not publish
notifications and do not replace human click evidence.

### Backend operations

The current native transport launches owned hidden Windows-to-WSL processes.
The trusted backend launch file determines the executable arguments, profile
and state. Notifications and user messages never choose commands or grant
execution authority. Explicit investigation streams lifecycle and transient
permission previews; feedback, decisions and saved-job verification use
independent bounded exchanges with exact identity/context checks.

The conversation section names the configured model and access/usage boundary.
**Send to agent** first saves the selected item's message, verifies the post-save
configuration/context, then starts a repository investigation. **Save without
sending** performs only the local save; **Investigate this item** saves an
item-scoped investigation request and starts or continues work. None approves a merge.
The latest verified answer is visible separately from expandable history and
technical evidence. A refreshed recommendation retains same-item selection and
historical conversations, but never inherits old action consent or advice freshness.

A failed current investigation offers an explicit retry with a usage reminder.
It names the failed attempt, rechecks the current context and creates a new job;
history remains intact and lost replies never cause automatic inference replay.
Only allow-once, deny and cancel are offered for live access requests. They do
not authorize a merge. Repository aliases associate action history without
rewriting an exact saved plan, head, consent or result binding.

Closing the window hides it to the tray. Exit stops accepting new requests,
stops the owned investigation and waits for outstanding backend exchanges.
Approved actions finish and record their outcome instead of being killed or
retried. UI errors enter the same drain path and retain an unsuccessful exit
status. Cancellation cannot undo an already-started command.

Synthetic diagnostics exercise the real Windows/WSL/controller/journal path
with controlled external collection/provider boundaries. Their success is not
live-model, installed-package, scheduled-operation or human usability evidence.
The diagnostic window requires `--diagnostic-window`; a stale manifest beside
the binary is never an ordinary entry point. Normal rejected startup does not
write events beside installed binaries or use rejected input as an error sink.
See [architecture](../../docs/architecture.md) and
[validation](../../docs/validation.md) for the authoritative contracts and gates.
