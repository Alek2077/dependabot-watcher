using System;
using System.IO;
using System.Reflection;
using System.Runtime.ExceptionServices;
using System.Text.Json.Nodes;

internal static class ConversationContracts
{
    private const string Repository = "example-org/example-app";
    private static readonly string Context = new('a', 64);
    private static readonly string OriginalId = "proposal_" + new string('b', 16);
    private static readonly string CurrentId = "proposal_" + new string('c', 16);

    internal static void Run(Assembly assembly)
    {
        var proposalType = assembly.GetType("WatcherNativeProof.QueueProposal", true)!;
        var viewType = assembly.GetType("WatcherNativeProof.NativeSupervisorView", true)!;
        object? Call(Type type, object? target, string method, params object?[] arguments)
        {
            try { return type.GetMethod(method, BindingFlags.NonPublic | BindingFlags.Static | BindingFlags.Instance)!
                .Invoke(target, arguments); }
            catch (TargetInvocationException failure) when (failure.InnerException is not null) {
                ExceptionDispatchInfo.Capture(failure.InnerException).Throw(); throw;
            }
        }
        object Proposal(string id, string repository = Repository, int number = 42, string kind = "pull_request") =>
            Call(proposalType, null, "Parse", JsonNode.Parse(new JsonObject {
                ["proposal_id"] = id, ["version"] = 0, ["plan_id"] = "plan_" + new string('a', 16),
                ["repository"] = repository, ["handling"] = "pending", ["evidence_status"] = "complete", ["feedback"] = new JsonArray(),
                ["step"] = new JsonObject { ["kind"] = kind, ["order"] = 1, ["summary"] = "Refreshed recommendation",
                    ["identity"] = new JsonObject { ["repository"] = repository, ["number"] = number, ["head_sha"] = new string('e', 40) } }
            }.ToJsonString()), "plan_" + new string('a', 16))!;
        var item = Proposal(CurrentId);
        var originalItem = new JsonObject { ["repository"] = Repository, ["kind"] = "pull_request", ["number"] = 42,
            ["head_sha"] = new string('d', 40), ["expected_base"] = "main", ["observed_base"] = "main" };
        var response = JsonNode.Parse(new JsonObject {
            ["mode"] = "supervisor_status", ["repository"] = Repository, ["context_digest"] = Context,
            ["capabilities"] = new JsonObject { ["model_calls"] = false, ["github_mutations"] = false },
            ["jobs"] = new JsonArray(new JsonObject {
                ["job_id"] = "job_" + new string('a', 32), ["context_digest"] = new string('f', 64),
                ["status"] = "completed", ["current_context"] = false, ["provider_completion_observed"] = true,
                ["messages"] = new JsonArray(new JsonObject { ["proposal_id"] = OriginalId,
                    ["request_id"] = "decision_1_saved", ["processing"] = "answered_historical",
                    ["question"] = "Did you receive my original question?", ["created_at"] = "2026-10-06T07:52:59Z", ["item"] = originalItem }),
                ["result"] = new JsonObject { ["summary"] = "Saved answer", ["recommendations"] = new JsonArray(new JsonObject {
                        ["proposal_id"] = OriginalId, ["conclusion"] = "hold", ["explanation"] = "Old revision advice" }),
                    ["replies"] = new JsonArray(new JsonObject { ["proposal_id"] = OriginalId, ["request_id"] = "decision_1_saved",
                        ["message"] = "Yes, I received it. No GitHub changes." }) },
                ["provenance"] = new JsonObject { ["provider"] = "fixture", ["model"] = "fixture-model",
                    ["reasoning_effort"] = "low", ["session_id"] = "fixture-session",
                    ["usage"] = new JsonObject { ["input_tokens"] = 0, ["cached_input_tokens"] = 0,
                        ["output_tokens"] = 0, ["reasoning_output_tokens"] = 0 } }
            })
        }.ToJsonString())!.AsObject();
        var view = Call(viewType, null, "Parse", response, Repository, Context)!;
        var text = (string)Call(viewType, view, "Describe", item)!;
        var latest = (string)Call(viewType, view, "LatestAnswer", item)!;
        if (!text.Contains("Did you receive my original question?") || !latest.Contains("Yes, I received it.")
            || !latest.Contains("earlier saved context") || text.Contains("Old revision advice"))
            throw new InvalidDataException("Refreshed item lost its historical conversation or promoted old advice.");
        foreach (var other in new[] { Proposal(CurrentId, "other/repository"), Proposal(CurrentId, number: 43), Proposal(CurrentId, kind: "alert") })
            if (Call(viewType, view, "LatestAnswer", other) is not null)
                throw new InvalidDataException("Historical answer leaked across repository, item number or kind.");
        var interrupted = (JsonObject)response.DeepClone();
        var interruptedJob = interrupted["jobs"]![0]!;
        interruptedJob["status"] = "interrupted";
        interruptedJob["observed_result"] = interruptedJob["result"]!.DeepClone();
        interruptedJob["result"] = null;
        var interruptedView = Call(viewType, null, "Parse", interrupted, Repository, Context)!;
        if (Call(viewType, null, "Result", interruptedJob.AsObject()) is not JsonObject received
            || received["replies"]!.AsArray().Count != 1)
            throw new InvalidDataException("Diagnostic result lookup lost an observation-only answer.");
        if (!((string)Call(viewType, interruptedView, "LatestAnswer", item)!).Contains("final job acceptance was not recorded")
            || !((string)Call(viewType, interruptedView, "Describe", item)!).Contains("Your question:"))
            throw new InvalidDataException("A retained observation after revocation broke conversation history.");
        var diagnosticType = assembly.GetType("WatcherNativeProof.NativeSupervisorProbe", true)!;
        Call(diagnosticType, null, "CheckPresentation", interruptedView, item, interrupted);
        var escaped = (JsonObject)interrupted.DeepClone();
        escaped["jobs"]![0]!["messages"]![0]!["question"] = "Question with \u202eBidi and \u200bformat";
        escaped["jobs"]![0]!["observed_result"]!["replies"]![0]!["message"] = "Answer with \u202eBidi and \u200bformat";
        var irrelevant = escaped["jobs"]![0]!.DeepClone();
        irrelevant["job_id"] = "job_" + new string('b', 32);
        irrelevant["messages"]![0]!["item"]!["number"] = 43;
        irrelevant["observed_result"]!["replies"]![0]!["message"] = "Unrelated answer";
        escaped["jobs"]!.AsArray().Add(irrelevant);
        var escapedView = Call(viewType, null, "Parse", escaped, Repository, Context)!;
        var matched = (JsonObject)Call(diagnosticType, null, "CheckPresentation", escapedView, item, escaped)!;
        if (matched["job_id"]!.GetValue<string>() != "job_" + new string('a', 32))
            throw new InvalidDataException("Diagnostic matched another item's more recent job.");
        var pending = (JsonObject)interrupted.DeepClone();
        pending["jobs"]![0]!["status"] = "running";
        if (Call(viewType, Call(viewType, null, "Parse", pending, Repository, Context), "LatestAnswer", item) is not null)
            throw new InvalidDataException("Unaccepted running observation was shown as a terminal answer.");
        foreach (var fault in new[] { "two_results", "unobserved", "accepted_status" })
        {
            var invalid = (JsonObject)interrupted.DeepClone();
            var invalidJob = invalid["jobs"]![0]!;
            if (fault == "two_results") invalidJob["result"] = invalidJob["observed_result"]!.DeepClone();
            else if (fault == "unobserved") invalidJob["provider_completion_observed"] = false;
            else invalidJob["status"] = "completed";
            var refused = false;
            try { Call(viewType, null, "Parse", invalid, Repository, Context); }
            catch (InvalidDataException) { refused = true; }
            if (!refused) throw new InvalidDataException("Contradictory observation-only acceptance: " + fault);
        }
        foreach (var fault in new[] { "repository", "kind", "number", "timestamp", "question", "head", "extra", "request", "processing" })
        {
            var invalid = (JsonObject)response.DeepClone();
            var receipt = invalid["jobs"]![0]!["messages"]![0]!;
            switch (fault) {
                case "repository": receipt["item"]!["repository"] = "other/repository"; break;
                case "kind": receipt["item"]!["kind"] = "command"; break;
                case "number": receipt["item"]!["number"] = 0; break;
                case "timestamp": receipt["created_at"] = "2026-10-06T07:52:59"; break;
                case "question": receipt["question"] = ""; break;
                case "head": receipt["item"]!["head_sha"] = "unknown"; break;
                case "extra": receipt["item"]!["command"] = "ignored fields forbidden"; break;
                case "request": receipt["request_id"] = "decision_2_unknown"; break;
                case "processing": receipt["processing"] = "answered_current"; break;
            }
            var refused = false;
            try { Call(viewType, null, "Parse", invalid, Repository, Context); }
            catch (InvalidDataException) { refused = true; }
            if (!refused) throw new InvalidDataException("Malformed conversation receipt accepted: " + fault);
        }
        var choices = Array.CreateInstance(proposalType, 2);
        choices.SetValue(Proposal("proposal_" + new string('d', 16), number: 43), 0);
        choices.SetValue(item, 1);
        var key = JsonNode.Parse(originalItem.ToJsonString())!.AsObject();
        key["repository"] = Repository.ToUpperInvariant();
        if (!ReferenceEquals(Call(proposalType, null, "RestoreSelection", choices, OriginalId, key), item))
            throw new InvalidDataException("Selection did not follow the same item across a recommendation revision.");
        Console.WriteLine("Conversation contracts passed: historical reply/selection continuity, scope isolation, observation-only recovery and 12 malformed receipts/states.");
    }
}
