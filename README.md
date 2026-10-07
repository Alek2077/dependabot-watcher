# Dependabot Watcher

**Turn scattered Dependabot pull requests and security alerts into one clear action list, then safely execute one approved update at a time.**

Dependabot Watcher is a local-first, provider-neutral control plane for Dependabot activity. It focuses on open pull requests authored by Dependabot, connects them to security alerts and recent sensitive changes, and can provide raw inspection facts, optional model-assisted advice, or a deterministic order of actions for handling the queue.

The current `0.3` release line has one narrow approval-gated action. Inspection, advice, and planning remain non-mutating. An explicitly enabled `execute` command can merge one exact, freshly revalidated Dependabot pull request only after a separate preflight produces an action-bound approval confirmation token.

It does not close, comment, approve, label, rebase an open PR branch, create remediation changes, clone target repositories, or execute mutations unattended. It may use GitHub's `rebase` merge method when explicitly configured. The default setup remains model-off and read-only. Optional `run` and `schedule` commands can persist bounded private reconciliation state, register a same-user native schedule, and deliver sparse desktop notifications; none of those capabilities authorizes GitHub mutation.

The intended product end state is safe automation of the Dependabot maintenance loop: inspect, plan, recheck, execute only policy-authorized actions, and report what changed or still needs attention. Future provider adapters may use an officially supported subscription-authenticated CLI or an API credential, depending on what each provider permits. Deterministic collection, candidate grouping, sufficient decision-relevant context, and escalation only when useful should keep ordinary runs efficient without assuming that every repository needs the strongest available model.

## Install and set up

Install the published package from PyPI with `uv` (recommended) or `pipx`.
Homebrew, standalone installers, and release binaries remain later distribution
options rather than current claims.

Before the first PyPI release, or when evaluating an unreleased revision, install
an explicitly reviewed public tag or commit from a source checkout:

```bash
git clone https://github.com/Alek2077/dependabot-watcher.git
cd dependabot-watcher
git checkout EXACT_TAG_OR_COMMIT
uv tool install .
```

The moving `main` branch is prerelease source, not a reproducible release target.

Prerequisites:

- Python 3.11 or newer;
- [`uv`](https://docs.astral.sh/uv/) (recommended) or [`pipx`](https://pipx.pypa.io/);
- [GitHub CLI](https://cli.github.com/) installed and authenticated; and
- access to every configured repository and its Dependabot alerts.

GitHub authorization is required because Dependabot security alerts and private repository evidence are not anonymous data. The current adapter uses `gh` for credential handling and bounded read-only collection; only explicitly approved `execute` can call the exact-head PR merge endpoint. No IDE is required, and the watcher does not inspect or reuse a VS Code login. The credential held by `gh` may have broader permissions than the watcher uses; a tested least-privilege credential profile is a gate for public release.

Check the active GitHub CLI account before setup:

```bash
gh auth status
gh auth login # only if no suitable account is authenticated
```

Install the command with `uv`:

```bash
uv tool install dependabot-watcher
```

`uv` installs Watcher in an isolated user-level tool environment and exposes
the `dependabot-watcher` command without modifying the system Python.

Alternatively, install with `pipx`:

```bash
pipx install dependabot-watcher
```

The recommended setup verifies GitHub CLI authentication and read-only access, discovers the repository's default branch, then creates a non-secret, model-off, read-only profile. Replace `OWNER/REPOSITORY` with the GitHub repository you want to inspect:

```bash
dependabot-watcher setup --repo OWNER/REPOSITORY
```

Setup saves these choices in the watcher's configuration profile, so they do not need to be supplied on every command. It never invokes a model, changes GitHub, schedules work, or sends a notification, and it refuses to overwrite an existing configuration. Its receipt names the repositories, states whether AI can be used and what that means for provider quota or cost, and identifies the remaining cadence, notification, and recurring-operation decisions.

Choose the command that matches the result you want:

| Command | Practical result |
|---|---|
| `dependabot-watcher doctor` | Checks whether the installation, GitHub access, configuration, and every repository in the loaded profile are ready. |
| `dependabot-watcher inspect` | Returns current Dependabot PR, alert, check, and repository facts without using a model. |
| `dependabot-watcher advise` | Adds optional per-PR or per-alert model recommendations when advice is explicitly configured. |
| `dependabot-watcher plan` | Produces an ordered action list for the complete queue, using optional advice if configured; it does not execute the actions. |
| `dependabot-watcher execute` | Revalidates one saved plan step, then either prints its exact approval confirmation token or uses that token to merge only the bound PR head. |
| `dependabot-watcher run` | Performs one durable, non-mutating inspect/advice/plan reconciliation for manual or scheduled use. |
| `dependabot-watcher schedule` | Inventories, previews, or manages Watcher-owned native host schedules without granting mutation authority. |

Run the preflight, then exercise one durable read-only reconciliation before scheduling. Direct inspection and planning remain available when that is the result you need:

```bash
dependabot-watcher doctor
dependabot-watcher run --notify off
dependabot-watcher inspect
dependabot-watcher plan
```

## Agent operation

Dependabot Watcher is designed to remain usable directly while also giving
agents a stable CLI and versioned JSON contract. The
[agent operator guide](https://github.com/Alek2077/dependabot-watcher/blob/main/docs/agent-operator-guide.md) defines the portable
workflow and authority boundaries. The
[agent support matrix](https://github.com/Alek2077/dependabot-watcher/blob/main/docs/agent-support-matrix.md) distinguishes an agent
being able to operate the CLI, availability of a packaged skill/plugin, and the
watcher's own optional advisory-provider support.

The repository also contains a Phase 8 skills-only plugin prototype with
OpenAI and Claude Code compatibility manifests. It adds no MCP server, hooks,
scheduler, credentials, or mutation code; it teaches a supported agent to
invoke the same installed CLI. The prototype is for local validation and is not
yet published to a public plugin directory. Source-checkout installation and
current client evidence are documented in the
[agent operator guide](https://github.com/Alek2077/dependabot-watcher/blob/main/docs/agent-operator-guide.md#optional-agent-package).

### Why use Watcher with an agent?

A capable agent can investigate a dependency update directly. Watcher supplies repeatable operational machinery that an ad hoc conversation usually has to reconstruct:

- one normalized evidence surface across repositories;
- PR-alert correlation and explicit incomplete-evidence states;
- stable human and machine-readable contracts;
- deterministic safety and repository-policy gates;
- exact repository, PR, base, and head binding;
- one-action execution followed by reinspection and replanning; and
- auditable execution, reconciliation, and recovery evidence.

The agent supplies judgment; Watcher supplies dependable evidence and authority boundaries. Revision deduplication, bounded run history, native scheduling adapters, and sparse desktop notifications are available in the current release line. Deeper tool-capable investigation and unattended mutation remain roadmap capabilities rather than current claims.

Common output and scope options:

```bash
dependabot-watcher inspect --format json                 # Complete machine-readable evidence
dependabot-watcher inspect --format markdown             # Human-readable Markdown
dependabot-watcher inspect --verbosity standard          # Item-level PR and alert details
dependabot-watcher inspect --repo OWNER/REPOSITORY       # One configured repository
dependabot-watcher inspect --config /path/to/config.toml # A non-default profile
dependabot-watcher plan --verbosity detailed             # More human-facing detail
dependabot-watcher plan --plain                          # No animation, color, or terminal styling
```

Interactive text runs use a restrained spinner on stderr for live setup, diagnostics, inspection, advice, planning, and execution revalidation, then add status hierarchy to the final stdout report. Long operations add elapsed-time reassurance on the same transient line. `--plain`, `TERM=dumb`, or `CI` selects stable line-oriented progress without animation or styling. `NO_COLOR` keeps the hierarchy and animation but removes ANSI color. JSON, Markdown, pipes, redirected output, and non-interactive agent runs never receive animation or terminal styling.

Optional advice is a separate, explicit operation:

```bash
dependabot-watcher advise
dependabot-watcher advise --format json
```

Advice defaults to `off` (no AI). `economy` enables bounded AI triage, while `reviewed` adds a second AI review for selected candidates. Enabling either AI mode requires explicit provider configuration; see the [configuration reference](https://github.com/Alek2077/dependabot-watcher/blob/main/docs/configuration.md).

When advice is enabled, selected repository, PR, alert, dependency, changed-path, check, and deterministic-assessment evidence is sent to the configured provider. Candidate and call limits bound workload; they are not financial spending caps. Cost or subscription usage depends on the provider authentication configured outside the watcher.

Advanced setup can record a tested provider choice while still making no model call:

```bash
dependabot-watcher setup \
  --repo OWNER/REPOSITORY \
  --advisory-mode economy \
  --triage-model your-model-id \
  --triage-reasoning high \
  --acknowledge-model-use
```

Use `--offline` only when setup cannot verify GitHub access; the resulting profile remains visibly unverified. Use `--branch owner/repository=branch` for an intentional branch override. See the [setup and diagnostics guide](https://github.com/Alek2077/dependabot-watcher/blob/main/docs/setup.md).

`plan` always collects fresh inspection evidence and uses the configured advisory mode. With the default `off` mode it makes zero model calls. Its concise output shows current repository state followed by an ordered action list for the Dependabot queue, expected alert impact, blockers, and the reinspection gate after every step. Plans never authorize mutation.

A deterministic plan prioritizes collected GitHub, manifest, check, and policy evidence; it is not proof that an update is compatible with the application. For ambiguous, grouped, transitive, major, sensitive, or otherwise complex updates, use optional model advice together with repository-specific tests and deployment checks. Model advice can improve investigation or make an action more conservative, but it cannot authorize mutation or replace validation.

## Approval-gated execution

Execution is off by default. A new profile can opt in only by naming an explicit merge method and at least one required passing check for every selected repository:

```bash
dependabot-watcher setup \
  --repo OWNER/REPOSITORY \
  --autonomy approval_required \
  --merge-method OWNER/REPOSITORY=squash \
  --required-check OWNER/REPOSITORY=CI \
  --acknowledge-mutation
```

Use a prior read-only `inspect --verbosity standard` or `plan --format json` run to discover the exact, case-sensitive observed check names, then verify those names against the repository's branch protection or ruleset. Ambiguous duplicate names fail closed. Execution requires an explicit successful GitHub result for every configured required check: a completed check run with `conclusion = SUCCESS`, or a legacy commit-status context with `state = SUCCESS`. `NEUTRAL`, `SKIPPED`, pending, missing, malformed, unknown, and failing checks do not qualify. Setup refuses to overwrite an existing profile, so either deliberately edit the reviewed profile or create a separate execution profile with `--config` after confirming these settings.

Save a machine-readable plan, then run a non-mutating execution preflight for one displayed step ID. The selected repository plan and target step must be complete; unrelated repositories may remain partial and cannot be executed from that evidence:

```bash
(umask 077 && dependabot-watcher plan \
  --repo OWNER/REPOSITORY \
  --format json > watcher-plan.json)
dependabot-watcher execute --plan-file watcher-plan.json --step-id STEP_ID
```

The saved plan contains private repository evidence. Keep it in a user-private location, do not publish it, and remove it when no longer needed.

If the exact action is eligible, preflight prints an approval confirmation token bound to the saved repository plan, step, repository, PR number, base, head SHA, and merge method. The token confirms the displayed binding; possession of it is not independent authorization. After the operator explicitly authorizes that exact current action, supply the matching token:

```bash
dependabot-watcher execute \
  --plan-file watcher-plan.json \
  --step-id STEP_ID \
  --approval-token APPROVAL_TOKEN
```

The token is a deterministic, non-secret confirmation value—not a signature, separate approver identity, or proof that a human performed preflight. A local caller able to read the complete binding and run both invocations can reproduce it. The safety boundary therefore also depends on trusted local automation, deliberate profile opt-in, live fail-closed revalidation, and the authority of the active GitHub credential.

The preflight and mutating invocation repeat deterministic live collection without making another model call. Stable identities are recomputed from the saved plan before use, so editing its material action, evidence, or advice without producing a new plan is rejected and any conservative model conclusion remains binding. The mutating invocation performs one final recheck under an execution lock and submits the planned head SHA to GitHub. Any changed head, base, alert/check evidence, policy, incomplete collection, missing required check, unsupported workflow gate, or invalid token fails closed. Each invocation performs at most one merge. After a successful merge, the command performs a model-free reinspection and reports the exact action, merge result, remaining PR and alert counts, uncovered-alert count, attention-required count, and remaining recommended-step count. The merge changes the base SHA and invalidates the remaining saved steps for that repository; generate a fresh plan before another action. Partial, malformed, or failed follow-up collection is reported as unavailable and its counts are suppressed; the successful mutation remains explicit and the operator is told to run `inspect` and `plan`. The audit log is stored under the platform state directory (`~/.local/state/dependabot-watcher` on ordinary Linux unless `XDG_STATE_HOME` is set) as a current-user-owned regular file with user-only permissions.

This first execution surface is intentionally not automatic. It accepts only an unblocked `validate_then_merge` step that remains a deterministic baseline candidate. Model advice may veto or make the plan more conservative, but neither a model recommendation nor the plan itself authorizes the merge.

The default config path is `$XDG_CONFIG_HOME/dependabot-watcher/config.toml` or `~/.config/dependabot-watcher/config.toml`. On native Windows it resolves under `%APPDATA%`. `DEPENDABOT_WATCHER_CONFIG` can override it.

Human text and Markdown support `concise`, `standard`, and `detailed` verbosity. Concise inspection shows every item in queues of five or fewer; larger queues use adaptive highlights, always prioritizing attention-required PRs and uncovered alerts and stating how many items are omitted. Standard inspection shows the complete human queue, while detailed inspection adds technical evidence such as checks, path classifications, and URLs. Planning always shows every ordered step; standard adds human-readable overlap/replanning relationships, and detailed adds freshness identities. Every plan explains that a separately enabled `execute` call—not a model or the prose report—recollects and enforces the live execution gates. The profile default can be overridden per command with `--verbosity`; JSON remains complete at every level, and verbosity never changes collection, model workload, policy, or authority.

## What inspection covers

For every configured repository, the command collects:

- open Dependabot pull requests and their current head, base, checks, merge state, and changed paths;
- the configured base branch's exact head SHA;
- open Dependabot security alerts;
- recently merged pull requests within the configured lookback window; and
- recent non-Dependabot changes to sensitive paths.

Pull-request file lists are collected separately with explicit pagination. If their configured ceiling or any other collection bound is reached, inspection reports a partial result instead of assessing an incomplete path list.

It then correlates alerts with obvious Dependabot PRs and classifies changed paths using generic dependency, CI, deployment/infrastructure, authentication/security, runtime-configuration, and Dependabot-configuration categories. Repository-specific patterns can extend the built-ins.

For Python requirements files, inspection follows bounded local `-r`/`--requirement` and `-c`/`--constraint` relationships at the collected base SHA. It never executes repository content or follows URLs, absolute paths, or paths outside the repository. This allows planning to distinguish direct, likely, indirect, and unverified PR/alert relationships without overstating alert coverage.

Generic inspection and planning are not Python-specific: they operate on Dependabot PRs, alerts, checks, changed paths, and repository state exposed by GitHub. Python requirements relationships are the first deeper ecosystem adapter. Until equivalent adapters are implemented and validated, other supported Dependabot ecosystems receive generic evidence rather than unearned ecosystem-specific conclusions. A public support matrix will distinguish generic inspection, deeper analysis, and eligibility for later automatic execution.

`baseline_candidate` is inspection evidence, not merge authorization. Inspection, advice, and planning reports explicitly record `mutation_authorized: false`; authorization exists only for the single exact execution attempt carrying a current matching token.

## Optional advisory modes

- `off`: deterministic evidence only and zero AI/model calls;
- `economy`: bounded AI triage of deterministically prioritized candidates;
- `reviewed`: bounded AI triage plus a stronger second AI pass for ambiguous or higher-risk candidates.

The provider boundary is generic, but only the first-party Codex CLI adapter is currently implemented. Its command, schema, parsing, timeout, failure, economy, and reviewed paths have deterministic coverage. Real Watcher-initiated `economy` and `reviewed` calls, plus a Windows Task Scheduler → WSL → Watcher → Codex run, have completed without mutation; wider installed-path and release evidence remains recorded in the validation workflow. The adapter runs non-interactively with an ephemeral session, read-only sandbox, isolated temporary work directory, schema-constrained output, ignored Codex user configuration and exec-policy rules, a sanitized process environment, and explicit timeout/model settings. Web search, shell tools, apps/connectors, dependency-install tools, and subagents are disabled. Codex platform safety still applies, while the watcher's own prompt, schema, and deterministic validator define the portable advisory contract.

## Scheduled reconciliation

`run` performs one durable, scheduler-independent reconciliation. It collects current evidence, skips repeated model work for an unchanged revision, persists a bounded private history and current plan, observes prior execution outcomes, and decides whether a sparse notification is needed. It never authorizes or performs a GitHub mutation.

```bash
dependabot-watcher run
dependabot-watcher run --quiet --notify desktop
```

`schedule` discovers and manages stable Watcher-owned registrations through Windows Task Scheduler for WSL2 or native Windows, a systemd user timer on Linux, or a LaunchAgent on macOS. Inventory and preview are non-mutating; install/update, status, disable, and remove operate only on verified Watcher identities and refuse unrelated native jobs.

Before installation, choose the repository profile, cadence, notification mode, optional AI mode/models/reasoning/call limits, and approval-gated mutation mode deliberately. The commands below show placeholder syntax; replace the values only after confirming those choices. Preview and install receipts summarize them. On Windows and WSL, native readback also reports the next run time and whether the task has ever run. After installation, use the exact manual verification command in the install receipt; it matches the installed profile and notification mode but does not prove the native scheduler has fired.

```bash
dependabot-watcher schedule inventory
dependabot-watcher schedule preview --interval-minutes CADENCE_MINUTES --notify NOTIFICATION_MODE
dependabot-watcher schedule install --interval-minutes CADENCE_MINUTES --notify NOTIFICATION_MODE
```

Replace the placeholders only after confirming the cadence and notification
mode. Use the exact install arguments for status. For disable or removal,
prefer the exact `recovery_command` returned by `schedule inventory` rather
than reconstructing profile and cadence arguments from memory.

`schedule inventory` does not load a profile or require its configuration file to
still exist. It scans only the current user's native scheduler, validates the
Watcher ownership marker, embedded command, current-user authority, and
configuration-derived job ID, then prints an exact recovery command. A matching
name without that evidence is reported as untrusted and is never removal
authority.

WSL2 scheduling uses Windows Task Scheduler to start the exact distribution, originating Linux user, and Watcher console executable, so an open WSL terminal is unnecessary. Preview shows the actual trusted System32 WSL wrapper and its arguments. If the computer is asleep or powered off, no local process can run; the next available invocation performs one current-state catch-up rather than replaying every missed interval. `--notify desktop` sends bounded attention information without exposing the full private queue or embedding execution authority. The development WSL route now requires the deliberately selected optional Windows component described below; a click opens the saved review queue. Other platform adapters retain their informational notification behavior. Submission confirms adapter acceptance, not visibility. Model calls still follow the profile's explicit advisory mode, model, reasoning, and workload budget.

The Windows Task Scheduler → WSL2 path has completed live collection, desktop-notification, and scheduled-Codex lifecycle validation. Native Windows has completed isolated installation, private ACL/state/locking validation, real Task Scheduler lifecycle, and a scheduled fail-closed launch without native GitHub CLI; live native GitHub collection and the remaining release exercises are still pending. Native Linux systemd and macOS launchd operation remain evidence gates before corresponding support claims.

Model output is treated as untrusted advice. The watcher binds it back to opaque collector-issued candidate IDs, rejects missing/unknown/duplicate results, and retains collector-owned repository, PR, alert, base, and head identity. Provider failure is reported without erasing deterministic inspection evidence.

## Ordered action planning

`plan` keeps responsibilities separate: inspection owns facts, optional advice supplies bounded interpretation, and deterministic planning owns ordering, freshness, prerequisites, conflicts, and validation gates. A provider can make a recommendation more conservative, but cannot promote blocked work, change hard priority classes, invent a target, or make a plan executable.

Every plan is bound to the collected base SHA, PR head SHA, PR update time, alert-snapshot digest, and observation time. Every step requires fresh reinspection before later execution. Optional repository workflow profiles can record merge method, promotion branch, required checks, deployment environments, and validation notes without encoding one organization's release flow into the core.

## Safety contract

`inspect` is deliberately side-effect-free within the product boundary:

- GitHub operations are read-only;
- no model is invoked by `inspect` or by advisory mode `off`;
- no report, cache, state, or log is persisted;
- no notification is sent; and
- final reports go to stdout; interactive progress and CLI errors go to stderr.

Collection limits are explicit. If a repository exceeds one, the report says which surface was truncated and exits as partial rather than silently claiming completeness.

`doctor` is also non-mutating and never makes a model call. It checks every repository in the loaded profile, reports executable versions and authentication readiness without retaining raw command output, verifies each configured branch, reads only configured repositories, and never opens provider credential files. Use a repository-scoped `inspect --repo OWNER/REPOSITORY` when only that repository is in scope; it checks collected evidence but does not replace the whole-profile readiness preflight. Local persistence is explicit: `setup` creates one requested non-secret profile, `run` writes bounded private reconciliation state and retained plans, approved execution appends a private audit trail, and schedule lifecycle commands manage one Watcher-owned native registration. Approved execution remains the only GitHub mutation.

## Output and exit codes

| Code | Meaning |
|---:|---|
| `0` | Successful command completion |
| `2` | CLI, setup, or configuration error |
| `3` | An uncaught direct GitHub transport, authentication, or API operation failed; per-repository collection failures instead produce a partial report |
| `4` | Partial direct report, doctor attention/blockage, or persisted `run` outcome `partial`/`failed`, including GitHub or advisory incompleteness |
| `5` | Advisory provider could not be initialized |
| `6` | Planning input contract error |
| `7` | Execution eligibility, freshness, approval, or audit error |
| `8` | Reconciliation state or run-once contract error |
| `9` | Native schedule definition or lifecycle error |
| `10` | Desktop notification delivery failed; the persisted run report remains authoritative for the reconciliation outcome |
| `70` | Unexpected internal failure |

The JSON output is versioned with `schema_version: 1`. See [the architecture](https://github.com/Alek2077/dependabot-watcher/blob/main/docs/architecture.md), [setup guide](https://github.com/Alek2077/dependabot-watcher/blob/main/docs/setup.md), [advisory contract](https://github.com/Alek2077/dependabot-watcher/blob/main/docs/advisory-contract.md), [planning contract](https://github.com/Alek2077/dependabot-watcher/blob/main/docs/planning-contract.md), [execution contract](https://github.com/Alek2077/dependabot-watcher/blob/main/docs/execution-contract.md), [output contract](https://github.com/Alek2077/dependabot-watcher/blob/main/docs/output-contract.md), [configuration reference](https://github.com/Alek2077/dependabot-watcher/blob/main/docs/configuration.md), and [roadmap](https://github.com/Alek2077/dependabot-watcher/blob/main/docs/roadmap.md).

## Update, reset, uninstall, or roll back

Upgrade to the latest published release with the installer that owns the tool:

```bash
uv tool upgrade dependabot-watcher
# or: pipx upgrade dependabot-watcher
```

To pin or roll back, install an exact published version:

```bash
uv tool install --force dependabot-watcher==0.3.0
# or: pipx install --force dependabot-watcher==0.3.0
```

Published versions are reproducible installation targets; a moving source branch is not.

`doctor` prints the configuration path it actually loaded. Because `setup` refuses to overwrite it, reset safely by moving that file to a backup, running `setup` again, and deleting the backup only after the replacement works. `DEPENDABOT_WATCHER_CONFIG` can keep experimental profiles separate.

Before uninstalling, run `dependabot-watcher schedule inventory` in every host context where Watcher scheduling was used—for example, once in native Windows PowerShell and once in WSL when both were configured. Each invocation inspects only that context's selected native scheduler. Use each listed `recovery_command` to remove forgotten or moved profiles, or use `schedule remove` with the original arguments when they remain available. Repeat inventory in every applicable context and confirm that no verified registrations remain. Untrusted name-matching candidates require manual investigation and must never be removed by name alone. Then uninstall the executable with the installer that created it:

```bash
uv tool uninstall dependabot-watcher
# or: pipx uninstall dependabot-watcher
```

Uninstalling does not delete the configuration or execution audit history and never reverses a GitHub merge. Remove those local files only after locating them with `doctor`, deciding whether the audit record must be retained, and confirming the exact paths. Repository recovery after a bad merge remains an explicit repository workflow; see the execution output and audit guidance before taking another action.

## Platform status

Python 3.11–3.14 is the supported runtime range. The core CLI is validated separately from native background scheduling because each operating system has a different scheduler lifecycle.

| Surface | `0.3` status |
|---|---|
| WSL2 CLI and Windows Task Scheduler operation | Supported after final release-candidate verification |
| Native Windows CLI and Task Scheduler operation | Supported after final release-candidate verification |
| Native Linux CLI | Release gate: hosted CI and installed-path verification |
| Native Linux systemd user scheduling | Experimental pending real native-Linux acceptance |
| macOS CLI | Release gate: hosted CI and installed-path verification |
| macOS launchd scheduling | Experimental pending real macOS acceptance |

Experimental scheduler adapters remain available for evaluation, but they are not yet support claims.

The run-once reconciler remains scheduler-independent. GitHub Actions or another always-on remote runner, additional model providers, richer notification destinations, and unattended mutation remain later separately tested capabilities.

## Development

### Development supervisor and optional Windows review

Unreleased source adds a tool-capable Codex supervisor and one optional aggregate Windows review window. Python owns the saved context, messages, attempts, permissions and exact-action recovery; the window is an interaction channel, not a browser page or a second policy engine. AI-supervised maintenance is the intended primary automation path; the existing deterministic/manual and bounded metadata-advice modes remain available.

The development WSL entry can open an already built, deliberately selected component:

```bash
dependabot-watcher review --native-component /absolute/windows-mounted/path/WatcherNativeProof.exe
dependabot-watcher review
```

Opening it makes no model call or GitHub change. Its receipt confirms process submission only, not visible UI. Saved messages do not themselves launch an agent; the explicit investigation control uses the separately acknowledged [supervisor configuration](docs/configuration.md#development-supervisor-configuration). Failed investigations have a deliberate, context-checked retry; previous attempts and outcomes remain saved. Tool access does not grant an exact merge, and broad agent access cannot claim isolation from ambient credentials or direct remote operations.

The development `notification test` command tests the normal selected component
without resetting reminder suppression or changing GitHub. `notification status`
reads Windows notification settings/history; `notification remove` clears only
that profile's link/notices. Supply the same `--config` and `--state-dir` as
`review`. The toast has one action—open this saved queue—not approve a merge.
See the [native component guide](native/windows/README.md#notification-link-and-diagnostics).

The development queue, feedback, permissions and installed read-only entry have Windows/WSL evidence. Ordinary setup saves supervisor policy; the WSL `run` route can submit the registered native owner quietly for configured investigations. Supervised merge readiness requires current verified affirmative agent reasoning as well as separate exact approval and fresh GitHub checks. Companion distribution/update, workspace provisioning/refresh, complete installed scheduled/live-agent acceptance, reliable foreground activation and an exact supervised merge remain open gates. This is a private development checkpoint, not an unattended-maintenance release. See [architecture](docs/architecture.md#planned-supervisor-access-and-approval-boundary) and [native component source](native/windows/README.md).

```bash
python -m pip install -e '.[dev]'
pytest
```

Material features and onboarding changes should also pass the documented
[installed-path validation workflow](https://github.com/Alek2077/dependabot-watcher/blob/main/docs/validation-workflow.md).

This repository intentionally contains only synthetic examples and fixtures. Private repository identities, workstation paths, schedules, notification settings, and historical runtime artifacts belong outside the product source.

## Support, security, and license

Community support is available through [GitHub Issues](https://github.com/Alek2077/dependabot-watcher/issues) on a best-effort basis without a guaranteed response or resolution time. Report security vulnerabilities privately as described in [SECURITY.md](https://github.com/Alek2077/dependabot-watcher/blob/main/SECURITY.md). Contribution guidance is in [CONTRIBUTING.md](https://github.com/Alek2077/dependabot-watcher/blob/main/CONTRIBUTING.md).

Copyright 2026 Alex Micic. Licensed under the [Apache License 2.0](https://github.com/Alek2077/dependabot-watcher/blob/main/LICENSE).
