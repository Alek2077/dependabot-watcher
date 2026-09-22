# Dependabot Watcher

> A local-first, provider-neutral control plane for inspecting and safely triaging Dependabot activity across multiple repositories.

Dependabot Watcher is currently a **read-only private pre-release**. It works with existing Dependabot installations, reads multiple repositories from one command, correlates pull requests with security alerts and recent sensitive changes, and produces text, JSON, or Markdown on stdout. It can turn that evidence into a concise deterministic Recommended Order. Optional provider-neutral advice can interpret evidence conservatively without authorizing or performing GitHub changes.

It does not merge, close, comment, approve, label, notify, clone target repositories, or write persistent runtime state. The default configuration invokes no model.

The intended product end state is safe automation of the Dependabot maintenance loop: inspect, plan, recheck, execute only policy-authorized actions, and report what changed or still needs attention. Future provider adapters may use an officially supported subscription-authenticated CLI or an API credential, depending on what each provider permits. Deterministic collection, candidate grouping, sufficient decision-relevant context, and escalation only when useful should keep ordinary runs efficient without assuming that every repository needs the strongest available model.

## Install and inspect

Prerequisites:

- Python 3.10 or newer;
- [GitHub CLI](https://cli.github.com/) installed and authenticated with `gh auth login`; and
- access to every configured repository and its Dependabot alerts.

Install from a checkout with either tool:

```bash
uv tool install .
# or
pipx install .
```

Create user configuration:

```bash
mkdir -p ~/.config/dependabot-watcher
cp config.example.toml ~/.config/dependabot-watcher/config.toml
```

Replace the synthetic repository in that file, then run:

```bash
dependabot-watcher inspect
```

Useful variants:

```bash
dependabot-watcher inspect --format json
dependabot-watcher inspect --format markdown
dependabot-watcher inspect --repo example-org/example-app
dependabot-watcher inspect --config /path/to/config.toml
```

Optional advice is a separate command:

```bash
dependabot-watcher advise
dependabot-watcher advise --format json
dependabot-watcher plan
dependabot-watcher plan --format markdown
```

Advice defaults to `off`. Enabling `economy` or `reviewed` requires explicit provider configuration; see the [configuration reference](docs/configuration.md).

`plan` always collects fresh inspection evidence and uses the configured advisory mode. With the default `off` mode it makes zero model calls. Its concise output shows current repository state followed by an ordered maintenance strategy, expected alert impact, blockers, and the reinspection gate after every step. Plans never authorize mutation.

The default config path is `$XDG_CONFIG_HOME/dependabot-watcher/config.toml` or `~/.config/dependabot-watcher/config.toml`. On native Windows it resolves under `%APPDATA%`. `DEPENDABOT_WATCHER_CONFIG` can override it.

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

`baseline_candidate` is inspection evidence, not merge authorization. Every report explicitly records `mutation_authorized: false`.

## Optional advisory modes

- `off`: deterministic evidence only and zero model calls;
- `economy`: bounded triage of deterministically prioritized candidates;
- `reviewed`: bounded triage plus a stronger second pass for ambiguous or higher-risk candidates.

The provider boundary is generic, but only the first-party Codex CLI adapter is currently implemented and tested. It runs non-interactively with an ephemeral session, read-only sandbox, isolated temporary work directory, schema-constrained output, ignored Codex user configuration and exec-policy rules, a sanitized process environment, and explicit timeout/model settings. Web search, shell tools, apps/connectors, dependency-install tools, and subagents are disabled. Codex platform safety still applies, while the watcher's own prompt, schema, and deterministic validator define the portable advisory contract.

Model output is treated as untrusted advice. The watcher binds it back to opaque collector-issued candidate IDs, rejects missing/unknown/duplicate results, and retains collector-owned repository, PR, alert, base, and head identity. Provider failure is reported without erasing deterministic inspection evidence.

## Recommended Order planning

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

## Output and exit codes

| Code | Meaning |
|---:|---|
| `0` | Complete inspection |
| `2` | CLI or configuration error |
| `3` | GitHub CLI/authentication failure before inspection |
| `4` | Partial result because inspection or advisory processing was incomplete |
| `5` | Advisory provider could not be initialized |
| `6` | Planning input contract error |
| `70` | Unexpected internal failure |

The JSON output is versioned with `schema_version: 1`. See [the architecture](docs/architecture.md), [advisory contract](docs/advisory-contract.md), [planning contract](docs/planning-contract.md), [output contract](docs/output-contract.md), [configuration reference](docs/configuration.md), and [roadmap](docs/roadmap.md).

## Platform status

The initial implementation targets WSL2 and ordinary Linux. Ubuntu CI covers the supported Python versions once this repository is connected to GitHub. macOS and native Windows are architectural goals, not current support claims.

The private pre-release currently retains Python 3.10 compatibility so it can be validated in the existing WSL environment. Before public release, the project must move its minimum to a still-supported Python version and align packaging metadata, classifiers, static analysis, CI, and installation validation with that floor.

The core commands are scheduler-independent. Task Scheduler, cron/systemd, launchd, GitHub Actions, additional model providers, notifications, persistent decision records, and gated mutation belong to later separately tested adapters.

## Development

```bash
python -m pip install -e '.[dev]'
pytest
```

This repository intentionally contains only synthetic examples and fixtures. Private repository identities, workstation paths, schedules, notification settings, and historical runtime artifacts belong outside the product source.
