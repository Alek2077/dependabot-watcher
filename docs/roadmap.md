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
8. **Agent ecosystem integration and portability — in progress.** Evaluate whether a thin project-owned skill/plugin materially improves the stable CLI and structured contracts; validate Codex CLI and Codex in the ChatGPT desktop app as the first operator family; smoke-test standalone-skill/operator compatibility in the Codex IDE extension without duplicating product logic; retain portable Claude-compatible packaging as explicitly experimental; and publish an exact support matrix that distinguishes operator, packaged-integration, and advisory-provider support.
9. **Scheduled closed-loop foundation.** Add optional scheduling adapters, revision deduplication, sparse action-oriented notifications, live Codex advisory validation, post-action observation, automatic escalation routing, and actionable recovery guidance while preserving approval-gated mutation.
10. **Private dogfood and publication gate.** Require repeated installed-path reliability, an explicitly approved exact-head-bound mutation and recovery exercise, security/privacy/history review, license and public documentation, synthetic examples, contribution/support expectations, release packaging, functioning CI, and final canonical-source migration before changing visibility. After public visibility, repeat anonymous multi-agent onboarding from the public surface before release promotion.
11. **Tool-capable investigation and resumable orchestration.** Add explicit read-only investigator and sandboxed-validator capability profiles, private resumable job journals and scratch workspaces, bounded parallel workers, synthesis, and progressively richer context without weakening the stable evidence or action contracts.
12. **Expanded autonomy and provider ecosystem.** Reassess bounded unattended policy from dogfood evidence, add high-assurance multi-role review where it proves useful, deepen selected ecosystem adapters, and expand providers and agent surfaces according to demand and testability.

The preliminary private safety freeze was treated as the opening step of Phase 1 rather than a separate product phase. Cross-cutting tests, proportional independent review, documentation, live read-only checks, and founder checkpoints apply throughout rather than being deferred to Phase 10.

### Phase 6 delivery status

Phase 6 delivered one approval-gated `validate_then_merge` action with explicit profile opt-in, saved-plan integrity checks, an exact repository/PR/base/head/merge-method approval token, a final model-free live reinspection, required-check and workflow-policy enforcement, a single exact-head GitHub merge, private append-only audit events, and idempotent live-state reconciliation.

The installed product completed one explicitly approved real merge from a fresh remote clone. Independent GitHub verification confirmed the exact planned head was merged; audit history recorded start, success, and an idempotent retry reconciliation; a repeated invocation made no second mutation; and post-action inspection observed the reduced open-PR queue. The sanitized validation record is in [validation.md](validation.md).

This evidence completes the approval-gated Phase 6 milestone. It does not justify unattended execution yet. Reassess a narrowly bounded automatic class only after repeated installed-path dogfooding shows that eligibility, downstream validation, reporting, and recovery remain reliable across more than one repository/update shape.

A post-delivery multi-model onboarding checkpoint exposed two safety clarifications that the approval-gated contract must preserve: execution-required checks need explicit GitHub success rather than the broader inspection treatment of neutral/skipped conclusions, and the deterministic approval token is an exact-action confirmation value rather than independent human authentication. It also prompted bounded Dependabot-alert permission diagnostics and clearer private-plan, clone/authentication, and one-action freshness guidance. These corrections refine the delivered boundary without changing the historical Phase 6 milestone.

### Closed-loop automation and recovery direction

The mature goal is exception-driven closed-loop automation for routine Dependabot maintenance, not a claim that every dependency problem can be resolved autonomously. Once a repository policy is deliberately configured and dogfooded, the ordinary loop should be able to trigger on a schedule or event, collect fresh evidence, process only a validated low-risk class one action at a time, observe downstream checks and supported deployment gates, replan after every base change, retain an audit history, and report what changed. Major, grouped, sensitive, ambiguous, unsupported, or failed work should halt the affected queue and escalate with sufficient evidence rather than being forced through.

```text
deterministic collection and guardrails
                -> AI review and investigation
                -> policy-authorized action
                -> post-action observation and replanning
                -> quiet success or actionable escalation
```

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
- fully validate Codex in the ChatGPT desktop app as an operator through the same portable integration, with a standalone-skill/operator smoke test for the Codex IDE extension rather than a plugin claim or separate product implementation;
- retain portable Claude-compatible packaging as experimental until official runtime behavior can be validated, without making Claude support a first-public-release gate; and
- defer first-class Claude, GitHub Copilot, OpenCode, Antigravity, and Hermes support claims until user evidence and installed-path validation justify them, while preserving generic shell/structured-output compatibility and recording truthful unverified or experimental status.

A Claude advisory adapter or verified Claude operator path is not a first-public-release prerequisite. The portable package may remain available for experimental source installation, while the support matrix clearly states that first-class operation and in-product model advice initially target Codex. Additional providers should be added by demand, official automation support, privacy/cost clarity, and the ability to test them without weakening the stable contract.

“AI is optional” describes the trust boundary, not a recommendation to avoid models. Model-free collection, planning, and authorization make the product inspectable, economical, and able to fail safely. Guided setup and public documentation should recommend model assistance when a supported provider is available, explain what additional judgment it supplies, and obtain explicit consent before transmitting private evidence or consuming quota. Model review should be recommended—and may be required by configured policy—for ambiguous, grouped, transitive, major, sensitive, ecosystem-unsupported, or otherwise higher-risk work. Deterministic evidence and repository checks remain the authority boundary because a model is neither a permission system nor proof of compatibility. Public wording may evolve with user evidence, but it must distinguish zero-model operation, recommended model review, provider support, repository validation, and execution authority without implying that deterministic ordering alone understands every dependency interaction.

### Future phase delivery boundaries

Phase 7 should end with a materially improved installed terminal experience worth a founder checkpoint: current CLI comparison evidence, restrained interactive progress, understandable status hierarchy, plain and accessible fallbacks, strong completion summaries, and repeated fresh-install run-once reliability on WSL2 and Ubuntu. It does not add scheduling or provider-specific product logic.

Phase 8 should end with an evidence-backed integration decision, an explicit support matrix, verified Codex operator workflows, portable package validation, and documented differences between CLI, desktop, and IDE surfaces. Current OpenAI surfaces support plugins in ChatGPT/Codex and Codex CLI but not the Codex IDE extension; the IDE extension may consume a standalone skill and operate the same CLI. If a thin project-owned skill/plugin outperforms documentation alone, ship and validate it; otherwise retain the simpler agent runbook. Claude packaging may be retained with an experimental/unverified label, but official Claude runtime validation no longer blocks the Codex-first public release.

Phase 9 should close the recurring operational loop with optional scheduling, quiet-on-success notifications, post-action observation, stop/escalate behavior, and recovery guidance. Scheduling must call the same run-once core rather than create a second execution system. A scheduled reconciler should deterministically detect no-work and already-processed revisions before invoking a model, notifying, or mutating; stable repository/PR/head/evidence identities should suppress duplicate analysis and output. Skills may guide an agent launched by that reconciler but do not provide scheduling themselves. No-prompt mutation remains unavailable unless the Phase 6 safety contract and repeated Phase 7-9 dogfood justify a separately documented low-risk class.

Deliver Phase 9 in evidence-gated slices rather than coupling every autonomy concern into the first scheduler:

1. **Scheduled observation:** run the existing deterministic core on a cadence, deduplicate unchanged revisions, retain bounded run state, and notify only for actionable exceptions or an optional digest. No unattended mutation.
2. **AI-supervised operation:** live-validate the existing Codex `economy` and `reviewed` modes, establish the versioned job/capability/budget contracts needed by later stronger investigation, and route unresolved work to an actionable report rather than asking the user to interpret raw technical evidence.
3. **Publication-ready operation:** dogfood the scheduled read/advice/plan path, approval-gated exact action, post-action observation, failure routing, and notifications without making deep tool-capable investigation or unattended mutation a condition for the first release.

The Codex-first public release target should support unattended scheduled wake-up, silent no-work handling, complete deterministic collection and planning, configurable AI supervision, sparse actionable notifications or digests, and the existing exact-action approval flow. Guided setup should recommend a tested Codex model/reasoning/budget profile while allowing the user to choose supported Codex model identifiers and reasoning levels. General unattended merging is not a first-release requirement; a narrowly dogfooded class may ship only if Phase 9 evidence justifies it. Otherwise the scheduled system should present the recommended next action through the lowest-friction approval channel the supported notification adapter can safely provide.

When policy requires AI supervision, every candidate that could reach mutation must have a successful current advice result at the required assurance level. Advisory budgets may control how much work happens in one run, but deferred, failed, or unreviewed candidates must remain ineligible rather than falling through to deterministic execution. Model-assisted ordering must account for the complete queue or report its bounded view explicitly; the Phase 9 design should choose the simplest context and batching mechanism that establishes this honestly.

Current conflict evidence covers same-repository pull requests that share changed paths or expected alert impact; it does not prove semantic relationships across different packages, manifests, or repositories. Phase 9 ordering should remain conservative when relationship evidence is absent, while later investigator and ecosystem adapters may add deeper evidence where dogfood demonstrates a need.

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

The Codex adapter, advisory contract, economy orchestration, and reviewed orchestration have deterministic coverage, but the current validation record does not yet contain a real Watcher-initiated Codex advisory call. Before Phase 9 claims scheduled model-assisted operation, run an explicitly approved installed-path `economy` exercise and, when a candidate actually qualifies for the second stage, a `reviewed` exercise. Record the transmitted evidence boundary, configured model and reasoning level, call counts, usage, output validation, failure behavior, and confirmation that advice neither mutated GitHub nor authorized an action.

### AI supervision and investigation direction

`off` is the only model-free advisory mode. `economy` already performs bounded LLM triage, while `reviewed` adds a second LLM pass for selected ambiguous or higher-risk candidates. Preserve `off` for privacy-sensitive, offline, diagnostic, and deterministic-only use, but do not mistake model availability for a reason to make the recommended automation path model-free. As provider cost falls and evidence quality improves, the recommended public path should increasingly use model oversight before mutation. Deterministic-only mutation should remain an explicit advanced choice, not the default unattended promise.

Before the public configuration contract stabilizes, reconsider whether `economy` communicates behavior clearly enough. It describes intended workload, not the fact that it invokes an LLM, and can be misread as deterministic mode. Prefer behavior-first setup language such as `model off`, `bounded AI triage`, and `AI triage plus review`; if the configuration key changes, provide an explicit migration or compatibility path rather than silently reinterpreting an existing profile.

The current evidence-only Codex adapter is a safe first provider boundary, not the final agent design. It deliberately cannot research release notes, inspect a checkout, run package-manager analysis, execute tests, or ask subagents. Those capabilities can materially improve judgments about transitive conflicts, ecosystem compatibility, upstream regressions, vulnerable versions, and remediation order. Add them through explicit capability profiles rather than removing all boundaries or silently inheriting a user's ambient agent configuration:

- **bounded reviewer:** normalized Watcher evidence only, no tools; fast, cheap, and reproducible;
- **read-only investigator:** trusted-source web research, repository/API evidence, and a disposable read-only checkout with citations and provenance;
- **sandboxed validator:** the investigator profile plus bounded shell/package-manager/test execution in an isolated environment without host secrets or mutation credentials;
- **high-assurance synthesis:** one or more independent bounded roles over a shared evidence packet when ambiguity or impact justifies the additional cost, followed by an explicit synthesis record.

Capability, assurance, cost, and autonomy are separate controls. A user should be able to choose or accept recommended limits for models, reasoning, calls, tokens or spend, wall time, concurrency, investigation depth, and mutation policy without a vague `full access` switch. A stronger profile may use more tools and context while remaining read-only; a cheap model may still operate under strict action authority; an expensive model does not gain permission to mutate merely because it is capable. Setup and every scheduled report should make the active profile and its material limits visible.

Context should be progressive and workload-aware rather than arbitrarily minimal or indiscriminately maximal. Deterministic collection should suppress no-work and unchanged revisions, normalize shared evidence once, group related alerts and PRs, and cache investigation by stable repository/PR/head/evidence identity. Agents should retrieve more evidence when uncertainty, version relationships, ecosystem risk, or repository impact warrants it. Large queues should be partitioned by repository and dependency/conflict relationships, with summarized evidence passed to synthesis rather than forcing one context to contain every raw record.

Future orchestration should support bounded parallelism across independent repositories and investigative roles while serializing mutation and replanning within each repository. Use a worker queue with explicit concurrency, rate, timeout, cancellation, cost, and retry limits; do not equate one agent per repository or many-agent fan-out with better judgment by default. The useful target is faster independent evidence gathering and role separation where measured value exceeds coordination overhead, not maximum agent count.

The preferred exception path is automatic escalation, not abandonment and not asking the user to perform expert dependency analysis. An evidence-only reviewer may route to a stronger investigator; an investigator may run isolated validation; unresolved work may produce a concise recommendation for defer, remediate, approve, or reject. `Stop` means stop the unsafe action or affected repository queue while preserving evidence, continuing independent safe work, and routing the exception. Some external, legal, licensing, product-policy, credential, deployment, or business decisions cannot honestly be solved by more model effort; human governance remains necessary there, but the product should present a recommended action and the smallest meaningful choice rather than a technical homework assignment.

Long-running investigation should survive provider context compaction and process interruption through a private, schema-versioned job journal and bounded scratch workspace. Persist evidence references, completed steps, conclusions, unresolved questions, budgets, and provenance—not credentials or unrestricted transcripts. Treat model-authored updates to repository policy, prompts, skills, or operational preferences as proposals requiring confirmation and versioned validation, not as automatic self-modification.

Do not hardcode every current Codex, Claude, or other provider tool name into the provider-neutral core. Define abstract capability contracts and let tested adapters translate them to supported provider flags and tools. Maintain a Codex-first implementation and release path now, while leaving the job, evidence, and action boundaries extensible to future local CLIs, APIs, remote workers, and much larger bounded worker pools without prebuilding speculative scale.

Before unattended mutation, evaluate not only whether configured checks passed but whether the repository's validation surface is adequate for the claimed update class. A green but shallow suite does not establish compatibility. Future repository profiles or validator evidence may describe relevant test coverage, build/package-manager resolution, staging/deployment observation, and known blind spots. Do not publish numerical residual-risk promises without empirical evidence; the target is a demonstrably very-low-risk automated class, not a subjective score encoded as policy.

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

In a future unattended mode, authority should come from the operator's deliberate advance authorization of a narrow, inspectable policy envelope—not from model prose at run time. The configured envelope must state repository scope, eligible update/risk classes, required deterministic and model gates, action limits, notification timing, stop conditions, and recovery behavior. Routine low-risk work may eventually complete without per-action approval after dogfooding; non-trivial, ambiguous, sensitive, unsupported, or failed work should stop or request approval according to policy.

A deterministic plan alone is never sufficient evidence for mutation. Phase 6 should classify execution policy by risk and evidence. Model-free execution may eventually be allowed only for an explicit, narrowly bounded, dogfooded low-risk policy class with complete deterministic evidence and required repository validation. Ambiguous, sensitive, major, ecosystem-unsupported, or otherwise higher-risk actions should require configured model review and/or human approval. If a policy requires model review and the provider is unavailable, incomplete, or over budget, execution must fail closed rather than silently fall back to model-free mutation. Passing model review does not replace tests, required checks, deployment gates, or freshness reinspection.

The long-term fully automated mode should produce concise action summaries—what merged, closed, deferred, or failed and why—while retaining detailed machine-readable evidence for agents and audits.

Phase 6 starts with one intentionally narrow mutation surface: approval-gated merging of one Dependabot pull request already present as a `validate_then_merge` step in a saved JSON plan. A read-only execution preflight must recollect current evidence and produce an approval token bound to the plan, step, repository, pull-request number, expected base, head SHA, and explicit merge method. The mutating invocation must present that exact token, repeat the live recheck, and submit the expected head SHA to GitHub so a changed pull request fails closed.

Execution additionally requires explicit profile opt-in, complete collection, a deterministic baseline-candidate assessment, configured required checks observed as passing, and an explicit repository merge method. `repository_default`, missing checks, workflow validation that the tool cannot verify, advisory confidence, and a plan action by themselves are not execution authority. The first implementation performs at most one action per invocation.

Every attempt must produce permission-restricted append-only audit events with a stable action identity, sanitized outcome or failure evidence, and recovery guidance. Concurrent execution against one state directory must fail closed. A retry must reconcile live pull-request state and prior success evidence instead of blindly repeating the mutation.

Automatic execution remains unavailable until the approval-gated path completes an explicitly authorized real-world action and recovery exercise. That checkpoint must reassess the evidence and define a narrowly dogfooded low-risk class before any no-prompt mutation is implemented; it is not unlocked merely because the execution command exists.

### Phase 7 delivery status

Phase 7 added a dependency-free terminal presentation boundary over the unchanged run-once core. Interactive text runs use a restrained stderr spinner and light final-report hierarchy; `--plain`, `TERM=dumb`, and `CI` use persistent line-oriented status; `NO_COLOR` removes color; unsupported Unicode falls back to ASCII; and JSON, Markdown, pipes, redirects, and non-interactive agents receive no animation or decoration. Human renderers remove terminal control sequences and flatten injected line breaks while versioned JSON preserves original structured evidence.

A founder multi-repository installed-path checkpoint exposed follow-up presentation gaps after the initial merge: live setup and doctor calls could remain silent long enough to appear stalled, long collection needed elapsed-time reassurance, concise inspection needed explicit adaptive-highlights semantics with attention prioritization, and standard planning leaked opaque internal conflict identities while repeating universal execution gates. The Phase 7 correction keeps policy and structured evidence unchanged while fixing those human-facing boundaries. The broader work formerly discussed as one phase remains deliberately redistributed: Phase 7 owns run-once UX, Phase 8 agent integration, Phase 9 scheduled operation, and Phase 10 publication.

Concise inspection now includes bounded PR and uncovered-alert detail, error/truncation counts, and an actionable next step. Doctor labels Codex as an optional advice provider. Installation guidance now distinguishes source and target authorization and documents update, rollback, profile replacement, uninstall, and audit-retention boundaries.

The bounded comparison used current GitHub CLI, uv, Ruff, and Codex CLI evidence to adopt explicit lifecycle/authentication guidance and clean stdout/stderr controls while rejecting a full-screen TUI. Deterministic tests, static analysis, isolated wheel installation, piped JSON parsing, Markdown/plain output, and real PTY behavior passed on Ubuntu 22.04 under WSL2. Independent review findings on concise Markdown, freshness sanitization, strict-encoding fallback, `NO_COLOR`, and output documentation were corrected and closed. Hosted ordinary-Linux CI still has not run because of the documented external GitHub account restriction; Phase 7 does not convert that missing evidence into a support claim.

## Later platform and product gates

- WSL2 and ordinary Linux validation first.
- During Phase 7, run the bounded presentation/usability benchmark in the [installed-path validation workflow](validation-workflow.md): an assigned agent should compare a small current sample of relevant popular open-source CLIs, use disposable installations where direct interaction is necessary, capture sanitized screenshots when useful, and record patterns to adopt, adapt, defer, or reject. Use that evidence to replace the current static transient progress line with a restrained animated spinner and clearer terminal status hierarchy for interactive human runs. Preserve a plain fallback, respect `NO_COLOR` and accessibility needs, and suppress animation and styling for JSON, Markdown, pipes, redirected output, logs, and non-interactive agent execution.
- macOS and native Windows CI plus real smoke tests before support claims.
- Publish an ordinary Python package and make one package-index command the primary cross-platform installation path. Keep source-checkout installation for development and private prereleases.
- Before public adoption, publish a license with an appropriate standard warranty/liability disclaimer, a versioned release/tag, and reproducible release provenance; document how users pin an approved version or commit rather than trusting a moving branch. Add plain operational-risk disclosure for mutation and unattended modes without presenting project documentation as a substitute for legal advice or inventing a click-through acceptance flow without evidence that one is needed. Evaluate release-build integrity and dependency policy explicitly, but do not impose an application-style transitive lockfile on the distributable library without a concrete packaging reason.
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
