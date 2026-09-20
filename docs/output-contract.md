# Output contract

`dependabot-watcher inspect` supports `text`, `json`, and `markdown`. All three represent the same inspection. None is written to disk by the command.

JSON is the automation contract. Its top-level fields are:

- `schema_version`: output schema version, currently `1`;
- `mode`: always `inspect` in the current release;
- `generated_at`: UTC timestamp;
- `status`: `complete` or `partial`;
- `capabilities`: explicit false values for model calls, local persistence, GitHub mutation, and notifications;
- `summary`: aggregate counts; and
- `repositories`: per-repository PR, alert, recent-sensitive-change, truncation, and error evidence.

Every inspected pull request includes collector-owned repository, PR, base, and head identity. `classification` is either `baseline_candidate`, `attention_required`, or `collection_error`. It is not an execution decision, and `mutation_authorized` is always false.

An open alert is called `likely_covered_by_open_pr` only when a verified Dependabot PR matches the exact package, ecosystem branch, manifest path, and alert's first patched version. A related PR without that complete evidence remains `related_open_pr_unverified` and still counts as unresolved. Open alerts possibly associated with a recent merge also remain in the unresolved count until GitHub closes them.

Errors and truncations remain in the report so scheduled wrappers and future agents can distinguish “nothing needs attention” from “inspection was incomplete.”
