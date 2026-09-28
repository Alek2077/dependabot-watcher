# Output contract

`dependabot-watcher inspect`, `advise`, `plan`, `execute`, `run`, and `schedule` support `text`, `json`, and `markdown`. Inspection, advice, and planning are not written to disk by those direct commands. Approved execution writes its separate local audit log; `run` writes bounded private reconciliation state; schedule lifecycle commands may change only the derived Watcher-owned native registration.

Human text and Markdown support `concise`, `standard`, and `detailed` verbosity. JSON is identical regardless of the selected human verbosity. Presentation depth never changes evidence collection, provider calls, deterministic policy, or mutation authority.

When both text stdout and stderr are attached to an interactive terminal, live setup, diagnostics, inspection, advice, planning, and execution revalidation show a restrained animated spinner on stderr so they do not appear stalled. Long operations add elapsed-time reassurance on the same transient line. Final reports remain on stdout and may use color, symbols, and a light status hierarchy. `NO_COLOR` removes ANSI color without removing semantic hierarchy. `--plain`, `TERM=dumb`, and `CI` use stable line-oriented progress with no animation or styling. JSON, Markdown, redirected output, pipes, and non-interactive agent execution suppress progress and decoration entirely.

Terminal capabilities affect presentation only. Unsupported Unicode falls back to ASCII markers and spinner frames. Human renderers flatten line breaks and remove terminal control sequences from repository/provider-controlled values; versioned JSON retains the original structured evidence. Progress never appears on stdout, and presentation cannot change collection, advice, planning, or authorization.

JSON is the automation contract. Command reports share a versioned wrapper, but not every operational receipt has every evidence field. The complete mode/status matrix is:

- `setup`: `created`;
- `doctor`: `ready`, `attention`, or `blocked`;
- `inspect`, `advise`, and `plan`: `complete` or `partial`;
- `execute_preflight`: `ready` or `already_applied`;
- `execute`: `merged` or `already_applied`;
- `run`: report `status` is `complete` or `partial`, with the more specific outcome documented below; and
- `schedule`: `preview`, `active`, `disabled`, `missing`, `drifted`, or `removed`.

Top-level fields are:

- `schema_version`: output schema version, currently `1`;
- `mode`: the command-specific wrapper named above;
- `generated_at`: UTC timestamp on time-bearing reports; schedule lifecycle reports omit it;
- `status`: one of the command-specific values above;
- `capabilities`: explicit booleans for model calls, local persistence, GitHub mutation, and notifications; and
- `mutation_authorized`: present on inspection/advice/planning/execution/reconciliation reports and always false outside token-bearing execution. Setup, doctor, and schedule receipts express authority through their capability fields and command-specific contract instead.

Evidence wrappers additionally contain aggregate `summary` counts and per-repository PR, alert, recent-sensitive-change, truncation, and error evidence. Reconciliation and schedule wrappers use the command-specific fields defined below rather than pretending to share the inspection shape.

Every inspected pull request includes collector-owned repository, PR, base, and head identity plus normalized named status-check evidence. Each status-check record preserves whether GitHub supplied a Checks API `check_run` or a legacy commit `status_context`; duplicate names remain visible instead of being collapsed. `classification` is either `baseline_candidate`, `attention_required`, or `collection_error`. It is not an execution decision, and `mutation_authorized` is always false.

An open alert is called `likely_covered_by_open_pr` only when a verified Dependabot PR matches the exact package, ecosystem branch, and manifest path, and its target release meets the alert's first-patched threshold. For plain numeric dotted releases, the report distinguishes `exact`, `equivalent_release` (for example `2.9` versus `2.9.0`), `newer_release`, and `older_release`. Qualified or otherwise non-numeric versions remain `unverified` rather than being guessed. A related PR without complete evidence remains `related_open_pr_unverified` and still counts as unresolved. Open alerts possibly associated with a recent merge also remain in the unresolved count until GitHub closes them.

Alerts also expose `package_related_open_pull_requests` when a verified Dependabot PR matches the package and ecosystem but not necessarily the alert's manifest path. Each such relationship states whether the manifest path matches, how the PR target relates to the first-patched version, and the already-collected deterministic PR assessment (checks, merge state, path classification, and blockers). This is evidence for advisory analysis, not proof of coverage: a manifest mismatch never upgrades the alert classification. Pull-request assessments contain the corresponding `related_alerts` evidence so an advisory provider does not have to rediscover the relationship from separate report sections.

Inspection additionally records the collected base SHA, evidence observation time, workflow profile, and bounded manifest graph. PR/alert relationships use four planning evidence levels: `direct` for exact-manifest and patched-version evidence; `likely` when a collected manifest inclusion connects the alert manifest to the PR's changed manifest and the target meets the patch threshold; `indirect` for package/ecosystem/patch evidence without a collected manifest path; and `unverified` when patch or relationship evidence is incomplete. Only the existing exact-manifest rule changes the alert's fail-closed inspection classification.

Errors and truncations remain in the report so scheduled wrappers and future agents can distinguish “nothing needs attention” from “inspection was incomplete.”

## Advisory output

`dependabot-watcher advise` returns a separate versioned wrapper rather than changing inspection schema version 1 silently. It contains:

- the complete underlying `inspection` report;
- the configured advisory mode and provider;
- the applied workload budget;
- collector-bound candidate decisions;
- separate triage and optional review advice;
- an `effective_advice` view for concise consumers;
- deferred candidates and exact budget/incomplete-evidence reasons;
- provider errors and per-call model/reasoning/usage provenance; and
- explicit false capability values for persistence, GitHub mutation, and notifications;
- report-level `mutation_authorized: false`; and
- `mutation_authorized: false` on every decision and deferred candidate.

Recommendations such as `merge_candidate` mean only that the provider considers the pull request suitable for a later deterministic authorization stage. Human-readable text and Markdown output also state that mutation is not authorized.

The text output is intentionally concise. JSON retains the complete inspection, advice, evidence, provenance, errors, and deferrals. Markdown provides a standard decision table and explicit error/deferred sections.

## Planning output

`dependabot-watcher plan` returns a versioned planning report containing:

- a stable overall plan ID and per-repository plan IDs;
- the current repository state and workflow profile;
- the base-SHA and alert-snapshot freshness binding;
- a deterministic Recommended Order over the complete inspected queue;
- per-step identity, action, priority reason, prerequisites, conflicts, blockers, expected alert impact, optional effective advice, validation gates, and freshness evidence;
- explicit unresolved collection/truncation evidence; and
- the complete source advisory/inspection report for agent and audit consumers.

Human text and Markdown emphasize current state and the ordered next steps. JSON retains the complete graph. `validate_then_merge` is a recommendation, not authorization: every report and step has `mutation_authorized: false`, and every step has `reinspection_required: true`. Phase 6 can separately consume one saved step only after current evidence matches and an exact approval token is supplied.

Human planning output identifies whether the plan used deterministic policy alone or was informed by configured optional model advice. The deterministic planner owns ordering and action constraints in both cases.

Concise inspection is a bounded highlights view rather than an implied complete item list. It shows all items for queues of five or fewer; larger queues show approximately twenty percent with a floor of five and ceiling of ten, prioritizing attention-required PRs and uncovered alerts. The heading and omitted count identify that selection explicitly. Standard inspection shows the complete human item list, detailed inspection adds technical evidence, and JSON remains complete at every level.

Planning never removes ordered steps for concision. Standard planning renders overlapping changed paths or shared alert impact as human-readable replanning relationships; the internal step identities remain in JSON. These relationships mean that handling either step can stale the other and trigger reinspection; they do not claim that Git has detected a textual merge conflict. Universal execution gates are summarized once in human output rather than repeated beneath every step. The structured plan retains every individual validation-gate record, and the separately enabled executor enforces the applicable gates from freshly collected evidence rather than trusting a user or model to follow prose.

`expected_alert_impact` contains only direct or likely alert relationships. `related_alerts` retains the complete relationship evidence, including indirect and unverified associations, so structured consumers do not have to interpret mere relatedness as predicted remediation.

## Operational output

`dependabot-watcher doctor` emits a separate versioned report with runtime details, sanitized tool readiness, configuration/profile status, configured repository and branch access, bounded Dependabot-alert read readiness, the configuration path actually detected, ecosystems/support depth, findings, and all mutation/model/persistence capabilities explicitly false. A doctor status of `attention` or `blocked` exits with code `4`; the report remains available on stdout.

`dependabot-watcher setup` emits a versioned receipt for the one configuration file it explicitly created, the selected repositories and safe operation settings, verification state, and exact next commands. Its `persistent_local_writes` capability is true because setup is explicitly a configuration-writing command; model calls, GitHub mutation, notifications, and scheduling remain false. Operational reports use the same established `persistent_local_writes` and `github_mutations` capability keys as inspection, advice, and planning.

## Reconciliation output

`dependabot-watcher run` emits a versioned durable report with `mode = "run"`. It contains a unique run ID, outcome, stable evidence revision, complete queue synopsis, advisory coverage and provider usage, post-action execution observation, schedule/catch-up observation, current plan and lineage, retry boundary, sparse notification and delivery state, sanitized failure category, state/report paths, and the underlying inspection. Outcomes include `action_required`, `no_work`, `unchanged`, `partial`, `failed`, and `retry_deferred`.

The report always states `mutation_authorized: false`. Its capability flags reflect whether that run attempted a model call, wrote private local state, or delivered a notification; `github_mutations` remains false. Provider usage distinguishes attempted from successful calls so a timeout or failed call is never reported as model-free. `--quiet` suppresses stdout without changing the persisted report or notification decision. A fatal GitHub collection failure persists only a generic sanitized category and retry state rather than raw CLI output.

## Schedule output

`dependabot-watcher schedule` emits `mode = "schedule"` with action, status, changed flag, stable job identity, exact run command, configuration/state paths, cadence, authority, output destination, definition digest, native readback state, missed-run policy, overlap policy, and next step. Preview never changes local state. Install, disable, and remove mark `persistent_local_writes: true` because they may change the host scheduler; every schedule report keeps model calls, notifications, and GitHub mutation false.

Status distinguishes `active`, `disabled`, `missing`, and `drifted`. Watcher normalizes native-equivalent definitions where the host omits defaults or rewrites an equivalent duration, but reports material changes to identity, command, cadence, authority, or safety settings as drift. A still-loaded owned native job whose definition file disappeared is reported as orphaned drift and remains removable through its stable identity. Launchd uses a private Watcher registration record rather than treating unstable human-formatted `launchctl print` text as an ownership API. Lifecycle commands refuse an unrelated native job at the derived Watcher identity.

## Execution output and audit

Calling `execute` without `--approval-token` is a read-only live preflight. It checks any existing selected reconciliation state before returning a token, without creating state when none exists. A ready report includes the exact target binding, stable action ID, `mutation_performed: false`, `mutation_authorized: false`, and the deterministic approval confirmation token for that current action. This token provides exact-action two-step confirmation; it is not an independent credential, signature, approver identity, or proof of human interaction. An already-applied report returns no token.

Calling it with the current token repeats the recheck and may return `merged`, `already_applied`, or an error. When the selected state directory contains reconciliation history, execution also requires the submitted plan to be its current non-revoked plan; a newer or no-work reconciliation invalidates the older token-bearing plan. A merged report includes `mutation_performed: true`, `mutation_authorized: true`, the exact binding, validated deterministic decision context, GitHub's bounded merge result, audit path, and recovery/next-step guidance. It also includes a model-free post-action observation with remaining PR, alert, uncovered-alert, attention-required, and recommended-step counts when that follow-up collection is complete and structurally valid. A partial, malformed, or failed follow-up collection is represented as unavailable and its counts are suppressed without disguising or reversing the already completed merge. The authority applies only to that completed attempt; it does not change the saved plan or authorize later work.

The JSON Lines audit uses separate versioned events: `execution_started`, `execution_succeeded`, `execution_failed`, and `execution_reconciled`. Events retain the exact action binding and sanitized outcome without credentials, approval tokens, raw model output, or raw GitHub error text.
