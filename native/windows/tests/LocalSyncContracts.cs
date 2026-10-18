using System;
using System.IO;
using System.Reflection;
using System.Runtime.ExceptionServices;
using System.Text.Json.Nodes;

internal static class LocalSyncContracts
{
    internal static void Run(Assembly assembly)
    {
        var windowType = assembly.GetType("WatcherNativeProof.BackendQueueWindow", true)!;
        var latestMethod = windowType.GetMethod("LatestLocalSyncOutcome", BindingFlags.Static | BindingFlags.NonPublic)!;
        var older = JsonNode.Parse("{\"review_id\":\"sync_zz\",\"binding\":{\"target_id\":\"target\"},\"expires_at\":\"2026-10-16T09:15:00Z\",\"status\":\"completed\"}")!;
        var newer = JsonNode.Parse("{\"review_id\":\"sync_aa\",\"binding\":{\"target_id\":\"target\"},\"expires_at\":\"2026-10-16T09:30:00Z\",\"status\":\"recovery_required\"}")!;
        var preview = JsonNode.Parse("{\"review_id\":\"sync_mm\",\"binding\":{\"target_id\":\"target\"},\"expires_at\":\"2026-10-16T09:45:00Z\",\"status\":\"awaiting_confirmation\"}")!;
        var history = new JsonArray(newer, preview, older);
        if (!ReferenceEquals(latestMethod.Invoke(null, new object[] { history, "target" }), newer))
            throw new InvalidDataException("Random review ordering or a preview hid the latest uncertain checkout outcome.");
        older["sequence"] = 1L; newer["sequence"] = 2L;
        newer["expires_at"] = "2026-10-16T08:00:00Z";
        if (!ReferenceEquals(latestMethod.Invoke(null, new object[] { history, "target" }), newer))
            throw new InvalidDataException("Clock rollback hid a causally newer checkout outcome.");
        var wireHistory = JsonNode.Parse(history.ToJsonString())!.AsArray();
        var wireLatest = (JsonObject?)latestMethod.Invoke(null, new object[] { wireHistory, "target" });
        if (wireLatest?["review_id"]?.GetValue<string>() != "sync_aa")
            throw new InvalidDataException("Serialized checkout history lost causal outcome ordering.");
        var type = assembly.GetType("WatcherNativeProof.NativeLocalSyncReview", true)!;
        object? Call(string name, params object?[] values) {
            try { return type.GetMethod(name, BindingFlags.Static | BindingFlags.NonPublic)!.Invoke(null, values); }
            catch (TargetInvocationException exception) when (exception.InnerException is not null) {
                ExceptionDispatchInfo.Capture(exception.InnerException).Throw(); throw;
            }
        }
        void Refused(string name, params object?[] values) {
            try { Call(name, values); }
            catch (InvalidDataException) { return; }
            throw new InvalidDataException("Malformed local synchronization response was accepted: " + name);
        }
        var target = new JsonObject { ["repository"] = "example-org/example-app", ["directory"] = "/owned/checkout",
            ["branch"] = "integration", ["target_id"] = new string('a', 32) };
        var binding = (JsonObject)target.DeepClone();
        var workflow = new JsonObject { ["state"] = "ready", ["method"] = "fast_forward",
            ["message"] = "The assessed workflow permits a separately approved clean fast-forward." };
        Call("WorkflowState", workflow);
        var incompatible = (JsonObject)workflow.DeepClone(); incompatible["method"] = "rebase";
        Refused("WorkflowState", incompatible);
        incompatible["state"] = "unsupported";
        if ((string)Call("WorkflowState", incompatible)! != "unsupported")
            throw new InvalidDataException("A different integration method was relabelled as fast-forward readiness.");
        var unknownWorkflow = new JsonObject { ["state"] = "assessment_needed", ["message"] = "Relevant guidance conflicts." };
        Call("WorkflowState", unknownWorkflow);
        var observation = new JsonObject { ["target_id"] = target["target_id"]!.GetValue<string>(),
            ["status"] = "current", ["mutation_performed"] = false, ["checked_at"] = "2026-10-17T20:00:00Z",
            ["message"] = "Local branch matched the checked remote commit.",
            ["head_sha"] = new string('b', 40), ["remote_sha"] = new string('b', 40), ["tracking_sha"] = new string('b', 40) };
        Call("Observation", observation, target);
        var noZone = (JsonObject)observation.DeepClone(); noZone["checked_at"] = "2026-10-17T20:00:00";
        Refused("Observation", noZone, target);
        var different = (JsonObject)observation.DeepClone(); different["remote_sha"] = new string('c', 40);
        Refused("Observation", different, target);
        different["status"] = "review_needed";
        Call("Observation", different, target);
        different["target_id"] = new string('f', 32);
        Refused("Observation", different, target);
        var staleTracking = (JsonObject)observation.DeepClone(); staleTracking["tracking_sha"] = new string('c', 40);
        Refused("Observation", staleTracking, target);
        binding["head_sha"] = new string('b', 40); binding["target_sha"] = new string('c', 40); binding["commits"] = 1;
        var prepared = new JsonObject { ["schema_version"] = 1, ["status"] = "ready", ["mutation_performed"] = false,
            ["review_id"] = "sync_" + new string('d', 32), ["approval_token"] = "sync-" + new string('e', 64),
            ["binding"] = binding.DeepClone() };
        Call("Preparation", prepared, target);
        var trackingOnly = (JsonObject)prepared.DeepClone();
        trackingOnly["binding"]!["head_sha"] = new string('c', 40);
        trackingOnly["binding"]!["commits"] = 0;
        trackingOnly["binding"]!["tracking_refresh"] = new JsonObject {
            ["ref"] = "refs/remotes/origin/integration", ["old_sha"] = new string('b', 40),
            ["config_digest"] = new string('f', 64) };
        Call("Preparation", trackingOnly, target);
        var description = (string)Call("TrackingDescription", trackingOnly["binding"]!)!;
        if (!description.Contains("refs/remotes/origin/integration") || !description.Contains("No other"))
            throw new InvalidDataException("Tracking-reference update scope is not disclosed.");
        var unrelated = (JsonObject)trackingOnly.DeepClone();
        unrelated["binding"]!["tracking_refresh"]!["ref"] = "refs/remotes/origin/other";
        Refused("Preparation", unrelated, target);
        var noUpdate = (JsonObject)trackingOnly.DeepClone();
        noUpdate["binding"]!["tracking_refresh"]!["old_sha"] = new string('c', 40);
        Refused("Preparation", noUpdate, target);
        foreach (var field in new[] { "repository", "directory", "branch", "target_id" }) {
            var malformed = (JsonObject)prepared.DeepClone(); malformed["binding"]![field] = "wrong";
            Refused("Preparation", malformed, target);
        }
        var mutatingPreflight = (JsonObject)prepared.DeepClone(); mutatingPreflight["mutation_performed"] = true;
        Refused("Preparation", mutatingPreflight, target);
        var confirmed = new JsonObject { ["schema_version"] = 1, ["status"] = "synchronized", ["mutation_performed"] = true,
            ["review_id"] = prepared["review_id"]!.GetValue<string>(), ["binding"] = binding.DeepClone() };
        Call("Confirmed", confirmed, prepared);
        foreach (var field in new[] { "review_id", "mutation_performed", "binding" }) {
            var malformed = (JsonObject)confirmed.DeepClone(); malformed[field] = null;
            Refused("Confirmed", malformed, prepared);
        }
        var stale = (JsonObject)confirmed.DeepClone(); stale["binding"]!["target_sha"] = new string('f', 40);
        Refused("Confirmed", stale, prepared);
        var recovered = new JsonObject { ["schema_version"] = 1, ["status"] = "completed", ["mutation_performed"] = false,
            ["review_id"] = prepared["review_id"]!.GetValue<string>(), ["binding"] = binding.DeepClone(),
            ["result"] = new JsonObject { ["status"] = "observed_applied", ["mutation_performed"] = false,
                ["binding"] = binding.DeepClone() } };
        Call("Recovered", recovered, prepared["review_id"]!.GetValue<string>(), binding);
        var replay = (JsonObject)recovered.DeepClone(); replay["result"]!["mutation_performed"] = true;
        Refused("Recovered", replay, prepared["review_id"]!.GetValue<string>(), binding);
        Refused("Recovered", recovered, "sync_" + new string('f', 32), binding);
        var partial = (JsonObject)recovered.DeepClone(); partial["status"] = "recovery_required";
        partial["result"]!["status"] = "manual_repair_required";
        Call("Recovered", partial, prepared["review_id"]!.GetValue<string>(), binding);
        partial["status"] = "completed";
        Refused("Recovered", partial, prepared["review_id"]!.GetValue<string>(), binding);
        Console.WriteLine("Local synchronization response contracts passed: exact selection, review, binding and mutation flags.");
    }
}
