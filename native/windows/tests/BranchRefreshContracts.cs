using System;
using System.IO;
using System.Reflection;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Controls;

internal static class BranchRefreshContracts
{
    internal static void Run(Assembly assembly)
    {
        Exception? failure = null;
        var thread = new Thread(() => {
            try {
                var type = assembly.GetType("WatcherNativeProof.BackendQueueWindow", true)!;
                var parse = type.GetMethod("ParseBranchRefresh", BindingFlags.Static | BindingFlags.NonPublic)!;
                var binding = new JsonObject { ["repository"] = "example-org/example-app", ["number"] = 319,
                    ["head_branch"] = "dependabot/example", ["base_branch"] = "staging", ["head_sha"] = new string('a', 40), ["base_sha"] = new string('b', 40) };
                var record = new JsonObject { ["review_id"] = "refresh_" + new string('a', 32), ["binding"] = binding,
                    ["sequence"] = 1L, ["status"] = "awaiting_confirmation" };
                parse.Invoke(null, new object[] { record });
                record["status"] = "updated";
                var refused = false;
                try { parse.Invoke(null, new object[] { record }); }
                catch (TargetInvocationException ex) when (ex.InnerException is InvalidDataException) { refused = true; }
                if (!refused) throw new InvalidDataException("Updated branch was shown without observed revision/base evidence.");
                record["observed_head"] = new string('c', 40); record["base_current"] = true;
                parse.Invoke(null, new object[] { record });
                var clientType = assembly.GetType("WatcherNativeProof.BackendClient", true)!;
                var client = Activator.CreateInstance(clientType, BindingFlags.Instance | BindingFlags.NonPublic, null,
                    new object[] { Array.Empty<string>(), true }, null)!;
                var window = Activator.CreateInstance(type, BindingFlags.Instance | BindingFlags.NonPublic, null, new[] { client }, null)!;
                object Field(string name) => type.GetField(name, BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(window)!;
                ((TabItem)Field("_selectedWorkspace")).ApplyTemplate();
                if (((TextBlock)Field("_branchRefreshStatus")).FontWeight != System.Windows.FontWeights.Normal)
                    throw new InvalidDataException("Selected-tab typography makes body/status text bold.");
                var proposalType = assembly.GetType("WatcherNativeProof.QueueProposal", true)!;
                object Proposal(char id, string repo, string kind, int number) => proposalType.GetMethod("Parse", BindingFlags.Static | BindingFlags.NonPublic)!.Invoke(null, new object[] {
                    new JsonObject { ["proposal_id"] = "proposal_" + new string(id, 16), ["version"] = 0, ["plan_id"] = "plan_" + new string('a', 16),
                        ["repository"] = repo, ["handling"] = "pending", ["evidence_status"] = "complete", ["feedback"] = new JsonArray(),
                        ["step"] = new JsonObject { ["kind"] = kind, ["action"] = "investigate", ["order"] = 1, ["summary"] = "Synthetic candidate",
                            ["identity"] = new JsonObject { ["repository"] = repo, ["number"] = number } } }, "plan_" + new string('a', 16) })!;
                var items = Array.CreateInstance(proposalType, 4);
                items.SetValue(Proposal('a', "example-org/example-app", "alert", 99), 0);
                items.SetValue(Proposal('b', "example-org/example-app", "pull_request", 319), 1);
                items.SetValue(Proposal('c', "other/repository", "pull_request", 319), 2);
                items.SetValue(Proposal('d', "example-org/example-app", "pull_request", 320), 3);
                var queue = (DataGrid)Field("_queue"); queue.ItemsSource = items; queue.SelectedItem = items.GetValue(0);
                var candidates = (ComboBox)Field("_branchRefreshCandidate");
                if (candidates.Items.Count != 2 || candidates.SelectedItem is not null)
                    throw new InvalidDataException("Alert branch-update candidates leak repositories or silently choose an unrelated PR.");
                queue.SelectedItem = items.GetValue(1);
                if (!ReferenceEquals(candidates.SelectedItem, queue.SelectedItem))
                    throw new InvalidDataException("Selected PR did not become its own branch-update target.");
                if (((Button)Field("_reviewBranchRefresh")).IsEnabled)
                    throw new InvalidDataException("Unverified queue enables remote branch-update review.");
                ((Task)type.GetMethod("PrepareBranchRefreshAsync", BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(window, null)!).GetAwaiter().GetResult();
                // A new empty selection must not crash, and saved terminal
                // outcomes must survive reopening without suggesting a retry.
                queue.SelectedItem = null;
                type.GetMethod("RefreshBranchRefreshCandidates", BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(window, null);
                queue.SelectedItem = items.GetValue(1);
                var earlier = (JsonObject)record.DeepClone();
                earlier["status"] = "rejected"; earlier["rejection_status"] = 422; earlier["sequence"] = 1L;
                earlier["observed_head"] = null; earlier["base_current"] = null;
                var later = (JsonObject)earlier.DeepClone();
                later["review_id"] = "refresh_" + new string('0', 32); later["sequence"] = 2L; later["status"] = "closed";
                type.GetMethod("ReadBranchRefreshReviews", BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(window,
                    new object[] { new JsonObject { ["branch_refresh_reviews"] = new JsonArray(later, earlier) } });
                if (!((TextBlock)Field("_branchRefreshStatus")).Text.Contains("PR closed")
                    || ((Button)Field("_checkBranchRefresh")).Visibility != System.Windows.Visibility.Collapsed)
                    throw new InvalidDataException("Reopened outcome is unordered or presents a closed operation as pending.");
                queue.ItemsSource = Array.CreateInstance(proposalType, 0);
                later["status"] = "awaiting_update";
                type.GetMethod("ReadBranchRefreshReviews", BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(window,
                    new object[] { new JsonObject { ["branch_refresh_reviews"] = new JsonArray(later.DeepClone()) } });
                var history = (StackPanel)Field("_branchRefreshHistory");
                var checkCount = 0;
                foreach (var child in history.Children) if (child is Button) checkCount++;
                if (checkCount != 2 || !((TabItem)Field("_maintenanceWorkspace")).Header.ToString()!.Contains("1 pending"))
                    throw new InvalidDataException("Retired PR lost its independent Maintenance outcome-check route.");
                queue.ItemsSource = items; queue.SelectedItem = items.GetValue(1); queue.SelectedItem = items.GetValue(0);
                if (candidates.SelectedItem is not null)
                    throw new InvalidDataException("Switching from a PR to an alert retained its PR branch-update recipient.");
                var selectedReview = type.GetMethod("SelectedBranchRefresh", BindingFlags.Instance | BindingFlags.NonPublic)!;
                if (selectedReview.Invoke(window, null) is not null)
                    throw new InvalidDataException("Alert without an explicit candidate acquired another PR's continuation.");
                queue.SelectedItem = items.GetValue(3);
                if (selectedReview.Invoke(window, null) is not null)
                    throw new InvalidDataException("Unrelated selected PR acquired pending continuation.");
                type.GetField("_agentStatusUnverified", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(window, true);
                type.GetProperty("_refreshRequired", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(window, false);
                type.GetMethod("SetBusy", BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(window, new object[] { false });
                if (!history.IsEnabled || (bool)type.GetProperty("BranchMaintenanceBlocked", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(window)!)
                    throw new InvalidDataException("Unavailable agent status blocks independent saved-branch recovery.");
            } catch (Exception ex) { failure = ex; }
        });
        thread.SetApartmentState(ApartmentState.STA); thread.Start(); thread.Join();
        if (failure is not null) throw new InvalidDataException("Branch-update contracts failed.", failure);
        Console.WriteLine("Branch-update contracts passed: observation proof, candidate scope, explicit recipient and stale-queue guard.");
    }
}
