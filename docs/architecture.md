# Architecture

Dependabot Watcher has a side-effect-free inspection core and an optional advisory layer.

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

## Ownership boundaries

- `config.py` owns schema versioning, validation, platform config paths, collection bounds, repository identities, and user path-policy overrides.
- `github.py` owns the read-only GitHub adapter. The inspector depends on the `GitHubClient` protocol rather than subprocess details.
- `policy.py` owns deterministic path categorization, check-state assessment, collector-owned candidate identity, recent-sensitive-change matching, and alert correlation.
- `inspector.py` owns bounded multi-repository orchestration and explicit partial-result accounting.
- `output.py` owns the three stdout representations.
- `cli.py` owns command parsing and stable process exit semantics.
- `advisory.py` owns provider-neutral request/result contracts, deterministic candidate priority, budgets, batching, escalation, identity rebinding, and advisory orchestration.
- `providers/` owns provider-specific execution. The initial `codex_cli` adapter uses non-interactive, ephemeral, read-only Codex execution and strict structured output.
- `prompts/` owns generic triage/review roles and the shared provider response schema.

## Safety invariants

- The standalone core contains no GitHub mutation method.
- Repository, PR number, base, head reference, and head SHA come from configuration and GitHub collection, never from a model.
- `inspect` never invokes a model. `advise` invokes one only when explicitly enabled in configuration.
- Model output is advisory and is rebound through an opaque collector-issued candidate ID.
- A provider cannot select the executable repository, PR, alert, base, or head commit.
- Advice cannot clear a deterministic blocker or authorize mutation.
- Inspection does not persist reports, state, caches, or logs.
- Collection failures and limit hits cannot be mistaken for a complete empty queue.
- Pull-request changed paths use an explicitly paginated REST endpoint; nested GitHub CLI file-list caps are not trusted.
- Incomplete recent merged history blocks baseline-candidate classification because sensitive-change evidence is not complete.
- A `baseline_candidate` classification is evidence for later policy evaluation, never authorization to merge.

## Advisory modes

- `off`: deterministic inspection evidence only; zero provider calls.
- `economy`: bounded triage of a deterministically prioritized candidate queue.
- `reviewed`: bounded triage plus bounded second-stage review for ambiguous, sensitive, unresolved, or higher-risk candidates.

Modes control workload rather than universal model names. Provider/model/reasoning selection remains explicit adapter configuration.

## Future boundaries

Additional providers, persistent decision records, schedulers, notifications, gated mutation, high-coverage analysis, and user-selectable autonomy/observability belong to later phases. They must consume the same trusted identity and deterministic evidence without weakening fail-closed behavior.
