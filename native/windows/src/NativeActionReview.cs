using System;
using System.IO;
using System.Linq;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace WatcherNativeProof;

// Display-only consumer of the Python-owned confirmation/result contract.
internal sealed class NativeActionReview
{
    internal required JsonObject Value { get; init; }
    internal string? Id => Value["review_id"]?.GetValue<string>();
    internal string Status => Value["status"]!.GetValue<string>();
    internal JsonObject Preparation => Value["preparation"]!.AsObject();
    internal JsonObject Binding => Preparation["binding"]!.AsObject();
    internal string ProposalId => Preparation["proposal_id"]!.GetValue<string>();
    internal bool HasPendingCompletion => Status == "completed" && Value["completion"] is JsonObject
        && Value["completion_stage"]?.GetValue<string>() != "completed";
    internal string? RecordedMaintenanceSummary => Status == "completed"
        && Value["outcome_recorded"]?.GetValue<bool>() == true
        && Value["completion_stage"]?.GetValue<string>() == "completed"
        ? (Value["result"]?["status"]?.GetValue<string>() == "already_applied"
                ? $"Already confirmed merged into {Binding["expected_base"]}. No new merge was performed. "
                : $"Merged into {Binding["expected_base"]}. ")
            + "Saved completion: configured follow-up checks"
            + (Value["completion"]?["policy"]?.GetValue<string>() == "selected_local_sync" ? " and selected local updates" : "")
            + " were verified. "
            + "This is the recorded outcome, not a new live check. No deployment or production-health verification was performed."
        : null;
    internal bool CanAssessCompletion => Status == "completed" && Value["completion"] is JsonObject completion
        && completion["schema_version"]?.GetValue<int>() == 2
        && completion["merge_commit_sha"] is JsonValue sha && sha.TryGetValue<string>(out var text)
        && Regex.IsMatch(text, "^([a-fA-F0-9]{40}|[a-fA-F0-9]{64})$");

    // Repository scope is case-insensitive; exact consent and evidence below
    // retain their original bytes, head, plan and proposal identities.
    internal bool MatchesRepository(string? repository) => repository is not null
        && StringComparer.OrdinalIgnoreCase.Equals(Binding["repository"]!.GetValue<string>(), repository);

    internal static NativeActionReview Parse(JsonNode? node)
    {
        var value = node as JsonObject ?? throw new InvalidDataException("Missing saved action review.");
        var review = new NativeActionReview { Value = value };
        if (value["schema_version"]?.GetValue<int>() != 1
            || review.Status is not ("awaiting_confirmation" or "recovery_required" or "completed"
                or "invalidated" or "expired" or "already_applied")
            || review.Preparation["schema_version"]?.GetValue<int>() != 1
            || !Regex.IsMatch(review.ProposalId, "^proposal_[a-f0-9]{16}$")
            || review.Preparation["version"]!.GetValue<int>() < 0
            || !Regex.IsMatch(review.Preparation["plan_id"]!.GetValue<string>(), "^plan_[a-f0-9]{16}$")
            || review.Preparation["mutation_authorized"]?.GetValue<bool>() != false
            || review.Preparation["mutation_performed"]?.GetValue<bool>() != false
            || review.Preparation["capabilities"]?["github_mutations"]?.GetValue<bool>() != false
            || review.Preparation["capabilities"]?["model_calls"]?.GetValue<bool>() != false
            || string.IsNullOrWhiteSpace(review.Binding["repository"]?.GetValue<string>())
            || review.Binding["pull_request"]!.GetValue<int>() <= 0
            || !Regex.IsMatch(review.Binding["head_sha"]!.GetValue<string>(), "^([a-fA-F0-9]{40}|[a-fA-F0-9]{64})$")
            || string.IsNullOrWhiteSpace(review.Binding["expected_base"]?.GetValue<string>())
            || review.Binding["merge_method"]?.GetValue<string>() is not ("merge" or "squash" or "rebase")
            || review.Preparation["decision"] is not JsonObject decision
            || string.IsNullOrWhiteSpace(decision["title"]?.GetValue<string>())
            || decision["model_used"] is not JsonValue model || !model.TryGetValue<bool>(out _)
            || decision["required_checks"] is not JsonArray checks || checks.Count > 1000)
            throw new InvalidDataException("Saved action identity or display context is invalid.");
        foreach (var check in checks)
            if (string.IsNullOrWhiteSpace(check?["name"]?.GetValue<string>())
                || string.IsNullOrWhiteSpace(check?["state"]?.GetValue<string>()))
                throw new InvalidDataException("Saved action check evidence is invalid.");
        if (review.Status == "already_applied")
        {
            if (review.Id is not null || review.Preparation["status"]?.GetValue<string>() != "already_applied")
                throw new InvalidDataException("Already-applied context cannot authorize an action.");
            return review;
        }
        if (review.Id is null || !Regex.IsMatch(review.Id, "^review_[a-f0-9]{32}$")
            || review.Preparation["status"]?.GetValue<string>() != "ready"
            || !DateTimeOffset.TryParse(value["created_at"]?.GetValue<string>(), out var created)
            || !DateTimeOffset.TryParse(value["expires_at"]?.GetValue<string>(), out var expires)
            || expires - created != TimeSpan.FromMinutes(15)
            || value["outcome_recorded"] is not JsonValue recorded || !recorded.TryGetValue<bool>(out _))
            throw new InvalidDataException("Saved action confirmation is invalid.");
        if (review.Status is "completed" or "recovery_required")
        {
            if (!DateTimeOffset.TryParse(value["consented_at"]?.GetValue<string>(), out var consent)
                || consent < created || consent >= expires)
                throw new InvalidDataException("Action outcome lacks valid saved consent.");
        }
        else if (value["consented_at"] is not null)
            throw new InvalidDataException("Unconsumed review incorrectly contains consent.");
        if (review.Status == "completed")
        {
            var result = value["result"] as JsonObject;
            var status = result?["status"]?.GetValue<string>();
            if (result is null || status is not ("merged" or "already_applied")
                || !JsonNode.DeepEquals(result["binding"], review.Binding)
                || result["mutation_performed"]?.GetValue<bool>() != (status == "merged"))
                throw new InvalidDataException("Action result does not match its exact reviewed binding.");
        }
        if (value["completion"] is null && value["completion_stage"] is not null)
            throw new InvalidDataException("Maintenance stage lacks its bound completion receipt.");
        if (value["completion"] is not null)
        {
            var completion = value["completion"] as JsonObject
                ?? throw new InvalidDataException("Saved maintenance scope is invalid.");
            var stage = value["completion_stage"]?.GetValue<string>();
            if (completion["schema_version"]?.GetValue<int>() != 2
                || completion["action_id"]?.GetValue<string>() != review.Preparation["action_id"]?.GetValue<string>()
                || completion["repository"]?.GetValue<string>() != review.Binding["repository"]?.GetValue<string>()
                || completion["base_branch"]?.GetValue<string>() != review.Binding["expected_base"]?.GetValue<string>()
                || completion["policy"]?.GetValue<string>() is not ("remote_only" or "selected_local_sync")
                || stage is not ("merge_outcome" or "refresh" or "assessment" or "local_sync" or "verification" or "completed")
                || (stage == "completed") != (completion["finalization"] is JsonObject)
                || (stage == "completed" && review.Status != "completed"))
                throw new InvalidDataException("Saved maintenance scope differs from the reviewed action.");
            if (stage == "completed") RequireFinalization(completion, review);
        }
        return review;
    }

    private static void RequireFinalization(JsonObject completion, NativeActionReview review)
    {
        bool Keys(JsonObject value, params string[] fields) => value.Count == fields.Length && fields.All(value.ContainsKey);
        string? Text(JsonNode? value) => value is JsonValue scalar && scalar.TryGetValue<string>(out var text) ? text : null;
        bool Sha(JsonNode? value) => Text(value) is string text && Regex.IsMatch(text, "\\A([a-fA-F0-9]{40}|[a-fA-F0-9]{64})\\z");
        var evidence = completion["evidence"] as JsonObject;
        var final = completion["finalization"] as JsonObject;
        var targets = completion["targets"] as JsonArray;
        var results = completion["local_results"] as JsonObject;
        if (!Keys(completion, "schema_version", "action_id", "repository", "base_branch", "policy", "targets",
                "assessment_required", "merge_commit_sha", "evidence", "assessment", "local_results", "finalization")
            || !Sha(completion["merge_commit_sha"]) || evidence is null || final is null
            || !Keys(evidence, "base_sha", "context_digest") || !Sha(evidence["base_sha"])
            || Text(evidence["context_digest"]) is not string digest || !Regex.IsMatch(digest, "\\A[a-f0-9]{64}\\z")
            || !Keys(final, "verified_at", "base_sha", "context_digest")
            || !JsonNode.DeepEquals(final["base_sha"], evidence["base_sha"])
            || !JsonNode.DeepEquals(final["context_digest"], evidence["context_digest"])
            || Text(final["verified_at"]) is not string verified
            || !Regex.IsMatch(verified, "(Z|[+-][0-9]{2}:[0-9]{2})\\z")
            || !DateTimeOffset.TryParse(verified, out _)
            || completion["assessment_required"] is not JsonValue required || !required.TryGetValue<bool>(out var assessmentRequired)
            || targets is null || targets.Count > 10 || results is null
            || (Text(completion["policy"]) == "selected_local_sync") != (targets.Count > 0))
            throw new InvalidDataException("Saved maintenance completion lacks final verification evidence.");
        var scope = new JsonObject { ["policy"] = completion["policy"]!.DeepClone(),
            ["targets"] = targets.DeepClone(), ["assessment_required"] = completion["assessment_required"]!.DeepClone() };
        if (!JsonNode.DeepEquals(scope, review.Preparation["decision"]?["completion_scope"]))
            throw new InvalidDataException("Saved maintenance completion differs from the approved workflow scope.");
        var identifiers = new System.Collections.Generic.HashSet<string>();
        foreach (var node in targets)
        {
            if (node is not JsonObject target || !Keys(target, "repository", "directory", "branch", "target_id")
                || !StringComparer.OrdinalIgnoreCase.Equals(Text(target["repository"]), Text(review.Binding["repository"]))
                || Text(target["branch"]) != Text(review.Binding["expected_base"])
                || string.IsNullOrWhiteSpace(Text(target["directory"]))
                || Text(target["target_id"]) is not string id || !Regex.IsMatch(id, "\\A[a-f0-9]{32}\\z") || !identifiers.Add(id))
                throw new InvalidDataException("Saved completion has an invalid selected target.");
        }
        if (results.Count != identifiers.Count || results.Any(entry => !identifiers.Contains(entry.Key)
                || entry.Value is not JsonObject local || !Keys(local, "head_sha", "remote_sha")
                || !JsonNode.DeepEquals(local["head_sha"], evidence["base_sha"])
                || !JsonNode.DeepEquals(local["remote_sha"], evidence["base_sha"])))
            throw new InvalidDataException("Saved completion has incomplete local results.");
        var assessment = completion["assessment"] as JsonObject;
        if (assessmentRequired ? assessment is null || !Keys(assessment, "job_id", "session_id", "context_digest")
                || Text(assessment["job_id"]) is not string job || !Regex.IsMatch(job, "\\Ajob_[a-f0-9]{32}\\z")
                || string.IsNullOrWhiteSpace(Text(assessment["session_id"]))
                || !JsonNode.DeepEquals(assessment["context_digest"], evidence["context_digest"])
            : completion["assessment"] is not null)
            throw new InvalidDataException("Saved completion lacks its required assessment provenance.");
    }

    internal void RequireSelection(QueueProposal item)
    {
        if (ProposalId != item.Id || Preparation["version"]!.GetValue<int>() != item.Version
            || Preparation["plan_id"]?.GetValue<string>() != item.Evidence["plan_id"]?.GetValue<string>()
            || Binding["repository"]?.GetValue<string>() != item.Evidence["repository"]?.GetValue<string>()
            || Binding["pull_request"]!.GetValue<int>() != item.Identity["number"]!.GetValue<int>()
            || Binding["head_sha"]?.GetValue<string>() != item.Identity["head_sha"]?.GetValue<string>()
            || Binding["expected_base"]?.GetValue<string>() != item.Identity["expected_base"]?.GetValue<string>()
            || Binding["step_id"]?.GetValue<string>() != item.Step["step_id"]?.GetValue<string>())
            throw new InvalidDataException("Prepared confirmation differs from the selected exact action.");
    }

    internal string Describe()
    {
        var decision = Preparation["decision"]!.AsObject();
        var checks = string.Join(", ", decision["required_checks"]!.AsArray().Select(c => $"{c?["name"]}: {c?["state"]}"));
        return $"{decision["title"]}\n{Binding["repository"]} · PR #{Binding["pull_request"]}\n"
            + $"Merge into {Binding["expected_base"]} using {Binding["merge_method"]}.\nExact head: {Binding["head_sha"]}\n"
            + $"Required checks: {checks}\n"
            + (decision["model_used"]!.GetValue<bool>() ? "Saved model advice informed this recommendation; this review launches no agent."
                : "Deterministic recommendation; no AI was involved.");
    }
}
