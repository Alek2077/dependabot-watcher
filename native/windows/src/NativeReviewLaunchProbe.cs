using System;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;

namespace WatcherNativeProof;

internal static class NativeReviewLaunchProbe
{
    internal static void Run()
    {
        const string executable = "C:\\Owned Fixture\\Watcher.exe";
        var fingerprint = new string('a', 64);
        var launch = new JsonObject {
            ["schema_version"] = 2, ["diagnostic_transport"] = false,
            ["component"] = new JsonObject { ["path"] = executable, ["sha256"] = fingerprint },
            ["arguments"] = new JsonArray("--distribution", "Fixture Ubuntu", "--user", "fixture-user", "--exec",
                "/usr/bin/env", "-u", "PYTHONPATH", "PATH=/home/fixture-user/.local/bin:/home/fixture-user/.npm-global/bin:/usr/local/bin:/usr/bin:/bin",
                "/owned installed café/bin/python", "-I", "-m", "dependabot_watcher.review_bridge", "--config", "/owned/config with spaces.toml",
                "--state-dir", "/owned state")
        };
        static string PathFor(byte[] bytes) => "\\\\wsl.localhost\\Fixture Ubuntu\\owned state\\native-review\\profiles\\"
            + Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant() + "\\backend-launch.json";
        var bytes = Encoding.UTF8.GetBytes(launch.ToJsonString());
        var valid = NativeReviewLaunch.Validate(bytes, PathFor(bytes), executable, fingerprint);
        if (valid.Client.DiagnosticTransport || !valid.Directory.Contains("native-review\\profiles", StringComparison.Ordinal))
            throw new InvalidDataException("Normal profile did not retain its exact non-diagnostic client.");
        var rejected = 0;
        foreach (var fault in new[] { "schema", "diagnostic", "unknown", "component_path", "component_hash", "missing_component",
            "module", "shell", "environment", "search_path", "user", "relative_python", "config_traversal", "state_traversal",
            "extra_argument", "foreign_profile", "foreign_distribution", "modified_bytes", "duplicate_key", "oversized", "unisolated_python" })
        {
            var invalid = (JsonObject)launch.DeepClone();
            var arguments = invalid["arguments"]!.AsArray();
            switch (fault)
            {
                case "schema": invalid["schema_version"] = 1; break;
                case "diagnostic": invalid["diagnostic_transport"] = true; break;
                case "unknown": invalid["override"] = true; break;
                case "component_path": invalid["component"]!["path"] = "C:\\Other.exe"; break;
                case "component_hash": invalid["component"]!["sha256"] = new string('b', 64); break;
                case "missing_component": invalid.Remove("component"); break;
                case "module": arguments[12] = "other_backend"; break;
                case "shell": arguments[11] = "-c"; break;
                case "unisolated_python": arguments[10] = "-s"; break;
                case "environment": arguments[7] = "OTHER_VARIABLE"; break;
                case "search_path": arguments[8] = "PATH=/untrusted:/usr/bin"; break;
                case "user": arguments[3] = "--exec"; break;
                case "relative_python": arguments[9] = "python"; break;
                case "config_traversal": arguments[14] = "/owned/../other.toml"; break;
                case "state_traversal": arguments[16] = "/owned//other"; break;
                case "extra_argument": arguments.Add("--override"); break;
            }
            var invalidBytes = Encoding.UTF8.GetBytes(invalid.ToJsonString());
            if (fault == "duplicate_key") invalidBytes = Encoding.UTF8.GetBytes(invalid.ToJsonString()[..^1] + ",\"schema_version\":2}");
            if (fault == "oversized") invalidBytes = new byte[65537];
            var path = PathFor(invalidBytes);
            if (fault == "foreign_profile") path = path.Replace("owned state", "other state");
            if (fault == "foreign_distribution") path = path.Replace("Fixture Ubuntu", "Other Ubuntu");
            if (fault == "modified_bytes") path = PathFor(bytes) + ".changed";
            var refused = false;
            try { NativeReviewLaunch.Validate(invalidBytes, path, executable, fingerprint); }
            catch (InvalidDataException) { refused = true; }
            if (!refused) throw new InvalidDataException("Unsafe normal launch was accepted: " + fault);
            rejected++;
        }
        App.Record("normal_review_entry_probe_passed", new { rejected, normal_client_retained = true });
    }
}
