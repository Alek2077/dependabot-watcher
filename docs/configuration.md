# Configuration

Dependabot Watcher uses a versioned TOML file. Unknown keys are rejected so spelling mistakes cannot silently change behavior.

```toml
schema_version = 1

[collection]
history_lookback_days = 30
max_open_prs = 100
max_open_alerts = 500
max_history_items = 200
max_files_per_pull_request = 500
command_timeout_seconds = 120

[path_policy]
additional_dependency_patterns = []
additional_sensitive_patterns = []

[advisory]
mode = "off"

[[repositories]]
name = "example-org/example-app"
default_branch = "main"

[repositories.path_policy]
additional_dependency_patterns = ["deps/*.lock"]
additional_sensitive_patterns = ["ops/production/**"]
```

## Repository entries

`name` must use `owner/repository` form. `default_branch` is the expected Dependabot target; a PR targeting another branch is reported for attention.

Patterns under the top-level `path_policy` apply to every repository. Patterns under `repositories.path_policy` apply only to that repository. Paths are repository-relative and use `/` separators.

Additional dependency patterns identify files that are safe to classify as dependency-related evidence. Additional sensitive patterns make the classification more conservative. They do not authorize mutation.

## Collection bounds

The watcher inspects every returned item up to each per-repository limit. These defaults are conservative operational budgets, not expected queue sizes. Large installations can raise them deliberately, but doing so can materially increase sequential GitHub API work.

A limit hit is surfaced as truncation, changes the report status to `partial`, and produces exit code `4`; the watcher never silently treats a bounded collection as complete.

The history lookback is time-bounded separately from the item ceiling. Changed files are fetched through GitHub's paginated pull-request-files endpoint and have their own ceiling. A PR that exceeds it becomes an explicit partial collection error rather than being assessed from an incomplete path list. GitHub returns at most 3,000 files for that endpoint, so configuration intentionally caps this value at 2,999 to preserve one additional record for truncation detection.

If recent merged-history collection is missing or truncated, open PRs cannot receive `baseline_candidate` because sensitive-change evidence is incomplete.

## Advisory configuration

Model advice is opt-in. A configuration without `[advisory]`, or with `mode = "off"`, makes no provider calls.

Supported modes are:

- `off`: deterministic inspection only;
- `economy`: bounded triage of prioritized unresolved alerts and pull requests;
- `reviewed`: the same triage followed by a bounded reviewer pass for candidates with deterministic attention signals or uncertain/higher-risk triage.

The current implementation ships one adapter, `codex_cli`. Provider-specific model defaults are not prescribed yet, so configure the Codex model deliberately when enabling advice:

```toml
[advisory]
mode = "economy"
provider = "codex_cli"

[advisory.codex_cli.triage]
model = "your-model-id"
reasoning_effort = "high"
timeout_seconds = 900
```

Enabled Codex stages require explicit `model` and `reasoning_effort` values so cost and behavior do not drift with a user's current CLI defaults. `reviewed` mode additionally requires `[advisory.codex_cli.review]` with its own model, reasoning effort, and optional timeout.

Default economy budgets are eight candidates, batches of four, and two triage calls. Reviewed mode adds at most four reviewer candidates and one reviewer call. Override them explicitly when needed:

```toml
[advisory.budget]
max_candidates = 8
batch_size = 4
max_triage_calls = 2
max_review_candidates = 4
max_review_calls = 1
```

Candidate priority is deterministic: unresolved critical/high alerts precede attention-required pull requests, other unresolved alerts, baseline candidates, and low-severity alerts. A provider cannot expand its own queue or budget.

Codex authentication stays with the user's Codex installation. Do not put API keys, ChatGPT credentials, or auth files in this TOML file. The adapter ignores the Codex user `config.toml` while retaining Codex authentication. It also ignores user/project exec-policy `.rules`, runs from an isolated temporary directory, disables web search, shell execution, unified execution, apps/connectors, dependency-install tools, and subagents, and gives any model-launched command surface an empty inherited environment. This prevents personal hooks, MCP servers, notifications, telemetry exporters, unrelated project instructions, local files, or shell secrets from becoming implicit advisory behavior.

These flags do not disable Codex's platform safety. The watcher supplies its own provider-neutral triage/review prompt and output schema, and its deterministic Python validator remains authoritative after the model returns. User-specific operating preferences should eventually enter through the watcher's explicit operational profile rather than through an ambient Codex harness; see the [roadmap](roadmap.md).
