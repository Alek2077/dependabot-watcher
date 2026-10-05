using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace WatcherNativeProof;

internal sealed record NativePermission(string ConnectionId, string PermissionId, string Digest,
    string JobId, string ThreadId, string TurnId, string ItemId, string Description, IReadOnlyList<string> Choices);

internal static class NativePermissionView
{
    internal static string ParseDecision(JsonObject response, string repository, string expectedDigest,
        NativePermission expected, string decision)
    {
        Parse(response, repository, expectedDigest);
        if (response["mode"]?.GetValue<string>() != "supervisor_permission_decide")
            throw new InvalidDataException("Access acknowledgement is not a decision receipt.");
        var connection = response["connections"]!.AsArray().OfType<JsonObject>()
            .SingleOrDefault(value => value["connection_id"]?.GetValue<string>() == expected.ConnectionId)
            ?? throw new InvalidDataException("Access acknowledgement lost its exact connection.");
        var request = connection["requests"]?[expected.PermissionId] as JsonObject;
        if (connection["job_id"]?.GetValue<string>() != expected.JobId
            || connection["session_id"]?.GetValue<string>() != expected.ThreadId
            || connection["turn_id"]?.GetValue<string>() != expected.TurnId
            || connection["context_digest"]?.GetValue<string>() != expectedDigest
            || request is null || request["digest"]?.GetValue<string>() != expected.Digest
            || request["item_id"]?.GetValue<string>() != expected.ItemId
            || request["decision"]?.GetValue<string>() != decision)
            throw new InvalidDataException("Access acknowledgement differs from the exact submitted decision.");
        return request["status"]?.GetValue<string>() switch {
            "decided" => "saved_waiting_delivery",
            "consumed" => "sent_waiting_resolution",
            "resolved" => "resolved",
            "invalidated" => "invalidated",
            _ => throw new InvalidDataException("Access decision has no confirmed saved outcome.")
        };
    }

    internal static IReadOnlyList<NativePermission> Parse(JsonObject response, string repository, string expectedDigest)
    {
        if (response["mode"]?.GetValue<string>() is not ("supervisor_permissions" or "supervisor_permission_decide")
            || response["repository"]?.GetValue<string>() != repository
            || response["capabilities"]?["model_calls"]?.GetValue<bool>() != false
            || response["capabilities"]?["github_mutations"]?.GetValue<bool>() != false
            || response["connections"] is not JsonArray connections || connections.Count > 50)
            throw new InvalidDataException("Agent permission response has an invalid scope or capability contract.");
        var result = new List<NativePermission>();
        var connectionIds = new HashSet<string>();
        foreach (var node in connections)
        {
            var connection = node as JsonObject ?? throw new InvalidDataException("Invalid permission connection.");
            var connectionId = Identifier(connection["connection_id"], "^connection_[a-f0-9]{32}$");
            var jobId = Identifier(connection["job_id"], "^job_[a-f0-9]{32}$");
            var threadId = Identifier(connection["session_id"], "^[a-zA-Z0-9_-]{1,128}$");
            var digest = Identifier(connection["context_digest"], "^[a-f0-9]{64}$");
            var status = Text(connection["status"], 20);
            var jobStatus = Text(connection["job_status"], 20);
            if (connection["live_connection_confirmed"] is not JsonValue liveValue
                || !liveValue.TryGetValue<bool>(out var live))
                throw new InvalidDataException("Permission response lacks current backend-owner evidence.");
            if (!connectionIds.Add(connectionId) || status is not ("active" or "closed")
                || jobStatus is not ("prepared" or "running" or "completed" or "superseded" or "failed" or "interrupted")
                || connection["current_context"]?.GetValue<bool>() != (digest == expectedDigest)
                || (status == "closed" && live)
                || connection["requests"] is not JsonObject requests || requests.Count > 200)
                throw new InvalidDataException("Permission connection identity or freshness is invalid.");
            string? turnId = connection["turn_id"] is null ? null : Identifier(connection["turn_id"], "^[a-zA-Z0-9_-]{1,128}$");
            foreach (var entry in requests)
            {
                var request = entry.Value as JsonObject ?? throw new InvalidDataException("Invalid permission request.");
                var permissionId = Identifier(request["permission_id"], "^permission_[a-f0-9]{32}$");
                var requestDigest = Identifier(request["digest"], "^[a-f0-9]{64}$");
                var itemId = Identifier(request["item_id"], "^[a-zA-Z0-9_-]{1,128}$");
                var requestStatus = Text(request["status"], 20);
                var description = Text(request["description"], 8192);
                if (entry.Key != permissionId || request["thread_id"]?.GetValue<string>() != threadId
                    || turnId is null || request["turn_id"]?.GetValue<string>() != turnId
                    || requestStatus is not ("waiting" or "decided" or "consumed" or "resolved" or "invalidated")
                    || request["choices"] is not JsonArray choices || choices.Count is < 1 or > 4)
                    throw new InvalidDataException("Permission request is not bound to its saved connection and turn.");
                var allowed = choices.Select(value => Text(value, 20)).ToList();
                if (allowed.Distinct().Count() != allowed.Count
                    || allowed.Any(value => value is not ("allow_once" or "allow_session" or "deny" or "cancel"))
                    || (requestStatus == "waiting" && request["decision"] is not null)
                    || (request["decision"] is not null && !allowed.Contains(Text(request["decision"], 20))))
                    throw new InvalidDataException("Permission choices or decision evidence are invalid.");
                if (status == "active" && (requestStatus is "waiting" or "decided" or "consumed")
                    && (jobStatus != "running" || !live || digest != expectedDigest))
                    throw new InvalidDataException("An agent access request is stale or its backend owner stopped. Refresh the saved queue; the previous agent request needs recovery, not a new grant.");
                if (requestStatus == "waiting" && status == "active" && jobStatus == "running" && live && digest == expectedDigest)
                    result.Add(new NativePermission(connectionId, permissionId, requestDigest, jobId, threadId, turnId,
                        itemId, description, allowed));
            }
        }
        return result;
    }

    private static string Identifier(JsonNode? node, string pattern)
    {
        var value = Text(node, 128);
        return Regex.IsMatch(value, pattern) ? value : throw new InvalidDataException("Invalid permission identifier.");
    }

    private static string Text(JsonNode? node, int limit)
    {
        if (node is not JsonValue value || !value.TryGetValue<string>(out var text)
            || string.IsNullOrWhiteSpace(text) || Encoding.UTF8.GetByteCount(text) > limit)
            throw new InvalidDataException("Permission response contains invalid bounded text.");
        return text;
    }
}
