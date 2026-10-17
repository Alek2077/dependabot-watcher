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

### Selected local updates (development)

The optional `repositories.local_sync` array registers up to ten absolute checkout paths and their selected attached branches per repository. Omitted/empty arrays retain remote-only behavior. Each path can belong to only one configured repository; registration is scope, not standing approval. These entries do not change `operation.mutation_enabled`, which governs the separate GitHub merge capability. Fresh local-update reviews include the matching configured `origin` upstream tracking reference, when present, so the approved Git transaction updates both branch and tracking metadata. No unrelated references or upstream settings are changed; absent/different upstreams keep their existing workflow. Tracking-only corrections require their own exact review, not a silent fetch.

```toml
[[repositories.local_sync]]
directory = "/absolute/path/to/selected-checkout"
branch = "SELECTED_BRANCH"
```

Place the entry under the intended repository; replace placeholders with the user's workflow. The development `sync` command and native review channel require their own exact fresh confirmation for clean fast-forward integration. Saved intent/outcomes live in the private `local-sync.json` journal. Bounded historical observations enter supervisor context only when local targets are configured; they neither grant edit authority nor establish live readiness. See [setup and recovery boundaries](setup.md#selected-local-checkout-updates-development) and the [validation record](validation.md#selected-local-checkout-synchronization--development-checkpoint-2026-10-09) for reviewed installed checks and remaining live acceptance.

For the connected development workflow, the repository-level `completion_policy` distinguishes `remote_only` (default) from `selected_local_sync`. Selected mode requires registered checkouts on that repository's configured base branch. It records the obligation with the exact approved merge, rather than treating registration as consent. Fresh remote evidence, configured post-merge agent assessment and live selected-checkout verification are separate from remote merge success. Required unfinished completion blocks another same-repository merge through the durable action-review route; direct `execute` cannot bypass selected completion. This integration is not yet release-accepted. Changing configuration does not erase a saved unfinished obligation or authorize replay. Neither mode implies deployment or production-health verification.

### Reconciliation and execution

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

Metadata advice is opt-in. A configuration without `[advisory]`, or with `mode = "off"`, makes no metadata-advice provider calls. The separate development supervisor below can still invoke a model when explicitly selected and launched.

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

## Development supervisor configuration

This unreleased configuration is separate from metadata advice and defaults to `mode = "off"`. Ordinary `setup` can save its explicit choices (see [supervisor setup](setup.md#tool-capable-supervisor-setup-development)); the development WSL scheduled route uses the registered quiet native owner. The native investigation control accepts only the trusted saved profile; messages cannot choose executable commands, workspace paths, models or access.

```toml
[supervisor]
mode = "approval_first"
provider = "codex_app_server"
model = "selected-model-id"
reasoning_effort = "high"
expected_account_type = "chatgpt"
access = "inherit"
model_use_acknowledged = true
validation_authorized = false
timeout_seconds = 900
max_jobs_per_run = 1
workspaces = [{ repository = "example-org/example-app", directory = "/absolute/isolated/worktree", instruction_sources = ["/absolute/isolated/worktree/AGENTS.md"] }]
```

The registered repository must already be in `[[repositories]]`. This launcher
requires metadata advice off and an owned POSIX/WSL linked worktree at the
freshly collected base. Existing worktrees can still be registered explicitly.
Ordinary setup can also opt into provisioning from an explicitly supplied local
checkout; the saved configuration remains an explicit ordered
instruction-source expectation. Model acknowledgement permits transmitting
repository evidence and saved questions to the selected provider; turns consume
subscription or API usage. Configuration parsing is not live account, harness,
isolation or spend-enforcement evidence.

Provisioning creates a private clone and detached linked maintenance copy under
the Watcher state directory's `supervisor-workspaces/` path, outside the source
checkout. The source is read-only and may be dirty; only committed Git objects
are copied, with no source branch, checkout, index, files or Git configuration
changed, and hooks/filters are disabled during provisioning. Online setup pins
the exact configured-branch SHA already collected from GitHub. If that object
is absent from the supplied checkout, provisioning fails before creating paths;
this version does not fetch missing commits. Offline setup pins only local
`HEAD` and labels it unverified, without claiming network or GitHub-base
verification.

The default setup-wide estimated-size ceiling is 1 GiB across all new copies'
object stores, materialized checkouts and bounded metadata allowances; each
copy must also fit within that ceiling. Setup accepts a higher explicit limit
up to 16 GiB. A free-space reserve is checked before creation
using the same `supervisor.validation.minimum_free_bytes` value (default
512 MiB) and configured `storage_reserve_paths`. On WSL, provisioning requires
an explicit physical-volume reserve path such as `/mnt/c`; Linux guest free
space alone does not establish host capacity. Reserve paths are checked read-only
and mount permissions are never changed. Estimates round files to the target
allocation unit, include per-file/directory, Git-object and loose/packed-ref
metadata reserves,
allow up to twofold line-ending expansion, and refuse pinned `ident` or
`working-tree-encoding` rules rather than guess their output size. These are
preflight estimates, not kernel quotas. Reference metadata is bounded to
100,000 entries and a 256 MiB estimate; non-files Git ref storage is
unsupported. The configuration output and private root must remain outside
selected source checkouts and their common Git metadata directories; the
configuration output must also remain outside persistent workspace storage.
Copies and ownership receipts persist by default for session
continuity and setup never deletes them. The setup report gives the exact path,
estimated size, ceiling, reserve, and retention behavior. Exact-PR validation
copies are separate disposable paths and are cleaned after each job.

Setup records the actual repository-root and maintenance-path ancestor
`AGENTS.md` sources it can discover; it does not create an instruction file.
An absolute root symlink remains unchanged. Relative symlinks must resolve to an
in-repository target after relocation; a relative external target is refused
before writes. Explicit `--supervisor-instruction` values for a provisioned
repository replace the discovered expected list in the supplied order. Setup
does not establish the later Codex global/runtime instruction list, even when
the discovered list is empty; launch still compares the exact runtime list
with the saved expectation before a turn. The setup report shows source and
maintenance-path ancestor instructions when they differ. A local workspace
check does not verify model/account selection or runtime instruction discovery.

With `validation_authorized = true`, `[supervisor.validation]` can additionally select `preparation_authorized = true`, `network_authorized` (default false), `maximum_bytes` (default 268435456), `minimum_free_bytes` (default 536870912), `command_timeout_seconds` (default 120), and `storage_reserve_paths = []`. Watcher validation-tool commands use private exact-revision copies and existing Linux/WSL Bubblewrap, with no host-execution fallback; other Codex tools follow the chosen agent access policy, not this sandbox. Dependencies/probes stay in disposable copies; private caches scoped to repository and identical source tree, hashed receipts and bounded historical execution metadata are retained. History is not a fresh execution or proof that the environment remains equivalent. Recognized credential paths are excluded and reported, but unknown embedded secrets are not scanned. Storage is monitored, not a hard kernel quota; network-enabled code can reach the host network and local services. On WSL, explicitly include the physical volume backing the virtual disk (for example `/mnt/c`) in reserve paths; virtual Linux free space alone does not establish host capacity. Network/preparation choices never authorize a merge or developer-checkout change. The validation record distinguishes installed operating evidence from acceptance still pending for new increments.

Per-workspace `refresh_authorized = true` optionally permits advancing an owned
detached maintenance copy after its initial clean-base registration. It defaults
false and does not authorize edits, developer-checkout synchronization, pushes or
merges. Dirty files, untracked work, attached branches or unexpected commits are
preserved and block this refresh. An interrupted checkout is reconciled only from
a verified clean detached prior or intended head; it is never forcibly retried.
This development capability remains subject to its
own operating acceptance.

| Choice | Accepted values / effect |
|---|---|
| `mode`, `provider` | `off`, or acknowledged `approval_first` with `codex_app_server` |
| `reasoning_effort` | `minimal`, `low`, `medium`, `high`, `xhigh` |
| `expected_account_type` | `chatgpt` or `apiKey`, checked before a turn |
| `access` | `inherit`, `restricted`, `full` |
| Restricted access | Requires `permission_profile`, naming an existing Codex permission profile; returned selection is checked |
| Full access | Requires `full_access_acknowledged = true`; this acknowledgement must be false in other modes |
| Validation | `validation_authorized` is distinct from exact merge authority |
| Limits | Timeout 1–3,600 seconds; configured job limit 1–20; actual operating budget acceptance remains a gate |

Inherited mode uses base Codex settings, not a named CLI configuration preset. The current app-server route refuses a preset it cannot preserve. Inherited/restricted launch needs a connected permission consumer; the current native client offers allow-once, deny and cancel, not session-wide or persistent rule grants. Filesystem restrictions do not establish isolation from credentials, connectors or remote actions; broad access has explicitly weaker enforcement. No access choice substitutes for Watcher's exact-action consent.

When several preview-verified requests are pending, the native client can confirm and allow that displayed batch once. The confirmation includes the exact commands and scope; new requests are not included. Individual decisions remain available. A lost acknowledgement stops batch processing rather than replaying uncertain decisions. The batch is not a classification of commands as read-only or safe.

Validation storage refusals expose the saved budget, reserve and measured allocation after owned cleanup. These are historical measurements, not a live disk-capacity check. Review storage and approve any configuration change separately, or choose a smaller validation scope; Watcher does not silently increase limits or delete unrelated files. Same-context retries retain the last saved refusal until a later successful validation clears the warning; historical failure evidence remains. Remote PR branch updates are not an investigation capability: chat replies and command-access decisions do not add that operation or replace exact merge consent.

The native client's separate **Update a candidate PR branch** section offers a deterministic branch-update review. From an alert, choose the related PR; Watcher switches the visible conversation recipient to that PR before requesting approval. This requires `operation.autonomy = "approval_required"` and `mutation_enabled = true`, an idle repository, and an open, non-draft, same-repository Dependabot PR targeting the configured default branch. Conflicts, unverified mergeability, changed heads, unfinished maintenance and pending branch-update outcomes block a new update. Selecting a candidate or reviewing it makes no remote change.

The fifteen-minute confirmation names the repository, PR, source/target branches, expected PR head and observed target tip. Approval requests GitHub's branch-update operation once, not a merge, local synchronization or deployment. GitHub pins the expected PR head but may include newer target commits while processing; Watcher cannot atomically pin the target tip. Accepted or uncertain submissions remain saved for read-only outcome checks and are never automatically replayed. A definite HTTP rejection is recorded separately. Pending outcomes prevent another same-repository investigation or merge until checked.

Maintenance retains branch-update outcomes and read-only checks even when a PR leaves the active queue. If the head remains unchanged or is rewritten and the outcome cannot be established, **Release unresolved workflow hold** offers a separate local confirmation. It does not cancel GitHub's request, prove that nothing happened, retry a write or start an agent. GitHub may still finish later. Watcher retains the uncertain outcome and revokes the old collected plan before lifting its hold; fresh collection and any further exact approvals are required. Do not release a hold merely to describe an uncertain update as successful.

After observing a changed head that preserves the old PR head and includes the current target, Watcher saves one continuation message and offers one automatic start through the configured investigation route. That route collects fresh evidence and reuses compatible conversation and validation evidence; changed code still needs fresh checks. A repeated outcome check does not restart an investigation. If launch or delivery is uncertain, review saved status and use the normal investigation control deliberately. If the target advanced again or the PR closed, Watcher explains the next step without granting another branch update. Merge approval remains a separate exact confirmation.

Failure and interruption are durable outcomes, not permission to replay. A deliberate retry names the failed current job, passes fresh preflight and records a new attempt linked to the preserved failure. Changing evidence, policy or messages invalidates the old retry selection. This does not retry an uncertain merge.

## Scheduling and notifications

Scheduling is deliberately separate from TOML configuration. The native registration binds the exact resolved executable, configuration path, state directory, cadence, notification mode, and, for WSL, distribution name. Use `schedule preview` before `schedule install`; use the same arguments for status, disable, and removal so Watcher derives the same stable job identity and expected definition.

Supported adapter values are `wsl`, `windows`, `systemd`, and `launchd`; `auto` selects from the current platform. Cadence must be 15 to 10,080 minutes. A registration runs as the current user, prevents overlapping Watcher runs, asks the host to run after a missed start where supported, and does not grant GitHub mutation authority. Watcher refuses to overwrite or remove a native definition that lacks its ownership marker.

Schedule identity is derived from the exact configuration path. Use the same configuration, state, platform, cadence, notification, and WSL distribution arguments for ordinary lifecycle commands. If a profile moves, is deleted, or its arguments are forgotten, `schedule inventory` scans the current user's selected native scheduler without loading a profile, validates Watcher's ownership marker, embedded command, current-user authority, and configuration-derived job ID, and returns an exact recovery command. Inventory must be run separately in every host context where scheduling was used; native Windows PowerShell and WSL are separate contexts when both were configured. Candidates that match only by name are reported as untrusted and are never removal authority.

`--notify desktop` is optional and sparse. It sends a bounded action message,
queue counts and AI-use information; it never includes full evidence, private
paths or an execution command. In the development WSL route, select the optional
Windows component through `review --native-component PATH` or
`notification test --native-component PATH` with the same configuration/state.
Its toast opens that saved aggregate queue through an opaque profile key. A
missing, changed or unavailable component fails visibly rather than reverting
to the short-lived balloon. macOS `osascript`, Linux `notify-send` and the legacy
native-Windows adapter remain informational routes without this review action.

A successful adapter return proves submission acceptance, not that the user saw
it. Failed submission remains in the durable report and produces a
notification-specific nonzero exit without changing the completed reconciliation
result. The separate `notification test` bypasses reminder suppression without
altering it; `notification status` reads Windows settings/history; removal clears
only this profile's lookup and notices, not configuration, queues or schedules.
Later desktop-enabled delivery can register again. These development controls
do not establish cold/warm or scheduled supervisor operating acceptance.
