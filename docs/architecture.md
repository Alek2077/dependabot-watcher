# Architecture

Dependabot Watcher currently has one capability: side-effect-free inspection.

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

## Ownership boundaries

- `config.py` owns schema versioning, validation, platform config paths, collection bounds, repository identities, and user path-policy overrides.
- `github.py` owns the read-only GitHub adapter. The inspector depends on the `GitHubClient` protocol rather than subprocess details.
- `policy.py` owns deterministic path categorization, check-state assessment, collector-owned candidate identity, recent-sensitive-change matching, and alert correlation.
- `inspector.py` owns bounded multi-repository orchestration and explicit partial-result accounting.
- `output.py` owns the three stdout representations.
- `cli.py` owns command parsing and stable process exit semantics.

## Safety invariants

- The standalone core contains no GitHub mutation method.
- Repository, PR number, base, head reference, and head SHA come from configuration and GitHub collection, never from a model.
- Models are absent from the current runtime.
- Inspection does not persist reports, state, caches, or logs.
- Collection failures and limit hits cannot be mistaken for a complete empty queue.
- Pull-request changed paths use an explicitly paginated REST endpoint; nested GitHub CLI file-list caps are not trusted.
- Incomplete recent merged history blocks baseline-candidate classification because sensitive-change evidence is not complete.
- A `baseline_candidate` classification is evidence for later policy evaluation, never authorization to merge.

## Future adapter boundaries

Provider-neutral model advice, durable decision records, schedulers, notifications, and gated mutation are intentionally outside the current package surface. Each requires a separate contract and validation phase. They must consume trusted candidate identity from this core and must not weaken its fail-closed reporting.
