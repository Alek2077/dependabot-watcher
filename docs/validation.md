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
