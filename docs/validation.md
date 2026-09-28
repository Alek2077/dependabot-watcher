# Validation record

This document records reproducible product-level validation without exposing private repository identities, workstation paths, credentials, approval tokens, or private reviewer artifacts. It complements the deterministic test suite; it is not a substitute for functioning hosted CI or independent user adoption.

## Phase 6 approval-gated execution — 2026-09-24

The approval-gated execution boundary was validated in four layers:

1. The complete deterministic suite passed, together with Pyright, compile checks, and repository diff checks.
2. A built wheel was installed into a disposable tool environment and exercised through live `setup`, `doctor`, `inspect`, `plan`, and execution preflight. The preflight was model-free, mutation-free, state-free, and audit-free.
3. The merged product was cloned again from remote `main`, installed through the documented source-checkout path, and exercised from a fresh isolated configuration and state directory.
4. The founder explicitly approved one exact action bound to a private repository, pull request, base branch, full head SHA, squash method, and passing required check.

The real action produced these verified outcomes:

- GitHub accepted exactly one squash merge for the approved Dependabot PR head.
- The execution response reported `mutation_performed: true`, `mutation_authorized: true`, no model call, and the bounded merge result.
- The private audit contained `execution_started` and `execution_succeeded` events with user-only directory/file permissions.
- An immediate retry of the same approved action returned `already_applied`, performed no second GitHub mutation, and appended `execution_reconciled`.
- Independent GitHub state confirmed the exact planned head was merged.
- Fresh post-action inspection observed one remaining open Dependabot PR. GitHub still reported eight open alerts immediately after the merge, six without direct open-PR coverage, demonstrating why post-action state must be observed rather than inferred from expected alert impact.
- The fresh clone remained clean, and disposable tool, configuration, state, and checkout paths were removed after evidence capture.

The implementation was reviewed through independent AI code-review passes using OpenAI Codex and Google Gemini. Findings were reproduced or checked against the implementation before acceptance. Iterative Codex fix checks continued until no medium/high defect remained. Accepted fixes covered saved-plan identity integrity, duplicated live reinspection, output guidance, filesystem ownership/type/no-follow handling, audit-history validation and capacity, pathological JSON, complete audit writes, and actionable CLI coverage.

The real exercise exposed one usability gap: the merged command required a separate `inspect`/`plan` run to summarize the remaining queue. A bounded follow-up added integrated model-free post-action reinspection and a human digest of the exact decision, merge result, required checks, and remaining queue. Incomplete, malformed, or failed follow-up collection preserves the successful merge but suppresses untrustworthy counts and reports the observation as unavailable. Focused review found and corrected partial-result, attention-count, repository-scope, malformed-recommendation, and failure-path coverage gaps. The resulting complete suite passed 240 tests; Pyright reported zero diagnostics; compileall and diff checks passed; and the review fix check reported no remaining medium/high issue. No second real mutation was needed to validate this reporting delta.

The implementation and pre-merge evidence were merged in [PR #8](https://github.com/Alek2077/dependabot-watcher/pull/8). Hosted GitHub Actions did not start because of an external account-level billing/authorization restriction, so this record does not claim a remote matrix result. Functioning hosted CI on the release commit remains a publication gate.

## Post-Phase 6 authority and onboarding correction — 2026-09-24

A private authenticated onboarding checkpoint used the same read-only third-party-adoption prompt across six model/harness combinations. The reviewers materially agreed on the product's approval-gated, non-unattended boundary and exposed several authority and onboarding details that needed correction rather than a Phase 6 redesign.

The resulting correction:

- requires exact, case-sensitive, unambiguous configured check names;
- preserves GitHub's distinct CheckRun and legacy StatusContext evidence instead of collapsing duplicate names;
- accepts execution success only from a structurally valid completed CheckRun with `conclusion = SUCCESS` or a structurally valid StatusContext with `state = SUCCESS`;
- rejects neutral, skipped, pending, missing, malformed, mixed-kind, coerced, boundary-whitespace, duplicate, unknown, and failing check evidence;
- adds a bounded, projected Dependabot-alert permission/readiness probe to `doctor`, with sanitized attention results for command, size, JSON, and shape failures;
- characterizes the deterministic approval value as an exact-action two-invocation confirmation rather than a secret, signature, separate approver identity, or proof of human presence; and
- clarifies private saved-plan handling, authenticated cloning, setup/audit persistence, rebase-merge terminology, required-check discovery, one-action plan invalidation, optional-model evidence/cost disclosure, and the longer-term closed-loop automation/recovery direction.

The final deterministic suite passed 266 tests. Pyright reported zero diagnostics; compileall and repository diff checks passed. A wheel built from an isolated copy of the final tree was installed into a disposable tool environment and exercised through `--version`, `--help`, approval-gated setup, live `doctor`, `inspect`, `plan`, and tokenless `execute` preflight. Doctor reported Dependabot-alert access available, inspection and planning were complete, the live required check retained its `check_run` kind with explicit success, and preflight remained model-free, mutation-free, state-free, and audit-free. The disposable checkout, installation, configuration, plan, and generated confirmation value were removed afterward.

Independent OpenAI Codex and Google Gemini fix checks inspected the final correction. The Codex reviewer found and drove closure of ambiguous/malformed status-record and alert-parser cases across iterative checks; its closure pass reported no remaining medium/high finding. The fresh Gemini fix check independently reported no medium/high finding. Reviewer claims were checked against source and deterministic/live validation rather than accepted automatically.

## Known limits

- This proves one approval-gated merge and idempotent retry, not unattended execution.
- The live target was a private Python repository; broader ecosystem and repository-policy coverage remains bounded by the documented support depth.
- Alert state may lag immediately after a dependency PR merges while GitHub updates dependency data.
- A later public release still requires the license, supported Python floor, release packaging, least-privilege authentication guidance, functioning CI, security/privacy/history review, and anonymous public onboarding gates in the roadmap.

## Phase 7 operational UX and run-once reliability — 2026-09-25

The Phase 7 terminal and lifecycle work was validated on Ubuntu 22.04 under WSL2 without changing GitHub:

- a bounded current comparison of GitHub CLI, uv, Ruff, and Codex CLI informed the auth/config/reset guidance, stdout/stderr boundary, progress controls, and decision not to add a full-screen TUI;
- a dependency-free presentation layer added an interactive stderr spinner, restrained status hierarchy, `--plain`, `NO_COLOR`, `TERM=dumb`, `CI`, and Unicode-to-ASCII behavior while keeping machine and redirected output undecorated;
- human renderers remove terminal control sequences and injected line breaks from external values, preserve the original versioned JSON evidence, and safely replace characters unsupported by a strict output encoding;
- concise text and Markdown inspection now expose bounded PR/alert detail, incomplete-evidence counts, and the next useful command;
- authentication, source-versus-target access, update, rollback, profile replacement, uninstall, and audit-retention guidance was exercised against the installed product boundary; and
- the final deterministic suite passed 277 tests; Pyright reported zero diagnostics; compileall and repository diff checks passed.

A wheel built from the current tree included the new terminal module and was installed under isolated tool, configuration, and state roots. Installed `--version`, `--help`, read-only setup, live doctor, inspect, plan, JSON parsing through `jq`, concise Markdown, static plain output, and an interactive PTY run all completed. Reports recorded zero model calls and zero GitHub mutations. The final plain plan correctly reported the remaining queue and retained the explicit non-authorization message.

A later founder fresh-clone checkpoint exercised three live repositories in one isolated profile and exposed presentation gaps that a one-repository installed run plus deterministic fixtures had missed. The observed setup and doctor calls took roughly five to ten seconds without progress, multi-repository planning could take substantially longer with one unchanging message, concise inspection hid small-queue items behind a fixed three-item cap, and standard plan output rendered shared-path replanning relationships as opaque internal step IDs. The correction adds progress to setup and doctor, elapsed-time reassurance for long interactive operations, adaptive attention-first highlights, an always-visible explanation of executor-enforced safety gates, and human-readable overlap/replanning labels while preserving complete JSON evidence and unchanged authority.

The correction was then rebuilt as a wheel and installed under a fresh temporary tool/configuration/state root. A live three-repository setup, doctor, concise inspection, standard plan, redirected JSON inspection, and machine-output boundary all completed successfully. The installed run reported 3 repositories, 10 open PRs, 4 alerts, 1 attention-required item, and 0 errors/truncations; JSON retained complete evidence with model calls and GitHub mutations false, while redirected stderr remained empty. The deterministic suite now passes 280 tests with zero Pyright diagnostics, successful compilation, and a clean diff check.

An independent OpenAI Codex GPT-5.6 Luna Max review found five medium issues: non-actionable concise Markdown, one unsanitized detailed freshness path, strict-ASCII output failure, incorrect false-like `NO_COLOR` handling, and contradictory stdout documentation. All were reproduced and fixed; the same reviewer then reported no remaining medium/high finding and independently reproduced the 277-test suite. A second-provider review was explicitly waived for this phase.

This record proves the installed WSL2 Ubuntu run-once experience. It does not claim ordinary non-WSL hosted Ubuntu success: GitHub Actions remains unable to start because of the known external account-level billing/authorization restriction, and functioning hosted CI remains a publication gate.

## Phase 8 agent-package prototype — 2026-09-25

The first Phase 8 package contains one portable Watcher skill plus OpenAI and Claude Code compatibility manifests. It contains no MCP server, hook, helper script, scheduler, credential, or mutation implementation. Structural validation passed with the bundled OpenAI skill and plugin validators, Claude Code's strict plugin and marketplace validators, JSON parsing for every manifest, and repository diff checks.

Codex CLI `0.157.0` discovered the local marketplace, listed the package, installed version `0.1.0`, enabled it, and cached the expected manifests and skill under an isolated `CODEX_HOME`. Claude Code `2.1.282` performed the equivalent marketplace discovery and user-scope install under an isolated `CLAUDE_CONFIG_DIR`. Neither test modified the operator's normal client configuration. Claude model execution was not tested because the local Claude client reported that it was signed out; no authentication change was attempted.

A matched, no-product-execution GPT-5.6 Luna comparison tested the same ambiguous maintenance request with and without the skill. The no-skill response stopped safely but offered only generic dry-run, confidentiality, and per-PR review advice. The skill-assisted response selected Watcher's versioned JSON path, preserved opt-in model/privacy disclosure, named the no-token execution preflight and exact action binding, refused to infer authority from a generated confirmation value, and required replanning after a successful merge. This material product-specific improvement supports retaining the thin package.

A manual WSL-connected VS Code smoke then validated the standalone Codex skill path. The `openai.chatgpt` extension `26.917.62051` discovered the local skill and displayed it as an active local-file integration. In a new GPT-6 Sol Medium thread, a no-command scenario preserved one-repository scope across inspection, advice, and planning; selected JSON for agent reasoning; disclosed optional-model privacy and quota/cost requirements; and required a private complete plan, no-token preflight, exact action binding, and separate explicit authorization. It performed no command, model-provider call through Watcher, or mutation in the first boundary test.

A second explicitly authorized read-only run used the same skill to invoke only the installed version, JSON doctor, and one-repository JSON inspection commands. The agent reported Watcher `0.3.0.dev0`, a ready profile, complete evidence, one open Dependabot PR, four open medium-severity alerts, two alerts directly linked to the PR, two without matching PR coverage, passing checks, and a baseline candidate classification. It explicitly kept that classification non-authoritative and reported model calls, GitHub mutations, and automatic execution disabled. The observed counts matched the live product output and no other repository, model call, file mutation, or GitHub mutation was used. This proves standalone-skill discovery and scoped read-only CLI operation in the Codex IDE extension; it does not prove plugin-bundle support there.

The Claude Code VS Code extension `2.1.282` also opened successfully, but runtime and plugin behavior could not be tested because the operator has no Claude account. An unofficial Anthropic-compatible proxy was considered but intentionally excluded: it would test a community translation layer and alternate provider, not supported Anthropic runtime behavior.

The ChatGPT desktop app rendered the custom Watcher plugin and skill icons, repository link, description, and default prompt. The operator installed and activated the plugin and opened a Work chat with it attached. Luna XHigh and Sol Medium both loaded the skill and stopped when the Windows runtime could not find the CLI installed inside WSL. This validates desktop presentation, activation, and missing-prerequisite handling; it does not establish live desktop CLI operation.

A normal Codex CLI `0.157.0` session activated the installed skill and used an existing three-repository read-only profile. The first live call hit the client's default network restriction; `gh` displayed an authentication error, although a host-level check confirmed the credential was valid. After the operator identified the likely sandbox cause, both agents requested one-time approval for the same `doctor` and `inspect` commands. Sol Medium completed the live run and correctly summarized 10 open PRs, 4 alerts, 2 unresolved alerts, and 1 attention-required item with no model call, product state write, or GitHub mutation. Luna XHigh preserved the command and approval boundary but produced no reliable queue summary after retries. Its transcript appeared to contain inspection output, so that run cannot establish a Watcher output failure. These are assisted recovery observations, not proof that a fresh agent diagnoses the sandbox failure unaided.

This exercise also exposed a validation requirement: test agents through the normal client permission flow before applying environment overrides. A diagnostic retry can establish the cause of a failure, but it must be recorded separately from unassisted adoption. Claude runtime activation, public catalog discovery, and retained use remain unverified.

None of the Phase 8 operator exercises invoked Watcher's optional Codex advisory adapter. The adapter and the `economy` and `reviewed` orchestration paths have deterministic tests with simulated provider processes, but the validation record does not yet establish a real Watcher-initiated model call. That live, explicitly approved provider exercise remains required before claiming scheduled model-assisted operation in Phase 9 or complete advisory-provider validation for release.

## Phase 9 reconciliation, scheduling, and advisory operation — 2026-09-27

Phase 9 closed the real-provider gap with bounded installed-path calls. One `economy` exercise used GPT-6 Luna High for one live candidate and produced one schema-valid recommendation with recorded usage and no mutation. One `reviewed` exercise used Luna High triage followed by GPT-6 Sol Medium review for one attention-required candidate, preserved four budget-deferred candidates explicitly, and performed no mutation. A separate Windows Task Scheduler → WSL → Watcher run invoked one Luna High triage call, persisted its usage and complete plan, then suppressed all model work and notification output on an unchanged second run. These results validate the current bounded Codex adapter path; they do not establish tool-capable investigation or another provider.

Phase 9 also exercised native Windows independently of WSL. A disposable native Python 3.12 installation applied and verified current-user-only ACLs, replaced private JSON state atomically, rejected a second process while the reconciliation lock was held, created an offline profile, and completed real Task Scheduler install/readback/idempotency/disable/remove behavior. A native scheduled launch started Watcher and persisted the expected sanitized `github_collection_error` with result code `4` because native GitHub CLI was intentionally unavailable; it made no model call or mutation, and the task was removed afterward. This is real native state, locking, failure-path, and scheduler evidence—not live native GitHub collection or a public Windows support claim.

Three independent Luna Max review passes then divided the final implementation by responsibility: reconciliation/advice/execution/notifications; runtime state and native schedulers; and CLI/output/documentation integration. Their reproduced findings drove exact-state propagation, unconfirmed-merge failure handling, advisory-policy-aware deduplication, safe notification retry, sanitized repository failures, retained unchanged-plan lineage, complete exit-code/persistence documentation, current-user ACL ownership checks, trusted System32 helper resolution, reparse/open identity checks, explicit WSL-user binding, native-definition ownership, orphaned-job detection/removal, cross-host platform rejection, and secure configuration loading.

The final hardened WSL schedule bound the exact invoked Watcher executable, Ubuntu distribution, and Linux user. A real Windows Task Scheduler launch collected three repositories with 10 open Dependabot PRs, 4 alerts, 2 unresolved alerts, and 1 attention-required item; it made no model call or GitHub mutation and returned result `0`. A second launch returned `unchanged`, retained the current plan and lineage, stayed silent, and again returned `0`. Native readback was active and non-drifted, private files retained user-only modes, and the exact task was removed with native absence confirmed.

An installed-wheel closeout caught a WSL-to-PowerShell transport regression introduced by later root-task-path hardening: an embedded backslash reached PowerShell as an empty `TaskPath`. The corrected adapter transports the exact root path through JSON stdin, enforces native root-path readback before every lifecycle mutation and after registration, and normalizes only documented Task Scheduler XML defaults while treating an added repetition duration as drift. Real Task Scheduler install/readback/idempotent reinstall/disable/remove and final absence passed after correction. The same platform reviewer verified the corrections and reported no remaining medium/high findings. The exact reviewed tree passed 379 deterministic tests with 2 native-platform skips, production Pyright with zero diagnostics, compileall, `git diff --check`, isolated sdist/wheel build and installation, installed help checks, and a final active/non-drifted/idempotent native lifecycle. Native Linux and macOS adapters remain deterministically validated but require real platform evidence before support is claimed.
