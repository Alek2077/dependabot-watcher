# Execution contract

`dependabot-watcher execute` is the product's first and only mutating edge. It can merge one exact Dependabot pull request per invocation. It cannot close, comment, approve, label, rebase an open PR branch, create a remediation PR, deploy, promote a branch, or execute unattended. An explicitly configured `rebase` method means GitHub rebase-merge, not a separate PR-branch rebase operation.

## Authority layers

Execution requires all three independent layers:

1. **Capability:** the operational profile explicitly uses `autonomy = "approval_required"` and `mutation_enabled = true`.
2. **Eligibility:** a saved complete JSON plan contains one `validate_then_merge` PR step that remains an unblocked deterministic baseline candidate under a fresh live reinspection and the configured repository workflow policy.
3. **Action authorization:** a preceding read-only execution preflight emits an approval confirmation token bound to that saved repository plan ID, step ID, repository, PR number, expected base, head SHA, and merge method. The mutating invocation must supply the exact current token.

None of these layers substitutes for another. Setup does not authorize an action. A plan, model recommendation, or baseline classification does not authorize mutation. Before preflight, the executor recomputes the saved step, repository-plan, and overall plan identities and rejects content that no longer matches them. The verified repository-plan identity binds the complete material step, including conservative advice and action, into the approval confirmation token. A token becomes invalid when its exact action binding no longer matches.

The token is a deterministic truncated SHA-256 value over the public action binding. It is not a credential, signature, separate approver identity, expiry mechanism, or proof that a human ran preflight. Any trusted local caller with the binding and permission to run both commands can reproduce it. The boundary provides deliberate two-invocation confirmation and exact-action freshness, while actual authority still depends on local process/tool governance, explicit profile opt-in, and the active GitHub credential.

## Eligible first action

The initial executor accepts only a pull-request step whose action is `validate_then_merge` and whose deterministic classification remains `baseline_candidate`. Collection and repository-plan status must be complete. The expected and observed base must equal the configured branch, the head must be unchanged, blockers must be empty, and every configured required check must match an exact, case-sensitive, unambiguous observed name. A qualifying check is either a completed GitHub check run with `conclusion = SUCCESS` or a legacy commit-status context with `state = SUCCESS`. `NEUTRAL`, `SKIPPED`, pending, missing, malformed, unknown, and failing evidence does not satisfy an execution-required check.

Repository policy must name one explicit `merge`, `squash`, or `rebase` method and at least one required check. `repository_default` is not executable. A configured promotion branch, deployment environment, or free-form validation note makes the action ineligible because this implementation cannot verify those gates.

Execution reinspection is deliberately model-free. If optional advice made the saved plan more conservative, that action remains non-executable. Free-form model wording is not part of authorization, and a model cannot select or expand the target.

## Freshness and race boundaries

The preflight loads the saved JSON plan, reads the exact PR's live state, and regenerates a deterministic plan for only the target repository. It compares material base, head, update, alert-snapshot, check, classification, blocker, action, and workflow evidence. The approved invocation repeats that process after acquiring the local execution lock. Saved plans contain private repository evidence and should be created and retained with user-only permissions.

The GitHub merge request names the repository and PR and includes the expected full head SHA. GitHub rejects a changed head and enforces its current server-side repository rules. No remote API can atomically bind every locally observed alert or check field to the merge request, so a residual race remains; the final recheck is intentionally adjacent to the mutation, and branch protection remains an important server-side backstop.

## Audit, idempotency, and recovery

Actual execution creates a current-user-owned private state directory, takes an exclusive regular-file lock, and appends JSON Lines events to a current-user-owned private regular file before and after the GitHub call. Symbolic links and non-regular state files fail closed. Event types are `execution_started`, `execution_succeeded`, `execution_failed`, and `execution_reconciled`. Before a new action, the existing bounded history is parsed and its event structure is validated under the lock; malformed, non-UTF-8, or pathologically encoded history blocks mutation and is also reported by `doctor`. Records contain the exact action binding and sanitized outcome, never the approval token, credentials, raw provider output, or raw GitHub error text. The audit has a bounded safety ceiling; the executor computes and reserves the larger possible terminal-event payload before mutation, bounds response fields, and blocks a new GitHub call when the remaining capacity is insufficient so an operator can deliberately archive the reviewed history. Audit appends require a complete write and successful synchronization; an incomplete start record blocks the GitHub call, while a terminal I/O failure after a remote result requires live-state reconciliation.

A retry reads live PR state. If the exact planned head is already merged, it does not call the merge endpoint again; an authorized retry appends a reconciliation event and records whether a prior success event exists. A closed-unmerged PR, changed head/base, partial reinspection, policy change, malformed audit, insecure state permissions, or existing lock fails closed.

After a successful merge, execution performs a model-free repository reinspection before releasing the local action flow. The response identifies the validated deterministic classification and required checks, GitHub merge result, and refreshed counts for open PRs, open alerts, alerts without direct open-PR coverage, attention-required items, and recommended remaining steps. Counts are shown only for complete, structurally valid collection. This is an immediate observation rather than a promise that GitHub has already completed every downstream dependency scan. The changed base SHA invalidates the remaining saved steps for that repository, so another action requires a fresh plan. If reinspection is partial, malformed, or fails after GitHub has accepted the merge, the merge remains reported as successful and the post-action state is explicitly unavailable; run `inspect` and `plan` before considering another action.

After failure, re-run inspection before considering another action. A leftover lock after interruption must be inspected alongside GitHub state and the audit log; it is not automatically deleted as stale.

## Automatic execution boundary

There is no automatic mode in this contract. The approval-gated path must first complete an explicitly authorized real action and recovery/idempotency exercise. Only then may a later design pass define and dogfood a narrowly bounded low-risk automatic class. Enabling the current execution capability does not reserve or imply that future authority.

Any later automatic class must distinguish update magnitude, grouping, ecosystem support, repository validation, and downstream risk explicitly rather than admitting major, grouped, or otherwise ambiguous updates merely because their changed paths and current checks look clean.
