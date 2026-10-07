using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Text.Json.Nodes;

internal static class ScheduledContracts
{
    internal static void Run(Assembly assembly)
    {
        var type = assembly.GetType("WatcherNativeProof.NativeScheduledRun", true)!;
        var parse = type.GetMethod("Parse", BindingFlags.Static | BindingFlags.NonPublic)!;
        var run = "run_" + new string('a', 20);
        var digest = new string('b', 64);
        var repository = "example-org/example-app";
        var scope = new Dictionary<string, string> { [repository] = digest };
        var selected = new JsonObject { ["repository"] = repository, ["context_digest"] = digest };
        var selection = new JsonObject {
            ["schema_version"] = 1, ["mode"] = "scheduled_supervisor_plan", ["run_id"] = run,
            ["status"] = "ready", ["max_jobs_per_run"] = 1,
            ["repositories"] = new JsonArray(selected), ["skipped"] = new JsonArray(),
            ["capabilities"] = new JsonObject { ["model_calls"] = false, ["github_mutations"] = false }
        };
        var receipt = new JsonObject {
            ["schema_version"] = 1, ["run_id"] = run, ["configuration_digest"] = new string('c', 64),
            ["selection"] = selection, ["selection_digest"] = new string('d', 64),
            ["slots"] = new JsonArray(new JsonObject { ["repository"] = repository, ["context_digest"] = digest,
                ["phase"] = "reserved", ["job_id"] = null })
        };
        var response = new JsonObject { ["trigger"] = new JsonObject { ["claimed"] = true, ["receipt"] = receipt } };
        object Read(JsonObject value) => parse.Invoke(null, new object[] { value, run, scope })
            ?? throw new InvalidDataException("No scheduled result.");
        int Slots(object value) {
            var count = 0;
            foreach (var item in (IEnumerable)type.GetProperty("Slots")!.GetValue(value)!) count++;
            return count;
        }
        if (Slots(Read(response)) != 1) throw new InvalidDataException("Fresh reservation rejected.");
        scope["example-org/second-app"] = new string('f', 64);
        try { Read(response); throw new InvalidDataException("Omitted configured repository accepted."); }
        catch (TargetInvocationException error) when (error.InnerException is InvalidDataException) { }
        var completeScope = (JsonObject)response.DeepClone();
        completeScope["trigger"]!["receipt"]!["selection"]!["skipped"]!.AsArray().Add(new JsonObject {
            ["repository"] = "EXAMPLE-ORG/SECOND-APP", ["reason"] = "budget_deferred"
        });
        if (Slots(Read(completeScope)) != 1) throw new InvalidDataException("Complete case-insensitive partition rejected.");
        scope.Remove("example-org/second-app");
        var duplicate = (JsonObject)response.DeepClone();
        duplicate["trigger"]!["claimed"] = false;
        var duplicateResult = Read(duplicate);
        if (Slots(duplicateResult) != 0 || type.GetProperty("Attention")!.GetValue(duplicateResult) is not true)
            throw new InvalidDataException("Abandoned reservation was replayed or hidden.");
        var completed = (JsonObject)duplicate.DeepClone();
        completed["trigger"]!["receipt"]!["slots"]![0]!["phase"] = "finished";
        completed["trigger"]!["receipt"]!["slots"]![0]!["job_id"] = "job_" + new string('e', 32);
        if (Slots(Read(completed)) != 0 || type.GetProperty("Attention")!.GetValue(Read(completed)) is not false)
            throw new InvalidDataException("Finished duplicate did not stay quiet.");
        var rejected = 0;
        foreach (var fault in new[] { "extra", "wrong_run", "wrong_digest", "unknown_repo", "bad_limit", "capability",
            "consumed", "missing_slot", "wrong_phase", "bad_finished", "status", "duplicate" })
        {
            var changed = (JsonObject)response.DeepClone();
            var item = changed["trigger"]!["receipt"]!;
            switch (fault) {
                case "extra": item["command"] = "never executed"; break;
                case "wrong_run": item["run_id"] = "run_" + new string('f', 20); break;
                case "wrong_digest": item["slots"]![0]!["context_digest"] = new string('e', 64); break;
                case "unknown_repo": item["selection"]!["repositories"]![0]!["repository"] = "other/repo"; break;
                case "bad_limit": item["selection"]!["max_jobs_per_run"] = 21; break;
                case "capability": item["selection"]!["capabilities"]!["model_calls"] = true; break;
                case "consumed": item["slots"]![0]!["phase"] = "started"; break;
                case "missing_slot": item["slots"]!.AsArray().Clear(); break;
                case "wrong_phase": item["slots"]![0]!["phase"] = "approved"; break;
                case "bad_finished": item["slots"]![0]!["phase"] = "finished"; break;
                case "status": item["selection"]!["status"] = "idle"; break;
                case "duplicate": item["selection"]!["repositories"]!.AsArray().Add(item["selection"]!["repositories"]![0]!.DeepClone()); break;
            }
            try { Read(changed); throw new InvalidDataException("Malformed reservation accepted: " + fault); }
            catch (TargetInvocationException error) when (error.InnerException is InvalidDataException or InvalidOperationException or FormatException) { rejected++; }
        }
        var activation = assembly.GetType("WatcherNativeProof.NativeScheduledActivation", true)!
            .GetMethod("Read", BindingFlags.Static | BindingFlags.NonPublic)!;
        var file = @"\\wsl.localhost\Fixture\private state\backend-launch.json";
        var arguments = "--launch-file \"" + file + "\" --scheduled-run " + run;
        if (activation.Invoke(null, new object[] { arguments }) is null
            || activation.Invoke(null, new object[] { "\"" + Environment.ProcessPath + "\" " + arguments }) is null)
            throw new InvalidDataException("Windows-quoted scheduled activation rejected.");
        foreach (var mode in new[] { "off", "desktop" })
        {
            var decoded = activation.Invoke(null, new object[] { arguments + " --notify-mode " + mode })!;
            if (decoded.GetType().GetField("Item3")!.GetValue(decoded) is not bool notify || notify != (mode == "desktop"))
                throw new InvalidDataException("Notification choice was not preserved through activation.");
        }
        foreach (var input in new[] { arguments + " --merge", arguments.Replace(run, "../other"),
            "--scheduled-run " + run, arguments + "\0", arguments.Replace("--scheduled-run", "--notification-run"),
            arguments + " --notify-mode auto", arguments + " --notify-mode" })
        {
            try { activation.Invoke(null, new object[] { input }); throw new InvalidDataException("Malformed activation accepted."); }
            catch (TargetInvocationException error) when (error.InnerException is InvalidDataException) { rejected++; }
        }
        Console.WriteLine($"Scheduled contracts passed: duplicate/recovery visibility, bounded reservations and {rejected} malformed inputs.");
    }
}
