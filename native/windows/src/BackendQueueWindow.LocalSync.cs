using System;
using System.IO;
using System.Linq;
using System.Threading;
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
    private long _localSyncReadGeneration;
    private Task _localObservationTask = Task.CompletedTask;
    private CancellationTokenSource? _localObservationCancellation;

    private void InitializeLocalSync(StackPanel inspector)
    {
        var description = new StackPanel();
        description.Children.Add(new TextBlock { Text = "Local checkouts", FontSize = 16, FontWeight = FontWeights.SemiBold,
            Margin = new Thickness(0, 8, 0, 6) });
        description.Children.Add(_localSyncStatus);
        _localSyncPanel.Children.Add(description);
        AddCopyIcon(_localSyncStatus, description);
        // The inspector owns scrolling. A nested ScrollViewer swallows wheel
        // input over checkout rows even when its contents fit without scrolling.
        _localSyncPanel.Children.Add(_localSyncTargets);
        inspector.Children.Add(_localSyncPanel);
    }

    private static JsonObject LocalSyncRequest(string operation) => new() {
        ["schema_version"] = 1, ["request_id"] = "sync_" + Guid.NewGuid().ToString("N"),
        ["operation"] = operation
    };

    private async Task ReadLocalSyncTargetsAsync(NativeActionReview? currentLocalStep = null)
    {
        var generation = ++_localSyncReadGeneration;
        _localObservationCancellation?.Cancel();
        try
        {
            var response = await _client.SendAsync(LocalSyncRequest("local_sync_targets"));
            if (response["targets"] is not JsonArray targets || response["reviews"] is not JsonArray reviews)
                throw new InvalidDataException("Local checkout state is unavailable.");
            if (_shutdownRequested || generation != _localSyncReadGeneration) return;
            RenderLocalSyncTargets(targets, reviews, null, currentLocalStep, observing: true);
            // History/recovery and the queue never await slow network status.
            var previous = _localObservationTask;
            var cancellation = new CancellationTokenSource();
            _localObservationCancellation = cancellation;
            _localObservationTask = ObserveLocalSyncTargetsAsync(targets, reviews, currentLocalStep, generation, previous, cancellation);
        }
        catch (Exception)
        {
            if (_shutdownRequested || generation != _localSyncReadGeneration) return;
            _localSyncPanel.Visibility = Visibility.Visible;
            _localSyncTargets.Children.Clear();
            _localSyncStatus.Text = "Local checkout status could not be loaded. Refresh saved state before reviewing an update.";
        }
    }

    private async Task ObserveLocalSyncTargetsAsync(JsonArray targets, JsonArray reviews,
        NativeActionReview? currentLocalStep, long generation, Task previous, CancellationTokenSource cancellation)
    {
        JsonArray? observations = null;
        try {
            await previous; // Coalesce refreshes: never run two optional status children at once.
            if (_shutdownRequested || generation != _localSyncReadGeneration || cancellation.IsCancellationRequested) return;
            var observed = await _client.ObserveLocalSyncAsync(LocalSyncRequest("local_sync_status"), cancellation.Token);
            observations = observed["observations"] as JsonArray;
        }
        catch (Exception) { /* The bounded read cannot hide history/recovery. */ }
        finally {
            if (ReferenceEquals(_localObservationCancellation, cancellation)) _localObservationCancellation = null;
            cancellation.Dispose();
        }
        if (_shutdownRequested || generation != _localSyncReadGeneration) return;
        RenderLocalSyncTargets(targets, reviews, observations, currentLocalStep);
    }

    private void RenderLocalSyncTargets(JsonArray targets, JsonArray reviews, JsonArray? observations,
        NativeActionReview? currentLocalStep, bool observing = false)
    {
            _localSyncTargets.Children.Clear();
            foreach (var node in targets)
            {
                if (node is not JsonObject target) throw new InvalidDataException("Local checkout identity is invalid.");
                var id = target["target_id"]!.GetValue<string>();
                var label = NativeDisplayText.Escape($"{target["repository"]} · {target["branch"]}\n{target["directory"]}");
                var row = new StackPanel();
                row.Children.Add(new TextBlock { Text = label, TextWrapping = TextWrapping.Wrap,
                    Foreground = SystemColors.GrayTextBrush, Margin = new Thickness(0, 0, 0, 10) });
                JsonObject? observation = null;
                var state = "unknown";
                try {
                    var matching = observations?.OfType<JsonObject>().Where(value =>
                        value["target_id"]?.GetValue<string>() == id).ToArray();
                    if (matching?.Length == 1) {
                        observation = matching[0];
                        state = NativeLocalSyncReview.Observation(observation, target);
                    }
                }
                catch (Exception) { observation = null; } // A live-data fault must not remove recovery controls.
                var needsReview = state == "review_needed";
                var current = state == "current";
                var guidance = current ? "✓ Local checkout matched GitHub at the last check"
                    : needsReview ? "Local update needs review"
                    : observing ? "Checking current checkout status…"
                    : "Local checkout needs checking";
                var message = observation?["message"]?.GetValue<string>()
                    ?? (observing ? "Comparing with GitHub. Your checkout is unchanged; saved history remains available."
                        : "No current comparison is available. A past successful update is not current sync status.");
                var detail = "Read-only observation. No Git objects were fetched and no approval was created.";
                if (observation?["checked_at"]?.GetValue<string>() is string checkedAt)
                    detail += "\nChecked " + checkedAt + ". Changes made afterward require another check.";
                if (state is "current" or "review_needed" && observation?["head_sha"] is JsonNode head)
                    detail += $"\nLocal: {head}\nGitHub: {observation["remote_sha"]}\nCached origin: {observation["tracking_sha"] ?? JsonValue.Create("no matching upstream")}";
                var statusText = new TextBlock { Text = NativeDisplayText.Escape(guidance),
                    TextWrapping = TextWrapping.Wrap, FontSize = 16, FontWeight = FontWeights.SemiBold,
                    Margin = new Thickness(0, 0, 0, 5) };
                row.Children.Add(statusText);
                row.Children.Add(new TextBlock { Text = NativeDisplayText.Escape(message),
                    TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 0, 0, 10) });
                var evidence = new TextBlock { Text = NativeDisplayText.Escape(detail), TextWrapping = TextWrapping.Wrap };
                row.Children.Add(new Expander { Header = "Comparison details and checked time", Content = evidence,
                    Margin = new Thickness(0, 0, 0, 6) });
                AddCopyIcon(evidence);
                if (!current && !SystemParameters.HighContrast)
                {
                    statusText.Foreground = new System.Windows.Media.SolidColorBrush(System.Windows.Media.Color.FromRgb(135, 72, 10));
                }
                var latest = LatestLocalSyncOutcome(reviews, id);
                var applied = latest is not null &&
                    latest["status"]?.GetValue<string>() == "completed"
                    && latest["result"]?["status"]?.GetValue<string>() is "synchronized" or "observed_applied"
                    ? latest : null;
                var hasApplied = applied is not null && JsonNode.DeepEquals(applied["binding"], applied["result"]?["binding"])
                    && new[] { "repository", "directory", "branch" }.All(key => JsonNode.DeepEquals(applied["binding"]?[key], target[key]))
                    && applied["result"]?["mutation_performed"]?.GetValue<bool>() ==
                        (applied["result"]?["status"]?.GetValue<string>() == "synchronized");
                if (hasApplied) {
                    var receipt = new TextBlock { Text = NativeDisplayText.Escape(
                        $"Previously verified: {applied!["binding"]?["target_sha"]}\nHistorical receipt only; the current comparison is shown above."),
                        TextWrapping = TextWrapping.Wrap,
                        Foreground = SystemColors.GrayTextBrush,
                        Margin = new Thickness(0, 8, 0, 6) };
                    row.Children.Add(new Expander { Header = "Previous local update", Content = receipt,
                        Margin = new Thickness(0, 0, 0, 6) });
                    AddCopyIcon(receipt);
                }
                var pending = latest is not null && latest["status"]?.GetValue<string>() is "executing" or "recovery_required"
                    ? latest : null;
                if (latest?["status"]?.GetValue<string>() == "failed")
                    row.Children.Add(new TextBlock { Text = "The latest saved update was not applied. Review current checkout state before another update.",
                        TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 6, 0, 4) });
                if (pending is not null)
                {
                    var reviewId = pending["review_id"]!.GetValue<string>();
                    var binding = pending["binding"]?.DeepClone() as JsonObject
                        ?? throw new InvalidDataException("Saved local update binding is missing.");
                    row.Children.Add(Button("Check saved local update outcome", () => RecoverLocalSyncAsync(reviewId, binding)));
                }
                else
                {
                    var update = Button(current ? "Recheck local checkout" : "Review local update",
                        () => current ? ReadLocalSyncTargetsAsync() : PrepareLocalSyncAsync((JsonObject)target.DeepClone()));
                    update.HorizontalAlignment = HorizontalAlignment.Left;
                    update.Margin = new Thickness(0, 8, 0, 0);
                    update.IsEnabled = !_refreshRequired && !observing;
                    if (_refreshRequired) update.ToolTip = "Refresh saved queue before reviewing a checkout update.";
                    if (!current) { update.Background = PrimaryBrush; update.Foreground = SystemColors.HighlightTextBrush; }
                    row.Children.Add(update);
                }
                foreach (var text in row.Children.OfType<TextBlock>().ToArray()) AddCopyIcon(text);
                _localSyncTargets.Children.Add(new Border { Child = row, Padding = new Thickness(16, 14, 16, 14),
                    Margin = new Thickness(0, 12, 0, 4), BorderThickness = new Thickness(3, 0, 0, 0),
                    BorderBrush = SystemParameters.HighContrast ? SystemColors.WindowTextBrush
                        : new System.Windows.Media.SolidColorBrush(!current
                            ? System.Windows.Media.Color.FromRgb(193, 119, 24) : System.Windows.Media.Color.FromRgb(196, 207, 203)),
                    Background = SystemParameters.HighContrast ? SystemColors.WindowBrush
                        : new System.Windows.Media.SolidColorBrush(System.Windows.Media.Color.FromRgb(247, 249, 248)) });
            }
            _localSyncStatus.Text = _reviews.Any(review => review.HasPendingCompletion
                && review.Value["completion"]?["policy"]?.GetValue<string>() == "selected_local_sync")
                ? "Selected updates are part of your configured maintenance completion. Each update is separately approved; no automatic integration."
                : "Optional and separately approved. Remote merges do not update these checkouts automatically.";
            _localSyncPanel.Visibility = targets.Count == 0 ? Visibility.Collapsed : Visibility.Visible;
    }

    private static JsonObject? LatestLocalSyncOutcome(JsonArray reviews, string targetId) => reviews.OfType<JsonObject>()
        .Where(review => review["binding"]?["target_id"]?.GetValue<string>() == targetId
            && review["status"]?.GetValue<string>() != "awaiting_confirmation")
        // New backend receipts carry causal order; expiry is only a fallback
        // for legacy history. It never changes the approval deadline.
        .OrderBy(review => review["sequence"]?.GetValue<long>() ?? 0)
        .ThenBy(review => DateTimeOffset.Parse(review["expires_at"]!.GetValue<string>())).LastOrDefault();

    private async Task PrepareLocalSyncAsync(JsonObject target)
    {
        if (MaintenanceOperationBlocked || _refreshRequired) return;
        SetBusy(true);
        ShowActionNotice("Checking local checkout update", "Checking the selected branch and current remote commit. Missing Git objects may be downloaded; no branch integration is being performed.");
        try
        {
            var request = LocalSyncRequest("prepare_local_sync"); request["target_id"] = target["target_id"]!.GetValue<string>();
            request["tracking_refresh_supported"] = true;
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
            var description = NativeDisplayText.Escape($"{binding["repository"]} · {binding["branch"]}\n{binding["directory"]}\n\n{binding["head_sha"]} → {binding["target_sha"]}\n{binding["commits"]} commits\n\nUpdates this clean local branch, its index and tracked files. {NativeLocalSyncReview.TrackingDescription(binding)} No tests, push, remote merge, stash, reset, branch switch, dependency installation or deployment.\n\nUpdate this checkout now?");
            if (!NativeConfirmationDialog.Confirm(this, "Confirm selected local update", description, "Update this checkout"))
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
        if (MaintenanceOperationBlocked) return;
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
