using System;
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

    private void InitializeCompletion(StackPanel inspector)
    {
        _completionPanel.Children.Add(new TextBlock { Text = "Post-merge verification · separate from the selected queue item", FontSize = 16,
            FontWeight = FontWeights.SemiBold });
        _completionPanel.Children.Add(_completionStatus);
        _completionPanel.Children.Add(_completionActions);
        inspector.Children.Add(_completionPanel);
    }

    private void RenderCompletion()
    {
        _completionActions.Children.Clear();
        var pending = _reviews.Where(review => review.HasPendingCompletion).ToList();
        _completionPanel.Visibility = pending.Count == 0 ? Visibility.Collapsed : Visibility.Visible;
        if (pending.Count == 0) return;
        _completionStatus.Text = "The remote merge is recorded. Verification below is a separate follow-up, not another merge. Check its status to see the next required step.";
        foreach (var review in pending)
            _completionActions.Children.Add(Button($"Check next step · PR #{review.Binding["pull_request"]}",
                () => CheckCompletionAsync(review)));
    }

    private async Task CheckCompletionAsync(NativeActionReview review)
    {
        if (_busy || _shutdownRequested || _scheduledRunning || _investigationCancellation is not null)
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
        foreach (var review in _reviews.Where(item => item.HasPendingCompletion).ToList())
        {
            if (_shutdownRequested) return;
            try { await CheckCompletionCoreAsync(review); }
            catch (Exception exception)
            {
                _completionPanel.Visibility = Visibility.Visible;
                _completionStatus.Text = "Maintenance completion still needs checking. "
                    + NativeDisplayText.Escape(exception.Message) + " No action was retried.";
            }
        }
    }

    private async Task CheckCompletionCoreAsync(NativeActionReview review)
    {
        if (review.Id is not string id || review.Value["completion"] is not JsonObject) return;
        _completionPanel.Visibility = Visibility.Visible;
        _completionStatus.Text = "Checking saved assessment and fresh remote evidence"
            + (review.Value["completion"]?["policy"]?.GetValue<string>() == "selected_local_sync" ? " and selected local checkouts" : "")
            + "…";
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
        _completionStatus.Text = NativeDisplayText.Escape(completion["message"]!.GetValue<string>());
        _completionActions.Children.Clear();
        if (stage == "refresh")
            _completionActions.Children.Add(Button("Refresh post-merge evidence", CollectAsync));
        else if (stage == "assessment")
            _completionActions.Children.Add(Button("Continue post-merge assessment", async () => {
                if (_busy || _shutdownRequested || _scheduledRunning || _investigationCancellation is not null) return;
                SetBusy(true);
                try { await ContinueAfterMergeAsync(review, allowRecovery: true); }
                finally { SetBusy(false); }
            }));
        else if (stage == "local_sync")
        {
            _completionStatus.Text += " Review the selected checkout updates below. Each update needs its own exact confirmation.";
            await ReadLocalSyncTargetsAsync();
        }
        else if (stage == "merge_outcome")
            _completionActions.Children.Add(Button("Check exact merge outcome", () => CheckReviewOutcomeAsync(review)));
        if (stage != "completed")
            _completionActions.Children.Add(Button("Check maintenance progress", () => CheckCompletionAsync(review)));
        else
        {
            // Reload durable state without inference or mutations so merge
            // controls observe the newly recorded completion gate.
            Apply(await _client.SendAsync(Request("snapshot")));
            var recorded = _reviews.SingleOrDefault(item => item.Id == id);
            if (recorded?.RecordedMaintenanceSummary is null)
                throw new InvalidDataException("Saved completion changed or its outcome still needs recovery. No completed maintenance is being claimed.");
            ShowActionResult(recorded);
        }
    }
}
