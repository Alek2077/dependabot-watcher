# Agent integration strategy

This document records the current agent-surface and skill decision for future integration work. It is a design checkpoint, not a claim that every named surface is supported today. Revalidate the external landscape before Phase 8 implementation because agent clients, plugin formats, and third-party skills change quickly.

## Stable product boundary

Dependabot Watcher is a standalone CLI and structured evidence/decision system first. Its configuration, collection, planning, authority, execution, audit, and recovery behavior must remain usable and testable without a particular agent client. Desktop apps, IDE extensions, skills, and plugins are replaceable operator surfaces over that contract; they must not become separate implementations of product policy.

Treat agent support as three independent claims:

1. **Operator compatibility:** an agent can invoke the installed CLI and interpret its human or structured output.
2. **Packaged integration:** a skill, plugin, or equivalent package supplies current Watcher-specific workflows and safety guidance.
3. **Advisory-provider support:** Watcher itself can invoke the provider through a tested adapter for optional model analysis.

## Is a project-owned skill required?

No. A skill is not required for the product to work, and it is not automatically a public-release gate. Fresh-agent testing has already shown that capable agents can understand and operate the CLI from repository documentation alone. It is nevertheless a credible distribution and onboarding surface: a skills catalog entry is searchable, links back to its source repository, and installs directly from that source. This can expose Watcher to users and agents who search for Dependabot help, although an installation count or source link does not prove that a user visited, adopted, or retained the product.

A thin project-owned skill or plugin may still be valuable when evidence shows that it materially improves one or more of:

- discovery in the agent surfaces used by the target audience;
- correct installation and profile selection;
- consistent interpretation of model, privacy, cost, and mutation boundaries;
- reliable use of structured plans, approval-bound execution, audit, and recovery; or
- portability of the same workflow across supported agent families.

Phase 8 should therefore compare the no-skill workflow with an integration prototype before deciding to publish one. The comparison should test discovery as well as task correctness: can a fresh agent find the integration from a Dependabot-oriented query, understand that it operates a separate installed product, and reach the canonical repository and release documentation? If ordinary repository documentation and a concise agent runbook perform equally well, keep those simpler surfaces. If a skill measurably improves discovery, reduces repeated prompting, or prevents safety mistakes, publish a project-owned package because the Watcher project—not a third-party generic skill—must own instructions about its current commands and authority contract.

If created, the package stays thin. It may teach discovery, installation, setup, inspection, planning, model-review selection, execution preflight, exact-action confirmation, post-action verification, and cleanup. It must call the installed product rather than reimplement collection, PR classification, alert correlation, ordering, execution eligibility, authorization, or recovery in prose.

### Probable first package shape

Current OpenAI documentation describes a universal public plugin directory shared by ChatGPT and Codex. A plugin may contain one skill without an MCP server, UI, scripts, or hooks. That makes a small skills-only Watcher plugin the leading Phase 8 prototype rather than a bare Codex-only customization. It could give the integration a stable identity and public listing while keeping the installed Watcher CLI as the product and authority boundary.

Start with one focused skill and no helper script unless testing shows a repeated deterministic step that the CLI itself should not own. Do not add an MCP server merely to wrap local commands, and do not add lifecycle hooks for discovery, ordinary invocation, or scheduling. Hooks are justified only when a supported runtime event genuinely needs automatic setup or validation; scheduling remains an explicit Phase 9 adapter. Test the portable package in Codex CLI, Codex in the ChatGPT desktop app, and the planned Claude operator surfaces before deciding whether provider-specific overlays are necessary. [OpenAI's plugin documentation](https://learn.chatgpt.com/docs/plugins) states that plugins are not available in the Codex IDE extension, while standalone skills are; record IDE validation as operator/standalone-skill compatibility rather than a plugin claim.

OpenAI's broad product reach makes the shared directory a meaningful potential distribution channel, but the addressable audience is the much smaller subset using supported plugin surfaces for software-maintenance work. Directory publication should therefore be treated as an acquisition experiment with discovery, installation, activation, repository-visit, successful-first-run, and retained-use evidence—not as automatic exposure to every ChatGPT user.

### Phase 8 prototype decision

The thin package earned continued development in an initial matched Codex CLI comparison. Without the skill, a fresh GPT-5.6 Luna session recognized the request's authority conflict and proposed generic caution, but did not know Watcher's structured command path or exact execution contract. With the skill, the same model selected `doctor`, `inspect`, and `plan` JSON; distinguished optional model disclosure from deterministic evidence; required a no-token preflight and the exact repository/PR/base/head/method/check binding; refused to treat the confirmation value as authority; and required a fresh plan after a merge. That is a material consistency and safety improvement, not merely different wording.

Codex CLI `0.157.0` and Claude Code `2.1.282` both discovered and installed the package successfully under disposable client configuration roots. The Claude manifests also passed strict validation. Claude behavioral activation remains unverified because the installed client was signed out. Later Codex CLI and IDE exercises validated skill activation and read-only operation; the ChatGPT desktop app validated plugin activation and safe handling of a missing local CLI. These results justify retaining the skills-only package while keeping public submission and broader support labels gated on later evidence.

For future adoption checks, use the normal client permission flow and an outcome-oriented prompt that lets the agent discover the product and its prerequisites. Command-bounded prompts remain useful for a specific authority or output contract, but they do not prove unassisted discovery or onboarding.

## Existing skill landscape

Reviewed 2026-09-25. These are useful prior art and possible complements, not interchangeable substitutes for the Watcher.

| Skill/workflow | Primary scope | Relationship to Watcher | Material lesson |
|---|---|---|---|
| [`github/awesome-copilot: dependabot`](https://github.com/github/awesome-copilot/tree/71c55d4e12cf0f05d473bd7b5fb3943f3e1ed112/skills/dependabot) | General Dependabot configuration, grouping, update policy, PR commands, and supply-chain guidance | Complementary. It teaches GitHub Dependabot itself; it does not provide Watcher's multi-repository queue, plan, exact-action execution, or audit model. | Do not duplicate the general configuration reference. Link or interoperate where appropriate. |
| [`cli/cli: dependabot-triager`](https://github.com/cli/cli/tree/5c39f803f10777aea7f37c62b1299f20f5637c60/.github/skills/dependabot-triager) | Hourly repository-specific AI review that posts one recommendation per PR head | Strong adjacent prior art, but advisory and tailored to GitHub CLI's Go/Actions dependency graph. It does not merge, correlate a multi-repository alert queue, or provide Watcher's reusable local control plane. | Adopt the separation of deterministic no-work filtering from paid reasoning, upstream-diff/reachability evidence, prompt-injection treatment, exact-head deduplication, and safe bounded output. |
| [`efoo-team/skills: dependabot-sweep`](https://github.com/efoo-team/skills/tree/df7a17db7677981ebe9f3ba09c5faa5044eb38c1/skills/dependabot-sweep) | Explicitly invoked bulk consolidation: edit dependencies on one branch, create one replacement PR, comment on and close original Dependabot PRs | Different and substantially more mutation-heavy. It solves one repository's consolidation workflow rather than safe queue inspection and bounded execution. | Consolidation may be a future optional policy, but its blast radius and closure behavior require stronger evidence and authority than prose instructions alone. |
| [`medusajs/medusa: diagnosing-dependabot-alerts`](https://github.com/medusajs/medusa/tree/8886900506dc76f56cfb4f17b84574cfbe5f9213/.claude/skills/diagnosing-dependabot-alerts) | Medusa-specific yarn-monorepo alert tracing and least-invasive remediation, paired with scheduled deterministic triage and Claude-created fix PRs | The closest closed-loop reference, but deliberately coupled to Medusa's workspace layout, lockfile, custom scripts, tokens, and release model. It can automate remediation inside that one repository more deeply than Watcher currently does. | Preserve deterministic pre-filtering, provide models bounded evidence without broad credentials, separate the agent's edits from workflow-owned commit/PR mutation, and learn from ecosystem-specific reachability. |
| [`equinor/fusion-skills: fusion-dependency-review`](https://github.com/equinor/fusion-skills/tree/f9b1878dba09159321dbcc0258c21764fd6e3a88/skills/.experimental/fusion-dependency-review) | Experimental single-PR research, parallel security/quality/impact lenses, verdict, comments, and maintainer-confirmed source-control action | Complementary review workflow rather than a persistent queue/control plane. | Distinguish recommendation, evidence confidence, and mechanical readiness; reuse one evidence packet across independent lenses; preserve disagreement until synthesis. |
| [`bitwarden/ai-plugins: triaging-security-findings`](https://github.com/bitwarden/ai-plugins/tree/44659bdf648f7293838fb2b9eb43e1dbac5496e4/plugins/bitwarden-security-engineer/skills/triaging-security-findings) | Bitwarden-specific Aikido/Jira security operations plus GitHub-native Dependabot and secret-alert lookup | Organization-specific security operations, not a general Dependabot product. | Keep alert scope pinned, verify group-wide effects before group actions, and route uncertain findings instead of declaring false positives. |

Before reusing text, scripts, templates, or bundled assets, verify the exact source revision, license, provenance, and compatibility. Conceptual lessons may inform design; copying is not assumed.

### Popularity signal, with limits

On 2026-09-25, the Skills CLI reported these total deduplicated install counts for the six reviewed skills:

| Skill | Reported installs |
|---|---:|
| [`github/awesome-copilot: dependabot`](https://skills.sh/github/awesome-copilot/dependabot) | 4.4K |
| [`equinor/fusion-skills: fusion-dependency-review`](https://skills.sh/equinor/fusion-skills/fusion-dependency-review) | 2.0K |
| [`bitwarden/ai-plugins: triaging-security-findings`](https://skills.sh/bitwarden/ai-plugins/triaging-security-findings) | 80 |
| [`cli/cli: dependabot-triager`](https://skills.sh/cli/cli/dependabot-triager) | 58 |
| [`efoo-team/skills: dependabot-sweep`](https://skills.sh/efoo-team/skills/dependabot-sweep) | 36 |
| [`medusajs/medusa: diagnosing-dependabot-alerts`](https://skills.sh/medusajs/medusa/diagnosing-dependabot-alerts) | 3 |

Treat these as catalog-discovery evidence, not active-user, invocation, retention, or quality metrics. Counts can benefit from the visibility of the owning repository and from users installing a repository or pack containing several skills. The two highest-count entries also have broad or reusable scopes: GitHub's skill teaches general Dependabot configuration and management, while Equinor's experimental skill offers a structured dependency-PR review with research, independent security/quality/impact lenses, a synthesized verdict, and explicit confirmation. The evidence does not establish that those features caused their install counts.

The low catalog counts for the GitHub CLI and Medusa skills do not make their workflows unimportant. Both are repository-local components embedded in real scheduled automation, so marketplace installation is not their primary adoption path. Operational sophistication and catalog popularity are different signals.

### A skill is not a scheduler

Installing a skill gives an active agent instructions and any packaged resources. It does not create a daemon, cron entry, GitHub Actions workflow, webhook, or other wake-up mechanism. A harness may load the skill when a user request matches its description, but no work occurs while no agent session or external workflow is running.

The GitHub CLI and Medusa examples make this separation explicit: a scheduled GitHub workflow wakes up, performs deterministic collection or filtering, and then asks an agent to follow the repository-local skill. Their skill is the reasoning procedure; GitHub Actions is the scheduler and authority envelope. A future Watcher skill can improve discovery and agent operation, but Phase 9 scheduling remains necessary for unattended monitoring.

### Strongest adjacent workflows versus Watcher

| Capability | Watcher today | GitHub CLI triager | Medusa alert workflow | Direction |
|---|---|---|---|---|
| Scope | Reusable multi-repository PR and alert queue | One repository's Dependabot PRs | One yarn monorepo's security alerts | Preserve Watcher's reusable multi-repository core. |
| Scheduled operation | None | Hourly reconciler | Daily scheduled triage and remediation | Add optional scheduling adapters in Phase 9; do not bury scheduling inside a skill. |
| Deterministic pre-filter | Yes: identity, checks, paths, completeness, alert relationships, policy, and execution eligibility | Yes: bot author, terminal CI, current head, and whether that head was already assessed | Yes: production-dependency closure identifies dismissible versus actionable alerts | Preserve and extend deterministic no-work/model-call gating. |
| Dependency reachability | Bounded Python requirements inclusion only; generic GitHub evidence elsewhere | Go production build graph plus repository use of GitHub Actions | Yarn dependency-chain, workspace ownership, published-package, and runtime-impact analysis | Add explicit ecosystem adapters based on evidence and demand; never ask a model to invent reachability. |
| Revision deduplication | Exact-head action binding, stale-plan rejection, audit reconciliation, and idempotent merge retry; no scheduled advisory-comment deduplication | Skips a PR already commented on at the same full head SHA | Avoids agent work when deterministic triage leaves no alerts; creates a new remediation branch when work remains | Reuse stable identities in Phase 9 to suppress duplicate analysis, notifications, and actions. |
| Agent output | Bounded schema-validated advice and local text/Markdown/JSON; no GitHub comments | One bounded recommendation comment per PR head | Bounded file edits and PR metadata; workflow owns commit/push/PR creation | Keep comments/PR creation as optional adapters, not default authority. |
| Mutation | One exact approval-gated Dependabot merge with final recheck, audit, reconciliation, and post-action report | Comment only; never merges | Deterministically dismisses irrelevant alerts and lets Claude prepare remediation changes for workflow-owned PR creation; does not auto-merge that PR | Expand only through separately authorized, observable policy classes. |

Watcher therefore already has deterministic filtering, bounded model input/output, and stronger exact-action execution/audit machinery. It does not yet match the GitHub CLI workflow's Go/Actions reachability or upstream-change review, its scheduled exact-head comment reconciliation, or Medusa's repository-specific remediation depth. Those are real gaps and useful design evidence, not proof that one repository-specific workflow generalizes safely to every Watcher user.

Equinor's skill reviews a single dependency-update PR—not an ordinary feature PR. It accepts Dependabot, Renovate, or another dependency-update PR, gathers its discussion and external evidence, runs separate security/code-quality/impact lenses, synthesizes a verdict, and requires maintainer confirmation before source-control action.

## Obsolescence assessment

The skill ecosystem does not make Dependabot Watcher obsolete. Skills are instruction packages executed by an agent inside a particular repository and permission environment. Several can perform impressive narrow workflows, but their behavior depends on the current model, harness, prompt, checkout, tools, credentials, and repository-specific assumptions.

Watcher is valuable where it supplies durable machinery that skills commonly lack:

- one bounded view across multiple repositories;
- normalized PR, alert, check, path, history, and freshness evidence;
- stable text, Markdown, and versioned JSON contracts;
- deterministic ordering and explicit incomplete-evidence states;
- provider-independent identity and policy boundaries;
- exact repository/PR/base/head/merge-method action binding;
- final live revalidation, single-action mutation, audit, idempotent reconciliation, and recovery evidence; and
- a scheduler-independent core that future adapters can invoke consistently.

The competitive lesson is still important: repository-specific agent workflows can move faster and perform deeper ecosystem remediation than a generic product. Watcher must earn its place through reliable cross-repository operation, clearer safety and evidence, good agent ergonomics, and progressively deeper adapters—not by assuming that a standalone CLI is automatically superior to a well-designed skill.

## Competitive learning principle

Product quality and pain removal take precedence over being different for its own sake. When another project demonstrates a capability that materially improves reliability, evidence quality, efficiency, safety, or effort, evaluate and adapt that pattern even if it reduces superficial feature differentiation. Conversely, popularity or competitor presence alone is not sufficient reason to add a feature.

Classify findings as:

- **table stakes:** capabilities users reasonably need for the promised outcome;
- **complementary strengths:** capabilities worth integrating without replacing Watcher's core;
- **repository-specific advantages:** patterns to generalize only through explicit adapters and bounded evidence; or
- **out-of-scope choices:** behavior that adds authority, complexity, or cost without enough product value.

Watcher's differentiation should emerge from the combined system—cross-repository evidence, deeper adapters, efficient model judgment, deterministic authority, exact execution, audit/recovery, and low-friction closed-loop operation—not from intentionally omitting useful capabilities that other tools have.

## Initial surface and AI direction

Codex CLI remains the initial advisory provider and first operator surface. Codex in the ChatGPT desktop app is the corresponding graphical operator target, with the Codex IDE extension receiving a compatibility smoke test rather than separate product logic. Claude Code CLI and Claude Desktop are the second operator family; a Claude advisory adapter is not required before the first public release. GitHub Copilot, OpenCode, Antigravity, and Hermes remain later evidence-driven packaging targets while generic shell and structured-output compatibility is preserved.

The initial Codex IDE smoke validated the intended distinction: the extension discovered and applied the standalone local skill even though plugin bundles are not supported there, then used it for correctly scoped live read-only doctor and inspection commands with no model call or mutation. The matching Claude Code VS Code UI opened, but no Claude account was available for runtime validation. Do not use an unofficial Anthropic-compatible proxy to promote that row: such a run would establish only experimental bridge/provider compatibility, not official Claude runtime support.

“AI is optional” defines the trust boundary rather than discouraging model use. Guided setup and public documentation should recommend model assistance when a supported provider is available and obtain explicit consent before transmitting private evidence or consuming quota. Model review should be recommended, and may be policy-required, for ambiguous, grouped, transitive, major, sensitive, ecosystem-unsupported, or otherwise higher-risk work. Models interpret evidence and may veto; deterministic evidence, repository checks, and explicit policy authorize actions.
