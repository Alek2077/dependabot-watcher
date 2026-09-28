# Agent support matrix

This matrix separates three claims that are easy to conflate:

- **Operator compatibility:** the agent can run the installed CLI and preserve its evidence and authority boundaries.
- **Packaged integration:** a project-owned skill or plugin is installable and validated on that surface.
- **Advisory provider:** Dependabot Watcher itself can invoke that provider for optional model analysis.

`Planned` and `unverified` are not support claims. Update this matrix only from recorded installed-path evidence against a named client and version.

| Surface | Operator | Packaged integration | Advisory provider | Current status |
|---|---|---|---|---|
| Codex CLI | Read-only operator path verified on `0.157.0` | Installed skill discovery and activation passed; live inspection passed after command-specific network approval | Bounded real `economy`, `reviewed`, and scheduled `economy` calls passed without mutation | Sol Medium summarized three repositories; Watcher used Luna High triage and Sol Medium review with schema-valid recorded usage |
| Codex in the ChatGPT desktop app | Missing-CLI handling verified; live CLI operation unverified | Local plugin rendering, installation, and activation passed | Not a separate adapter | Luna XHigh and Sol Medium stopped when the Windows app could not see the WSL-installed CLI |
| Codex IDE extension | Verified read-only operator path on `openai.chatgpt` `26.917.62051` | Standalone local skill discovery and activation passed; plugin bundles remain unavailable | Not a separate adapter | GPT-6 Sol Medium ran scoped version/doctor/inspect, summarized complete evidence, and made zero model calls or mutations |
| Claude Code CLI | Runtime activation blocked by signed-out local client | Install/discovery only on `2.1.282`; runtime activation unverified | Not implemented | Experimental portability evidence; official runtime validation is post-release unless priorities change |
| Claude Desktop Code tab | Unverified | Unverified; local plugin UI smoke pending | Not implemented | Later evidence-driven target, not a Codex-first release gate |
| Claude Code VS Code extension | Extension UI activation passed on `2.1.282`; runtime unavailable without a Claude account | Plugin activation unverified | Not implemented | Later target; do not treat an unofficial provider proxy as Anthropic runtime evidence |
| GitHub Copilot | Unverified | None | Not implemented | Later evidence-driven target |
| OpenCode | Unverified | None | Not implemented | Later evidence-driven target |
| Antigravity | Unverified | None | Not implemented | Later evidence-driven target |
| Hermes | Unverified | None | Not implemented | Later evidence-driven target |

## Package boundary

The repository contains a thin skills-only plugin prototype with OpenAI and Claude Code compatibility manifests. It has no MCP server, lifecycle hooks, helper scripts, scheduler, GitHub credentials, or mutation implementation. It teaches an active agent to operate the separately installed `dependabot-watcher` command and to preserve the same model, privacy, approval, audit, and recovery boundaries described in the [agent operator guide](agent-operator-guide.md).

Current [OpenAI plugin documentation](https://learn.chatgpt.com/docs/plugins) states that plugins are available in ChatGPT and Codex supported surfaces and through the Codex CLI plugin browser, but not in the Codex IDE extension. Standalone skills are available in the IDE extension. The matrix therefore records plugin and standalone-skill support separately. Current Claude documentation describes user-scoped plugins across its terminal, Desktop Code tab, and VS Code extension. The isolated install proves only local package discovery and caching; actual activation on every client remains unverified until each row is exercised.

The local package was installed and discovered under isolated client configuration roots with Codex CLI `0.157.0` and Claude Code `2.1.282`. A matched Codex GPT-5.6 Luna guidance comparison showed that the skill added Watcher's JSON workflow, private-evidence disclosure, no-token preflight, exact action binding, confirmation-token boundary, and mandatory replanning that the no-skill response omitted.

A later normal Codex CLI session activated the installed skill against an existing three-repository read-only profile. Its first GitHub call failed under the default network sandbox, and `gh` reported an authentication error even though host-level authentication was valid. After the operator prompted both agents to reconsider that result, they requested approval for the unchanged Watcher commands. Sol Medium completed `doctor` and `inspect` and summarized 10 pull requests, 4 alerts, 2 unresolved alerts, and 1 attention-required item without model use or mutation. Luna XHigh received approval but produced no reliable queue summary after retries; its transcript appeared to contain inspection output, so this run cannot establish a Watcher output failure. This proves command-specific approval handling after operator guidance, not unassisted recovery from the initial failure.

A separate Codex VS Code smoke established standalone-skill discovery, bounded reasoning, and live read-only CLI operation against one configured repository. The ChatGPT desktop app rendered and installed the package, attached it to a Work chat, and activated the skill; both tested models stopped safely when the Windows runtime could not find the WSL-installed CLI. Claude's VS Code UI opened, while model/plugin execution remained unavailable because the operator has no Claude account. Public discovery and retained use remain unverified.

## Evidence required to promote a surface

A first-class operator claim requires a fresh task that establishes discovery, installation proposal, setup and authentication explanation, model/privacy/cost disclosure, read-only inspection and planning, exact-action execution explanation, structured-output handling, interruption/failure behavior, and cleanup without relying on private development history.

A packaged-integration claim additionally requires installation through that surface, correct skill activation, and evidence that the package improves discovery, consistency, or safety over the documentation-only runbook. Merely being able to load `SKILL.md` is not sufficient.

End-to-end adoption checks should use the ordinary client permission flow and a minimally scripted outcome-oriented request. A command-bounded prompt tests a specific contract; it does not establish unassisted discovery, setup, or recovery.
