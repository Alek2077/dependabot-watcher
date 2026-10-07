using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Threading;
using Microsoft.Windows.AppLifecycle;
using Microsoft.Windows.AppNotifications;
using Microsoft.Windows.AppNotifications.Builder;
using Forms = System.Windows.Forms;
using Windows.ApplicationModel.Activation;

namespace WatcherNativeProof;

public partial class App : System.Windows.Application
{
    private AppInstance? _instance;
    private Window? _window;
    private Forms.NotifyIcon? _tray;
    private bool _registered;
    private bool _trayExplained;
    private string? _backendLaunchFile;
    private BackendClient? _normalClient;
    private string? _stateRoot;
    private bool _diagnosticMode;
    private bool _diagnosticWindow;
    private volatile bool _notificationReady;
    private System.Threading.Timer? _notificationDeadline;
    private readonly object _notificationDeadlineGate = new();
    private long _notificationExpiresAt;
    private bool _notificationCompleted;
    private readonly HashSet<string> _scheduledRuns = new(StringComparer.Ordinal);
    private Task _scheduledTail = Task.CompletedTask;
    internal bool IsQuitting { get; private set; }
    internal static string Root => (Current as App)?._stateRoot
        ?? ((Current as App)?._diagnosticMode == true ? AppContext.BaseDirectory
            : throw new InvalidOperationException("No private review profile is bound."));

    internal static void Record(string kind, object? details = null)
    {
        // Before ordinary profile validation there is no trusted event sink.
        // Do not put private errors beside installed binaries or follow paths
        // from a rejected manifest. Explicit diagnostics own their legacy sink.
        if (Current is not App app || (app._stateRoot is null && !app._diagnosticMode)) return;
        File.AppendAllText(Path.Combine(Root, $"events.{Environment.ProcessId}.jsonl"),
            JsonSerializer.Serialize(new { at = DateTimeOffset.UtcNow, kind, details }) + "\n");
    }

    protected override async void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        var activationCandidate = e.Args.Contains("----AppNotificationActivated:");
        var startupStage = "arguments";
        DispatcherUnhandledException += (_, args) =>
        {
            args.Handled = true;
            // A UI fault must not abandon independent backend operations or
            // kill an already-approved action. Even logging may fail (disk
            // full); cleanup must still enter the same owned drain path.
            try { Record("unhandled_error", args.Exception.ToString()); } catch { }
            RequestQuit(1);
        };
        try
        {
            var launchIndex = Array.IndexOf(e.Args, "--launch-file");
            var notificationMode = launchIndex == 0 && e.Args.Length >= 3 ? e.Args[2] : null;
            var scheduledRunId = notificationMode == "--scheduled-run" && (e.Args.Length == 4 || (e.Args.Length == 6
                && e.Args[4] == "--notify-mode" && e.Args[5] is "off" or "desktop"))
                && System.Text.RegularExpressions.Regex.IsMatch(e.Args[3], "\\Arun_[a-f0-9]{20}\\z") ? e.Args[3] : null;
            var scheduledNotify = e.Args.Length != 6 || e.Args[5] == "desktop";
            if (scheduledRunId is not null) notificationMode = null;
            var coldActivation = NativeNotifications.IsColdActivation(e.Args);
            _diagnosticWindow = e.Args.Length == 1 && e.Args[0] == "--diagnostic-window";
            _diagnosticMode = _diagnosticWindow || e.Args.Any(arg => arg is "--launch-roundtrip-probe"
                or "--launch-stream-probe" or "--permission-probe" or "--supervisor-probe"
                or "--bridge-probe" or "--child-probe" or "--cleanup" or "--notify" or "--status");
            if (launchIndex >= 0 && !_diagnosticMode)
            {
                if (launchIndex != 0 || !(e.Args.Length == 2
                    || (e.Args.Length == 3 && notificationMode is "--notification-test" or "--notification-status" or "--notification-remove")
                    || (e.Args.Length == 4 && notificationMode == "--notification-run")
                    || scheduledRunId is not null))
                    throw new ArgumentException("Open the configured review with one trusted launch file.");
                if (notificationMode is not null)
                {
                    // This process performs only a bounded notification operation.
                    // A lost outer caller must not leave an indefinite SDK sender.
                    // Exit only this process, never an independently opened queue.
                    _notificationExpiresAt = Environment.TickCount64 + 50_000;
                    _notificationDeadline = new System.Threading.Timer(_ => NotificationDeadlineElapsed(), null,
                        TimeSpan.FromSeconds(50), System.Threading.Timeout.InfiniteTimeSpan);
                }
                BindLaunch(e.Args[1]);
                if (notificationMode is "--notification-status" or "--notification-remove")
                    _ = NativeNotificationProfiles.Read(NativeNotificationProfiles.Key(_backendLaunchFile!));
                else NativeNotificationProfiles.Register(_backendLaunchFile!);
            }
            if (e.Args.Contains("--launch-roundtrip-probe"))
            {
                var file = Array.IndexOf(e.Args, "--launch-file");
                var testCase = Array.IndexOf(e.Args, "--case");
                if (file < 0 || file + 1 >= e.Args.Length || testCase < 0 || testCase + 1 >= e.Args.Length)
                    throw new ArgumentException("The launch roundtrip requires its owned launch file and controlled case.");
                await NativeLaunchRoundtripProbe.RunAsync(e.Args[file + 1], e.Args[testCase + 1]);
                Shutdown();
                return;
            }
            if (e.Args.Contains("--launch-stream-probe"))
            {
                await NativeLaunchProbe.RunAsync();
                Shutdown();
                return;
            }
            if (e.Args.Contains("--permission-probe"))
            {
                NativePermissionProbe.Run();
                Shutdown();
                return;
            }
            if (e.Args.Contains("--supervisor-probe"))
            {
                var index = Array.IndexOf(e.Args, "--launch-file");
                if (index < 0 || index + 1 >= e.Args.Length)
                    throw new ArgumentException("The agent reply proof requires its launch file.");
                await NativeSupervisorProbe.RunAsync(e.Args[index + 1]);
                Shutdown();
                return;
            }
            if (e.Args.Contains("--bridge-probe"))
            {
                var index = Array.IndexOf(e.Args, "--launch-file");
                if (index < 0 || index + 1 >= e.Args.Length)
                    throw new ArgumentException("The backend proof requires its launch file.");
                await BackendClient.ProbeAsync(e.Args[index + 1]);
                Shutdown();
                return;
            }
            if (e.Args.Contains("--child-probe"))
            {
                await ProbeHiddenChild();
                Shutdown();
                return;
            }
            if (_normalClient is null && !_diagnosticWindow && !coldActivation
                && !e.Args.Any(arg => arg is "--cleanup" or "--notify" or "--status"))
                throw new InvalidDataException("Open the configured review through dependabot-watcher review.");
            AppNotificationManager.Default.NotificationInvoked += (_, args) =>
            {
                // Check when raised, not later when the dispatcher runs: the
                // initial COM callback belongs to GetActivatedEventArgs below.
                if (!_notificationReady) return;
                Dispatcher.BeginInvoke(() =>
                {
                    // Initial COM activation is handled from GetActivatedEventArgs
                    // after Register. Later clicks can reach any live SDK instance.
                    if (_notificationReady) RouteNotification(args);
                });
            };
            startupStage = "sdk-register";
            AppNotificationManager.Default.Register();
            _registered = true;
            // First notification activation is returned here; subsequent clicks
            // on a running instance are delivered through NotificationInvoked.
            startupStage = "sdk-activation";
            var activationArgs = AppInstance.GetCurrent().GetActivatedEventArgs();
            if (coldActivation)
            {
                if (activationArgs.Kind != ExtendedActivationKind.AppNotification
                    || activationArgs.Data is not AppNotificationActivatedEventArgs notice || notice.UserInput.Count != 0)
                    throw new InvalidDataException("A registered notification activation is required.");
                startupStage = "profile-lookup";
                var saved = NativeNotificationProfiles.Read(NativeNotifications.ActivationKey(notice.Argument));
                startupStage = "profile-bind";
                BindLaunch(saved.File);
            }
            if (notificationMode is not null)
            {
                var profileKey = NativeNotificationProfiles.Key(_backendLaunchFile!);
                _notificationReady = true; // A click during publication must also reach its queue.
                var receipt = notificationMode switch {
                    "--notification-test" => await NativeNotifications.PublishAsync(_normalClient!, profileKey, null, test: true),
                    "--notification-run" => await NativeNotifications.PublishAsync(_normalClient!, profileKey, e.Args[3], test: false),
                    "--notification-status" => await NativeNotifications.StatusAsync(profileKey),
                    "--notification-remove" => await NativeNotifications.RemoveAsync(_backendLaunchFile!),
                    _ => throw new InvalidDataException("Unsupported notification operation.")
                };
                // A completed publication must not turn into a failed delivery
                // if SDK shutdown stalls. Re-arm a successful publisher-only
                // exit; a queued old timer callback checks the new deadline.
                lock (_notificationDeadlineGate)
                {
                    _notificationCompleted = true;
                    _notificationExpiresAt = Environment.TickCount64 + 10_000;
                    _notificationDeadline!.Change(TimeSpan.FromSeconds(10), System.Threading.Timeout.InfiniteTimeSpan);
                }
                Console.Out.WriteLine(receipt.ToJsonString());
                Shutdown();
                return;
            }
            if (e.Args.Contains("--cleanup"))
            {
                AppNotificationManager.Default.UnregisterAll();
                _registered = false;
                Record("registration_removed");
                Shutdown();
                return;
            }
            if (e.Args.Contains("--notify"))
            {
                SendNotice();
                Shutdown();
                return;
            }
            if (e.Args.Contains("--status"))
            {
                var notices = await AppNotificationManager.Default.GetAllAsync();
                File.WriteAllText(Path.Combine(Root, "notification-status.json"),
                    JsonSerializer.Serialize(new {
                        setting = AppNotificationManager.Default.Setting.ToString(),
                        notificationIds = notices.Select(n => n.Id).ToArray()
                    }));
                Shutdown();
                return;
            }
            startupStage = "owner-routing";
            var key = "WatcherNativeProof-" + Convert.ToHexString(
                SHA256.HashData(Encoding.UTF8.GetBytes(Root)))[..16];
            _instance = AppInstance.FindOrRegisterForKey(key);
            _instance.Activated += (_, incoming) => Dispatcher.BeginInvoke(
                () => RouteOwnerActivation(incoming));
            if (!_instance.IsCurrent)
            {
                await _instance.RedirectActivationToAsync(activationArgs);
                Shutdown();
                return;
            }
            var notificationLaunch = activationArgs.Kind == ExtendedActivationKind.AppNotification;
            _notificationReady = true;
            // The toast has only one operation: open the aggregate review queue.
            // Display that read-only window after SDK startup; the notification
            // payload is not accepted as action authority.
            // Decisions remain deliberate interactions inside the window.
            startupStage = "queue-open";
            if (scheduledRunId is not null) QueueScheduledRun(scheduledRunId, scheduledNotify);
            else OpenQueue(notificationLaunch ? "notification_open" : "normal_open");
        }
        catch (Exception ex)
        {
            if (activationCandidate)
                try { NativeNotificationProfiles.RecordActivationFailure(startupStage, ex.HResult, e.Args.Length); } catch { }
            try { Record("startup_error", ex.ToString()); } catch { }
            RequestQuit(1);
        }
    }

    private void RouteOwnerActivation(AppActivationArguments activation)
    {
        try
        {
            if (_diagnosticWindow) { OpenQueue("existing_diagnostic_activation"); return; }
            if (activation.Kind == ExtendedActivationKind.AppNotification
                && activation.Data is AppNotificationActivatedEventArgs notice) { RouteNotification(notice); return; }
            if (activation.Kind != ExtendedActivationKind.Launch || activation.Data is not ILaunchActivatedEventArgs launch)
                throw new InvalidDataException("Unsupported owner activation.");
            var scheduled = NativeScheduledActivation.Read(launch.Arguments);
            if (scheduled is null) { OpenQueue("existing_instance_activation"); return; }
            var selected = NativeReviewLaunch.Read(scheduled.Value.File);
            if (!string.Equals(selected.Directory, _stateRoot, StringComparison.Ordinal))
                throw new InvalidDataException("Scheduled activation belongs to another profile.");
            QueueScheduledRun(scheduled.Value.RunId, scheduled.Value.Notify);
        }
        catch { try { Record("owner_activation_refused"); } catch { } }
    }

    private void QueueScheduledRun(string runId, bool notify)
    {
        if (IsQuitting || !_scheduledRuns.Add(runId)) return;
        _scheduledTail = RunScheduledAfterAsync(_scheduledTail, runId, notify);
    }

    private async Task RunScheduledAfterAsync(Task previous, string runId, bool notify)
    {
        await previous;
        previous = Task.CompletedTask; // Do not retain a chain of finished runs.
        if (IsQuitting) return;
        try
        {
            var result = await EnsureQueue().RunScheduledAsync(runId, notify);
            try { Record("scheduled_investigation_checked", new { run_id = runId, outcome = result }); } catch { }
            if (result != "quiet" && !IsQuitting && notify) NotifyInvestigation(result);
        }
        catch
        {
            try { Record("scheduled_investigation_unconfirmed", new { run_id = runId }); } catch { }
            if (!IsQuitting && notify) NotifyInvestigation("attention");
        }
        finally { _scheduledRuns.Remove(runId); }
    }

    internal static void NotifyInvestigation(string kind)
    {
        if (Current is not App app || app._backendLaunchFile is null || app.IsQuitting
            || app._window is BackendQueueWindow { IsStopping: true }) return;
        try { NativeNotifications.PublishInvestigation(NativeNotificationProfiles.Key(app._backendLaunchFile), kind); }
        catch { try { Record("agent_notification_unavailable"); } catch { } }
    }

    private void NotificationDeadlineElapsed()
    {
        lock (_notificationDeadlineGate)
        {
            if (Environment.TickCount64 < _notificationExpiresAt) return;
            Environment.Exit(_notificationCompleted ? 0 : 1);
        }
    }

    private void BindLaunch(string file)
    {
        // Both CLI entry and cold lookup must pass the same immutable command,
        // component fingerprint and private profile-byte validation.
        _backendLaunchFile = Path.GetFullPath(file);
        var selected = NativeReviewLaunch.Read(_backendLaunchFile);
        _stateRoot = selected.Directory;
        _normalClient = selected.Client;
    }

    private void RouteNotification(AppNotificationActivatedEventArgs args)
    {
        try
        {
            if (_diagnosticWindow && args.Argument == "action=open_synthetic_queue")
            {
                if (_instance?.IsCurrent == true) OpenQueue("diagnostic_notification_click");
                return;
            }
            if (args.UserInput.Count != 0) throw new InvalidDataException("Notification inputs are not accepted.");
            var saved = NativeNotificationProfiles.Read(NativeNotifications.ActivationKey(args.Argument));
            if (_instance?.IsCurrent == true && string.Equals(saved.Launch.Directory, _stateRoot, StringComparison.Ordinal))
            {
                OpenQueue("notification_click");
                return;
            }
            // A click for a different profile must not switch a live window's
            // backend. Normal profile ownership redirects to its existing queue.
            var start = new ProcessStartInfo {
                FileName = Environment.ProcessPath!, UseShellExecute = true
            };
            // A desktop launch does not share the sender's redirected pipes.
            // The selected executable/typed arguments are not a shell command.
            start.ArgumentList.Add("--launch-file");
            start.ArgumentList.Add(saved.File);
            using var process = Process.Start(start) ?? throw new IOException("The review queue could not be opened.");
        }
        catch
        {
            // Stale/unknown clicks grant nothing and must not close a different
            // profile's live investigation or approved action.
            try { Record("notification_activation_refused"); } catch { }
        }
    }

    private void OpenQueue(string reason)
    {
        EnsureQueue();
        _window!.Show();
        if (_window.WindowState == WindowState.Minimized) _window.WindowState = WindowState.Normal;
        var activated = _window.Activate();
        Record("queue_opened", new { reason, pid = Environment.ProcessId, activated, active = _window.IsActive });
    }

    private BackendQueueWindow EnsureQueue()
    {
        if (_window is null)
        {
            var client = _normalClient;
            if (client is null)
            {
                if (!_diagnosticWindow)
                    throw new InvalidDataException("No normal review profile is bound; diagnostic transport needs an explicit diagnostic entry.");
                var launchFile = Path.Combine(Root, "backend-launch.json");
                if (!File.Exists(launchFile))
                    throw new InvalidDataException("No review profile is configured. Open Watcher through dependabot-watcher review.");
                client = BackendClient.FromLaunchFile(launchFile);
            }
            _window = new BackendQueueWindow(client);
            MainWindow = _window;
            _tray = new Forms.NotifyIcon {
                Icon = System.Drawing.SystemIcons.Information,
                Text = "Watcher development review queue", Visible = true
            };
            var menu = new Forms.ContextMenuStrip();
            menu.Items.Add("Open review", null, (_, _) => OpenQueue("tray_open"));
            // Normal cold-notification/profile activation is a separate gate;
            // do not offer the synthetic notification in an ordinary profile.
            if (_backendLaunchFile is null)
                menu.Items.Add("Send test notification", null, (_, _) => SendNotice());
            menu.Items.Add("Exit", null, (_, _) => RequestQuit());
            _tray.ContextMenuStrip = menu;
            _tray.DoubleClick += (_, _) => OpenQueue("tray_open");
        }
        return _window as BackendQueueWindow ?? throw new InvalidDataException("No owned review queue exists.");
    }

    private void RequestQuit(int exitCode = 0)
    {
        if (_window is BackendQueueWindow queue) queue.RequestQuit(exitCode);
        else QuitNow(exitCode);
    }

    internal static void QuitNow(int exitCode = 0)
    {
        if (Current is App app) { app.IsQuitting = true; app.Shutdown(exitCode); }
    }

    internal static void ExplainTrayOnce()
    {
        if (Current is not App app || app._trayExplained) return;
        app._trayExplained = true;
        if (app._backendLaunchFile is not null) return;
        var notice = new AppNotificationBuilder()
            .AddText("Watcher is still running in the tray")
            .AddText("Closing the review window hides it. Reopen from the tray; use Exit to quit. An approved action finishes before quitting.")
            .BuildNotification();
        notice.Tag = "tray-explanation"; notice.Group = "native-proof";
        notice.Expiration = DateTimeOffset.Now.AddMinutes(10);
        AppNotificationManager.Default.Show(notice);
        Record("tray_explanation_submitted");
    }

    internal static void SendNotice()
    {
        var notice = new AppNotificationBuilder()
            .AddArgument("action", "open_synthetic_queue")
            .AddText("Watcher native proof: decisions ready")
            .AddText("20 synthetic recommendations. Open the review window to inspect and decide. No GitHub or AI access.")
            .AddButton(new AppNotificationButton("Open review").AddArgument("action", "open_synthetic_queue"))
            .BuildNotification();
        notice.Tag = "synthetic-queue";
        notice.Group = "native-proof";
        notice.Expiration = DateTimeOffset.Now.AddDays(1);
        AppNotificationManager.Default.Show(notice);
        Record("notification_submitted", new { id = notice.Id, setting = AppNotificationManager.Default.Setting.ToString() });
    }

    private static async Task ProbeHiddenChild()
    {
        foreach (var expected in new[] { 0, 7 })
        {
            var start = new ProcessStartInfo {
                FileName = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "wsl.exe"),
                UseShellExecute = false, CreateNoWindow = true,
                RedirectStandardOutput = true, RedirectStandardError = true
            };
            foreach (var arg in new[] { "--exec", "/bin/sh", "-c", $"printf native-proof; exit {expected}" })
                start.ArgumentList.Add(arg);
            using var process = Process.Start(start) ?? throw new InvalidOperationException("Child did not start.");
            var stdout = process.StandardOutput.ReadToEndAsync();
            var stderr = process.StandardError.ReadToEndAsync();
            try
            {
                await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(30));
            }
            catch (TimeoutException)
            {
                process.Kill(entireProcessTree: true);
                await process.WaitForExitAsync();
                throw;
            }
            var output = await stdout;
            Record("hidden_child", new { expected, actual = process.ExitCode, output, error = await stderr });
            if (process.ExitCode != expected || output != "native-proof")
                throw new InvalidOperationException("Child completion did not match its requested result.");
        }
    }

    protected override void OnExit(ExitEventArgs e)
    {
        try
        {
            _tray?.Dispose();
            if (_registered) AppNotificationManager.Default.Unregister();
        }
        finally
        {
            try { Record("process_exited", new { code = e.ApplicationExitCode, pid = Environment.ProcessId }); } catch { }
            base.OnExit(e);
            _notificationDeadline?.Dispose();
        }
    }
}
