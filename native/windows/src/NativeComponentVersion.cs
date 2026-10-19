using System;
using System.IO;
using System.Linq;
using System.Text.Json;

namespace WatcherNativeProof;

internal static class NativeComponentVersion
{
    internal static string Read()
    {
        var directory = Path.GetDirectoryName(typeof(NativeComponentVersion).Assembly.Location)!;
        var manifest = new FileInfo(Path.Combine(directory, "component-manifest.json"));
        try
        {
            if (!manifest.Exists || manifest.Length is < 1 or > 4_000_000) return "development build";
            using var document = JsonDocument.Parse(File.ReadAllText(manifest.FullName));
            if (document.RootElement.ValueKind == JsonValueKind.Object
                && document.RootElement.TryGetProperty("version", out var value)
                && value.ValueKind == JsonValueKind.String
                && value.GetString() is { Length: > 0 and <= 64 } version
                && char.IsAsciiDigit(version[0])
                && version.All(character => char.IsAsciiLetterOrDigit(character) || character is '.' or '+' or '-'))
                return version;
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException) { }
        return "development build";
    }
}
