using System;
using System.IO;
using System.Text.Json.Nodes;

namespace WatcherNativeProof;

// Deterministic consumer contract test, not a model or repository operation.
internal static class NativePermissionProbe
{
    internal static void Run()
    {
        const string repository = "example-org/example-app";
        var context = new string('a', 64);
        var permissionId = "permission_" + new string('b', 32);
        var response = new JsonObject {
            ["mode"] = "supervisor_permissions", ["repository"] = repository,
            ["capabilities"] = new JsonObject { ["model_calls"] = false, ["github_mutations"] = false },
            ["connections"] = new JsonArray(new JsonObject {
                ["connection_id"] = "connection_" + new string('c', 32),
                ["job_id"] = "job_" + new string('d', 32), ["session_id"] = "thread1", ["turn_id"] = "turn1",
                ["context_digest"] = context, ["current_context"] = true, ["status"] = "active",
                ["job_status"] = "running", ["live_connection_confirmed"] = true,
                ["requests"] = new JsonObject { [permissionId] = new JsonObject {
                    ["permission_id"] = permissionId, ["thread_id"] = "thread1", ["turn_id"] = "turn1", ["item_id"] = "item1",
                    ["digest"] = new string('e', 64), ["status"] = "waiting", ["description"] = "Synthetic read",
                    ["choices"] = new JsonArray("allow_once", "deny", "cancel"), ["decision"] = null
                }}
            })
        };
        var parsed = NativePermissionView.Parse(response, repository, context);
        if (parsed.Count != 1 || parsed[0].PermissionId != permissionId || parsed[0].Choices.Count != 3)
            throw new InvalidDataException("Valid live permission was not presented exactly.");
        var checkedFaults = 0;
        foreach (var fault in new[] { "owner_gone", "owner_missing", "stale_context", "terminal_job",
            "wrong_thread", "wrong_turn", "missing_item", "empty_choices", "broader_choice", "foreign_repository", "closed_live" })
        {
            var invalid = (JsonObject)response.DeepClone();
            var connection = invalid["connections"]![0]!;
            var request = connection["requests"]![permissionId]!;
            switch (fault)
            {
                case "owner_gone": connection["live_connection_confirmed"] = false; break;
                case "owner_missing": connection.AsObject().Remove("live_connection_confirmed"); break;
                case "stale_context": connection["context_digest"] = new string('f', 64); connection["current_context"] = false; break;
                case "terminal_job": connection["job_status"] = "interrupted"; break;
                case "wrong_thread": request["thread_id"] = "foreign_thread"; break;
                case "wrong_turn": request["turn_id"] = "foreign_turn"; break;
                case "missing_item": request.AsObject().Remove("item_id"); break;
                case "empty_choices": request["choices"] = new JsonArray(); break;
                case "broader_choice": request["choices"] = new JsonArray("allow_all"); break;
                case "foreign_repository": invalid["repository"] = "another/repository"; break;
                case "closed_live": connection["status"] = "closed"; break;
            }
            var rejected = false;
            try { NativePermissionView.Parse(invalid, repository, context); }
            catch (InvalidDataException) { rejected = true; }
            if (!rejected) throw new InvalidDataException($"Unsafe permission frame was accepted: {fault}");
            checkedFaults++;
        }
        var history = (JsonObject)response.DeepClone();
        var retired = history["connections"]![0]!;
        retired["status"] = "closed"; retired["job_status"] = "interrupted";
        retired["live_connection_confirmed"] = false;
        retired["requests"]![permissionId]!["status"] = "invalidated";
        if (NativePermissionView.Parse(history, repository, context).Count != 0)
            throw new InvalidDataException("Invalidated history became actionable.");
        var receipt = (JsonObject)response.DeepClone();
        receipt["mode"] = "supervisor_permission_decide";
        var target = receipt["connections"]![0]!["requests"]![permissionId]!;
        target["decision"] = "allow_once";
        foreach (var status in new[] { "decided", "consumed", "resolved", "invalidated" })
        {
            target["status"] = status;
            var connection = receipt["connections"]![0]!;
            connection["status"] = status == "invalidated" ? "closed" : "active";
            connection["job_status"] = status == "invalidated" ? "interrupted" : "running";
            connection["live_connection_confirmed"] = status != "invalidated";
            var expectedState = status switch { "decided" => "saved_waiting_delivery",
                "consumed" => "sent_waiting_resolution", _ => status };
            if (NativePermissionView.ParseDecision(receipt, repository, context, parsed[0], "allow_once") != expectedState)
                throw new InvalidDataException("Exact decision acknowledgement lost its actual delivery state.");
        }
        var completedReceipt = (JsonObject)receipt.DeepClone();
        completedReceipt["connections"]![0]!["job_status"] = "completed";
        completedReceipt["connections"]![0]!["requests"]![permissionId]!["status"] = "resolved";
        if (NativePermissionView.ParseDecision(completedReceipt, repository, context, parsed[0], "allow_once") != "resolved")
            throw new InvalidDataException("Completed owner history lost its confirmed resolution.");
        var redirected = (JsonObject)receipt.DeepClone();
        redirected["connections"]![0]!["requests"]![permissionId]!["digest"] = new string('f', 64);
        var redirectedRejected = false;
        try { NativePermissionView.ParseDecision(redirected, repository, context, parsed[0], "allow_once"); }
        catch (InvalidDataException) { redirectedRejected = true; }
        if (!redirectedRejected) throw new InvalidDataException("A different request was acknowledged as the submitted decision.");
        App.Record("permission_consumer_probe_passed", new { rejected_frames = checkedFaults,
            valid_live_request = true, invalidated_history_nonactionable = true,
            exact_acknowledgement_states = 4, redirected_receipt_rejected = true, model_calls = 0, github_mutations = false });
    }
}
