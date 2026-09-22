# Planning contract

`dependabot-watcher plan` converts fresh inspection evidence and optional advisory results into a deterministic maintenance strategy. It does not execute the strategy.

## Responsibility boundary

- Inspection owns GitHub facts and bounded ecosystem evidence.
- Advice may explain risk or make a candidate more conservative.
- Planning owns hard priority classes, relationship levels, freshness, step identity, and validation gates.
- A later executor may consume only a fresh plan under separately configured deterministic action authority.

The planner processes the complete inspected queue even when advisory budgets defer model analysis. Missing advice never removes a candidate from the plan.

## Relationship evidence

- `direct`: exact package, ecosystem, manifest path, and patched-version evidence.
- `likely`: package/ecosystem/patched-version evidence plus a collected manifest-inclusion path from the alert manifest to a PR-changed manifest.
- `indirect`: package/ecosystem/patched-version evidence without a collected manifest path.
- `unverified`: the release or relationship cannot be proven from collected facts.

Direct and likely relationships place the PR before the associated alert and make the alert an expected impact rather than a duplicate initial step. Indirect and unverified alerts remain explicit investigation steps. These planning relationships do not change the inspection core's exact-manifest alert classification.

The planner accepts relationship evidence only when the alert and PR records agree in both directions. Version claims, changed-manifest paths, and collected manifest-graph edges must support the stated relationship level. Missing, duplicated, one-sided, or contradictory evidence fails closed before a plan is returned.

The advisory queue follows the same deduplication boundary: when direct or likely collected PR evidence already carries an alert into a PR candidate, the alert is not sent to the model again as a separate candidate. The complete alert remains in inspection evidence and expected plan impact.

## Freshness

Every repository plan binds to the configured base branch and collected base SHA. Every PR step also binds to the collected PR head SHA and update time. The alert queue is represented by a deterministic digest of normalized alert identity and state. Observation timestamps and collection completeness are retained.

Every step requires reinspection after it and before any later execution. A changed base, PR head, relevant checks, alert snapshot, or incomplete collection makes the old plan unsuitable for execution.

PR steps with overlapping changed paths or shared alert impact contain symmetric conflict records. These are staleness/reinspection warnings, not proof that Git will report a textual conflict. Paths are bounded in each conflict record while the complete inspected paths remain in source evidence.

## Workflow profiles

Repository configuration may supply merge method, promotion branch, required checks, deployment environments, and validation notes. These values are bounded data used to explain gates. They are never shell commands and are never executed.

## Safety

Plan actions are recommendations such as `validate_then_merge`, `investigate`, `hold`, `human_attention`, or `no_action`. They do not grant capability. The report and every step state `mutation_authorized: false`; GitHub mutation, notifications, scheduling, persistent writes, and repository checkout remain outside Phase 4.
