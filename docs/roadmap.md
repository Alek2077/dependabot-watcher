# Product roadmap

Dependabot Watcher is being restored in bounded phases. This roadmap records product boundaries and future design constraints; it is not a promise that every listed capability ships in the first public release.

Historical phase requirements remain as design records. Record verified delivery status separately rather than rewriting prior requirements as completed behavior.

The current read-only position remains “a local-first, provider-neutral control plane for inspecting and safely triaging Dependabot activity across multiple repositories.” The intended mature position is safe automation of the Dependabot maintenance loop, not full repository maintenance: collect evidence, recommend an order, recheck live state, execute only policy-authorized actions, and retain a concise human summary plus complete structured audit evidence.

## Phase entry gate

Before committing to a new phase or material feature, briefly revalidate its premise against the current product, user evidence, dependencies, safety boundary, and relevant external ecosystem. Ask whether the work is still needed, whether its intended outcome remains justified, and whether the planned approach is still the best proportionate option.

This is a high-level decision gate, not an invitation to repeatedly reopen settled requirements or research every implementation detail. Use targeted local inspection or current primary-source research only when a material assumption is uncertain or may have changed. Record any material scope or direction change and its rationale before implementation.

## Phase map

1. **Deterministic safety restoration — complete.** Freeze unsafe private execution, restore a genuinely non-mutating inspection boundary, bind candidate identity to collector-owned evidence, and correct pagination, correlation, and freshness defects.
2. **Standalone multi-user core — complete.** Extract the generic read-only product into its own repository with typed configuration, packaging, deterministic tests, structured output, and Ubuntu CI.
3. **Provider-neutral advisory layer — complete.** Add model-off, economy, and reviewed modes behind a stable advisory contract, with one tested provider adapter and model output kept non-authoritative.
4. **Ordered planning and evidence graph — complete.** Add complete-queue ordering, PR-alert relationship levels, bounded manifest evidence, workflow profiles, conflicts, validation gates, and freshness binding without mutation.
5. **Setup, discovery, and operational profile — complete.** Add safe setup, `doctor`, a versioned non-secret profile, ecosystem-readiness reporting, and selectable human verbosity.
6. **Gated execution and autonomy — next.** Execute only fresh plans with exact target/head binding, live rechecks, repository-policy enforcement, bounded actions, idempotency, audit history, and recovery guidance. Introduce approval-gated action before a narrowly defined fully automatic policy class.
7. **Operational deployment, terminal experience, and platform adapters.** Prove repeated WSL2 and Ubuntu operation, retain scheduler-independent run-once behavior, add a restrained interactive spinner/status hierarchy, then add optional scheduling and sparse action-oriented notifications. Validate macOS and native Windows before claiming support.
8. **Private dogfood and publication gate.** Require repeated installed-path reliability, an explicitly approved exact-head-bound mutation and recovery exercise, security/privacy/history review, license and public documentation, synthetic examples, contribution/support expectations, release packaging, functioning CI, and final canonical-source migration before changing visibility. After public visibility, repeat anonymous multi-agent onboarding from the public surface before release promotion.

The preliminary private safety freeze was treated as the opening step of Phase 1 rather than a separate product phase. Cross-cutting tests, proportional independent review, documentation, live read-only checks, and founder checkpoints apply throughout rather than being deferred to Phase 8.

## Delivered foundation

- Side-effect-free, model-free inspection across configured repositories.
- Deterministic PR, alert, check, path-risk, and recent-sensitive-change assessment.
- Text, JSON, and Markdown stdout with explicit partial-state reporting.
- Optional provider-neutral advice with `off`, `economy`, and `reviewed` modes.
- One isolated Codex CLI adapter with explicit model/reasoning configuration.
- Collector-owned candidate identity and advisory-only model output.

## Phase 4 design contract and delivery status

The following requirements recorded the Phase 4 design contract before implementation:

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

The Phase 4 implementation added the non-mutating `plan` command, complete-queue deterministic ordering, base/head/alert freshness binding, optional repository workflow profiles, and bounded Python requirements-inclusion evidence. It deliberately leaves plan execution, persistent decision history, general dependency-graph resolution, and mutation for later phases. Current-tree live dogfood and independent review completed before merge.

### Cross-ecosystem support contract

Do not equate the Python implementation language or the first requirements-file adapter with a Python-only product. The generic core should consume GitHub's normalized Dependabot PR, alert, check, changed-path, and repository-state evidence for every supported ecosystem. Deeper ecosystem adapters should be added only where deterministic parsing materially improves relationships, validation, or execution safety.

Maintain an explicit support matrix with separate levels for:

- generic inspection and planning;
- deeper ecosystem-specific relationship analysis; and
- validated eligibility for later automatic execution.

Unknown ecosystems or incomplete evidence remain visible and conservative; they do not become safe merely because a model can interpret them. Models may investigate unfamiliar evidence and explain likely relationships, while deterministic policy determines whether an action is authorized. Prioritize deeper adapters using real dogfood and user demand rather than attempting every package manager before release.

### Context and provider efficiency

Use deterministic collection and prioritization before model calls. Send sufficient, decision-relevant, well-structured evidence; group related PRs and alerts; batch when doing so preserves clarity; and escalate to a stronger or second-stage model only when repository risk or ambiguity justifies it. Efficiency is a design requirement, not a reason to omit material context or a promise that every repository or update can be understood from a small fixed context.

Context collection should be progressive rather than artificially minimal. A provider must preserve uncertainty and identify missing evidence. Later investigation capabilities may gather additional bounded diffs, dependency relationships, release information, or disposable-checkout results before another decision. Portable provider instructions should enforce evidence over assumption, distinguish observation from inference, and prohibit filling evidence gaps with confidence or plausible-sounding guesses.

Provider neutrality includes multiple transport types. An adapter may use an officially supported subscription-authenticated local CLI, a direct provider API, an aggregation service, or a compatible endpoint. Setup must state which transport is active, whether it can incur separate cost, and what authentication mechanism the provider officially supports. The product must not read secret files, silently enable spending, or imply that subscription authentication exists where the provider does not offer it.

## Phase 5 design contract and delivery status

The following requirements recorded the Phase 5 design contract for reducing setup friction without weakening consent, privacy, or deterministic safety.

Phase 5 was not complete until `doctor`, exclusive safe setup, versioned operational metadata in the existing configuration source of truth, and presentation-only human verbosity passed deterministic validation, installed-path dogfood, a live read-only preflight, and independent review.

### Read-only environment discovery

Add a `doctor` or equivalent preflight command that can report, without changing the machine:

- operating system and supported runtime details;
- installed provider CLIs, versions, and supported adapter capabilities;
- authentication readiness as `available`, `unavailable`, or `unknown`, never credential values;
- GitHub CLI availability and access to configured repositories;
- detected Dependabot ecosystems and the watcher's support level for each;
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

The Phase 5 implementation added `setup`, `doctor`, the versioned non-secret operational profile, presentation-only verbosity, fail-visible Dependabot readiness checks, and conservative first-use documentation. Deterministic validation, installed-path dogfood, live read-only preflight, and independent review completed before merge. It deliberately leaves scheduling, notifications, persistent operational history, and mutation for later phases.

The current `setup` command creates a conservative profile from explicit repository and provider choices. Online setup verifies GitHub access and the selected repository/branch; `--offline` creates an explicitly unverified profile for later `doctor` validation. Setup does not enumerate arbitrary installed agent tools or silently choose a provider, model, spend level, or autonomy level. A later guided setup may use bounded `doctor` evidence and supported-adapter metadata to recommend those choices, while retaining non-interactive flags for agents. It must not inspect secret stores, infer credentials from unrelated applications, or infer consent to model spend or GitHub mutation.

## Phase 6 design contract: gated execution and autonomy

Mutation authority is separate from provider selection, analysis spend, and report verbosity. Potential modes are:

- inspect/advice only;
- approval-gated actions; and
- fully automatic actions inside explicit deterministic policy.

Recommended setup must remain non-mutating until an execution phase is implemented and validated. Later mutation requires explicit opt-in, exact repository/PR/head binding, current live-state rechecks, repository merge-policy enforcement, bounded action counts, audit records, and recovery/downstream-impact guidance. A model may recommend or veto; it does not independently authorize an action or expand its target.

A deterministic plan alone is never sufficient evidence for mutation. Phase 6 should classify execution policy by risk and evidence. Model-free execution may eventually be allowed only for an explicit, narrowly bounded, dogfooded low-risk policy class with complete deterministic evidence and required repository validation. Ambiguous, sensitive, major, ecosystem-unsupported, or otherwise higher-risk actions should require configured model review and/or human approval. If a policy requires model review and the provider is unavailable, incomplete, or over budget, execution must fail closed rather than silently fall back to model-free mutation. Passing model review does not replace tests, required checks, deployment gates, or freshness reinspection.

The long-term fully automated mode should produce concise action summaries—what merged, closed, deferred, or failed and why—while retaining detailed machine-readable evidence for agents and audits.

Phase 6 starts with one intentionally narrow mutation surface: approval-gated merging of one Dependabot pull request already present as a `validate_then_merge` step in a saved JSON plan. A read-only execution preflight must recollect current evidence and produce an approval token bound to the plan, step, repository, pull-request number, expected base, head SHA, and explicit merge method. The mutating invocation must present that exact token, repeat the live recheck, and submit the expected head SHA to GitHub so a changed pull request fails closed.

Execution additionally requires explicit profile opt-in, complete collection, a deterministic baseline-candidate assessment, configured required checks observed as passing, and an explicit repository merge method. `repository_default`, missing checks, workflow validation that the tool cannot verify, advisory confidence, and a plan action by themselves are not execution authority. The first implementation performs at most one action per invocation.

Every attempt must produce permission-restricted append-only audit events with a stable action identity, sanitized outcome or failure evidence, and recovery guidance. Concurrent execution against one state directory must fail closed. A retry must reconcile live pull-request state and prior success evidence instead of blindly repeating the mutation.

Automatic execution remains unavailable until the approval-gated path completes an explicitly authorized real-world action and recovery exercise. That checkpoint must reassess the evidence and define a narrowly dogfooded low-risk class before any no-prompt mutation is implemented; it is not unlocked merely because the execution command exists.

## Later platform and product gates

- WSL2 and ordinary Linux validation first.
- During Phase 7, run the bounded presentation/usability benchmark in the [installed-path validation workflow](validation-workflow.md): an assigned agent should compare a small current sample of relevant popular open-source CLIs, use disposable installations where direct interaction is necessary, capture sanitized screenshots when useful, and record patterns to adopt, adapt, defer, or reject. Use that evidence to replace the current static transient progress line with a restrained animated spinner and clearer terminal status hierarchy for interactive human runs. Preserve a plain fallback, respect `NO_COLOR` and accessibility needs, and suppress animation and styling for JSON, Markdown, pipes, redirected output, logs, and non-interactive agent execution.
- macOS and native Windows CI plus real smoke tests before support claims.
- Publish an ordinary Python package and make one package-index command the primary cross-platform installation path. Keep source-checkout installation for development and private prereleases.
- Before public adoption, publish a license, versioned release/tag, and reproducible release provenance; document how users pin an approved version or commit rather than trusting a moving branch. Evaluate release-build integrity and dependency policy explicitly, but do not impose an application-style transitive lockfile on the distributable library without a concrete packaging reason.
- Treat Homebrew, signed release archives, and maintained bootstrap installers as later distribution channels that require versioned releases and platform validation; they do not require rewriting the Python application.
- Document and validate a least-privilege GitHub credential profile for the exact repository, pull-request, check, content, and Dependabot-alert reads used by the product.
- Before publication, document installation, upgrade, uninstall, configuration reset, and rollback paths. While the repository remains private, make the documented clone command compatible with the stated GitHub CLI authentication prerequisite, or explicitly explain the required Git credential setup.
- Preserve GitHub CLI as the first supported GitHub adapter while keeping the core transport boundary open to a direct fine-grained token, OAuth/device flow, or GitHub App when user evidence justifies the additional surface.
- Do not infer or extract credentials from VS Code or another IDE. An IDE is not required, and its private authentication state is not a supported cross-application credential interface.
- Add only truthful README badges and product imagery after the corresponding license, release, platform, and hosted-CI claims are established.
- Optional scheduler and notification adapters.
- Read-only Dependabot readiness/preflight validation before any automatic configuration feature.
- Additional providers based on demand and testability.
- A high-coverage/high-spend analysis mode after evaluating whether triage still adds value or direct review is clearer.
- Bounded commit, patch, and PR-body-staleness evidence when the API-first inspection record is insufficient; these signals should be collected or derived by the core rather than accepted as model-owned facts.
- Repeated private dogfooding through the same installation and setup path intended for users.
- Apply the [multi-agent onboarding checkpoints](validation-workflow.md#multi-agent-onboarding-checkpoints) after Phase 6 or another material onboarding change, and repeat anonymously immediately after public visibility before release promotion. Keep private authenticated results distinct from genuine anonymous adoption evidence.
- Make optional-model disclosure prominent before opt-in: identify the categories of private repository evidence transmitted, explain that candidate/call bounds are workload controls rather than financial limits, and state where provider authentication, subscription, or API cost is determined.
- Apply the [installed-path validation workflow](validation-workflow.md) proportionately after material features or onboarding changes so human, agent, machine-output, safety, and cleanup behavior are exercised rather than inferred from unit tests alone.
- After Phase 6 proves the first gated-action exercise, create one concise, sanitized, self-contained `docs/validation.md` before publication. Record reproducible commands and results, live dogfood boundaries, independently validated AI-review findings and fix-check closure, known limitations, and links to public PRs, commits, and CI. Preserve detailed private Lab records as source evidence; do not copy private paths, target identities, internal agent handles, quota anecdotes, or model scorecards into the public repository.
- Require functioning hosted CI on the release commit rather than treating a configured workflow or local test result as hosted-CI evidence. Until GitHub resolves the current startup/billing restriction, describe local and independent validation accurately without implying that the matrix executed remotely.
- Security, privacy, history, license, contribution, support, and release gates before public visibility.
