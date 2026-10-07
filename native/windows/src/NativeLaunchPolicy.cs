using System;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json.Nodes;

namespace WatcherNativeProof;

internal sealed record NativeLaunchPolicy(bool Configured, string Description,
    string AgentLabel = "No agent configured", string UsageSummary = "Sending is unavailable.")
{
    internal static NativeLaunchPolicy Parse(JsonObject policy)
    {
        var names = new[] { "status", "reason", "mode", "provider", "model", "reasoning_effort", "expected_account_type",
            "access", "permission_profile", "validation_authorized", "timeout_seconds", "model_use_acknowledged",
            "full_access_acknowledged", "workspace", "instruction_sources", "requires_fresh_preflight", "live_runtime_verified",
            "github_mutations_authorized", "usage_effect", "evidence_privacy" };
        if (policy.Count != names.Length || policy.Any(field => !names.Contains(field.Key))
            || policy["requires_fresh_preflight"]?.GetValue<bool>() != true
            || policy["live_runtime_verified"]?.GetValue<bool>() != false
            || policy["github_mutations_authorized"]?.GetValue<bool>() != false
            || policy["timeout_seconds"] is not JsonValue timeoutValue || !timeoutValue.TryGetValue<int>(out var timeout) || timeout is < 1 or > 3600
            || Text(policy["usage_effect"]) != "subscription_or_api_usage_if_launched"
            || Text(policy["evidence_privacy"]) != "repository_evidence_and_saved_questions_sent_to_selected_provider_if_launched")
            throw new InvalidDataException("Saved investigation policy has an unsupported authority contract.");
        var access = Text(policy["access"]);
        if (access is not ("inherit" or "restricted" or "full")
            || policy["instruction_sources"] is not JsonArray instructions || instructions.Count > 100)
            throw new InvalidDataException("Saved investigation access or harness is invalid.");
        foreach (var source in instructions) Text(source);
        foreach (var name in new[] { "provider", "model", "reasoning_effort", "expected_account_type", "permission_profile", "workspace" })
            if (policy[name] is not null) Text(policy[name]);
        var validation = policy["validation_authorized"]!.GetValue<bool>();
        var acknowledged = policy["model_use_acknowledged"]!.GetValue<bool>();
        var full = policy["full_access_acknowledged"]!.GetValue<bool>();
        var status = Text(policy["status"]);
        if (status == "blocked") return new NativeLaunchPolicy(false, NativeDisplayText.Escape("Investigation is not configured: " + Text(policy["reason"])));
        if (status != "configured" || policy["reason"] is not null || Text(policy["mode"]) != "approval_first"
            || Text(policy["provider"]) != "codex_app_server" || !acknowledged
            || policy["model"] is null || policy["reasoning_effort"] is null || policy["workspace"] is null
            || Text(policy["expected_account_type"]) is not ("chatgpt" or "apiKey")
            || (access == "full" && !full) || (access == "restricted" && policy["permission_profile"] is null))
            throw new InvalidDataException("Saved investigation policy is not explicitly configured.");
        var accessText = access switch {
            "inherit" => "Your existing Codex permissions and command rules",
            "restricted" => "Watcher-specific restrictions: " + policy["permission_profile"],
            _ => "Full access — an agent with credentials can act outside Watcher's guarded executor"
        };
        return new NativeLaunchPolicy(true, NativeDisplayText.Escape($"Agent: {policy["model"]} · {policy["reasoning_effort"]}\nAccess: {accessText}\n"
            + (validation ? "Inspection and appropriate local tests are authorized." : "Inspection/research only; local tests are not authorized.")
            + $"\nOwned workspace: {policy["workspace"]}\nPrivate repository evidence and saved questions go to your selected Codex account. Uses subscription/API allowance."
            + $"\nAgent timeout: {timeout} seconds. Investigation does not authorize a merge. Workspace, instructions and runtime are checked again when starting."),
            NativeDisplayText.Escape($"{policy["model"]} · {policy["reasoning_effort"]}"),
            NativeDisplayText.Escape($"{accessText}. Sending shares repository context/questions with your selected Codex account and uses its allowance. No merge is authorized."));
    }

    private static string Text(JsonNode? node)
    {
        if (node is not JsonValue value || !value.TryGetValue<string>(out var text) || string.IsNullOrWhiteSpace(text)
            || text.Contains('\0') || Encoding.UTF8.GetByteCount(text) > 8192)
            throw new InvalidDataException("Saved investigation policy contains invalid text.");
        return text;
    }
}
