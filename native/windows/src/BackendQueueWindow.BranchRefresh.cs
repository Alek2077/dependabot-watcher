using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;

namespace WatcherNativeProof;

internal sealed partial class BackendQueueWindow
{
    private readonly Expander _branchRefreshPanel = new() { Header = "Update a candidate PR branch", Margin = new Thickness(0, 12, 0, 4), Visibility = Visibility.Collapsed };
    private readonly ComboBox _branchRefreshCandidate = new() { DisplayMemberPath = "QueueLabel", MinHeight = 30, Margin = new Thickness(0, 8, 0, 6) };
    private readonly TextBlock _branchRefreshStatus = new() { TextWrapping = TextWrapping.Wrap };
    private Button? _reviewBranchRefresh;
    private Button? _checkBranchRefresh;
    private readonly List<JsonObject> _branchRefreshRecords = new();
    private readonly StackPanel _branchRefreshHistory = new();
    private readonly TextBlock _branchRefreshHistoryStatus = new() { TextWrapping = TextWrapping.Wrap };
    private readonly StackPanel _branchRefreshHistoryStatusSurface = new();
    private JsonObject? _branchRefreshSelectionOwner;
    private bool BranchMaintenanceBlocked => _busy || _shutdownRequested || KnownInvestigationOwnsMaintenance;

    private void InitializeBranchRefresh()
    {
        var panel = new StackPanel();
        panel.Children.Add(new TextBlock { Text = "Bring the target branch into a Dependabot PR before validation. This updates the PR branch—not the target branch or your checkout—and never merges the PR.", TextWrapping = TextWrapping.Wrap });
        panel.Children.Add(_branchRefreshCandidate);
        _reviewBranchRefresh = Button("Review PR branch update", PrepareBranchRefreshAsync);
        _reviewBranchRefresh.HorizontalAlignment = HorizontalAlignment.Left;
        panel.Children.Add(_reviewBranchRefresh);
        panel.Children.Add(_branchRefreshStatus);
        AddCopyIcon(_branchRefreshStatus);
        _checkBranchRefresh = Button("Check update and continue validation", CheckBranchRefreshAsync);
        _checkBranchRefresh.HorizontalAlignment = HorizontalAlignment.Left;
        panel.Children.Add(_checkBranchRefresh);
        _branchRefreshPanel.Content = panel;
        _actions.Children.Add(_branchRefreshPanel);
        _branchRefreshHistoryStatusSurface.Children.Add(_branchRefreshHistoryStatus);
        AddCopyIcon(_branchRefreshHistoryStatus);
        _branchRefreshCandidate.SelectionChanged += (_, _) => UpdateBranchRefreshControls();
    }

    private static JsonObject BranchRefreshRequest(string operation) => new() {
        ["schema_version"] = 1, ["request_id"] = "action_" + Guid.NewGuid().ToString("N"), ["operation"] = operation
    };

    private static JsonObject ParseBranchRefresh(JsonNode? node)
    {
        if (node is not JsonObject record || record["binding"] is not JsonObject binding
            || !Regex.IsMatch(record["review_id"]?.GetValue<string>() ?? "", "^refresh_[a-f0-9]{32}$")
            || !Regex.IsMatch(binding["repository"]?.GetValue<string>() ?? "", "^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
            || binding["number"]?.GetValue<int>() is not > 0
            || record["sequence"] is not JsonValue sequence || !sequence.TryGetValue<long>(out var sequenceNumber) || sequenceNumber <= 0
            || record["status"]?.GetValue<string>() is not ("awaiting_confirmation" or "executing" or "awaiting_update" or "recovery_required" or "updated" or "expired" or "invalidated" or "closed" or "rejected" or "released"))
            throw new InvalidDataException("Invalid PR branch-update identity or state.");
        foreach (var name in new[] { "head_sha", "base_sha" })
            if (!Regex.IsMatch(binding[name]?.GetValue<string>() ?? "", "^([a-fA-F0-9]{40}|[a-fA-F0-9]{64})$"))
                throw new InvalidDataException("Invalid branch-update revision.");
        foreach (var name in new[] { "head_branch", "base_branch" })
            if (binding[name] is not JsonValue value || !value.TryGetValue<string>(out var branch) || string.IsNullOrWhiteSpace(branch) || branch.Length > 255)
                throw new InvalidDataException("Invalid branch-update branch.");
        if (record["status"]!.GetValue<string>() == "updated"
            && (!Regex.IsMatch(record["observed_head"]?.GetValue<string>() ?? "", "^([a-fA-F0-9]{40}|[a-fA-F0-9]{64})$")
                || record["base_current"] is not JsonValue flag || !flag.TryGetValue<bool>(out _)))
            throw new InvalidDataException("Updated branch lacks observation evidence.");
        return (JsonObject)record.DeepClone();
    }

    private void ReadBranchRefreshReviews(JsonObject response)
    {
        _branchRefreshRecords.Clear();
        if (response["branch_refresh_reviews"] is JsonArray records) {
            if (records.Count > 100) throw new InvalidDataException("Branch-update history exceeds its bound.");
            foreach (var record in records) _branchRefreshRecords.Add(ParseBranchRefresh(record));
        }
        RefreshBranchRefreshCandidates();
        RenderBranchRefreshHistory();
    }

    private void RefreshBranchRefreshCandidates()
    {
        var selected = _queue.SelectedItem as QueueProposal;
        var repository = selected?.Evidence["repository"]?.GetValue<string>();
        var candidates = _queue.Items.OfType<QueueProposal>().Where(item => item.Kind == "PR" && item.Handling == "pending"
            && item.Evidence["repository"]?.GetValue<string>() == repository).ToList();
        var previousCandidate = _branchRefreshCandidate.SelectedItem as QueueProposal;
        var oldNumber = selected is not null && _branchRefreshSelectionOwner is not null && selected.SameItem(_branchRefreshSelectionOwner)
            && previousCandidate is not null && previousCandidate.Evidence["repository"]?.GetValue<string>() == repository
            ? previousCandidate.Identity["number"]?.GetValue<int>() : null;
        _branchRefreshSelectionOwner = selected?.ItemIdentity;
        _branchRefreshCandidate.ItemsSource = candidates;
        _branchRefreshCandidate.SelectedItem = selected?.Kind == "PR" ? selected
            : candidates.SingleOrDefault(item => item.Identity["number"]?.GetValue<int>() == oldNumber);
        UpdateBranchRefreshControls();
    }

    private JsonObject? SelectedBranchRefresh()
    {
        var selected = _queue.SelectedItem as QueueProposal;
        var repository = selected?.Evidence["repository"]?.GetValue<string>();
        var number = (_branchRefreshCandidate.SelectedItem as QueueProposal)?.Identity["number"]?.GetValue<int>();
        if (number is null) return null;
        return _branchRefreshRecords.OrderBy(record => record["sequence"]!.GetValue<long>()).LastOrDefault(record => record["binding"]?["repository"]?.GetValue<string>() == repository
            && record["binding"]?["number"]?.GetValue<int>() == number
            && record["status"]?.GetValue<string>() is "executing" or "awaiting_update" or "recovery_required" or "updated" or "rejected" or "closed" or "released");
    }

    private void UpdateBranchRefreshControls()
    {
        if (_reviewBranchRefresh is null || _checkBranchRefresh is null) return;
        var candidate = _branchRefreshCandidate.SelectedItem as QueueProposal;
        var saved = SelectedBranchRefresh();
        var repository = (_queue.SelectedItem as QueueProposal)?.Evidence["repository"]?.GetValue<string>();
        var pendingInRepository = _branchRefreshRecords.Any(record => record["binding"]?["repository"]?.GetValue<string>() == repository
            && record["status"]?.GetValue<string>() is "executing" or "awaiting_update" or "recovery_required");
        _branchRefreshPanel.Visibility = _queue.SelectedItem is QueueProposal && (_branchRefreshCandidate.Items.Count > 0 || saved is not null)
            ? Visibility.Visible : Visibility.Collapsed;
        _reviewBranchRefresh.IsEnabled = !MaintenanceOperationBlocked && !_refreshRequired && _executionEnabled && candidate is not null && !pendingInRepository;
        var checkable = saved is not null && saved["status"]?.GetValue<string>() is "executing" or "awaiting_update" or "recovery_required" or "updated";
        _checkBranchRefresh.Visibility = checkable ? Visibility.Visible : Visibility.Collapsed;
        _checkBranchRefresh.IsEnabled = checkable && !MaintenanceOperationBlocked && !_refreshRequired;
        if (saved is not null) {
            var status = saved["status"]!.GetValue<string>();
            var description = status switch {
                "updated" when saved["base_current"]?.GetValue<bool>() != true => "Branch changed, but the current target is not included. Review a new update; no further update was authorized.",
                "updated" when saved["continuation_claimed"]?.GetValue<bool>() == true => "✓ Branch update observed. Validation continuation saved; see the agent status and conversation below.",
                "updated" => "✓ Branch update observed. Continue validation; no merge occurred.",
                "closed" => "PR closed. No update will be replayed and no validation was started by this check.",
                "rejected" => $"GitHub rejected the update (HTTP {saved["rejection_status"]}). Check permissions or conflicts, then review again. This approval will not be replayed.",
                "released" => "Outcome remains uncertain; you released Watcher's hold. GitHub's request was not cancelled and may still finish. Collect fresh evidence before further actions.",
                _ => "Branch update outcome pending or uncertain. Checking never repeats the update."
            };
            _branchRefreshStatus.Text = NativeDisplayText.Escape($"PR #{saved["binding"]!["number"]} · " + description);
            _checkBranchRefresh.Content = saved["continuation_claimed"]?.GetValue<bool>() == true ? "Check saved update" : "Check update and continue validation";
            if (status != "updated") _branchRefreshPanel.IsExpanded = true;
        } else _branchRefreshStatus.Text = pendingInRepository ? "A saved branch-update outcome needs checking in Maintenance before another update. No unrelated PR will be selected automatically."
            : candidate is null ? "Choose the candidate PR to review its update." : "Review checks the current PR head and target branch before asking for approval.";
    }

    private void StoreBranchRefresh(JsonObject record)
    {
        _branchRefreshRecords.RemoveAll(value => value["review_id"]!.GetValue<string>() == record["review_id"]!.GetValue<string>());
        _branchRefreshRecords.Add(record);
        UpdateBranchRefreshControls();
        RenderBranchRefreshHistory();
    }

    private void RenderBranchRefreshHistory()
    {
        _branchRefreshHistory.Children.Clear();
        UpdateMaintenanceHeader();
        var records = _branchRefreshRecords.Where(record => record["status"]?.GetValue<string>() is
            "executing" or "awaiting_update" or "recovery_required" or "updated" or "closed" or "rejected" or "released")
            .OrderByDescending(record => record["sequence"]!.GetValue<long>()).ToList();
        var unused = _branchRefreshRecords.Where(record => record["status"]?.GetValue<string>() is "awaiting_confirmation" or "expired" or "invalidated")
            .OrderByDescending(record => record["sequence"]!.GetValue<long>()).ToList();
        if (records.Count == 0 && unused.Count == 0) return;
        _branchRefreshHistory.Children.Add(new TextBlock { Text = "PR branch updates", FontSize = 16, FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 14, 0, 6) });
        _branchRefreshHistory.Children.Add(_branchRefreshHistoryStatusSurface);
        foreach (var record in records) {
            var status = record["status"]!.GetValue<string>();
            var pending = status is "executing" or "awaiting_update" or "recovery_required";
            var description = status switch {
                "updated" when record["base_current"]?.GetValue<bool>() != true => "Branch changed, but the target was not included at the last check. Collect fresh evidence and review another update if needed; no second write was authorized.",
                "updated" => "Branch update observed (saved). Validation and merge are separate outcomes.",
                "closed" => "PR closed. No update was replayed.",
                "rejected" => $"GitHub rejected this update (HTTP {record["rejection_status"]}). This consent will not be replayed.",
                "released" => "Uncertain outcome retained; workflow hold released by you. GitHub may still finish the request. Fresh evidence is required for further actions.",
                _ => "Update outcome needs checking. No update will be replayed."
            };
            var text = new TextBlock { TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 6, 0, 0),
                Text = NativeDisplayText.Escape($"{record["binding"]!["repository"]} · PR #{record["binding"]!["number"]}\n{description}") };
            _branchRefreshHistory.Children.Add(text); AddCopyIcon(text);
            if (pending) {
                var saved = (JsonObject)record.DeepClone();
                var check = Button("Check branch-update outcome", async () => {
                    _branchRefreshHistoryStatus.Text = "Checking the saved PR branch update. No update or investigation is being repeated…";
                    await CheckSavedBranchRefreshAsync(saved, continueValidation: false);
                    _branchRefreshHistoryStatus.Text = _outcome.Text;
                });
                check.HorizontalAlignment = HorizontalAlignment.Left;
                _branchRefreshHistory.Children.Add(check);
                var release = Button("Release unresolved workflow hold…", async () => await ReleaseBranchRefreshHoldAsync(saved));
                release.HorizontalAlignment = HorizontalAlignment.Left;
                _branchRefreshHistory.Children.Add(release);
            }
        }
        if (unused.Count > 0) {
            var details = new TextBlock { TextWrapping = TextWrapping.Wrap, Text = NativeDisplayText.Escape(string.Join("\n\n", unused.Select(record =>
                $"{record["binding"]!["repository"]} · PR #{record["binding"]!["number"]}\n" + (record["status"]!.GetValue<string>() switch {
                    "expired" => "Review expired.", "invalidated" => "Review invalidated by changed context.", _ => "Review prepared but not used."
                }) + " No branch update was approved by this review; review again before approving."))) };
            _branchRefreshHistory.Children.Add(new Expander { Header = $"Unused branch reviews · {unused.Count}", Content = details, Margin = new Thickness(0, 10, 0, 0) });
            AddCopyIcon(details);
        }
    }

    private void UpdateMaintenanceHeader()
    {
        var pending = _reviews.Count(review => review.HasPendingCompletion)
            + _branchRefreshRecords.Count(record => record["status"]?.GetValue<string>() is "executing" or "awaiting_update" or "recovery_required");
        _maintenanceWorkspace.Header = pending == 0 ? "Maintenance" : $"Maintenance · {pending} pending";
    }

    private async Task ReleaseBranchRefreshHoldAsync(JsonObject saved)
    {
        if (BranchMaintenanceBlocked || _refreshRequired) return;
        SetBusy(true);
        try {
            if (!NativeConfirmationDialog.Confirm(this, "Release this unresolved workflow hold",
                NativeDisplayText.Escape($"{saved["binding"]!["repository"]} · PR #{saved["binding"]!["number"]}\nExpected original head: {saved["binding"]!["head_sha"]}\n\nThe branch update outcome is still uncertain. Releasing Watcher's hold does NOT cancel GitHub's request; it may still finish later. It does not prove failure or success, retry the update, merge anything or start an investigation.\n\nThe uncertain record stays in history. Collect fresh evidence before any further action, and review any new update or merge separately."),
                "Release hold; keep uncertain history")) {
                _branchRefreshHistoryStatus.Text = "Hold retained. No local or remote change was requested.";
                return;
            }
            var request = BranchRefreshRequest("release_branch_refresh");
            request["review_id"] = saved["review_id"]!.GetValue<string>();
            request["binding_digest"] = saved["binding_digest"]!.GetValue<string>();
            var result = ParseBranchRefresh((await _client.SendAsync(request))["branch_refresh"]);
            if (result["review_id"]?.GetValue<string>() != saved["review_id"]?.GetValue<string>()
                || !JsonNode.DeepEquals(result["binding"], saved["binding"]) || result["status"]?.GetValue<string>() != "released")
                throw new InvalidDataException("Hold-release receipt differs from the confirmed operation.");
            StoreBranchRefresh(result);
            _refreshRequired = true;
            _branchRefreshHistoryStatus.Text = "Workflow hold released; outcome remains uncertain. Refresh saved queue, then collect fresh evidence before reviewing another action. GitHub's request was not cancelled or replayed.";
        } catch (Exception ex) {
            _branchRefreshHistoryStatus.Text = NativeDisplayText.Escape($"Hold release was not confirmed: {ex.Message}\nRefresh saved state; no remote write was requested.");
        } finally { SetBusy(false); }
    }

    private async Task PrepareBranchRefreshAsync()
    {
        if (_reviewBranchRefresh?.IsEnabled != true || _branchRefreshCandidate.SelectedItem is not QueueProposal candidate) return;
        // This explicit PR action changes the displayed recipient before review;
        // continuation must never attach a PR operation to an unrelated alert.
        _queue.SelectedItem = candidate;
        SetBusy(true); _outcome.Text = $"Checking PR #{candidate.Identity["number"]} for a branch update…";
        try {
            var request = BranchRefreshRequest("prepare_branch_refresh"); request["proposal_id"] = candidate.Id; request["version"] = candidate.Version;
            var prepared = ParseBranchRefresh((await _client.SendAsync(request))["branch_refresh"]);
            var binding = prepared["binding"]!.AsObject();
            if (binding["repository"]?.GetValue<string>() != candidate.Evidence["repository"]?.GetValue<string>()
                || binding["number"]?.GetValue<int>() != candidate.Identity["number"]?.GetValue<int>()
                || prepared["status"]?.GetValue<string>() != "awaiting_confirmation")
                throw new InvalidDataException("Branch-update review differs from the selected PR.");
            if (!NativeConfirmationDialog.Confirm(this, "Approve this PR branch update",
                NativeDisplayText.Escape($"{binding["repository"]} · PR #{binding["number"]}\n\nMerge the latest {binding["base_branch"]} into {binding["head_branch"]}.\nExpected PR head: {binding["head_sha"]}\nObserved target tip: {binding["base_sha"]}\n\nGitHub may include newer target commits while processing; its API pins the PR head, not the target tip. Existing PR commits are preserved. Conflicts or changed heads stop the operation.\n\nThis changes the remote PR branch and may trigger CI. It does NOT merge the PR, update your checkout or deploy. After the update is observed, continue validation with your configured agent; that consumes its normal usage and reuses prior context where compatible."),
                "Approve update and continue validation")) { _outcome.Text = "Branch update cancelled. No remote write was requested."; return; }
            var confirm = BranchRefreshRequest("confirm_branch_refresh"); confirm["review_id"] = prepared["review_id"]!.GetValue<string>();
            _outcome.Text = "Submitting the approved branch update. No PR merge is authorized…";
            var recorded = ParseBranchRefresh((await _client.SendAsync(confirm))["branch_refresh"]);
            if (!JsonNode.DeepEquals(recorded["binding"], binding) || recorded["review_id"]?.GetValue<string>() != prepared["review_id"]?.GetValue<string>())
                throw new InvalidDataException("Branch-update receipt differs from the approved review.");
            StoreBranchRefresh(recorded);
            if (recorded["status"]?.GetValue<string>() == "rejected") {
                _outcome.Text = $"GitHub rejected the branch update (HTTP {recorded["rejection_status"]}). Check repository permissions or conflicts, then request a new review. Nothing was merged and this approval will not be replayed.";
                return;
            }
        } catch (Exception ex) {
            _outcome.Text = NativeDisplayText.Escape($"Branch update was not confirmed: {ex.Message}\nRefresh saved state and check the recorded outcome before another approval. No automatic write retry.");
            return;
        } finally { SetBusy(false); }
        await CheckBranchRefreshAsync();
    }

    private Task CheckBranchRefreshAsync() => SelectedBranchRefresh() is JsonObject saved
        ? CheckSavedBranchRefreshAsync(saved, continueValidation: true) : Task.CompletedTask;

    private async Task CheckSavedBranchRefreshAsync(JsonObject saved, bool continueValidation)
    {
        if (BranchMaintenanceBlocked || _refreshRequired) return;
        JsonObject current = saved;
        SetBusy(true);
        try {
            for (var attempt = 0; attempt < 10; attempt++) {
                _outcome.Text = "Checking the PR branch update outcome. No update or merge is being repeated…";
                var request = BranchRefreshRequest("check_branch_refresh"); request["review_id"] = saved["review_id"]!.GetValue<string>();
                current = ParseBranchRefresh((await _client.SendAsync(request))["branch_refresh"]);
                if (!JsonNode.DeepEquals(current["binding"], saved["binding"]) || current["review_id"]?.GetValue<string>() != saved["review_id"]?.GetValue<string>())
                    throw new InvalidDataException("Branch-update observation changed review scope.");
                StoreBranchRefresh(current);
                if (current["status"]?.GetValue<string>() is "updated" or "closed" || _shutdownRequested) break;
                _outcome.Text = "GitHub has not yet confirmed the updated head. Waiting; no update is being replayed…";
                if (attempt < 9) await Task.Delay(2000);
            }
        } catch (Exception ex) {
            _outcome.Text = NativeDisplayText.Escape($"Could not verify branch update: {ex.Message}\nThe saved operation remains available for a read-only check; do not approve it again.");
            return;
        } finally { SetBusy(false); }
        if (current["status"]?.GetValue<string>() == "closed") {
            _outcome.Text = "The PR has closed. No update was replayed and no further validation was started.";
            return;
        }
        if (_shutdownRequested || current["status"]?.GetValue<string>() != "updated") return;
        if (current["base_current"]?.GetValue<bool>() != true) {
            _outcome.Text = "The PR branch changed, but the target advanced again or is not included. Review a fresh branch update; no second update was authorized.";
            return;
        }
        if (!continueValidation) {
            _outcome.Text = "✓ Branch update observed. No investigation was started by this check. Select this PR to continue validation if it is still open.";
            return;
        }
        var binding = current["binding"]!;
        var target = _queue.Items.OfType<QueueProposal>().SingleOrDefault(item => item.Kind == "PR"
            && item.Evidence["repository"]?.GetValue<string>() == binding["repository"]?.GetValue<string>()
            && item.Identity["number"]?.GetValue<int>() == binding["number"]?.GetValue<int>());
        if (target is null) { _outcome.Text = "Branch update observed. Refresh the saved queue to locate this PR before continuing validation."; return; }
        _queue.SelectedItem = target;
        await ReadAgentRepliesAsync();
        if (MaintenanceOperationBlocked || _refreshRequired) { _outcome.Text = "Branch update observed. Agent status needs verification before continuing validation."; return; }
        var startAllowed = false;
        SetBusy(true);
        try {
            var request = BranchRefreshRequest("continue_branch_refresh"); request["review_id"] = current["review_id"]!.GetValue<string>();
            var response = await _client.SendAsync(request);
            if (response["continuation_start_allowed"] is not JsonValue flag || !flag.TryGetValue<bool>(out startAllowed))
                throw new InvalidDataException("Continuation lacks its single-use launch outcome.");
            Apply(response);
            await ReadAgentRepliesAsync();
        } catch (Exception ex) {
            _outcome.Text = NativeDisplayText.Escape($"Branch update is observed, but continuation was not confirmed: {ex.Message}\nRefresh saved state before using the normal investigation control. No update or model launch was replayed.");
            return;
        } finally { SetBusy(false); }
        if (startAllowed && _investigate.IsEnabled) await InvestigateAsync();
        else _outcome.Text = "Branch update observed. Continuation was already saved or could not start under the current agent policy. Review saved replies or use the normal investigation control; no branch update was repeated.";
    }
}
