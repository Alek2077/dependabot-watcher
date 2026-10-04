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
window's manifest or silently activate an older binary. Process submission is
not proof of a visible window. Normal startup does not fall back to synthetic
recommendations. Installed-wheel read-only queue/feedback/decision/reopen plumbing
has been exercised on Windows/WSL; component distribution/setup, cold notification
activation, scheduled supervision and live-action acceptance remain open.
Synthetic notification controls are not offered in a normal profile.

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

The current native transport launches owned hidden Windows-to-WSL processes.
The trusted backend launch file determines the executable arguments, profile
and state. Notifications and user messages never choose commands or grant
execution authority. Explicit investigation streams lifecycle and transient
permission previews; feedback, decisions and saved-job verification use
independent bounded exchanges with exact identity/context checks.

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
