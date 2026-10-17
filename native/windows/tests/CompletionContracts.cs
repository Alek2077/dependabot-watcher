using System;
using System.IO;
using System.Collections;
using System.Linq;
using System.Reflection;
using System.Runtime.ExceptionServices;
using System.Text.Json.Nodes;
using System.Threading;
using System.Windows;
using System.Windows.Controls;

internal static class CompletionContracts
{
    internal static void Run(Assembly assembly)
    {
        var type = assembly.GetType("WatcherNativeProof.NativeActionReview", true)!;
        object Parse(JsonObject value) {
            try { return type.GetMethod("Parse", BindingFlags.Static | BindingFlags.NonPublic)!.Invoke(null, new object[] { value })!; }
            catch (TargetInvocationException exception) when (exception.InnerException is not null) {
                ExceptionDispatchInfo.Capture(exception.InnerException).Throw(); throw;
            }
        }
        void Refuse(JsonObject value) {
            try { Parse(value); }
            catch (InvalidDataException) { return; }
            throw new InvalidDataException("Invalid completion display binding was accepted.");
        }
        var binding = new JsonObject { ["repository"] = "example-org/example-app", ["pull_request"] = 42,
            ["head_sha"] = new string('a', 40), ["expected_base"] = "integration", ["merge_method"] = "squash" };
        var preparation = new JsonObject { ["schema_version"] = 1, ["status"] = "ready", ["version"] = 0,
            ["proposal_id"] = "proposal_" + new string('a', 16), ["plan_id"] = "plan_" + new string('b', 16),
            ["action_id"] = "action_" + new string('c', 32), ["binding"] = binding.DeepClone(),
            ["mutation_authorized"] = false, ["mutation_performed"] = false,
            ["capabilities"] = new JsonObject { ["github_mutations"] = false, ["model_calls"] = false },
            ["decision"] = new JsonObject { ["title"] = "Synthetic update", ["model_used"] = false,
                ["required_checks"] = new JsonArray() } };
        var value = new JsonObject { ["schema_version"] = 1, ["status"] = "completed",
            ["review_id"] = "review_" + new string('d', 32), ["preparation"] = preparation,
            ["created_at"] = "2026-10-10T12:00:00Z", ["expires_at"] = "2026-10-10T12:15:00Z",
            ["consented_at"] = "2026-10-10T12:01:00Z", ["outcome_recorded"] = true,
            ["result"] = new JsonObject { ["status"] = "merged", ["mutation_performed"] = true,
                ["binding"] = binding.DeepClone() } };
        Parse(value); // Legacy records do not acquire invented obligations.
        var summary = type.GetProperty("RecordedMaintenanceSummary", BindingFlags.Instance | BindingFlags.NonPublic)!;
        if (summary.GetValue(Parse(value)) is not null)
            throw new InvalidDataException("Legacy merge invents maintenance completion.");
        var canAssess = type.GetProperty("CanAssessCompletion", BindingFlags.Instance | BindingFlags.NonPublic)!;
        if (canAssess.GetValue(Parse(value)) is not false)
            throw new InvalidDataException("Legacy completion can launch an unbound assessment.");
        value["completion"] = new JsonObject { ["schema_version"] = 2,
            ["action_id"] = preparation["action_id"]!.GetValue<string>(),
            ["repository"] = "example-org/example-app", ["base_branch"] = "integration",
            ["policy"] = "selected_local_sync", ["finalization"] = null,
            ["targets"] = new JsonArray(new JsonObject { ["repository"] = "example-org/example-app",
                ["directory"] = "/tmp/selected", ["branch"] = "integration", ["target_id"] = new string('a', 32) }),
            ["assessment_required"] = false, ["merge_commit_sha"] = null,
            ["evidence"] = null, ["assessment"] = null, ["local_results"] = new JsonObject() };
        preparation["decision"]!["completion_scope"] = new JsonObject { ["policy"] = "selected_local_sync",
            ["targets"] = value["completion"]!["targets"]!.DeepClone(), ["assessment_required"] = false };
        value["completion_stage"] = "local_sync";
        if (canAssess.GetValue(Parse(value)) is not false)
            throw new InvalidDataException("Missing merge revision can launch an assessment.");
        value["completion"]!["merge_commit_sha"] = new string('e', 40);
        if (canAssess.GetValue(Parse(value)) is not true)
            throw new InvalidDataException("Exact merge revision cannot continue assessment.");
        var parsed = Parse(value);
        if (summary.GetValue(parsed) is not null)
            throw new InvalidDataException("Pending maintenance is presented as complete.");
        if (type.GetProperty("HasPendingCompletion", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(parsed) is not true)
            throw new InvalidDataException("Pending completion is not visible after restart.");
        foreach (var field in new[] { "schema_version", "action_id", "repository", "base_branch", "policy" }) {
            var malformed = (JsonObject)value.DeepClone();
            malformed["completion"]![field] = field == "schema_version" ? JsonValue.Create(1) : JsonValue.Create("wrong");
            Refuse(malformed);
        }
        var premature = (JsonObject)value.DeepClone(); premature["completion_stage"] = "completed";
        Refuse(premature);
        var finished = (JsonObject)value.DeepClone(); finished["completion_stage"] = "completed";
        var sha = new string('e', 40); var digest = new string('f', 64);
        finished["completion"]!["evidence"] = new JsonObject { ["base_sha"] = sha, ["context_digest"] = digest };
        finished["completion"]!["local_results"] = new JsonObject { [new string('a', 32)] = new JsonObject {
            ["head_sha"] = sha, ["remote_sha"] = sha } };
        finished["completion"]!["finalization"] = new JsonObject { ["base_sha"] = sha,
            ["context_digest"] = digest, ["verified_at"] = "2026-10-10T12:05:00Z" };
        if (type.GetProperty("HasPendingCompletion", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(Parse(finished)) is not false)
            throw new InvalidDataException("Completed maintenance remains falsely pending.");
        if (summary.GetValue(Parse(finished)) is not string text || !text.Contains("Saved completion:")
            || !text.Contains("not a live checkout check") || !text.Contains("Local synchronization:")
            || !text.Contains("Production promotion:") || !text.Contains("not verified by this action"))
            throw new InvalidDataException("Saved completion is missing or overclaims live/production checks.");
        foreach (var fault in new[] { "no_completion", "no_merge", "empty_final", "wrong_digest", "missing_local", "missing_assessment", "wrong_scope", "no_zone" })
        {
            var broken = (JsonObject)finished.DeepClone();
            switch (fault) {
                case "no_completion": broken["completion"] = null; break;
                case "no_merge": broken["completion"]!["merge_commit_sha"] = null; break;
                case "empty_final": broken["completion"]!["finalization"] = new JsonObject(); break;
                case "wrong_digest": broken["completion"]!["finalization"]!["context_digest"] = new string('a', 64); break;
                case "missing_local": broken["completion"]!["local_results"] = new JsonObject(); break;
                case "missing_assessment":
                    broken["completion"]!["assessment_required"] = true;
                    broken["preparation"]!["decision"]!["completion_scope"]!["assessment_required"] = true; break;
                case "wrong_scope": broken["preparation"]!["decision"]!["completion_scope"]!["policy"] = "remote_only"; break;
                case "no_zone": broken["completion"]!["finalization"]!["verified_at"] = "2026-10-10T12:05:00"; break;
            }
            Refuse(broken);
        }
        var remoteOnly = (JsonObject)finished.DeepClone();
        remoteOnly["completion"]!["policy"] = "remote_only";
        remoteOnly["completion"]!["targets"] = new JsonArray();
        remoteOnly["completion"]!["local_results"] = new JsonObject();
        remoteOnly["preparation"]!["decision"]!["completion_scope"] = new JsonObject {
            ["policy"] = "remote_only", ["targets"] = new JsonArray(), ["assessment_required"] = false };
        if (summary.GetValue(Parse(remoteOnly)) is not string remoteText || remoteText.Contains("local updates"))
            throw new InvalidDataException("Remote-only completion claims selected local updates.");
        var unrecorded = (JsonObject)finished.DeepClone(); unrecorded["outcome_recorded"] = false;
        if (summary.GetValue(Parse(unrecorded)) is not null)
            throw new InvalidDataException("Unrecorded action outcome claims saved maintenance completion.");
        var recovered = (JsonObject)finished.DeepClone();
        recovered["result"]!["status"] = "already_applied";
        recovered["result"]!["mutation_performed"] = false;
        if (summary.GetValue(Parse(recovered)) is not string recoveredText
            || !recoveredText.Contains("Already confirmed merged into integration")
            || !recoveredText.Contains("No new merge was performed")
            || !recoveredText.Contains("Saved completion:"))
            throw new InvalidDataException("Recovered merge hides recorded completion or claims a new merge.");
        Exception? workspaceFailure = null;
        var workspaceThread = new Thread(() => {
            try {
                var clientType = assembly.GetType("WatcherNativeProof.BackendClient", true)!;
                var client = Activator.CreateInstance(clientType, BindingFlags.Instance | BindingFlags.NonPublic,
                    null, new object[] { Array.Empty<string>(), true }, null)!;
                var windowType = assembly.GetType("WatcherNativeProof.BackendQueueWindow", true)!;
                var window = Activator.CreateInstance(windowType, BindingFlags.Instance | BindingFlags.NonPublic,
                    null, new object[] { client }, null)!;
                object Field(string name) => windowType.GetField(name, BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(window)!;
                var reviews = (IList)Field("_reviews");
                var assessment = (JsonObject)value.DeepClone();
                assessment["completion_stage"] = "assessment";
                assessment["completion"]!["assessment_required"] = true;
                assessment["preparation"]!["decision"]!["completion_scope"]!["assessment_required"] = true;
                reviews.Add(Parse(assessment));
                windowType.GetMethod("RenderActivity", BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(window, null);
                windowType.GetMethod("RenderCompletion", BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(window, null);
                var tabs = (TabControl)Field("_workspaces");
                if (!ReferenceEquals(tabs.SelectedItem, Field("_selectedWorkspace"))
                    || ((TabItem)Field("_maintenanceWorkspace")).Header.ToString() != "Maintenance · 1 pending")
                    throw new InvalidDataException("Historical pending work stole selection or lost its pending count.");
                var messages = ((StackPanel)Field("_completionActions")).Children.OfType<StackPanel>()
                    .SelectMany(row => row.Children.OfType<Grid>())
                    .SelectMany(block => block.Children.OfType<TextBlock>()).Select(text => text.Text);
                if (!messages.Any(text => text.Contains("No accepted post-merge assessment is saved")))
                    throw new InvalidDataException("Pending assessment hides its specific missing receipt.");
                var another = (JsonObject)assessment.DeepClone();
                another["review_id"] = "review_" + new string('f', 32);
                another["preparation"]!["binding"]!["pull_request"] = 43;
                another["result"]!["binding"]!["pull_request"] = 43;
                reviews.Add(Parse(another));
                windowType.GetMethod("RenderCompletion", BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(window, null);
                var completionRows = (IDictionary)Field("_completionRows");
                if (completionRows.Count != 2 || ((StackPanel)Field("_completionActions")).Children.Count != 2)
                    throw new InvalidDataException("One pending action replaced another action's controls.");
                var firstRow = (StackPanel)((StackPanel)Field("_completionActions")).Children[0];
                var firstActions = (StackPanel)firstRow.Children[1]; firstActions.Children.Clear();
                var secondRow = (StackPanel)((StackPanel)Field("_completionActions")).Children[1];
                if (((StackPanel)secondRow.Children[1]).Children.Count != 1)
                    throw new InvalidDataException("Updating one action's controls changed another pending action.");
                reviews.Clear(); reviews.Add(Parse(finished));
                windowType.GetMethod("RenderCompletion", BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(window, null);
                if (((StackPanel)Field("_completionPanel")).Visibility != Visibility.Collapsed
                    || ((TabItem)Field("_maintenanceWorkspace")).Header.ToString() != "Maintenance")
                    throw new InvalidDataException("Completed maintenance still presents a pending task.");
            } catch (Exception exception) { workspaceFailure = exception; }
        });
        workspaceThread.SetApartmentState(ApartmentState.STA); workspaceThread.Start(); workspaceThread.Join();
        if (workspaceFailure is not null) throw new InvalidDataException("Maintenance workspace contract failed.", workspaceFailure);
        Console.WriteLine("Maintenance display contracts passed: legacy records, pending restart, workspace isolation and exact action/branch binding.");
    }
}
