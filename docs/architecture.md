# Architecture

Dependabot Watcher has a side-effect-free inspection core, an optional advisory layer, a deterministic planning layer, and one separately gated execution edge.

The documentation and command contracts are maintained for both human operators and capable automation agents. Future changes should preserve explicit scope and authority, structured evidence, and clear operational commands.

Phase 5 adds an operational edge around those product layers:

```text
setup -> versioned non-secret profile -> doctor -> inspect / advise / plan
  |                                      |
  +-- explicit local write only          +-- bounded read-only discovery
```

```text
TOML configuration
        |
        v
CLI -> inspector -> GitHubClient -> gh / GitHub APIs
          |               |
          v               v
   deterministic     read-only evidence
   path/PR/alert
   assessment
          |
          v
 text / JSON / Markdown on stdout
```

Optional advice is layered after inspection:

```text
inspection report -> deterministic candidate queue -> AdvisoryProvider
                           |                              |
                           v                              v
                    budgets/batches              provider adapter
                           |                              |
                           +------ validated advice -----+
                                          |
                                          v
                        collector-bound advisory report
```

Planning consumes the complete queue, not only model-budgeted candidates:

```text
inspection + optional advice -> deterministic evidence graph -> Recommended Order
                                      |                           |
                                      v                           v
                          freshness / hard priority      text / JSON / Markdown
```

Execution never consumes model-selected identity or a free-form instruction:

```text
saved JSON plan -> live replan -> exact material-evidence match -> approval token
                                                              |
                                     final locked recheck -----+
                                                              v
                                            exact-head GitHub merge -> audit events
```

## Ownership boundaries

- `config.py` owns schema versioning, validation, platform config paths, collection bounds, repository identities, and user path-policy overrides.
- `setup.py` owns validated setup choices and exclusive creation of one non-secret user profile. It does not overwrite existing configuration or invoke a model.
- `diagnostics.py` owns bounded executable/auth/repository/configured-branch readiness checks and Dependabot ecosystem support reporting. Provider commands are adapter-owned rather than configuration-selected, repository YAML is safely bounded, and raw command output is not retained in reports.
- `operational_output.py` renders setup and diagnostic reports without mixing them into inspection evidence.
- `github.py` owns the GitHub transport. Collection methods remain read-only; the sole mutation method merges one named PR using an explicit expected head SHA and supported merge method.
- `policy.py` owns deterministic path categorization, check-state assessment, collector-owned candidate identity, recent-sensitive-change matching, and alert correlation.
- `inspector.py` owns bounded multi-repository orchestration and explicit partial-result accounting.
- `manifests.py` owns bounded, data-only Python requirements inclusion evidence.
- `planning.py` owns stable plan identity, deterministic ordering, PR/alert relationship levels, freshness, and validation gates.
- `execution.py` owns saved-plan validation, live material-evidence matching, exact approval tokens, execution eligibility, one-action locking, audit events, reconciliation, and the call to the sole GitHub mutation method.
- `output.py` owns the three stdout representations.
- `cli.py` owns command parsing and stable process exit semantics.
- `advisory.py` owns provider-neutral request/result contracts, deterministic candidate priority, budgets, batching, escalation, identity rebinding, and advisory orchestration.
- `providers/` owns provider-specific execution. The initial `codex_cli` adapter uses non-interactive, ephemeral, read-only Codex execution and strict structured output.
- `prompts/` owns generic triage/review roles and the shared provider response schema.

The GitHub collection and planning contracts are ecosystem-neutral wherever GitHub supplies normalized Dependabot evidence. Ecosystem adapters add bounded deterministic evidence; they do not replace the generic core or silently widen automatic-action eligibility. Provider adapters likewise translate one supported CLI or API transport into the stable advisory contract without owning repository policy, context selection, or action authority.

## GitHub access boundary

Authenticated access is a product requirement for private repositories and Dependabot security alerts; GitHub CLI is only the first implementation of that requirement. The current adapter delegates bounded repository, pull-request, and API reads to `gh`, plus the sole explicitly enabled exact-head PR merge call. GitHub CLI also supplies its supported browser, credential-store, or environment-token authentication. No IDE is required.

Editor authentication is intentionally outside this boundary. The watcher does not inspect or extract VS Code or extension credentials, because they belong to another application's private integration and are not a safe or durable credential API.

The `GitHubClient` protocol keeps collection and policy independent from GitHub CLI subprocess details. A later direct REST client, fine-grained-token adapter, OAuth/device flow, or GitHub App must preserve the same bounded reads, pagination, error visibility, secret-handling rules, and repository identity contract. Adding an adapter may reduce installation friction; it does not remove the need for explicit authorization.

## Safety invariants

- The only GitHub mutation method is an exact repository/PR/head-bound merge. No generic command runner or model-selected mutation surface exists.
- Repository, PR number, base, head reference, and head SHA come from configuration and GitHub collection, never from a model.
- `inspect` never invokes a model. `advise` and `plan` invoke one only when explicitly enabled in configuration.
- Model output is advisory and is rebound through an opaque collector-issued candidate ID.
- A provider cannot select the executable repository, PR, alert, base, or head commit.
- Advice cannot clear a deterministic blocker or authorize mutation.
- Inspection does not persist reports, state, caches, or logs.
- Collection failures and limit hits cannot be mistaken for a complete empty queue.
- Pull-request changed paths use an explicitly paginated REST endpoint; nested GitHub CLI file-list caps are not trusted.
- Incomplete recent merged history blocks baseline-candidate classification because sensitive-change evidence is not complete.
- A `baseline_candidate` classification is evidence for later policy evaluation, never authorization to merge.
- Plans bind repository/base state, alert snapshot, and PR head state; every step requires reinspection and remains non-mutating.
- Repository workflow strings are displayed as configuration evidence and are never executed. Initial execution refuses promotion, deployment, or free-form validation gates it cannot verify.
- Configuration may enable only `approval_required` execution; it cannot authorize a particular action. The default remains `read_only`, and unsupported authority fails closed.
- A saved plan, `baseline_candidate`, advisory result, or approval confirmation token alone is insufficient. Execution requires a matching fresh plan, configured required checks observed with explicit success, an explicit merge method, a second recheck under the execution lock, and the exact current token.
- Execution reinspection is deterministic and makes no new model call. A model-conservative saved action remains binding, while model prose or nondeterminism cannot destabilize the freshness identity.
- Each invocation mutates at most one pull request. The GitHub merge request includes the expected head SHA.
- Execution audit records are append-only JSON Lines with user-only permissions. They exclude credentials, raw provider output, and raw GitHub error text.
- Concurrent mutation against one state directory fails closed. Already-merged exact heads reconcile without repeating the mutation.
- Human verbosity changes presentation only; JSON evidence, collection, workload, and authority are invariant.
- Setup can persist only the explicitly requested non-secret profile; doctor remains non-persistent and model-free.

## Advisory modes

- `off`: deterministic inspection evidence only; zero provider calls.
- `economy`: bounded triage of a deterministically prioritized candidate queue.
- `reviewed`: bounded triage plus bounded second-stage review for ambiguous, sensitive, unresolved, or higher-risk candidates.

Modes control workload rather than universal model names. Provider/model/reasoning selection remains explicit adapter configuration.

## Future boundaries

Additional providers, schedulers, notifications, broader mutation types, automatic execution, high-coverage analysis, and wider autonomy/observability belong to later phases. They must consume the same trusted identity and deterministic evidence without weakening fail-closed behavior.

### Scheduler, platform, and remote-runner boundary

The recurring core is an idempotent run-once reconciler, not a permanent Watcher daemon. Watcher owns collection, stable revision identity, no-work and duplicate suppression, policy, bounded durable run history, notification decisions, and action/recovery state. A platform adapter owns only registration, wake-up, environment selection, and removal, then invokes the same reconciler.

The initial adapters are Windows Task Scheduler invoking WSL2, Windows Task Scheduler invoking native Windows, a systemd user timer on native Linux, and `launchd` on macOS. Each adapter implements a small common lifecycle boundary: capability detection, non-mutating preview, idempotent install/update, native-definition readback, status and drift reporting, disable/remove, and the exact scheduler-facing command. Every registration has a stable Watcher-owned identity so updates cannot create duplicates across old cadence or authority settings and removal cannot target unrelated jobs. Registration uses absolute executable/configuration/state paths and the same least-privileged user that owns the Watcher profile and GitHub/provider credentials by default; elevated or system-wide operation is a separately disclosed advanced choice.

A missed interval is never replayed as a backlog. Windows uses `StartWhenAvailable` with overlapping instances suppressed, and the proven WSL path performs one overdue reconciliation against current authoritative state. The systemd adapter invokes reconciliation shortly after the user manager starts and then at the configured interval, but its monotonic interval does not yet guarantee an immediate post-suspend invocation; real suspend/resume catch-up remains a native-Linux Phase 10 gate. The launchd adapter uses `RunAtLoad` plus `StartInterval`, with actual sleep/login/power-cycle behavior likewise requiring installed macOS evidence. Closing a WSL terminal is not a failure because the Windows task can start the distribution; a sleeping or powered-off machine cannot run local work until a supported native trigger runs after it returns.

Platform-private storage is an explicit boundary. POSIX ownership/mode, no-follow, lock, and atomic-write assumptions must not be reused as if they establish equivalent native Windows privacy or durability. The state layer must provide platform-appropriate private-directory creation, file validation, locking, atomic replacement, and recovery while preserving the same higher-level state schema and fail-closed invariants.

Scheduled history is lineage, not an unbounded transcript. Retain stable job/run identity, repository and evidence revisions, advice assurance and provider usage, plan supersession, action/audit outcome, post-action observation, deferral/blocker reason, recovery state, and timestamps. A fresh plan revokes stale executable authority but links to the event and plan it supersedes; token-bearing execution checks the selected state directory and rejects a plan that current reconciliation state superseded or revoked. Credentials, secret material, and unrestricted provider transcripts are never scheduled-state fields.

The reconciler must remain runnable from a future user-owned CI job, self-hosted server/container, or managed service. That seam preserves a path to continuous availability without coupling the local product to multitenant hosting, webhook infrastructure, or one scheduler library before demand justifies them.

### Implementation-language boundary

Python owns the current CLI, evidence, policy, advisory, execution, state, and scheduler-adapter implementation. Preserve language-neutral boundaries around persisted schemas, structured JSON reports, job identities, subprocess invocation, provider contracts, and future remote-runner messages so the architecture does not require every component to share one runtime.

A future Go, Rust, or other native component must solve a measured problem and remain narrower than the product core. Prefer a separately versioned executable or service with explicit input/output schemas, capability and authority limits, timeouts, cancellation, provenance, and compatibility tests. Do not share opaque in-process state, duplicate policy or execution authorization, or let a native helper silently gain broader filesystem, credential, network, or mutation authority than the Python caller.

The leading future Go roles are native launcher/scheduler registration, single-binary distribution support, a low-overhead webhook or remote runner, and bounded high-concurrency workers. Keep these roles replaceable and preserve the Python run-once reconciler and exact-action boundary unless evidence shows that replacing a specific responsibility produces a better reliability and maintenance outcome than adapting it.

### Future agent-runtime seam

Do not couple deeper AI behavior directly to the synchronous Codex subprocess. Preserve `AdvisoryProvider` as the bounded evidence-review contract while adding future investigation through versioned jobs and artifacts:

```text
scheduler/reconciler -> normalized evidence job -> bounded worker pool
                                                   |        |
                                                   v        v
                                            investigators validators
                                                   \        /
                                                    synthesis
                                                        |
                                                        v
                                              policy/action boundary
```

Jobs should carry stable repository/PR/head/evidence identities, requested capability and assurance profiles, provider/model provenance, budgets, deadlines, and cancellation state. Independent repository investigation may run concurrently, but mutation and post-action replanning remain serialized per repository. This seam should permit a future local queue, background workers, remote execution, or multiple providers without forcing the run-once CLI, evidence schema, or exact-action execution boundary to depend on one orchestration library.

Long investigations need a private resumable work journal outside the installed package and target repositories. Store schema-versioned plans, evidence references, completed steps, decisions, unresolved questions, and provider/cost provenance under the permission-restricted Watcher state boundary, keyed by stable job and revision identity. Give investigators a bounded scratch workspace when writing intermediate notes materially improves resumability; do not let them edit the installed Watcher package, target repository, plugin, `SKILL.md`, or policy merely to preserve context. Raw credentials, secret-file contents, unrestricted provider transcripts, and unrelated user data never belong in the journal.

Learning should be controlled rather than silent self-modification. Repeated operator corrections or validated repository facts may produce proposed repository-profile, policy, prompt, or skill changes with provenance and evidence. Material scope, spend, capability, mutation, and instruction changes require explicit confirmation, deterministic validation where applicable, and a versioned product update. Disposable worker notes are not durable policy merely because a model wrote them.
