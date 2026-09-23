# Architecture

Dependabot Watcher has a side-effect-free inspection core, an optional advisory layer, and a deterministic planning layer.

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

## Ownership boundaries

- `config.py` owns schema versioning, validation, platform config paths, collection bounds, repository identities, and user path-policy overrides.
- `setup.py` owns validated setup choices and exclusive creation of one non-secret user profile. It does not overwrite existing configuration or invoke a model.
- `diagnostics.py` owns bounded executable/auth/repository/configured-branch readiness checks and Dependabot ecosystem support reporting. Provider commands are adapter-owned rather than configuration-selected, repository YAML is safely bounded, and raw command output is not retained in reports.
- `operational_output.py` renders setup and diagnostic reports without mixing them into inspection evidence.
- `github.py` owns the read-only GitHub adapter. The inspector depends on the `GitHubClient` protocol rather than subprocess details.
- `policy.py` owns deterministic path categorization, check-state assessment, collector-owned candidate identity, recent-sensitive-change matching, and alert correlation.
- `inspector.py` owns bounded multi-repository orchestration and explicit partial-result accounting.
- `manifests.py` owns bounded, data-only Python requirements inclusion evidence.
- `planning.py` owns stable plan identity, deterministic ordering, PR/alert relationship levels, freshness, and validation gates.
- `output.py` owns the three stdout representations.
- `cli.py` owns command parsing and stable process exit semantics.
- `advisory.py` owns provider-neutral request/result contracts, deterministic candidate priority, budgets, batching, escalation, identity rebinding, and advisory orchestration.
- `providers/` owns provider-specific execution. The initial `codex_cli` adapter uses non-interactive, ephemeral, read-only Codex execution and strict structured output.
- `prompts/` owns generic triage/review roles and the shared provider response schema.

The GitHub collection and planning contracts are ecosystem-neutral wherever GitHub supplies normalized Dependabot evidence. Ecosystem adapters add bounded deterministic evidence; they do not replace the generic core or silently widen automatic-action eligibility. Provider adapters likewise translate one supported CLI or API transport into the stable advisory contract without owning repository policy, context selection, or action authority.

## GitHub access boundary

Authenticated access is a product requirement for private repositories and Dependabot security alerts; GitHub CLI is only the first implementation of that requirement. The current adapter delegates read-only repository, pull-request, and API calls to `gh`, which also supplies its supported browser, credential-store, or environment-token authentication. No IDE is required.

Editor authentication is intentionally outside this boundary. The watcher does not inspect or extract VS Code or extension credentials, because they belong to another application's private integration and are not a safe or durable credential API.

The `GitHubClient` protocol keeps collection and policy independent from GitHub CLI subprocess details. A later direct REST client, fine-grained-token adapter, OAuth/device flow, or GitHub App must preserve the same bounded reads, pagination, error visibility, secret-handling rules, and repository identity contract. Adding an adapter may reduce installation friction; it does not remove the need for explicit authorization.

## Safety invariants

- The standalone core contains no GitHub mutation method.
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
- Repository workflow strings are displayed as configuration evidence and are never executed.
- Configuration cannot claim mutation authority in Phase 5: autonomy is `read_only` and mutation must be false.
- Human verbosity changes presentation only; JSON evidence, collection, workload, and authority are invariant.
- Setup can persist only the explicitly requested non-secret profile; doctor remains non-persistent and model-free.

## Advisory modes

- `off`: deterministic inspection evidence only; zero provider calls.
- `economy`: bounded triage of a deterministically prioritized candidate queue.
- `reviewed`: bounded triage plus bounded second-stage review for ambiguous, sensitive, unresolved, or higher-risk candidates.

Modes control workload rather than universal model names. Provider/model/reasoning selection remains explicit adapter configuration.

## Future boundaries

Additional providers, persistent decision records, schedulers, notifications, gated mutation, high-coverage analysis, and user-selectable autonomy/observability belong to later phases. They must consume the same trusted identity and deterministic evidence without weakening fail-closed behavior.
