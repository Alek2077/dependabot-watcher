# Advisory provider contract

The advisory layer separates product policy from provider execution. Core orchestration owns collection, candidate priority, budgets, batching, escalation, identity, failure semantics, and output. A provider adapter owns only the transport required to obtain schema-shaped advice.

When inspection has already related an alert to a PR with `direct` or `likely` planning evidence, the alert is carried inside the PR candidate rather than sent to the model again as a duplicate candidate. The complete alert remains in the underlying inspection report. This workload optimization does not change the alert's fail-closed inspection classification.

## Request

Every request contains:

- `schema_version`;
- role: `triage` or `review`; and
- a bounded candidate array.

Each candidate has an opaque collector-issued `candidate_id`, a candidate kind, trusted identity, deterministic assessment or alert evidence, and optional prior advice for review. Repository-controlled text remains evidence, not instruction.

Adapters receive detached deep copies of candidate evidence and prior advice. An in-process adapter therefore cannot mutate collector-owned inspection data or the deterministic state later used to validate its recommendation.

The opaque ID is the only provider-returned join key. Provider output never supplies an executable repository, PR, alert, base branch, or head SHA. The core retains that map and reattaches identity only after strict validation.

## Current context envelope

The initial adapter sends only the triage or review prompt plus a bounded JSON candidate batch. For a pull-request candidate, that packet contains collector-owned repository/PR/base/head identity; untrusted title and URL display evidence; and the deterministic assessment, including author, observation/update times, draft/mergeability/merge-state/check evidence, changed-path classification, recent-sensitive-change overlap, blockers, classification, and correlated alert evidence when present. For an uncovered alert candidate, it contains collector-owned repository/alert identity and the collected alert evidence, including dependency, ecosystem, manifest, severity, vulnerable/patched-version, advisory, and open-PR relationship fields that GitHub and Watcher supplied. A review-stage candidate additionally receives the complete prior triage decision. Candidates may see the other candidates in the same bounded batch, but neither stage receives the complete unselected queue merely because it exists.

The current packet does not include a target-repository checkout, full diff or patch, arbitrary source files, PR discussion, complete manifest or lockfile contents, release notes, upstream issues, external web results, repository test-quality assessment, deployment/runtime observations, or ambient user/project instructions. The prompt requires the model to identify missing, incomplete, stale, or conflicting evidence in `unresolved_questions`; confidence must describe the supplied evidence rather than conceal absent context. Future context additions should be versioned, provenance-bearing, decision-relevant, and covered by contract tests so provider adapters receive equivalent meaning even when their tool transports differ.

## Result

A provider returns one decision for every requested candidate with exactly these fields:

- `candidate_id`;
- `recommended_action`;
- `risk_level`;
- `confidence`;
- `requires_review`;
- `summary`;
- `rationale`;
- `evidence`; and
- `unresolved_questions`.

The shared JSON Schema under `src/dependabot_watcher/prompts/` constrains model generation. Core Python validation independently rejects unknown fields, unknown or duplicate IDs, missing candidates, invalid enums, invalid types, and empty required text.

Recommendations are deliberately advisory. `merge_candidate` means the provider considers a pull request suitable for a later deterministic authorization stage; it is not permission to merge. The report and every decision or deferred candidate set `mutation_authorized: false`.

The action vocabulary separates automated routing from human intervention:

- `review_required`: a stronger configured model should reassess the existing evidence;
- `investigate`: additional evidence or remediation analysis is needed; and
- `human_attention`: the configured review completed but could not resolve the decision safely, or the decision inherently belongs to the maintainer.

Triage cannot request `human_attention` directly; it routes unresolved judgment to `review_required`. In `reviewed` mode, a candidate selected for review has no effective merge recommendation unless that review completes successfully. A missing, failed, or budget-deferred review produces a conservative `review_required` effective result while retaining triage as evidence. The review stage itself cannot return `review_required` or ask for another model review; it must reach a substantive recommendation or `human_attention`.

Core validation rejects `merge_candidate` for alerts and for any pull request whose deterministic classification is not `baseline_candidate`. A provider therefore cannot clear failing checks, sensitive paths, wrong-base state, incomplete evidence, or another deterministic blocker through either triage or review output.

## Adapter obligations

An adapter must:

- implement the `AdvisoryProvider` protocol;
- accept the versioned request without changing candidate identity;
- return the shared canonical result vocabulary;
- enforce a timeout;
- expose provider/model/reasoning provenance and usage when available;
- avoid logging or returning credentials;
- suppress raw provider stdout/stderr from user-facing diagnostics;
- use the least privilege supported by its transport;
- raise an explicit advisory error on transport, authentication, timeout, or malformed-output failure; and
- never perform GitHub or repository mutation as part of analysis.

The core does not silently fall back to another provider. Such fallback could spend money unexpectedly or mix evidence from differently configured services.

## Initial Codex adapter

The first adapter uses `codex exec` with an ephemeral session, read-only sandbox, isolated temporary working directory, strict output schema, sanitized process environment, ignored user configuration/rules, and explicit model/reasoning/timeout configuration. Web search, shell/unified execution, apps/connectors, dependency-install tools, and subagents are disabled; model-launched command environments are also configured to inherit nothing. Saved Codex authentication remains available. It parses JSONL completion and usage events, then hands the final JSON object to the same core validator used for every provider.

These restrictions make the provider a bounded interpreter of collector-owned evidence rather than an autonomous repository or internet investigator. Ignoring user configuration and rules prevents unrelated local instructions from silently changing the advisory contract. Disabling interactive approval requests makes the non-interactive child process fail instead of expanding its tools or authority mid-call; consent to transmit evidence and consume provider quota belongs to Watcher setup and operator policy before invocation. Additional release, upstream-source, reachability, or checkout evidence should enter through an explicit bounded collector or investigation capability with its own provenance and audit boundary, not through undeclared provider tool use.

Do not mistake this initial evidence-only adapter for the permanent ceiling on model capability. A future investigator adapter may legitimately use web research, a read-only disposable checkout, sandboxed shell commands, package-manager analysis, test execution, or bounded subagents when those tools materially improve compatibility or remediation judgment. Such access must be an explicit named capability profile with visible scope, provenance, time/call/token limits, isolated credentials, and no implicit GitHub mutation authority. Scheduled automation must not silently inherit an operator's ambient interactive-agent permissions, because those permissions may include unrelated files, secrets, write access, applications, or unbounded spend. If an evidence-only run needs more capability, it should return a structured investigation requirement that can be routed automatically to a pre-authorized stronger profile or surfaced for narrow approval; it should not improvise a hidden privilege expansion.

The provider-neutral core should name abstract capabilities such as web research, repository reading, scratch writing, sandboxed command execution, test execution, and bounded worker delegation. Each provider adapter translates only the capabilities it can support and test into current provider-specific flags or tools; the core must not maintain a brittle universal list of every vendor tool name. Unknown capabilities fail closed. A future advanced delegated-profile mode may intentionally use a named user-managed agent environment, but it must disclose and snapshot the effective authority and must not become the default unattended mutation boundary.

Additional adapters should translate their provider's authentication, request, response, retry, and usage semantics at this boundary. They must not add provider-specific states to deterministic product policy.
