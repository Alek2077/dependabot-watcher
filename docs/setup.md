# Setup and diagnostics

The default first-use path remains read-only and model-off. Approval-gated execution, model advice, native scheduling, and desktop notifications are separate explicit opt-ins. Unattended mutation remains unavailable.

## GitHub access and authentication

GitHub authorization is unavoidable for private repositories and Dependabot security alerts. The current implementation uses GitHub CLI as its first GitHub transport and credential broker: the adapter issues bounded `gh pr`, `gh repo`, and `gh api` reads while the core depends on a transport-neutral `GitHubClient` protocol.

GitHub CLI does not require an IDE. Its browser login, credential store, and supported token environment variables are independent from VS Code or another editor. The watcher deliberately does not inspect, extract, or reuse editor-managed credentials; those credentials are scoped to another application and are not a stable public integration contract.

Before installation, use `gh auth status` to confirm the active account and `gh auth login` only when authentication is absent or unsuitable. Access to the Watcher source repository, access to each configured target repository, and permission to read Dependabot alerts are distinct. A successful `gh repo clone` proves only the first; setup verifies repository/branch access, doctor probes alert readiness, and a complete inspection remains the operational proof that the required read surfaces work.

The default product path issues read-only GitHub requests. The sole mutating edge is the explicitly enabled exact-head PR merge in `execute`; a user's existing `gh` credential may carry broader authority than either path needs. Before public release, document and validate least-privilege credential profiles for the selected repositories, required read surfaces, and the optional merge capability. Future GitHub adapters may support a directly supplied fine-grained token, OAuth/device flow, or a GitHub App when their added complexity is justified. These adapters can remove the hard GitHub CLI dependency, but not the underlying authorization requirement.

## Recommended setup

```bash
dependabot-watcher setup --repo owner/repository
```

`owner/repository` denotes the GitHub repository identifier and must be replaced with a real value. It is example syntax, not a built-in demonstration repository.

The command checks GitHub CLI authentication, verifies read-only repository and configured-branch access, discovers default branches, creates a non-secret operational profile, and prints the exact `doctor` and read-only `run` commands to use next. Its receipt also states repository scope, AI/provider usage, and the decisions still required before recurring operation.

The generated profile is deliberately conservative: advisory mode `off`, autonomy `read_only`, mutation disabled, concise human reporting, complete JSON machine output, and no scheduler or notification configuration.

Subsequent commands load this profile automatically from the platform configuration path. Setup flags are initial choices, not arguments users or agents must repeat for every run.

Setup publishes the complete file atomically and refuses to overwrite an existing path. Move or edit an existing configuration deliberately rather than relying on a destructive force flag.

For an environment where GitHub cannot be reached during setup, `--offline` creates an explicitly unverified profile. Supply branch overrides as `--branch owner/repository=branch`; otherwise offline setup uses `main` as an assumption that `doctor` must later verify.

## Advanced model setup

Provider use is independent from GitHub action authority. Enabling advice requires explicit mode, model, reasoning, and acknowledgement:

```bash
dependabot-watcher setup \
  --repo owner/repository \
  --advisory-mode reviewed \
  --triage-model TRIAGE_MODEL \
  --triage-reasoning high \
  --review-model REVIEW_MODEL \
  --review-reasoning max \
  --acknowledge-model-use
```

Setup checks Codex CLI availability and authentication but never invokes a model. The acknowledgement records consent to later quota/cost consumption; it does not authorize GitHub mutation. Provider credentials remain in the provider's supported authentication store and never enter the Watcher profile.

Add `--require-advice-before-execution` only when approval-gated execution must fail closed unless the exact candidate received current successful advice under the configured model, reasoning, and budget policy. It does not make the model an approver and does not enable mutation by itself.

## Run once and schedule

### Tool-capable supervisor setup (development)

The unreleased supervisor can be selected during ordinary setup rather than by
hand-editing TOML. Setup saves policy only: it does not create a workspace, start
an agent or install a schedule. Register an existing isolated
POSIX/WSL linked worktree and its expected trusted instruction sources:

```bash
dependabot-watcher setup \
  --repo owner/repository \
  --supervisor-mode approval_first \
  --supervisor-model MODEL_ID \
  --supervisor-reasoning low \
  --supervisor-account chatgpt \
  --supervisor-access inherit \
  --supervisor-workspace owner/repository=/absolute/isolated/worktree \
  --supervisor-instruction owner/repository=/absolute/isolated/worktree/AGENTS.md \
  --acknowledge-supervisor-use
```

Replace the model/path placeholders deliberately. Repeat workspace flags for
each supervised repository and instruction flags in the expected discovery
order. Selected sources are checked during launch; saving their paths is not
proof that a runtime loaded them. Quote shell paths containing spaces.

Optionally add `--supervisor-refresh-workspace owner/repository` to permit
refreshing that registered detached maintenance copy after initial verification
at the collected base. This does not move your development branch or overwrite
unfinished maintenance work. Without it, a stale workspace still blocks launch.
Workspace creation and developer-checkout integration remain separate capabilities.

Access choices are `inherit`, `restricted` with an existing
`--supervisor-permission-profile NAME`, or `full` with the separate
`--acknowledge-supervisor-full-access`. Full-access tools may bypass Watcher's
guarded executor; instructions do not isolate mutation credentials. This choice
is not approval for a particular merge. `--supervisor-validation-authorized`
permits later workspace tests/builds; leave it absent for inspection-only work.
Timeout and job limits can be saved with `--supervisor-timeout-seconds` and
`--supervisor-max-jobs-per-run`; these are workload limits, not spending caps.

Supervisor and metadata advice cannot both be selected in this setup route.
Online setup checks existing CLI authentication without launching a model;
`--offline` skips that check. Neither verifies the actual runtime, workspace,
account/model selection or scheduled operation. Setup still refuses to overwrite
an existing profile. The separate [configuration contract](configuration.md#development-supervisor-configuration)
describes current operating limits.

The development scheduled-supervisor route requires WSL and a deliberately
registered Windows component (`review --native-component PATH`). With
supervision enabled, `run` submits that existing owner quietly, without an open
terminal or automatically showing the window. The owner starts eligible
configured investigations, saves answers and exposes access or recovery needs.
`--notify desktop` enables attention notices; `--notify off` keeps them off.
Process submission is not proof of agent completion. Installed scheduled/live-
provider acceptance and automatic workspace management remain open.

### Reconciliation and native schedule

Exercise the durable workflow manually before installing a schedule:

```bash
dependabot-watcher run
dependabot-watcher run --format json
```

The command writes bounded private reconciliation state but performs no GitHub mutation. Repeating it with unchanged evidence skips repeated model work and suppresses an already delivered notification. `--quiet` suppresses stdout for scheduler use; the private latest-run report remains authoritative.

Preview the exact same-user native registration before creating it:

```bash
dependabot-watcher schedule inventory
dependabot-watcher schedule preview --interval-minutes CADENCE_MINUTES --notify NOTIFICATION_MODE
dependabot-watcher schedule install --interval-minutes CADENCE_MINUTES --notify NOTIFICATION_MODE
```

Replace the placeholders only after confirming the cadence and notification
mode. Use the exact install arguments for status. For disable or removal,
prefer the exact `recovery_command` returned by `schedule inventory` rather
than reconstructing profile and cadence arguments from memory.

Before previewing or installing, choose the cadence and notification mode and decide whether the existing model-off profile is sufficient. If AI is enabled, review the provider, exact model and reasoning settings, maximum calls per changed run, private evidence sent, and quota/cost implications. The workload limits are not financial spending caps. Also keep approval-gated mutation as a separate decision; scheduling does not enable it.

The install receipt reports the recurring profile and, where the native scheduler exposes it, the next scheduled run and whether any native run has completed. Use the exact manual verification command printed in that receipt; it uses the same configuration, state directory, cadence, and notification mode as the installed schedule. Only native task history proves that the scheduler itself has fired.

Inventory is read-only and independent of any one profile. It validates the
native ownership marker, current-user authority, embedded command, and
configuration-derived job ID before returning a recovery command. A similarly
named candidate that fails those checks is reported as untrusted rather than
treated as removal authority.

On WSL, use `--platform wsl --distro EXACT-DISTRIBUTION` when automatic detection is unavailable or when the registration must bind a specific distribution. The task binds the exact Watcher console executable used for installation, the originating Linux user, and trusted Windows System32 WSL/PowerShell helpers; preview displays that actual host-wrapper command. `--notify desktop` is optional. The registered Windows component's notice opens its aggregate review queue; it never grants agent access or merge consent. Other desktop adapters may provide an informational notice only. Adapter acceptance does not prove that the user saw the notification. The host can start WSL without an open terminal, but no local schedule runs while the computer is powered off; Watcher records one overdue catch-up after the host returns instead of replaying every missed interval.

Check or remove only the Watcher-owned registration while preserving configuration, run history, plans, and execution audit:

```bash
dependabot-watcher schedule status --config CONFIG_PATH --state-dir STATE_DIRECTORY \
  --interval-minutes CADENCE_MINUTES --notify NOTIFICATION_MODE
```

Use the exact `recovery_command` from `schedule inventory` for removal.

WSL2 through Windows Task Scheduler has completed live collection, desktop-notification, and scheduled-Codex lifecycle validation. Native Windows has completed isolated installation, current-user configuration/state ACLs, atomic replacement, cross-process locking, offline setup, native Task Scheduler lifecycle, and a scheduled fail-closed launch without native GitHub CLI. Live native-Windows GitHub collection, notification delivery, missed-run catch-up, update/uninstall, and cleanup remain release gates. The systemd and launchd adapters still have deterministic coverage only. None of these private pre-release results is a broad platform support claim until the applicable validation gate closes.

## Approval-gated execution setup

Use the execution option only after verifying the repository's real merge method and exact required-check names. A read-only `inspect --verbosity standard` or `plan --format json` report exposes observed names; confirm them against the repository's branch protection or ruleset rather than guessing:

```bash
dependabot-watcher setup \
  --repo owner/repository \
  --autonomy approval_required \
  --merge-method owner/repository=squash \
  --required-check owner/repository=CI \
  --acknowledge-mutation
```

Repeat `--required-check` for every check the watcher must observe as explicitly successful. Names match exactly and case-sensitively, and ambiguous duplicate names fail closed. A qualifying result is either a completed check run with `conclusion = SUCCESS` or a legacy commit-status context with `state = SUCCESS`; `NEUTRAL`, `SKIPPED`, pending, missing, malformed, unknown, and failing evidence does not satisfy an execution gate. For multiple repositories, supply repository-qualified merge/check settings for each. Check names containing spaces should be shell-quoted.

This setup call still performs no mutation. It records that later `execute` calls may request approval. Each action separately requires a saved JSON plan whose selected repository and target step are complete, a read-only live execution preflight, and the exact approval confirmation token bound to that current repository, PR, base, head SHA, and merge method. Other repositories may remain partial but are not executable from that evidence. The deterministic token is a two-invocation confirmation value, not a secret, signature, separate approver identity, or proof of human interaction. Setup refuses to infer mutation consent from model/provider choices.

With supervision enabled, the exact current proposal must also have a verified
completed agent recommendation to proceed with explicit `ready_for_action = true`
for that exact action. Recommending a candidate for further validation is not
action readiness; older results without this field cannot enable approval.
Missing or outdated reasoning,
`hold`, or a request for further investigation blocks approval readiness.
Changed feedback, workspace or trusted instructions require fresh investigation.
The final executor repeats these checks and records agent attribution; it does
not launch a model or turn its recommendation into approval. Already-merged
exact heads need observation only, not another agent call.

## Doctor

```bash
dependabot-watcher doctor
dependabot-watcher doctor --format json
dependabot-watcher doctor --offline
```

`doctor` reports OS/Python runtime, CLI readiness, configuration/profile status, read-only repository and configured-branch access, Dependabot security-alert read access, default-branch drift, detected Dependabot ecosystems, and support depth for every repository in the loaded profile. It does not accept a repository selector. Use `inspect --repo OWNER/REPOSITORY` for a read-only evidence check limited to one configured repository, while recognizing that scoped inspection does not replace `doctor`'s whole-profile readiness preflight. Doctor's bounded alert probe retrieves at most one open alert per configured repository and retains only the readiness state. It recognizes both `.github/dependabot.yml` and `.github/dependabot.yaml`; missing, inaccessible, malformed, oversized, or structurally excessive configuration remains visible as an attention state rather than being treated as ready.

`doctor` discards raw authentication output. It never shows tokens, reads secret files, invokes a model, writes state, mutates GitHub, sends notifications, or creates schedules. Provider authentication is needed when either advice or supervision is enabled; CLI login readiness does not prove the selected app-server runtime, account/model or workspace. `--offline` still checks local executable versions but skips authentication and repository calls, so its result remains `attention` rather than claiming verified readiness.

The current deeper adapter is limited to bounded Python requirements inclusion evidence. Every detected Dependabot ecosystem still receives generic GitHub-based inspection and planning. Approval-gated execution is based on generic exact PR/check/path evidence rather than ecosystem-specific compatibility claims; no ecosystem is eligible for automatic execution.

## Operational profile

The versioned configuration is also the first operational profile. It stores stable non-secret choices and provenance rather than creating a second source of truth:

```toml
[operation]
autonomy = "read_only"
mutation_enabled = false
report_verbosity = "concise"
machine_output = "json"

[profile]
source = "setup_quick"
created_at = "2026-09-22T12:00:00Z"
confirmed_at = "2026-09-22T12:00:00Z"
last_verified_at = "2026-09-22T12:00:00Z"
```

Credentials, tokens, cookies, raw auth responses, environment-file contents, and private model output are forbidden profile content. Future feedback may propose profile changes, but it may not silently expand repository scope, spending, mutation authority, or policy.

## Profile lifecycle and removal

`doctor` reports the configuration path it resolved and, when execution is enabled, the audit-state location. Setup deliberately has no destructive overwrite flag. To replace a profile, move the existing file to a private backup, create and validate the replacement, then remove the backup only when it is no longer needed. `DEPENDABOT_WATCHER_CONFIG` is the safer choice for parallel experiments or a separate execution profile.

Run `dependabot-watcher schedule inventory` before uninstalling the executable in every host context where scheduling was used. Native Windows PowerShell and WSL select separate scheduler contexts and must each be checked when both were configured. Remove each verified registration with its listed `recovery_command`, then repeat inventory in every applicable context and confirm that no verified registrations remain. A name-matching candidate that inventory reports as untrusted requires manual investigation and must not be removed by name alone. Then uninstall with `uv tool uninstall dependabot-watcher` or `pipx uninstall dependabot-watcher`, matching the installer originally used. Uninstalling leaves configuration, run history, plans, and execution audit evidence in place and has no GitHub effect. Delete local configuration or state only after confirming the exact paths and any retention requirement; deleting an audit log is not a recovery mechanism and does not undo a merge.

Prefer exact published versions for updates and rollbacks. Source-based recovery should use an explicitly reviewed tag or commit, never a moving branch; reinstall that checkout with `uv tool install --force .` or `pipx install --force .`, then rerun `doctor`, `inspect`, and `plan` before enabling execution.
