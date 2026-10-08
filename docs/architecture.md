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
- `review_queue.py` owns scoped saved proposals, local handling/messages and context freshness; `review_actions.py` delegates exact consent, execution and uncertain-outcome recovery to the existing executor.
- `supervision.py`, `supervisor_runner.py` and `supervisor_events.py` own immutable job contexts, attempt/session lineage and checked completion receipts. `supervisor_permissions.py` owns live request identity and single-use decisions.
- `supervisor_workspace.py` verifies registered worktrees and selected instructions; `supervisor_launch.py` binds fresh collection and runtime settings to one configured job. The status and bridge modules expose bounded reads, launch frames and decisions without a listener.
- `supervisor_action.py` requires a current affirmative completed investigation, exact runtime/completion receipts and freshly verified workspace before supervised action readiness; the existing executor repeats that gate and persists agent attribution without treating intelligence as consent.
- `scheduled_supervisor.py` selects current reasoning work; `scheduled_triggers.py` owns single-use per-run reservations, workload slots and checked outcomes. Selection, submission and completion are separate evidence.
- `native_review.py` binds the installed backend to a deliberately selected optional Windows executable. `native/windows/src` supplies one aggregate WPF review window and tray; it does not own policy, jobs or action authority.

The GitHub collection and planning contracts are ecosystem-neutral wherever GitHub supplies normalized Dependabot evidence. Ecosystem adapters add bounded deterministic evidence; they do not replace the generic core or silently widen automatic-action eligibility. Provider adapters likewise translate one supported CLI or API transport into the stable advisory contract without owning repository policy, context selection, or action authority.

## GitHub access boundary

Authenticated access is a product requirement for private repositories and Dependabot security alerts; GitHub CLI is only the first implementation of that requirement. The current adapter delegates bounded repository, pull-request, and API reads to `gh`, plus the sole explicitly enabled exact-head PR merge call. GitHub CLI also supplies its supported browser, credential-store, or environment-token authentication. No IDE is required.

Editor authentication is intentionally outside this boundary. The watcher does not inspect or extract VS Code or extension credentials, because they belong to another application's private integration and are not a safe or durable credential API.

The `GitHubClient` protocol keeps collection and policy independent from GitHub CLI subprocess details. A later direct REST client, fine-grained-token adapter, OAuth/device flow, or GitHub App must preserve the same bounded reads, pagination, error visibility, secret-handling rules, and repository identity contract. Adding an adapter may reduce installation friction; it does not remove the need for explicit authorization.

## Safety invariants

- Within Watcher's GitHub adapter and executor, the sole mutation is an exact repository/PR/head-bound merge. A tool-capable supervisor is a separate access boundary: inherited, named restricted or full access does not establish isolation from ambient credentials, connectors or direct remote actions. The current adapter explicitly records that isolation as unestablished; prompts and a worktree do not supply it.
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
- Supervised execution additionally requires an exact current pending proposal and verified affirmative agent recommendation with explicit `ready_for_action = true`. Candidate prioritization alone, legacy results without readiness, missing, stale or conservative analysis cannot enable an action. Readiness is a model assessment, not consent or deterministic proof of compatibility. Runtime and completion receipts bind the selected account/access/workspace/model; action audit retains bounded job/session/context attribution. Policy is reread after this check and immediately before the remote call, but arbitrary concurrent same-user configuration writers are not atomically frozen.
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

### Planned supervisor access and approval boundary

This section separates development implementation from remaining operating acceptance. The configured app-server launcher, durable jobs/permissions, feedback/status bridge and optional Windows consumer exist. Ordinary setup saves explicit supervisor choices; the WSL `run` path submits the registered native owner for scheduled investigation. Complete installed/live-provider acceptance remains open. The metadata-advice adapter stays separate from the tool-capable maintenance engine. Python owns job/context journals, observations and exact-action authority, not the provider or desktop client.

The native confirmation path now follows a recorded successful merge and complete queue refresh with one configured investigation of the affected repository. Its saved context includes repository-scoped recent execution observations alongside refreshed PRs, alerts and prior feedback. Session continuity is the default for the same maintenance task: a new commit changes evidence, not conversation identity. Matching owned worktree/Git identity, trusted instructions and configured runtime policy permit the existing backend-owned session to resume while each job still binds fresh evidence. A bounded, provenance-checked pre-action assessment also preserves historical reasoning in the job input; old item identities never become current readiness or consent. Existing job deduplication and permission handling apply; disabled or blocked supervision does not silently acquire authority. The fixed action notice separates merge completion from assessment progress or failure. Reopening history does not replay inference. This continuation does not authorize another merge, publish repairs or synchronize developer branches; crash/reopen continuation and connected live acceptance remain separate verification requirements. Provider compaction and deliberate context retirement need their own acceptance; conversation growth alone does not justify silent replacement.

#### User-selected access

| Agent access | Launch requirement | User-visible consequence |
|---|---|---|
| Inherited | Preserve and verify the effective selected Codex settings, trusted instructions and command rules | Existing access and approval behavior apply; unexpected requests enter Watcher's review flow |
| Watcher-restricted | Explicitly select and verify scoped workspace/filesystem, network and tool capabilities | The agent works within the agreed maintenance scope; additional capability requires a visible decision |
| Full access | Require deliberate opt-in and honor managed constraints | Routine sandbox prompts can be avoided, but an agent with direct mutation credentials can act outside Watcher's guarded executor |

Access does not select the model, authorize private-evidence transmission, set a call budget or grant unattended repository mutations. Configure those separately and show the effective choices before starting. Do not modify global Codex configuration or persistent command rules to manufacture a successful launch. If a selected config preset, capability or required tool is unsupported, explain the limitation rather than silently substitute another profile, account or unrestricted mode.

The development configuration stores supervisor policy separately from metadata advice and defaults it off. Ordinary setup saves explicit provider, model, effort, account type, access, private-evidence acknowledgement, validation authority, timeout, per-run job limit and registered workspace/instruction paths. Full access requires its own acknowledgement. Each repository context includes only its selected workspace metadata; policy changes affect context freshness. Setup rejects ambiguous repository aliases and choices that would otherwise be discarded in inactive modes before authentication or writes. Saving these choices does not establish workspace ownership, harness integrity, effective runtime settings or operating readiness.

The development preflight separately verifies a registered POSIX/WSL linked worktree's owned top-level, GitHub origin, clean state and exact collected base, then fingerprints the explicitly selected trusted instruction files. Its backend-only receipt enters the same scoped queue/job/native context digest, not a model- or UI-selected path. Registration changes or a new fingerprint invalidate affected job and permission freshness; obsolete receipts remain historical rather than current. The private queue migrates existing handling and feedback without granting authority, and shared journal parsing rejects ambiguous duplicate keys before rewriting state. Receipt persistence is not live filesystem monitoring, Codex instruction interpretation or isolation from shared Git configuration/credentials; normal launch must perform its own fresh checks.

Optional workspace refresh is a separate saved capability. Initial clean,
detached registration uses the collected base or an unchanged, freshly reverified
backend workspace proof when refresh is enabled after base movement. A private ownership receipt permits
non-force movement to a later exact base only while identity, prior head and clean
state still match. Git hooks and configured filters are neutralized; developer
branches are not updated. Pending checkout intent distinguishes observed completion
from an operation needing recovery. Configured launches share a worktree-local OS
lease across collection and inference, including different Watcher state profiles.
This coordinates Watcher processes, not arbitrary same-user editors or Git tools.
Provisioning, conflict repair and selected developer-checkout integration remain
separate work.

Receipt-backed development jobs also check selected instruction fingerprints during the owned runtime: changed file/target metadata triggers a bounded recheck, and inference, grant transmission and result acceptance force fresh reads. Revocation interrupts the attempt and invalidates outstanding consent; a validated late observation remains historical rather than promoted. Provider launch settings and returned runtime must match the receipt and saved model/access policy. Receipt-bearing jobs cannot use the legacy non-eventful adapter or claim completion without a checked connection. These checkpoints do not freeze concurrent writers, watch every referenced document or undo already-started commands.

The configured backend launcher refreshes evidence, verifies the registered worktree, records its receipt and runs one repository job using only saved policy. It reuses current completed work and resumes only a matching backend-owned session; failed or interrupted work is not automatically replayed. Reconciliation serializes collectors separately from the shared journal lock and rejects changed run/execution state before commit, allowing feedback and permission decisions during remote collection. The launch route requires metadata advice off and, for inherited/restricted access, a caller-owned permission presentation and consumer. The native consumer invokes it from explicit feedback or the scheduled path; complete installed acceptance and workspace provisioning remain open. Advisory-enabled reconciliation retains its existing locked advice stage.

Conversation compatibility is narrower than exact job-policy equality: changes to model, reasoning effort, timeout, run budget, maintenance-refresh authorization and validation resource limits retain a compatible owned conversation while the new job applies and verifies its current settings. Explicit retry may also resume a compatible completed conversation without replaying the failed attempt or its permissions. Failed-only sessions, changed account/access/tool capabilities and changed workspace/instruction identity remain separate recovery boundaries; unsupported compatibility must not be inferred from history availability.

The isolated-validation increment under implementation separates the agent's trusted investigation worktree from disposable exact-PR source snapshots. Python reads raw Git tree/blob objects rather than attribute-sensitive exports, binds preparation and commands to job/proposal/head/context, and reports excluded credential paths and nonordinary entries. Recognized-path filtering is not a secret-content scan. Watcher's validation tools use an existing Linux/WSL Bubblewrap runtime without host-execution fallback; this does not isolate other tools available under the supervisor's chosen Codex permissions. The agent chooses checks and may install dependencies or write disposable probes under saved preparation policy; caches and hashed receipts persist separately. Content, identity, mode and kernel change-time checks flag tracked-source changes, including ordinary write/restore operations, but cannot prove that selected tests are adequate. Validation commands have bounded lifetime/output and monitored storage/free-space reserves, not kernel quotas. Opt-in validation networking includes host-network/local-service connectivity. Trusted backend revision acquisition separately uses the registered Git origin and normal host authentication. Additional reserve paths can check the physical volume backing a WSL virtual disk. Installed/live-provider acceptance and recovery remain unverified; repair publication and developer-branch synchronization remain separate capabilities.

Scheduled WSL supervision reuses one quiet registered Windows owner, not a new service. Every trigger refreshes the hidden queue and claims an exact saved run/configuration with complete configured-repository coverage. Python reserves bounded slots before inference and consumes each once; fresh collection cannot renew the run's budget. Duplicate or uncertain work is not replayed. Native results require independently saved provider completion, and notification opt-out applies to permission and result notices. Process submission is separately recorded before launch; missing final receipts remain uncertain and same-run resubmission is refused. Abandoned reservations still need deliberate operating recovery/retention; this path does not establish unattended mutation or support outside WSL.

Trusted harness preservation and untrusted-code execution are different boundaries. Verify actual instruction discovery in the owned workspace. A restricted validator must not inherit mutation credentials, unrelated developer files or service sockets merely because its shell directory is a worktree. MCP, connectors, web/browser tools and command traffic have distinct controls; a filesystem sandbox is not a universal remote-action boundary. Full-access operation needs honest weaker-enforcement claims, not a fictitious guarantee that prompts constrain all tools.

#### Checkpoints and permission requests

Failed or interrupted investigation has an explicit recovery action in the development window. Retry names one failed job in the exact current context, keeps that record unchanged and creates a separately recorded attempt after fresh preflight. Consumed retry requests cannot launch again; a failed retry needs a new deliberate choice. Changed context invalidates the old retry selection. This is investigation recovery, never replay of merge consent or an uncertain mutation.

The intended interaction is authorize investigation, receive recommendations, authorize identified actions, then receive outcomes and exceptions. Scope the first stage to configured repositories/workspaces and agreed exploration, research and validation. Scope later stages to the actual proposed changes and local integration; dependency-repair publication requires its own explicit contract. Routine operations inside a granted stage should not require repeated founder attention. A generic `gh` or `git` prefix grant does not distinguish reads from writes.

An aggregate action decision may cover an identified set, but each action still needs current eligibility, exact target/head binding and serialized execution. Changed scope, heads, material evidence or uncertain prior outcomes revoke the affected consent. The existing one-action executor is not already an implementation of batch approval or standing unattended authority.

Provider permission requests must retain job/repository/workspace, thread/turn/request/item identity and the exact requested access. Present supported decisions through the aggregate native window, including scoped grants where the provider implements them. Never turn a stage approval into blanket acceptance of arbitrary later requests or silently amend global rules. Persist waiting and resolution evidence; reject stale or mismatched responses. If a live provider connection disappears, pending requests must be invalidated or deliberately recovered, not assumed resumable just because conversation history persists. Window closure must not imply approval, rejection or successful work.

#### Provider lifecycle and acceptance

Generation also constrains recommendations to the current job's proposal IDs
and binds each reply to its actual proposal/question pair, including saved
historical feedback. Empty namespaces require empty arrays. Provider schema
limits are checked before opening a runtime; an oversized investigation is
refused rather than silently dropping identity constraints. Independent result
validation still rejects malformed, mismatched or duplicated attribution. A
failed provider result is not remapped to a guessed PR or promoted to completion.

Codex CLI is the first installed/authenticated host. The development integration combines a narrow local app-server stdio transport, checked access settings and the existing supervisor runner. Python owns the job/context/session receipt and permission journal; the provider translates runtime events and decisions. Requests remain pending independently of event reading, communication and lifetime are bounded, and POSIX process cleanup is owned; the Windows interface uses WSL. The native consumer has a scoped access-request panel; saving policy alone does not establish complete installed acceptance. The existing `exec` reasoning adapter remains separate.

The transport decision follows evaluation of Codex CLI and Python SDK 0.160.0. The SDK's default command/file approval handler accepts, its callback runs on the only event reader, and its request waits and event buffers do not provide this client's required bounds. A safe native approval wait would therefore need additional lifecycle/routing machinery around that SDK. The narrow stdio adapter avoids that coupling and an additional pinned runtime dependency; this is a fit decision for this client, not a universal SDK prohibition. Treat experimental permission fields as version-sensitive, and verify compatibility before extending execution. No browser, network listener, new agent platform or permanent worker is needed solely for approvals.

Before starting a turn, the trusted backend supplies the expected instruction-source chain and checks the returned chain, model, effort, workspace, session and effective permission settings. A source-path receipt is not proof of instruction content or command-rule enforcement; actual harness/rule checks remain part of operating acceptance. The current app-server route rejects named CLI config presets it cannot preserve rather than silently using base settings. Other providers and desktop/IDE-only installations can implement the portable job/feedback/action contracts later.

Permission decisions bind to the original request digest and exact job/context/session/turn. Persist the decision before acknowledgement and consume it once before responding through the owned live connection. Resolution is recorded separately; clearing an unanswered request cannot become successful delivery or job completion. A denied/cancelled general command cannot then be recorded as successfully executed. Managed network denial is destination-specific and does not forbid a command finishing through local/cached alternatives. Disconnect invalidates outstanding or uncertain requests; earlier resolved delivery stays historical even if later execution contradicts it. The current client offers allow-once, deny and cancel, not session-wide grants or persistent rule amendments. Legacy session-grant records remain readable but cannot be consumed anew. Effective-settings receipts retain only checked allowlisted fields, not arbitrary nested runtime configuration. Result generation uses a per-turn structured schema carrying fixed job/context identities, while backend validation independently checks returned recommendations and exact saved-message replies.

The development bridge has a context-bound configured-launch operation with bounded JSON-line lifecycle and permission-preview frames. A request enters the durable journal before its exact preview is sent; replies still use the independent checked decision endpoint. Sensitive command/destination/filesystem details stay transient, while journals retain generic descriptions and exact digests. Local command/network and explicit legacy filesystem overlays are supported; terminal input, remote environments and newer entry/glob overlays remain refused until a complete presentation exists. Nonreading or disconnected clients cannot fall back to a blocking ordinary-response writer. Owned consumer loss is checked before launch and during transport polling, including quiet access waits; this proves reader presence, not user visibility or command rollback.

The development native consumer now streams those frames independently of user decisions and journal reads. Saved launch-policy disclosure is bound to the current context but is not runtime attestation. An explicit repository investigation includes saved messages and cannot supply launch overrides or merge consent. The window matches every transient preview to the independent live permission journal, including job/context/connection/session/turn/item/request/digest and choices, before offering grants. Feedback changes context; outdated consent is refused and results remain historical. Completion requires an independently saved job/result receipt. Hiding preserves the owned investigation; Exit and UI-error shutdown stop new requests, end the investigation and drain all owned backend exchanges before exiting. Approved merges finish their separate outcome-recording lifecycle rather than being killed or replayed. Cancellation does not undo already-started commands. Untrusted display text visibly escapes control/format characters without rewriting original messages or context. Repository-owned native source is under `native/windows/src`. These are development-path contracts with controlled-provider Windows/WSL evidence; ordinary setup/scheduling, live-provider and installed acceptance remain open.

The WSL `review` entry deliberately selects an optional Windows component and saves fingerprints of both its executable launcher and sibling application DLL. Self-contained .NET launchers may remain unchanged across application-code updates, so an EXE-only identity is insufficient. Registration schema 2 and launch schema 3 bind both files; legacy registrations require deliberate reselection and old immutable profiles are retained. The profile also binds the installed interpreter, module, configuration, state, distribution and user. Native intake independently hashes the running launcher and loaded sibling assembly, checks the exact command/profile bytes once and retains that client. Python isolated mode prevents current-directory or Python-path package substitution. Changed application code gets a distinct profile/owner, not silent activation of an older binary. This is file identity binding, not signing or authenticity of every bundled dependency; submission alone does not prove visibility.

Ordinary startup has no diagnostic fallback. Synthetic window/transport controls require explicit diagnostic entry; rejected normal startup cannot choose an error-log destination or write events beside installed binaries. Bound normal events live under the private profile, and exit logging is best-effort. Installed-wheel read-only queue, feedback, decisions and reopen/restart plumbing have real Windows/WSL evidence. That narrower acceptance does not establish component distribution/setup, cold notification activation, scheduled supervision or real-action readiness.

The development notification link uses a separate private Windows current-user
lookup from opaque profile ID to an immutable normal launch file. SDK activation
rechecks the installed-command/profile bytes and current launcher/application fingerprints
before opening its queue. A running window keeps its original backend; clicks
for other profiles enter their separately owned normal launch path. Toast content
comes from a bounded backend read of the exact saved current run, and open intent
is never action or tool consent. Explicit notification test/status/remove controls
are separate from production reminder state. This adds no listener or permanent
worker; actual cold/warm SDK activation remains an operating proof requirement,
not a conclusion from parsing tests. Dependency authenticity, signing and
distribution remain release boundaries beyond application-file fingerprinting.

The existing backend holds one per-job OS owner lock before publishing a running attempt, including provider startup and the legacy adapter. Normal completion, failure or uncertain exit releases it; a process crash releases it through the operating system. Status, preparation and reads mark unowned running attempts interrupted, while permission recovery invalidates outstanding consent. The lock proves backend ownership, not remote-model responsiveness; persisted history alone proves neither. Short journal transactions remain separate from this lifetime lock, and no heartbeat worker or additional service is required.

Acceptance requires actual selected-mode execution and observed denials/grants, not configuration readback alone. Exercise both completion and failure directions: scoped reads/writes, inherited rules, full-access disclosure, delayed permission decisions, decline/cancel, stale requests, interrupted processes, resumed feedback, effective model/account provenance and fresh post-action observation. Compare authorized models on matched evidence without claiming general model rankings or using routine script checks as proof of intelligent investigation. Installed scheduled use and real supervised action remain separate gates from development-path diagnostics.
