using System;
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
            _diagnosticWindow = e.Args.Length == 1 && e.Args[0] == "--diagnostic-window";
            _diagnosticMode = _diagnosticWindow || e.Args.Any(arg => arg is "--launch-roundtrip-probe"
                or "--launch-stream-probe" or "--permission-probe" or "--supervisor-probe"
                or "--bridge-probe" or "--child-probe" or "--cleanup" or "--notify" or "--status");
            if (launchIndex >= 0 && !_diagnosticMode)
            {
                if (launchIndex + 1 >= e.Args.Length || e.Args.Length != 2)
                    throw new ArgumentException("Open the configured review with one trusted launch file.");
                _backendLaunchFile = Path.GetFullPath(e.Args[launchIndex + 1]);
                // The normal CLI owns this private profile directory. No
                // notification, model reply or UI text selects this path.
                var selected = NativeReviewLaunch.Read(_backendLaunchFile);
                _stateRoot = selected.Directory;
                _normalClient = selected.Client;
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
            if (_normalClient is null && !_diagnosticWindow
                && !e.Args.Any(arg => arg is "--cleanup" or "--notify" or "--status"))
                throw new InvalidDataException("Open the configured review through dependabot-watcher review.");
            AppNotificationManager.Default.NotificationInvoked += (_, _) =>
            {
                Dispatcher.BeginInvoke(() =>
                {
                    if (_instance?.IsCurrent == true) OpenQueue("notification_click");
                });
            };
            AppNotificationManager.Default.Register();
            _registered = true;
            // First notification activation is returned here; subsequent clicks
            // on a running instance are delivered through NotificationInvoked.
            var activationArgs = AppInstance.GetCurrent().GetActivatedEventArgs();
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
            var key = "WatcherNativeProof-" + Convert.ToHexString(
                SHA256.HashData(Encoding.UTF8.GetBytes(Root)))[..16];
            _instance = AppInstance.FindOrRegisterForKey(key);
            _instance.Activated += (_, _) => Dispatcher.BeginInvoke(
                () => OpenQueue("existing_instance_activation"));
            if (!_instance.IsCurrent)
            {
                await _instance.RedirectActivationToAsync(activationArgs);
                Shutdown();
                return;
            }
            var notificationLaunch = activationArgs.Kind == ExtendedActivationKind.AppNotification;
            // The toast has only one operation: open the aggregate review queue.
            // Display that read-only window after SDK startup; the notification
            // payload is not accepted as action authority.
            // Decisions remain deliberate interactions inside the window.
            OpenQueue(notificationLaunch ? "notification_open" : "normal_open");
        }
        catch (Exception ex)
        {
            try { Record("startup_error", ex.ToString()); } catch { }
            RequestQuit(1);
        }
    }

    private void OpenQueue(string reason)
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
        _window.Show();
        if (_window.WindowState == WindowState.Minimized) _window.WindowState = WindowState.Normal;
        _window.Activate();
        Record("queue_opened", new { reason, pid = Environment.ProcessId });
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
        }
    }
}
