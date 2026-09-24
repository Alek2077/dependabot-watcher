# Installed-path validation workflow

Use this workflow proportionately after a material feature, onboarding change, packaging change, or user-facing CLI revision. It complements deterministic tests and independent review; it does not replace either.

The objective is to exercise the product as a user or supervising agent receives it, detect friction that source-level tests cannot reveal, and preserve evidence without exposing credentials or private repository data.

Agents should run the applicable deterministic, installed-path, and review checks before Gitflow by default. Founder hands-on testing is optional for ordinary changes rather than a mandatory release tax. Request or recommend a founder checkpoint when the change materially affects first-use onboarding, authorization, mutation, autonomy policy, destructive/recovery behavior, or public-release experience, and whenever the founder wants direct product feedback before Gitflow.

## Safety boundary

- Use a temporary clean checkout or built distribution, not the active development import path.
- Install through the documented user path in an isolated tool environment.
- Point `DEPENDABOT_WATCHER_CONFIG` at a file inside the temporary validation directory, or pass that same temporary `--config` path to every applicable command. Do not let validation create or reuse the operator's real default profile.
- Prefer a synthetic fixture or approved read-only live target. Never broaden repository scope or authorize mutation merely to complete validation.
- Do not inspect secret files, print credentials, or capture screenshots containing tokens or unnecessary private data.
- Remove temporary checkouts, tool installations, test profiles, screenshots, and other state after evidence has been recorded, unless the operator explicitly keeps them for a follow-up test.

## Validation sequence

1. Run the deterministic suite, static analysis, compilation, packaging, and documentation checks appropriate to the changed surface.
2. Install the current artifact through the documented path in a clean environment. Confirm the executable and version resolve from the installed environment rather than the development checkout.
3. Exercise `--help`, `setup`, `doctor`, `inspect`, and `plan` when the change could affect the end-to-end path. Keep setup and all later commands bound to the isolated temporary configuration. Exercise `advise` only with an explicitly approved provider configuration and usage boundary.
4. Repeat the specific changed workflow using representative success, partial-evidence, and failure inputs where practical.
5. Verify structured output separately. JSON must remain parseable and complete, stdout must contain only the selected report, and transient progress or diagnostics must not contaminate pipes or machine consumers.
6. When onboarding or agent usability changed, repeat the task in a fresh minimally briefed agent session using only public repository documentation. Require the agent to explain its plan and safety boundary before installation or mutation.
7. Record material findings, fixes, accepted limitations, commands, and cleanup. Remove the temporary profile as well as the checkout and tool environment. Keep private details in private evidence surfaces; publish only sanitized, reproducible validation evidence.

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
