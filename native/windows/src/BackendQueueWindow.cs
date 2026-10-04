using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Data;
using System.Windows.Media;

namespace WatcherNativeProof;

internal sealed class QueueProposal
{
    internal required JsonObject Evidence { get; init; }
    internal string Id => Evidence["proposal_id"]!.GetValue<string>();
    internal int Version => Evidence["version"]!.GetValue<int>();
    internal JsonObject Step => Evidence["step"]!.AsObject();
    internal JsonObject Identity => Step["identity"]!.AsObject();
    public string Label => NativeDisplayText.Escape($"{Evidence["repository"]} · {Kind} #{Identity["number"]}");
    public string Kind => Step["kind"]!.GetValue<string>() == "alert" ? "Alert" : "PR";
    public string Handling => Evidence["handling"]!.GetValue<string>();
    public int Order => Step["order"]!.GetValue<int>();

    internal static QueueProposal Parse(JsonNode? value, string planId)
    {
        var evidence = value as JsonObject ?? throw new InvalidDataException("Malformed queue proposal.");
        var item = new QueueProposal { Evidence = evidence };
        if (!Regex.IsMatch(item.Id, "^proposal_[a-f0-9]{16}$")
            || item.Version < 0 || evidence["plan_id"]?.GetValue<string>() != planId
            || string.IsNullOrWhiteSpace(evidence["repository"]?.GetValue<string>())
            || item.Identity["repository"]?.GetValue<string>() != evidence["repository"]?.GetValue<string>()
            || item.Identity["number"]!.GetValue<int>() <= 0
            || item.Step["kind"]?.GetValue<string>() is not ("pull_request" or "alert")
            || string.IsNullOrWhiteSpace(item.Step["summary"]?.GetValue<string>())
            || item.Handling is not ("pending" or "rejected" or "deferred")
            || evidence["evidence_status"]?.GetValue<string>() is not ("complete" or "partial")
            || evidence["feedback"] is not JsonArray feedback || feedback.Count > 20)
            throw new InvalidDataException("Queue proposal identity or context is invalid.");
        foreach (var message in feedback)
            if (message is not JsonObject entry || entry["message"] is not JsonValue text
                || !text.TryGetValue<string>(out _) || entry["status"]?.GetValue<string>() != "saved_not_submitted")
                throw new InvalidDataException("Queue feedback has an invalid delivery contract.");
        return item;
    }
}

// The window has no GitHub, model or state-file implementation. All local
// choices are acknowledged by a fresh Python controller process after saving.
internal sealed class BackendQueueWindow : Window
{
    private readonly BackendClient _client;
    private readonly DataGrid _queue = new();
    private readonly TextBlock _summary = new() { TextWrapping = TextWrapping.Wrap };
    private readonly TextBlock _detail = new() { TextWrapping = TextWrapping.Wrap, FontSize = 14 };
    private readonly TextBlock _recommendation = new() { TextWrapping = TextWrapping.Wrap, FontSize = 17, FontWeight = FontWeights.SemiBold };
    private readonly TextBlock _handling = new() { TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 8, 0, 0) };
    private readonly TextBlock _messageStatus = new() { TextWrapping = TextWrapping.Wrap, FontWeight = FontWeights.SemiBold, Foreground = Brushes.DarkSlateBlue, Margin = new Thickness(0, 14, 0, 8) };
    private readonly TextBlock _savedMessages = new() { TextWrapping = TextWrapping.Wrap };
    private readonly TextBlock _agentReplies = new() { TextWrapping = TextWrapping.Wrap };
    private readonly TextBlock _launchPolicy = new() { TextWrapping = TextWrapping.Wrap };
    private readonly TextBlock _launchProgress = new() { TextWrapping = TextWrapping.Wrap, FontWeight = FontWeights.SemiBold };
    private readonly Button _investigate;
    private readonly Button _stopInvestigation;
    private readonly Button _collect;
    private NativeLaunchPolicy? _selectedLaunchPolicy;
    private string? _launchPolicyRepository;
    private string? _launchPolicyDigest;
    private string? _retryJobId;
    private CancellationTokenSource? _investigationCancellation;
    private string? _investigationRepository;
    private long _investigationGeneration;
    private bool _launchRefreshPending;
    private bool _launchRefreshInFlight;
    private readonly Dictionary<string, JsonObject> _permissionPreviews = new();
    private long _agentReadSequence;
    private long _permissionReadSequence;
    private readonly StackPanel _permissions = new();
    private Dictionary<string, string> _supervisorDigests = new();
    private readonly TextBlock _outcome = new() { TextWrapping = TextWrapping.Wrap };
    private readonly TextBox _message = new() { AcceptsReturn = true, TextWrapping = TextWrapping.Wrap, MinHeight = 70 };
    private readonly StackPanel _actions = new();
    private readonly Button _resume;
    private readonly Button _prepare;
    private readonly Button _confirm;
    private readonly Button _check;
    private readonly StackPanel _confirmation = new() { Visibility = Visibility.Collapsed };
    private readonly TextBlock _confirmationText = new() { TextWrapping = TextWrapping.Wrap };
    private readonly StackPanel _activity = new();
    private readonly TextBlock _historyStatus = new() { TextWrapping = TextWrapping.Wrap };
    private readonly Expander _history = new();
    private List<NativeActionReview> _reviews = new();
    private NativeActionReview? _prepared;
    private bool _executionEnabled;
    private bool _evidenceComplete;
    private bool _actionInFlight;
    private bool _shutdownRequested;
    private int _shutdownExitCode;
    private TaskCompletionSource<bool>? _investigationFinished;
    private readonly Dictionary<string, string> _drafts = new();
    private string? _selectedId;
    private long _sequence;
    private bool _busy;
    private TaskCompletionSource<bool>? _idle;
    private bool _rendering;
    private bool _loaded;
    private bool _refreshRequired = true;

    internal BackendQueueWindow(BackendClient client)
    {
        _client = client;
        Title = "Dependabot Watcher — review queue (development integration)";
        Width = 1080; Height = 740; MinWidth = 850; MinHeight = 560;
        WindowStartupLocation = WindowStartupLocation.CenterScreen;
        var root = new DockPanel { Margin = new Thickness(22) };
        var header = new StackPanel { Margin = new Thickness(0, 0, 0, 18) };
        header.Children.Add(new TextBlock { Text = "Recommendations", FontSize = 24, FontWeight = FontWeights.SemiBold });
        header.Children.Add(_summary);
        header.Children.Add(_launchProgress);
        DockPanel.SetDock(header, Dock.Top); root.Children.Add(header);
        var footer = new WrapPanel { Margin = new Thickness(0, 15, 0, 0) };
        footer.Children.Add(Button("Refresh saved queue", async () => await RefreshAsync()));
        _collect = Button("Collect fresh recommendations", CollectAsync);
        footer.Children.Add(_collect);
        footer.Children.Add(Button("Hide to tray", () => { Hide(); return Task.CompletedTask; }));
        footer.Children.Add(Button("Exit", () => { RequestQuit(); return Task.CompletedTask; }));
        if (_client.DiagnosticTransport)
            footer.Children.Add(Button("Inject diagnostic UI failure", () =>
                Task.FromException(new InvalidOperationException("Owned synthetic dispatcher failure."))));
        var bottom = new StackPanel();
        bottom.Children.Add(footer);
        bottom.Children.Add(_historyStatus);
        _history.Margin = new Thickness(0, 8, 0, 0);
        _history.Content = new ScrollViewer { Content = _activity, MaxHeight = 200, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        bottom.Children.Add(_history);
        DockPanel.SetDock(bottom, Dock.Bottom); root.Children.Add(bottom);
        var split = new Grid();
        split.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1.15, GridUnitType.Star) });
        split.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        _queue.AutoGenerateColumns = false; _queue.IsReadOnly = true; _queue.CanUserAddRows = false;
        _queue.SelectionMode = DataGridSelectionMode.Single;
        _queue.HeadersVisibility = DataGridHeadersVisibility.Column;
        _queue.Margin = new Thickness(0, 0, 22, 0);
        _queue.Columns.Add(new DataGridTextColumn { Header = "Recommendation", Binding = new Binding("Label"), Width = new DataGridLength(1, DataGridLengthUnitType.Star) });
        _queue.Columns.Add(new DataGridTextColumn { Header = "Repo order", Binding = new Binding("Order"), Width = DataGridLength.Auto });
        _queue.Columns.Add(new DataGridTextColumn { Header = "Local decision", Binding = new Binding("Handling"), Width = DataGridLength.Auto, MinWidth = 115 });
        _queue.SelectionChanged += (_, _) => SelectProposal();
        _message.TextChanged += (_, _) => {
            if (!_rendering && _selectedId is not null) _drafts[_selectedId] = _message.Text;
            if (!_rendering) UpdateMessageStatus();
        };
        split.Children.Add(_queue);
        _actions.Children.Add(_recommendation);
        _actions.Children.Add(_handling);
        var choices = new WrapPanel { Margin = new Thickness(0, 15, 0, 0) };
        _prepare = Button("Review and approve this PR", PrepareAsync);
        choices.Children.Add(_prepare);
        choices.Children.Add(Button("Defer one day", async () => await DecideAsync("defer")));
        choices.Children.Add(Button("Reject locally", async () => await DecideAsync("reject")));
        _resume = Button("Undo rejection or deferral", async () => await DecideAsync("resume"));
        choices.Children.Add(_resume);
        _actions.Children.Add(choices);
        _confirmation.Children.Add(new TextBlock { Text = "Confirm this one exact merge", FontSize = 17, FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 12, 0, 8) });
        _confirmation.Children.Add(_confirmationText);
        _confirm = Button("Confirm this merge", ConfirmAsync);
        _confirmation.Children.Add(_confirm);
        _confirmation.Children.Add(Button("Back to recommendations", () => {
            _prepared = null; _confirmation.Visibility = Visibility.Collapsed;
            _outcome.Text = "No approval was submitted."; return Task.CompletedTask;
        }));
        _actions.Children.Add(_confirmation);
        _check = Button("Check saved action outcome", CheckOutcomeAsync);
        _check.Visibility = Visibility.Collapsed; _actions.Children.Add(_check);
        _outcome.Margin = new Thickness(0, 12, 0, 0); _actions.Children.Add(_outcome);
        _actions.Children.Add(new Expander {
            Header = "Technical evidence", Content = _detail,
            Margin = new Thickness(0, 15, 0, 0)
        });
        _actions.Children.Add(_messageStatus);
        _actions.Children.Add(_message);
        _actions.Children.Add(Button("Save message locally", async () => await DecideAsync("feedback")));
        _actions.Children.Add(new Expander {
            Header = "Saved messages for this item", Content = _savedMessages,
            Margin = new Thickness(0, 8, 0, 0)
        });
        var agentPanel = new StackPanel();
        agentPanel.Children.Add(_launchPolicy);
        _investigate = Button("Investigate this repository / answer saved messages", InvestigateAsync);
        _investigate.IsEnabled = false;
        agentPanel.Children.Add(_investigate);
        _stopInvestigation = Button("Stop this investigation", StopInvestigationAsync);
        _stopInvestigation.Visibility = Visibility.Collapsed;
        header.Children.Add(_stopInvestigation);
        agentPanel.Children.Add(_agentReplies);
        agentPanel.Children.Add(Button("Refresh saved agent replies", ReadAgentRepliesAsync));
        _actions.Children.Add(new Expander {
            Header = "Agent answers and recommendations", Content = agentPanel,
            IsExpanded = true, Margin = new Thickness(0, 12, 0, 0)
        });
        var permissionPanel = new StackPanel();
        permissionPanel.Children.Add(_permissions);
        permissionPanel.Children.Add(Button("Refresh agent access requests", ReadPermissionsAsync));
        bottom.Children.Add(new Expander {
            Header = "Agent needs additional access", Content = new ScrollViewer {
                Content = permissionPanel, MaxHeight = 180, VerticalScrollBarVisibility = ScrollBarVisibility.Auto },
            IsExpanded = true, Margin = new Thickness(0, 12, 0, 0)
        });
        var scroll = new ScrollViewer { Content = _actions, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        System.Windows.Automation.AutomationProperties.SetName(scroll, "Recommendation details");
        Grid.SetColumn(scroll, 1); split.Children.Add(scroll);
        root.Children.Add(split); Content = root;
        Closing += (_, e) => {
            if (Application.Current is App { IsQuitting: true }) return;
            e.Cancel = true; Hide(); App.ExplainTrayOnce(); App.Record("queue_hidden");
        };
        IsVisibleChanged += async (_, _) => { if (IsVisible && _loaded) await RefreshAsync(); };
        Loaded += async (_, _) => { _loaded = true; await RefreshAsync(); };
    }

    internal void RequestQuit(int exitCode = 0)
    {
        if (exitCode != 0) _shutdownExitCode = exitCode;
        if (_shutdownRequested) return;
        _shutdownRequested = true;
        _agentReadSequence++; _permissionReadSequence++;
        _launchRefreshPending = false;
        _investigationCancellation?.Cancel();
        SetBusy(_busy);
        _launchProgress.Text = "Finishing owned backend requests before quitting. "
            + (_actionInFlight ? "The approved merge is not canceled or retried. " : "Investigation is being stopped; no action is retried. ")
            + "You can close this window to hide it meanwhile.";
        _ = QuitAfterDrainingAsync();
    }

    private async Task QuitAfterDrainingAsync()
    {
        await _client.StopAcceptingAndDrainAsync();
        await WaitForIdleAsync();
        if (_investigationFinished is not null) await _investigationFinished.Task;
        try { App.Record("native_backend_shutdown_drained"); } catch { }
        App.QuitNow(_shutdownExitCode);
    }

    private Task StopInvestigationAsync()
    {
        _investigationCancellation?.Cancel();
        _stopInvestigation.IsEnabled = false;
        _launchProgress.Text = "Stopping the owned investigation and checking saved state. No automatic retry will occur.";
        return Task.CompletedTask;
    }

    private static Button Button(string label, Func<Task> action)
    {
        var button = new Button { Content = label, Padding = new Thickness(12, 7, 12, 7), Margin = new Thickness(0, 6, 8, 0) };
        button.Click += async (_, _) => await action();
        return button;
    }

    private JsonObject Request(string operation) => new() {
        ["schema_version"] = 1,
        ["request_id"] = operation is "prepare_action" or "confirm_action" or "check_action" or "action_history" or "collect_queue"
            ? "action_" + Guid.NewGuid().ToString("N")
            : operation == "snapshot" ? "read_" + Guid.NewGuid().ToString("N")
            : $"decision_{_sequence}_" + Guid.NewGuid().ToString("N"),
        ["operation"] = operation
    };

    private void SetBusy(bool busy)
    {
        if (busy && !_busy) _idle = new(TaskCreationOptions.RunContinuationsAsynchronously);
        if (!busy) { _idle?.TrySetResult(true); _idle = null; }
        _busy = busy;
        _queue.IsEnabled = !busy && !_shutdownRequested;
        _actions.IsEnabled = !busy && !_shutdownRequested && !_refreshRequired && _queue.SelectedItem is QueueProposal;
        _activity.IsEnabled = !busy && !_shutdownRequested;
        _permissions.IsEnabled = !busy && !_shutdownRequested && !_refreshRequired;
        _collect.IsEnabled = !busy && !_shutdownRequested && _investigationCancellation is null;
        UpdateInvestigationControls();
        if (!busy && !_shutdownRequested && _launchRefreshPending && !_launchRefreshInFlight) _ = RefreshInvestigationScopeAsync();
    }

    private void UpdateInvestigationControls()
    {
        _investigate.Content = _retryJobId is null ? "Investigate this repository / answer saved messages" : "Retry investigation";
        var repository = (_queue.SelectedItem as QueueProposal)?.Evidence["repository"]?.GetValue<string>();
        _investigate.IsEnabled = !_busy && !_shutdownRequested && !_refreshRequired && _investigationCancellation is null
            && _selectedLaunchPolicy?.Configured == true && repository == _launchPolicyRepository
            && repository is not null && _supervisorDigests.GetValueOrDefault(repository) == _launchPolicyDigest;
        _prepare.IsEnabled = !_busy && !_shutdownRequested && _investigationCancellation is null && CanPrepareSelected();
        _confirm.IsEnabled = !_busy && !_shutdownRequested && _investigationCancellation is null;
    }

    private bool CanPrepareSelected() => _queue.SelectedItem is QueueProposal item && !_refreshRequired
        && _executionEnabled && _evidenceComplete && item.Evidence["evidence_status"]?.GetValue<string>() == "complete"
        && item.Handling == "pending" && item.Step["action"]?.GetValue<string>() == "validate_then_merge"
        && !_reviews.Any(review => review.MatchesRepository(item.Evidence["repository"]?.GetValue<string>())
            && (review.Status == "recovery_required" || (review.Status == "completed"
                && review.Preparation["plan_id"]?.GetValue<string>() == item.Evidence["plan_id"]?.GetValue<string>())));

    private async Task WaitForIdleAsync()
    {
        while (_busy && _idle is not null) await _idle.Task;
    }

    private void Apply(JsonObject response)
    {
        // Every applied queue frame invalidates pending reads, including a
        // policy change that keeps the same proposal ID and local version.
        _agentReadSequence++;
        _permissionReadSequence++;
        _permissions.Children.Clear();
        _agentReplies.Text = "Agent replies need verification against this queue frame.";
        _selectedLaunchPolicy = null; _launchPolicyRepository = null; _launchPolicyDigest = null;
        _retryJobId = null;
        _launchPolicy.Text = "Checking the saved investigation policy…";
        _investigate.IsEnabled = false;
        if (response["capabilities"]?["github_mutations"]?.GetValue<bool>() != false
            || response["capabilities"]?["model_calls"]?.GetValue<bool>() != false)
            throw new InvalidOperationException("Local review boundary unexpectedly reported additional capabilities.");
        var sequence = response["next_request_sequence"]!.GetValue<long>();
        var raw = response["proposals"]!.AsArray();
        var planId = response["plan_id"]?.GetValue<string>();
        var evidenceStatus = response["evidence_status"]?.GetValue<string>();
        if (sequence <= 0 || raw.Count > 1000
            || evidenceStatus is not ("complete" or "partial" or "unavailable")
            || (raw.Count > 0 && (planId is null || !Regex.IsMatch(planId, "^plan_[a-f0-9]{16}$"))))
            throw new InvalidDataException("Queue response context is invalid.");
        // Validate the complete frame before changing the displayed selection or
        // request sequence. A malformed later item must not partially apply it.
        var proposals = raw.Select(value => QueueProposal.Parse(value, planId ?? "")).ToList();
        var digests = response["supervisor_context_digests"] as JsonObject
            ?? throw new InvalidDataException("Queue frame lacks supervisor context revisions.");
        var supervisorDigests = new Dictionary<string, string>();
        foreach (var entry in digests)
        {
            var digest = entry.Value?.GetValue<string>() ?? "";
            if (!Regex.IsMatch(digest, "^[a-f0-9]{64}$")) throw new InvalidDataException("Invalid supervisor context revision.");
            supervisorDigests.Add(entry.Key, digest);
        }
        if (proposals.Any(item => !supervisorDigests.ContainsKey(item.Evidence["repository"]!.GetValue<string>())))
            throw new InvalidDataException("Queue frame lacks the selected repository context.");
        if (proposals.Select(item => item.Id).Distinct().Count() != proposals.Count)
            throw new InvalidDataException("Queue response repeats a proposal identity.");
        var rawReviews = response["reviews"] as JsonArray;
        if (rawReviews is { Count: > 100 }) throw new InvalidDataException("Action history exceeds its bound.");
        var reviews = rawReviews?.Select(NativeActionReview.Parse).ToList() ?? new();
        if (reviews.Select(r => r.Id).Distinct().Count() != reviews.Count)
            throw new InvalidDataException("Action history repeats a review identity.");
        var executionEnabled = response["execution_enabled"]?.GetValue<bool>() ?? false;
        _sequence = sequence;
        _refreshRequired = false;
        _prepared = null; _confirmation.Visibility = Visibility.Collapsed;
        _executionEnabled = executionEnabled; _reviews = reviews;
        _supervisorDigests = supervisorDigests;
        _evidenceComplete = evidenceStatus == "complete";
        RenderActivity();
        var previous = _selectedId;
        _summary.Text = (_client.DiagnosticTransport ? "SYNTHETIC TRANSPORT TEST — no real GitHub access or model jobs.\n" : "")
            + $"{proposals.Count} saved recommendations · Evidence: {response["evidence_status"]}\nRecommended order within each repository. Pending = no local decision yet.\n"
            + (executionEnabled ? "Exact single-PR approval is enabled. Nothing merges until you review and separately confirm."
                : "Merging is disabled in this profile. Starting an investigation uses the configured agent; local queue decisions do not.");
        _queue.ItemsSource = proposals;
        _queue.SelectedItem = proposals.FirstOrDefault(item => item.Id == previous) ?? proposals.FirstOrDefault();
        if (proposals.Count == 0)
        {
            _selectedId = null;
            _recommendation.Text = "No current recommendations.";
            _handling.Text = "Saved action results and recovery remain available below.";
            _outcome.Text = "";
            _detail.Text = "No current saved proposal. Collect fresh recommendations to check the repositories.";
            _prepare.Visibility = Visibility.Collapsed; _resume.Visibility = Visibility.Collapsed;
            _check.Visibility = Visibility.Collapsed; _savedMessages.Text = "";
            _message.Text = "";
            _messageStatus.Text = "Select a current recommendation to leave a question.";
            _agentReadSequence++; _agentReplies.Text = "Select an item to read its saved agent replies.";
            _launchPolicy.Text = "Select a recommendation to investigate its repository.";
            _ = ReadPermissionsAsync();
        }
        var retired = response["history"]?.AsArray().Count ?? 0;
        App.Record("backend_queue_loaded", new { proposals = proposals.Count, retired });
    }

    private void SelectProposal()
    {
        _prepared = null; _confirmation.Visibility = Visibility.Collapsed;
        _selectedId = (_queue.SelectedItem as QueueProposal)?.Id;
        _selectedLaunchPolicy = null; _launchPolicyRepository = null; _launchPolicyDigest = null;
        _launchPolicy.Text = "Checking the saved investigation policy…";
        _investigate.IsEnabled = false;
        _actions.IsEnabled = !_busy && !_shutdownRequested && !_refreshRequired && _selectedId is not null;
        if (_queue.SelectedItem is not QueueProposal item) return;
        var step = item.Step;
        var feedback = item.Evidence["feedback"]!.AsArray();
        var messages = NativeDisplayText.Escape(string.Join("\n\n", feedback.Select(value =>
            $"{value!["created_at"]}: {value["message"]}\nSaved locally. Verified answers, when available, appear below.")));
        _recommendation.Text = NativeDisplayText.Escape(item.Identity["title"]?.GetValue<string>() ?? step["summary"]!.GetValue<string>());
        _handling.Text = $"{item.Label}\nDecision: {item.Handling}"
            + (item.Handling == "deferred" ? $"\nDeferred until {item.Evidence["resume_at"]}." : "")
            + "\nThese controls change your local queue, not GitHub.";
        _resume.Visibility = item.Handling == "pending" ? Visibility.Collapsed : Visibility.Visible;
        _prepare.Visibility = item.Kind == "PR" ? Visibility.Visible : Visibility.Collapsed;
        _prepare.IsEnabled = _executionEnabled && _evidenceComplete && item.Evidence["evidence_status"]?.GetValue<string>() == "complete"
            && item.Handling == "pending" && item.Step["action"]?.GetValue<string>() == "validate_then_merge";
        _savedMessages.Text = messages.Length == 0 ? "No saved messages for this item." : messages;
        if (!_refreshRequired) _outcome.Text = "";
        var previousAction = _reviews.LastOrDefault(r => r.ProposalId == item.Id);
        if (_reviews.Any(r => r.MatchesRepository(item.Evidence["repository"]?.GetValue<string>())
            && (r.Status == "recovery_required" || (r.Status == "completed"
                && r.Preparation["plan_id"]?.GetValue<string>() == item.Evidence["plan_id"]?.GetValue<string>()))))
            _prepare.IsEnabled = false;
        _check.Visibility = previousAction?.Status == "recovery_required" ? Visibility.Visible : Visibility.Collapsed;
        if (previousAction is not null && previousAction.Status != "awaiting_confirmation")
            _outcome.Text = DescribeOutcome(previousAction);
        var blockers = step["blockers"] as JsonArray;
        var blockText = blockers is { Count: > 0 }
            ? "\n\nBlockers:\n" + string.Join("\n", blockers.Select(value => value?.ToString())) : "";
        var checks = step["status_checks"] as JsonArray;
        var checkText = checks is { Count: > 0 }
            ? string.Join("\n", checks.Select(value => $"{value?["name"]}: {value?["state"]} / {value?["conclusion"]}"))
            : "No saved check evidence; do not assume passing.";
        var advice = step["advice"] as JsonObject;
        var adviceText = advice is null ? "Deterministic recommendation; no saved AI advice for this item."
            : $"Saved {advice["assurance"]} advice: {advice["summary"]}\nRationale: {advice["rationale"]}\nThis is not permission to merge or compatibility proof.";
        _detail.Text = NativeDisplayText.Escape($"{item.Identity["title"] ?? step["summary"]}\n\n{item.Label}\nSaved order: {step["order"]}\nAction: {step["action"]}\nHead: {item.Identity["head_sha"] ?? "Not applicable"}\nExpected base: {item.Identity["expected_base"] ?? "Not applicable"}\nObserved base: {item.Identity["observed_base"] ?? "Not applicable"}\nPlan: {item.Evidence["plan_id"]}\nEvidence: {item.Evidence["evidence_status"]}\n\nWhy: {step["priority_reason"] ?? step["summary"]}{blockText}\n\nSaved checks:\n{checkText}\n\nRecommendation source:\n{adviceText}\n\nDecision: {item.Handling}\nDeferred until: {item.Evidence["resume_at"] ?? "Not deferred"}\n\nSaved messages:\n{(messages.Length == 0 ? "None" : messages)}");
        _rendering = true;
        _message.Text = _drafts.GetValueOrDefault(item.Id, "");
        UpdateMessageStatus();
        _rendering = false;
        _ = ReadAgentRepliesAsync();
        _ = ReadPermissionsAsync();
        UpdateInvestigationControls();
    }

    private async Task ReadPermissionsAsync()
    {
        if (_shutdownRequested) return;
        var generation = ++_permissionReadSequence;
        // A refresh invalidates the old handlers immediately. Do not leave
        // clickable stale choices visible while the new frame is being read.
        _permissions.Children.Clear();
        _permissions.Children.Add(new TextBlock { Text = "Checking current agent access requests…", TextWrapping = TextWrapping.Wrap });
        if (_refreshRequired) return;
        var repository = PermissionRepository();
        if (repository is null) {
            _permissions.Children.Clear();
            _permissions.Children.Add(new TextBlock { Text = "No repository selected for access requests." });
            return;
        }
        if (!_supervisorDigests.TryGetValue(repository, out var expectedDigest)) return;
        bool StillCurrent() => generation == _permissionReadSequence && !_shutdownRequested && !_refreshRequired
            && PermissionRepository() == repository
            && _supervisorDigests.TryGetValue(repository, out var digest) && digest == expectedDigest;
        try
        {
            var response = await _client.SendAsync(new JsonObject {
                ["schema_version"] = 1, ["request_id"] = "read_" + Guid.NewGuid().ToString("N"),
                ["operation"] = "supervisor_permissions", ["repository"] = repository
            });
            var requests = NativePermissionView.Parse(response, repository, expectedDigest);
            if (!StillCurrent()) return;
            _permissions.Children.Clear();
            if (requests.Count == 0)
                _permissions.Children.Add(new TextBlock { Text = "No additional access is waiting for your decision.", TextWrapping = TextWrapping.Wrap });
            foreach (var permission in requests)
            {
                var panel = new StackPanel { Margin = new Thickness(0, 8, 0, 12) };
                panel.Children.Add(new TextBlock { Text = NativeDisplayText.Escape(repository + "\n" + permission.Description), TextWrapping = TextWrapping.Wrap, FontWeight = FontWeights.SemiBold });
                var previewKey = permission.ConnectionId + "/" + permission.PermissionId;
                var hasPreview = _permissionPreviews.TryGetValue(previewKey, out var preview)
                    && preview["repository"]?.GetValue<string>() == repository
                    && NativeLaunchStream.MatchesPermission(preview, permission, expectedDigest);
                panel.Children.Add(new TextBlock { Text = hasPreview ? NativeLaunchStream.DescribePreview(preview!)
                    : "Exact access details are not available in this window. Allow is disabled; you may deny or cancel. No access is granted automatically.",
                    TextWrapping = TextWrapping.Wrap });
                panel.Children.Add(new TextBlock { Text = "This is agent access—not approval to merge. A stopped or changed job cannot reuse this decision.", TextWrapping = TextWrapping.Wrap });
                var choices = new WrapPanel();
                foreach (var choice in permission.Choices)
                {
                    if ((choice is "allow_once" or "allow_session") && !hasPreview) continue;
                    var label = choice switch { "allow_once" => "Allow once", "allow_session" => "Allow for this session",
                        "deny" => "Deny access", _ => "Cancel this request" };
                    choices.Children.Add(Button(label, async () => {
                        if (!StillCurrent() || _busy) return;
                        if (choice is "allow_once" or "allow_session")
                            if (!_permissionPreviews.TryGetValue(previewKey, out var livePreview)
                                || !NativeLaunchStream.MatchesPermission(livePreview, permission, expectedDigest)) return;
                        panel.IsEnabled = false;
                        try
                        {
                            var acknowledged = await _client.SendAsync(new JsonObject {
                                ["schema_version"] = 1, ["request_id"] = "permission_" + Guid.NewGuid().ToString("N"),
                                ["operation"] = "supervisor_permission_decide", ["repository"] = repository,
                                ["connection_id"] = permission.ConnectionId, ["permission_id"] = permission.PermissionId,
                                ["digest"] = permission.Digest, ["decision"] = choice
                            });
                            var state = NativePermissionView.ParseDecision(acknowledged, repository, expectedDigest, permission, choice);
                            if (StillCurrent())
                                _outcome.Text = state switch {
                                    "saved_waiting_delivery" => "Your access decision was saved, but has not yet been sent to the agent. This does not authorize a merge.",
                                    "sent_waiting_resolution" => "Your access decision was sent to Codex; confirmation is pending. This does not authorize a merge.",
                                    "resolved" => "Codex resolved this access request. This does not authorize a merge.",
                                    _ => "The agent stopped and this access decision was invalidated. It will not be replayed. Refresh the saved queue to recover."
                                };
                            if (!_shutdownRequested) await ReadPermissionsAsync();
                        }
                        catch (Exception ex)
                        {
                            if (StillCurrent()) _outcome.Text = $"Access decision was not confirmed: {ex.Message}\nRefresh the requests before trying again.";
                            // A lost reply may follow a saved decision. Never re-enable/replay blindly.
                        }
                    }));
                }
                panel.Children.Add(choices);
                _permissions.Children.Add(panel);
            }
        }
        catch (Exception ex)
        {
            if (!StillCurrent()) return;
            _permissions.Children.Clear();
            _permissions.Children.Add(new TextBlock { Text = $"Access requests could not be verified: {ex.Message}\nNo access decision was made by this refresh.", TextWrapping = TextWrapping.Wrap });
        }
    }

    private string? PermissionRepository() => _investigationRepository
        ?? (_queue.SelectedItem as QueueProposal)?.Evidence["repository"]?.GetValue<string>();

    private async Task ReadAgentRepliesAsync()
    {
        if (_shutdownRequested || _investigationCancellation is not null) return;
        var generation = ++_agentReadSequence;
        if (_refreshRequired || _queue.SelectedItem is not QueueProposal item) return;
        var repository = item.Evidence["repository"]!.GetValue<string>();
        if (!_supervisorDigests.TryGetValue(repository, out var expectedDigest)) return;
        var request = new JsonObject { ["schema_version"] = 1, ["request_id"] = "read_" + Guid.NewGuid().ToString("N"),
            ["operation"] = "supervisor_status", ["repository"] = repository };
        _agentReplies.Text = "Checking saved agent replies. No model call is being made…";
        try
        {
            var response = await _client.SendAsync(request);
            var view = NativeSupervisorView.Parse(response, repository, expectedDigest);
            if (generation != _agentReadSequence || _investigationCancellation is not null
                || _refreshRequired || _queue.SelectedItem is not QueueProposal selected
                || selected.Id != item.Id || selected.Version != item.Version
                || !_supervisorDigests.TryGetValue(repository, out var currentDigest) || currentDigest != expectedDigest)
            {
                App.Record("supervisor_reply_discarded", new { proposal_id = item.Id, context_digest = expectedDigest });
                return;
            }
            _agentReplies.Text = view.Describe(item);
            _selectedLaunchPolicy = view.LaunchPolicy;
            _retryJobId = view.RetryJobId;
            _launchPolicyRepository = repository; _launchPolicyDigest = expectedDigest;
            _launchPolicy.Text = view.LaunchPolicy?.Description ?? "This backend does not expose a verified investigation policy. Starting an agent is unavailable.";
            UpdateInvestigationControls();
            App.Record("supervisor_replies_loaded", new { proposal_id = item.Id, version = item.Version });
        }
        catch (Exception ex)
        {
            if (generation == _agentReadSequence)
            {
                _agentReplies.Text = $"Agent replies could not be verified: {ex.Message}\nNo agent delivery or answer is being claimed.";
                _selectedLaunchPolicy = null;
                _retryJobId = null;
                _launchPolicy.Text = "Investigation policy could not be verified. Refresh the saved queue before starting.";
                UpdateInvestigationControls();
            }
        }
    }

    private async Task InvestigateAsync()
    {
        if (!_investigate.IsEnabled || _queue.SelectedItem is not QueueProposal item) return;
        var repository = item.Evidence["repository"]!.GetValue<string>();
        var digest = _supervisorDigests[repository];
        if (_selectedLaunchPolicy?.Configured != true || _launchPolicyRepository != repository || _launchPolicyDigest != digest) return;
        var retryJobId = _retryJobId;
        if (retryJobId is not null && MessageBox.Show(this,
            "Start a fresh investigation attempt? It consumes the selected model's usage. The failed attempt stays saved; current evidence, workspace and permissions are checked again. No merge is approved.",
            "Retry investigation", MessageBoxButton.YesNo, MessageBoxImage.Question) != MessageBoxResult.Yes) return;
        // Chosen policy is displayed above this explicit start control. No UI
        // field can supply a model, workspace, account or broader permissions.
        var generation = ++_investigationGeneration;
        using var cancellation = new CancellationTokenSource();
        _investigationCancellation = cancellation;
        _agentReadSequence++;
        _retryJobId = null;
        _agentReplies.Text = "Investigation is running. Earlier results are not this attempt's answer; verified saved replies will refresh after completion.";
        _investigationFinished = new(TaskCreationOptions.RunContinuationsAsynchronously);
        _investigationRepository = repository;
        _permissionPreviews.Clear();
        _prepared = null; _confirmation.Visibility = Visibility.Collapsed;
        _stopInvestigation.Visibility = Visibility.Visible; _stopInvestigation.IsEnabled = true;
        SetBusy(false);
        _launchProgress.Text = $"Investigating {repository}. Fresh collection, workspace and account checks happen before the agent starts. Saved messages are included; no merge is authorized.";
        var request = new JsonObject {
            ["schema_version"] = 1, ["request_id"] = "launch_" + Guid.NewGuid().ToString("N"),
            ["operation"] = "supervisor_start", ["repository"] = repository, ["context_digest"] = digest
        };
        if (retryJobId is not null) request["retry_job_id"] = retryJobId;
        try
        {
            var terminal = await _client.StartSupervisorAsync(request, frame => {
                // The pipe reader only posts bounded, already-validated frames.
                // UI refresh and user decisions never block provider event reads.
                Dispatcher.BeginInvoke(new Action(() => {
                    if (_shutdownRequested || generation != _investigationGeneration || _investigationCancellation != cancellation) return;
                    if (frame.Event == "permission_preview")
                    {
                        var identity = frame.Value["request"]!;
                        _permissionPreviews.Add(identity["connection_id"] + "/" + identity["permission_id"], frame.Value);
                        _launchProgress.Text = $"The agent investigating {repository} needs access. Review the exact request below; this does not approve a merge.";
                        _launchRefreshPending = true;
                        if (!_busy && !_launchRefreshInFlight) _ = RefreshInvestigationScopeAsync();
                    }
                    else if (frame.Event == "starting")
                        _launchProgress.Text = $"Checking fresh evidence and the configured workspace for {repository}…";
                    else if (frame.Event == "error")
                        _launchProgress.Text = "Investigation could not complete. Checking saved state; no retry will occur.";
                }));
            }, cancellation.Token);
            if (_shutdownRequested) return;
            // A terminal stream frame is not a verified answer. Independently
            // read the saved queue and job receipt before presenting success.
            await WaitForIdleAsync();
            SetBusy(true);
            try
            {
                Apply(await _client.SendAsync(Request("snapshot")));
                if (!_supervisorDigests.TryGetValue(repository, out var currentDigest))
                    throw new InvalidDataException("The investigation repository is no longer in the saved queue scope.");
                var saved = await _client.SendAsync(new JsonObject {
                    ["schema_version"] = 1, ["request_id"] = "read_" + Guid.NewGuid().ToString("N"),
                    ["operation"] = "supervisor_status", ["repository"] = repository
                });
                var view = NativeSupervisorView.Parse(saved, repository, currentDigest);
                _launchProgress.Text = view.DescribeTerminal(terminal);
                App.Record("native_investigation_receipt_checked", new { repository, job_id = terminal["job_id"]!.GetValue<string>() });
            }
            finally { SetBusy(false); }
        }
        catch (Exception exception)
        {
            if (!_shutdownRequested)
                _launchProgress.Text = cancellation.IsCancellationRequested
                    ? "Stop requested; the launch connection has ended. Saved messages remain. Refresh to confirm recorded recovery; no automatic retry will occur."
                    : NativeDisplayText.Escape($"Investigation was not confirmed: {exception.Message}\nRefresh saved state before deciding what to do next; no automatic retry will occur.");
            // Even after a lost terminal reply, the durable job may exist. A
            // snapshot/status read recovers ownership, never replays inference.
            _launchRefreshPending = !_shutdownRequested;
            App.Record("native_investigation_unconfirmed", new { repository, stopped = cancellation.IsCancellationRequested });
        }
        finally
        {
            _investigationGeneration++;
            _investigationCancellation = null;
            _investigationRepository = null;
            _permissionPreviews.Clear();
            _stopInvestigation.Visibility = Visibility.Collapsed;
            _investigationFinished?.TrySetResult(true);
            SetBusy(_busy);
            if (!_shutdownRequested) _ = ReadPermissionsAsync();
            if (!_shutdownRequested) _ = ReadAgentRepliesAsync();
        }
    }

    private async Task RefreshInvestigationScopeAsync()
    {
        if (_shutdownRequested || _launchRefreshInFlight || _busy) return;
        _launchRefreshInFlight = true;
        try
        {
            while (_launchRefreshPending && !_busy && !_shutdownRequested)
            {
                _launchRefreshPending = false;
                SetBusy(true);
                try { Apply(await _client.SendAsync(Request("snapshot"))); }
                catch (Exception exception)
                {
                    _refreshRequired = true;
                    _permissions.Children.Clear();
                    _launchProgress.Text = $"Current investigation scope could not be verified: {exception.Message}\nAccess grants are unavailable until a successful refresh.";
                }
                finally { SetBusy(false); }
            }
        }
        finally { _launchRefreshInFlight = false; }
    }

    private static string DescribeOutcome(NativeActionReview review)
    {
        var result = review.Value["result"] as JsonObject;
        return review.Status switch {
            "completed" => (result?["status"]?.GetValue<string>() == "merged"
                ? "GitHub confirmed this exact PR was merged."
                : "GitHub confirms this exact head is already merged; no additional merge was performed.")
                + (review.Value["outcome_recorded"]?.GetValue<bool>() == false
                    ? "\nThe local outcome could not be saved. Use Check saved action outcome before another action."
                    : "\nOutcome saved. The queue needs fresh collection before further merges.")
                + (result?["post_action_state"]?["status"]?.GetValue<string>() == "unavailable"
                    ? "\nPost-merge collection was unavailable; merge success is still confirmed." : ""),
            "recovery_required" => "The action outcome is not yet confirmed. It may still be running. Check its saved/live outcome; do not approve it again.",
            "expired" => "This confirmation expired. Review the PR again before approving.",
            "invalidated" => "This confirmation was not used. Context changed or execution could not start; request a fresh review.",
            "already_applied" => "This exact PR head is already merged; no approval is needed.",
            _ => "A review is prepared, but no approval has been submitted."
        };
    }

    private void RenderActivity()
    {
        _activity.Children.Clear();
        // Never hide an older unresolved action behind a bounded list of newer
        // results. Historical recovery does not depend on a current proposal.
        var displayed = _reviews.Where(NeedsOutcomeCheck).Concat(_reviews.TakeLast(20).Reverse()).Distinct().ToList();
        var unresolved = _reviews.Count(NeedsOutcomeCheck);
        _history.Header = unresolved == 0 ? "Recent action results" : $"Saved action recovery — {unresolved} outcome(s) need checking";
        if (unresolved > 0) _history.IsExpanded = true;
        var latestResult = _reviews.LastOrDefault(r => r.Status == "completed");
        if (string.IsNullOrWhiteSpace(_historyStatus.Text) && latestResult is not null)
            _historyStatus.Text = $"Last saved result: {latestResult.Binding["repository"]} · PR #{latestResult.Binding["pull_request"]}\n{DescribeOutcome(latestResult)}";
        if (displayed.Count == 0) _activity.Children.Add(new TextBlock { Text = "No approved action has been recorded." });
        foreach (var review in displayed)
        {
            var row = new StackPanel { Margin = new Thickness(0, 6, 0, 8) };
            row.Children.Add(new TextBlock { TextWrapping = TextWrapping.Wrap,
                Text = $"{review.Binding["repository"]} · PR #{review.Binding["pull_request"]}\n{DescribeOutcome(review)}" });
            if (NeedsOutcomeCheck(review))
                row.Children.Add(Button("Check this saved action outcome", async () => await CheckReviewOutcomeAsync(review)));
            _activity.Children.Add(row);
        }
    }

    private static bool NeedsOutcomeCheck(NativeActionReview review) => review.Status == "recovery_required"
        || review.Value["outcome_recorded"]?.GetValue<bool>() == false;

    private void StoreReview(NativeActionReview review)
    {
        _reviews.RemoveAll(r => r.Id == review.Id);
        _reviews.Add(review); RenderActivity();
    }

    private async Task PrepareAsync()
    {
        if (_shutdownRequested || _busy || _investigationCancellation is not null || _refreshRequired || !_executionEnabled || _queue.SelectedItem is not QueueProposal item) return;
        var request = Request("prepare_action"); request["proposal_id"] = item.Id; request["version"] = item.Version;
        _prepared = null; _confirmation.Visibility = Visibility.Collapsed;
        SetBusy(true); _outcome.Text = "Checking the exact PR, current policy and required checks. No merge or AI call is being made…";
        try
        {
            var response = await _client.SendAsync(request);
            var review = NativeActionReview.Parse(response["review"]);
            review.RequireSelection(item);
            if (review.Status == "already_applied") { _outcome.Text = DescribeOutcome(review); return; }
            if (review.Status != "awaiting_confirmation") throw new InvalidDataException("No confirmable action was returned.");
            _prepared = review; StoreReview(review);
            _confirmationText.Text = NativeDisplayText.Escape(review.Describe() + $"\nConfirmation expires at {DateTimeOffset.Parse(review.Value["expires_at"]!.GetValue<string>()).ToLocalTime():g}.\nConfirm merges only this PR. GitHub will be rechecked again before the action.");
            _confirmation.Visibility = Visibility.Visible;
            _outcome.Text = "Review prepared. Nothing has been approved or merged.";
        }
        catch (Exception ex) { _outcome.Text = $"Cannot prepare this PR: {ex.Message}\nNo approval was submitted."; }
        finally { SetBusy(false); }
    }

    private async Task ConfirmAsync()
    {
        if (_shutdownRequested || _busy || _investigationCancellation is not null || _refreshRequired || _prepared?.Id is not string reviewId || _queue.SelectedItem is not QueueProposal item) return;
        _prepared.RequireSelection(item);
        var request = Request("confirm_action"); request["review_id"] = reviewId;
        _prepared = null; _confirmation.Visibility = Visibility.Collapsed;
        _actionInFlight = true; SetBusy(true);
        _outcome.Text = "Your exact approval was submitted. Watcher is rechecking and executing this one action. You can hide the window; it will not retry automatically.";
        try
        {
            var response = await _client.SendAsync(request);
            var review = NativeActionReview.Parse(response["review"]);
            review.RequireSelection(item);
            if (review.Id != reviewId) throw new InvalidDataException("Outcome returned for a different review.");
            StoreReview(review); _outcome.Text = DescribeOutcome(review);
            _check.Visibility = review.Status == "recovery_required" || review.Value["outcome_recorded"]?.GetValue<bool>() == false
                ? Visibility.Visible : Visibility.Collapsed;
            // Preserve the exact result independently of the next selection,
            // then collect a new deterministic queue without model calls.
            _prepare.IsEnabled = false;
            if (review.Status == "completed")
            {
                _historyStatus.Text = $"{review.Binding["repository"]} · PR #{review.Binding["pull_request"]}\n{DescribeOutcome(review)}";
                if (!_shutdownRequested) await CollectCoreAsync();
            }
        }
        catch (Exception ex)
        {
            _refreshRequired = true;
            _summary.Text = "Approval outcome needs checking. No automatic retry will occur.";
            _outcome.Text = $"The reply could not be verified: {ex.Message}\nRefresh saved state to check the outcome; do not approve again.";
        }
        finally
        {
            _actionInFlight = false; SetBusy(false);
        }
    }

    private async Task CheckOutcomeAsync()
    {
        if (_shutdownRequested || _busy || _queue.SelectedItem is not QueueProposal item) return;
        var review = _reviews.LastOrDefault(r => r.ProposalId == item.Id);
        if (review is not null) await CheckReviewOutcomeAsync(review);
    }

    private async Task CheckReviewOutcomeAsync(NativeActionReview review)
    {
        if (_shutdownRequested || _busy || review.Id is not string reviewId) return;
        var request = Request("check_action"); request["review_id"] = reviewId;
        var target = $"{review.Binding["repository"]} · PR #{review.Binding["pull_request"]}";
        SetBusy(true); _historyStatus.Text = $"Checking {target}'s execution audit and exact live PR. No action is being retried…";
        try
        {
            var response = await _client.SendAsync(request);
            var checkedReview = NativeActionReview.Parse(response["review"]);
            if (checkedReview.Id != reviewId || !JsonNode.DeepEquals(checkedReview.Preparation, review.Preparation))
                throw new InvalidDataException("Outcome belongs to a different exact historical action.");
            StoreReview(checkedReview); _historyStatus.Text = $"{target}\n{DescribeOutcome(checkedReview)}";
            if ((_queue.SelectedItem as QueueProposal)?.Id == checkedReview.ProposalId)
                _outcome.Text = DescribeOutcome(checkedReview);
            _check.Visibility = checkedReview.Status == "recovery_required" ? Visibility.Visible : Visibility.Collapsed;
        }
        catch (Exception ex) { _historyStatus.Text = $"Cannot confirm {target}'s outcome yet: {ex.Message}\nNo action was retried."; }
        finally { SetBusy(false); }
    }

    private void UpdateMessageStatus()
    {
        var saved = (_queue.SelectedItem as QueueProposal)?.Evidence["feedback"]?.AsArray().Count ?? 0;
        _messageStatus.Text = _message.Text.Length > 0
            ? "Draft for this selected item — not saved or sent until you choose Save message locally."
            : saved > 0 ? $"{saved} message(s) saved locally. Check the agent answers below for verified processing. Write another here."
            : "Ask a question or leave a direction — saving stores it locally; it does not itself launch an agent.";
    }

    private async Task CollectCoreAsync()
    {
        var savedResult = _historyStatus.Text;
        _historyStatus.Text = savedResult + "\nCollecting fresh recommendations. No model or merge is being invoked…";
        try
        {
            var response = await _client.SendAsync(Request("collect_queue"));
            var status = response["collection_status"]?.GetValue<string>();
            if (status is not ("complete" or "partial")) throw new InvalidDataException("Fresh collection returned an invalid completion status.");
            Apply(response);
            _historyStatus.Text = savedResult + (status == "complete"
                ? "\nFresh recommendations collected. Further actions still need their own exact confirmation."
                : "\nCollection is incomplete. Approvals remain disabled until complete evidence is available.");
        }
        catch (Exception ex)
        {
            _refreshRequired = true;
            _summary.Text = "Fresh collection was not confirmed. Previously displayed recommendations may be stale.";
            _historyStatus.Text = savedResult + $"\nCould not refresh recommendations: {ex.Message}\nAny confirmed merge remains confirmed; no action was retried.";
        }
    }

    private async Task CollectAsync()
    {
        if (_shutdownRequested || _busy || _investigationCancellation is not null) return;
        SetBusy(true);
        try { await CollectCoreAsync(); }
        finally { SetBusy(false); }
    }

    private async Task RefreshAsync()
    {
        if (_shutdownRequested || _busy) return;
        SetBusy(true); _outcome.Text = "Loading the saved queue…";
        try {
            Apply(await _client.SendAsync(Request("snapshot")));
            _outcome.Text = "Saved queue refreshed." + (string.IsNullOrWhiteSpace(_outcome.Text) ? "" : "\n" + _outcome.Text);
        }
        catch (Exception ex) {
            _refreshRequired = true;
            _summary.Text = "Refresh failed. Previously displayed items may be outdated; refresh successfully before deciding.";
            _outcome.Text = $"Cannot refresh: {ex.Message}\nAny previously displayed evidence is stale. Local decisions are disabled until refresh succeeds.";
            App.Record("backend_queue_error", new { operation = "snapshot" });
        }
        finally { SetBusy(false); }
    }

    private async Task DecideAsync(string operation)
    {
        if (_shutdownRequested || _busy || _refreshRequired || _queue.SelectedItem is not QueueProposal item) return;
        var message = _message.Text;
        if (operation == "feedback" && (string.IsNullOrWhiteSpace(message) || Encoding.UTF8.GetByteCount(message) > 8192))
        { _outcome.Text = "Enter a message of at most 8192 UTF-8 bytes."; return; }
        var request = Request(operation);
        request["proposal_id"] = item.Id; request["version"] = item.Version;
        if (operation == "feedback") request["message"] = message;
        if (operation == "defer") request["seconds"] = 86400;
        SetBusy(true); _outcome.Text = "Saving the local decision…";
        try
        {
            var response = await _client.SendAsync(request);
            Apply(response);
            if (operation == "feedback") { _drafts.Remove(item.Id); SelectProposal(); }
            _outcome.Text = operation == "feedback" ? "Message saved locally. Saving did not launch an agent; verified replies appear below when available."
                : "Local decision saved. No PR or security alert was changed.";
            App.Record("backend_queue_decision", new { operation, replayed = response["replayed"]?.GetValue<bool>() });
        }
        catch (Exception ex)
        {
            // A lost reply can follow a successful save. Never reapply blindly.
            _refreshRequired = true;
            _summary.Text = "The last save was not confirmed. Refresh to check what was actually saved before deciding again.";
            _outcome.Text = $"Save was not confirmed: {ex.Message}\nRefresh to inspect persisted state before trying another decision.";
            App.Record("backend_queue_error", new { operation });
        }
        finally { SetBusy(false); }
    }
}
