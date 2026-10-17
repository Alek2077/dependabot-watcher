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
using System.Windows.Markup;

namespace WatcherNativeProof;

internal sealed class QueueProposal
{
    internal required JsonObject Evidence { get; init; }
    internal string Id => Evidence["proposal_id"]!.GetValue<string>();
    internal int Version => Evidence["version"]!.GetValue<int>();
    internal JsonObject Step => Evidence["step"]!.AsObject();
    internal JsonObject Identity => Step["identity"]!.AsObject();
    public string Label => NativeDisplayText.Escape($"{Evidence["repository"]} · {Kind} #{Identity["number"]}");
    public string QueueLabel => NativeDisplayText.Escape($"{Kind} #{Identity["number"]} · {Evidence["repository"]}");
    public string Kind => Step["kind"]!.GetValue<string>() == "alert" ? "Alert" : "PR";
    public string Handling => Evidence["handling"]!.GetValue<string>();
    public int Order => Step["order"]!.GetValue<int>();
    internal string InvestigationRequest => $"Investigate the selected {Kind} #{Identity["number"]} in {Evidence["repository"]}. "
        + "Assess current evidence, identify and perform needed investigation or validation within the configured permissions, "
        + "and report the next action and any remaining blockers. Reuse prior investigation where relevant. "
        + "This request does not authorize merging, pushing, deploying or changing developer checkouts.";

    internal bool SameItem(JsonObject identity) =>
        string.Equals(Evidence["repository"]!.GetValue<string>(), identity["repository"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase)
        && Step["kind"]!.GetValue<string>() == identity["kind"]?.GetValue<string>()
        && Identity["number"]!.GetValue<int>() == identity["number"]?.GetValue<int>();

    internal JsonObject ItemIdentity => new() {
        ["repository"] = Evidence["repository"]!.GetValue<string>(),
        ["kind"] = Step["kind"]!.GetValue<string>(), ["number"] = Identity["number"]!.GetValue<int>()
    };

    internal (string Repository, string Kind, int Number) ItemKey =>
        (Evidence["repository"]!.GetValue<string>().ToUpperInvariant(), Step["kind"]!.GetValue<string>(), Identity["number"]!.GetValue<int>());

    internal static QueueProposal? RestoreSelection(IReadOnlyList<QueueProposal> proposals, string? exactId, JsonObject? identity) =>
        proposals.FirstOrDefault(item => item.Id == exactId)
        ?? (identity is null ? null : proposals.SingleOrDefault(item => item.SameItem(identity)))
        ?? (exactId is null && identity is null ? proposals.FirstOrDefault() : null);

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
internal sealed partial class BackendQueueWindow : Window
{
    private readonly BackendClient _client;
    private readonly DataGrid _queue = new();
    private readonly TextBlock _summary = new() { TextWrapping = TextWrapping.Wrap };
    private readonly TextBlock _detail = new() { TextWrapping = TextWrapping.Wrap, FontSize = 14 };
    private readonly TextBlock _recommendation = new() { TextWrapping = TextWrapping.Wrap, FontSize = 17, FontWeight = FontWeights.SemiBold };
    private readonly TextBlock _handling = new() { TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 8, 0, 0) };
    private readonly TextBlock _agentRecommendation = new() { TextWrapping = TextWrapping.Wrap, FontSize = 18, FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 18, 0, 6) };
    private readonly TextBlock _agentReason = new() { TextWrapping = TextWrapping.Wrap, FontSize = 14 };
    private readonly TextBlock _nextStep = new() { TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 10, 0, 0) };
    private readonly TextBlock _messageStatus = new() { TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 10, 0, 6) };
    private readonly TextBlock _savedMessages = new() { TextWrapping = TextWrapping.Wrap };
    private readonly TextBlock _agentReplies = new() { TextWrapping = TextWrapping.Wrap };
    private readonly TextBlock _agentIdentity = new() { Text = "Agent: checking configuration…", TextWrapping = TextWrapping.Wrap,
        FontSize = 14, Foreground = SystemColors.GrayTextBrush, Margin = new Thickness(0, 0, 0, 6) };
    private readonly TextBlock _agentUsage = new() { TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 0, 0, 8) };
    private readonly TextBlock _latestReply = new() { TextWrapping = TextWrapping.Wrap, FontSize = 14,
        Foreground = SystemColors.WindowTextBrush, Margin = new Thickness(0, 6, 0, 8) };
    private readonly TextBlock _latestQuestion = new() { TextWrapping = TextWrapping.Wrap, FontSize = 14, Margin = new Thickness(0, 6, 0, 12) };
    private readonly TextBlock _replySource = new() { TextWrapping = TextWrapping.Wrap, Foreground = SystemColors.GrayTextBrush };
    private readonly TextBlock _replyFreshness = new() { TextWrapping = TextWrapping.Wrap, Foreground = SystemColors.GrayTextBrush, Margin = new Thickness(0, 6, 0, 0) };
    private readonly StackPanel _latestAnswer = new() { Visibility = Visibility.Collapsed };
    private readonly TextBlock _launchPolicy = new() { TextWrapping = TextWrapping.Wrap };
    private readonly TextBlock _launchProgress = new() { TextWrapping = TextWrapping.Wrap, FontWeight = FontWeights.SemiBold };
    private readonly TextBlock _elapsedActivity = new() { TextWrapping = TextWrapping.Wrap,
        Foreground = SystemColors.GrayTextBrush, Visibility = Visibility.Collapsed };
    private readonly System.Diagnostics.Stopwatch _activityElapsed = new();
    private TimeSpan _activityBaseline;
    private string? _activityJobId;
    private readonly System.Windows.Threading.DispatcherTimer _activityClock = new() { Interval = TimeSpan.FromSeconds(1) };
    private readonly ProgressBar _activityIndicator = new() { Height = 3, IsIndeterminate = true,
        Visibility = Visibility.Collapsed, Margin = new Thickness(0, 8, 0, 0) };
    private readonly Button _permissionAttention = new() { Visibility = Visibility.Collapsed,
        HorizontalAlignment = HorizontalAlignment.Left, Padding = new Thickness(14, 9, 14, 9),
        Margin = new Thickness(0, 8, 0, 0), FontWeight = FontWeights.SemiBold };
    private readonly Button _investigate;
    private readonly Button _send;
    private readonly Button _saveMessage;
    private readonly Button _stopInvestigation;
    private readonly Button _collect;
    private NativeLaunchPolicy? _selectedLaunchPolicy;
    private bool _currentAgentReady;
    private bool _suggestInvestigation;
    private bool _supervisionDisabled;
    private bool _savedInvestigationActive;
    private bool _agentStatusUnverified;
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
    private long _permissionRenderSequence;
    private string _permissionRenderKey = "";
    private bool _permissionDecisionInFlight;
    private readonly TextBlock _resourceBlocker = new() { TextWrapping = TextWrapping.Wrap,
        Margin = new Thickness(0, 8, 0, 8), Visibility = Visibility.Collapsed };
    private readonly StackPanel _permissions = new();
    private readonly ScrollViewer _accessScroll = new();
    private string _accessRequestSet = "";
    private readonly Expander _accessRequests = new() { Header = "Agent access requests", IsExpanded = false };
    private Dictionary<string, string> _supervisorDigests = new();
    private readonly TextBlock _outcome = new() { TextWrapping = TextWrapping.Wrap };
    private readonly Border _outcomeSurface = new() { Visibility = Visibility.Collapsed,
        Padding = new Thickness(12), Margin = new Thickness(0, 12, 0, 0), CornerRadius = new CornerRadius(5) };
    private readonly TextBox _message = new() { AcceptsReturn = true, TextWrapping = TextWrapping.Wrap, MinHeight = 70 };
    private readonly StackPanel _actions = new();
    private readonly TextBlock _emptySelection = new() { TextWrapping = TextWrapping.Wrap,
        Foreground = SystemColors.GrayTextBrush, Margin = new Thickness(0, 4, 0, 8), Visibility = Visibility.Collapsed };
    private readonly ScrollViewer _detailsScroll = new();
    private readonly TabControl _workspaces = new() { BorderThickness = new Thickness(0), Background = System.Windows.Media.Brushes.Transparent };
    private readonly TabItem _selectedWorkspace = new() { Header = "Selected item" };
    private readonly TabItem _maintenanceWorkspace = new() { Header = "Maintenance" };
    private readonly ScrollViewer _maintenanceScroll = new() { VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
        Padding = new Thickness(12, 12, 12, 8) };
    private readonly Button _resume;
    private readonly Button _prepare;
    private readonly Button _confirm;
    private readonly Button _check;
    private readonly StackPanel _confirmation = new() { Visibility = Visibility.Collapsed };
    private readonly WrapPanel _decisions = new() { Margin = new Thickness(0, 15, 0, 0) };
    private readonly TextBlock _confirmationText = new() { TextWrapping = TextWrapping.Wrap };
    private readonly StackPanel _activity = new();
    private readonly TextBlock _historyStatus = new() { TextWrapping = TextWrapping.Wrap };
    private readonly TextBlock _actionHeadline = new() { TextWrapping = TextWrapping.Wrap, FontSize = 18, FontWeight = FontWeights.SemiBold };
    private readonly TextBlock _actionSummary = new() { TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 6, 0, 0) };
    private readonly Border _actionNotice = new() { Visibility = Visibility.Collapsed, Padding = new Thickness(14),
        Margin = new Thickness(0, 0, 0, 14), CornerRadius = new CornerRadius(5) };
    private readonly Expander _history = new();
    private List<NativeActionReview> _reviews = new();
    private NativeActionReview? _prepared;
    private bool _executionEnabled;
    private bool _evidenceComplete;
    private bool _actionInFlight;
    private bool _shutdownRequested;
    private bool _scheduledRunning;
    private Task? _initialization;
    private int _shutdownExitCode;
    private TaskCompletionSource<bool>? _investigationFinished;
    private readonly Dictionary<string, string> _drafts = new();
    private string? _selectedId;
    private long _sequence;
    private bool _busy;
    private TaskCompletionSource<bool>? _idle;
    private bool _rendering;
    private bool _loaded;
    private bool _refreshRequiredValue = true;
    private bool _refreshRequired {
        get => _refreshRequiredValue;
        set {
            _refreshRequiredValue = value;
            if (value)
            {
                _permissionReadSequence++;
                _permissionAttention.Visibility = Visibility.Collapsed;
                _permissions.Children.Clear();
                _accessRequestSet = "";
                _accessRequests.IsExpanded = false;
                _accessRequests.Header = "Agent access requests — refresh required";
                _launchProgress.Text = "Current evidence needs refresh. Action and access decisions are unavailable until the saved queue is verified.";
            }
        }
    }

    internal BackendQueueWindow(BackendClient client)
    {
        _client = client;
        Title = "Dependabot Watcher — review queue (development integration)";
        Width = 1080; Height = 840; MinWidth = 850; MinHeight = 560;
        WindowStartupLocation = WindowStartupLocation.CenterScreen;
        var root = new DockPanel { Margin = new Thickness(22) };
        var header = new StackPanel { Margin = new Thickness(0, 0, 0, 18) };
        FontFamily = new FontFamily("Segoe UI"); FontSize = 14;
        ApplyTheme();
        header.Children.Add(new TextBlock { Text = "Dependency review", FontSize = 24, FontWeight = FontWeights.SemiBold });
        header.Children.Add(_summary);
        header.Children.Add(_launchProgress);
        header.Children.Add(_elapsedActivity);
        _activityClock.Tick += (_, _) => {
            var active = !_shutdownRequested && (_savedInvestigationActive || _investigationCancellation is not null);
            _elapsedActivity.Visibility = active ? Visibility.Visible : Visibility.Collapsed;
            if (!active) { ResetActivityElapsed(); return; }
            if (!_activityElapsed.IsRunning) _activityElapsed.Start();
            var elapsed = _activityBaseline + _activityElapsed.Elapsed;
            _elapsedActivity.Text = $"Investigation elapsed: {Math.Max(0, (int)elapsed.TotalMinutes)}m {Math.Max(0, elapsed.Seconds):00}s wall time · includes approvals and tool waits."
                + " Current tool progress is not reported; elapsed time alone does not prove progress.";
        };
        _activityClock.Start();
        Closed += (_, _) => _activityClock.Stop();
        System.Windows.Automation.AutomationProperties.SetName(_activityIndicator, "Background operation in progress");
        header.Children.Add(_activityIndicator);
        _permissionAttention.Click += (_, _) => {
            _accessRequests.IsExpanded = true;
            _accessRequests.BringIntoView();
        };
        header.Children.Add(_permissionAttention);
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
        _history.Margin = new Thickness(0, 8, 0, 0);
        var historyContent = new StackPanel();
        historyContent.Children.Add(_historyStatus);
        historyContent.Children.Add(_activity);
        var historyScroll = new ScrollViewer { Content = historyContent, MaxHeight = 200, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        _history.Content = historyScroll;
        bottom.Children.Add(_history);
        DockPanel.SetDock(bottom, Dock.Bottom); root.Children.Add(bottom);
        var split = new Grid();
        split.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(0.7, GridUnitType.Star), MinWidth = 360 });
        split.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(10) });
        split.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1.3, GridUnitType.Star), MinWidth = 400 });
        _queue.AutoGenerateColumns = false; _queue.IsReadOnly = true; _queue.CanUserAddRows = false;
        _queue.SelectionMode = DataGridSelectionMode.Single;
        var rowStyle = new Style(typeof(DataGridRow));
        rowStyle.Setters.Add(new Setter(ToolTipProperty, new Binding("Label")));
        _queue.RowStyle = rowStyle;
        _queue.HeadersVisibility = DataGridHeadersVisibility.Column;
        _queue.Margin = new Thickness(0, 0, 4, 0);
        _queue.Columns.Add(new DataGridTextColumn { Header = "PR / alert", Binding = new Binding("QueueLabel"), Width = new DataGridLength(1, DataGridLengthUnitType.Star), MinWidth = 180 });
        _queue.Columns.Add(new DataGridTextColumn { Header = "Order", Binding = new Binding("Order"), Width = 65 });
        _queue.Columns.Add(new DataGridTextColumn { Header = "Decision", Binding = new Binding("Handling"), Width = 90 });
        _queue.SelectionChanged += (_, _) => SelectProposal();
        _message.TextChanged += (_, _) => {
            if (!_rendering && _selectedId is not null) _drafts[_selectedId] = _message.Text;
            if (!_rendering) UpdateMessageStatus();
        };
        split.Children.Add(_queue);
        var divider = new GridSplitter { Width = 6, HorizontalAlignment = HorizontalAlignment.Center,
            VerticalAlignment = VerticalAlignment.Stretch, ResizeDirection = GridResizeDirection.Columns,
            ResizeBehavior = GridResizeBehavior.PreviousAndNext, ShowsPreview = true };
        System.Windows.Automation.AutomationProperties.SetName(divider, "Resize queue and workspace");
        Grid.SetColumn(divider, 1); split.Children.Add(divider);
        var selectedSummary = new StackPanel();
        selectedSummary.Children.Add(_recommendation);
        selectedSummary.Children.Add(_handling);
        _actions.Children.Add(selectedSummary);
        AddCopyIcon(_handling, selectedSummary, () => _recommendation.Text + "\n" + _handling.Text);
        var assessment = new StackPanel();
        _agentIdentity.Margin = new Thickness(0, 12, 0, 0);
        assessment.Children.Add(_agentIdentity);
        assessment.Children.Add(_agentRecommendation);
        assessment.Children.Add(new Expander { Header = "Details and evidence", Content = _agentReason,
            Margin = new Thickness(0, 4, 0, 0), IsExpanded = false });
        assessment.Children.Add(_resourceBlocker);
        assessment.Children.Add(_nextStep);
        _actions.Children.Add(assessment);
        AddCopyIcon(_nextStep, assessment, () => string.Join("\n", new[] {
            _agentIdentity.Text, _agentRecommendation.Text, _resourceBlocker.Text, _nextStep.Text }.Where(value => !string.IsNullOrWhiteSpace(value))));
        _investigate = Button("Start investigation", StartItemInvestigationAsync);
        _investigate.IsEnabled = false;
        _actions.Children.Add(_investigate);
        var choices = _decisions;
        _prepare = Button("Review proposed merge", PrepareAsync);
        _prepare.ToolTip = "Opens the exact repository, PR, commit and merge details. Nothing merges unless you separately confirm on the next screen.";
        choices.Children.Add(_prepare);
        choices.Children.Add(Button("Defer one day", async () => await DecideAsync("defer")));
        choices.Children.Add(Button("Reject locally", async () => await DecideAsync("reject")));
        _resume = Button("Undo rejection or deferral", async () => await DecideAsync("resume"));
        choices.Children.Add(_resume);
        _actions.Children.Add(choices);
        InitializeBranchRefresh();
        _confirmation.Children.Add(new TextBlock { Text = "Confirm this one exact merge", FontSize = 17, FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 12, 0, 8) });
        _confirmation.Children.Add(_confirmationText);
        _confirm = Button("Confirm this merge", ConfirmAsync);
        _confirm.Background = PrimaryBrush;
        _confirm.Foreground = SystemColors.HighlightTextBrush;
        _confirm.FontWeight = FontWeights.SemiBold;
        _confirmation.Children.Add(_confirm);
        _confirmation.Children.Add(Button("Cancel review", () => {
            _prepared = null; _confirmation.Visibility = Visibility.Collapsed;
            _decisions.Visibility = Visibility.Visible;
            _outcome.Text = "Review canceled. Nothing was approved or merged.";
            _ = ReadAgentRepliesAsync();
            Dispatcher.BeginInvoke(System.Windows.Threading.DispatcherPriority.ContextIdle,
                new Action(() => { _detailsScroll.UpdateLayout(); _detailsScroll.ScrollToTop(); }));
            return Task.CompletedTask;
        }));
        _actions.Children.Add(_confirmation);
        _check = Button("Check saved action outcome", CheckOutcomeAsync);
        _check.Visibility = Visibility.Collapsed; _actions.Children.Add(_check);
        _outcomeSurface.Background = SystemParameters.HighContrast ? SystemColors.ControlBrush
            : new SolidColorBrush(Color.FromRgb(226, 232, 240));
        _outcomeSurface.Child = _outcome;
        _actions.Children.Add(_outcomeSurface);
        var outcomeDescriptor = System.ComponentModel.DependencyPropertyDescriptor.FromProperty(TextBlock.TextProperty, typeof(TextBlock));
        outcomeDescriptor.AddValueChanged(_outcome, (_, _) => {
            _outcomeSurface.Visibility = string.IsNullOrWhiteSpace(_outcome.Text) ? Visibility.Collapsed : Visibility.Visible;
        });
        _actions.Children.Add(new TextBlock { Text = "Conversation", FontSize = 16,
            Margin = new Thickness(0, 18, 0, 8) });
        var accessDetails = new StackPanel();
        accessDetails.Children.Add(_agentUsage);
        accessDetails.Children.Add(_launchPolicy);
        _actions.Children.Add(new Expander { Header = "Agent access and usage", Content = accessDetails,
            Margin = new Thickness(0, 0, 0, 8) });
        AddCopyIcon(_launchPolicy, accessDetails, () => string.Join("\n\n", new[] {
            _agentUsage.Text, _launchPolicy.Text }.Where(value => !string.IsNullOrWhiteSpace(value))));
        _actions.Children.Add(_messageStatus);
        _actions.Children.Add(new TextBlock { Text = "Your message to the agent", FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 0, 0, 6) });
        _actions.Children.Add(_message);
        var conversationActions = new WrapPanel();
        _send = Button("Send to agent", SendToAgentAsync);
        _send.FontWeight = FontWeights.SemiBold;
        _send.Padding = new Thickness(18, 9, 18, 9);
        _send.Background = PrimaryBrush;
        _send.Foreground = SystemColors.HighlightTextBrush;
        conversationActions.Children.Add(_send);
        _saveMessage = Button("Save without sending", async () => await DecideAsync("feedback"));
        conversationActions.Children.Add(_saveMessage);
        _actions.Children.Add(conversationActions);
        _actions.Children.Add(new Expander {
            Header = "Saved messages for this item", Content = _savedMessages,
            Margin = new Thickness(0, 8, 0, 0)
        });
        var agentPanel = new StackPanel();
        _latestAnswer.Children.Add(new TextBlock { Text = "Latest conversation", FontSize = 18,
            FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 18, 0, 10) });
        _latestAnswer.Children.Add(new TextBlock { Text = "YOU SENT", FontSize = 12, FontWeight = FontWeights.SemiBold, Foreground = SystemColors.GrayTextBrush });
        _latestAnswer.Children.Add(_latestQuestion);
        var replyPanel = new StackPanel();
        replyPanel.Children.Add(new TextBlock { Text = "AGENT REPLIED", FontSize = 12, FontWeight = FontWeights.SemiBold });
        replyPanel.Children.Add(_replySource);
        replyPanel.Children.Add(_latestReply);
        _latestAnswer.Children.Add(new Border { BorderBrush = PrimaryBrush, BorderThickness = new Thickness(3, 0, 0, 0),
            Padding = new Thickness(12, 8, 0, 8), Child = replyPanel });
        AddCopyIcon(_latestReply, replyPanel);
        _latestAnswer.Children.Add(_replyFreshness);
        _actions.Children.Add(_latestAnswer);
        _stopInvestigation = Button("Stop this investigation", StopInvestigationAsync);
        _stopInvestigation.HorizontalAlignment = HorizontalAlignment.Left;
        _stopInvestigation.Visibility = Visibility.Collapsed;
        header.Children.Add(_stopInvestigation);
        agentPanel.Children.Add(_agentReplies);
        agentPanel.Children.Add(Button("Refresh saved agent replies", ReadAgentRepliesAsync));
        _actions.Children.Add(new Expander {
            Header = "All agent answers and recommendation history", Content = agentPanel,
            IsExpanded = false, Margin = new Thickness(0, 12, 0, 0)
        });
        _actions.Children.Add(new Expander {
            Header = "Technical evidence", Content = _detail,
            Margin = new Thickness(0, 15, 0, 0)
        });
        var permissionPanel = new StackPanel();
        permissionPanel.Children.Add(_permissions);
        var refreshAccess = Button("Refresh agent access requests", ReadPermissionsAsync);
        refreshAccess.HorizontalAlignment = HorizontalAlignment.Left;
        permissionPanel.Children.Add(refreshAccess);
        var accessScroll = _accessScroll;
        accessScroll.Content = permissionPanel;
        accessScroll.MaxHeight = 180;
        accessScroll.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
        _accessRequests.Content = accessScroll;
        _accessRequests.Visibility = Visibility.Collapsed;
        _accessRequests.Expanded += (_, _) => {
            _workspaces.Visibility = Visibility.Collapsed;
            _detailsScroll.Visibility = Visibility.Collapsed;
            _accessRequests.Visibility = Visibility.Visible;
            Dispatcher.BeginInvoke(System.Windows.Threading.DispatcherPriority.ContextIdle,
                new Action(() => accessScroll.ScrollToTop()));
        };
        _accessRequests.Collapsed += (_, _) => {
            _accessRequests.Visibility = Visibility.Collapsed;
            _detailsScroll.Visibility = Visibility.Visible;
            _workspaces.Visibility = Visibility.Visible;
        };
        SizeChanged += (_, _) => {
            historyScroll.MaxHeight = Math.Clamp(ActualHeight * 0.12, 80, 160);
        };
        _accessRequests.Margin = new Thickness(12, 8, 12, 8);
        permissionPanel.Children.Insert(0, Button("Back to selected item", () => {
            _accessRequests.IsExpanded = false;
            return Task.CompletedTask;
        }));
        var scroll = _detailsScroll;
        scroll.Padding = new Thickness(12, 8, 12, 8);
        scroll.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
        System.Windows.Automation.AutomationProperties.SetName(scroll, "Recommendation details");
        var maintenance = new StackPanel();
        var actionNoticeContent = new StackPanel();
        actionNoticeContent.Children.Add(_actionHeadline);
        actionNoticeContent.Children.Add(_actionSummary);
        _actionNotice.Child = actionNoticeContent;
        AddCopyIcon(_actionSummary, actionNoticeContent);
        _actionNotice.Background = SystemParameters.HighContrast ? SystemColors.ControlBrush
            : new SolidColorBrush(Color.FromRgb(226, 232, 240));
        maintenance.Children.Add(_actionNotice);
        InitializeCompletion(maintenance);
        InitializeLocalSync(maintenance);
        maintenance.Children.Add(_branchRefreshHistory);
        _maintenanceScroll.Content = maintenance;
        var inspector = new StackPanel();
        inspector.Children.Add(new TextBlock { Text = "Selected item · actions and conversation", FontSize = 13,
            Foreground = SystemColors.GrayTextBrush, Margin = new Thickness(0, 0, 0, 8) });
        inspector.Children.Add(_emptySelection);
        inspector.Children.Add(_actions);
        scroll.Content = inspector;
        _selectedWorkspace.Content = scroll;
        _maintenanceWorkspace.Content = _maintenanceScroll;
        _workspaces.Items.Add(_selectedWorkspace);
        _workspaces.Items.Add(_maintenanceWorkspace);
        _workspaces.SelectedItem = _selectedWorkspace;
        var workspace = new Grid();
        workspace.SizeChanged += (_, _) => accessScroll.MaxHeight = Math.Max(120, workspace.ActualHeight - 50);
        workspace.Children.Add(_workspaces);
        workspace.Children.Add(_accessRequests);
        Grid.SetColumn(workspace, 2); split.Children.Add(workspace);
        root.Children.Add(split); Content = root;
        foreach (var text in new[] { _summary, _launchProgress, _actionHeadline, _actionSummary,
            _completionStatus, _localSyncStatus, _recommendation, _handling, _agentIdentity,
            _agentRecommendation, _agentReason, _nextStep, _messageStatus, _savedMessages,
            _agentReplies, _latestQuestion, _latestReply, _replySource, _replyFreshness,
            _agentUsage, _launchPolicy, _confirmationText, _detail, _outcome })
        {
            var copy = new MenuItem { Header = "Copy text" };
            copy.Click += (_, _) => {
                try { Clipboard.SetText(text.Text); }
                catch (System.Runtime.InteropServices.ExternalException) {
                    _launchProgress.Text = "Clipboard is busy. Please try copying again.";
                }
            };
            text.ContextMenu = new ContextMenu(); text.ContextMenu.Items.Add(copy);
            text.ToolTip = "Right-click to copy this text.";
        }
        foreach (var text in new[] { _summary, _launchProgress, _emptySelection, _messageStatus,
            _savedMessages, _historyStatus, _replyFreshness, _agentReason, _latestQuestion,
            _agentReplies, _confirmationText, _detail, _outcome })
            AddCopyIcon(text);
        Closing += (_, e) => {
            if (Application.Current is App { IsQuitting: true }) return;
            e.Cancel = true; Hide(); App.ExplainTrayOnce(); App.Record("queue_hidden");
        };
        IsVisibleChanged += async (_, _) => { if (IsVisible && _loaded) await RefreshAsync(); };
        Loaded += async (_, _) => await InitializeAsync();
    }

    internal Task InitializeAsync()
    {
        // Scheduled startup initializes the saved queue without ever showing
        // the window. Loaded alone cannot initialize an invisible tray owner.
        if (_initialization is not null) return _initialization;
        _loaded = true;
        return _initialization = RefreshAsync();
    }

    internal bool IsStopping => _shutdownRequested;

    internal async Task<string> RunScheduledAsync(string runId, bool notify)
    {
        await InitializeAsync();
        if (!await NativeInvestigationCoordination.WaitAsync(WaitForIdleAsync,
            () => _investigationCancellation is not null ? _investigationFinished?.Task : null,
            () => _shutdownRequested)) return "quiet";
        _scheduledRunning = true;
        SetBusy(true);
        try
        {
            NativeScheduledRun trigger;
            try
            {
                // A tray owner may outlive several collections or feedback
                // revisions. Initialization's cached snapshot is not current
                // authority for a new scheduled reservation.
                _refreshRequired = true;
                Apply(await _client.SendAsync(Request("snapshot")));
                if (_shutdownRequested) return "quiet";
                var response = await _client.SendAsync(new JsonObject {
                    ["schema_version"] = 1, ["request_id"] = "read_" + Guid.NewGuid().ToString("N"),
                    ["operation"] = "scheduled_supervisor_claim", ["run_id"] = runId
                });
                if (_shutdownRequested) return "quiet";
                trigger = NativeScheduledRun.Parse(response, runId, _supervisorDigests);
            }
            finally { SetBusy(false); }
            if (!trigger.Claimed)
            {
                if (trigger.Attention) _launchProgress.Text = trigger.ExistingReservation
                    ? "This scheduled trigger was already reserved. Work may still be running or need recovery; check saved agent state. Nothing was automatically retried."
                    : "Scheduled investigation needs attention before it can start. Check saved collection, configuration and agent recovery. No new agent was launched.";
                return trigger.Attention ? "attention" : "quiet";
            }
            var confirmed = false;
            var attention = trigger.Attention;
            foreach (var slot in trigger.Slots)
            {
                if (_shutdownRequested) return "quiet";
                var finished = await RunInvestigationAsync(slot.Repository, slot.ContextDigest, null, null, runId, notify);
                if (_shutdownRequested) return "quiet";
                confirmed |= finished;
                if (!finished) { attention = true; break; }
            }
            return attention ? "attention" : confirmed ? "completed" : "quiet";
        }
        catch (Exception exception)
        {
            if (!_shutdownRequested) _launchProgress.Text = NativeDisplayText.Escape(
                $"Scheduled work was not confirmed: {exception.Message}\nCheck saved agent state before continuing; nothing is automatically retried.");
            throw;
        }
        finally { _scheduledRunning = false; SetBusy(_busy); }
    }

    internal void RequestQuit(int exitCode = 0)
    {
        if (exitCode != 0) _shutdownExitCode = exitCode;
        if (_shutdownRequested) return;
        _shutdownRequested = true;
        _agentReadSequence++; _permissionReadSequence++;
        _launchRefreshPending = false;
        _localObservationCancellation?.Cancel();
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

    private void ResetActivityElapsed()
    {
        _activityElapsed.Reset();
        _activityBaseline = TimeSpan.Zero;
        _activityJobId = null;
    }

    private static Brush PrimaryBrush => SystemParameters.HighContrast
        ? SystemColors.HighlightBrush : new SolidColorBrush(Color.FromRgb(24, 104, 91));

    private void ApplyTheme()
    {
        // Retain the platform's accessible colors/templates in high contrast.
        if (SystemParameters.HighContrast) return;
        Background = new SolidColorBrush(Color.FromRgb(248, 250, 252));
        Resources = (ResourceDictionary)XamlReader.Parse("""
            <ResourceDictionary xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
                                xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml">
              <Style TargetType="TabItem">
                <Setter Property="Padding" Value="12,8"/>
                <Setter Property="Foreground" Value="#64748B"/>
                <Setter Property="Template">
                  <Setter.Value>
                    <ControlTemplate TargetType="TabItem">
                      <Border x:Name="TabSurface" Padding="{TemplateBinding Padding}"
                              Background="Transparent" BorderBrush="Transparent" BorderThickness="0,0,0,2">
                        <ContentPresenter x:Name="TabHeader" ContentSource="Header" RecognizesAccessKey="True"/>
                      </Border>
                      <ControlTemplate.Triggers>
                        <Trigger Property="IsSelected" Value="True">
                          <Setter Property="Foreground" Value="#1F2937"/>
                          <Setter TargetName="TabHeader" Property="TextElement.FontWeight" Value="SemiBold"/>
                          <Setter TargetName="TabSurface" Property="BorderBrush" Value="#2D5553"/>
                        </Trigger>
                        <Trigger Property="IsMouseOver" Value="True"><Setter TargetName="TabSurface" Property="Background" Value="#E2E8F0"/></Trigger>
                        <Trigger Property="IsKeyboardFocused" Value="True"><Setter TargetName="TabSurface" Property="Background" Value="#E2E8F0"/></Trigger>
                      </ControlTemplate.Triggers>
                    </ControlTemplate>
                  </Setter.Value>
                </Setter>
              </Style>
              <Style TargetType="Button">
                <Setter Property="Background" Value="White"/>
                <Setter Property="Foreground" Value="#1F2937"/>
                <Setter Property="BorderBrush" Value="#CBD5E1"/>
                <Setter Property="BorderThickness" Value="1"/>
                <Setter Property="MinHeight" Value="34"/>
                <Setter Property="Template">
                  <Setter.Value>
                    <ControlTemplate TargetType="Button">
                      <Border x:Name="Surface" Background="{TemplateBinding Background}"
                              BorderBrush="{TemplateBinding BorderBrush}" BorderThickness="{TemplateBinding BorderThickness}"
                              CornerRadius="5" Padding="{TemplateBinding Padding}">
                        <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center" RecognizesAccessKey="True"/>
                      </Border>
                      <ControlTemplate.Triggers>
                        <Trigger Property="IsMouseOver" Value="True"><Setter TargetName="Surface" Property="Opacity" Value="0.85"/></Trigger>
                        <Trigger Property="IsPressed" Value="True"><Setter TargetName="Surface" Property="Opacity" Value="0.7"/></Trigger>
                        <Trigger Property="IsKeyboardFocused" Value="True"><Setter TargetName="Surface" Property="BorderThickness" Value="2"/><Setter TargetName="Surface" Property="BorderBrush" Value="#334155"/></Trigger>
                        <Trigger Property="IsEnabled" Value="False"><Setter TargetName="Surface" Property="Opacity" Value="0.5"/></Trigger>
                      </ControlTemplate.Triggers>
                    </ControlTemplate>
                  </Setter.Value>
                </Setter>
              </Style>
              <Style TargetType="DataGrid">
                <Setter Property="Background" Value="White"/>
                <Setter Property="BorderBrush" Value="#CBD5E1"/>
                <Setter Property="GridLinesVisibility" Value="None"/>
                <Setter Property="RowHeight" Value="30"/>
                <Setter Property="AlternatingRowBackground" Value="#F8FAFC"/>
              </Style>
              <Style TargetType="DataGridColumnHeader">
                <Setter Property="Background" Value="#F1F5F9"/>
                <Setter Property="Padding" Value="8,8"/>
                <Setter Property="FontSize" Value="12"/>
                <Setter Property="FontWeight" Value="SemiBold"/>
                <Setter Property="BorderThickness" Value="0"/>
              </Style>
              <Style TargetType="DataGridCell">
                <Setter Property="BorderThickness" Value="0"/>
                <Setter Property="Padding" Value="4"/>
                <Style.Triggers>
                  <Trigger Property="IsSelected" Value="True"><Setter Property="Background" Value="#E2E8F0"/><Setter Property="Foreground" Value="#111827"/></Trigger>
                </Style.Triggers>
              </Style>
            </ResourceDictionary>
            """);
    }

    private void AddCopyIcon(TextBlock text, FrameworkElement? region = null, Func<string>? copyText = null)
    {
        region ??= text;
        var parent = region.Parent;
        var index = parent is Panel panel ? panel.Children.IndexOf(region) : -1;
        if (parent is Panel container) container.Children.RemoveAt(index);
        else if (parent is ContentControl content && ReferenceEquals(content.Content, region)) content.Content = null;
        else if (parent is Decorator decorator && ReferenceEquals(decorator.Child, region)) decorator.Child = null;
        else return;
        // The reserved gap and hidden icon must remain part of one hover target.
        // A null background makes empty Grid space fall through to the inspector,
        // hiding the icon while the pointer crosses from the text to the button.
        var block = new Grid { Margin = region.Margin, Background = Brushes.Transparent };
        region.Margin = new Thickness(0, 0, 30, 0);
        block.Children.Add(region);
        var icon = new Button { Content = "\uE8C8", FontFamily = new FontFamily("Segoe MDL2 Assets"),
            Width = 26, Height = 26, Padding = new Thickness(3), BorderThickness = new Thickness(0),
            Background = Brushes.Transparent, HorizontalAlignment = HorizontalAlignment.Right,
            VerticalAlignment = VerticalAlignment.Top, Opacity = 0, IsHitTestVisible = false,
            ToolTip = "Copy text" };
        System.Windows.Automation.AutomationProperties.SetName(icon, "Copy this text block");
        void Reveal() {
            var visible = block.IsMouseOver || block.IsKeyboardFocusWithin;
            icon.Opacity = visible ? 1 : 0;
            icon.IsHitTestVisible = visible;
        }
        block.MouseEnter += (_, _) => Reveal(); block.MouseLeave += (_, _) => Reveal();
        block.IsKeyboardFocusWithinChanged += (_, _) => Reveal();
        icon.Click += async (_, _) => {
            try
            {
                Clipboard.SetText(copyText?.Invoke() ?? CopyRegionText(region));
                icon.Content = "\uE73E";
                icon.ToolTip = "Copied";
                await Task.Delay(1500);
                icon.Content = "\uE8C8";
                icon.ToolTip = "Copy text";
            }
            catch (System.Runtime.InteropServices.ExternalException)
            {
                _launchProgress.Text = "Clipboard is busy. Nothing was copied; please try again.";
            }
        };
        block.Children.Add(icon);
        if (parent is Panel destination) destination.Children.Insert(index, block);
        else if (parent is ContentControl destinationContent) destinationContent.Content = block;
        else ((Decorator)parent).Child = block;
        if (ReferenceEquals(region, text))
        {
            void UpdateVisibility() => block.Visibility = string.IsNullOrWhiteSpace(text.Text)
                || text.Visibility == Visibility.Collapsed ? Visibility.Collapsed : text.Visibility;
            System.ComponentModel.DependencyPropertyDescriptor.FromProperty(TextBlock.TextProperty, typeof(TextBlock))
                .AddValueChanged(text, (_, _) => UpdateVisibility());
            System.ComponentModel.DependencyPropertyDescriptor.FromProperty(VisibilityProperty, typeof(TextBlock))
                .AddValueChanged(text, (_, _) => UpdateVisibility());
            UpdateVisibility();
        }
    }

    private static string CopyRegionText(FrameworkElement region) => region is Panel panel
        ? string.Join("\n", panel.Children.OfType<TextBlock>()
            .Where(child => child.Visibility == Visibility.Visible && !string.IsNullOrWhiteSpace(child.Text))
            .Select(child => child.Text))
        : region is TextBlock text ? text.Text : "";

    private static Button Button(string label, Func<Task> action)
    {
        var button = new Button { Content = label, Padding = new Thickness(12, 7, 12, 7), Margin = new Thickness(0, 6, 8, 0) };
        button.Click += async (_, _) => await action();
        return button;
    }

    private JsonObject Request(string operation) => new() {
        ["schema_version"] = 1,
        ["request_id"] = operation is "prepare_action" or "confirm_action" or "check_action" or "check_completion" or "action_history" or "collect_queue"
            ? "action_" + Guid.NewGuid().ToString("N")
            : operation == "snapshot" ? "read_" + Guid.NewGuid().ToString("N")
            : $"decision_{_sequence}_" + Guid.NewGuid().ToString("N"),
        ["operation"] = operation
    };

    private bool KnownInvestigationOwnsMaintenance => _scheduledRunning || _savedInvestigationActive || _investigationCancellation is not null;
    private bool InvestigationOwnsMaintenance => KnownInvestigationOwnsMaintenance || _agentStatusUnverified;
    private bool MaintenanceOperationBlocked => _busy || _shutdownRequested || InvestigationOwnsMaintenance;

    private void SetBusy(bool busy)
    {
        if (_refreshRequired) _permissionAttention.Visibility = Visibility.Collapsed;
        if (busy && !_busy) _idle = new(TaskCreationOptions.RunContinuationsAsynchronously);
        if (!busy) { _idle?.TrySetResult(true); _idle = null; }
        _busy = busy;
        _queue.IsEnabled = !busy && !_shutdownRequested;
        _actions.IsEnabled = !busy && !_shutdownRequested && !_refreshRequired && _queue.SelectedItem is QueueProposal;
        _activity.IsEnabled = !MaintenanceOperationBlocked;
        _completionPanel.IsEnabled = !MaintenanceOperationBlocked;
        _permissions.IsEnabled = !busy && !_shutdownRequested && !_refreshRequired && !_permissionDecisionInFlight;
        _collect.IsEnabled = !MaintenanceOperationBlocked;
        _localSyncPanel.IsEnabled = !MaintenanceOperationBlocked && !_refreshRequired;
        _branchRefreshHistory.IsEnabled = !BranchMaintenanceBlocked && !_refreshRequired;
        _decisions.IsEnabled = !MaintenanceOperationBlocked && !_refreshRequired;
        _saveMessage.IsEnabled = _decisions.IsEnabled;
        UpdateBranchRefreshControls();
        UpdateInvestigationControls();
        if (!busy && !_shutdownRequested && _launchRefreshPending && !_launchRefreshInFlight) _ = RefreshInvestigationScopeAsync();
    }

    private void UpdateInvestigationControls()
    {
        _activityIndicator.Visibility = !_shutdownRequested && (_busy || _savedInvestigationActive || _investigationCancellation is not null)
            ? Visibility.Visible : Visibility.Collapsed;
        _investigate.Content = _retryJobId is null ? "Investigate this item" : "Retry investigation";
        _investigate.Visibility = _prepared is null && (_suggestInvestigation || _retryJobId is not null)
            ? Visibility.Visible : Visibility.Collapsed;
        var repository = (_queue.SelectedItem as QueueProposal)?.Evidence["repository"]?.GetValue<string>();
        _investigate.IsEnabled = !_busy && !_shutdownRequested && !_scheduledRunning && !_refreshRequired && !_savedInvestigationActive && _investigationCancellation is null
            && _selectedLaunchPolicy?.Configured == true && repository == _launchPolicyRepository
            && repository is not null && _supervisorDigests.GetValueOrDefault(repository) == _launchPolicyDigest;
        _send.IsEnabled = _investigate.IsEnabled && !string.IsNullOrWhiteSpace(_message.Text)
            && Encoding.UTF8.GetByteCount(_message.Text) <= 8192;
        _prepare.IsEnabled = !_busy && !_shutdownRequested && !_scheduledRunning && _investigationCancellation is null
            && CanPrepareSelected() && (_currentAgentReady || _supervisionDisabled);
        _prepare.Background = _prepare.IsEnabled ? PrimaryBrush : SystemColors.ControlBrush;
        _prepare.Foreground = _prepare.IsEnabled ? SystemColors.HighlightTextBrush : SystemColors.GrayTextBrush;
        _investigate.Background = _investigate.IsEnabled ? PrimaryBrush : SystemColors.ControlBrush;
        _investigate.Foreground = _investigate.IsEnabled ? SystemColors.HighlightTextBrush : SystemColors.GrayTextBrush;
        _confirm.IsEnabled = !_busy && !_shutdownRequested && !_scheduledRunning && _investigationCancellation is null;
    }

    private bool CanPrepareSelected() => _queue.SelectedItem is QueueProposal item && !_refreshRequired
        && _executionEnabled && _evidenceComplete && item.Evidence["evidence_status"]?.GetValue<string>() == "complete"
        && item.Handling == "pending" && item.Step["action"]?.GetValue<string>() == "validate_then_merge"
        && !_reviews.Any(review => review.MatchesRepository(item.Evidence["repository"]?.GetValue<string>())
            && (review.Status == "recovery_required" || review.HasPendingCompletion || (review.Status == "completed"
                && review.Preparation["plan_id"]?.GetValue<string>() == item.Evidence["plan_id"]?.GetValue<string>())));

    private async Task WaitForIdleAsync()
    {
        while (_busy && _idle is not null) await _idle.Task;
    }

    private void Apply(JsonObject response)
    {
        var priorItem = (_queue.SelectedItem as QueueProposal)?.ItemIdentity;
        (string Question, string Answer, string Source, string Freshness)? priorConversation =
            _investigationCancellation is not null && _latestAnswer.Visibility == Visibility.Visible
                ? (_latestQuestion.Text, _latestReply.Text, _replySource.Text, _replyFreshness.Text) : null;
        var priorResource = _resourceBlocker.Visibility == Visibility.Visible ? _resourceBlocker.Text : "";
        var priorRepository = priorItem?["repository"]?.GetValue<string>();
        var priorDigest = priorRepository is null ? null : _supervisorDigests.GetValueOrDefault(priorRepository);
        // Every applied queue frame invalidates pending reads, including a
        // policy change that keeps the same proposal ID and local version.
        _agentReadSequence++;
        _permissionReadSequence++;
        _permissionRenderSequence++;
        _permissionRenderKey = "";
        _permissionAttention.Visibility = Visibility.Collapsed;
        _permissions.Children.Clear();
        _accessRequests.Header = "Agent access requests — checking";
        _agentReplies.Text = "Agent replies need verification against this queue frame.";
        _latestAnswer.Visibility = Visibility.Collapsed;
        _resourceBlocker.Text = "";
        _resourceBlocker.Visibility = Visibility.Collapsed;
        ResetAgentGuidance();
        _agentUsage.Text = "Checking access and usage…";
        _selectedLaunchPolicy = null; _launchPolicyRepository = null; _launchPolicyDigest = null;
        _retryJobId = null;
        _launchPolicy.Text = "Checking the saved investigation policy…";
        _agentIdentity.Text = "Agent: checking configuration…";
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
        if (proposals.Select(item => item.ItemKey).Distinct().Count() != proposals.Count)
            throw new InvalidDataException("Queue response repeats a repository item.");
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
        RenderCompletion();
        var previous = _selectedId;
        var previousItem = (_queue.SelectedItem as QueueProposal)?.ItemIdentity;
        _summary.Text = (_client.DiagnosticTransport ? "SYNTHETIC TRANSPORT TEST — no real GitHub access or model jobs.\n" : "")
            + $"{proposals.Count} items · Evidence: {response["evidence_status"]} · "
            + (executionEnabled ? "Approval required" : "Read-only")
            + "\nPlanner order is a starting order, not an approved merge sequence.";
        _queue.ItemsSource = proposals;
        _queue.SelectedItem = QueueProposal.RestoreSelection(proposals, previous, previousItem);
        ReadBranchRefreshReviews(response);
        // Initial empty queues do not raise SelectionChanged. Apply the same
        // clearing path used when a previously selected item is retired.
        if (_queue.SelectedItem is null) SelectProposal();
        // A read-only scope refresh must not blank the same item's already
        // verified history while its new status read is pending. Never carry
        // current readiness or a different item's text across this boundary.
        if (_investigationCancellation is not null && priorItem is not null
            && _queue.SelectedItem is QueueProposal restored && restored.SameItem(priorItem)) {
            if (priorConversation is not null) ShowConversation(priorConversation, running: true);
            if (priorRepository is not null && priorDigest == _supervisorDigests.GetValueOrDefault(priorRepository)) {
                _resourceBlocker.Text = priorResource;
                _resourceBlocker.Visibility = priorResource.Length == 0 ? Visibility.Collapsed : Visibility.Visible;
            }
        }
        var retired = response["history"]?.AsArray().Count ?? 0;
        App.Record("backend_queue_loaded", new { proposals = proposals.Count, retired });
    }

    private void SelectProposal()
    {
        _workspaces.SelectedItem = _selectedWorkspace;
        _permissionReadSequence++;
        _permissionRenderSequence++;
        _permissionRenderKey = "";
        _permissions.Children.Clear();
        _accessRequests.IsExpanded = false;
        _accessRequests.Header = "Agent access requests — checking";
        _decisions.Visibility = Visibility.Visible;
        _permissionAttention.Visibility = Visibility.Collapsed;
        _prepared = null; _confirmation.Visibility = Visibility.Collapsed;
        _selectedId = (_queue.SelectedItem as QueueProposal)?.Id;
        RefreshBranchRefreshCandidates();
        _selectedLaunchPolicy = null; _launchPolicyRepository = null; _launchPolicyDigest = null;
        _launchPolicy.Text = "Checking the saved investigation policy…";
        _investigate.IsEnabled = false;
        _agentIdentity.Text = "Agent: checking configuration…";
        _agentUsage.Text = "Checking access and usage…";
        _latestAnswer.Visibility = Visibility.Collapsed;
        _resourceBlocker.Text = "";
        _resourceBlocker.Visibility = Visibility.Collapsed;
        ResetAgentGuidance(_investigationCancellation is null ? "Checking status…" : "Investigation running");
        _actions.IsEnabled = !_busy && !_shutdownRequested && !_refreshRequired && _selectedId is not null;
        _agentReplies.Text = "Checking this item's saved conversation…";
        if (_queue.SelectedItem is not QueueProposal item)
        {
            _actions.Visibility = Visibility.Collapsed;
            _emptySelection.Text = _queue.Items.Count == 0 ? "No items remain in the saved queue. Previous outcomes and follow-up are in Maintenance."
                : "Select a queue item to investigate or send a message. Previous-action follow-up has its own scope.";
            _emptySelection.Visibility = Visibility.Visible;
            _recommendation.Text = "Select a queue item to investigate or send a message.";
            _handling.Text = "The previous item may have left the active queue. Its follow-up is in Maintenance.";
            _rendering = true; _message.Clear(); _rendering = false;
            _messageStatus.Text = "No message recipient selected.";
            _outcome.Text = ""; _detail.Text = ""; _savedMessages.Text = "";
            _agentReplies.Text = "No queue item selected.";
            _prepare.Visibility = Visibility.Collapsed; _resume.Visibility = Visibility.Collapsed;
            _check.Visibility = Visibility.Collapsed; _investigate.Visibility = Visibility.Collapsed;
            _permissions.Children.Clear();
            _accessRequests.IsExpanded = false;
            _accessRequestSet = "";
            if (_loaded && _investigationCancellation is not null && !_refreshRequired)
                _ = ReadPermissionsAsync();
            return;
        }
        _actions.Visibility = Visibility.Visible;
        _emptySelection.Visibility = Visibility.Collapsed;
        var step = item.Step;
        var feedback = item.Evidence["feedback"]!.AsArray();
        var messages = NativeDisplayText.Escape(string.Join("\n\n", feedback.Select(value =>
            $"{value!["created_at"]}: {value["message"]}\nSaved locally. Verified answers, when available, appear below.")));
        _recommendation.Text = NativeDisplayText.Escape(item.Identity["title"]?.GetValue<string>() ?? step["summary"]!.GetValue<string>());
        _handling.Text = $"{item.Label}\nYour saved decision: {(item.Handling == "pending" ? "none yet" : item.Handling)}"
            + (item.Handling == "deferred" ? $"\nDeferred until {item.Evidence["resume_at"]}." : "");
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
        if (_shutdownRequested || _permissionDecisionInFlight) return;
        var generation = ++_permissionReadSequence;
        if (_refreshRequired) return;
        var repository = PermissionRepository();
        if (repository is null) {
            _permissions.Children.Clear();
            _accessRequests.Header = "Agent access requests";
            _accessRequests.IsExpanded = false;
            _permissions.Children.Add(new TextBlock { Text = "No repository selected for access requests." });
            return;
        }
        if (!_supervisorDigests.TryGetValue(repository, out var expectedDigest)) return;
        bool StillCurrent() => generation == _permissionReadSequence && !_shutdownRequested && !_refreshRequired
            && PermissionRepository() == repository
            && _supervisorDigests.TryGetValue(repository, out var digest) && digest == expectedDigest;
        try
        {
            JsonObject? response = null;
            Exception? readFailure = null;
            await NativeScopeRefresh.RunAsync(async () => {
                response = await _client.SendAsync(new JsonObject {
                    ["schema_version"] = 1, ["request_id"] = "read_" + Guid.NewGuid().ToString("N"),
                    ["operation"] = "supervisor_permissions", ["repository"] = repository
                });
            }, (exception, retry) => {
                readFailure = exception;
                if (retry && StillCurrent())
                    _accessRequests.Header = "Agent access requests — checking current requests…";
            }, () => !StillCurrent(), duration => Task.Delay(duration));
            if (!StillCurrent()) return;
            if (response is null) throw readFailure ?? new IOException("Access requests were not received.");
            var requests = NativePermissionView.Parse(response, repository, expectedDigest);
            if (!StillCurrent() || _permissionDecisionInFlight) return;
            var requestSet = string.Join("|", requests.Select(permission => permission.ConnectionId + "/" + permission.PermissionId));
            var renderKey = repository + "/" + expectedDigest + "/" + string.Join("|", requests.Select(permission =>
                permission.ConnectionId + "/" + permission.PermissionId + "/" + permission.Digest + "/"
                + string.Join(",", permission.Choices) + "/"
                + (_permissionPreviews.TryGetValue(permission.ConnectionId + "/" + permission.PermissionId, out var exact)
                    && NativeLaunchStream.MatchesPermission(exact, permission, expectedDigest))));
            if (renderKey == _permissionRenderKey) return;
            _permissionRenderKey = renderKey;
            var renderSequence = ++_permissionRenderSequence;
            bool DecisionCurrent() => renderSequence == _permissionRenderSequence && !_shutdownRequested && !_refreshRequired
                && PermissionRepository() == repository
                && _supervisorDigests.TryGetValue(repository, out var digest) && digest == expectedDigest;
            var priorRequests = _accessRequestSet.Split('|').ToHashSet(StringComparer.Ordinal);
            var newlyPending = requests.Any(permission => !priorRequests.Contains(permission.ConnectionId + "/" + permission.PermissionId));
            _accessRequestSet = requestSet;
            _permissions.Children.Clear();
            // A visible call to action must reflect verified current requests,
            // not merely the existence of this optional access panel.
            _accessRequests.Header = requests.Count == 0
                ? "Agent access requests — none pending" : $"Agent needs additional access · {requests.Count} pending";
            _permissionAttention.Content = $"Action needed: review {requests.Count} agent access request(s)";
            _permissionAttention.Visibility = requests.Count == 0 ? Visibility.Collapsed : Visibility.Visible;
            if (_investigationCancellation is not null)
                _launchProgress.Text = requests.Count == 0
                    ? $"Investigation running · {NativeDisplayText.Escape(repository)} · no access decision is currently pending. No additional merge is approved."
                    : $"Waiting for your decision · {requests.Count} access request(s) · {NativeDisplayText.Escape(repository)}. This is not merge approval.";
            if (requests.Count == 0 && _actionInFlight && _investigationCancellation is not null)
                _actionSummary.Text = NativeDisplayText.Escape(repository)
                    + "\nMerge complete. Post-merge investigation is running; no access decision is currently pending.";
            _permissionAttention.Background = SystemParameters.HighContrast ? SystemColors.HighlightBrush
                : new SolidColorBrush(Color.FromRgb(255, 234, 190));
            if (requests.Count == 0) _accessRequests.IsExpanded = false;
            else if (newlyPending) _accessRequests.IsExpanded = true;
            if (requests.Count == 0)
                _permissions.Children.Add(new TextBlock { Text = "No additional access is waiting for your decision.", TextWrapping = TextWrapping.Wrap });
            var batch = requests.Where(permission => permission.Choices.Contains("allow_once")
                && _permissionPreviews.TryGetValue(permission.ConnectionId + "/" + permission.PermissionId, out var exact)
                && exact["repository"]?.GetValue<string>() == repository
                && NativeLaunchStream.MatchesPermission(exact, permission, expectedDigest)).ToList();
            Button? batchAction = null;
            void UpdateBatchAction() {
                if (batchAction is null) return;
                batchAction.Content = $"Allow these {batch.Count} requests once…";
                batchAction.Visibility = batch.Count > 1 ? Visibility.Visible : Visibility.Collapsed;
            }
            if (batch.Count > 1) {
                batchAction = Button($"Allow these {batch.Count} requests once…", async () => {
                    if (!DecisionCurrent() || _busy || _permissionDecisionInFlight) return;
                    if (batch.Count == 0) { await ReadPermissionsAsync(); return; }
                    var displayedBatch = batch.ToList();
                    var displayedPreviews = new List<JsonObject>();
                    foreach (var permission in displayedBatch) {
                        if (!_permissionPreviews.TryGetValue(permission.ConnectionId + "/" + permission.PermissionId, out var exact)
                            || exact["repository"]?.GetValue<string>() != repository
                            || !NativeLaunchStream.MatchesPermission(exact, permission, expectedDigest)) {
                            _outcome.Text = "These access details changed. Refresh requests before deciding; no batch decision was sent.";
                            return;
                        }
                        displayedPreviews.Add((JsonObject)exact.DeepClone());
                    }
                    var details = string.Join("\n\n", displayedPreviews.Select((preview, index) =>
                        $"Request {index + 1}\n" + PermissionDescription(preview)));
                    if (!NativeConfirmationDialog.Confirm(this, "Allow displayed access requests once",
                        $"{repository}\n\nAllow only these {displayedBatch.Count} requests? This does not approve future requests or a merge. Commands may have side effects; this is not a read-only classification.\n\n{details}",
                        "Allow these requests once")) return;
                    if (!DecisionCurrent() || _permissionDecisionInFlight) return;
                    _permissionDecisionInFlight = true;
                    _permissions.IsEnabled = false;
                    var completed = 0;
                    try {
                        foreach (var permission in displayedBatch) {
                            if (!DecisionCurrent()) break;
                            var key = permission.ConnectionId + "/" + permission.PermissionId;
                            if (!_permissionPreviews.TryGetValue(key, out var exact)
                                || !NativeLaunchStream.MatchesPermission(exact, permission, expectedDigest)) break;
                            var reply = await _client.SendAsync(new JsonObject {
                                ["schema_version"] = 1, ["request_id"] = "permission_" + Guid.NewGuid().ToString("N"),
                                ["operation"] = "supervisor_permission_decide", ["repository"] = repository,
                                ["connection_id"] = permission.ConnectionId, ["permission_id"] = permission.PermissionId,
                                ["digest"] = permission.Digest, ["decision"] = "allow_once"
                            });
                            var state = NativePermissionView.ParseDecision(reply, repository, expectedDigest, permission, "allow_once");
                            if (state == "invalidated") break;
                            completed++;
                            _permissionAttention.Content = $"Access decisions saved: {completed} of {batch.Count}";
                        }
                        if (DecisionCurrent()) _outcome.Text = $"Saved {completed} of {displayedBatch.Count} access decisions. Only this displayed batch was included; no merge was approved.";
                    } catch (Exception ex) {
                        if (DecisionCurrent()) _outcome.Text = $"Batch stopped after {completed} confirmed decisions: {ex.Message}\nAn unconfirmed decision may already be saved. Refresh before deciding again; no automatic replay.";
                    } finally {
                        _permissionDecisionInFlight = false;
                        _permissions.IsEnabled = !_busy && !_shutdownRequested && !_refreshRequired;
                        if (!_shutdownRequested) await ReadPermissionsAsync();
                    }
                });
                batchAction.HorizontalAlignment = HorizontalAlignment.Left;
                batchAction.Background = PrimaryBrush;
                batchAction.Foreground = SystemColors.HighlightTextBrush;
                _permissions.Children.Add(batchAction);
            }
            foreach (var permission in requests)
            {
                var panel = new StackPanel { Margin = new Thickness(0, 8, 0, 12) };
                panel.Children.Add(new TextBlock { Text = NativeDisplayText.Escape(repository + "\n" + permission.Description), TextWrapping = TextWrapping.Wrap, FontWeight = FontWeights.SemiBold });
                var previewKey = permission.ConnectionId + "/" + permission.PermissionId;
                var hasPreview = _permissionPreviews.TryGetValue(previewKey, out var preview)
                    && preview["repository"]?.GetValue<string>() == repository
                    && NativeLaunchStream.MatchesPermission(preview, permission, expectedDigest);
                if (hasPreview) panel.Children.Add(PermissionPreview(preview!));
                else panel.Children.Add(new TextBlock {
                    Text = "Exact access details are not available in this window. Allow is disabled; you may deny or cancel. No access is granted automatically.",
                    TextWrapping = TextWrapping.Wrap });
                panel.Children.Add(new TextBlock { Text = "This is agent access—not approval to merge. A stopped or changed job cannot reuse this decision.", TextWrapping = TextWrapping.Wrap });
                var choices = new WrapPanel();
                foreach (var choice in permission.Choices)
                {
                    if ((choice is "allow_once" or "allow_session") && !hasPreview) continue;
                    var label = choice switch { "allow_once" => "Allow once", "allow_session" => "Allow for this session",
                        "deny" => "Deny access", _ => "Cancel this request" };
                    var permissionAction = Button(label, async () => {
                        if (!DecisionCurrent() || _busy || _permissionDecisionInFlight) return;
                        if (choice is "allow_once" or "allow_session")
                            if (!_permissionPreviews.TryGetValue(previewKey, out var livePreview)
                                || !NativeLaunchStream.MatchesPermission(livePreview, permission, expectedDigest)) return;
                        panel.IsEnabled = false;
                        _permissionDecisionInFlight = true;
                        _permissions.IsEnabled = false;
                        var decisionStatus = new TextBlock { Text = "Saving your access decision…", TextWrapping = TextWrapping.Wrap };
                        panel.Children.Add(decisionStatus);
                        try
                        {
                            var acknowledged = await _client.SendAsync(new JsonObject {
                                ["schema_version"] = 1, ["request_id"] = "permission_" + Guid.NewGuid().ToString("N"),
                                ["operation"] = "supervisor_permission_decide", ["repository"] = repository,
                                ["connection_id"] = permission.ConnectionId, ["permission_id"] = permission.PermissionId,
                                ["digest"] = permission.Digest, ["decision"] = choice
                            });
                            var state = NativePermissionView.ParseDecision(acknowledged, repository, expectedDigest, permission, choice);
                            if (DecisionCurrent())
                                _outcome.Text = state switch {
                                    "saved_waiting_delivery" => "Your access decision was saved, but has not yet been sent to the agent. This does not authorize a merge.",
                                    "sent_waiting_resolution" => "Your access decision was sent to Codex; confirmation is pending. This does not authorize a merge.",
                                    "resolved" => "Codex resolved this access request. This does not authorize a merge.",
                                    _ => "The agent stopped and this access decision was invalidated. It will not be replayed. Refresh the saved queue to recover."
                                };
                            batch.Remove(permission);
                            UpdateBatchAction();
                            decisionStatus.Text = state == "invalidated"
                                ? "Request invalidated because the agent stopped. No decision will be replayed."
                                : "Decision saved. Waiting for the agent to continue.";
                        }
                        catch (Exception ex)
                        {
                            batch.Remove(permission);
                            UpdateBatchAction();
                            decisionStatus.Text = "Decision not confirmed. Refresh requests before trying again.";
                            if (DecisionCurrent()) _outcome.Text = $"Access decision was not confirmed: {ex.Message}\nRefresh the requests before trying again.";
                            // A lost reply may follow a saved decision. Never re-enable/replay blindly.
                        }
                        finally {
                            _permissionDecisionInFlight = false;
                            _permissions.IsEnabled = !_busy && !_shutdownRequested && !_refreshRequired;
                            if (!_shutdownRequested) await ReadPermissionsAsync();
                        }
                    });
                    if (choice == "allow_once") {
                        permissionAction.BorderBrush = PrimaryBrush;
                        permissionAction.Foreground = PrimaryBrush;
                        permissionAction.FontWeight = FontWeights.SemiBold;
                    }
                    choices.Children.Add(permissionAction);
                }
                panel.Children.Add(choices);
                _permissions.Children.Add(panel);
            }
            if (newlyPending)
            {
                _history.IsExpanded = false;
                _ = Dispatcher.BeginInvoke(System.Windows.Threading.DispatcherPriority.ContextIdle,
                    new Action(() => { if (StillCurrent()) _accessScroll.ScrollToTop(); }));
            }
        }
        catch (Exception ex)
        {
            if (!StillCurrent() || _permissionDecisionInFlight) return;
            _permissionRenderSequence++;
            _permissionRenderKey = "";
            _permissions.Children.Clear();
            _accessRequests.Header = "Agent access requests — could not verify";
            _permissionAttention.Content = "Agent access status unavailable · review details";
            _permissionAttention.Visibility = Visibility.Visible;
            _accessRequests.IsExpanded = true;
            _permissions.Children.Add(new TextBlock { Text = $"Access requests could not be verified: {ex.Message}\nNo access decision was made by this refresh.", TextWrapping = TextWrapping.Wrap });
        }
    }

    private string? PermissionRepository() => _investigationRepository
        ?? (_queue.SelectedItem as QueueProposal)?.Evidence["repository"]?.GetValue<string>();

    private static StackPanel PermissionPreview(JsonObject frame)
    {
        var panel = new StackPanel();
        var preview = frame["preview"]!.AsObject();
        void Field(string label, string text, bool command = false)
        {
            panel.Children.Add(new TextBlock { Text = label, FontWeight = FontWeights.SemiBold,
                Margin = new Thickness(0, 10, 0, 4) });
            var field = new TextBox { Text = NativeDisplayText.Escape(text), IsReadOnly = true,
                TextWrapping = TextWrapping.Wrap, MaxHeight = command ? 110 : 80,
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                FontFamily = new FontFamily(command ? "Consolas" : "Segoe UI") };
            field.PreviewMouseWheel += (_, args) => {
                if (field.Template?.FindName("PART_ContentHost", field) is not ScrollViewer inner
                    || field.Parent is not UIElement parent) return;
                var atBoundary = args.Delta > 0 ? inner.VerticalOffset <= 0
                    : inner.VerticalOffset >= inner.ScrollableHeight;
                if (!atBoundary) return;
                args.Handled = true;
                parent.RaiseEvent(new System.Windows.Input.MouseWheelEventArgs(args.MouseDevice, args.Timestamp, args.Delta) {
                    RoutedEvent = System.Windows.Input.Mouse.MouseWheelEvent
                });
            };
            panel.Children.Add(field);
        }
        if (preview["reason"] is JsonNode reason) Field("Agent's reason · not a safety guarantee", reason.GetValue<string>());
        if (preview["network"] is JsonObject network) Field("Network destination", $"{network["protocol"]}://{network["host"]}");
        if (preview["command"] is JsonNode command) Field("Exact command", command.GetValue<string>(), true);
        if (preview["cwd"] is JsonNode cwd) Field("Working directory", cwd.GetValue<string>());
        if (preview["additional_permissions"] is JsonObject extra) Field("Additional requested access", extra.ToJsonString());
        return panel;
    }

    private static string PermissionDescription(JsonObject frame)
    {
        var preview = frame["preview"]!.AsObject();
        var fields = new List<string>();
        if (preview["reason"] is JsonNode reason) fields.Add("Agent's reason (not a safety guarantee): " + reason.GetValue<string>());
        if (preview["command"] is JsonNode command) fields.Add("Exact command:\n" + command.GetValue<string>());
        if (preview["cwd"] is JsonNode cwd) fields.Add("Working directory: " + cwd.GetValue<string>());
        if (preview["network"] is JsonObject network) fields.Add($"Network destination: {network["protocol"]}://{network["host"]}");
        if (preview["additional_permissions"] is JsonObject extra) fields.Add("Additional requested access: " + extra.ToJsonString());
        return NativeDisplayText.Escape(string.Join("\n", fields));
    }

    private void ShowConversation((string Question, string Answer, string Source, string Freshness)? conversation, bool running)
    {
        _latestQuestion.Text = conversation?.Question ?? "";
        _latestReply.Text = conversation?.Answer ?? "";
        _replySource.Text = conversation?.Source ?? "";
        _replyFreshness.Text = conversation is null ? "" : running
            ? "Previous saved reply for this item. A new investigation is running; this is not its result or merge approval."
            : conversation.Value.Freshness;
        _latestAnswer.Visibility = conversation is null ? Visibility.Collapsed : Visibility.Visible;
    }

    private async Task ReadAgentRepliesAsync()
    {
        if (_shutdownRequested) return;
        var generation = ++_agentReadSequence;
        if (_refreshRequired || _queue.SelectedItem is not QueueProposal item) return;
        var repository = item.Evidence["repository"]!.GetValue<string>();
        if (!_supervisorDigests.TryGetValue(repository, out var expectedDigest)) return;
        var request = new JsonObject { ["schema_version"] = 1, ["request_id"] = "read_" + Guid.NewGuid().ToString("N"),
            ["operation"] = "supervisor_status", ["repository"] = repository };
        _agentReplies.Text = "Checking saved agent replies. No model call is being made…";
        _agentStatusUnverified = true;
        SetBusy(_busy);
        try
        {
            var response = await _client.SendAsync(request);
            var view = NativeSupervisorView.Parse(response, repository, expectedDigest);
            if (generation != _agentReadSequence
                || _refreshRequired || _queue.SelectedItem is not QueueProposal selected
                || selected.Id != item.Id || selected.Version != item.Version
                || !_supervisorDigests.TryGetValue(repository, out var currentDigest) || currentDigest != expectedDigest)
            {
                App.Record("supervisor_reply_discarded", new { proposal_id = item.Id, context_digest = expectedDigest });
                return;
            }
            _agentReplies.Text = view.Describe(item);
            _resourceBlocker.Text = view.ResourceGuidance ?? "";
            _resourceBlocker.Visibility = view.ResourceGuidance is null ? Visibility.Collapsed : Visibility.Visible;
            var conversation = view.LatestConversation(item);
            ShowConversation(conversation, view.HasActiveInvestigation || _investigationCancellation is not null);
            var recommendation = view.CurrentRecommendation(item);
            _currentAgentReady = recommendation?.Ready == true && _investigationCancellation is null;
            _savedInvestigationActive = view.HasActiveInvestigation || _investigationCancellation is not null;
            if (view.HasActiveInvestigation && view.ActiveJobId is string activeJob && _activityJobId != activeJob) {
                _activityJobId = activeJob;
                var baseline = view.ActiveStartedAt is DateTimeOffset started ? DateTimeOffset.UtcNow - started : TimeSpan.Zero;
                _activityBaseline = baseline > TimeSpan.Zero ? baseline : TimeSpan.Zero;
                _activityElapsed.Restart();
            }
            _agentStatusUnverified = false;
            _suggestInvestigation = view.SuggestsInvestigation(item);
            _supervisionDisabled = response["configured_launch"]?["mode"]?.GetValue<string>() == "off";
            _agentRecommendation.Text = recommendation?.Heading ?? "Needs investigation";
            _agentRecommendation.Foreground = _currentAgentReady && !SystemParameters.HighContrast
                ? new SolidColorBrush(Color.FromRgb(20, 100, 65)) : SystemColors.WindowTextBrush;
            _nextStep.Text = view.ActionGuidance(item, _executionEnabled);
            if (view.HasPreparedInvestigation && _investigationCancellation is null) {
                _agentRecommendation.Text = "Investigation has not started";
                _nextStep.Text = "Next: choose Investigate this item to start after fresh checks. A saved preparation is not a running agent or a result.";
            }
            if (view.ResourceGuidance is not null && !_currentAgentReady) {
                _agentRecommendation.Text = "Validation needs attention";
                _nextStep.Text = "Next: review storage and approve any limit change, or choose a smaller validation scope. Do not repeat the same blocked work unchanged.";
            }
            var followUp = item.Kind == "PR" && item.Step["action"]?.GetValue<string>() == "validate_then_merge"
                ? "After an approved merge: Watcher records the outcome and refreshes PR/alert evidence. Configured supervision reassesses the updated state. Selected local updates, when configured, need their own exact confirmation.\nNot included: publishing repairs, promoting to production or verifying production health."
                : "Investigation may identify tests, repairs or a related PR. It does not itself resolve this item or authorize changes.";
            var actionDetail = item.Kind == "PR" && item.Step["action"]?.GetValue<string>() == "validate_then_merge"
                ? $"Merge review: PR #{item.Identity["number"]} into {NativeDisplayText.Escape(item.Identity["expected_base"]?.GetValue<string>() ?? "the configured branch")}. The next screen checks the exact head and merge method. A separate confirmation performs the merge."
                : "This item cannot be merged directly. The investigation determines the required work.";
            _agentReason.Text = "Current assessment\n" + (recommendation?.Explanation ?? "No current accepted agent recommendation. Earlier replies are retained in conversation history.")
                + "\n\nAction scope\n" + actionDetail + "\n\nAfterward\n" + followUp;
            _selectedLaunchPolicy = view.LaunchPolicy;
            _retryJobId = view.RetryJobId;
            if (_retryJobId is not null)
            {
                _agentRecommendation.Text = "Investigation needs recovery";
                _nextStep.Text = view.RetryExplanation
                    ?? "Next: review the failure details and explicitly retry investigation. Completed merges and local updates will not be repeated.";
            }
            _launchPolicyRepository = repository; _launchPolicyDigest = expectedDigest;
            _launchPolicy.Text = view.LaunchPolicy?.Description ?? "This backend does not expose a verified investigation policy. Starting an agent is unavailable.";
            _agentIdentity.Text = view.LaunchPolicy is { Configured: true } policy
                ? "Agent: " + policy.AgentLabel
                : "No agent configured for this repository";
            _agentUsage.Text = view.LaunchPolicy?.UsageSummary ?? "Sending is unavailable.";
            if (view.LaunchPolicy?.Configured != true)
                _nextStep.Text = "Agent not configured. Review saved evidence.";
            if (_savedInvestigationActive)
            {
                _agentRecommendation.Text = "Investigation in progress";
                _nextStep.Text = "Wait for results, then refresh saved queue.";
                if (conversation is not null)
                    _replyFreshness.Text = "Previous saved reply for this item. A new investigation is running; this is not its result or merge approval.";
            }
            SetBusy(_busy);
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
                _agentIdentity.Text = "Agent configuration could not be verified";
                _agentUsage.Text = "Sending is unavailable until configuration is verified.";
                ResetAgentGuidance("Current agent recommendation could not be verified", preserveConversation: true);
                if (_latestAnswer.Visibility == Visibility.Visible)
                    _replyFreshness.Text = "Previous saved reply for this item. Current agent status could not be verified; this is not a new result or merge approval.";
                _agentStatusUnverified = true;
                SetBusy(_busy);
            }
        }
    }

    private void ResetAgentGuidance(string heading = "Checking status…", bool preserveConversation = false)
    {
        if (!preserveConversation) {
            _latestAnswer.Visibility = Visibility.Collapsed;
            _latestQuestion.Text = ""; _latestReply.Text = "";
            _replySource.Text = ""; _replyFreshness.Text = "";
        }
        _agentRecommendation.Text = heading;
        _agentRecommendation.Foreground = SystemColors.WindowTextBrush;
        _currentAgentReady = false;
        _suggestInvestigation = false;
        _supervisionDisabled = false;
        _savedInvestigationActive = false;
        _agentStatusUnverified = _queue.SelectedItem is QueueProposal;
        _agentReason.Text = "";
        _nextStep.Text = "";
    }

    private async Task InvestigateAsync()
    {
        if (!_investigate.IsEnabled || _queue.SelectedItem is not QueueProposal item) return;
        var repository = item.Evidence["repository"]!.GetValue<string>();
        var digest = _supervisorDigests[repository];
        if (_selectedLaunchPolicy?.Configured != true || _launchPolicyRepository != repository || _launchPolicyDigest != digest) return;
        var retryJobId = _retryJobId;
        if (retryJobId is not null && !NativeConfirmationDialog.Confirm(this, "Retry investigation",
            NativeDisplayText.Escape($"{repository}\n\nStart a fresh investigation attempt? It consumes the selected model's usage. The interrupted or failed attempt stays saved. Current evidence, workspace and permissions are checked again, and a compatible owned conversation is resumed when available.\n\nNo merge or local checkout update is approved or repeated."),
            "Retry investigation")) return;
        await RunInvestigationAsync(repository, digest, retryJobId, item.ItemIdentity, null);
    }

    private async Task<bool> RunInvestigationAsync(string repository, string digest, string? retryJobId,
        JsonObject? explicitItem, string? scheduledRunId, bool notify = true)
    {
        if (_shutdownRequested || _investigationCancellation is not null
            || (_scheduledRunning && scheduledRunId is null)) return false;
        var confirmed = false;
        // Chosen policy is displayed above this explicit start control. No UI
        // field can supply a model, workspace, account or broader permissions.
        var generation = ++_investigationGeneration;
        ResetActivityElapsed();
        _activityElapsed.Start();
        using var cancellation = new CancellationTokenSource();
        _investigationCancellation = cancellation;
        _agentReadSequence++;
        _retryJobId = null;
        if (_queue.SelectedItem is QueueProposal activeItem
            && string.Equals(activeItem.Evidence["repository"]?.GetValue<string>(), repository, StringComparison.OrdinalIgnoreCase))
        {
            _replyFreshness.Text = "Previous saved reply for this item. A new investigation is running; this is not its result or merge approval.";
            ResetAgentGuidance("Investigation running", preserveConversation: true);
            _nextStep.Text = "Waiting for agent results.";
        }
        _investigationFinished = new(TaskCreationOptions.RunContinuationsAsynchronously);
        _investigationRepository = repository;
        _ = ReadAgentRepliesAsync();
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
        if (scheduledRunId is not null) request["scheduled_run_id"] = scheduledRunId;
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
                        if (_actionInFlight)
                            _actionSummary.Text = NativeDisplayText.Escape(repository)
                                + "\nMerge complete. The post-merge check is waiting for an access decision in Agent access requests.";
                        _launchRefreshPending = true;
                        if (scheduledRunId is not null && notify) App.NotifyInvestigation("permission");
                        if (!_busy && !_launchRefreshInFlight) _ = RefreshInvestigationScopeAsync();
                    }
                    else if (frame.Event == "starting")
                        _launchProgress.Text = $"Checking fresh evidence and the configured workspace for {repository}…";
                    else if (frame.Event == "error")
                        _launchProgress.Text = "Investigation could not complete. Checking saved state; no retry will occur.";
                }));
            }, cancellation.Token);
            if (_shutdownRequested) return false;
            // A terminal stream frame is not a verified answer. Independently
            // read the saved queue and job receipt before presenting success.
            await WaitForIdleAsync();
            SetBusy(true);
            try
            {
                // Contention while reading a completed job is not failure of
                // that investigation. Retry only these observation requests;
                // never replay the launch, access decision or repository action.
                await NativeScopeRefresh.RunAsync(async () => {
                    Apply(await _client.SendAsync(Request("snapshot")));
                    if (!_supervisorDigests.TryGetValue(repository, out var currentDigest))
                        throw new InvalidDataException("The investigation repository is no longer in the saved queue scope.");
                    var saved = await _client.SendAsync(new JsonObject {
                        ["schema_version"] = 1, ["request_id"] = "read_" + Guid.NewGuid().ToString("N"),
                        ["operation"] = "supervisor_status", ["repository"] = repository
                    });
                    if (_shutdownRequested) return;
                    var view = NativeSupervisorView.Parse(saved, repository, currentDigest);
                    var verified = saved["jobs"]!.AsArray().Single(value => value!["job_id"]!.GetValue<string>() == terminal["job_id"]!.GetValue<string>());
                    confirmed = verified!["provider_completion_observed"]!.GetValue<bool>()
                        && verified["status"]!.GetValue<string>() is "completed" or "superseded";
                    _launchProgress.Text = view.DescribeTerminal(terminal);
                    App.Record("native_investigation_receipt_checked", new { repository, job_id = terminal["job_id"]!.GetValue<string>() });
                }, (exception, retry) => {
                    confirmed = false;
                    _launchProgress.Text = NativeDisplayText.Escape(
                        "The investigation ended, but its saved outcome could not yet be verified. "
                        + (retry ? "Retrying the read-only verification. " : "Refresh saved state to check its outcome. ")
                        + "No investigation or repository action is being retried.\n" + exception.Message);
                }, () => _shutdownRequested, duration => Task.Delay(duration));
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
            if (!_shutdownRequested)
            {
                await ReadAgentRepliesAsync();
                // An explicit send/continuation should reveal its answer. Quiet
                // refreshes never scroll the user's conversation or steal focus.
                if (!_shutdownRequested && explicitItem is not null && _queue.SelectedItem is QueueProposal selected
                    && selected.SameItem(explicitItem) && _latestAnswer.Visibility == Visibility.Visible)
                    _latestAnswer.BringIntoView();
            }
        }
        return confirmed && !_shutdownRequested;
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
                try
                {
                    var refreshed = await NativeScopeRefresh.RunAsync(
                        async () => Apply(await _client.SendAsync(Request("snapshot"))),
                        (exception, retry) => {
                            _refreshRequired = true;
                            _permissions.Children.Clear();
                            _accessRequests.Header = retry
                                ? "Agent access requests — checking current scope…"
                                : "Agent access requests — refresh required";
                            _launchProgress.Text = NativeDisplayText.Escape(
                                $"Current investigation scope could not be verified: {exception.Message}\n"
                                + (retry ? "Retrying the read-only scope check. Access grants remain unavailable."
                                    : "Access grants are unavailable until a successful refresh. Use Refresh saved queue."));
                        }, () => _shutdownRequested, duration => Task.Delay(duration));
                    if (refreshed && !_shutdownRequested && _investigationCancellation is not null)
                        _launchProgress.Text = "Investigation scope verified. Checking current agent access requests; no merge is approved.";
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
        var displayed = _reviews.OrderByDescending(review => DateTimeOffset.Parse(review.Value["created_at"]!.GetValue<string>())).ToList();
        var unresolved = _reviews.Count(NeedsOutcomeCheck);
        _history.Header = unresolved == 0 ? "Recent action results" : $"Saved action recovery — {unresolved} outcome(s) need checking";
        if (unresolved > 0) _history.IsExpanded = true;
        var latestResult = _reviews.Where(r => r.Status == "completed")
            .OrderBy(r => DateTimeOffset.Parse(r.Value["consented_at"]!.GetValue<string>())).LastOrDefault();
        var latestAction = _reviews.Where(r => r.Status is "completed" or "recovery_required")
            .OrderBy(r => DateTimeOffset.Parse(r.Value["consented_at"]!.GetValue<string>())).LastOrDefault();
        if (latestAction is not null && !_actionInFlight)
            ShowActionResult(latestAction, reveal: false);
        if (string.IsNullOrWhiteSpace(_historyStatus.Text) && latestResult is not null)
            _historyStatus.Text = $"Last saved result: {latestResult.Binding["repository"]} · PR #{latestResult.Binding["pull_request"]}\n{DescribeOutcome(latestResult)}";
        if (displayed.Count == 0) _activity.Children.Add(new TextBlock { Text = "No approved action has been recorded." });
        foreach (var group in displayed.GroupBy(review => (
            Repository: review.Binding["repository"]!.GetValue<string>().ToUpperInvariant(),
            Number: review.Binding["pull_request"]!.GetValue<int>(),
            Head: review.Binding["head_sha"]!.GetValue<string>())))
        {
            var primary = group.Where(NeedsOutcomeCheck).ToList();
            if (primary.Count == 0) primary.Add(group.FirstOrDefault(review => review.Status == "completed") ?? group.First());
            var rows = new StackPanel { Margin = new Thickness(0, 6, 0, 8) };
            foreach (var review in primary)
            {
                var resultText = new TextBlock { TextWrapping = TextWrapping.Wrap,
                    Text = $"{review.Binding["repository"]} · PR #{review.Binding["pull_request"]}\n{DescribeOutcome(review)}" };
                rows.Children.Add(resultText);
                AddCopyIcon(resultText);
                if (NeedsOutcomeCheck(review))
                    rows.Children.Add(Button("Check this saved action outcome", async () => await CheckReviewOutcomeAsync(review)));
            }
            var historical = group.Except(primary).ToList();
            if (historical.Count != 0)
            {
                var historyText = new TextBlock { TextWrapping = TextWrapping.Wrap, Text = string.Join("\n\n", historical.Select(review =>
                    $"{review.Value["created_at"]} · {DescribeOutcome(review)}")) };
                rows.Children.Add(new Expander { Header = $"Earlier reviews and refreshes · {historical.Count}",
                    Content = historyText });
                AddCopyIcon(historyText);
            }
            _activity.Children.Add(rows);
        }
    }

    private static bool NeedsOutcomeCheck(NativeActionReview review) => review.Status == "recovery_required"
        || review.Value["outcome_recorded"]?.GetValue<bool>() == false
        || (review.Status == "completed" && review.Value["completion_stage"]?.GetValue<string>() == "merge_outcome");

    private void ShowActionNotice(string headline, string summary, bool reveal = true)
    {
        if (reveal) _workspaces.SelectedItem = _maintenanceWorkspace;
        _actionHeadline.Text = NativeDisplayText.Escape(headline);
        _actionSummary.Text = NativeDisplayText.Escape(summary);
        _actionNotice.Visibility = Visibility.Visible;
        var success = headline.EndsWith("merged successfully", StringComparison.Ordinal)
            || headline is "Maintenance action complete (saved)" or "Remote maintenance complete (saved)" or "Selected local checkout updated" or "Local update verified";
        _actionNotice.Background = SystemParameters.HighContrast ? SystemColors.ControlBrush
            : new SolidColorBrush(success ? Color.FromRgb(220, 242, 228) : Color.FromRgb(226, 232, 240));
    }

    private void ShowActionResult(NativeActionReview review, bool reveal = true)
    {
        var target = $"{review.Binding["repository"]} · PR #{review.Binding["pull_request"]}";
        var merged = review.Status == "completed"
            && review.Value["result"]?["status"]?.GetValue<string>() == "merged";
        if (review.RecordedMaintenanceSummary is string completion)
        {
            ShowActionNotice("Remote maintenance complete (saved)", target
                + "\n" + completion
                + "\nNo further merge will start automatically.", reveal);
            return;
        }
        ShowActionNotice(merged ? $"PR #{review.Binding["pull_request"]} merged successfully"
            : "Action outcome needs review", target + "\n" + (merged
                ? $"Merged into {review.Binding["expected_base"]}. "
                    + (review.Value["outcome_recorded"]?.GetValue<bool>() == false
                        ? "Outcome needs recovery; check saved action results."
                        : "Outcome saved. No further merge will start automatically.")
                : DescribeOutcome(review)), reveal);
    }

    private void StoreReview(NativeActionReview review)
    {
        _reviews.RemoveAll(r => r.Id == review.Id);
        _reviews.Add(review); RenderActivity(); RenderCompletion();
    }

    private async Task PrepareAsync()
    {
        if (_shutdownRequested || _busy || _investigationCancellation is not null || _refreshRequired || !_executionEnabled || _queue.SelectedItem is not QueueProposal item) return;
        var request = Request("prepare_action"); request["proposal_id"] = item.Id; request["version"] = item.Version;
        _prepared = null; _confirmation.Visibility = Visibility.Collapsed;
        SetBusy(true); _outcome.Text = "Checking merge readiness…\nRechecking the exact PR, current policy and required checks. Nothing is being merged.";
        _ = Dispatcher.BeginInvoke(new Action(() => _outcomeSurface.BringIntoView()));
        try
        {
            var response = await _client.SendAsync(request);
            var review = NativeActionReview.Parse(response["review"]);
            review.RequireSelection(item);
            if (review.Status == "already_applied") { _outcome.Text = DescribeOutcome(review); return; }
            if (review.Status != "awaiting_confirmation") throw new InvalidDataException("No confirmable action was returned.");
            _prepared = review; StoreReview(review);
            var mergeVerb = review.Binding["merge_method"]!.GetValue<string>() switch {
                "squash" => "Squash-merge", "rebase" => "Rebase-merge", _ => "Merge"
            };
            _confirmationText.Text = NativeDisplayText.Escape(review.Describe() + $"\nConfirmation expires at {DateTimeOffset.Parse(review.Value["expires_at"]!.GetValue<string>()).ToLocalTime():g}.\n\nWhen you confirm:\n1. Recheck that the approval, PR and agent evidence are still valid.\n2. {mergeVerb} only #{review.Binding["pull_request"]} into {review.Binding["expected_base"]}.\n3. Record the outcome and refresh PRs and alerts.\n\nWith AI enabled, a successful complete refresh is followed by an agent check using the selected model allowance. Blocked checks are reported here. This does not authorize another merge or update your local checkout.");
            _confirmation.Visibility = Visibility.Visible;
            _decisions.Visibility = Visibility.Collapsed;
            _nextStep.Text = "Next: confirm this exact merge, or cancel the review. No merge has happened yet.";
            _outcome.Text = "Ready for confirmation. Nothing has been approved or merged.";
            _ = Dispatcher.BeginInvoke(new Action(() => _confirmation.BringIntoView()));
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
        _decisions.Visibility = Visibility.Visible;
        _actionInFlight = true; SetBusy(true);
        ShowActionNotice($"Merging PR #{item.Identity["number"]}…", "Rechecking your exact approval and GitHub state. Please wait; do not confirm again.");
        _outcome.Text = "Your exact approval was submitted. Watcher is rechecking and executing this one action. You can hide the window; it will not retry automatically.";
        try
        {
            var response = await _client.SendAsync(request);
            var review = NativeActionReview.Parse(response["review"]);
            review.RequireSelection(item);
            if (review.Id != reviewId) throw new InvalidDataException("Outcome returned for a different review.");
            StoreReview(review); _outcome.Text = DescribeOutcome(review);
            ShowActionResult(review);
            _check.Visibility = review.Status == "recovery_required" || review.Value["outcome_recorded"]?.GetValue<bool>() == false
                ? Visibility.Visible : Visibility.Collapsed;
            // Preserve the exact result independently of the next selection,
            // then collect a new deterministic queue without model calls.
            _prepare.IsEnabled = false;
            if (review.Status == "completed")
            {
                _historyStatus.Text = $"{review.Binding["repository"]} · PR #{review.Binding["pull_request"]}\n{DescribeOutcome(review)}";
                if (!_shutdownRequested)
                {
                    await CollectCoreAsync();
                    ShowActionResult(review);
                    _actionSummary.Text += _refreshRequired
                        ? "\nPost-merge assessment not started: queue refresh failed."
                        : !_evidenceComplete ? "\nPost-merge assessment not started: refreshed evidence is incomplete."
                        : "\nQueue refreshed. This merge is complete.";
                    _detailsScroll.ScrollToTop();
                    if (!_refreshRequired && _evidenceComplete && review.Value["outcome_recorded"]?.GetValue<bool>() != false
                        && review.Value["result"]?["status"]?.GetValue<string>() == "merged" && review.CanAssessCompletion)
                        await ContinueAfterMergeAsync(review);
                    else if (review.HasPendingCompletion && !review.CanAssessCompletion)
                        _actionSummary.Text += "\nThe exact merge revision needs outcome recovery before an agent assessment can start.";
                    else if (review.Value["outcome_recorded"]?.GetValue<bool>() == false)
                        _actionSummary.Text += "\nPost-merge assessment not started: the action outcome needs recovery.";
                }
            }
        }
        catch (Exception ex)
        {
            _refreshRequired = true;
            _summary.Text = "Approval outcome needs checking. No automatic retry will occur.";
            ShowActionNotice("Merge outcome needs checking", "Do not approve again. Check the saved action outcome; nothing will be retried automatically.");
            _outcome.Text = $"The reply could not be verified: {ex.Message}\nRefresh saved state to check the outcome; do not approve again.";
        }
        finally
        {
            _actionInFlight = false; SetBusy(false);
        }
    }

    private async Task ContinueAfterMergeAsync(NativeActionReview review, bool allowRecovery = false)
    {
        var repository = review.Binding["repository"]!.GetValue<string>();
        var merged = review.Value["result"]?["status"]?.GetValue<string>() == "already_applied"
            ? $"PR #{review.Binding["pull_request"]} already confirmed merged — no new merge performed"
            : $"PR #{review.Binding["pull_request"]} merged successfully";
        var scope = $"{repository} · PR #{review.Binding["pull_request"]}\n";
        // Only a just-confirmed, recorded merge enters here. Reopening history
        // never replays this continuation; the backend owns job deduplication,
        // policy checks, workspace freshness and deliberate failed-job recovery.
        try
        {
            if (_shutdownRequested) return;
            if (!review.CanAssessCompletion)
                throw new InvalidDataException("The exact saved maintenance action and merge revision must be recovered before an assessment starts.");
            if (!_supervisorDigests.TryGetValue(repository, out var digest))
                throw new InvalidDataException("The refreshed repository context is unavailable.");
            var status = await _client.SendAsync(new JsonObject {
                ["schema_version"] = 1, ["request_id"] = "read_" + Guid.NewGuid().ToString("N"),
                ["operation"] = "supervisor_status", ["repository"] = repository
            });
            var before = NativeSupervisorView.Parse(status, repository, digest);
            if (status["configured_launch"]?["mode"]?.GetValue<string>() == "off")
            {
                ShowActionNotice(merged, scope + "Queue refreshed. Automatic agent assessment is disabled in this profile.");
                await CheckCompletionCoreAsync(review);
                return;
            }
            if (before.LaunchPolicy?.Configured != true)
                throw new InvalidDataException("The configured agent is unavailable. Review its configuration; the recorded merge is unchanged.");
            if (before.HasActiveInvestigation)
            {
                ShowActionNotice(merged, scope + "Post-merge investigation is already in progress. Review activity and access requests; no second investigation was started.");
                return;
            }
            if (before.RetryJobId is not null)
            {
                if (!allowRecovery)
                    throw new InvalidDataException("The previous investigation needs explicit recovery. Use Continue post-merge assessment to review a retry; no automatic retry was started.");
                if (!NativeConfirmationDialog.Confirm(this, "Retry post-merge assessment",
                    NativeDisplayText.Escape(scope + "The previous attempt did not produce an accepted answer. Retry uses the configured model allowance and rechecks current evidence, workspace and permissions. A compatible owned conversation is resumed when available. The failed record remains.\n\nThis does not repeat the merge, authorize another merge or update a local checkout."), "Retry assessment"))
                {
                    _completionStatus.Text = "Assessment retry cancelled. The recorded merge and local outcomes are unchanged.";
                    return;
                }
            }
            ShowActionNotice(merged, scope + "Checking updated PRs, alerts and next steps with your configured agent… No additional merge is authorized.");
            var confirmed = await RunInvestigationAsync(repository, digest, before.RetryJobId, null, null);
            if (_shutdownRequested) return;
            if (!confirmed || !_supervisorDigests.TryGetValue(repository, out var currentDigest))
                throw new InvalidDataException("The post-merge assessment was not confirmed.");
            var saved = await _client.SendAsync(new JsonObject {
                ["schema_version"] = 1, ["request_id"] = "read_" + Guid.NewGuid().ToString("N"),
                ["operation"] = "supervisor_status", ["repository"] = repository
            });
            var after = NativeSupervisorView.Parse(saved, repository, currentDigest);
            var summary = after.CurrentAssessmentSummary
                ?? throw new InvalidDataException("No accepted assessment covers the refreshed repository state.");
            _historyStatus.Text += "\n\n" + NativeDisplayText.Escape(scope) + "Post-merge agent assessment:\n" + summary;
            ShowActionNotice(merged, scope + "Agent checked the updated state.\n"
                + (summary.Length <= 240 ? summary : "Full assessment is saved under Recent action results."));
            _detailsScroll.ScrollToTop();
            await CheckCompletionCoreAsync(review);
        }
        catch (Exception exception)
        {
            if (!_shutdownRequested)
                ShowActionNotice(merged, scope + "Post-merge assessment could not finish. The merge remains complete.\n"
                    + exception.Message + "\nNo additional merge or local checkout update was performed by Watcher.");
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
        if (MaintenanceOperationBlocked || review.Id is not string reviewId) return;
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
            ShowActionResult(checkedReview);
            if ((_queue.SelectedItem as QueueProposal)?.Id == checkedReview.ProposalId)
            {
                _outcome.Text = DescribeOutcome(checkedReview);
                _check.Visibility = checkedReview.Status == "recovery_required" ? Visibility.Visible : Visibility.Collapsed;
            }
        }
        catch (Exception ex) { _historyStatus.Text = $"Cannot confirm {target}'s outcome yet: {ex.Message}\nNo action was retried."; }
        finally { SetBusy(false); }
    }

    private void UpdateMessageStatus()
    {
        var saved = (_queue.SelectedItem as QueueProposal)?.Evidence["feedback"]?.AsArray().Count ?? 0;
        var recipient = (_queue.SelectedItem as QueueProposal)?.Label;
        _messageStatus.Text = _message.Text.Length > 0
            ? $"To: {recipient ?? "no recipient selected"}\nDraft — not sent. Sending does not approve a merge."
            : saved > 0 ? $"{saved} saved messages · Replies below"
            : "Optional: ask a question or leave a direction.";
        UpdateInvestigationControls();
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
                ? "\nQueue refreshed. No merge was started by this refresh."
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
        if (MaintenanceOperationBlocked) return;
        SetBusy(true);
        try { await CollectCoreAsync(); if (!_refreshRequired) await RecheckPendingCompletionsAsync(); }
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
            _outcome.Text = "Saved queue could not be refreshed. Previous evidence is stale; action and access decisions are disabled. Use Refresh saved queue to retry.";
            _detail.Text += "\n\nLatest refresh diagnostic\n" + NativeDisplayText.Escape(ex.Message);
            App.Record("backend_queue_error", new { operation = "snapshot" });
        }
        finally {
            await ReadLocalSyncTargetsAsync();
            if (!_refreshRequired && !_shutdownRequested && !InvestigationOwnsMaintenance)
                await RecheckPendingCompletionsAsync();
            SetBusy(false);
        }
    }

    private Task StartItemInvestigationAsync() => _retryJobId is not null
        ? InvestigateAsync() : SaveAndStartAgentAsync(investigateItem: true);

    private Task SendToAgentAsync() => SaveAndStartAgentAsync(investigateItem: false);

    private async Task SaveAndStartAgentAsync(bool investigateItem)
    {
        if (!(investigateItem ? _investigate.IsEnabled : _send.IsEnabled) || _queue.SelectedItem is not QueueProposal selected) return;
        var saved = false;
        try
        {
            saved = await SaveDecisionAsync("feedback", holdBusy: true,
                messageOverride: investigateItem ? selected.InvestigationRequest : null);
            if (!saved || _shutdownRequested) return;
            // Keep selection and other controls locked while verifying the
            // post-save policy/digest. A saved message is not a launch receipt.
            await ReadAgentRepliesAsync();
            if (_queue.SelectedItem is not QueueProposal current || current.Id != selected.Id)
            {
                _outcome.Text = "Your message was saved. The recommendation changed; check the selected item before sending it to the agent.";
                return;
            }
        }
        finally { SetBusy(false); }
        if (saved && _investigate.IsEnabled) await InvestigateAsync();
        else if (saved && !_shutdownRequested)
            _outcome.Text = "Your message was saved, but the agent could not start under the verified configuration. No model call was made by this send.";
    }

    private async Task DecideAsync(string operation) => await SaveDecisionAsync(operation);

    private async Task<bool> SaveDecisionAsync(string operation, bool holdBusy = false, string? messageOverride = null)
    {
        if (MaintenanceOperationBlocked || _refreshRequired
            || _queue.SelectedItem is not QueueProposal item) return false;
        var message = messageOverride ?? _message.Text;
        if (operation == "feedback" && (string.IsNullOrWhiteSpace(message) || Encoding.UTF8.GetByteCount(message) > 8192))
        { _outcome.Text = "Enter a message of at most 8192 UTF-8 bytes."; return false; }
        var request = Request(operation);
        request["proposal_id"] = item.Id; request["version"] = item.Version;
        if (operation == "feedback") request["message"] = message;
        if (operation == "defer") request["seconds"] = 86400;
        SetBusy(true); _outcome.Text = "Saving the local decision…";
        try
        {
            var response = await _client.SendAsync(request);
            Apply(response);
            if (operation == "feedback" && messageOverride is null) { _drafts.Remove(item.Id); SelectProposal(); }
            _outcome.Text = operation == "feedback" ? "Message saved locally. Saving did not launch an agent; verified replies appear below when available."
                : "Local decision saved. No PR or security alert was changed.";
            App.Record("backend_queue_decision", new { operation, replayed = response["replayed"]?.GetValue<bool>() });
            return true;
        }
        catch (Exception ex)
        {
            // A lost reply can follow a successful save. Never reapply blindly.
            _refreshRequired = true;
            _summary.Text = "The last save was not confirmed. Refresh to check what was actually saved before deciding again.";
            _outcome.Text = $"Save was not confirmed: {ex.Message}\nRefresh to inspect persisted state before trying another decision.";
            App.Record("backend_queue_error", new { operation });
            return false;
        }
        finally { if (!holdBusy) SetBusy(false); }
    }
}
