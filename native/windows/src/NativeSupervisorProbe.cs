using System;
using System.IO;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading.Tasks;

namespace WatcherNativeProof;

internal static class NativeSupervisorProbe
{
    internal static async Task RunAsync(string launchFile)
    {
        var client = BackendClient.FromLaunchFile(launchFile);
        JsonObject Request(string operation) => new() { ["schema_version"] = 1,
            ["request_id"] = "read_" + Guid.NewGuid().ToString("N"), ["operation"] = operation };
        var snapshot = await client.SendAsync(Request("snapshot"));
        var item = QueueProposal.Parse(snapshot["proposals"]!.AsArray().First(), snapshot["plan_id"]!.GetValue<string>());
        var request = Request("supervisor_status");
        var repository = item.Evidence["repository"]!.GetValue<string>();
        var digest = snapshot["supervisor_context_digests"]![repository]!.GetValue<string>();
        request["repository"] = repository;
        var response = await client.SendAsync(request);
        var view = NativeSupervisorView.Parse(response, repository, digest);
        var observed = CheckPresentation(view, item, response);
        foreach (var fault in new[] { "repository", "receipt", "provenance", "completion", "current_flag", "snapshot_digest" })
        {
            var invalid = (JsonObject)response.DeepClone();
            var job = invalid["jobs"]!.AsArray().Last(value => value!["provider_completion_observed"]!.GetValue<bool>())!;
            switch (fault)
            {
                case "repository": invalid["repository"] = "another/repository"; break;
                case "receipt": job["messages"]!.AsArray()[0]!["request_id"] = "decision_unknown"; break;
                case "provenance": job["provenance"]!["model"] = null; break;
                case "completion": job["provider_completion_observed"] = false; break;
                case "current_flag": job["current_context"] = !job["current_context"]!.GetValue<bool>(); break;
                case "snapshot_digest": invalid["context_digest"] = new string('f',64); break;
            }
            var rejected = false;
            try { NativeSupervisorView.Parse(invalid, repository, digest); }
            catch (InvalidDataException) { rejected = true; }
            if (!rejected) throw new InvalidDataException($"Malformed agent frame was displayed: {fault}");
        }
        App.Record("supervisor_native_probe_passed", new { model = observed["provenance"]!["model"]!.GetValue<string>(),
            exact_question_reply_proposal = true, malformed_frames_rejected = 6, no_new_model_call = true,
            visible_window_acceptance = false });
    }

    internal static JsonObject CheckPresentation(NativeSupervisorView view, QueueProposal item, JsonObject response)
    {
        var text = view.Describe(item);
        var observed = response["jobs"]!.AsArray().Last(job => job!["provider_completion_observed"]!.GetValue<bool>()
            && job["status"]!.GetValue<string>() is "completed" or "superseded" or "failed" or "interrupted"
            && job["messages"]!.AsArray().Any(receipt => item.SameItem(receipt!["item"]!.AsObject())))!.AsObject();
        var question = observed["messages"]!.AsArray().Last(receipt => item.SameItem(receipt!["item"]!.AsObject()))!;
        var reply = NativeSupervisorView.Result(observed)!["replies"]!.AsArray().Single(value =>
            value!["proposal_id"]!.GetValue<string>() == question["proposal_id"]!.GetValue<string>()
            && value["request_id"]!.GetValue<string>() == question["request_id"]!.GetValue<string>());
        var expectedReply = NativeDisplayText.Escape(reply!["message"]!.GetValue<string>());
        if (!text.Contains(expectedReply)
            || !text.Contains(NativeDisplayText.Escape(observed["provenance"]!["model"]!.GetValue<string>()))
            || !text.Contains(NativeDisplayText.Escape(question["question"]!.GetValue<string>())))
            throw new InvalidDataException("The actual backend reply, question or model did not reach the native display model.");
        var other = (JsonObject)item.Evidence.DeepClone();
        other["proposal_id"] = "proposal_ffffffffffffffff";
        other["step"]!["identity"]!["number"] = item.Identity["number"]!.GetValue<int>() + 1;
        var unrelated = view.Describe(new QueueProposal { Evidence = other });
        if (unrelated.Contains(expectedReply))
            throw new InvalidDataException("Agent reply leaked to a different repository item.");
        return observed;
    }
}
