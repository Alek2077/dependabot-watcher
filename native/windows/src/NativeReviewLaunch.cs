using System;
using System.Collections.Generic;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace WatcherNativeProof;

// Normal entry is not the diagnostic arbitrary-argv transport. Capture once,
// validate the producer contract and reuse the client through the window's life.
internal sealed record NativeReviewLaunch(BackendClient Client, string Directory)
{
    internal static NativeReviewLaunch Read(string file)
    {
        var executable = Environment.ProcessPath ?? throw new InvalidDataException("Review executable is unavailable.");
        var information = new FileInfo(file);
        if (!information.Exists || information.Length is < 1 or > 65536
            || (information.Attributes & FileAttributes.ReparsePoint) != 0)
            throw new InvalidDataException("Normal review requires a regular bounded launch file.");
        var bytes = File.ReadAllBytes(file);
        using var stream = File.OpenRead(executable);
        var fingerprint = Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();
        var assembly = typeof(NativeReviewLaunch).Assembly.Location;
        if (!string.Equals(Path.GetFullPath(assembly), Path.ChangeExtension(Path.GetFullPath(executable), ".dll"), StringComparison.OrdinalIgnoreCase)
            || (new FileInfo(assembly).Attributes & FileAttributes.ReparsePoint) != 0)
            throw new InvalidDataException("Review application code is not in the selected component directory.");
        using var assemblyStream = File.OpenRead(assembly);
        var assemblyFingerprint = Convert.ToHexString(SHA256.HashData(assemblyStream)).ToLowerInvariant();
        return Validate(bytes, file, executable, fingerprint, assemblyFingerprint);
    }

    internal static NativeReviewLaunch Validate(byte[] bytes, string file, string executable, string fingerprint, string assemblyFingerprint)
    {
        if (bytes.Length is < 1 or > 65536) throw new InvalidDataException("Review launch exceeds its byte bound.");
        using var document = JsonDocument.Parse(bytes, new JsonDocumentOptions { MaxDepth = 8 });
        NativeLaunchStream.RequireUniqueKeys(document.RootElement);
        var launch = JsonNode.Parse(bytes) as JsonObject ?? throw new InvalidDataException("Invalid review launch.");
        if (launch.Count != 4 || launch["schema_version"]?.ToJsonString() != "3"
            || launch["diagnostic_transport"]?.ToJsonString() != "false"
            || launch["component"] is not JsonObject component || component.Count != 3
            || Text(component["path"]) is not string selected || !Path.IsPathFullyQualified(selected)
            || !string.Equals(Path.GetFullPath(selected), Path.GetFullPath(executable), StringComparison.OrdinalIgnoreCase)
            || Text(component["sha256"]) is not string digest || !Regex.IsMatch(digest, "^[a-f0-9]{64}$")
            || digest != fingerprint
            || Text(component["assembly_sha256"]) is not string assemblyDigest || !Regex.IsMatch(assemblyDigest, "^[a-f0-9]{64}$")
            || assemblyDigest != assemblyFingerprint || launch["arguments"] is not JsonArray args || args.Count != 17)
            throw new InvalidDataException("Review launch does not match the selected component and protocol.");
        var arguments = new List<string>();
        foreach (var value in args)
        {
            var argument = Text(value);
            if (string.IsNullOrWhiteSpace(argument) || argument.Contains('\0') || Encoding.UTF8.GetByteCount(argument) > 8192)
                throw new InvalidDataException("Invalid review argument.");
            arguments.Add(argument);
        }
        if (arguments[0] != "--distribution" || !Regex.IsMatch(arguments[1], "^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$")
            || arguments[2] != "--user" || !Regex.IsMatch(arguments[3], "^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
            || arguments[4] != "--exec" || arguments[5] != "/usr/bin/env"
            || arguments[6] != "-u" || arguments[7] != "PYTHONPATH" || !ValidSearchPath(arguments[8])
            || !PosixPath(arguments[9]) || arguments[10] != "-I" || arguments[11] != "-m" || arguments[12] != "dependabot_watcher.review_bridge"
            || arguments[13] != "--config" || !PosixPath(arguments[14])
            || arguments[15] != "--state-dir" || !PosixPath(arguments[16]))
            throw new InvalidDataException("Normal review accepts only the installed backend command.");
        var profile = Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant();
        var expected = "\\\\wsl.localhost\\" + arguments[1]
            + arguments[16].Replace('/', '\\') + "\\native-review\\profiles\\" + profile + "\\backend-launch.json";
        var full = Path.GetFullPath(file);
        if (!string.Equals(full, expected, StringComparison.Ordinal))
            throw new InvalidDataException("Review launch is not bound to its private state/profile bytes.");
        return new NativeReviewLaunch(new BackendClient(arguments.AsReadOnly()),
            Path.GetDirectoryName(full) ?? throw new InvalidDataException("Review profile directory is missing."));
    }

    private static string? Text(JsonNode? value) => value is JsonValue scalar && scalar.TryGetValue<string>(out var text) ? text : null;

    private static bool PosixPath(string path)
    {
        if (!path.StartsWith('/') || path.Length <= 1 || path.EndsWith('/') || path.Contains('\\') || path.Contains('\0')) return false;
        foreach (var part in path[1..].Split('/'))
            if (part.Length == 0 || part is "." or "..") return false;
        return true;
    }

    private static bool ValidSearchPath(string value)
    {
        if (!value.StartsWith("PATH=", StringComparison.Ordinal)) return false;
        var paths = value[5..].Split(':');
        if (paths.Length != 5 || !PosixPath(paths[0]) || !PosixPath(paths[1])
            || !paths[0].EndsWith("/.local/bin", StringComparison.Ordinal)) return false;
        var home = paths[0][..^11];
        return home.Length > 0 && paths[1] == home + "/.npm-global/bin"
            && paths[2] == "/usr/local/bin" && paths[3] == "/usr/bin" && paths[4] == "/bin";
    }
}
