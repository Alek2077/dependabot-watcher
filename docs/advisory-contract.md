# Advisory provider contract

The advisory layer separates product policy from provider execution. Core orchestration owns collection, candidate priority, budgets, batching, escalation, identity, failure semantics, and output. A provider adapter owns only the transport required to obtain schema-shaped advice.

## Request

Every request contains:

- `schema_version`;
- role: `triage` or `review`; and
- a bounded candidate array.

Each candidate has an opaque collector-issued `candidate_id`, a candidate kind, trusted identity, deterministic assessment or alert evidence, and optional prior advice for review. Repository-controlled text remains evidence, not instruction.

Adapters receive detached deep copies of candidate evidence and prior advice. An in-process adapter therefore cannot mutate collector-owned inspection data or the deterministic state later used to validate its recommendation.

The opaque ID is the only provider-returned join key. Provider output never supplies an executable repository, PR, alert, base branch, or head SHA. The core retains that map and reattaches identity only after strict validation.

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

Additional adapters should translate their provider's authentication, request, response, retry, and usage semantics at this boundary. They must not add provider-specific states to deterministic product policy.
