# Configuration

Dependabot Watcher uses a versioned TOML file. Unknown keys are rejected so spelling mistakes cannot silently change behavior.

```toml
schema_version = 1

[collection]
history_lookback_days = 30
max_open_prs = 100
max_open_alerts = 500
max_history_items = 200
max_files_per_pull_request = 500
command_timeout_seconds = 120

[path_policy]
additional_dependency_patterns = []
additional_sensitive_patterns = []

[[repositories]]
name = "example-org/example-app"
default_branch = "main"

[repositories.path_policy]
additional_dependency_patterns = ["deps/*.lock"]
additional_sensitive_patterns = ["ops/production/**"]
```

## Repository entries

`name` must use `owner/repository` form. `default_branch` is the expected Dependabot target; a PR targeting another branch is reported for attention.

Patterns under the top-level `path_policy` apply to every repository. Patterns under `repositories.path_policy` apply only to that repository. Paths are repository-relative and use `/` separators.

Additional dependency patterns identify files that are safe to classify as dependency-related evidence. Additional sensitive patterns make the classification more conservative. They do not authorize mutation.

## Collection bounds

The watcher inspects every returned item up to each per-repository limit. These defaults are conservative operational budgets, not expected queue sizes. Large installations can raise them deliberately, but doing so can materially increase sequential GitHub API work.

A limit hit is surfaced as truncation, changes the report status to `partial`, and produces exit code `4`; the watcher never silently treats a bounded collection as complete.

The history lookback is time-bounded separately from the item ceiling. Changed files are fetched through GitHub's paginated pull-request-files endpoint and have their own ceiling. A PR that exceeds it becomes an explicit partial collection error rather than being assessed from an incomplete path list. GitHub returns at most 3,000 files for that endpoint, so configuration intentionally caps this value at 2,999 to preserve one additional record for truncation detection.

If recent merged-history collection is missing or truncated, open PRs cannot receive `baseline_candidate` because sensitive-change evidence is incomplete.
