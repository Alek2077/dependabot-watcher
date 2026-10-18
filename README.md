# Dependabot Watcher

**Turn scattered Dependabot pull requests and security alerts into one clear action list, then safely execute one approved update at a time.**

Dependabot Watcher helps you keep track of dependency updates across your repositories. It brings together Dependabot pull requests, security alerts and check results, connects related updates, and records what was approved and what happened.

Use it with your existing AI agent, or try Watcher's development supervisor to investigate updates and run checks in the background. **Merges require your approval for the specific PR.** Watcher checks it again before merging. It does not currently merge unattended or complete every maintenance task automatically.

## Requirements and current scope

For the current built-in background supervisor, you need:

- Windows x64 + WSL2
- Python 3.11–3.14
- `uv` or `pipx`
- GitHub CLI, authenticated
- Codex CLI, authenticated in WSL
- Watcher's Python backend + matching Windows companion

**The Windows companion is required today, even with its window hidden. Making it optional is [planned](docs/roadmap.md#current-acceptance-priority-approval-first-background-maintenance).**

Scope and alternatives:

- **Current background-supervisor path:** Windows with WSL2, Codex CLI installed and authenticated in WSL, the Python backend and the matching Windows x64 companion. The companion is required for this path even when its window stays hidden; a component-free supervisor is not currently supported.
- **CLI foundation:** stable Python 3.11–3.14, `uv` (recommended) or `pipx`, and authenticated GitHub CLI with access to each target repository and its Dependabot alerts. Basic inspection/planning can run without AI or the Windows companion.
- **AI support:** Watcher's built-in supervisor and bounded-advice adapter currently use Codex CLI. Your existing agent can also operate Watcher's CLI; see the [agent support matrix](docs/agent-support-matrix.md) for verified and unverified integrations.
- **Platform limits:** the review window is Windows-only. Native Windows CLI and scheduler checks have separate evidence and remaining gates; native Linux/macOS scheduling and broader platform acceptance remain experimental or pending. See [platform status](#platform-status). The ZIP includes the companion's runtime libraries; it does not install Python, GitHub CLI, Codex CLI or WSL.

## Choose how to use Watcher

| Your goal | Path |
|---|---|
| Work with your existing agent | Give it Watcher's CLI tools and structured evidence. Your agent investigates within the workflow and permissions you choose. |
| Have Watcher investigate in the background | Try the unreleased [Codex supervisor](docs/setup.md#tool-capable-supervisor-setup-development) from reviewed source. The current WSL path needs the Windows component. |
| Ask AI about the collected facts | Choose `economy` advice or `reviewed` advice with a second pass for selected items. These modes cannot research or run tests. |

We recommend an agent that can investigate and test updates—your own or Watcher's supervisor—rather than relying on opinions about collected facts alone. You choose the supported model, permissions and validation limits. Confirm the exact model and reasoning level before the first Watcher-managed AI call; a newer or available model is not automatically the right usage/cost choice. You can also use Watcher without AI. Setup starts read-only and makes no AI calls until you explicitly enable them. Watcher should fit your workflow, not impose ours. See the [agent operator guide](docs/agent-operator-guide.md) and [setup choices](docs/setup.md#choose-the-operating-path).

We are working toward [complete agent-led maintenance](#direction-complete-agent-led-maintenance): investigate, validate, handle approved changes, update selected working copies and report the results. That full loop is not finished yet.

**Validation status (2026-10-18):** Hosted CI is temporarily suspended. The full local Python 3.14 regression for the `0.4.0a1` source candidate passed **1,821 tests with two skips**, with additional local type, packaging and Windows checks. Hosted CI has not verified the current revision. See the [dated validation evidence and limitations](docs/validation.md#local-alpha-delivery-and-hosted-ci-restriction--2026-10-18).

## Install and set up

During development, install a reviewed source revision with `uv` (recommended) or `pipx`. No published Python release is claimed here. Homebrew and standalone installers remain later distribution options.

Install an explicitly reviewed tag or commit from a source checkout. Public source needs no private repository membership; a private fork or source repository requires access. Collecting Dependabot evidence requires the GitHub authentication described below:

```bash
gh repo clone Alek2077/dependabot-watcher
cd dependabot-watcher
git checkout EXACT_TAG_OR_COMMIT
uv tool install --python 3.14 .
```

The moving `main` branch is prerelease source, not a reproducible release target.

For a pinned source installation, use this README and the `docs/` files from the same checkout for commands, contracts and support status. Web links below that contain `blob/main` show the latest development documentation, which may differ from your installed revision; use the corresponding local file or view it at your selected tag/commit instead.

The example asks `uv` for stable Python 3.14; an existing stable Python 3.11 or newer also satisfies the runtime requirement. For a source install with `pipx`, select that installed interpreter explicitly: `pipx install --python /path/to/stable/python .`.

Prerequisites:

- stable Python 3.11 or newer (not a prerelease interpreter);
- [`uv`](https://docs.astral.sh/uv/) (recommended) or [`pipx`](https://pipx.pypa.io/);
- [GitHub CLI](https://cli.github.com/) installed and authenticated; and
- access to every configured repository and its Dependabot alerts.

GitHub authorization is required because Dependabot security alerts and private repository evidence are not anonymous data. Watcher uses `gh` for credential handling and bounded collection. No IDE is required, and Watcher does not inspect or reuse an editor login. Your GitHub credential may have broader permissions than Watcher uses; see [GitHub access and restricted-credential limitations](docs/setup.md#github-access-and-authentication).

Check the active GitHub CLI account before setup:

```bash
gh auth status
gh auth login # only if no suitable account is authenticated
```

`uv` installs Watcher in an isolated user-level tool environment and exposes the `dependabot-watcher` command without modifying the system Python.

Once a Python package is published, replace `VERSION` with an exact published version:

```bash
uv tool install --python 3.14 'dependabot-watcher==VERSION'
```

Alternatively, install with `pipx` using an installed stable interpreter:

```bash
pipx install --python /path/to/stable/python 'dependabot-watcher==VERSION'
```

The source version string does not establish that the package is published. Updates and rollback are covered in the [profile lifecycle guide](docs/setup.md#profile-lifecycle-and-removal).

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
| `dependabot-watcher run` | Performs durable reconciliation; with supervision configured on WSL, can submit background investigation through the registered Windows owner. It does not authorize a merge. |
| `dependabot-watcher schedule` | Inventories, previews, or manages Watcher-owned native host schedules without granting mutation authority. |

Run the preflight, then exercise one durable read-only reconciliation before scheduling. Direct inspection and planning remain available when that is the result you need:

```bash
dependabot-watcher doctor
dependabot-watcher run --notify off
dependabot-watcher inspect
dependabot-watcher plan
```

## Agent operation

Dependabot Watcher is designed to remain usable directly while also giving agents a stable CLI and versioned JSON contract. The [agent operator guide](https://github.com/Alek2077/dependabot-watcher/blob/main/docs/agent-operator-guide.md) defines the portable workflow and authority boundaries. The [agent support matrix](https://github.com/Alek2077/dependabot-watcher/blob/main/docs/agent-support-matrix.md) distinguishes an agent being able to operate the CLI, availability of a packaged skill/plugin, and the watcher's own optional advisory-provider support.

An optional skills-only plugin prototype teaches supported agents to use the same installed CLI. It adds no MCP server, hooks, scheduler, credentials or mutation code and is not published to a public plugin directory. Installation, compatibility manifests and client evidence are in the [agent operator guide](docs/agent-operator-guide.md#optional-agent-package).

### Why use Watcher with an agent?

A capable agent can investigate a dependency update directly. Watcher supplies repeatable operational machinery that an ad hoc conversation usually has to reconstruct:

- one normalized evidence surface across repositories;
- PR-alert correlation and explicit incomplete-evidence states;
- stable human and machine-readable contracts;
- deterministic safety and repository-policy gates;
- exact repository, PR, base, and head binding;
- one-action execution followed by reinspection and replanning; and
- auditable execution, reconciliation, and recovery evidence.

The agent supplies judgment; Watcher supplies repeatable evidence and authority boundaries. The development supervisor adds tool-capable investigation, bounded isolated validation and post-merge reassessment. Installed and real-use evidence, including separately approved local synchronization, is recorded in [validation](docs/validation.md); fresh-user distribution, broader recovery and platform acceptance remain separate gates. Unattended mutation remains future work.

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

JSON is complete at every verbosity level. Human text and Markdown offer concise, standard and detailed views; presentation never changes evidence, model workload or authority. Interactive text progress goes to stderr and is suppressed for machine-readable or redirected output. See the [output contract](docs/output-contract.md) for verbosity, progress and terminal controls.

`plan` collects fresh evidence and produces an ordered queue with alert impact, blockers and reinspection gates. A deterministic candidate is not proof of application compatibility. Use repository-specific investigation, tests and deployment checks for complex updates; optional advice cannot replace validation or authorize mutation. Setup branch overrides, offline profiles and provider settings are covered in [setup and diagnostics](docs/setup.md).

## Approval-gated execution

Execution is off by default. Enabling it requires an explicit merge method and required successful checks for each repository; enabling the capability does not approve an action. Each merge requires a complete current plan, a read-only preflight, operator approval of the exact PR/base/head/method and a matching confirmation token. Tokens confirm a binding; they are not secrets, independent credentials or proof of human approval.

Watcher rechecks live evidence before one exact-head merge, records a private audit trail and reinspects afterward. Changed or incomplete evidence fails closed. Another action requires a fresh plan; a failed follow-up inspection does not undo or hide a successful merge. Use the [execution setup procedure](docs/setup.md#approval-gated-execution-setup) and [CLI approval sequence, freshness and recovery contract](docs/execution-contract.md).

The development review window also supports a **separately approved PR branch update** to bring target-branch changes into a candidate before validation. This does not approve merging the PR or updating your local checkout. See the [review-window guide](docs/review-window.md) and [branch-update boundary](docs/execution-contract.md#separate-pr-branch-update).

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

`baseline_candidate` is inspection evidence, not merge authorization. Inspection, advice, and planning reports explicitly record `mutation_authorized: false`; authorization for that merge applies only to the single exact execution attempt carrying a current matching token.

## Optional advisory modes

- `off`: deterministic evidence only and zero AI/model calls;
- `economy`: bounded AI triage of deterministically prioritized candidates;
- `reviewed`: bounded AI triage plus a stronger second AI pass for ambiguous or higher-risk candidates.

Only the Codex CLI adapter is currently implemented for bounded advice. These modes interpret collected metadata without research or command tools; they are distinct from the tool-capable supervisor. Before invoking advice, confirm the effective account/usage boundary and exact model/reasoning. Advice uses ambient Codex authentication, not a Watcher-verified account binding; changed or unverifiable authentication requires a new decision.

Selected private repository evidence is sent to the configured provider. Candidate and call limits bound workload, not financial spending. Model output is untrusted: Watcher validates collector-owned identities, rejects invalid batches and preserves deterministic evidence when a provider fails. See [provider setup and consent](docs/setup.md#bounded-advice), the [advisory contract](docs/advisory-contract.md) for evidence, isolation and validation details, and [configuration](docs/configuration.md) for limits.

## Scheduled reconciliation

`run` performs one durable, scheduler-independent reconciliation. It collects current evidence, skips repeated model work for an unchanged revision, persists a bounded private history and current plan, observes prior execution outcomes, and decides whether a sparse notification is needed. It never authorizes or performs a GitHub mutation.

```bash
dependabot-watcher run
dependabot-watcher run --quiet --notify desktop
```

`schedule` manages verified Watcher-owned registrations through Windows Task Scheduler for WSL2 or native Windows, a systemd user timer on Linux, or a LaunchAgent on macOS. Inventory and preview are non-mutating; lifecycle commands refuse unrelated jobs. Choose cadence, notifications, models and usage limits explicitly before installing. Scheduling grants no merge authority.

WSL can run without an open terminal. No local schedule runs while the computer is asleep or powered off; the next available run performs one current-state catch-up rather than replaying missed intervals. Desktop notices contain bounded attention information, never action authority; adapter acceptance is not proof of visible delivery. The current WSL review-notification route requires the selected Windows component.

Use the [scheduling setup and recovery procedure](docs/setup.md#reconciliation-and-native-schedule) for exact commands, receipts, ownership checks and verification. [Platform status](#platform-status) and [validation evidence](docs/validation.md) distinguish implemented adapters from verified native operation.

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

`doctor` is also non-mutating and model-free. It checks every repository in the loaded profile without retaining raw authentication output or opening provider credential files. Use `inspect --repo OWNER/REPOSITORY` for a repository-scoped evidence check; that does not replace whole-profile readiness. Local persistence is explicit: setup writes the requested profile, reconciliation retains private state and plans, approved actions retain audit/recovery evidence and schedule lifecycle commands manage verified Watcher registrations. Exact-head merges and development PR branch updates have separate approval boundaries; local integration is separately approved too.

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

Read the selected version's [GitHub release notes](https://github.com/Alek2077/dependabot-watcher/releases) and update requirements first. Python and the Windows companion share one version and source revision. Experimental versions use an alpha/beta/release-candidate suffix and GitHub prerelease status; an unreleased source version is not proof that downloadable artifacts exist. Maintainers follow the [version and release procedure](docs/development-and-release.md#versions-and-release-notes).

For an existing WSL/uv installation using the Windows interface, use the development [coordinated update procedure](docs/setup.md#coordinated-wsluv-update-development) when the selected source revision provides it. It stages both components, closes verified Watcher owners gracefully and preserves the existing task. Do not replace a running backend with a simple reinstall.

The [profile lifecycle guide](docs/setup.md#profile-lifecycle-and-removal) covers pinned source/package updates, rollback compatibility, profile backup/reset and removal. Before uninstalling, inventory and remove verified Watcher schedules in every host context where they were used. Keep configuration and audit history until you deliberately decide their retention; uninstalling never reverses a GitHub merge.

## Platform status

Python 3.11–3.14 is the supported runtime range. The core CLI is validated separately from native background scheduling because each operating system has a different scheduler lifecycle.

| Surface | Current development status |
|---|---|
| WSL2 CLI and Windows Task Scheduler operation | Supported after final release-candidate verification |
| Native Windows CLI and Task Scheduler operation | Supported after final release-candidate verification |
| Native Linux CLI | Release gate: hosted CI and installed-path verification |
| Native Linux systemd user scheduling | Experimental pending real native-Linux acceptance |
| macOS CLI | Release gate: hosted CI and installed-path verification |
| macOS launchd scheduling | Experimental pending real macOS acceptance |

Experimental scheduler adapters remain available for evaluation, but they are not yet support claims.

The run-once reconciler remains scheduler-independent. GitHub Actions or another always-on remote runner, additional model providers, richer notification destinations, and unattended mutation remain later separately tested capabilities.

## Direction: complete agent-led maintenance

The intended end state is a closed maintenance loop: scheduled agents investigate dependency changes, prepare isolated environments, run relevant checks, choose an evidenced order, perform policy-authorized merges or repairs, reconcile selected working copies and report outcomes. Users choose capabilities and approval levels; routine work should not become repeated technical homework. Full unattended operation and broader provider support remain future capabilities, not current claims.

Development source supports [separately approved clean fast-forward checkout updates](docs/setup.md#selected-local-checkout-updates-development). It does not silently substitute that method for a user's dirty/divergent integration or promotion workflow. A remote merge alone does not update your checkout or establish deployment health. General conflict resolution, repair publication, production-health orchestration, unattended merges, component-free supervision and email remain future work. The [roadmap](docs/roadmap.md#agreed-supervisor-architecture--development-path-operating-acceptance-open) owns the intended scope; the [connected validation record](docs/validation.md#connected-supervised-completion--development-checkpoint-2026-10-11) records successes, earlier failures and remaining acceptance gates.

## Development

### Development supervisor and Windows review

See the [review-window guide](docs/review-window.md) for the two tabs, refresh controls, conversations and separate approval steps.

Development source adds a tool-capable Codex supervisor and one aggregate Windows review window. Python owns saved context, messages, attempts, permissions and exact-action recovery; the window is an interaction channel, not a browser page or a second policy engine. Existing-agent CLI operation and bounded metadata advice remain available.

The basic CLI works without the Windows component. The current WSL scheduled AI supervisor requires it installed, although the window can stay hidden. Use the [separate packaged component](docs/setup.md#windows-component-for-wsl-supervision), not an SDK build, for end-user setup. Before a public release archive is available, the matching package must be explicitly supplied. Component-free AI operation is post-release work; optional window visibility does not mean the current component is optional for that route.

Use the [supervisor setup guide](docs/setup.md#tool-capable-supervisor-setup-development) to configure investigation and select the component. Opening the review queue makes no model call or GitHub change; process submission is not proof of visible UI. Supervised merge readiness requires current verified affirmative reasoning, separate exact approval and fresh GitHub checks. Broad agent access does not imply isolation from ambient credentials or direct remote operations.

Conversation, retry and permission behavior is described in the [review-window guide](docs/review-window.md); notification testing and cleanup are in the [native component guide](native/windows/README.md#notification-link-and-diagnostics). Current architecture and acceptance evidence live in [architecture](docs/architecture.md#planned-supervisor-access-and-approval-boundary) and [validation](docs/validation.md#subsequent-uninterrupted-automatic-run). These development capabilities are not an unattended-maintenance release.

### Contributing and validation

```bash
python -m pip install -e '.[dev]'
pytest
```

Material features and onboarding changes should also pass the documented [installed-path validation workflow](https://github.com/Alek2077/dependabot-watcher/blob/main/docs/validation-workflow.md).

This repository intentionally contains only synthetic examples and fixtures. Private repository identities, workstation paths, schedules, notification settings, and historical runtime artifacts belong outside the product source.

## Support, security, and license

Community support is available through [GitHub Issues](https://github.com/Alek2077/dependabot-watcher/issues) on a best-effort basis without a guaranteed response or resolution time. Report security vulnerabilities privately as described in [SECURITY.md](https://github.com/Alek2077/dependabot-watcher/blob/main/SECURITY.md). Contribution guidance is in [CONTRIBUTING.md](https://github.com/Alek2077/dependabot-watcher/blob/main/CONTRIBUTING.md).

Copyright 2026 Alex Micic. Licensed under the [Apache License 2.0](https://github.com/Alek2077/dependabot-watcher/blob/main/LICENSE).
