# Installed-path validation workflow

Use this workflow proportionately after a material feature, onboarding change, packaging change, or user-facing CLI revision. It complements deterministic tests and independent review; it does not replace either.

The objective is to exercise the product as a user or supervising agent receives it, detect friction that source-level tests cannot reveal, and preserve evidence without exposing credentials or private repository data.

For development-to-delivery boundaries, follow [development, release and dogfooding](development-and-release.md). This file contains procedures; [validation.md](validation.md) contains historical evidence, not additional mandatory steps. Read the evidence relevant to the capability under test rather than treating its entire history as required context.

## Isolated acceptance versus ongoing dogfooding

The temporary-profile and cleanup rules below apply to isolated acceptance exercises. A continuing dogfooding installation is deliberately persistent: preserve its real configuration, state, scheduler and user/agent workspace across updates. Never apply disposable-test cleanup to it. Use the documented update path against an independently acquired reviewed remote revision or an actually published package; do not replace it with development binaries without an explicitly approved exception.

A user may continue a usability trial without repeating onboarding. This does not waive the applicable fresh-install or fresh-session checkpoints below; run those separately with isolated profiles rather than resetting the continuing trial. Test preservation and continuation when updating an existing installation. Record developer assistance rather than silently substituting it for user-path acquisition or updating. Pre-merge isolated artifact tests remain valid development evidence, but cannot replace the post-merge user-path update exercise.

Real-world acceptance tests must use the documented product workflow and the agent client's ordinary permission flow. When a run encounters friction, preserve that result and investigate it before changing the environment or coaching the agent. Do not pre-enable broad permissions, supply development-only workarounds, or otherwise bypass the behavior being evaluated to obtain a passing run. A narrowly authorized diagnostic retry is useful evidence, but record the assistance and do not count it as unassisted onboarding success. After correcting the product or guidance, use a fresh session to verify the normal path.

Run the applicable deterministic, installed-path, and review checks before merge by default. Maintainer hands-on testing is optional for ordinary changes rather than a mandatory release tax. Recommend a hands-on checkpoint when the change materially affects first-use onboarding, authorization, mutation, autonomy policy, destructive/recovery behavior, or public-release experience, or when direct product feedback is needed before merge.

For a material capability that does not yet exist on remote `main`, use two distinct stages rather than treating one as a substitute for the other:

1. **Before merge:** build the current artifact and install that artifact in an isolated temporary environment. Exercise the complete applicable command path through read-only/live preflight. Do not install directly from the active development checkout and call that installed-path validation.
2. **After merge:** acquire a new clone from the remote, check out the exact reviewed merged commit (or a reviewed tag verified to resolve to it), and verify the SHA and clean state before installing through the documented user path. Do not substitute the moving branch tip for the selected revision. Repeat the end-to-end workflow from repository acquisition onward. When the milestone includes an explicitly approved real mutation, perform that one exact action only from this fresh-clone installation, then verify audit, reinspection, idempotent retry/reconciliation, and cleanup.

The post-merge fresh-clone exercise is part of development validation, not a task that must be assigned to the user. A real mutation still requires separate authorization for the exact repository, PR, and head SHA.

## Critical supervisor context handoffs

When changing supervisor prompts, context construction, conversation reuse, action continuation or completion handoffs, run an applicable real-provider acceptance exercise as well as deterministic tests. Verify the actual submitted context and instruction sections, effective instruction-source readback, relevant referenced-file reads, accepted structured result and downstream decision at the affected stage. Cover fresh and resumed work, changed/missing guidance and recipient/revision binding where relevant; do not infer delivery or comprehension from a mocked provider, generated prompt string or model self-report alone. Preserve failed attempts and distinguish transport delivery, observed interpretation and deterministic enforcement. Start with the [opt-in synthetic context exercise](../tests/manual/README.md); add a bounded scenario for critical paths it does not exercise rather than generalizing its pass to the entire workflow. Real intelligence reduces errors only when supplied appropriate current context; it does not remove the need for exact-action safeguards.

## Safety boundary

- Use a temporary clean checkout or built distribution, not the active development import path.
- Install through the documented user path in an isolated tool environment.
- Point `DEPENDABOT_WATCHER_CONFIG` at a file inside the temporary validation directory, or pass that same temporary `--config` path to every applicable command. Do not let validation create or reuse the operator's real default profile.
- Prefer a synthetic fixture or approved read-only live target. Never broaden repository scope or authorize mutation merely to complete validation.
- For live Dependabot dogfood, choose operator-approved repositories with enabled Dependabot and usable PR/alert evidence for the workflow being tested. Acquiring and installing Watcher from its own repository is separate from choosing a maintenance target; self-dogfooding is appropriate when Watcher's own repository has relevant evidence and the product is ready for that exercise. Prefer inactive, clean developer checkouts when local synchronization is in scope; a dirty checkout is not automatically excluded, but its unrelated work must be preserved through an isolated maintenance copy and deliberate integration policy. A target whose alert collection is unavailable may document a negative case, but cannot be the sole target for declaring the end-to-end pass complete.
- Do not inspect secret files, print credentials, or capture screenshots containing tokens or unnecessary private data.
- Remove temporary checkouts, tool installations, test profiles, screenshots, and other state after evidence has been recorded, unless the operator explicitly keeps them for a follow-up test.

## Canonical local release-candidate build

The release workflow selects Python 3.14 for its Python build; CI also tests the supported runtime matrix. Local release-candidate validation must select its build context explicitly rather than inheriting whichever virtual environment happens to be active. Python 3.14 is the maintainer release-build environment; Watcher's supported user runtime remains Python 3.11 through 3.14.

Before treating artifacts as a release candidate, verify that the checkout is at the intended merged commit and clean. Then create a fresh external output directory and build with a stable Python 3.14 selected by `uv`:

```bash
candidate_dir="$(mktemp -d /tmp/dependabot-watcher-candidate.XXXXXX)"
uv run --no-project --python 3.14 --with build --with twine \
  python -c 'import sys; print(sys.version); print(sys.executable)'
uv run --no-project --python 3.14 --with build --with twine \
  python -m build --outdir "$candidate_dir"
uv run --no-project --python 3.14 --with build --with twine \
  python -m twine check --strict "$candidate_dir"/*
```

Record the resolved interpreter, commit, artifact paths, and hashes. Install the wheel and sdist separately into clean Python 3.14 environments for installed-path smoke checks. If a build accidentally uses an ambient interpreter or unintended virtual environment, mark those artifacts superseded and rebuild; a successful build alone does not make them release authority.

## Validation sequence

1. Before handing a tester or independent agent a fresh-install test, verify the exact old checkout, installed tool, configuration, and test-state locations that could contaminate it. Remove only disposable prior-test state whose ownership and purpose are established; otherwise isolate the new run under fresh tool, configuration, state, and checkout paths. State which boundary was verified so the tester does not have to rediscover it.
2. Run the deterministic suite, static analysis, compilation, packaging, and documentation checks appropriate to the changed surface.
3. Build the current artifact, then install that artifact through the documented path in a clean environment. Confirm the executable and version resolve from the installed environment rather than the development checkout. Installing `.` directly from the active checkout is a development smoke, not sufficient installed-artifact evidence for a material phase.
4. Exercise `--help`, `setup`, `doctor`, `inspect`, and `plan` when the change could affect the end-to-end path. For multi-repository behavior, use more than one approved live read-only target when available rather than treating deterministic multi-repository fixtures as complete installed-path proof. Keep setup and all later commands bound to the isolated temporary configuration. Exercise `advise` only with an explicitly approved provider configuration and usage boundary. For execution changes, save JSON plan output and exercise the no-token live preflight; do not supply an approval token or enable real mutation without separate authorization for the exact repository, PR, and head SHA.
5. Repeat the specific changed workflow using representative success, partial-evidence, and failure inputs where practical.
6. Verify structured output separately. JSON must remain parseable and complete, stdout must contain only the selected report, and transient progress or diagnostics must not contaminate pipes or machine consumers.
7. When onboarding or agent usability changed, repeat the task in a fresh minimally briefed agent session using only user-facing repository documentation, without maintainer-only context. A private checkpoint may use authorized repository access; the anonymous post-visibility checkpoint must use the public surface. Require the agent to explain its plan and safety boundary before installation or mutation.
8. Record material findings, fixes, accepted limitations, commands, and cleanup. Remove the temporary profile as well as the checkout and tool environment. Keep private details in private evidence surfaces; publish only sanitized, reproducible validation evidence.

For an explicitly approved mutation exercise, capture the before state, saved plan identity, preflight binding, exact approved head, configured merge/check policy, audit events, GitHub result, after-state reinspection, idempotent retry behavior, and recovery outcome. Stop after one bounded action. A general implementation or test request never substitutes for this exact mutation approval.

## Human and agent experience checks

Judge the workflow from both perspectives rather than only checking for a zero exit code:

- Is the product's current capability and limitation clear before installation?
- Are prerequisites, authentication needs, cost/model use, repository scope, and mutation authority explicit?
- Does every command make its effect, evidence source, and next useful step clear?
- During live reads, is there enough progress feedback to distinguish work from a stalled command without polluting non-interactive output?
- Are warnings and partial results visible and actionable?
- Do concise outputs remain scannable while making richer human detail and complete machine evidence discoverable?
- Are internal states translated into understandable human language without weakening the stable JSON contract?
- Does setup reuse saved choices predictably, and can the user locate, edit, replace, or remove the configuration?
- Does the result materially help complete the Dependabot workflow, rather than merely restating counts already available from GitHub?
- Can an agent consume the output without private development history or repeated environment rediscovery?

Capture screenshots only when visual hierarchy, progress behavior, terminal rendering, or confusing presentation is itself under review. Inspect and sanitize them before retention.

## External comparison

When installation or terminal experience changes materially, compare the relevant interaction with a small current sample of maintained open-source command-line tools serving a similar audience. Use current primary repositories or documentation, inspect only the comparable surface, and treat the result as product evidence rather than a style mandate. Do not install unrelated tools unless direct execution is necessary to answer a material question; use temporary isolation and remove them afterward.

Adopt patterns only when they improve clarity, safety, portability, or effort for this product. Do not add decoration, dependencies, installation channels, or provider-specific behavior merely because another project uses them.

### Phase 7 presentation and usability benchmark

Assign one agent a bounded presentation/usability pass before settling Phase 7 terminal work. The pass should examine a small, current set of popular maintained tools: prioritize adjacent GitHub, maintainer, dependency, and agent-oriented CLIs, then include one or two broader CLI exemplars when they offer a materially relevant interaction. Previously discussed candidates such as GitCrawl, OpenClaw, Codex CLI, Claude Code, or CodexBar are starting points rather than a permanent required list; verify that each comparison remains current and relevant.

For each selected tool, inspect the first README screen, prerequisites, installation path, authentication explanation, first-run/setup flow, progress and waiting states, success/warning/error hierarchy, output density, configuration discoverability, and uninstall/reset guidance. Use its current primary repository or official documentation first. Install and exercise it only when static evidence cannot answer the relevant presentation question, and then use a disposable environment, record the tested version and date, avoid granting unnecessary account access, and remove it afterward.

Capture sanitized screenshots when visual hierarchy, animation, terminal behavior, or first-run flow is material. Produce a concise comparison that records what each tool does well, where Dependabot Watcher creates more friction, and which patterns should be adopted, adapted, deferred, or rejected with reasons. The goal is current usability evidence and a professional, modern experience—not imitation, decorative churn, or a sprawling competitive survey.

### Phase 8 agent-surface compatibility matrix

Validate agent integrations as thin operators over the installed CLI rather than as separate implementations of product policy. Start by testing repository documentation and a concise agent runbook without a skill. Prototype a project-owned skill/plugin only when it has a plausible discovery, consistency, or safety benefit, then compare the same tasks with and without it. If the package does not materially improve the result, do not publish it. If created, it may package instructions, workflows, and supported connection metadata, but it must not duplicate candidate classification, planning, authorization, or recovery logic that belongs in the product. Use the reviewed external landscape in [agent-integration-strategy.md](agent-integration-strategy.md) as comparison evidence; reuse only conventions whose provenance, license, authority, maintenance, and safety are acceptable.

Record three independent support levels for every evaluated surface:

- **operator:** can install or locate the CLI, explain its effects, run the approved workflow, and interpret structured results;
- **packaged integration:** when one is justified and published, can discover and correctly apply the project-owned skill/plugin; and
- **advisory provider:** can be invoked by the watcher itself through a tested provider adapter.

For the first support matrix:

- fully exercise Codex CLI as operator and advisory provider;
- fully exercise Codex in the ChatGPT desktop app as an operator using the portable integration, then smoke-test the Codex IDE extension for materially different configuration, permission, path, or command behavior;
- fully exercise Claude Code CLI and Claude Desktop as operators using portable instructions or a compatible plugin, then smoke-test the Claude Code IDE extension for materially different behavior; and
- list GitHub Copilot, OpenCode, Antigravity, and Hermes as unverified, experimental, or community-compatible until an explicit validation pass supports a stronger claim.

Do not require a maintainer or user to install every desktop app or editor extension. Agent-owned isolated validation is the default where available. Request hands-on testing when an available local surface is a primary supported path or when permissions, visual behavior, authentication, or operating-system integration cannot be evaluated credibly another way. Record the exact client, version, operating system, installation source, inherited instructions, permissions, and whether the test exercised only operator behavior or also a provider adapter.

Test at least: fresh discovery, installation proposal, setup explanation, model/privacy/cost disclosure, read-only operation, plan interpretation, approval-bound execution explanation, non-interactive output, interruption/failure behavior, and cleanup. A model or client being able to run shell commands is necessary but not sufficient for a first-class claim; it must preserve the product's scope, evidence, and authority boundaries without relying on private development history.

## Multi-agent onboarding checkpoints

Repeat agent-mediated onboarding at milestone boundaries rather than after every change:

- After Phase 6, or after another material installation/setup/authorization change, run a private authenticated checkpoint. Label it accurately: existing read-only GitHub credentials and global harness context mean it tests documentation comprehension and agent-mediated setup, not anonymous public adoption.
- During Phase 10, repeat from an isolated directory or environment with no development checkout or project-specific harness context. Immediately after the repository becomes publicly readable—and before announcing or promoting a release—repeat anonymously without existing GitHub authorization. Verify clone, install, setup explanation, safe validation, reset, and uninstall from the public surface users actually receive.

Do not repeat the full multi-agent checkpoint for presentation-only corrections. After the completed Phase 6 checkpoint, the next deliberate private checkpoint should follow material Phase 8 agent-integration work or Phase 9 scheduled-operation work, whichever first changes onboarding, authority, or recurring operation enough to justify it.

Choose the prompting style to match the question being tested. A controlled capability comparison may use the detailed common prompt below so every agent is judged against the same explicit rubric. A natural adoption test should instead begin with short, plausible user language and reveal only information that an ordinary user would supply as the conversation requires it. Do not preload the expected commands, authorization boundaries, model/privacy disclosures, scheduling decisions, recommended defaults, or stopping points; those are part of what the agent and public product surface must discover and explain. Do not manufacture total ignorance either: a developer who already uses Dependabot may reasonably know repository names, authentication state, and their desired outcome without knowing Watcher's architecture or command flow.

Preserve the first attempt, including mistaken assumptions, permission prompts, failed public access, overly broad exploration, excessive output, and ordinary user corrections. When a missing fact blocks progress, answer minimally and naturally rather than replacing the agent's reasoning with an operator runbook. If the user must constrain an uncomfortable or inappropriate action, record that assistance instead of restarting merely to produce a cleaner transcript. Judge the completed session against the same capability, cost, authority, limitation, installation, recurring-operation, and cleanup rubric afterward. This keeps the interaction realistic without treating one model's poor judgment as a product defect or coaching the model into a predetermined success.

For each applicable checkpoint, begin with a fresh capable agent on an available target surface. When testing cross-harness portability or investigating conflicting results, compare independent sessions on other relevant available harnesses using the same self-contained prompt; no particular provider roster is required. Record unavailable surfaces as coverage gaps and do not promote their support claims without evidence. Do not show agents one another's answers. Keep the first pass read-only and require each agent to explain current capability, automation limits, mutation authority, model/cost behavior, GitHub authorization, installation, validation, and unclear documentation before it changes anything.

The person or agent conducting validation compares any collected responses for factual agreement, safety interpretation, installation sequence, and recurring friction. Choose only one agent to perform the actual installation so parallel installers do not contaminate the environment. Stop when the applicable acceptance question is answered; agreement alone is not proof of an untested support path. If responses differ, determine whether the cause is ambiguous documentation, harness contamination, or weak agent performance before changing the product. Do not expand into a large model leaderboard unless a concrete disagreement requires more evidence.

For a controlled capability comparison only—not for a natural-adoption test—use
this common first-pass prompt, changing only the repository URL and the
private-access paragraph when necessary:

> Treat REPOSITORY_URL as a third-party repository I am considering adopting. I have repositories with Dependabot pull requests and security alerts and want to reduce the manual maintenance work.
>
> The repository is currently private. You are authorized to use the GitHub CLI authentication already available in this environment solely for read-only access to this repository and to create one isolated clean clone. Do not use an existing development checkout, change authentication, inspect unrelated repositories, or make any remote changes. Report the exact commit SHA reviewed and leave the clone unchanged.
>
> Do not rely on prior knowledge about this project or its development history. Review only the repository documentation and implementation as needed.
>
> Tell me:
>
> 1. What does it do today?
> 2. Can it currently automate my Dependabot maintenance?
> 3. What can and cannot it change?
> 4. Does it require an AI model, incur model costs, or require GitHub authorization?
> 5. What approval requirements, limitations, and safety boundaries should I understand?
> 6. How would you install and validate it safely from a fresh environment?
> 7. What would I need to do before allowing it to perform one real Dependabot action?
>
> Flag anything unclear, contradictory, unsafe, misleading, or unnecessarily difficult in its onboarding or documented workflow. Do not invent problems merely to find criticism.
>
> This is a read-only assessment. Do not install the package, create configuration, invoke a model through the product, execute a plan, authenticate or reauthenticate anything, or make local changes beyond the isolated clean clone. Do not perform any GitHub mutation. First provide your assessment and proposed installation/validation steps for my approval.

For a public or genuinely anonymous checkpoint, remove the private-access paragraph rather than authorizing existing credentials. Record private authenticated evidence separately from anonymous public adoption evidence.

When recording a response, preserve the model/provider, reasoning setting when known, agent harness, commit reviewed, inherited instruction context that may have influenced the result, whether authentication or a clone was authorized, and whether any changes occurred. This provenance helps distinguish a documentation defect from harness-assisted understanding without turning the exercise into a model leaderboard.

### Phase 9 advisory, scheduling, and platform validation

Close the real-provider gap before treating model-assisted scheduling as validated. One clear owner authorization may cover both installed-path, non-mutating Codex `economy` and `reviewed` exercises when it names the approved repositories or fixture, transmitted evidence categories, configured models/reasoning, and call or usage boundary. This is a meaningful privacy and quota decision, not a requirement for repeated ceremonial confirmation. Record call counts, provider usage, schema validation, advice/deferred coverage, failure behavior, and confirmation that no model result authorized or performed a mutation. If no live candidate qualifies for reviewed second-stage analysis, use a production-shaped local fixture with a real provider call and do not alter a live repository to force eligibility.

Validate the scheduler-independent reconciler separately from platform registration. Deterministic tests and isolated installed runs must cover unchanged/no-work suppression, stable revision deduplication, concurrent invocation, overdue catch-up, bounded history and retention, plan supersession, provider failure, notification silence/actionability, interruption, and recovery. Re-running unchanged evidence must not repeat model calls, notifications, or mutations.

Validate the schedule lifecycle as product behavior rather than only checking generated files. Preview must be non-mutating and show the exact command, user/authority, paths, cadence, missed-run behavior, concurrency policy, and output destination. Install/update must be idempotent, replace an obsolete Watcher registration without leaving another authority/cadence variant behind, and never collide with an unrelated native job. Status must read the native definition back and distinguish installed, active, disabled, missing, and drifted registrations. Remove must delete only the exact Watcher-owned registration and preserve product configuration, run history, and audit data unless a separate explicit cleanup action is requested. Default registration must not require administrator/root authority when the supported same-user path is sufficient.

For WSL2, install through the normal user path and register an ordinary Windows Task Scheduler task that invokes the exact WSL distribution, originating Linux user, and installed Watcher console executable through trusted System32 helpers. Verify operation with no WSL terminal open, a missed start followed by host availability, one catch-up reconciliation rather than interval replay, logs/output/state in documented locations, safe disable/removal, and no reliance on an internal daemon or WSL systemd instance remaining alive.

For native Windows, first prove private configuration/state creation, file validation, locking, atomic replacement, interruption recovery, and path handling under native Windows semantics; only then count Task Scheduler registration as product evidence. Exercise installation, setup, live read-only collection, scheduling, missed-run behavior, notification routing, update, disable, uninstall, and cleanup from native Windows rather than through WSL.

For native Linux and macOS, apply the same reconciler contract through systemd user timers and `launchd`. Configuration syntax or static tests alone do not establish platform support. Before a public support claim, record a real installed run, wake-up, missed-run/catch-up, failure, disable/uninstall, and cleanup exercise on the named operating system and version.

After Phase 9, repeat the broader minimally scripted adoption checkpoint from a fresh installation through scheduled read/advice/plan operation. Focused command-bounded tests establish safety contracts but do not establish that an outside agent can discover, configure, and operate the complete product unaided.
