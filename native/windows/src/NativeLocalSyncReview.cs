using System;
using System.IO;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace WatcherNativeProof;

internal static class NativeLocalSyncReview
{
    private static string Text(JsonNode value, string key, string pattern) {
        var text = value[key]?.GetValue<string>() ?? throw new InvalidDataException("Local update identity is missing.");
        if (!Regex.IsMatch(text, pattern, RegexOptions.CultureInvariant))
            throw new InvalidDataException("Local update identity is invalid.");
        return text;
    }

    private static JsonObject Binding(JsonNode value) {
        var binding = value["binding"] as JsonObject ?? throw new InvalidDataException("Local update binding is missing.");
        Text(binding, "head_sha", "\\A(?:[a-f0-9]{40}|[a-f0-9]{64})\\z");
        foreach (var key in new[] { "repository", "directory", "branch" })
            Text(binding, key, "\\A[^\\x00-\\x1f]{1,8192}\\z");
        return binding;
    }

    internal static JsonObject Preparation(JsonNode? node, JsonObject target) {
        var review = node as JsonObject ?? throw new InvalidDataException("No local update review was returned.");
        if (review["schema_version"]?.GetValue<int>() != 1 || review["mutation_performed"]?.GetValue<bool>() != false)
            throw new InvalidDataException("Local update preflight returned an invalid mutation boundary.");
        var binding = Binding(review);
        foreach (var key in new[] { "repository", "directory", "branch" })
            if (!JsonNode.DeepEquals(binding[key], target[key]))
                throw new InvalidDataException("Local update preflight does not match the selected checkout.");
        var status = review["status"]?.GetValue<string>();
        if (status == "up_to_date") return review;
        if (status != "ready" || !JsonNode.DeepEquals(binding["target_id"], target["target_id"]))
            throw new InvalidDataException("No matching confirmable local update was returned.");
        Text(review, "review_id", "\\Async_[a-f0-9]{32}\\z");
        Text(review, "approval_token", "\\Async-[a-f0-9]{64}\\z");
        Text(binding, "target_sha", "\\A(?:[a-f0-9]{40}|[a-f0-9]{64})\\z");
        if (binding["commits"]?.GetValue<int>() is not > 0)
            throw new InvalidDataException("Local update commit count is invalid.");
        return review;
    }

    internal static void Confirmed(JsonNode? node, JsonObject prepared) {
        var result = node as JsonObject ?? throw new InvalidDataException("Local update result is missing.");
        if (result["schema_version"]?.GetValue<int>() != 1 || result["status"]?.GetValue<string>() != "synchronized"
            || result["mutation_performed"]?.GetValue<bool>() != true
            || !JsonNode.DeepEquals(result["review_id"], prepared["review_id"])
            || !JsonNode.DeepEquals(Binding(result), prepared["binding"]))
            throw new InvalidDataException("Local update completion does not match the exact approved review.");
    }

    internal static string Recovered(JsonNode? node, string reviewId, JsonObject expectedBinding) {
        var result = node as JsonObject ?? throw new InvalidDataException("Local update observation is missing.");
        var outcome = result["result"] as JsonObject ?? throw new InvalidDataException("Local update outcome remains unresolved.");
        var status = outcome["status"]?.GetValue<string>();
        if (result["schema_version"]?.GetValue<int>() != 1 || result["review_id"]?.GetValue<string>() != reviewId
            || result["mutation_performed"]?.GetValue<bool>() != false || outcome["mutation_performed"]?.GetValue<bool>() != false
            || !JsonNode.DeepEquals(Binding(result), expectedBinding) || !JsonNode.DeepEquals(Binding(outcome), expectedBinding)
            || status is not ("observed_applied" or "observed_not_applied" or "manual_repair_required")
            || result["status"]?.GetValue<string>() != (status == "observed_applied" ? "completed"
                : status == "observed_not_applied" ? "failed" : "recovery_required"))
            throw new InvalidDataException("Local update observation does not match the saved attempt.");
        return status;
    }
}
