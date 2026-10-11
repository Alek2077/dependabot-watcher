using System;
using System.IO;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;

namespace WatcherNativeProof;

// Headless actual Windows→WSL transport. The trusted launch file must point to
// the owned synthetic backend; all durable context/permission paths are real.
internal static class NativeLaunchRoundtripProbe
{
    private const string Repository = "example-org/example-app";

    private static JsonObject Read(string operation) => new() {
        ["schema_version"] = 1, ["request_id"] = "read_" + Guid.NewGuid().ToString("N"),
        ["operation"] = operation, ["repository"] = Repository
    };

    private static async Task<JsonObject> Snapshot(BackendClient client)
    {
        var request = Read("snapshot"); request.Remove("repository");
        var snapshot = await client.SendAsync(request);
        if (snapshot["capabilities"]?["github_mutations"]?.GetValue<bool>() != false
            || snapshot["capabilities"]?["model_calls"]?.GetValue<bool>() != false
            || snapshot["proposals"] is not JsonArray proposals || proposals.Count != 1
            || proposals[0]?["repository"]?.GetValue<string>() != Repository)
            throw new InvalidDataException("Only the owned single-repository synthetic fixture is permitted.");
        QueueProposal.Parse(proposals[0], snapshot["plan_id"]!.GetValue<string>());
        return snapshot;
    }

    private static string Digest(JsonObject snapshot) => snapshot["supervisor_context_digests"]![Repository]!.GetValue<string>();

    private static async Task SaveMessage(BackendClient client, JsonObject snapshot, string message)
    {
        var item = snapshot["proposals"]![0]!;
        await client.SendAsync(new JsonObject {
            ["schema_version"] = 1, ["request_id"] = "decision_" + snapshot["next_request_sequence"] + "_" + Guid.NewGuid().ToString("N"),
            ["operation"] = "feedback", ["proposal_id"] = item["proposal_id"]!.GetValue<string>(),
            ["version"] = item["version"]!.GetValue<int>(), ["message"] = message
        });
    }

    internal static async Task RunAsync(string launchFile, string testCase)
    {
        if (testCase is not ("allow_once" or "deny" or "cancel" or "disconnect" or "stale_feedback" or "resume_feedback" or "retry_interrupted" or "scheduled" or "scheduled_malformed" or "launch_busy" or "scheduled_terminal_busy"))
            throw new InvalidDataException("Unsupported controlled launch diagnostic.");
        var client = BackendClient.FromLaunchFile(launchFile);
        if (!client.DiagnosticTransport) throw new InvalidDataException("A synthetic transport marker is required.");
        if (testCase == "launch_busy")
        {
            var window = new BackendQueueWindow(client);
            await window.InitializeAsync();
            var snapshot = await Snapshot(client);
            var method = typeof(BackendQueueWindow).GetMethod("RunInvestigationAsync",
                System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic)!;
            var confirmed = await (Task<bool>)method.Invoke(window,
                new object?[] { Repository, Digest(snapshot), null, null, null, false })!;
            var progress = (System.Windows.Controls.TextBlock)typeof(BackendQueueWindow)
                .GetField("_launchProgress", System.Reflection.BindingFlags.Instance
                    | System.Reflection.BindingFlags.NonPublic)!.GetValue(window)!;
            if (confirmed || !progress.Text.Contains("Saved state is busy")
                || progress.Text.Contains("scheduled reconciliation"))
                throw new InvalidDataException("Actual launch contention did not reach the window accurately.");
            await client.StopAcceptingAndDrainAsync();
            App.Record("native_launch_busy_passed", new { actual_window_consumer = true,
                model_calls = 0, github_mutations = false });
            return;
        }
        if (testCase == "scheduled_malformed")
        {
            var window = new BackendQueueWindow(client);
            await window.InitializeAsync();
            bool Enabled(string field) => ((System.Windows.UIElement)typeof(BackendQueueWindow)
                .GetField(field, System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic)!
                .GetValue(window)!).IsEnabled;
            if (!Enabled("_actions")) throw new InvalidDataException("Fixture did not establish initially usable local controls.");
            File.WriteAllText(Path.Combine(Path.GetDirectoryName(launchFile)!, "malformed-snapshot.ready"), "controlled diagnostic only");
            var rejected = false;
            try { await window.RunScheduledAsync("run_" + new string('a', 20), notify: false); }
            catch (Exception error) when (error is InvalidDataException or InvalidOperationException) { rejected = true; }
            if (!rejected || Enabled("_actions") || Enabled("_prepare") || Enabled("_permissions")
                || typeof(BackendQueueWindow).GetField("_refreshRequired", System.Reflection.BindingFlags.Instance
                    | System.Reflection.BindingFlags.NonPublic)!.GetValue(window) is not true)
                throw new InvalidDataException("Malformed scheduled refresh left stale decision/access controls usable.");
            await client.StopAcceptingAndDrainAsync();
            App.Record("native_scheduled_malformed_passed", new { stale_controls_disabled = true,
                actual_hidden_window_flow = true, model_calls = 0, github_mutations = false });
            return;
        }
        if (testCase is "scheduled" or "scheduled_terminal_busy")
        {
            // Exercise the actual hidden window flow after its cached context
            // becomes stale, not only the standalone receipt parser.
            var window = new BackendQueueWindow(client);
            await window.InitializeAsync();
            var initial = await Snapshot(client);
            await SaveMessage(client, initial, "Scheduled stale-owner regression: answer this new exact question.");
            var current = await Snapshot(client);
            if (Digest(initial) == Digest(current)) throw new InvalidDataException("Scheduled context fixture did not change.");
            var runId = "run_" + new string('a', 20);
            if (await window.RunScheduledAsync(runId, notify: false) != "completed" || window.IsVisible)
                throw new InvalidDataException("Fresh scheduled work did not complete in the hidden existing window.");
            if (await window.RunScheduledAsync(runId, notify: false) != "quiet")
                throw new InvalidDataException("Consumed scheduled work was replayed or not quietly verified.");
            await client.StopAcceptingAndDrainAsync();
            App.Record("native_scheduled_roundtrip_passed", new { cached_context_changed = true,
                actual_hidden_window_flow = true, duplicate_quiet = true, model_calls = 0, github_mutations = false });
            return;
        }
        var rounds = testCase is "resume_feedback" or "retry_interrupted" ? 2 : 1;
        string? firstJob = null;
        for (var round = 0; round < rounds; round++)
        {
            var snapshot = await Snapshot(client);
            if (round > 0 && testCase == "resume_feedback") {
                await SaveMessage(client, snapshot, "Second exact question: continue the owned session with this message.");
                snapshot = await Snapshot(client);
            }
            var status = await client.SendAsync(Read("supervisor_status"));
            var savedView = NativeSupervisorView.Parse(status, Repository, Digest(snapshot));
            var policy = savedView.LaunchPolicy;
            if (policy?.Configured != true) throw new InvalidDataException("Chosen policy did not reach the actual native consumer.");
            var previewReady = new TaskCompletionSource<NativeLaunchFrame>(TaskCreationOptions.RunContinuationsAsynchronously);
            using var cancellation = new CancellationTokenSource();
            var request = new JsonObject { ["schema_version"] = 1, ["request_id"] = "launch_" + Guid.NewGuid().ToString("N"),
                ["operation"] = "supervisor_start", ["repository"] = Repository, ["context_digest"] = Digest(snapshot) };
            if (testCase == "retry_interrupted" && round > 0) {
                if (savedView.RetryJobId != firstJob) throw new InvalidDataException("Retry did not identify the exact failed parent.");
                request["retry_job_id"] = firstJob;
            }
            var launch = client.StartSupervisorAsync(request, frame => {
                if (frame.Event == "permission_preview" && !previewReady.TrySetResult(frame))
                    throw new InvalidDataException("This controlled child should issue exactly one access request.");
            }, cancellation.Token);
            var ready = await Task.WhenAny(previewReady.Task, launch).WaitAsync(TimeSpan.FromSeconds(30));
            if (ready != previewReady.Task) { await launch; throw new InvalidDataException("The configured child did not publish its expected preview."); }
            var preview = await previewReady.Task;
            var current = await Snapshot(client);
            var permissions = NativePermissionView.Parse(await client.SendAsync(Read("supervisor_permissions")), Repository, Digest(current));
            var permission = permissions.Single();
            if (!NativeLaunchStream.MatchesPermission(preview.Value, permission, Digest(current))
                || !NativeLaunchStream.DescribePreview(preview.Value).Contains("cat synthetic-fixture.txt"))
                throw new InvalidDataException("Native scope differs from the independent configured job/permission journal.");
            if (Digest(current) == Digest(snapshot) && round == 0)
                throw new InvalidDataException("Fresh verified workspace receipt did not change the initial context.");
            if (testCase == "disconnect" || testCase == "retry_interrupted" && round == 0) cancellation.Cancel();
            else if (testCase == "stale_feedback")
            {
                await SaveMessage(client, current, "New context while agent waits: old access must never be granted.");
                var rejected = false;
                try { await Decide(client, permission, "allow_once", Digest(current)); }
                catch (InvalidDataException) { rejected = true; }
                if (!rejected) throw new InvalidDataException("Changed context accepted an old access grant.");
            }
            else await Decide(client, permission, testCase is "resume_feedback" or "retry_interrupted" ? "allow_once" : testCase, Digest(current));
            if (testCase is "disconnect" or "stale_feedback" || testCase == "retry_interrupted" && round == 0)
            {
                var failed = false;
                try { await launch.WaitAsync(TimeSpan.FromSeconds(15)); }
                catch (Exception exception) when (exception is OperationCanceledException or InvalidDataException) { failed = true; }
                if (!failed) throw new InvalidDataException("A disconnected or stale investigation claimed completion.");
                // Actual OS cleanup is additionally verified from WSL after this
                // native process exits. A status read recovers owner history only.
                var recovered = await client.SendAsync(Read("supervisor_status"));
                if (testCase == "retry_interrupted") {
                    firstJob = permission.JobId;
                    var latest = await Snapshot(client);
                    if (NativeSupervisorView.Parse(recovered, Repository, Digest(latest)).RetryJobId != firstJob)
                        throw new InvalidDataException("Disconnected attempt was not explicitly recoverable.");
                    continue;
                }
                break;
            }
            var terminal = await launch.WaitAsync(TimeSpan.FromSeconds(30));
            current = await Snapshot(client);
            var view = NativeSupervisorView.Parse(await client.SendAsync(Read("supervisor_status")), Repository, Digest(current));
            if (!view.DescribeTerminal(terminal).StartsWith("Agent results are saved"))
                throw new InvalidDataException("Real configured completion was not independently verified.");
            var item = QueueProposal.Parse(current["proposals"]![0], current["plan_id"]!.GetValue<string>());
            if (!view.Describe(item).Contains("Exact fixture question answered."))
                throw new InvalidDataException("Saved question/answer did not reach the native display model.");
            if (round > 0 && (terminal["job_id"]!.GetValue<string>() == firstJob || terminal["reused"]!.GetValue<bool>()))
                throw new InvalidDataException("New feedback did not cause a new context-bound turn.");
            firstJob = terminal["job_id"]!.GetValue<string>();
        }
        App.Record("native_launch_roundtrip_passed", new { test_case = testCase, rounds,
            actual_windows_wsl_transport = true, exact_preview_journal_binding = true, external_provider = "controlled",
            model_calls = 0, github_mutations = false, visible_window_acceptance = false });
    }

    private static async Task Decide(BackendClient client, NativePermission permission, string choice, string digest)
    {
        var response = await client.SendAsync(new JsonObject {
            ["schema_version"] = 1, ["request_id"] = "permission_" + Guid.NewGuid().ToString("N"),
            ["operation"] = "supervisor_permission_decide", ["repository"] = Repository,
            ["connection_id"] = permission.ConnectionId, ["permission_id"] = permission.PermissionId,
            ["digest"] = permission.Digest, ["decision"] = choice
        });
        NativePermissionView.ParseDecision(response, Repository, digest, permission, choice);
    }
}
