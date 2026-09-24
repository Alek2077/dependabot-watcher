# Installed-path validation workflow

Use this workflow proportionately after a material feature, onboarding change, packaging change, or user-facing CLI revision. It complements deterministic tests and independent review; it does not replace either.

The objective is to exercise the product as a user or supervising agent receives it, detect friction that source-level tests cannot reveal, and preserve evidence without exposing credentials or private repository data.

Agents should run the applicable deterministic, installed-path, and review checks before Gitflow by default. Founder hands-on testing is optional for ordinary changes rather than a mandatory release tax. Request or recommend a founder checkpoint when the change materially affects first-use onboarding, authorization, mutation, autonomy policy, destructive/recovery behavior, or public-release experience, and whenever the founder wants direct product feedback before Gitflow.

For a material capability that does not yet exist on remote `main`, use two distinct stages rather than treating one as a substitute for the other:

1. **Before Gitflow:** build the current artifact and install that artifact in an isolated temporary environment. Exercise the complete applicable command path through read-only/live preflight. Do not install directly from the active development checkout and call that installed-path validation.
2. **After Gitflow:** make a new clone from the merged remote branch, install through the documented user path, and repeat the end-to-end workflow from repository acquisition onward. When the milestone includes an explicitly approved real mutation, perform that one exact action only from this fresh-clone installation, then verify audit, reinspection, idempotent retry/reconciliation, and cleanup.

The post-Gitflow fresh-clone exercise is agent-owned validation, not optional founder manual testing. A real mutation still requires separate authorization for the exact repository, PR, and head SHA.

## Safety boundary

- Use a temporary clean checkout or built distribution, not the active development import path.
- Install through the documented user path in an isolated tool environment.
- Point `DEPENDABOT_WATCHER_CONFIG` at a file inside the temporary validation directory, or pass that same temporary `--config` path to every applicable command. Do not let validation create or reuse the operator's real default profile.
- Prefer a synthetic fixture or approved read-only live target. Never broaden repository scope or authorize mutation merely to complete validation.
- Do not inspect secret files, print credentials, or capture screenshots containing tokens or unnecessary private data.
- Remove temporary checkouts, tool installations, test profiles, screenshots, and other state after evidence has been recorded, unless the operator explicitly keeps them for a follow-up test.

## Validation sequence

1. Run the deterministic suite, static analysis, compilation, packaging, and documentation checks appropriate to the changed surface.
2. Build the current artifact, then install that artifact through the documented path in a clean environment. Confirm the executable and version resolve from the installed environment rather than the development checkout. Installing `.` directly from the active checkout is a development smoke, not sufficient installed-artifact evidence for a material phase.
3. Exercise `--help`, `setup`, `doctor`, `inspect`, and `plan` when the change could affect the end-to-end path. Keep setup and all later commands bound to the isolated temporary configuration. Exercise `advise` only with an explicitly approved provider configuration and usage boundary. For execution changes, save JSON plan output and exercise the no-token live preflight; do not supply an approval token or enable real mutation without separate authorization for the exact repository, PR, and head SHA.
4. Repeat the specific changed workflow using representative success, partial-evidence, and failure inputs where practical.
5. Verify structured output separately. JSON must remain parseable and complete, stdout must contain only the selected report, and transient progress or diagnostics must not contaminate pipes or machine consumers.
6. When onboarding or agent usability changed, repeat the task in a fresh minimally briefed agent session using only public repository documentation. Require the agent to explain its plan and safety boundary before installation or mutation.
7. Record material findings, fixes, accepted limitations, commands, and cleanup. Remove the temporary profile as well as the checkout and tool environment. Keep private details in private evidence surfaces; publish only sanitized, reproducible validation evidence.

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

## Multi-agent onboarding checkpoints

Repeat agent-mediated onboarding at milestone boundaries rather than after every change:

- After Phase 6, or after another material installation/setup/authorization change, run a private authenticated checkpoint. Label it accurately: existing read-only GitHub credentials and global harness context mean it tests documentation comprehension and agent-mediated setup, not anonymous public adoption.
- During Phase 8, repeat from an isolated directory or environment with no development checkout or project-specific harness context. Immediately after the repository becomes publicly readable—and before announcing or promoting a release—repeat anonymously without existing GitHub authorization. Verify clone, install, setup explanation, safe validation, reset, and uninstall from the public surface users actually receive.

For each checkpoint, give the same self-contained prompt independently to one fresh capable Codex session, one fresh Gemini/Antigravity session, and either OpenCode or Hermes as a different harness. Do not show agents one another's answers. Keep the first pass read-only and require each agent to explain current capability, automation limits, mutation authority, model/cost behavior, GitHub authorization, installation, validation, and unclear documentation before it changes anything.

The main implementation agent compares the responses; the founder need only provide or point to the outputs. Compare factual agreement, safety interpretation, installation sequence, and recurring friction. Choose only one agent to perform the actual installation so parallel installers do not contaminate the environment. If all three materially agree, stop. If one differs, determine whether the cause is ambiguous documentation, harness contamination, or weak agent performance before changing the product. Do not expand into a large model leaderboard unless a concrete disagreement requires more evidence.

Use this common first-pass prompt, changing only the repository URL when necessary:

> Treat REPOSITORY_URL as a third-party repository I am considering adopting. I have repositories with Dependabot pull requests and security alerts and want to reduce the manual maintenance work.
>
> Do not rely on prior knowledge about this project or its development history. Review only the repository documentation and implementation as needed.
>
> Tell me: (1) What does it do today? (2) Can it currently automate my Dependabot maintenance? (3) What can and cannot it change? (4) Does it require an AI model, incur model costs, or require GitHub authorization? (5) What important limitations or safety boundaries should I understand? (6) How would you install and validate it safely from a fresh environment?
>
> Flag anything unclear, contradictory, unsafe, misleading, or unnecessarily difficult in the onboarding documentation. Do not invent problems merely to find criticism. Do not install anything, create configuration, authenticate, or make local or remote changes until I approve your proposed steps.

For a private authenticated checkpoint, if the agent correctly refuses anonymous access, authorize only the minimum continuation: existing GitHub CLI authentication for read-only access to this repository and one isolated clean clone. Prohibit use of the development checkout, authentication changes, package installation, setup, configuration writes, unrelated repository inspection, and remote mutation until the proposal is reviewed. Record this as private authenticated evidence, not anonymous public adoption. Do not include this clarification in the Phase 8 anonymous checkpoint.

When recording a response, preserve the model/provider, reasoning setting when known, agent harness, commit reviewed, inherited instruction context that may have influenced the result, whether authentication or a clone was authorized, and whether any changes occurred. This provenance helps distinguish a documentation defect from harness-assisted understanding without turning the exercise into a model leaderboard.
