# Configuration

Dependabot Watcher uses a versioned TOML file. Unknown keys are rejected so spelling mistakes cannot silently change behavior.

The file is operational authority even though it must contain no secrets. Watcher therefore rejects symbolic links, foreign-owned files or parent directories, group/world-writable POSIX paths, oversized files, and files replaced during safe open. Setup creates the platform-appropriate current-user boundary automatically; preserve it when moving or restoring a profile.

```toml
schema_version = 1

[operation]
autonomy = "read_only"
mutation_enabled = false
require_advice_before_execution = false
report_verbosity = "concise"
machine_output = "json"

[collection]
history_lookback_days = 30
max_open_prs = 100
max_open_alerts = 500
max_history_items = 200
max_files_per_pull_request = 500
max_manifest_files = 50
max_manifest_file_bytes = 131072
max_manifest_depth = 5
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

[repositories.workflow]
merge_method = "repository_default"
required_checks = []
deployment_environments = []
validation_notes = []
```

## Operational profile

The configuration is also the current non-secret operational profile. `operation.autonomy` accepts `read_only` or `approval_required`. The booleans must match: `read_only` requires `mutation_enabled = false`, while `approval_required` requires `mutation_enabled = true`. Unsupported or contradictory authority fails closed.

This opt-in enables the execution capability, not any particular action. A mutation still requires a saved plan whose selected repository and target step are complete, an eligible exact step, a fresh live recheck, configured repository policy, and the approval token produced by a preceding non-mutating execution preflight. The overall plan may be partial, but partial evidence or advice outside the selected complete repository/step remains ineligible. Automatic execution is not supported.

`require_advice_before_execution = true` additionally requires current successful advice under the exact configured advisory policy. Budget-deferred, failed, unreviewed, stale, or differently configured advice is ineligible rather than falling through to deterministic execution. Model advice remains a gate, not mutation authority.

`report_verbosity` accepts `concise`, `standard`, or `detailed`. It affects human text and Markdown only. JSON stays complete, and presentation depth never changes evidence collection, advisory workload, policy, or action authority. `machine_output` currently accepts only `json`.

An optional `[profile]` table records `source` (`manual`, `setup_quick`, or `setup_advanced`) plus timezone-bearing ISO-8601 `created_at`, `confirmed_at`, and `last_verified_at` values. Setup writes these fields; hand-written configurations may omit them. They are provenance, not credentials.

Never place tokens, cookies, passwords, environment-file contents, or raw provider authentication responses in this file. The [setup and diagnostics guide](setup.md) documents safe generation and read-only verification.

## Repository entries

`name` must use `owner/repository` form. `default_branch` is the expected Dependabot target; a PR targeting another branch is reported for attention.

Patterns under the top-level `path_policy` apply to every repository. Patterns under `repositories.path_policy` apply only to that repository. Paths are repository-relative and use `/` separators.

Additional dependency patterns identify files that are safe to classify as dependency-related evidence. Additional sensitive patterns make the classification more conservative. They do not authorize mutation.

Each repository may define an optional `workflow` table. `merge_method` accepts `repository_default`, `merge`, `squash`, or `rebase`. `promotion_branch` is optional. `required_checks`, `deployment_environments`, and `validation_notes` are bounded single-line string arrays.

Planning displays all workflow fields as evidence and never executes strings. The initial approval-gated merge surface requires an explicit `merge`, `squash`, or `rebase` method and at least one exact, case-sensitive required-check name. Ambiguous duplicates fail closed. Each required check must be either a completed check run with `conclusion = SUCCESS` or a legacy commit-status context with `state = SUCCESS`. `NEUTRAL`, `SKIPPED`, pending, missing, malformed, unknown, and failing checks do not satisfy the execution gate. It refuses `repository_default` and any configured promotion branch, deployment environment, or free-form validation note because it cannot yet verify those gates. The watcher does not deploy or promote branches.

## Runtime state and audit

`run` writes bounded reconciliation history, the latest private report, and retained current plans under the platform state directory. Records include evidence revision, queue synopsis completeness, advisory coverage and usage provenance, plan lineage, execution observation, notification delivery, missed-run timing, retry state, and sanitized failure categories. They exclude credentials, unrestricted provider transcripts, and raw GitHub errors. Unchanged revisions skip repeated model work; missed intervals produce one current-state catch-up rather than an interval backlog.

Approved execution writes `execution-audit.jsonl` under the platform state directory. On ordinary Linux this defaults to `$XDG_STATE_HOME/dependabot-watcher` or `~/.local/state/dependabot-watcher`; `DEPENDABOT_WATCHER_STATE_DIR` provides an explicit override for isolated validation and managed environments. The directory and file must not be symbolic links or accessible to group/other users.

Audit events contain the action binding, timestamps, outcome, merge commit when available, and recovery guidance. They never contain tokens, credentials, raw provider output, or raw GitHub error text. A sibling exclusive lock prevents concurrent execution. A leftover lock after an interrupted process must be inspected deliberately rather than automatically discarded.

## Collection bounds

The watcher inspects every returned item up to each per-repository limit. These defaults are conservative operational budgets, not expected queue sizes. Large installations can raise them deliberately, but doing so can materially increase sequential GitHub API work.

A limit hit is surfaced as truncation, changes the report status to `partial`, and produces exit code `4`; the watcher never silently treats a bounded collection as complete.

The history lookback is time-bounded separately from the item ceiling. Changed files are fetched through GitHub's paginated pull-request-files endpoint and have their own ceiling. A PR that exceeds it becomes an explicit partial collection error rather than being assessed from an incomplete path list. GitHub returns at most 3,000 files for that endpoint, so configuration intentionally caps this value at 2,999 to preserve one additional record for truncation detection.

If recent merged-history collection is missing or truncated, open PRs cannot receive `baseline_candidate` because sensitive-change evidence is incomplete.

Manifest collection is bounded separately by file count, bytes per file, and inclusion depth. It currently recognizes local Python requirements/constraints inclusions. Unsafe paths, remote URLs, and executable repository content are never followed. A manifest limit or read failure is explicit partial evidence rather than silently inferred coverage.

## Advisory configuration

Model advice is opt-in. A configuration without `[advisory]`, or with `mode = "off"`, makes no provider calls.

Supported modes are:

- `off`: deterministic inspection only, with no AI/model call;
- `economy`: bounded AI triage of prioritized unresolved alerts and pull requests;
- `reviewed`: the same AI triage followed by a bounded second AI reviewer pass for candidates with deterministic attention signals or uncertain/higher-risk triage.

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

The Codex adapter command is fixed to `codex`; configuration cannot redirect execution to another binary or script. Future providers receive separate, explicitly implemented adapters rather than user-selected executable commands.

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

The current budget is global to the selected multi-repository run, not a per-repository allowance. Inspection and planning still cover the complete collected queue, but candidates beyond the advisory budget are recorded as deferred and receive no model decision. Current approval-gated execution does not require every deterministic baseline candidate to have model advice.

Codex authentication stays with the user's Codex installation. Do not put API keys, ChatGPT credentials, or auth files in this TOML file. The adapter ignores the Codex user `config.toml` while retaining Codex authentication. It also ignores user/project exec-policy `.rules`, runs from an isolated temporary directory, disables web search, shell execution, unified execution, apps/connectors, dependency-install tools, and subagents, and gives any model-launched command surface an empty inherited environment. This prevents personal hooks, MCP servers, notifications, telemetry exporters, unrelated project instructions, local files, or shell secrets from becoming implicit advisory behavior.

These flags do not disable Codex's platform safety. The watcher supplies its own provider-neutral triage/review prompt and output schema, and its deterministic Python validator remains authoritative after the model returns. User-specific operating preferences enter through the watcher's explicit operational profile rather than through an ambient Codex harness; see the [roadmap](roadmap.md).

## Scheduling and notifications

Scheduling is deliberately separate from TOML configuration. The native registration binds the exact resolved executable, configuration path, state directory, cadence, notification mode, and, for WSL, distribution name. Use `schedule preview` before `schedule install`; use the same arguments for status, disable, and removal so Watcher derives the same stable job identity and expected definition.

Supported adapter values are `wsl`, `windows`, `systemd`, and `launchd`; `auto` selects from the current platform. Cadence must be 15 to 10,080 minutes. A registration runs as the current user, prevents overlapping Watcher runs, asks the host to run after a missed start where supported, and does not grant GitHub mutation authority. Watcher refuses to overwrite or remove a native definition that lacks its ownership marker.

Schedule identity is derived from the exact configuration path. Use the same configuration, state, platform, cadence, notification, and WSL distribution arguments for ordinary lifecycle commands. If a profile moves, is deleted, or its arguments are forgotten, `schedule inventory` scans the current user's selected native scheduler without loading a profile, validates Watcher's ownership marker, embedded command, current-user authority, and configuration-derived job ID, and returns an exact recovery command. Inventory must be run separately in every host context where scheduling was used; native Windows PowerShell and WSL are separate contexts when both were configured. Candidates that match only by name are reported as untrusted and are never removal authority.

`--notify desktop` is optional and sparse. It sends a bounded action message and next command through Windows PowerShell, macOS `osascript`, or Linux `notify-send`; it does not include the full queue or raw evidence. Failed delivery remains in the durable report and produces a notification-specific nonzero exit without changing the completed reconciliation result.
