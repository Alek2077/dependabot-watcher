using System;
using System.IO;
using System.Reflection;
using System.Runtime.ExceptionServices;
using System.Text.Json.Nodes;

internal static class LocalSyncContracts
{
    internal static void Run(Assembly assembly)
    {
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
