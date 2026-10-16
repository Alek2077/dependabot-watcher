using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json.Nodes;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;

namespace WatcherNativeProof;

// Persistent guidance only. Python owns completion evidence and gates; the
// existing local-update preview/confirmation remains the only Git writer.
internal sealed partial class BackendQueueWindow
{
    private readonly StackPanel _completionPanel = new() { Visibility = Visibility.Collapsed,
        Margin = new Thickness(0, 0, 0, 12) };
    private readonly TextBlock _completionStatus = new() { TextWrapping = TextWrapping.Wrap };
    private readonly StackPanel _completionActions = new();
    private readonly Dictionary<string, (TextBlock Status, StackPanel Actions)> _completionRows = new();

    private void InitializeCompletion(StackPanel inspector)
    {
        var description = new StackPanel();
        description.Children.Add(new TextBlock { Text = "Post-merge verification", FontSize = 16, FontWeight = FontWeights.SemiBold,
            Margin = new Thickness(0, 0, 0, 6), TextWrapping = TextWrapping.Wrap });
        description.Children.Add(_completionStatus);
        _completionPanel.Children.Add(description);
        AddCopyIcon(_completionStatus, description);
        _completionPanel.Children.Add(_completionActions);
        inspector.Children.Add(_completionPanel);
    }

    private void RenderCompletion()
    {
        _completionActions.Children.Clear();
        _completionRows.Clear();
        var pending = _reviews.Where(review => review.HasPendingCompletion).ToList();
        _maintenanceWorkspace.Header = pending.Count == 0 ? "Maintenance" : $"Maintenance · {pending.Count} pending";
        _completionPanel.Visibility = pending.Count == 0 ? Visibility.Collapsed : Visibility.Visible;
        if (pending.Count == 0) return;
        _completionStatus.Text = "Saved follow-up status. A completed merge is not repeated; checking status does not update your checkout.";
        foreach (var review in pending)
        {
            var stage = review.Value["completion_stage"]?.GetValue<string>();
            var reason = stage switch {
                "assessment" => "No accepted post-merge assessment is saved. Verification is unfinished, not the merge.",
                "refresh" => "Post-merge evidence has not been recorded.",
                "local_sync" => "A separately approved local update is still required by the completion policy.",
                "merge_outcome" => "The exact merge outcome still needs verification. Do not merge again.",
                "verification" => "Final verification has not been recorded.",
                _ => "Saved follow-up needs checking before another merge."
            };
            var status = new TextBlock { Text = $"{review.Binding["repository"]} · PR #{review.Binding["pull_request"]}\n{reason}",
                TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 8, 0, 4) };
            var row = new StackPanel();
            row.Children.Add(status);
            AddCopyIcon(status);
            var actions = new StackPanel();
            row.Children.Add(actions);
            _completionActions.Children.Add(row);
            _completionRows.Add(review.Id!, (status, actions));
            actions.Children.Add(Button($"Refresh follow-up status · PR #{review.Binding["pull_request"]}",
                () => CheckCompletionAsync(review)));
        }
    }

    private async Task CheckCompletionAsync(NativeActionReview review)
    {
        if (MaintenanceOperationBlocked)
        {
            _completionStatus.Text = $"PR #{review.Binding["pull_request"]}: work is already in progress. No new check or merge was started. Review the activity status and any pending access requests.";
            return;
        }
        SetBusy(true);
        try { await CheckCompletionCoreAsync(review); }
        catch (Exception exception)
        {
            _completionStatus.Text = "Maintenance completion could not be verified. " + NativeDisplayText.Escape(exception.Message)
                + " No merge or local update was retried.";
        }
        finally { SetBusy(false); }
    }

    private async Task RecheckPendingCompletionsAsync()
    {
        // Internal callers may own _busy; investigation ownership is distinct.
        if (_shutdownRequested || InvestigationOwnsMaintenance) return;
        foreach (var review in _reviews.Where(item => item.HasPendingCompletion).ToList())
        {
            if (_shutdownRequested) return;
            try { await CheckCompletionCoreAsync(review, reveal: false); }
            catch (Exception exception)
            {
                _completionPanel.Visibility = Visibility.Visible;
                _completionStatus.Text = "Maintenance completion still needs checking. "
                    + NativeDisplayText.Escape(exception.Message) + " No action was retried.";
            }
        }
    }

    private async Task CheckCompletionCoreAsync(NativeActionReview review, bool reveal = true)
    {
        if (review.Id is not string id || review.Value["completion"] is not JsonObject) return;
        if (reveal) _workspaces.SelectedItem = _maintenanceWorkspace;
        if (!_completionRows.TryGetValue(id, out var row))
        {
            RenderCompletion();
            if (!_completionRows.TryGetValue(id, out row)) return;
        }
        _completionPanel.Visibility = Visibility.Visible;
        _completionStatus.Text = "Checking saved assessment and fresh remote evidence"
            + (review.Value["completion"]?["policy"]?.GetValue<string>() == "selected_local_sync" ? " and selected local checkouts" : "")
            + "…";
        row.Status.Text = $"{review.Binding["repository"]} · PR #{review.Binding["pull_request"]}\nChecking follow-up status…";
        var request = Request("check_completion"); request["review_id"] = id;
        var response = await _client.SendAsync(request);
        var completion = response["completion"] as JsonObject
            ?? throw new InvalidDataException("Completion response is missing.");
        var stage = completion["stage"]?.GetValue<string>();
        if (completion["review_id"]?.GetValue<string>() != id
            || stage is not ("merge_outcome" or "refresh" or "assessment" or "local_sync" or "verification" or "blocked" or "completed")
            || completion["complete"]?.GetValue<bool>() != (stage == "completed")
            || string.IsNullOrWhiteSpace(completion["message"]?.GetValue<string>()))
            throw new InvalidDataException("Completion response differs from the exact saved action.");
        _completionStatus.Text = $"Checked {DateTimeOffset.Now:HH:mm:ss zzz} · "
            + NativeDisplayText.Escape(completion["message"]!.GetValue<string>());
        row.Status.Text = $"{review.Binding["repository"]} · PR #{review.Binding["pull_request"]}\n" + _completionStatus.Text;
        row.Actions.Children.Clear();
        if (stage == "refresh")
            row.Actions.Children.Add(Button("Refresh post-merge evidence", CollectAsync));
        else if (stage == "assessment")
            row.Actions.Children.Add(Button("Continue post-merge assessment", async () => {
                if (_busy || _shutdownRequested || _scheduledRunning || _investigationCancellation is not null) return;
                SetBusy(true);
                try { await ContinueAfterMergeAsync(review, allowRecovery: true); }
                finally { SetBusy(false); }
            }));
        else if (stage == "local_sync")
        {
            _completionStatus.Text += " Review the selected checkout updates below. Each update needs its own exact confirmation.";
            await ReadLocalSyncTargetsAsync(review);
        }
        else if (stage == "merge_outcome")
            row.Actions.Children.Add(Button("Check exact merge outcome", () => CheckReviewOutcomeAsync(review)));
        if (stage != "completed")
            row.Actions.Children.Add(Button("Check maintenance progress", () => CheckCompletionAsync(review)));
        else
        {
            // Reload durable state without inference or mutations so merge
            // controls observe the newly recorded completion gate.
            Apply(await _client.SendAsync(Request("snapshot")));
            var recorded = _reviews.SingleOrDefault(item => item.Id == id);
            if (recorded?.RecordedMaintenanceSummary is null)
                throw new InvalidDataException("Saved completion changed or its outcome still needs recovery. No completed maintenance is being claimed.");
            ShowActionResult(recorded, reveal);
            await ReadLocalSyncTargetsAsync();
        }
    }
}
