using System;
using System.IO;
using System.Reflection;
using System.Runtime.ExceptionServices;
using System.Runtime.Loader;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Xml.Linq;

// Pure contract tests against the built production assembly, without adding
// diagnostic entry points to it, copying its runtime or starting its application.
if (args.Length != 1) throw new ArgumentException("Select the built Windows component assembly.");
var assemblyFile = Path.GetFullPath(args[0]);
using var sdkContext = new NativeSdkContext(Path.ChangeExtension(assemblyFile, ".exe"));
var resolver = new AssemblyDependencyResolver(assemblyFile);
AssemblyLoadContext.Default.Resolving += (_, name) => resolver.ResolveAssemblyToPath(name) is string path
    ? AssemblyLoadContext.Default.LoadFromAssemblyPath(path) : null;
var assembly = AssemblyLoadContext.Default.LoadFromAssemblyPath(assemblyFile);
ConversationContracts.Run(assembly);
ScheduledContracts.Run(assembly);
LocalSyncContracts.Run(assembly);
CompletionContracts.Run(assembly);
ScopeRefreshContracts.Run(assembly);
InvestigationCoordinationContracts.Run(assembly);
try {
    var rejectedLaunches = assembly.GetType("WatcherNativeProof.NativeReviewLaunchProbe", true)!
        .GetMethod("Run", BindingFlags.Static | BindingFlags.NonPublic)!.Invoke(null, null);
    if (rejectedLaunches is not int count || count != 23)
        throw new InvalidDataException("Normal launch probe did not verify all expected malformed profiles.");
} catch (TargetInvocationException failure) when (failure.InnerException is not null) {
    ExceptionDispatchInfo.Capture(failure.InnerException).Throw();
    throw;
}
Console.WriteLine("Normal launch contracts passed: apphost/application identity and 23 invalid profiles.");
var profiles = assembly.GetType("WatcherNativeProof.NativeNotificationProfiles", throwOnError: true)!;
var notices = assembly.GetType("WatcherNativeProof.NativeNotifications", throwOnError: true)!;
var coldCheck = notices.GetMethod("IsColdActivation", BindingFlags.Static | BindingFlags.NonPublic)!;
foreach (var input in new[] { new[] { "----AppNotificationActivated:" }, new[] { "----AppNotificationActivated:", "-Embedding" } })
    if (coldCheck.Invoke(null, new object[] { input }) is not true)
        throw new InvalidDataException("The documented COM notification launch was rejected.");
foreach (var input in new[] { Array.Empty<string>(), new[] { "-Embedding" },
    new[] { "----AppNotificationActivated:", "--launch-file" },
    new[] { "----AppNotificationActivated:", "-Embedding", "extra" },
    new[] { "----AppNotificationActivated:\n" } })
    if (coldCheck.Invoke(null, new object[] { input }) is not false)
        throw new InvalidDataException("Unexpected COM notification launch arguments were accepted.");

string Invoke(Type type, string method, params object[] arguments)
{
    try {
        return (string)(type.GetMethod(method, BindingFlags.Static | BindingFlags.NonPublic)!
            .Invoke(null, arguments) ?? throw new InvalidDataException("Contract returned no value."));
    } catch (TargetInvocationException failure) when (failure.InnerException is not null) {
        ExceptionDispatchInfo.Capture(failure.InnerException).Throw();
        throw;
    }
}

var key = new string('a', 64);
var file = @"\\wsl.localhost\Fixture-Ubuntu\private\state\native-review\profiles\" + key + @"\backend-launch.json";
var record = new JsonObject { ["schema_version"] = 1, ["profile_id"] = key, ["launch_file"] = file };
if (Invoke(profiles, "Validate", Encoding.UTF8.GetBytes(record.ToJsonString()), key) != file
    || Invoke(notices, "ActivationKey", "action=open_review;profile=" + key) != key
    || Invoke(notices, "ActivationKey", "profile=" + key + ";action=open_review") != key)
    throw new InvalidDataException("Valid open-only notification binding was rejected.");
var rejected = 0;
foreach (var input in new[] { "action=merge;profile=" + key, "action=open_review;profile=../other",
    "action=open_review;profile=" + key + ";command=gh", "action=open_review;profile=" + key + ";profile=" + key,
    "action=open_review;profile=" + key + "\n", new string('x', 65536),
    "action=open_review&profile=" + key, "action=open_review;profile=" + key + ";",
    "action=open_review;action=open_review", "profile=" + key + ";profile=" + key })
{
    var refused = false;
    try { Invoke(notices, "ActivationKey", input); } catch (InvalidDataException) { refused = true; }
    if (!refused) throw new InvalidDataException("Unexpected notification input was accepted.");
    rejected++;
}
foreach (var fault in new[] { "different_key", "extra", "relative", "wrong_file", "boolean_version", "duplicate" })
{
    var changed = (JsonObject)record.DeepClone();
    switch (fault) {
        case "different_key": changed["profile_id"] = new string('b', 64); break;
        case "extra": changed["command"] = "ignored commands are forbidden"; break;
        case "relative": changed["launch_file"] = "relative.json"; break;
        case "wrong_file": changed["launch_file"] = file.Replace("backend-launch.json", "another.json"); break;
        case "boolean_version": changed["schema_version"] = true; break;
    }
    var text = changed.ToJsonString();
    if (fault == "duplicate") text = text[..^1] + ",\"profile_id\":\"" + key + "\"}";
    var refused = false;
    try { Invoke(profiles, "Validate", Encoding.UTF8.GetBytes(text), key); }
    catch (Exception error) when (error is InvalidDataException or JsonException) { refused = true; }
    if (!refused) throw new InvalidDataException("Invalid notification registration was accepted.");
    rejected++;
}
// Exercise the installed SDK's serialization, not a hand-built parser fixture.
// Build only: do not register, publish a toast or start the application.
var notification = notices.GetMethod("BuildNotification", BindingFlags.Static | BindingFlags.NonPublic)!
    .Invoke(null, new object[] { key, "Contract test", "No publication" })
    ?? throw new InvalidDataException("SDK builder returned no notification.");
var payload = (string)(notification.GetType().GetProperty("Payload")!.GetValue(notification)
    ?? throw new InvalidDataException("SDK notification has no payload."));
var toast = XDocument.Parse(payload).Root ?? throw new InvalidDataException("SDK payload has no toast.");
var button = toast.Element("actions")?.Element("action") ?? throw new InvalidDataException("SDK button is missing.");
foreach (var value in new[] { (string?)toast.Attribute("launch"), (string?)button.Attribute("arguments") })
    if (value is null || Invoke(notices, "ActivationKey", value) != key)
        throw new InvalidDataException("Real SDK launch/button arguments do not roundtrip through production intake.");
Console.WriteLine(JsonSerializer.Serialize(new { status = "passed", rejected, sdk_payload_roundtrip = true,
    open_only = true, model_calls = 0, github_mutations = false, notification_delivery_tested = false }));
