using System;
using System.IO;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;

namespace WatcherNativeProof;

internal sealed partial class BackendQueueWindow
{
    private readonly StackPanel _localSyncPanel = new() { Visibility = Visibility.Collapsed,
        Margin = new Thickness(0, 0, 0, 12) };
    private readonly StackPanel _localSyncTargets = new();
    private readonly TextBlock _localSyncStatus = new() { TextWrapping = TextWrapping.Wrap };

    private void InitializeLocalSync(DockPanel inspector)
    {
        _localSyncPanel.Children.Add(new TextBlock { Text = "Your selected local checkouts", FontSize = 16,
            FontWeight = FontWeights.SemiBold });
        _localSyncPanel.Children.Add(_localSyncStatus);
        _localSyncPanel.Children.Add(new ScrollViewer { Content = _localSyncTargets, MaxHeight = 150,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto });
        DockPanel.SetDock(_localSyncPanel, Dock.Top);
        inspector.Children.Add(_localSyncPanel);
    }

    private static JsonObject LocalSyncRequest(string operation) => new() {
        ["schema_version"] = 1, ["request_id"] = "sync_" + Guid.NewGuid().ToString("N"),
        ["operation"] = operation
    };

    private async Task ReadLocalSyncTargetsAsync()
    {
        try
        {
            var response = await _client.SendAsync(LocalSyncRequest("local_sync_targets"));
            if (response["targets"] is not JsonArray targets || response["reviews"] is not JsonArray reviews)
                throw new InvalidDataException("Local checkout state is unavailable.");
            _localSyncTargets.Children.Clear();
            foreach (var node in targets)
            {
                if (node is not JsonObject target) throw new InvalidDataException("Local checkout identity is invalid.");
                var id = target["target_id"]!.GetValue<string>();
                var label = NativeDisplayText.Escape($"{target["repository"]} · {target["branch"]}\n{target["directory"]}");
                var row = new StackPanel();
                row.Children.Add(new TextBlock { Text = label, TextWrapping = TextWrapping.Wrap });
                var pending = reviews.OfType<JsonObject>().LastOrDefault(review =>
                    review["binding"]?["target_id"]?.GetValue<string>() == id
                    && review["status"]?.GetValue<string>() is "executing" or "recovery_required");
                if (pending is not null)
                {
                    var reviewId = pending["review_id"]!.GetValue<string>();
                    var binding = pending["binding"]?.DeepClone() as JsonObject
                        ?? throw new InvalidDataException("Saved local update binding is missing.");
                    row.Children.Add(Button("Check saved local update outcome", () => RecoverLocalSyncAsync(reviewId, binding)));
                }
                else row.Children.Add(Button("Review local update", () => PrepareLocalSyncAsync((JsonObject)target.DeepClone())));
                _localSyncTargets.Children.Add(row);
            }
            _localSyncStatus.Text = _reviews.Any(review => review.HasPendingCompletion
                && review.Value["completion"]?["policy"]?.GetValue<string>() == "selected_local_sync")
                ? "Selected updates are part of your configured maintenance completion. Each update is separately approved; no automatic integration."
                : "Optional and separately approved. Remote merges do not update these checkouts automatically.";
            _localSyncPanel.Visibility = targets.Count == 0 ? Visibility.Collapsed : Visibility.Visible;
        }
        catch (Exception)
        {
            _localSyncPanel.Visibility = Visibility.Visible;
            _localSyncTargets.Children.Clear();
            _localSyncStatus.Text = "Local checkout status could not be loaded. Refresh saved state before reviewing an update.";
        }
    }

    private async Task PrepareLocalSyncAsync(JsonObject target)
    {
        if (_busy || _shutdownRequested || _scheduledRunning || _investigationCancellation is not null) return;
        SetBusy(true);
        ShowActionNotice("Checking local checkout update", "Checking the selected branch and current remote commit. Missing Git objects may be downloaded; no branch integration is being performed.");
        try
        {
            var request = LocalSyncRequest("prepare_local_sync"); request["target_id"] = target["target_id"]!.GetValue<string>();
            var response = await _client.SendAsync(request);
            var review = NativeLocalSyncReview.Preparation(response["review"], target);
            if (_shutdownRequested) return;
            if (review["status"]?.GetValue<string>() == "up_to_date")
            {
                ShowActionNotice("Local checkout is up to date", "The selected branch already matches its remote branch. Nothing was integrated.");
                return;
            }
            if (review["status"]?.GetValue<string>() != "ready" || review["binding"] is not JsonObject binding)
                throw new InvalidDataException("Local update is not ready for confirmation.");
            var description = NativeDisplayText.Escape($"{binding["repository"]} · {binding["branch"]}\n{binding["directory"]}\n\n{binding["head_sha"]} → {binding["target_sha"]}\n{binding["commits"]} commits\n\nUpdates only this clean local branch, its index and tracked files. No tests, push, remote merge, stash, reset, branch switch, dependency installation or deployment.\n\nUpdate this checkout now?");
            if (MessageBox.Show(this, description, "Confirm selected local update", MessageBoxButton.YesNo,
                                MessageBoxImage.Question, MessageBoxResult.No) != MessageBoxResult.Yes)
            {
                ShowActionNotice("Local update cancelled", "Nothing was integrated. The selected checkout remains unchanged.");
                return;
            }
            var confirm = LocalSyncRequest("confirm_local_sync");
            confirm["review_id"] = review["review_id"]!.GetValue<string>();
            confirm["approval_token"] = review["approval_token"]!.GetValue<string>();
            ShowActionNotice("Updating selected local checkout", "Rechecking the exact reviewed state, then applying only the approved fast-forward. This update will not be retried automatically.");
            var result = await _client.SendAsync(confirm);
            NativeLocalSyncReview.Confirmed(result["review"], review);
            ShowActionNotice("Selected local checkout updated", NativeDisplayText.Escape($"{binding["repository"]} · {binding["branch"]}\n{binding["directory"]}")
                + "\nLocal branch and tracked files now match the approved remote commit. No tests, push, remote merge, dependency installation or deployment was performed.");
        }
        catch (Exception exception)
        {
            ShowActionNotice("Local update was not confirmed", $"{exception.Message}\nRefresh saved state and check any saved outcome. Nothing will be retried automatically.");
        }
        finally { await ReadLocalSyncTargetsAsync(); await RecheckPendingCompletionsAsync(); SetBusy(false); }
    }

    private async Task RecoverLocalSyncAsync(string reviewId, JsonObject binding)
    {
        if (_busy || _shutdownRequested || _scheduledRunning || _investigationCancellation is not null) return;
        SetBusy(true);
        ShowActionNotice("Checking saved local update", "Observing the checkout and saved attempt. No integration is being retried.");
        try
        {
            var request = LocalSyncRequest("recover_local_sync"); request["review_id"] = reviewId;
            var response = await _client.SendAsync(request);
            var status = NativeLocalSyncReview.Recovered(response["review"], reviewId, binding);
            ShowActionNotice(status == "observed_applied" ? "Local update verified"
                : status == "observed_not_applied" ? "Local update was not applied" : "Local checkout needs manual repair",
                status is "observed_applied" or "observed_not_applied"
                    ? "Saved outcome checked. No integration was retried."
                    : "The checkout has local changes and the update outcome is uncertain. Saved as requiring repair; no rollback or retry was performed. Preserve local work and resolve it through your normal workflow before another update.");
        }
        catch (Exception exception) { ShowActionNotice("Local update needs investigation", exception.Message + "\nNo recovery mutation was performed."); }
        finally { await ReadLocalSyncTargetsAsync(); await RecheckPendingCompletionsAsync(); SetBusy(false); }
    }
}
