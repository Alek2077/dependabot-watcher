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
        var rejected = new System.Collections.Generic.List<JsonObject> { new() {
            ["retry_available"] = true, ["failure"] = "output_contract_invalid"
        } };
        var rejectedView = Activator.CreateInstance(viewType, BindingFlags.Instance | BindingFlags.NonPublic,
            null, new object?[] { rejected, null }, null)!;
        var retryExplanation = viewType.GetProperty("RetryExplanation", BindingFlags.Instance | BindingFlags.NonPublic)!;
        if (retryExplanation.GetValue(rejectedView) is not string explanation
            || !explanation.Contains("answer failed validation") || !explanation.Contains("will not be repeated"))
            throw new InvalidDataException("Rejected-output recovery lacks specific visible guidance.");
        rejected[0]["failure"] = "interrupted";
        if (retryExplanation.GetValue(rejectedView) is not null)
            throw new InvalidDataException("Interrupted investigation was mislabeled as invalid output.");
        object? Call(Type type, object? target, string method, params object?[] arguments)
        {
            try { return type.GetMethod(method, BindingFlags.NonPublic | BindingFlags.Static | BindingFlags.Instance)!
                .Invoke(target, arguments); }
            catch (TargetInvocationException failure) when (failure.InnerException is not null) {
                ExceptionDispatchInfo.Capture(failure.InnerException).Throw(); throw;
            }
        }
        object Proposal(string id, string repository = Repository, int number = 42, string kind = "pull_request", string action = "validate_then_merge") =>
            Call(proposalType, null, "Parse", JsonNode.Parse(new JsonObject {
                ["proposal_id"] = id, ["version"] = 0, ["plan_id"] = "plan_" + new string('a', 16),
                ["repository"] = repository, ["handling"] = "pending", ["evidence_status"] = "complete", ["feedback"] = new JsonArray(),
                ["step"] = new JsonObject { ["kind"] = kind, ["action"] = action, ["order"] = 1, ["summary"] = "Refreshed recommendation",
                    ["identity"] = new JsonObject { ["repository"] = repository, ["number"] = number, ["head_sha"] = new string('e', 40) } }
            }.ToJsonString()), "plan_" + new string('a', 16))!;
        var item = Proposal(CurrentId);
        var list = (System.Collections.IList)Activator.CreateInstance(typeof(System.Collections.Generic.List<>).MakeGenericType(proposalType))!;
        list.Add(item);
        if (Call(proposalType, null, "RestoreSelection", list, OriginalId,
            new JsonObject { ["repository"] = Repository, ["kind"] = "alert", ["number"] = 99 }) is not null)
            throw new InvalidDataException("Retired selection silently retargeted the conversation to another item.");
        if (Call(proposalType, null, "RestoreSelection", list, null, null) != item)
            throw new InvalidDataException("Initial queue selection was not restored.");
        var investigationRequest = (string)proposalType.GetProperty("InvestigationRequest", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(item)!;
        if (!investigationRequest.Contains("PR #42") || !investigationRequest.Contains(Repository)
            || !investigationRequest.Contains("does not authorize merging"))
            throw new InvalidDataException("One-click investigation is not item scoped or fails to preserve action authority.");
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
        if (Call(viewType, view, "CurrentRecommendation", item) is not null)
            throw new InvalidDataException("Historical advice became a current recommendation.");
        var conversation = ((string, string, string, string))Call(viewType, view, "LatestConversation", item)!;
        if (!conversation.Item1.Contains("original question") || !conversation.Item2.Contains("No GitHub changes")
            || !conversation.Item4.Contains("Earlier conversation"))
            throw new InvalidDataException("Structured conversation lost its authorship or freshness.");
        var currentResponse = (JsonObject)response.DeepClone();
        var currentJob = currentResponse["jobs"]![0]!;
        currentJob["context_digest"] = Context; currentJob["current_context"] = true;
        currentJob["messages"] = new JsonArray(); currentJob["result"]!["replies"] = new JsonArray();
        var currentAdvice = currentJob["result"]!["recommendations"]![0]!;
        currentAdvice["proposal_id"] = CurrentId; currentAdvice["conclusion"] = "recommend"; currentAdvice["ready_for_action"] = true;
        var currentView = Call(viewType, null, "Parse", currentResponse, Repository, Context)!;
        var resourceResponse = (JsonObject)currentResponse.DeepClone();
        resourceResponse["jobs"]![0]!["validation_blocker"] = new JsonObject {
            ["job_id"] = resourceResponse["jobs"]![0]!["job_id"]!.GetValue<string>(),
            ["context_digest"] = Context, ["active"] = true,
            ["maximum_bytes"] = 2147483648L, ["minimum_free_bytes"] = 1073741824L,
            ["allocated_bytes_after_cleanup"] = 4096L
        };
        var resourceView = Call(viewType, null, "Parse", resourceResponse, Repository, Context)!;
        var resourceGuidance = (string?)viewType.GetProperty("ResourceGuidance", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(resourceView);
        if (resourceGuidance is null || !resourceGuidance.Contains("after cleanup") || !resourceGuidance.Contains("approved budget change")
            || !resourceGuidance.Contains("4 KiB") || !resourceGuidance.Contains("2 GiB"))
            throw new InvalidDataException("Storage blocker lacks measured provenance or bounded next step.");
        var retryResponse = (JsonObject)resourceResponse.DeepClone();
        var retry = (JsonObject)currentJob.DeepClone();
        retry["job_id"] = "job_" + new string('e', 32); retry["status"] = "prepared";
        retry["retry_of"] = currentJob["job_id"]!.GetValue<string>();
        retry["provider_completion_observed"] = false; retry["result"] = null; retry["provenance"] = null;
        retry["observed_result"] = null; retry["messages"] = new JsonArray();
        retryResponse["jobs"]!.AsArray().Add(retry);
        var retryView = Call(viewType, null, "Parse", retryResponse, Repository, Context)!;
        if (viewType.GetProperty("ResourceGuidance", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(retryView) is not string)
            throw new InvalidDataException("Prepared retry hid an unresolved same-context storage refusal.");
        retry["validation_blocker"] = resourceResponse["jobs"]![0]!["validation_blocker"]!.DeepClone();
        retry["validation_blocker"]!["job_id"] = retry["job_id"]!.GetValue<string>();
        retry["validation_blocker"]!["active"] = false;
        var clearedView = Call(viewType, null, "Parse", retryResponse, Repository, Context)!;
        if (viewType.GetProperty("ResourceGuidance", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(clearedView) is not null)
            throw new InvalidDataException("Successful validation did not clear an earlier same-context refusal.");
        retry["validation_blocker"] = null; retry["retry_of"] = null;
        var independentView = Call(viewType, null, "Parse", retryResponse, Repository, Context)!;
        if (viewType.GetProperty("ResourceGuidance", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(independentView) is not null)
            throw new InvalidDataException("Independent investigation inherited another job's storage warning.");
        resourceResponse["jobs"]![0]!["validation_blocker"]!["context_digest"] = new string('f', 64);
        var resourceRefused = false;
        try { Call(viewType, null, "Parse", resourceResponse, Repository, Context); }
        catch (InvalidDataException) { resourceRefused = true; }
        if (!resourceRefused) throw new InvalidDataException("Storage blocker crossed its saved job context.");
        if (viewType.GetProperty("CurrentAssessmentSummary", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(view) is not null
            || (string?)viewType.GetProperty("CurrentAssessmentSummary", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(currentView) != "Saved answer")
            throw new InvalidDataException("Post-merge summary promoted historical context or lost accepted current assessment.");
        var recommendation = ((string, string, bool))Call(viewType, currentView, "CurrentRecommendation", item)!;
        if (!recommendation.Item3 || recommendation.Item1 != "Ready for merge review")
            throw new InvalidDataException("Verified current readiness was not displayed.");
        var guidance = (string)Call(viewType, currentView, "ActionGuidance", item, true)!;
        if (!guidance.StartsWith("Next: review merge into ") || guidance.Length > 120)
            throw new InvalidDataException("Action guidance is not a concise merge-review step.");
        if (!((string)Call(viewType, currentView, "ActionGuidance", item, false)!).Contains("disabled"))
            throw new InvalidDataException("Read-only guidance incorrectly offers merge approval.");
        foreach (var nonMerge in new[] { Proposal(CurrentId, kind: "alert", action: "investigate"), Proposal(CurrentId, action: "human_attention") })
        {
            var advice = ((string, string, bool))Call(viewType, currentView, "CurrentRecommendation", nonMerge)!;
            if (advice.Item3 || advice.Item1 == "Ready for merge review")
                throw new InvalidDataException("Non-merge advice was presented as PR merge approval.");
            if (((string)Call(viewType, currentView, "ActionGuidance", nonMerge, true)!).Contains("merge"))
                throw new InvalidDataException("Non-merge guidance offers merge consent.");
        }
        if (Call(viewType, currentView, "CurrentRecommendation", Proposal(OriginalId)) is not null)
            throw new InvalidDataException("Current advice leaked to a different proposal revision.");
        currentAdvice["ready_for_action"] = false;
        currentView = Call(viewType, null, "Parse", currentResponse, Repository, Context)!;
        if (((string, string, bool))Call(viewType, currentView, "CurrentRecommendation", item)! is var notReady && notReady.Item3)
            throw new InvalidDataException("A recommendation without readiness was promoted.");
        if ((string)Call(viewType, currentView, "ActionGuidance", item, true)! != "Next: continue investigation."
            || (string)Call(viewType, currentView, "ActionGuidance", Proposal(OriginalId), true)! != "Next: start investigation.")
            throw new InvalidDataException("Completed partial investigation was mislabeled as not started.");
        foreach (var quietConclusion in new[] { "hold", "no_action" })
        {
            currentAdvice["conclusion"] = quietConclusion;
            currentView = Call(viewType, null, "Parse", currentResponse, Repository, Context)!;
            if ((bool)Call(viewType, currentView, "SuggestsInvestigation", item)!
                || ((string)Call(viewType, currentView, "ActionGuidance", item, true)!).Contains("investigation"))
                throw new InvalidDataException("A hold/no-action result encouraged paid reinvestigation.");
        }
        if (!(bool)Call(viewType, view, "SuggestsInvestigation", item)!)
            throw new InvalidDataException("Missing current advice did not offer investigation.");
        foreach (var activeStatus in new[] { "prepared", "running" })
        {
            var activeResponse = (JsonObject)currentResponse.DeepClone();
            var activeJob = activeResponse["jobs"]![0]!;
            activeJob["status"] = activeStatus; activeJob["result"] = null;
            activeJob["provenance"] = null; activeJob["provider_completion_observed"] = false;
            var activeView = Call(viewType, null, "Parse", activeResponse, Repository, Context)!;
            var isRunning = activeStatus == "running";
            if ((bool)viewType.GetProperty("HasActiveInvestigation", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(activeView)! != isRunning
                || (bool)Call(viewType, activeView, "SuggestsInvestigation", item)! == isRunning)
                throw new InvalidDataException("An active saved job offered another investigation.");
            if (!isRunning && viewType.GetProperty("HasPreparedInvestigation", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(activeView) is not true)
                throw new InvalidDataException("An unstarted prepared job was hidden instead of offering explicit investigation.");
            if (activeStatus == "running")
            {
                activeJob["context_digest"] = new string('f', 64);
                activeJob["current_context"] = false;
                var staleActiveView = Call(viewType, null, "Parse", activeResponse, Repository, Context)!;
                if (!(bool)viewType.GetProperty("HasActiveInvestigation", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(staleActiveView)!
                    || (bool)Call(viewType, staleActiveView, "SuggestsInvestigation", item)!
                    || Call(viewType, staleActiveView, "CurrentRecommendation", item) is not null)
                    throw new InvalidDataException("Stale running ownership was lost or promoted to current advice.");
                activeJob["status"] = "superseded";
                var finishedStaleView = Call(viewType, null, "Parse", activeResponse, Repository, Context)!;
                if ((bool)viewType.GetProperty("HasActiveInvestigation", BindingFlags.NonPublic | BindingFlags.Instance)!.GetValue(finishedStaleView)!)
                    throw new InvalidDataException("Terminal stale history retained investigation ownership.");
            }
        }
        currentAdvice["ready_for_action"] = true; currentAdvice["conclusion"] = "hold";
        var contradictionRefused = false;
        try { Call(viewType, null, "Parse", currentResponse, Repository, Context); }
        catch (InvalidDataException) { contradictionRefused = true; }
        if (!contradictionRefused) throw new InvalidDataException("Contradictory readiness was accepted.");
        if (!text.Contains("Did you receive my original question?") || !latest.Contains("Yes, I received it.")
            || !latest.Contains("earlier saved context") || text.Contains("Old revision advice"))
            throw new InvalidDataException("Refreshed item lost its historical conversation or promoted old advice.");
        foreach (var other in new[] { Proposal(CurrentId, "other/repository"), Proposal(CurrentId, number: 43), Proposal(CurrentId, kind: "alert") })
            if (Call(viewType, view, "LatestAnswer", other) is not null || Call(viewType, view, "LatestConversation", other) is not null)
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
