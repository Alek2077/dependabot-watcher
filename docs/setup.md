# Setup and diagnostics

The default first-use path remains read-only and model-off. Phase 6 adds a separate explicit setup option for approval-gated execution; scheduling, notifications, silent model use, and unattended mutation remain unavailable.

## GitHub access and authentication

GitHub authorization is unavoidable for private repositories and Dependabot security alerts. The current implementation uses GitHub CLI as its first GitHub transport and credential broker: the adapter issues bounded `gh pr`, `gh repo`, and `gh api` reads while the core depends on a transport-neutral `GitHubClient` protocol.

GitHub CLI does not require an IDE. Its browser login, credential store, and supported token environment variables are independent from VS Code or another editor. The watcher deliberately does not inspect, extract, or reuse editor-managed credentials; those credentials are scoped to another application and are not a stable public integration contract.

The default product path issues read-only GitHub requests. The sole mutating edge is the explicitly enabled exact-head PR merge in `execute`; a user's existing `gh` credential may carry broader authority than either path needs. Before public release, document and validate least-privilege credential profiles for the selected repositories, required read surfaces, and the optional merge capability. Future GitHub adapters may support a directly supplied fine-grained token, OAuth/device flow, or a GitHub App when their added complexity is justified. These adapters can remove the hard GitHub CLI dependency, but not the underlying authorization requirement.

## Recommended setup

```bash
dependabot-watcher setup --repo owner/repository
```

`owner/repository` denotes the GitHub repository identifier and must be replaced with a real value. It is example syntax, not a built-in demonstration repository.

The command checks GitHub CLI authentication, verifies read-only repository and configured-branch access, discovers default branches, creates a non-secret operational profile, and prints the exact `doctor` and `plan` commands to run next.

The generated profile is deliberately conservative: advisory mode `off`, autonomy `read_only`, mutation disabled, concise human reporting, complete JSON machine output, and no scheduler or notification configuration.

Subsequent commands load this profile automatically from the platform configuration path. Setup flags are initial choices, not arguments users or agents must repeat for every run.

Setup publishes the complete file atomically and refuses to overwrite an existing path. Move or edit an existing configuration deliberately rather than relying on a destructive force flag.

For an environment where GitHub cannot be reached during setup, `--offline` creates an explicitly unverified profile. Supply branch overrides as `--branch owner/repository=branch`; otherwise offline setup uses `main` as an assumption that `doctor` must later verify.

## Advanced model setup

Provider use is independent from GitHub action authority. Enabling advice requires explicit mode, model, reasoning, and acknowledgement:

```bash
dependabot-watcher setup \
  --repo owner/repository \
  --advisory-mode reviewed \
  --triage-model TRIAGE_MODEL \
  --triage-reasoning high \
  --review-model REVIEW_MODEL \
  --review-reasoning max \
  --acknowledge-model-use
```

Setup checks Codex CLI availability and authentication but never invokes a model. The acknowledgement records consent to later quota/cost consumption; it does not authorize GitHub mutation. Provider credentials remain in the provider's supported authentication store and never enter the Watcher profile.

## Approval-gated execution setup

Use the execution option only after verifying the repository's real merge method and exact required-check names. A read-only `inspect --verbosity standard` or `plan --format json` report exposes observed names; confirm them against the repository's branch protection or ruleset rather than guessing:

```bash
dependabot-watcher setup \
  --repo owner/repository \
  --autonomy approval_required \
  --merge-method owner/repository=squash \
  --required-check owner/repository=CI \
  --acknowledge-mutation
```

Repeat `--required-check` for every check the watcher must observe as explicitly successful. Names match exactly and case-sensitively, and ambiguous duplicate names fail closed. A qualifying result is either a completed check run with `conclusion = SUCCESS` or a legacy commit-status context with `state = SUCCESS`; `NEUTRAL`, `SKIPPED`, pending, missing, malformed, unknown, and failing evidence does not satisfy an execution gate. For multiple repositories, supply repository-qualified merge/check settings for each. Check names containing spaces should be shell-quoted.

This setup call still performs no mutation. It records that later `execute` calls may request approval. Each action separately requires a complete saved JSON plan, a read-only live execution preflight, and the exact approval confirmation token bound to that current repository, PR, base, head SHA, and merge method. The deterministic token is a two-invocation confirmation value, not a secret, signature, separate approver identity, or proof of human interaction. Setup refuses to infer mutation consent from model/provider choices.

## Doctor

```bash
dependabot-watcher doctor
dependabot-watcher doctor --format json
dependabot-watcher doctor --offline
```

`doctor` reports OS/Python runtime, CLI readiness, configuration/profile status, read-only repository and configured-branch access, Dependabot security-alert read access, default-branch drift, detected Dependabot ecosystems, and support depth. Its bounded alert probe retrieves at most one open alert and retains only the readiness state. It recognizes both `.github/dependabot.yml` and `.github/dependabot.yaml`; missing, inaccessible, malformed, oversized, or structurally excessive configuration remains visible as an attention state rather than being treated as ready.

`doctor` discards raw authentication output. It never shows tokens, reads secret files, invokes a model, writes state, mutates GitHub, sends notifications, or creates schedules. Provider authentication is not probed when advisory mode is off. `--offline` still checks local executable versions but skips authentication and repository calls, so its result remains `attention` rather than claiming verified readiness.

The current deeper adapter is limited to bounded Python requirements inclusion evidence. Every detected Dependabot ecosystem still receives generic GitHub-based inspection and planning. Approval-gated execution is based on generic exact PR/check/path evidence rather than ecosystem-specific compatibility claims; no ecosystem is eligible for automatic execution.

## Operational profile

The versioned configuration is also the first operational profile. It stores stable non-secret choices and provenance rather than creating a second source of truth:

```toml
[operation]
autonomy = "read_only"
mutation_enabled = false
report_verbosity = "concise"
machine_output = "json"

[profile]
source = "setup_quick"
created_at = "2026-09-22T12:00:00Z"
confirmed_at = "2026-09-22T12:00:00Z"
last_verified_at = "2026-09-22T12:00:00Z"
```

Credentials, tokens, cookies, raw auth responses, environment-file contents, and private model output are forbidden profile content. Future feedback may propose profile changes, but it may not silently expand repository scope, spending, mutation authority, or policy.
