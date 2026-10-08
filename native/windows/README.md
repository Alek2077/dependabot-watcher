# Optional Windows review component

This directory brings the native review source into the repository. It is a
development integration, not yet an ordinary installed companion or a public
platform-support claim. Python remains the portable evidence, job, permission,
execution and recovery engine. This component supplies one aggregate window,
tray access and Windows notifications; it does not implement GitHub policy.

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
completed. Reliable foreground focus, component distribution/setup and complete
maintenance-loop acceptance remain open. See validation for exact snapshot boundaries.
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
will receive an optional built component rather than install an SDK. The Python
package must remain usable without the component or its bundled runtime.

Restore and build `src/WatcherNativeProof.csproj` using a compatible Windows
.NET SDK. A cached development build can use `--no-restore`. Build output and
private test/launch state must not be committed. Development tool caches are
not application runtime requirements.

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
