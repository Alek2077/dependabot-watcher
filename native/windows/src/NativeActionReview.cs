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
        return review;
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
