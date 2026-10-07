using System;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using Microsoft.Windows.AppNotifications;
using Microsoft.Windows.AppNotifications.Builder;

namespace WatcherNativeProof;

internal static class NativeNotifications
{
    internal static bool IsColdActivation(string[] arguments) =>
        arguments.Length is 1 or 2 && arguments[0] == "----AppNotificationActivated:"
        && (arguments.Length == 1 || arguments[1] == "-Embedding");

    private static string Group(string key) => "watcher-" + key[15..];
    internal static string ActivationKey(string argument)
    {
        // Inspect the original argument string, not a dictionary that may have
        // discarded duplicate fields. There are no text inputs or action grants.
        if (argument.Length > 128) throw new InvalidDataException("Notification activation exceeds its bound.");
        // AppNotificationBuilder serializes arguments with ';', and its map
        // iteration order is not a protocol guarantee. Admit exactly our two
        // known fields in either order, without collapsing duplicates.
        var match = Regex.Match(argument, "\\A(?:action=open_review;profile=([a-f0-9]{64})|profile=([a-f0-9]{64});action=open_review)\\z");
        if (!match.Success) throw new InvalidDataException("Unsupported notification activation.");
        return match.Groups[1].Success ? match.Groups[1].Value : match.Groups[2].Value;
    }

    internal static AppNotification BuildNotification(string key, string title, string body) =>
        new AppNotificationBuilder()
            .AddArgument("action", "open_review").AddArgument("profile", key)
            .AddText(title).AddText(body)
            .AddButton(new AppNotificationButton("Open review")
                .AddArgument("action", "open_review").AddArgument("profile", key))
            .BuildNotification();

    internal static async Task<JsonObject> PublishAsync(BackendClient client, string key, string? runId, bool test)
    {
        string title, body;
        if (test)
        {
            title = "Dependabot Watcher: notification test";
            body = "Open your saved review queue. This test uses no AI, changes no GitHub data and does not reset reminder state.";
        }
        else
        {
            var attention = await client.SendAsync(new JsonObject {
                ["schema_version"] = 1, ["request_id"] = "read_notice_" + Guid.NewGuid().ToString("N"),
                ["operation"] = "notification_attention", ["run_id"] = runId
            });
            if (attention["needed"]?.GetValue<bool>() != true)
                return Receipt("not_needed", key, 0);
            title = attention["title"]?.GetValue<string>() ?? throw new InvalidDataException("Notification title is missing.");
            body = attention["body"]?.GetValue<string>() ?? throw new InvalidDataException("Notification body is missing.");
            if (Encoding.UTF8.GetByteCount(title) is < 1 or > 480 || Encoding.UTF8.GetByteCount(body) is < 1 or > 4000
                || attention["run_id"]?.GetValue<string>() != runId)
                throw new InvalidDataException("Notification does not match the bounded saved run.");
        }
        var manager = AppNotificationManager.Default;
        if (manager.Setting.ToString() != "Enabled") return Receipt("unavailable", key, 0);
        var notification = BuildNotification(key, title, body);
        notification.Tag = Tag(key, test);
        notification.Group = Group(key);
        notification.Expiration = DateTimeOffset.Now.AddDays(1);
        manager.Show(notification);
        // This is diagnostic logging, not the authoritative acceptance receipt.
        // Disk/ACL failures after Show must not erase the SDK's accepted result.
        try { App.Record("review_notification_submitted", new { profile = key, test, id = notification.Id }); } catch { }
        return Receipt(notification.Id == 0 ? "unavailable" : "accepted", key, notification.Id);
    }

    private static string Tag(string key, bool test) => (test ? "t" : "r") + key[..15];

    internal static void PublishInvestigation(string key, string kind)
    {
        var text = kind switch {
            "permission" => ("Watcher: agent needs access", "Open review to inspect the exact access request. No merge is approved."),
            "completed" => ("Watcher: agent results are ready", "Open review for saved answers and recommendations. Each merge still needs exact confirmation."),
            "attention" => ("Watcher: maintenance needs attention", "Open review to inspect configuration, incomplete evidence or recovery. Failed work was not automatically retried."),
            _ => throw new InvalidDataException("Unsupported investigation notification.")
        };
        var manager = AppNotificationManager.Default;
        if (manager.Setting.ToString() != "Enabled") { App.Record("agent_notification_unavailable"); return; }
        var notice = BuildNotification(key, text.Item1, text.Item2);
        notice.Tag = "a" + key[..15]; notice.Group = Group(key);
        notice.Expiration = DateTimeOffset.Now.AddDays(1);
        manager.Show(notice);
        try { App.Record("agent_notification_submitted", new { kind, id = notice.Id }); } catch { }
    }

    internal static async Task<JsonObject> StatusAsync(string key)
    {
        _ = NativeNotificationProfiles.Read(key);
        var notices = await AppNotificationManager.Default.GetAllAsync();
        return new JsonObject {
            ["schema_version"] = 1, ["status"] = "registered", ["profile_id"] = key,
            ["setting"] = AppNotificationManager.Default.Setting.ToString(),
            ["review_count"] = notices.Count(item => item.Group == Group(key) && (item.Tag == Tag(key, false) || item.Tag == "a" + key[..15])),
            ["test_count"] = notices.Count(item => item.Group == Group(key) && item.Tag == Tag(key, true))
        };
    }

    internal static async Task<JsonObject> RemoveAsync(string launchFile)
    {
        var key = NativeNotificationProfiles.Key(launchFile);
        // Keep the lookup until both removals finish so a partial SDK failure can
        // be inspected/retried. An unsuccessful removal still permits only open
        // intent, never consent. Never clear other profiles with UnregisterAll.
        await AppNotificationManager.Default.RemoveByTagAndGroupAsync(Tag(key, false), Group(key));
        await AppNotificationManager.Default.RemoveByTagAndGroupAsync(Tag(key, true), Group(key));
        await AppNotificationManager.Default.RemoveByTagAndGroupAsync("a" + key[..15], Group(key));
        NativeNotificationProfiles.Remove(launchFile);
        return Receipt("removed", key, 0);
    }

    private static JsonObject Receipt(string status, string key, uint id) => new() {
        ["schema_version"] = 1, ["status"] = status, ["profile_id"] = key,
        ["notification_id"] = id, ["assurance"] = "adapter_accepted_only"
    };
}
