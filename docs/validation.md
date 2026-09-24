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

## Known limits

- This proves one approval-gated merge and idempotent retry, not unattended execution.
- The live target was a private Python repository; broader ecosystem and repository-policy coverage remains bounded by the documented support depth.
- Alert state may lag immediately after a dependency PR merges while GitHub updates dependency data.
- A later public release still requires the license, supported Python floor, release packaging, least-privilege authentication guidance, functioning CI, security/privacy/history review, and anonymous public onboarding gates in the roadmap.
