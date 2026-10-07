using System;
using System.IO;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace WatcherNativeProof;

// A notification supplies an opaque key, not a path or a command. The private
// per-user registration resolves it, then normal launch validation runs again.
internal static class NativeNotificationProfiles
{
    internal static string Key(string launchFile) => Path.GetFileName(Path.GetDirectoryName(launchFile))
        ?? throw new InvalidDataException("A bound review profile is required.");

    private static string Root(bool create)
    {
        var root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "dependabot-watcher-notifications");
        var identity = WindowsIdentity.GetCurrent().User
            ?? throw new InvalidDataException("The notification user is unavailable.");
        if (create && !Directory.Exists(root))
        {
            var security = new DirectorySecurity();
            security.SetOwner(identity);
            security.SetAccessRuleProtection(true, false);
            security.AddAccessRule(new FileSystemAccessRule(identity, FileSystemRights.FullControl,
                InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
                PropagationFlags.None, AccessControlType.Allow));
            new DirectoryInfo(root).Create(security);
        }
        var directory = new DirectoryInfo(root);
        if (!directory.Exists || (directory.Attributes & FileAttributes.ReparsePoint) != 0)
            throw new InvalidDataException("The private notification directory is unavailable.");
        RequirePrivate(directory.GetAccessControl(), identity);
        return root;
    }

    private static void RequirePrivate(FileSystemSecurity security, SecurityIdentifier identity)
    {
        if (!identity.Equals(security.GetOwner(typeof(SecurityIdentifier))))
            throw new InvalidDataException("Notification registration has a different owner.");
        foreach (FileSystemAccessRule rule in security.GetAccessRules(true, true, typeof(SecurityIdentifier)))
            if (rule.AccessControlType == AccessControlType.Allow && !identity.Equals(rule.IdentityReference))
                throw new InvalidDataException("Notification registration is not private to this user.");
    }

    private static string FilePath(string key, bool create = false)
    {
        if (!Regex.IsMatch(key, "\\A[a-f0-9]{64}\\z"))
            throw new InvalidDataException("Notification profile identity is invalid.");
        return Path.Combine(Root(create), key + ".json");
    }

    internal static void Register(string launchFile)
    {
        _ = NativeReviewLaunch.Read(launchFile);
        var key = Key(launchFile);
        var path = FilePath(key, create: true);
        var payload = JsonSerializer.SerializeToUtf8Bytes(new {
            schema_version = 1, profile_id = key, launch_file = Path.GetFullPath(launchFile)
        });
        if (File.Exists(path))
        {
            // Immutable registration: never silently rebind an existing toast.
            var existing = Read(key);
            if (!string.Equals(existing.File, Path.GetFullPath(launchFile), StringComparison.Ordinal))
                throw new InvalidDataException("Notification profile registration changed.");
            return;
        }
        var temporary = path + "." + Convert.ToHexString(RandomNumberGenerator.GetBytes(16)) + ".tmp";
        try
        {
            using (var file = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            {
                file.Write(payload);
                file.Flush(flushToDisk: true);
            }
            File.Move(temporary, path); // Do not overwrite a concurrent registration.
        }
        catch (IOException) when (File.Exists(path))
        {
            var existing = Read(key);
            if (!string.Equals(existing.File, Path.GetFullPath(launchFile), StringComparison.Ordinal)) throw;
        }
        finally { if (File.Exists(temporary)) File.Delete(temporary); }
    }

    internal static (string File, NativeReviewLaunch Launch) Read(string key)
    {
        var path = FilePath(key);
        var file = new FileInfo(path);
        if (!file.Exists || file.Length is < 1 or > 16384
            || (file.Attributes & FileAttributes.ReparsePoint) != 0)
            throw new InvalidDataException("Notification profile is unavailable or no longer registered.");
        RequirePrivate(file.GetAccessControl(), WindowsIdentity.GetCurrent().User!);
        var bytes = File.ReadAllBytes(path);
        var launchFile = Validate(bytes, key);
        return (launchFile, NativeReviewLaunch.Read(launchFile));
    }

    internal static string Validate(byte[] bytes, string key)
    {
        if (!Regex.IsMatch(key, "\\A[a-f0-9]{64}\\z") || bytes.Length is < 1 or > 16384)
            throw new InvalidDataException("Notification registration exceeds its contract.");
        using var document = JsonDocument.Parse(bytes, new JsonDocumentOptions { MaxDepth = 3 });
        NativeLaunchStream.RequireUniqueKeys(document.RootElement);
        var record = JsonNode.Parse(bytes) as JsonObject;
        if (record is null || record.Count != 3 || record["schema_version"]?.ToJsonString() != "1"
            || record["profile_id"] is not JsonValue profile || !profile.TryGetValue<string>(out var id) || id != key
            || record["launch_file"] is not JsonValue path || !path.TryGetValue<string>(out var file)
            || string.IsNullOrWhiteSpace(file) || !Path.IsPathFullyQualified(file)
            || file.Contains('\0') || Key(file) != key || Path.GetFileName(file) != "backend-launch.json")
            throw new InvalidDataException("Notification registration does not match its opaque profile.");
        return Path.GetFullPath(file);
    }

    internal static void Remove(string launchFile)
    {
        var key = Key(launchFile);
        var saved = Read(key); // Refuse aliases or a changed executable/profile.
        if (!string.Equals(saved.File, Path.GetFullPath(launchFile), StringComparison.Ordinal))
            throw new InvalidDataException("Notification removal is not bound to this profile.");
        File.Delete(FilePath(key));
    }

    internal static void RecordActivationFailure(string stage, int errorCode, int argumentCount)
    {
        // Cold startup has no trusted queue log yet. Keep one small generic
        // failure in the existing fixed, private per-user notification store;
        // never write exception text, payload, arguments or caller-selected paths.
        var root = Root(create: true);
        var path = Path.Combine(root, "last-activation-failure.json");
        var existing = new FileInfo(path);
        if (existing.Exists)
        {
            if ((existing.Attributes & FileAttributes.ReparsePoint) != 0) return;
            RequirePrivate(existing.GetAccessControl(), WindowsIdentity.GetCurrent().User!);
        }
        var temporary = Path.Combine(root, Guid.NewGuid().ToString("N") + ".tmp");
        try
        {
            using (var file = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            {
                file.Write(JsonSerializer.SerializeToUtf8Bytes(new {
                    schema_version = 1, at = DateTimeOffset.UtcNow, stage, error_code = errorCode,
                    argument_count = argumentCount
                }));
                file.Flush(flushToDisk: true);
            }
            File.Move(temporary, path, overwrite: true);
        }
        finally { if (File.Exists(temporary)) File.Delete(temporary); }
    }
}
