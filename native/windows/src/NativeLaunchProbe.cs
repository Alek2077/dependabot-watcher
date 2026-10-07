using System;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;

namespace WatcherNativeProof;

internal static class NativeLaunchProbe
{
    private const string Repository = "example-org/example-app";
    private const string RequestId = "launch_fixture";
    private static readonly string Context = new('a', 64);
    private static readonly string Job = "job_" + new string('b', 32);
    private static readonly string Connection = "connection_" + new string('c', 32);
    private static readonly string Permission = "permission_" + new string('d', 32);
    private static readonly string Digest = new('e', 64);

    private static JsonObject Frame(string kind, int sequence) => new() {
        ["schema_version"] = 1, ["request_id"] = RequestId, ["repository"] = Repository,
        ["mode"] = "supervisor_launch", ["sequence"] = sequence, ["event"] = kind
    };

    private static JsonObject Starting()
    { var frame = Frame("starting", 0); frame["status"] = "ok"; return frame; }

    private static JsonObject Preview()
    {
        var frame = Frame("permission_preview", 1);
        frame["request"] = new JsonObject {
            ["job_id"] = Job, ["context_digest"] = Context, ["connection_id"] = Connection,
            ["permission_id"] = Permission, ["thread_id"] = "thread1", ["turn_id"] = "turn1",
            ["item_id"] = "item1", ["digest"] = Digest
        };
        frame["preview"] = new JsonObject {
            ["kind"] = "command", ["command"] = "cat fixture.txt\u202e", ["cwd"] = "/owned/worktree",
            ["reason"] = "Inspect the synthetic café fixture.", ["network"] = null,
            ["additional_permissions"] = new JsonObject { ["fileSystem"] = new JsonObject {
                ["read"] = new JsonArray("/owned/fixture"), ["write"] = null } },
            ["choices"] = new JsonArray("allow_once", "deny", "cancel")
        };
        return frame;
    }

    private static JsonObject Finished(int sequence)
    {
        var frame = Frame("finished", sequence);
        frame["status"] = "ok"; frame["job_id"] = Job; frame["context_digest"] = Context;
        frame["job_status"] = "completed"; frame["reused"] = false;
        return frame;
    }

    private static NativeLaunchStream Parser() => new(RequestId, Repository);
    private static byte[] Encode(JsonObject frame) => JsonSerializer.SerializeToUtf8Bytes(frame);

    internal static async Task RunAsync()
    {
        App.Record("normal_review_entry_probe_passed", new { rejected = NativeReviewLaunchProbe.Run(), normal_client_retained = true });
        VerifyPolicyAndReceipt();
        var startRequest = new JsonObject { ["schema_version"] = 1, ["request_id"] = RequestId,
            ["operation"] = "supervisor_start", ["repository"] = Repository, ["context_digest"] = Context };
        BackendClient.SupervisorParser(startRequest);
        startRequest["retry_job_id"] = Job;
        BackendClient.SupervisorParser(startRequest);
        foreach (var fault in new[] { "unknown", "missing", "null_retry", "malformed_retry" })
        {
            var invalid = (JsonObject)startRequest.DeepClone();
            if (fault == "unknown") invalid["model"] = "unselected";
            if (fault == "missing") invalid.Remove("context_digest");
            if (fault == "null_retry") invalid["retry_job_id"] = null;
            if (fault == "malformed_retry") invalid["retry_job_id"] = "another-session";
            var launchRejected = false;
            try { BackendClient.SupervisorParser(invalid); }
            catch (InvalidDataException) { launchRejected = true; }
            if (!launchRejected) throw new InvalidDataException("Native launch accepted an unbound retry: " + fault);
        }
        var parser = Parser();
        parser.Accept(Encode(Starting()));
        var preview = parser.Accept(Encode(Preview()));
        var permission = new NativePermission(Connection, Permission, Digest, Job, "thread1", "turn1", "item1",
            "Synthetic permission", new[] { "allow_once", "deny", "cancel" });
        if (!NativeLaunchStream.MatchesPermission(preview.Value, permission, Context)
            || NativeLaunchStream.MatchesPermission(preview.Value, permission with { JobId = "job_" + new string('f', 32) }, Context)
            || NativeLaunchStream.MatchesPermission(preview.Value, permission with { ConnectionId = "connection_" + new string('f', 32) }, Context)
            || NativeLaunchStream.MatchesPermission(preview.Value, permission with { PermissionId = "permission_" + new string('f', 32) }, Context)
            || NativeLaunchStream.MatchesPermission(preview.Value, permission with { ThreadId = "other-thread" }, Context)
            || NativeLaunchStream.MatchesPermission(preview.Value, permission with { TurnId = "other-turn" }, Context)
            || NativeLaunchStream.MatchesPermission(preview.Value, permission, new string('f', 64))
            || NativeLaunchStream.MatchesPermission(preview.Value, permission with { ItemId = "other-item" }, Context)
            || NativeLaunchStream.MatchesPermission(preview.Value, permission with { Digest = new string('f',64) }, Context)
            || NativeLaunchStream.MatchesPermission(preview.Value, permission with { Choices = new[] { "allow_session", "deny" } }, Context))
            throw new InvalidDataException("Preview is not bound to the exact permission journal.");
        var description = NativeLaunchStream.DescribePreview(preview.Value);
        if (description.Contains('\u202e') || !description.Contains("\\u202e")
            || !description.Contains("/owned/fixture") || !description.Contains("café"))
            throw new InvalidDataException("The permission display disguises or loses requested scope.");
        parser.Accept(Encode(Finished(2))); parser.RequireTerminal();
        var networkFrame = Preview();
        networkFrame["preview"]!["kind"] = "network";
        networkFrame["preview"]!["network"] = new JsonObject { ["host"] = "fixture.invalid:443", ["protocol"] = "https" };
        var networkParser = Parser(); networkParser.Accept(Encode(Starting()));
        var network = networkParser.Accept(Encode(networkFrame));
        var networkText = NativeLaunchStream.DescribePreview(network.Value);
        if (!networkText.Contains("https://fixture.invalid:443") || !networkText.Contains("cat fixture.txt") || !networkText.Contains("/owned/worktree"))
            throw new InvalidDataException("Managed network preview concealed supplied command context.");

        var rejected = 0;
        foreach (var fault in new[] { "request", "repository", "sequence", "duplicate_start", "unknown_event", "unknown_scope",
            "missing_item", "missing_cwd", "empty_choices", "persistent_choice", "relative_path", "unsupported_overlay",
            "duplicate_json", "oversized", "terminal_job", "repeat_permission", "after_terminal", "no_terminal" })
        {
            var invalid = Preview();
            var candidate = Parser(); candidate.Accept(Encode(Starting()));
            var failed = false;
            var checkedDirectly = false;
            try
            {
                switch (fault)
                {
                    case "request": invalid["request_id"] = "launch_foreign"; break;
                    case "repository": invalid["repository"] = "another/repository"; break;
                    case "sequence": invalid["sequence"] = 2; break;
                    case "duplicate_start": invalid = Starting(); invalid["sequence"] = 1; break;
                    case "unknown_event": invalid["event"] = "grant_everything"; break;
                    case "unknown_scope": invalid["preview"]!["future_scope"] = "undisclosed"; break;
                    case "missing_item": invalid["request"]!.AsObject().Remove("item_id"); break;
                    case "missing_cwd": invalid["preview"]!["cwd"] = null; break;
                    case "empty_choices": invalid["preview"]!["choices"] = new JsonArray(); break;
                    case "persistent_choice": invalid["preview"]!["choices"] = new JsonArray("allow_all"); break;
                    case "relative_path": invalid["preview"]!["additional_permissions"]!["fileSystem"]!["read"] = new JsonArray("relative"); break;
                    case "unsupported_overlay": invalid["preview"]!["additional_permissions"]!["fileSystem"]!["entries"] = new JsonArray(); break;
                    case "duplicate_json": candidate.Accept(Encoding.UTF8.GetBytes(invalid.ToJsonString().Replace("\"item_id\":", "\"item_id\":\"other\",\"item_id\":"))); checkedDirectly = true; break;
                    case "oversized": candidate.Accept(new byte[65536]); checkedDirectly = true; break;
                    case "terminal_job":
                        candidate.Accept(Encode(invalid)); invalid = Finished(2); invalid["job_id"] = "job_" + new string('f',32); break;
                    case "repeat_permission": candidate.Accept(Encode(invalid)); invalid["sequence"] = 2; break;
                    case "after_terminal": candidate.Accept(Encode(Finished(1))); break;
                    case "no_terminal": candidate.RequireTerminal(); checkedDirectly = true; break;
                }
                if (!checkedDirectly) candidate.Accept(Encode(invalid));
            }
            catch (Exception exception) when (exception is InvalidDataException or JsonException) { failed = true; }
            if (!failed) throw new InvalidDataException($"Invalid investigation stream was accepted: {fault}");
            rejected++;
        }
        var bytes = Encoding.UTF8.GetBytes(Starting().ToJsonString() + "\n" + Preview().ToJsonString() + "\n" + Finished(2).ToJsonString() + "\n");
        var frames = 0;
        using (var input = new BytewiseStream(bytes))
        {
            var final = await BackendClient.ReadLaunchAsync(input, Parser(), _ => frames++, CancellationToken.None);
            if (final.Event != "finished" || frames != 3) throw new InvalidDataException("Fragmented real reader lost a launch frame.");
        }
        foreach (var content in new[] { Starting().ToJsonString() + "\n", Starting().ToJsonString(), "\n", "{bad-json}\n" })
        {
            using var input = new MemoryStream(Encoding.UTF8.GetBytes(content));
            var failed = false;
            try { await BackendClient.ReadLaunchAsync(input, Parser(), _ => { }, CancellationToken.None); }
            catch (Exception exception) when (exception is InvalidDataException or JsonException) { failed = true; }
            if (!failed) throw new InvalidDataException("An incomplete or malformed byte stream was accepted.");
            rejected++;
        }
        App.Record("launch_stream_probe_passed", new { rejected_streams = rejected, fragmented_frames = frames,
            exact_permission_binding = true, raw_preview_logged = false, model_calls = 0, github_mutations = false });
    }

    private static JsonObject Policy() => new() {
        ["status"] = "configured", ["reason"] = null, ["mode"] = "approval_first", ["provider"] = "codex_app_server",
        ["model"] = "gpt-5.6-luna", ["reasoning_effort"] = "xhigh", ["expected_account_type"] = "chatgpt",
        ["access"] = "inherit", ["permission_profile"] = null, ["validation_authorized"] = false,
        ["timeout_seconds"] = 900, ["model_use_acknowledged"] = true, ["full_access_acknowledged"] = false,
        ["workspace"] = "/owned/worktree", ["instruction_sources"] = new JsonArray("/owned/worktree/AGENTS.md"),
        ["requires_fresh_preflight"] = true, ["live_runtime_verified"] = false, ["github_mutations_authorized"] = false,
        ["usage_effect"] = "subscription_or_api_usage_if_launched",
        ["evidence_privacy"] = "repository_evidence_and_saved_questions_sent_to_selected_provider_if_launched"
    };

    private static void VerifyPolicyAndReceipt()
    {
        var policy = NativeLaunchPolicy.Parse(Policy());
        if (!policy.Configured || !policy.Description.Contains("gpt-5.6-luna") || !policy.Description.Contains("xhigh")
            || !policy.Description.Contains("does not authorize a merge") || !policy.Description.Contains("local tests are not authorized"))
            throw new InvalidDataException("Chosen launch policy was not disclosed.");
        var blocked = Policy(); blocked["status"] = "blocked"; blocked["reason"] = "Register a workspace first.";
        if (NativeLaunchPolicy.Parse(blocked).Configured) throw new InvalidDataException("Blocked policy enabled investigation.");
        var full = Policy(); full["access"] = "full"; full["full_access_acknowledged"] = true;
        if (!NativeLaunchPolicy.Parse(full).Description.Contains("outside Watcher's guarded executor"))
            throw new InvalidDataException("Full access concealed its weaker mutation boundary.");
        foreach (var fault in new[] { "model", "account", "ack", "full_ack", "restricted_profile", "live_claim", "mutation_claim", "extra", "timeout" })
        {
            var invalid = Policy();
            switch (fault) {
                case "model": invalid["model"] = null; break;
                case "account": invalid["expected_account_type"] = "another-account"; break;
                case "ack": invalid["model_use_acknowledged"] = false; break;
                case "full_ack": invalid["access"] = "full"; break;
                case "restricted_profile": invalid["access"] = "restricted"; break;
                case "live_claim": invalid["live_runtime_verified"] = true; break;
                case "mutation_claim": invalid["github_mutations_authorized"] = true; break;
                case "extra": invalid["command_override"] = "arbitrary"; break;
                case "timeout": invalid["timeout_seconds"] = 3601; break;
            }
            var rejected = false;
            try { NativeLaunchPolicy.Parse(invalid); } catch (InvalidDataException) { rejected = true; }
            if (!rejected) throw new InvalidDataException("Unsafe launch policy was accepted: " + fault);
        }
        var response = new JsonObject {
            ["mode"] = "supervisor_status", ["repository"] = Repository, ["context_digest"] = Context,
            ["capabilities"] = new JsonObject { ["model_calls"] = false, ["github_mutations"] = false },
            ["configured_launch"] = Policy(), ["jobs"] = new JsonArray(new JsonObject {
                ["job_id"] = Job, ["context_digest"] = Context, ["status"] = "completed", ["current_context"] = true,
                ["provider_completion_observed"] = true, ["messages"] = new JsonArray(),
                ["result"] = new JsonObject { ["summary"] = "A synthetic verified result", ["recommendations"] = new JsonArray(), ["replies"] = new JsonArray() },
                ["provenance"] = new JsonObject { ["provider"] = "codex_app_server", ["model"] = "gpt-5.6-luna", ["reasoning_effort"] = "xhigh", ["session_id"] = "thread1",
                    ["usage"] = new JsonObject { ["input_tokens"] = 0, ["cached_input_tokens"] = 0, ["output_tokens"] = 0, ["reasoning_output_tokens"] = 0 } }
            })
        };
        // Exercise the decoded wire representation, not JsonValue<int> test
        // objects standing in for the parser's numeric JsonElements.
        response = JsonNode.Parse(response.ToJsonString())!.AsObject();
        var view = NativeSupervisorView.Parse(response, Repository, Context);
        if (!view.DescribeTerminal(Finished(2)).Contains("Agent results are saved"))
            throw new InvalidDataException("Independently verified completion was not recognized.");
        var failedResponse = (JsonObject)response.DeepClone();
        var failedJob = failedResponse["jobs"]![0]!;
        failedJob["status"] = "failed"; failedJob["result"] = null;
        failedJob["provider_completion_observed"] = false; failedJob["provenance"] = null;
        failedJob["failure"] = "provider_failed"; failedJob["retry_available"] = true;
        if (NativeSupervisorView.Parse(failedResponse, Repository, Context).RetryJobId != Job)
            throw new InvalidDataException("Exact failed-investigation retry was not exposed.");
        foreach (var fault in new[] { "historical", "completed", "duplicate" })
        {
            var invalid = (JsonObject)failedResponse.DeepClone();
            if (fault == "historical") {
                invalid["jobs"]![0]!["current_context"] = false;
                invalid["jobs"]![0]!["context_digest"] = new string('f',64);
            }
            else if (fault == "completed") invalid["jobs"]![0]!["status"] = "completed";
            else {
                var duplicate = invalid["jobs"]![0]!.DeepClone();
                duplicate["job_id"] = "job_" + new string('f',32);
                invalid["jobs"]!.AsArray().Add(duplicate);
            }
            var rejected = false;
            try { NativeSupervisorView.Parse(invalid, Repository, Context); }
            catch (InvalidDataException) { rejected = true; }
            if (!rejected) throw new InvalidDataException("Ambiguous or stale retry offered: " + fault);
        }
        var action = new NativeActionReview { Value = new JsonObject {
            ["preparation"] = new JsonObject { ["binding"] = new JsonObject { ["repository"] = Repository } } } };
        var exactBinding = action.Binding.ToJsonString();
        if (!action.MatchesRepository("EXAMPLE-ORG/Example-App") || action.MatchesRepository("example-org/another")
            || action.MatchesRepository(null) || action.Binding.ToJsonString() != exactBinding)
            throw new InvalidDataException("Case-only repository association rewrote exact action evidence.");
        App.Record("checkpoint_recovery_probe_passed", new { exact_retry = true, rejected_retry_frames = 3,
            case_alias_association = true, exact_binding_unchanged = true, model_calls = 0 });
        var rawText = "café\nSafe\u202e misleading\u001b[31m\u200b\U000e0001";
        var expectedText = "café\nSafe\\u202e misleading\\u001b[31m\\u200b\\U000e0001";
        if (NativeDisplayText.Escape(rawText) != expectedText)
            throw new InvalidDataException("Display escaping changed ordinary Unicode or concealed control/format runes.");
        var displayCase = (JsonObject)response.DeepClone();
        var proposalId = "proposal_" + new string('f', 16);
        var reply = new JsonObject { ["proposal_id"] = proposalId, ["request_id"] = "decision_1_fixture", ["message"] = rawText };
        displayCase["jobs"]![0]!["result"]!["replies"] = new JsonArray(reply);
        displayCase["jobs"]![0]!["messages"] = new JsonArray(new JsonObject {
            ["proposal_id"] = proposalId, ["request_id"] = "decision_1_fixture", ["processing"] = "answered_current",
            ["question"] = rawText, ["created_at"] = "2026-10-06T07:52:59Z",
            ["item"] = new JsonObject { ["repository"] = Repository, ["kind"] = "pull_request", ["number"] = 42,
                ["head_sha"] = new string('d', 40), ["expected_base"] = "main", ["observed_base"] = "main" } });
        var planId = "plan_" + new string('a', 16);
        var itemEvidence = new JsonObject {
            ["proposal_id"] = proposalId, ["repository"] = Repository,
            ["plan_id"] = planId, ["version"] = 0, ["handling"] = "pending", ["evidence_status"] = "complete",
            ["step"] = new JsonObject { ["kind"] = "pull_request", ["order"] = 1, ["summary"] = "Synthetic display contract", ["identity"] = new JsonObject {
                ["repository"] = Repository, ["number"] = 42 } },
            ["feedback"] = new JsonArray(new JsonObject {
                ["request_id"] = "decision_1_fixture", ["message"] = rawText, ["status"] = "saved_not_submitted" }) };
        var item = QueueProposal.Parse(JsonNode.Parse(itemEvidence.ToJsonString()), planId);
        var beforeDisplay = displayCase.ToJsonString();
        var beforeItem = item.Evidence.ToJsonString();
        var rendered = NativeSupervisorView.Parse(displayCase, Repository, Context).Describe(item);
        if (!rendered.Contains("Your question: " + expectedText) || !rendered.Contains("Agent answer: " + expectedText)
            || displayCase.ToJsonString() != beforeDisplay || item.Evidence.ToJsonString() != beforeItem)
            throw new InvalidDataException("Agent-result display concealed controls or mutated saved evidence.");
        App.Record("launch_display_probe_passed", new { result_escaped = true, original_evidence_unchanged = true,
            unicode_and_newlines_preserved = true });
        foreach (var fault in new[] { "job", "context", "status", "missing_result" })
        {
            var invalid = Finished(2); var saved = (JsonObject)response.DeepClone();
            if (fault == "job") invalid["job_id"] = "job_" + new string('f', 32);
            if (fault == "context") invalid["context_digest"] = new string('f', 64);
            if (fault == "status") invalid["job_status"] = "failed";
            if (fault == "missing_result") saved["jobs"]![0]!["result"] = null;
            var rejected = false;
            try { NativeSupervisorView.Parse(saved, Repository, Context).DescribeTerminal(invalid); }
            catch (InvalidDataException) { rejected = true; }
            if (!rejected) throw new InvalidDataException("Contradictory terminal receipt was accepted: " + fault);
        }
        App.Record("launch_policy_receipt_probe_passed", new { rejected_policies = 9, rejected_receipts = 4,
            chosen_policy_not_runtime_claim = true, independent_completion_required = true, model_calls = 0 });
    }

    private sealed class BytewiseStream(byte[] bytes) : MemoryStream(bytes)
    {
        public override ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken = default)
            => base.ReadAsync(buffer[..Math.Min(1, buffer.Length)], cancellationToken);
    }
}
