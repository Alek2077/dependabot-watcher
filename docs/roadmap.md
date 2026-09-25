# Product roadmap

Dependabot Watcher is being restored in bounded phases. This roadmap records product boundaries and future design constraints; it is not a promise that every listed capability ships in the first public release.

Historical phase requirements remain as design records. Record verified delivery status separately rather than rewriting prior requirements as completed behavior.

The original read-only position was “a local-first, provider-neutral control plane for inspecting and safely triaging Dependabot activity across multiple repositories.” The intended mature position is safe automation of the Dependabot maintenance loop, not full repository maintenance: collect evidence, recommend an order, recheck live state, execute only policy-authorized actions, and retain a concise human summary plus complete structured audit evidence.

## Phase entry gate

Before committing to a new phase or material feature, briefly revalidate its premise against the current product, user evidence, dependencies, safety boundary, and relevant external ecosystem. Ask whether the work is still needed, whether its intended outcome remains justified, and whether the planned approach is still the best proportionate option.

This is a high-level decision gate, not an invitation to repeatedly reopen settled requirements or research every implementation detail. Use targeted local inspection or current primary-source research only when a material assumption is uncertain or may have changed. Record any material scope or direction change and its rationale before implementation.

## Phase map

1. **Deterministic safety restoration — complete.** Freeze unsafe private execution, restore a genuinely non-mutating inspection boundary, bind candidate identity to collector-owned evidence, and correct pagination, correlation, and freshness defects.
2. **Standalone multi-user core — complete.** Extract the generic read-only product into its own repository with typed configuration, packaging, deterministic tests, structured output, and Ubuntu CI.
3. **Provider-neutral advisory layer — complete.** Add model-off, economy, and reviewed modes behind a stable advisory contract, with one tested provider adapter and model output kept non-authoritative.
4. **Ordered planning and evidence graph — complete.** Add complete-queue ordering, PR-alert relationship levels, bounded manifest evidence, workflow profiles, conflicts, validation gates, and freshness binding without mutation.
5. **Setup, discovery, and operational profile — complete.** Add safe setup, `doctor`, a versioned non-secret profile, ecosystem-readiness reporting, and selectable human verbosity.
6. **Gated execution and autonomy — complete for approval-gated execution.** Execute only fresh plans with exact target/head binding, live rechecks, repository-policy enforcement, bounded actions, idempotency, audit history, and recovery guidance. A narrowly defined fully automatic policy class remains deferred until repeated private dogfooding justifies it.
7. **Operational UX and run-once reliability — complete for the WSL2 Ubuntu installed path.** Retain a scheduler-independent run-once core, benchmark relevant modern CLIs, and add restrained interactive progress, clearer status hierarchy, accessible fallbacks, and complete post-run summaries without contaminating machine or non-interactive output. Ordinary non-WSL hosted Ubuntu evidence remains part of the publication/CI gate rather than an implied claim.
8. **Agent ecosystem integration and portability.** Evaluate whether a thin project-owned skill/plugin materially improves the stable CLI and structured contracts; fully validate Codex CLI and Codex in the ChatGPT desktop app as operator surfaces; validate Claude Code CLI and Claude Desktop as a second operator family; smoke-test the corresponding IDE extensions without duplicating product logic; and publish an exact support matrix that distinguishes operator, packaged-integration, and advisory-provider support.
9. **Scheduled closed-loop operation.** Add optional scheduling adapters, sparse action-oriented notifications, post-action monitoring, automatic stopping, and actionable recovery guidance. Reassess a narrowly bounded unattended policy class only after repeated dogfooding demonstrates reliable eligibility, downstream validation, reporting, and recovery.
10. **Private dogfood and publication gate.** Require repeated installed-path reliability, an explicitly approved exact-head-bound mutation and recovery exercise, security/privacy/history review, license and public documentation, synthetic examples, contribution/support expectations, release packaging, functioning CI, and final canonical-source migration before changing visibility. After public visibility, repeat anonymous multi-agent onboarding from the public surface before release promotion.

The preliminary private safety freeze was treated as the opening step of Phase 1 rather than a separate product phase. Cross-cutting tests, proportional independent review, documentation, live read-only checks, and founder checkpoints apply throughout rather than being deferred to Phase 10.

### Phase 6 delivery status

Phase 6 delivered one approval-gated `validate_then_merge` action with explicit profile opt-in, saved-plan integrity checks, an exact repository/PR/base/head/merge-method approval token, a final model-free live reinspection, required-check and workflow-policy enforcement, a single exact-head GitHub merge, private append-only audit events, and idempotent live-state reconciliation.

The installed product completed one explicitly approved real merge from a fresh remote clone. Independent GitHub verification confirmed the exact planned head was merged; audit history recorded start, success, and an idempotent retry reconciliation; a repeated invocation made no second mutation; and post-action inspection observed the reduced open-PR queue. The sanitized validation record is in [validation.md](validation.md).

This evidence completes the approval-gated Phase 6 milestone. It does not justify unattended execution yet. Reassess a narrowly bounded automatic class only after repeated installed-path dogfooding shows that eligibility, downstream validation, reporting, and recovery remain reliable across more than one repository/update shape.

A post-delivery multi-model onboarding checkpoint exposed two safety clarifications that the approval-gated contract must preserve: execution-required checks need explicit GitHub success rather than the broader inspection treatment of neutral/skipped conclusions, and the deterministic approval token is an exact-action confirmation value rather than independent human authentication. It also prompted bounded Dependabot-alert permission diagnostics and clearer private-plan, clone/authentication, and one-action freshness guidance. These corrections refine the delivered boundary without changing the historical Phase 6 milestone.

### Closed-loop automation and recovery direction

The mature goal is exception-driven closed-loop automation for routine Dependabot maintenance, not a claim that every dependency problem can be resolved autonomously. Once a repository policy is deliberately configured and dogfooded, the ordinary loop should be able to trigger on a schedule or event, collect fresh evidence, process only a validated low-risk class one action at a time, observe downstream checks and supported deployment gates, replan after every base change, retain an audit history, and report what changed. Major, grouped, sensitive, ambiguous, unsupported, or failed work should halt the affected queue and escalate with sufficient evidence rather than being forced through.

The product north star is that, after deliberate setup and authorization, routine Dependabot maintenance becomes quiet background infrastructure: the user no longer has to remember to inspect alerts, launch each run, interpret routine updates, or approve already-dogfooded low-risk work. Healthy runs should consume little compute and attention and remain silent or appear only in a configurable digest. Exceptions should stop safely and surface one concise, action-oriented report with the evidence and recovery path. “Invisible” must mean unobtrusive, not opaque: every mutation remains attributable, inspectable, and recoverable, and initial authorization, material policy expansion, private-evidence transmission, and spend never become hidden friction.

Recovery has distinct layers. Interrupted or ambiguous execution already uses audit, live reconciliation, idempotent retry, and deliberate lock handling. Later automation should add post-merge verification, automatic stopping, and actionable recovery guidance. An approval-gated revert or other bounded recovery action may be justified after real workflow evidence; automatic rollback must not be presumed correct merely because a downstream check failed.

Prefer a scheduler-independent run-once core with optional platform scheduling adapters before introducing an internal daemon. Poll the authoritative GitHub API initially; a GitHub App or webhook adapter may later supply event-driven triggers when demand justifies the added deployment and authorization surface. Email may be a notification destination, not the primary evidence source. Retain the `Dependabot Watcher` name for now and revisit it before public release only if repeated onboarding evidence shows that users materially misread the product as passive despite clear execution messaging; do not add a long-running watch mode merely to justify the name.

### Agent-first integration and AI direction

Dependabot Watcher should remain usable as a standalone CLI with stable structured output, explicit configuration, and deterministic authority even as agent-mediated operation becomes the dominant onboarding and operating path. Product logic must live in the CLI and provider-neutral contracts rather than being copied into each desktop app, IDE extension, or agent harness. Surface integrations should therefore be thin, replaceable packages that teach an agent how to install, configure, inspect, plan, review, execute, and recover through the same public commands a human can inspect.

Treat “agent support” as three separate claims:

1. **Operator compatibility:** an agent can invoke the standalone CLI and interpret its human or structured output.
2. **Packaged integration:** a project-owned portable skill/plugin supplies current workflows, safety boundaries, and command guidance for a supported agent family.
3. **Advisory-provider support:** the watcher itself can invoke that provider through a tested adapter for optional model analysis.

A project-owned Dependabot Watcher skill/plugin is a Phase 8 evidence question, not a foregone requirement. Existing third-party Dependabot skills and workflows cover configuration, PR review, repository-specific alert diagnosis, and mutation-heavy sweeps; the current comparison and decision gate are recorded in [agent-integration-strategy.md](agent-integration-strategy.md). If repository documentation plus a concise agent runbook already produces equally reliable operation, do not add packaging for its own sake. If an integration materially improves discovery, repeated operation, or safety, the Watcher project should own the thin package that describes its current commands and authority contract. Treat catalog presence as a plausible acquisition path to test, not as proof of adoption or a substitute for a useful product.

The first public support target is deliberately narrow:

- fully validate Codex CLI as both an operator and the initial advisory provider;
- fully validate Codex in the ChatGPT desktop app as an operator through the same portable integration, with a compatibility smoke test for the Codex IDE extension rather than a separate product implementation;
- validate Claude Code CLI and Claude Desktop as operator surfaces through portable instructions or a Claude-compatible plugin, with a compatibility smoke test for the Claude Code IDE extension; and
- defer first-class GitHub Copilot, OpenCode, Antigravity, and Hermes packaging until user evidence justifies it, while preserving generic shell/structured-output compatibility and recording truthful unverified or experimental status.

A Claude advisory adapter is not a first-public-release prerequisite. Claude users should still be able to operate the deterministic product through Claude Code, while the support matrix clearly states that in-product model advice initially uses the Codex CLI adapter. Additional advisory providers should be added by demand, official automation support, privacy/cost clarity, and the ability to test them without weakening the stable contract.

“AI is optional” describes the trust boundary, not a recommendation to avoid models. Model-free collection, planning, and authorization make the product inspectable, economical, and able to fail safely. Guided setup and public documentation should recommend model assistance when a supported provider is available, explain what additional judgment it supplies, and obtain explicit consent before transmitting private evidence or consuming quota. Model review should be recommended—and may be required by configured policy—for ambiguous, grouped, transitive, major, sensitive, ecosystem-unsupported, or otherwise higher-risk work. Deterministic evidence and repository checks remain the authority boundary because a model is neither a permission system nor proof of compatibility. Public wording may evolve with user evidence, but it must distinguish zero-model operation, recommended model review, provider support, repository validation, and execution authority without implying that deterministic ordering alone understands every dependency interaction.

### Future phase delivery boundaries

Phase 7 should end with a materially improved installed terminal experience worth a founder checkpoint: current CLI comparison evidence, restrained interactive progress, understandable status hierarchy, plain and accessible fallbacks, strong completion summaries, and repeated fresh-install run-once reliability on WSL2 and Ubuntu. It does not add scheduling or provider-specific product logic.

Phase 8 should end with an evidence-backed integration decision, an explicit support matrix, verified Codex and Claude operator workflows, and documented differences between CLI, desktop, and IDE surfaces. If a thin project-owned skill/plugin outperforms documentation alone, ship and validate it; otherwise retain the simpler agent runbook. Founder testing should focus on the Codex CLI and ChatGPT desktop paths available locally; agent-owned validation covers the wider matrix, with additional manual checks only where a surface-specific behavior cannot be established otherwise.

Phase 9 should close the recurring operational loop with optional scheduling, quiet-on-success notifications, post-action observation, stop/escalate behavior, and recovery guidance. Scheduling must call the same run-once core rather than create a second execution system. A scheduled reconciler should deterministically detect no-work and already-processed revisions before invoking a model, notifying, or mutating; stable repository/PR/head/evidence identities should suppress duplicate analysis and output. Skills may guide an agent launched by that reconciler but do not provide scheduling themselves. No-prompt mutation remains unavailable unless the Phase 6 safety contract and repeated Phase 7-9 dogfood justify a separately documented low-risk class.

Any unattended policy class must name the ecosystem evidence it depends on. Do not claim production/runtime reachability merely because a package appears in a lockfile or a model finds a plausible path. Add adapters incrementally from actual dogfood demand. A local toolchain such as Go, Node, or a package manager may be used only through an explicit isolated adapter with bounded network/execution behavior and fail-visible absence; Watcher must not assume every user's machine has every ecosystem tool installed. Disposable containers, repository CI, or a future remote integration may provide validation where installing toolchains locally is inappropriate.

Phase 10 remains the publication gate. Agent-surface breadth must not substitute for a license, functioning hosted CI, release provenance, least-privilege GitHub guidance, supported-runtime decisions, packaging, security/privacy/history review, or genuine anonymous onboarding from the public surface.

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

Add bounded ecosystem evidence where it materially improves planning. This includes manifest inclusion/inheritance relationships, resolved dependency versions, lockfile effects, grouped-update membership, production/runtime reachability, and relevant upstream source or release changes. Collect or derive relationship and reachability facts through explicit adapters, build/package-manager evidence, or repository APIs; do not ask a model to guess them from filenames. The planner may still request a bounded checkout-based investigation when API evidence cannot answer the question, but that must be an explicit capability with a disposable workspace and audit trail.

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

The existing `reviewed` mode already supplies bounded triage followed by a second-stage review for selected ambiguous or higher-risk candidates. Before adding a costly multi-agent or multi-provider review mode, test whether separating security, compatibility/code-quality, and repository-impact lenses over one normalized evidence packet improves decisions beyond the current reviewer pass. Preserve meaningful disagreement for synthesis, but do not run a permanent external-reviewer pair for every routine update merely because that pattern is useful for high-risk product development.

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

### Phase 7 delivery status

Phase 7 added a dependency-free terminal presentation boundary over the unchanged run-once core. Interactive text runs use a restrained stderr spinner and light final-report hierarchy; `--plain`, `TERM=dumb`, and `CI` use persistent line-oriented status; `NO_COLOR` removes color; unsupported Unicode falls back to ASCII; and JSON, Markdown, pipes, redirects, and non-interactive agents receive no animation or decoration. Human renderers remove terminal control sequences and flatten injected line breaks while versioned JSON preserves original structured evidence.

Concise inspection now includes bounded PR and uncovered-alert detail, error/truncation counts, and an actionable next step. Doctor labels Codex as an optional advice provider. Installation guidance now distinguishes source and target authorization and documents update, rollback, profile replacement, uninstall, and audit-retention boundaries.

The bounded comparison used current GitHub CLI, uv, Ruff, and Codex CLI evidence to adopt explicit lifecycle/authentication guidance and clean stdout/stderr controls while rejecting a full-screen TUI. Deterministic tests, static analysis, isolated wheel installation, piped JSON parsing, Markdown/plain output, and real PTY behavior passed on Ubuntu 22.04 under WSL2. Independent review findings on concise Markdown, freshness sanitization, strict-encoding fallback, `NO_COLOR`, and output documentation were corrected and closed. Hosted ordinary-Linux CI still has not run because of the documented external GitHub account restriction; Phase 7 does not convert that missing evidence into a support claim.

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
