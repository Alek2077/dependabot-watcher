using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace WatcherNativeProof;

internal sealed record ScheduledSlot(string Repository, string ContextDigest);

internal sealed record NativeScheduledRun(bool Claimed, bool Attention, IReadOnlyList<ScheduledSlot> Slots, bool ExistingReservation)
{
    internal static NativeScheduledRun Parse(JsonObject response, string runId, IReadOnlyDictionary<string, string> scope)
    {
        var trigger = response["trigger"] as JsonObject ?? throw new InvalidDataException("Missing scheduled trigger receipt.");
        Fields(trigger, "claimed", "receipt");
        var claimed = trigger["claimed"]!.GetValue<bool>();
        var receipt = trigger["receipt"] as JsonObject ?? throw new InvalidDataException("Missing scheduled reservation.");
        Fields(receipt, "schema_version", "run_id", "configuration_digest", "selection", "selection_digest", "slots");
        if (receipt["schema_version"]?.GetValue<int>() != 1 || receipt["run_id"]?.GetValue<string>() != runId
            || !Hash(receipt["configuration_digest"]) || !Hash(receipt["selection_digest"]))
            throw new InvalidDataException("Scheduled reservation does not match its run/configuration binding.");
        var selection = receipt["selection"] as JsonObject ?? throw new InvalidDataException("Missing scheduled selection.");
        Fields(selection, "schema_version", "mode", "run_id", "status", "max_jobs_per_run", "repositories", "skipped", "capabilities");
        var status = selection["status"]?.GetValue<string>();
        if (selection["schema_version"]?.GetValue<int>() != 1 || selection["mode"]?.GetValue<string>() != "scheduled_supervisor_plan"
            || selection["run_id"]?.GetValue<string>() != runId || status is not ("off" or "idle" or "ready" or "attention")
            || selection["max_jobs_per_run"] is not JsonValue limitValue || !limitValue.TryGetValue<int>(out var limit) || limit is < 1 or > 20)
            throw new InvalidDataException("Scheduled selection has invalid identity or workload bounds.");
        var capabilities = selection["capabilities"] as JsonObject ?? throw new InvalidDataException("Missing scheduled capability boundary.");
        Fields(capabilities, "model_calls", "github_mutations");
        if (capabilities["model_calls"]?.GetValue<bool>() != false || capabilities["github_mutations"]?.GetValue<bool>() != false)
            throw new InvalidDataException("A scheduled reservation is not inference or action authority.");
        var selected = selection["repositories"] as JsonArray ?? throw new InvalidDataException("Missing selected repositories.");
        var reserved = receipt["slots"] as JsonArray ?? throw new InvalidDataException("Missing reserved slots.");
        var skipped = selection["skipped"] as JsonArray ?? throw new InvalidDataException("Missing skipped repository reasons.");
        if (selected.Count != reserved.Count || selected.Count > limit
            || (status != "off" && selected.Count + skipped.Count != scope.Count)
            || (status == "ready") != (selected.Count > 0) || (status == "off" && skipped.Count > 0))
            throw new InvalidDataException("Scheduled workload contradicts its bounded selection.");
        var slots = new List<ScheduledSlot>();
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var pair in selected.Zip(reserved))
        {
            var item = pair.First as JsonObject ?? throw new InvalidDataException("Invalid selected repository.");
            var slot = pair.Second as JsonObject ?? throw new InvalidDataException("Invalid reserved slot.");
            Fields(item, "repository", "context_digest"); Fields(slot, "repository", "context_digest", "phase", "job_id");
            var repository = item["repository"]!.GetValue<string>();
            var digest = item["context_digest"]!.GetValue<string>();
            var current = scope.SingleOrDefault(entry => string.Equals(entry.Key, repository, StringComparison.OrdinalIgnoreCase));
            var phase = slot["phase"]?.GetValue<string>();
            if (current.Key is null || !seen.Add(repository) || !Hash(item["context_digest"])
                || slot["repository"]?.GetValue<string>() != repository || slot["context_digest"]?.GetValue<string>() != digest
                || phase is not ("reserved" or "started" or "finished" or "recovery_required")
                || (phase == "finished" ? slot["job_id"]?.GetValue<string>() is not string jobId
                    || !Regex.IsMatch(jobId, "\\Ajob_[a-f0-9]{32}\\z") : slot["job_id"] is not null)
                || (claimed && (phase != "reserved" || current.Value != digest)))
                throw new InvalidDataException("Scheduled slot is outside its current saved scope or already consumed.");
            slots.Add(new ScheduledSlot(repository, digest));
        }
        var attention = false;
        foreach (var node in skipped)
        {
            var item = node as JsonObject ?? throw new InvalidDataException("Invalid scheduled skip.");
            Fields(item, "repository", "reason");
            var repository = item["repository"]!.GetValue<string>();
            var reason = item["reason"]?.GetValue<string>();
            if (!scope.Keys.Any(key => string.Equals(key, repository, StringComparison.OrdinalIgnoreCase)) || !seen.Add(repository)
                || reason is not ("configuration_blocked" or "evidence_unavailable" or "already_running" or "completion_unverified"
                    or "unanswered_feedback" or "completed_current" or "recovery_required" or "no_work" or "budget_deferred" or "evidence_incomplete"))
                throw new InvalidDataException("Scheduled skip is outside its known scope.");
            attention |= reason is not ("completed_current" or "no_work" or "already_running");
        }
        attention |= !claimed && reserved.Any(slot => slot!["phase"]!.GetValue<string>() != "finished");
        if (status != "off" && !seen.SetEquals(scope.Keys))
            throw new InvalidDataException("Scheduled selection omits configured repository scope.");
        return new NativeScheduledRun(claimed, attention, claimed ? slots : Array.Empty<ScheduledSlot>(), !claimed && reserved.Count > 0);
    }

    private static bool Hash(JsonNode? value) => value is JsonValue scalar && scalar.TryGetValue<string>(out var text)
        && Regex.IsMatch(text, "\\A[a-f0-9]{64}\\z");

    private static void Fields(JsonObject value, params string[] names)
    {
        if (value.Count != names.Length || value.Any(field => !names.Contains(field.Key)))
            throw new InvalidDataException("Unsupported scheduled receipt field set.");
    }
}

internal static class NativeScheduledActivation
{
    [DllImport("shell32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    private static extern IntPtr CommandLineToArgvW(string commandLine, out int count);
    [DllImport("kernel32.dll")]
    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    private static extern IntPtr LocalFree(IntPtr pointer);

    internal static (string File, string RunId, bool Notify)? Read(string arguments)
    {
        if (arguments.Length > 65536 || arguments.Contains('\0')) throw new InvalidDataException("Invalid activation arguments.");
        // SDK versions may expose the executable plus arguments or arguments
        // alone. Use Windows' quoting rules, never shell interpolation/splitting.
        var pointer = CommandLineToArgvW("WatcherActivation " + arguments, out var count);
        if (pointer == IntPtr.Zero) throw new InvalidDataException("Activation command line could not be decoded.");
        try
        {
            if (count is < 1 or > 8) throw new InvalidDataException("Activation exceeds its argument bound.");
            var values = Enumerable.Range(1, count - 1).Select(index =>
                Marshal.PtrToStringUni(Marshal.ReadIntPtr(pointer, index * IntPtr.Size)) ?? "").ToArray();
            if (values.Length is 3 or 5 or 7 && string.Equals(Path.GetFullPath(values[0]), Environment.ProcessPath, StringComparison.OrdinalIgnoreCase))
                values = values[1..];
            if (values.Length == 2 && values[0] == "--launch-file") return null;
            if (values.Length is not (4 or 6) || values[0] != "--launch-file" || values[2] != "--scheduled-run"
                || !Regex.IsMatch(values[3], "\\Arun_[a-f0-9]{20}\\z")
                || (values.Length == 6 && (values[4] != "--notify-mode" || values[5] is not ("off" or "desktop"))))
                throw new InvalidDataException("Unsupported scheduled activation.");
            return (values[1], values[3], values.Length == 4 || values[5] == "desktop");
        }
        finally { LocalFree(pointer); }
    }
}
