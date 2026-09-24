# Output contract

`dependabot-watcher inspect`, `advise`, `plan`, and `execute` support `text`, `json`, and `markdown`. Inspection, advice, and planning are not written to disk by the command. Approved execution writes its separate local audit log.

Human text and Markdown support `concise`, `standard`, and `detailed` verbosity. JSON is identical regardless of the selected human verbosity. Presentation depth never changes evidence collection, provider calls, deterministic policy, or mutation authority.

When both text stdout and stderr are attached to an interactive terminal, long live-read commands may show transient progress on stderr so they do not appear stalled. Progress is suppressed for JSON, Markdown, redirected output, pipes, and non-interactive agent execution. Final reports remain on stdout.

JSON is the automation contract. Its top-level fields are:

- `schema_version`: output schema version, currently `1`;
- `mode`: `inspect`, `advise`, or `plan`, matching the command-specific wrapper;
- `generated_at`: UTC timestamp;
- `status`: `complete` or `partial`;
- `capabilities`: explicit false values for model calls, local persistence, GitHub mutation, and notifications;
- `summary`: aggregate counts; and
- `repositories`: per-repository PR, alert, recent-sensitive-change, truncation, and error evidence.

Every inspected pull request includes collector-owned repository, PR, base, and head identity plus normalized named status-check evidence. `classification` is either `baseline_candidate`, `attention_required`, or `collection_error`. It is not an execution decision, and `mutation_authorized` is always false.

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

Conflict records identify overlapping changed paths or shared alert impact between PR steps. They mean that handling either step can stale the other and trigger reinspection; they do not claim that Git has detected a textual merge conflict.

`expected_alert_impact` contains only direct or likely alert relationships. `related_alerts` retains the complete relationship evidence, including indirect and unverified associations, so structured consumers do not have to interpret mere relatedness as predicted remediation.

## Operational output

`dependabot-watcher doctor` emits a separate versioned report with runtime details, sanitized tool readiness, configuration/profile status, configured repository and branch access, the configuration path actually detected, ecosystems/support depth, findings, and all mutation/model/persistence capabilities explicitly false. A doctor status of `attention` or `blocked` exits with code `4`; the report remains available on stdout.

`dependabot-watcher setup` emits a versioned receipt for the one configuration file it explicitly created, the selected repositories and safe operation settings, verification state, and exact next commands. Its `persistent_local_writes` capability is true because setup is explicitly a configuration-writing command; model calls, GitHub mutation, notifications, and scheduling remain false. Operational reports use the same established `persistent_local_writes` and `github_mutations` capability keys as inspection, advice, and planning.

## Execution output and audit

Calling `execute` without `--approval-token` is a read-only live preflight. A ready report includes the exact target binding, stable action ID, `mutation_performed: false`, `mutation_authorized: false`, and the approval token for that current action. An already-applied report returns no token.

Calling it with the current token repeats the recheck and may return `merged`, `already_applied`, or an error. A merged report includes `mutation_performed: true`, `mutation_authorized: true`, the exact binding, GitHub's bounded merge result, audit path, and recovery/next-step guidance. The authority applies only to that completed attempt; it does not change the saved plan or authorize later work.

The JSON Lines audit uses separate versioned events: `execution_started`, `execution_succeeded`, `execution_failed`, and `execution_reconciled`. Events retain the exact action binding and sanitized outcome without credentials, approval tokens, raw model output, or raw GitHub error text.
