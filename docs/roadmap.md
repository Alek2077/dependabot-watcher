# Product roadmap

Dependabot Watcher is being restored in bounded phases. This roadmap records product boundaries and future design constraints; it is not a promise that every listed capability ships in the first public release.

## Delivered foundation

- Side-effect-free, model-free inspection across configured repositories.
- Deterministic PR, alert, check, path-risk, and recent-sensitive-change assessment.
- Text, JSON, and Markdown stdout with explicit partial-state reporting.
- Optional provider-neutral advice with `off`, `economy`, and `reviewed` modes.
- One isolated Codex CLI adapter with explicit model/reasoning configuration.
- Collector-owned candidate identity and advisory-only model output.

## Next: deterministic recommended-order planning

Before adding mutation, scheduling, or many more providers, add a first-class planning layer for non-trivial queues:

- `inspect` owns collected facts;
- `advise` supplies optional bounded model analysis;
- `plan` produces a concise, ordered maintenance strategy; and
- a later `execute` consumes only a fresh, validated plan under explicit action authority.

A plan should include repository state, ordered steps, prerequisites and conflicts, expected alert impact, a validation gate after each step, explicit deferrals, unresolved evidence, and a concise Recommended Order suitable for a human or supervising agent. The human-facing default should resemble an operational briefing rather than a wall of per-candidate records; the complete graph and evidence remain available in structured output.

The core must own plan identity, dependency relationships, freshness, and hard ordering constraints. Optional model advice may propose relationships or explain tradeoffs, but it cannot invent targets, clear blockers, or make the plan executable by itself.

Improve correlation beyond a binary covered/uncovered state. Preserve evidence levels such as direct, likely, indirect, and unverified coverage so grouped PRs and transitive alert fixes can be represented without overstating certainty.

Add bounded ecosystem evidence where it materially improves planning. This includes manifest inclusion/inheritance relationships, resolved dependency versions, lockfile effects, and grouped-update membership. Collect such evidence through explicit adapters or repository APIs; do not ask a model to guess relationships from filenames. The planner may still request a bounded checkout-based investigation when API evidence cannot answer the question, but that must be an explicit capability with a disposable workspace and audit trail.

Add an optional repository workflow profile for facts such as staging-to-main promotion, deployment gates, required validations, permitted merge methods, and repository-specific policy. Universal defaults must not encode one organization's workflow.

Bind every plan to its evidence snapshot: repository/base state, PR head SHA, check timestamp, alert snapshot, collection completeness, and plan creation time. A relevant remote change makes the affected plan step stale and requires reinspection before approval or execution.

Dogfood this layer against real multi-item queues before treating the exact presentation or ordering policy as settled user demand.

## Then: setup, deployment, and operational profile

The next product layer should reduce setup friction without weakening consent, privacy, or deterministic safety.

### Read-only environment discovery

Add a `doctor` or equivalent preflight command that can report, without changing the machine:

- operating system and supported runtime details;
- installed provider CLIs, versions, and supported adapter capabilities;
- authentication readiness as `available`, `unavailable`, or `unknown`, never credential values;
- GitHub CLI availability and access to configured repositories;
- supported scheduler and notification adapters;
- resolved configuration and state locations; and
- stale, conflicting, or unsupported configuration.

Discovery must use documented provider interfaces or bounded status commands. It must not open secret files, copy tokens, enumerate unrelated user data, silently authenticate, or contact a paid model merely to determine availability.

### Assisted setup

Add an explicit setup flow that converts discovery evidence into recommendations while keeping user choices inspectable:

- a quick recommended path for users who want minimal decisions;
- an advanced path for provider, model, cost, scheduling, autonomy, and reporting controls;
- provider-specific presets established and tested one provider at a time;
- clear confirmation before the first paid/model call, notification, schedule, or mutation capability; and
- a scheduler-independent `run once` path.

Agents may drive this flow, but the underlying commands and configuration must remain understandable and usable without an agent.

### Durable operational profile

Persist stable, non-secret facts and choices in a versioned provider-neutral profile outside the installed package. Candidate fields include:

- selected repositories and expected branches;
- provider adapter, model preset, reasoning level, and workload budget;
- merge method and repository-policy preferences;
- autonomy level and explicit mutation authorization state;
- human-report verbosity and machine-output preferences;
- notification and scheduling preferences;
- the evidence source for each detected fact or user choice; and
- creation, confirmation, and last-verification timestamps.

Provider credentials, tokens, cookies, raw auth responses, private model output, and environment-file contents never belong in this profile. Adapters should reference the provider's supported authentication mechanism rather than persist credentials themselves.

The tool should be able to emit a bounded, redacted operational-context document for agents and reviewers. This prevents repeated rediscovery while keeping secrets and unrelated machine state out of model context. Profiles must be schema-versioned, portable where practical, inspectable by the user, and invalidated or rechecked when the environment changes.

### Preference learning without uncontrolled self-modification

Future feedback handling may propose profile updates when a user rejects an action, changes a merge preference, or requests different reporting. The durable record should retain the observed outcome, proposed preference, confirmation authority, and effective date.

Feedback must not silently broaden repository scope, increase spending, enable mutation, weaken deterministic gates, or rewrite policy. A user or explicitly authorized agent confirms material profile changes. Repeated decisions should become deterministic configuration where possible rather than permanent model memory.

### Diagnostics and observability

Agents need actionable failure evidence, but raw provider stdout/stderr may contain credentials or private repository text. Keep normal reports structured and sanitized: adapter, stage, exit category/code, retryability, timestamp, and a correlation identifier where available.

A later opt-in local diagnostic mode may retain more detail only with explicit disclosure, bounded retention, redaction, and a private path. Raw diagnostics must never be included automatically in notifications, public artifacts, or ordinary agent context.

Human reporting should eventually offer concise, standard, and detailed levels over the same underlying structured record. Changing verbosity must not change policy or action authority.

## Later: gated execution and autonomy

Mutation authority is separate from provider selection, analysis spend, and report verbosity. Potential modes are:

- inspect/advice only;
- approval-gated actions; and
- fully automatic actions inside explicit deterministic policy.

Recommended setup must remain non-mutating until an execution phase is implemented and validated. Later mutation requires explicit opt-in, exact repository/PR/head binding, current live-state rechecks, repository merge-policy enforcement, bounded action counts, audit records, and recovery/downstream-impact guidance. A model may recommend or veto; it does not independently authorize an action or expand its target.

The long-term fully automated mode should produce concise action summaries—what merged, closed, deferred, or failed and why—while retaining detailed machine-readable evidence for agents and audits.

## Later platform and product gates

- WSL2 and ordinary Linux validation first.
- macOS and native Windows CI plus real smoke tests before support claims.
- Optional scheduler and notification adapters.
- Read-only Dependabot readiness/preflight validation before any automatic configuration feature.
- Additional providers based on demand and testability.
- A high-coverage/high-spend analysis mode after evaluating whether triage still adds value or direct review is clearer.
- Bounded commit, patch, and PR-body-staleness evidence when the API-first inspection record is insufficient; these signals should be collected or derived by the core rather than accepted as model-owned facts.
- Repeated private dogfooding through the same installation and setup path intended for users.
- Security, privacy, history, license, contribution, support, and release gates before public visibility.
