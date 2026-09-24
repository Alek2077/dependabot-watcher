# Dependabot Watcher

**Turn scattered Dependabot pull requests and security alerts into one clear action list, then safely execute one approved update at a time.**

Dependabot Watcher is a local-first, provider-neutral control plane for Dependabot activity. It focuses on open pull requests authored by Dependabot, connects them to security alerts and recent sensitive changes, and can provide raw inspection facts, optional model-assisted advice, or a deterministic order of actions for handling the queue.

The project is currently a **private pre-release with one narrow approval-gated action**. Inspection, advice, and planning remain non-mutating. An explicitly enabled `execute` command can merge one exact, freshly revalidated Dependabot pull request only after a separate preflight produces an action-bound approval token.

It does not close, comment, approve, label, rebase, create remediation changes, notify, clone target repositories, schedule itself, or execute unattended. The default setup remains model-off and read-only. Approved execution writes a local append-only audit trail; no other runtime command persists product state.

The intended product end state is safe automation of the Dependabot maintenance loop: inspect, plan, recheck, execute only policy-authorized actions, and report what changed or still needs attention. Future provider adapters may use an officially supported subscription-authenticated CLI or an API credential, depending on what each provider permits. Deterministic collection, candidate grouping, sufficient decision-relevant context, and escalation only when useful should keep ordinary runs efficient without assuming that every repository needs the strongest available model.

## Install and set up this private pre-release

The current release is installed from a local source checkout. A package-index installation is planned before public release; Homebrew, standalone installers, and release binaries remain later distribution options rather than current claims.

Prerequisites:

- Python 3.10 or newer;
- [`uv`](https://docs.astral.sh/uv/) (recommended) or [`pipx`](https://pipx.pypa.io/);
- [GitHub CLI](https://cli.github.com/) installed and authenticated; and
- access to every configured repository and its Dependabot alerts.

GitHub authorization is required because Dependabot security alerts and private repository evidence are not anonymous data. The current adapter uses `gh` for read-only GitHub API requests and credential handling. No IDE is required, and the watcher does not inspect or reuse a VS Code login. The credential held by `gh` may have broader permissions than the watcher uses; a tested least-privilege credential profile is a gate for public release.

Clone the private repository and enter its directory:

```bash
git clone https://github.com/Alek2077/dependabot-watcher.git
cd dependabot-watcher
```

Install the command with `uv` (recommended):

```bash
uv tool install .
```

Here `.` means the Python project in the current `dependabot-watcher` directory. `uv` installs it in an isolated user-level tool environment and exposes the `dependabot-watcher` command without modifying the system Python.

Alternatively, install the same checkout with `pipx`:

```bash
pipx install .
```

The recommended setup verifies GitHub CLI authentication and read-only access, discovers the repository's default branch, then creates a non-secret, model-off, read-only profile. Replace `OWNER/REPOSITORY` with the GitHub repository you want to inspect:

```bash
dependabot-watcher setup --repo OWNER/REPOSITORY
```

Setup saves these choices in the watcher's configuration profile, so they do not need to be supplied on every command. It never invokes a model, changes GitHub, schedules work, or sends a notification, and it refuses to overwrite an existing configuration.

Choose the command that matches the result you want:

| Command | Practical result |
|---|---|
| `dependabot-watcher doctor` | Checks whether the installation, GitHub access, configuration, and configured repositories are ready. |
| `dependabot-watcher inspect` | Returns current Dependabot PR, alert, check, and repository facts without using a model. |
| `dependabot-watcher advise` | Adds optional per-PR or per-alert model recommendations when advice is explicitly configured. |
| `dependabot-watcher plan` | Produces an ordered action list for the complete queue, using optional advice if configured; it does not execute the actions. |
| `dependabot-watcher execute` | Revalidates one saved plan step, then either prints its exact approval token or uses that token to merge only the bound PR head. |

Run the preflight, then inspect the evidence or request the ordered action list:

```bash
dependabot-watcher doctor
dependabot-watcher inspect
dependabot-watcher plan
```

Common output and scope options:

```bash
dependabot-watcher inspect --format json                 # Complete machine-readable evidence
dependabot-watcher inspect --format markdown             # Human-readable Markdown
dependabot-watcher inspect --verbosity standard          # Item-level PR and alert details
dependabot-watcher inspect --repo OWNER/REPOSITORY       # One configured repository
dependabot-watcher inspect --config /path/to/config.toml # A non-default profile
dependabot-watcher plan --verbosity detailed             # More human-facing detail
```

Optional advice is a separate, explicit operation:

```bash
dependabot-watcher advise
dependabot-watcher advise --format json
```

Advice defaults to `off`. Enabling `economy` or `reviewed` requires explicit provider configuration; see the [configuration reference](docs/configuration.md).

Advanced setup can record a tested provider choice while still making no model call:

```bash
dependabot-watcher setup \
  --repo OWNER/REPOSITORY \
  --advisory-mode economy \
  --triage-model your-model-id \
  --triage-reasoning high \
  --acknowledge-model-use
```

Use `--offline` only when setup cannot verify GitHub access; the resulting profile remains visibly unverified. Use `--branch owner/repository=branch` for an intentional branch override. See the [setup and diagnostics guide](docs/setup.md).

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

Save a complete machine-readable plan, then run a non-mutating execution preflight for one displayed step ID:

```bash
dependabot-watcher plan --format json > watcher-plan.json
dependabot-watcher execute --plan-file watcher-plan.json --step-id STEP_ID
```

If the exact action is eligible, preflight prints an approval token bound to the saved repository plan, step, repository, PR number, base, head SHA, and merge method. Supplying that token is the separate mutation authorization:

```bash
dependabot-watcher execute \
  --plan-file watcher-plan.json \
  --step-id STEP_ID \
  --approval-token APPROVAL_TOKEN
```

The preflight and mutating invocation repeat deterministic live collection without making another model call. Stable identities are recomputed from the saved plan before use, so editing its material action, evidence, or advice without producing a new plan is rejected and any conservative model conclusion remains binding. The mutating invocation performs one final recheck under an execution lock and submits the planned head SHA to GitHub. Any changed head, base, alert/check evidence, policy, incomplete collection, missing required check, unsupported workflow gate, or invalid token fails closed. Each invocation performs at most one merge. The audit log is stored under the platform state directory (`~/.local/state/dependabot-watcher` on ordinary Linux unless `XDG_STATE_HOME` is set) as a current-user-owned regular file with user-only permissions.

This first execution surface is intentionally not automatic. It accepts only an unblocked `validate_then_merge` step that remains a deterministic baseline candidate. Model advice may veto or make the plan more conservative, but neither a model recommendation nor the plan itself authorizes the merge.

The default config path is `$XDG_CONFIG_HOME/dependabot-watcher/config.toml` or `~/.config/dependabot-watcher/config.toml`. On native Windows it resolves under `%APPDATA%`. `DEPENDABOT_WATCHER_CONFIG` can override it.

Human text and Markdown support `concise`, `standard`, and `detailed` verbosity. The profile default can be overridden per command with `--verbosity`; JSON remains complete at every level, and verbosity never changes collection, model workload, policy, or authority.

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

- `off`: deterministic evidence only and zero model calls;
- `economy`: bounded triage of deterministically prioritized candidates;
- `reviewed`: bounded triage plus a stronger second pass for ambiguous or higher-risk candidates.

The provider boundary is generic, but only the first-party Codex CLI adapter is currently implemented and tested. It runs non-interactively with an ephemeral session, read-only sandbox, isolated temporary work directory, schema-constrained output, ignored Codex user configuration and exec-policy rules, a sanitized process environment, and explicit timeout/model settings. Web search, shell tools, apps/connectors, dependency-install tools, and subagents are disabled. Codex platform safety still applies, while the watcher's own prompt, schema, and deterministic validator define the portable advisory contract.

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
- all output goes to stdout, while CLI errors go to stderr.

Collection limits are explicit. If a repository exceeds one, the report says which surface was truncated and exits as partial rather than silently claiming completeness.

`doctor` is also non-mutating and never makes a model call. It reports executable versions and authentication readiness without retaining raw command output, verifies the configured branch, reads only configured repositories, and never opens provider credential files. `setup` persists one explicitly requested non-secret configuration file. Approved execution is the only GitHub mutation and the only other persistent path: it appends sanitized start/outcome events to a user-private local audit log.

## Output and exit codes

| Code | Meaning |
|---:|---|
| `0` | Complete inspection |
| `2` | CLI or configuration error |
| `3` | GitHub CLI, authentication, collection, or merge-API failure |
| `4` | Partial inspection/advice, or a doctor report requiring attention |
| `5` | Advisory provider could not be initialized |
| `6` | Planning input contract error |
| `7` | Execution eligibility, freshness, approval, or audit error |
| `70` | Unexpected internal failure |

The JSON output is versioned with `schema_version: 1`. See [the architecture](docs/architecture.md), [setup guide](docs/setup.md), [advisory contract](docs/advisory-contract.md), [planning contract](docs/planning-contract.md), [execution contract](docs/execution-contract.md), [output contract](docs/output-contract.md), [configuration reference](docs/configuration.md), and [roadmap](docs/roadmap.md).

## Platform status

The initial implementation targets WSL2 and ordinary Linux. Ubuntu CI covers the supported Python versions once this repository is connected to GitHub. macOS and native Windows are architectural goals, not current support claims.

The private pre-release currently retains Python 3.10 compatibility so it can be validated in the existing WSL environment. Before public release, the project must move its minimum to a still-supported Python version and align packaging metadata, classifiers, static analysis, CI, and installation validation with that floor.

The core commands are scheduler-independent. Task Scheduler, cron/systemd, launchd, GitHub Actions, additional model providers, notifications, and unattended execution belong to later separately tested adapters.

## Development

```bash
python -m pip install -e '.[dev]'
pytest
```

Material features and onboarding changes should also pass the documented
[installed-path validation workflow](docs/validation-workflow.md).

This repository intentionally contains only synthetic examples and fixtures. Private repository identities, workstation paths, schedules, notification settings, and historical runtime artifacts belong outside the product source.
