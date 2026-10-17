using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace WatcherNativeProof;

// A display-only consumer. Provider text never becomes an executable, approval,
// queue order or local decision. Backend receipts are the delivery authority.
internal sealed class NativeSupervisorView
{
    private readonly List<JsonObject> _jobs;
    internal NativeLaunchPolicy? LaunchPolicy { get; }
    internal string? ResourceGuidance {
        get {
            // A same-context retry must not hide an unresolved prior refusal.
            // A later explicit cleared marker ends that historical warning.
            var job = _jobs.LastOrDefault(value => value["current_context"]?.GetValue<bool>() == true);
            var seen = new HashSet<string>();
            while (job is not null && job["validation_blocker"] is not JsonObject) {
                if (!seen.Add(job["job_id"]!.GetValue<string>())
                    || job["retry_of"] is not JsonValue retry || !retry.TryGetValue<string>(out var parent))
                    return null;
                job = _jobs.LastOrDefault(value => value["job_id"]?.GetValue<string>() == parent
                    && value["current_context"]?.GetValue<bool>() == true);
            }
            if (job?["validation_blocker"] is not JsonObject blocker || blocker["active"]?.GetValue<bool>() != true)
                return null;
            var used = blocker["allocated_bytes_after_cleanup"]!.GetValue<long>();
            var limit = blocker["maximum_bytes"]!.GetValue<long>();
            var reserve = blocker["minimum_free_bytes"]!.GetValue<long>();
            return $"Saved allocation after cleanup: {StorageSize(used)}\nBudget: {StorageSize(limit)} · Free-space reserve: {StorageSize(reserve)}\n"
                + "Choose a smaller validation scope or review storage before an explicitly approved budget change. "
                + "This is saved evidence, not a live disk reading. No files or limits change automatically.\n"
                + $"Source investigation: {job["job_id"]}";
        }
    }
    private static string StorageSize(long bytes) => bytes >= 1073741824L ? $"{bytes / 1073741824d:0.##} GiB"
        : bytes >= 1048576L ? $"{bytes / 1048576d:0.##} MiB"
        : bytes >= 1024L ? $"{bytes / 1024d:0.##} KiB" : $"{bytes} bytes";
    internal bool HasActiveInvestigation {
        get {
            // Ownership survives context changes; freshness only controls
            // whether a job's advice can be presented as current.
            if (_jobs.Any(value => value["status"]?.GetValue<string>() == "running")) return true;
            return false;
        }
    }
    internal bool HasPreparedInvestigation => _jobs.LastOrDefault(job => job["current_context"]?.GetValue<bool>() == true)
        ?["status"]?.GetValue<string>() == "prepared";
    private JsonObject? ActiveJob => _jobs.LastOrDefault(job => job["status"]?.GetValue<string>() == "running");
    internal string? ActiveJobId => ActiveJob?["job_id"]?.GetValue<string>();
    internal DateTimeOffset? ActiveStartedAt {
        get {
            var active = ActiveJob;
            return DateTimeOffset.TryParse(active?["created_at"]?.GetValue<string>(), out var value) ? value : null;
        }
    }
    internal string? CurrentAssessmentSummary
    {
        get
        {
            var job = _jobs.LastOrDefault(value => value["current_context"]!.GetValue<bool>());
            return job is not null && job["status"]!.GetValue<string>() == "completed"
                && job["provider_completion_observed"]!.GetValue<bool>() && !ObservationOnly(job)
                ? NativeDisplayText.Escape(Result(job)!["summary"]!.GetValue<string>()) : null;
        }
    }
    internal string? RetryJobId => _jobs.SingleOrDefault(job => job["retry_available"]?.GetValue<bool>() == true)?["job_id"]?.GetValue<string>();
    internal string? RetryExplanation {
        get {
            var job = _jobs.SingleOrDefault(value => value["retry_available"]?.GetValue<bool>() == true);
            return job?["failure"]?.GetValue<string>() == "output_contract_invalid"
                ? "The agent finished, but its answer failed validation. No recommendation or maintenance completion was accepted. Retry requests a new checked answer; completed merges and local updates will not be repeated."
                : null;
        }
    }

    private NativeSupervisorView(List<JsonObject> jobs, NativeLaunchPolicy? launchPolicy)
    { _jobs = jobs; LaunchPolicy = launchPolicy; }

    internal static NativeSupervisorView Parse(JsonObject response, string repository, string expectedContextDigest)
    {
        if (response["mode"]?.GetValue<string>() != "supervisor_status"
            || response["repository"]?.GetValue<string>() != repository
            || response["capabilities"]?["model_calls"]?.GetValue<bool>() != false
            || response["capabilities"]?["github_mutations"]?.GetValue<bool>() != false
            || !Regex.IsMatch(Text(response["context_digest"], 64), "^[a-f0-9]{64}$")
            || response["context_digest"]!.GetValue<string>() != expectedContextDigest
            || response["jobs"] is not JsonArray jobs || jobs.Count > 50)
            throw new InvalidDataException("Agent reply scope or capability contract is invalid.");
        var parsed = new List<JsonObject>();
        var ids = new HashSet<string>();
        foreach (var value in jobs)
        {
            var job = value as JsonObject ?? throw new InvalidDataException("Invalid agent job.");
            var id = Text(job["job_id"], 36);
            var status = Text(job["status"], 20);
            if (!Regex.IsMatch(id, "^job_[a-f0-9]{32}$") || !ids.Add(id)
                || !Regex.IsMatch(Text(job["context_digest"], 64), "^[a-f0-9]{64}$")
                || status is not ("prepared" or "running" or "completed" or "superseded" or "failed" or "interrupted")
                || job["current_context"] is not JsonValue current || !current.TryGetValue<bool>(out var isCurrent)
                || isCurrent != (job["context_digest"]!.GetValue<string>() == expectedContextDigest)
                || job["provider_completion_observed"] is not JsonValue observed || !observed.TryGetValue<bool>(out var hasCompletion)
                || job["messages"] is not JsonArray messages || messages.Count > 2000)
                throw new InvalidDataException("Invalid agent job identity or completion.");
            var observationOnly = ObservationOnly(job);
            if (job["validation_blocker"] is JsonNode resource)
            {
                if (resource is not JsonObject blocker || blocker.Count != 6
                    || blocker["job_id"]?.GetValue<string>() != id
                    || blocker["context_digest"]?.GetValue<string>() != job["context_digest"]!.GetValue<string>()
                    || blocker["active"] is not JsonValue active || !active.TryGetValue<bool>(out _))
                    throw new InvalidDataException("Validation storage evidence is not bound to this job.");
                foreach (var name in new[] { "maximum_bytes", "minimum_free_bytes", "allocated_bytes_after_cleanup" })
                    if (blocker[name] is not JsonValue number || !number.TryGetValue<long>(out var amount) || amount < 0)
                        throw new InvalidDataException("Invalid validation storage amount.");
                if (blocker["maximum_bytes"]!.GetValue<long>() is < 1 or > 17179869184L
                    || blocker["minimum_free_bytes"]!.GetValue<long>() is < 1 or > 68719476736L)
                    throw new InvalidDataException("Invalid saved validation storage limits.");
            }
            if (job["observed_result"] is not null && (!hasCompletion || !observationOnly
                || status is not ("running" or "failed" or "interrupted")))
                throw new InvalidDataException("Observation-only data contradicts job acceptance or provider evidence.");
            if (Result(job) is JsonObject result)
            {
                Text(result["summary"], 8192);
                if (result["recommendations"] is not JsonArray recommendations || recommendations.Count > 1000
                    || result["replies"] is not JsonArray replies || replies.Count > 2000)
                    throw new InvalidDataException("Agent result exceeds its display contract.");
                var proposals = new HashSet<string>();
                foreach (var recommendation in recommendations)
                {
                    var proposal = Text(recommendation?["proposal_id"], 25);
                    if (!Regex.IsMatch(proposal, "^proposal_[a-f0-9]{16}$") || !proposals.Add(proposal)
                        || Text(recommendation?["conclusion"], 20) is not ("recommend" or "investigate" or "hold" or "no_action"))
                        throw new InvalidDataException("Invalid agent recommendation.");
                    Text(recommendation?["explanation"], 8192);
                    if (recommendation?["ready_for_action"] is JsonNode ready
                        && (ready is not JsonValue flag || !flag.TryGetValue<bool>(out var isReady)
                            || (isReady && recommendation?["conclusion"]?.GetValue<string>() != "recommend")))
                        throw new InvalidDataException("Invalid agent readiness claim.");
                }
                var answered = new HashSet<string>();
                foreach (var reply in replies)
                {
                    var key = ReplyKey(reply);
                    if (!answered.Add(key)) throw new InvalidDataException("Repeated agent answer.");
                    Text(reply?["message"], 8192);
                }
                var receipts = new HashSet<string>();
                foreach (var receipt in messages)
                {
                    var key = ReplyKey(receipt);
                    if (!hasCompletion || !answered.Contains(key) || !receipts.Add(key)
                        || receipt?["processing"]?.GetValue<string>() != (isCurrent && status == "completed" && !observationOnly
                            ? "answered_current" : "answered_historical"))
                        throw new InvalidDataException("Agent answer receipt does not match its result.");
                    ValidateReceipt(receipt!.AsObject(), repository);
                }
                if (hasCompletion && !receipts.SetEquals(answered))
                    throw new InvalidDataException("Agent answers lack processing receipts.");
            }
            else if (job["result"] is not null || messages.Count != 0)
                throw new InvalidDataException("Agent reply has no recorded result.");
            if (hasCompletion && Result(job) is not JsonObject)
                throw new InvalidDataException("Observed agent completion has no saved result.");
            if (hasCompletion)
            {
                var provenance = job["provenance"] as JsonObject
                    ?? throw new InvalidDataException("Agent completion lacks provenance.");
                foreach (var name in new[] { "provider", "model", "reasoning_effort", "session_id" }) Text(provenance[name], 128);
                var usage = provenance["usage"] as JsonObject ?? throw new InvalidDataException("Agent completion lacks usage.");
                foreach (var name in new[] { "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens" })
                    if (usage[name] is not JsonValue count || !count.TryGetValue<long>(out var number) || number < 0)
                        throw new InvalidDataException("Invalid agent usage observation.");
            }
            else if (job["provenance"] is not null || messages.Count != 0)
                throw new InvalidDataException("Agent delivery was not observed.");
            parsed.Add(job);
            if (job["retry_available"]?.GetValue<bool>() == true
                && (!isCurrent || status is not ("failed" or "interrupted")))
                throw new InvalidDataException("Retry does not belong to a current failed investigation.");
        }
        if (parsed.Count(job => job["retry_available"]?.GetValue<bool>() == true) > 1)
            throw new InvalidDataException("Agent status has ambiguous retry authority.");
        var launchPolicy = response["configured_launch"] is JsonObject policy ? NativeLaunchPolicy.Parse(policy) : null;
        return new NativeSupervisorView(parsed, launchPolicy);
    }

    private static string Text(JsonNode? node, int bytes)
    {
        if (node is not JsonValue value || !value.TryGetValue<string>(out var text)
            || string.IsNullOrWhiteSpace(text) || Encoding.UTF8.GetByteCount(text) > bytes)
            throw new InvalidDataException("Agent reply contains invalid bounded text.");
        return text;
    }

    private static string ReplyKey(JsonNode? reply) => Text(reply?["proposal_id"], 25) + "/" + Text(reply?["request_id"], 128);
    private static bool ObservationOnly(JsonObject job) => job["result"] is null && job["observed_result"] is JsonObject;
    internal static JsonObject? Result(JsonObject job) => job["result"] as JsonObject ?? job["observed_result"] as JsonObject;

    private static void ValidateReceipt(JsonObject receipt, string repository)
    {
        var keys = new HashSet<string> { "proposal_id", "request_id", "question", "created_at", "item", "processing" };
        if (!keys.SetEquals(receipt.Select(entry => entry.Key))
            || !Regex.IsMatch(Text(receipt["proposal_id"], 25), "^proposal_[a-f0-9]{16}$")
            || !Regex.IsMatch(Text(receipt["request_id"], 128), "^decision_[1-9][0-9]{0,18}_[A-Za-z0-9_-]{1,32}$")
            || receipt["item"] is not JsonObject item
            || !new HashSet<string> { "repository", "kind", "number", "head_sha", "expected_base", "observed_base" }
                .SetEquals(item.Select(entry => entry.Key))
            || !string.Equals(Text(item["repository"], 255), repository, StringComparison.OrdinalIgnoreCase)
            || Text(item["kind"], 20) is not ("pull_request" or "alert")
            || item["number"] is not JsonValue number || !number.TryGetValue<int>(out var value) || value <= 0)
            throw new InvalidDataException("Saved question has invalid conversation identity.");
        Text(receipt["question"], 8192);
        var at = Text(receipt["created_at"], 128);
        if (!Regex.IsMatch(at, "(Z|[+-][0-9]{2}:[0-9]{2})$") || !DateTimeOffset.TryParse(at, out _))
            throw new InvalidDataException("Saved question lacks an explicit timestamp.");
        if (item["head_sha"] is not null && !Regex.IsMatch(Text(item["head_sha"], 64), "^([a-fA-F0-9]{40}|[a-fA-F0-9]{64})$"))
            throw new InvalidDataException("Saved question has invalid revision evidence.");
        foreach (var name in new[] { "expected_base", "observed_base" })
            if (item[name] is not null) Text(item[name], 1024);
        if (item["kind"]!.GetValue<string>() == "alert"
            && new[] { "head_sha", "expected_base", "observed_base" }.Any(name => item[name] is not null))
            throw new InvalidDataException("An alert question cannot claim PR revision evidence.");
    }

    internal string DescribeTerminal(JsonObject terminal)
    {
        var job = _jobs.SingleOrDefault(value => value["job_id"]?.GetValue<string>() == terminal["job_id"]?.GetValue<string>());
        if (job is null || job["context_digest"]?.GetValue<string>() != terminal["context_digest"]?.GetValue<string>())
            throw new InvalidDataException("The terminal investigation receipt does not match an independently saved job.");
        var status = job["status"]!.GetValue<string>();
        var reportedStatus = terminal["job_status"]?.GetValue<string>();
        if (status != reportedStatus && !(reportedStatus == "completed" && status == "superseded"))
            throw new InvalidDataException("The investigation stream contradicts its independently saved outcome.");
        if (job["provider_completion_observed"]!.GetValue<bool>() && (status is "completed" or "superseded"))
            return (job["current_context"]!.GetValue<bool>() ? "Agent results are saved. Review the answers and recommendations below; each merge still needs its own exact confirmation."
                : "Agent results are saved for an earlier context. They are historical, not a current merge recommendation.");
        return $"Investigation is recorded as {status}. No verified completed recommendation is being claimed. Refresh saved state to inspect recovery; nothing is retried automatically.";
    }

    internal string Describe(QueueProposal item)
    {
        var sections = new List<string>();
        foreach (var failed in _jobs.Where(job => job["current_context"]!.GetValue<bool>()
                     && job["status"]!.GetValue<string>() is "failed" or "interrupted"))
            sections.Add((failed["failure"]?.GetValue<string>() == "output_contract_invalid"
                ? "The agent finished, but its answer failed validation. No recommendation or maintenance completion was accepted. "
                : $"Investigation {failed["status"]}. No completed recommendation is claimed. ")
                + (failed["retry_available"]?.GetValue<bool>() == true
                    ? "Use Retry investigation for a fresh checked attempt; the failed record is retained."
                    : "This earlier attempt is retained; check the newer attempt's outcome."));
        var questions = item.Evidence["feedback"]!.AsArray()
            .ToDictionary(message => message!["request_id"]!.GetValue<string>(), message => message!["message"]!.GetValue<string>());
        foreach (var job in _jobs.Where(job => job["provider_completion_observed"]!.GetValue<bool>()
                     && job["status"]!.GetValue<string>() is "completed" or "superseded" or "failed" or "interrupted").AsEnumerable().Reverse())
        {
            if (Result(job) is not JsonObject result) continue;
            var observationOnly = ObservationOnly(job);
            var recommendations = result["recommendations"]!.AsArray()
                .Where(value => !observationOnly && value?["proposal_id"]?.GetValue<string>() == item.Id).ToList();
            var receipts = job["messages"]!.AsArray().ToDictionary(ReplyKey, value => value!.AsObject());
            var replies = result["replies"]!.AsArray()
                .Where(value => item.SameItem(receipts[ReplyKey(value)]["item"]!.AsObject())).ToList();
            var current = job["current_context"]!.GetValue<bool>() && job["status"]!.GetValue<string>() == "completed" && !observationOnly;
            if (recommendations.Count == 0 && replies.Count == 0 && !current) continue;
            var source = job["provenance"]!;
            var content = $"{source["model"]} · {source["reasoning_effort"]} · {source["provider"]}\n"
                + (observationOnly ? "Provider answer was received, but final job acceptance was not recorded. Historical conversation only; no current recommendation."
                    : current ? "Agent answered the saved context. Recheck the PR before any action."
                    : "Earlier context — retained for reference, not a current approval recommendation.");
            if (current) content += $"\n\nRepository assessment: {result["summary"]}";
            foreach (var recommendation in recommendations)
                content += $"\n\nAgent recommendation: {recommendation!["conclusion"]}\n{recommendation["explanation"]}";
            foreach (var reply in replies)
            {
                var receipt = receipts[ReplyKey(reply)];
                var sameRevision = receipt["proposal_id"]!.GetValue<string>() == item.Id;
                if (sameRevision && (!questions.TryGetValue(receipt["request_id"]!.GetValue<string>(), out var original)
                    || original != receipt["question"]!.GetValue<string>()))
                    throw new InvalidDataException("Saved question contradicts the selected proposal's feedback.");
                content += $"\n\nYour question: {receipt["question"]}\nAgent answer: {reply!["message"]}";
                if (!sameRevision)
                    content += $"\nAnswered question from an earlier recommendation revision ({receipt["created_at"]}). "
                        + "This reply is retained for this same item; it does not approve the current recommendation.";
            }
            sections.Add(content);
        }
        return NativeDisplayText.Escape(sections.Count == 0 ? "No verified agent answer for this item. Saving a message alone does not launch or complete an agent job."
            : string.Join("\n\n────────\n\n", sections) + "\n\nReading these replies makes no model call or GitHub change.");
    }

    internal string? LatestAnswer(QueueProposal item)
    {
        foreach (var job in _jobs.AsEnumerable().Reverse().Where(job => job["provider_completion_observed"]!.GetValue<bool>()
                     && job["status"]!.GetValue<string>() is "completed" or "superseded" or "failed" or "interrupted"))
        {
            var receipts = job["messages"]!.AsArray().ToDictionary(ReplyKey, value => value!.AsObject());
            var reply = Result(job)!["replies"]!.AsArray().Reverse()
                .FirstOrDefault(value => item.SameItem(receipts[ReplyKey(value)]["item"]!.AsObject()));
            if (reply is null) continue;
            var receipt = receipts[ReplyKey(reply)];
            var revision = receipt["proposal_id"]!.GetValue<string>() == item.Id;
            var source = job["provenance"]!;
            return NativeDisplayText.Escape($"{source["model"]} · {source["reasoning_effort"]}\n\nYour question: {receipt["question"]}\n\nAgent: {reply["message"]}\n\n"
                + (ObservationOnly(job) ? "Provider answer received; final job acceptance was not recorded. Historical conversation only, not a current recommendation or approval."
                    : revision && job["current_context"]!.GetValue<bool>() && job["status"]!.GetValue<string>() == "completed" ? "Verified answer to the saved current context. No merge approval."
                    : "Verified answer to an earlier saved context for this same item. Not a current merge recommendation or approval."));
        }
        return null;
    }

    // Current advice is deliberately separate from historical conversation.
    // It never grants authority or changes the planner's order.
    private JsonNode? CurrentAdvice(QueueProposal item)
    {
        var job = _jobs.LastOrDefault(value => value["current_context"]!.GetValue<bool>());
        if (job is null || job["status"]!.GetValue<string>() != "completed"
            || !job["provider_completion_observed"]!.GetValue<bool>() || ObservationOnly(job)) return null;
        var recommendation = Result(job)!["recommendations"]!.AsArray()
            .SingleOrDefault(value => value?["proposal_id"]?.GetValue<string>() == item.Id);
        return recommendation;
    }

    internal (string Heading, string Explanation, bool Ready)? CurrentRecommendation(QueueProposal item)
    {
        var recommendation = CurrentAdvice(item);
        if (recommendation is null) return null;
        var providerReady = recommendation["ready_for_action"]?.GetValue<bool>() == true;
        var mergeStep = item.Kind == "PR" && item.Step["action"]?.GetValue<string>() == "validate_then_merge";
        var ready = providerReady && mergeStep;
        var heading = recommendation["conclusion"]!.GetValue<string>() switch {
            "recommend" when ready => "Ready for merge review",
            "recommend" when providerReady => "Action needed",
            "hold" => "On hold",
            "no_action" => "No action recommended",
            _ => "Further investigation needed"
        };
        return (heading, NativeDisplayText.Escape(recommendation["explanation"]!.GetValue<string>()), ready);
    }

    internal (string Question, string Answer, string Source, string Freshness)? LatestConversation(QueueProposal item)
    {
        foreach (var job in _jobs.AsEnumerable().Reverse().Where(value => value["provider_completion_observed"]!.GetValue<bool>()
            && value["status"]!.GetValue<string>() is "completed" or "superseded" or "failed" or "interrupted"))
        {
            var receipts = job["messages"]!.AsArray().ToDictionary(ReplyKey, value => value!.AsObject());
            var reply = Result(job)!["replies"]!.AsArray().Reverse()
                .FirstOrDefault(value => item.SameItem(receipts[ReplyKey(value)]["item"]!.AsObject()));
            if (reply is null) continue;
            var receipt = receipts[ReplyKey(reply)];
            var current = !ObservationOnly(job) && job["status"]!.GetValue<string>() == "completed"
                && job["current_context"]!.GetValue<bool>() && receipt["proposal_id"]!.GetValue<string>() == item.Id;
            return (NativeDisplayText.Escape(receipt["question"]!.GetValue<string>()),
                NativeDisplayText.Escape(reply["message"]!.GetValue<string>()),
                NativeDisplayText.Escape($"{job["provenance"]!["model"]} · {job["provenance"]!["reasoning_effort"]}"),
                current ? "Verified reply for the current saved context. This is not merge approval."
                    : "Earlier conversation for this item — not a current recommendation or approval.");
        }
        return null;
    }

    internal string ActionGuidance(QueueProposal item, bool executionEnabled)
    {
        var advice = CurrentAdvice(item);
        if (advice?["conclusion"]?.GetValue<string>() == "hold") return "On hold — see reason in details.";
        if (advice?["conclusion"]?.GetValue<string>() == "no_action") return "No action needed.";
        var recommendation = CurrentRecommendation(item);
        if (item.Kind != "PR" || item.Step["action"]?.GetValue<string>() != "validate_then_merge")
            return advice?["ready_for_action"]?.GetValue<bool>() == true ? "Next: review proposed action." : "Next: investigate this item.";
        if (recommendation?.Ready != true)
            return advice is null ? "Next: start investigation." : "Next: continue investigation.";
        var target = NativeDisplayText.Escape(item.Identity["expected_base"]?.GetValue<string>() ?? "the configured target branch");
        return executionEnabled
            ? $"Next: review merge into {target}."
            : "Merge disabled in this profile.";
    }

    internal bool SuggestsInvestigation(QueueProposal item)
    {
        if (HasActiveInvestigation) return false;
        var advice = CurrentAdvice(item);
        var conclusion = advice?["conclusion"]?.GetValue<string>();
        return conclusion is null or "investigate"
            || (conclusion == "recommend" && advice?["ready_for_action"]?.GetValue<bool>() != true);
    }
}
