# Dependabot Watcher

> A local-first, provider-neutral control plane for inspecting and safely triaging Dependabot activity across multiple repositories.

Dependabot Watcher is currently an **inspection-only private pre-release**. It works with existing Dependabot installations, reads multiple repositories from one command, correlates pull requests with security alerts and recent sensitive changes, and produces text, JSON, or Markdown on stdout.

It does not merge, comment, label, notify, invoke a model, clone target repositories, or write runtime state.

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

The default config path is `$XDG_CONFIG_HOME/dependabot-watcher/config.toml` or `~/.config/dependabot-watcher/config.toml`. On native Windows it resolves under `%APPDATA%`. `DEPENDABOT_WATCHER_CONFIG` can override it.

## What inspection covers

For every configured repository, the command collects:

- open Dependabot pull requests and their current head, base, checks, merge state, and changed paths;
- open Dependabot security alerts;
- recently merged pull requests within the configured lookback window; and
- recent non-Dependabot changes to sensitive paths.

Pull-request file lists are collected separately with explicit pagination. If their configured ceiling or any other collection bound is reached, inspection reports a partial result instead of assessing an incomplete path list.

It then correlates alerts with obvious Dependabot PRs and classifies changed paths using generic dependency, CI, deployment/infrastructure, authentication/security, runtime-configuration, and Dependabot-configuration categories. Repository-specific patterns can extend the built-ins.

`baseline_candidate` is inspection evidence, not merge authorization. Every report explicitly records `mutation_authorized: false`.

## Safety contract

`inspect` is deliberately side-effect-free within the product boundary:

- GitHub operations are read-only;
- no model is invoked;
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
| `4` | Partial inspection because a repository read failed or truncated |
| `70` | Unexpected internal failure |

The JSON output is versioned with `schema_version: 1`. See [the architecture](docs/architecture.md), [output contract](docs/output-contract.md), and [configuration reference](docs/configuration.md).

## Platform status

The initial implementation targets WSL2 and ordinary Linux. Ubuntu CI covers the supported Python versions once this repository is connected to GitHub. macOS and native Windows are architectural goals, not current support claims.

The private pre-release currently retains Python 3.10 compatibility so it can be validated in the existing WSL environment. Before public release, the project must move its minimum to a still-supported Python version and align packaging metadata, classifiers, static analysis, CI, and installation validation with that floor.

The core command is scheduler-independent. Task Scheduler, cron/systemd, launchd, GitHub Actions, model providers, notifications, persistent decision records, and gated mutation belong to later separately tested adapters.

## Development

```bash
python -m pip install -e '.[dev]'
pytest
```

This repository intentionally contains only synthetic examples and fixtures. Private repository identities, workstation paths, schedules, notification settings, and historical runtime artifacts belong outside the product source.
