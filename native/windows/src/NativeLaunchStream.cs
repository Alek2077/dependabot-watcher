using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace WatcherNativeProof;

internal sealed record NativeLaunchFrame(string Event, JsonObject Value);

// Transient transport data only. A preview is not consent: the independent
// permission journal must still establish the exact live request before grant.
internal sealed class NativeLaunchStream
{
    private readonly string _requestId;
    private readonly string _repository;
    private readonly HashSet<string> _permissions = new();
    private long _sequence;
    private long _bytes;
    private bool _started;
    private bool _ended;
    private string? _job;
    private string? _context;

    internal NativeLaunchStream(string requestId, string repository)
    {
        if (!Regex.IsMatch(requestId, "^launch_[a-zA-Z0-9_-]{1,57}$")
            || string.IsNullOrWhiteSpace(repository) || Encoding.UTF8.GetByteCount(repository) > 255)
            throw new InvalidDataException("Investigation requires an exact request and repository.");
        _requestId = requestId; _repository = repository;
    }

    internal NativeLaunchFrame Accept(byte[] bytes)
    {
        if (_ended || bytes.Length is < 1 or > 65535 || _bytes + bytes.Length + 1 > 16000000)
            throw new InvalidDataException("Investigation stream exceeded its contract.");
        using var document = JsonDocument.Parse(bytes, new JsonDocumentOptions { MaxDepth = 32 });
        RequireUniqueKeys(document.RootElement);
        var frame = JsonNode.Parse(bytes) as JsonObject
            ?? throw new InvalidDataException("Investigation frame is not an object.");
        if (frame["schema_version"] is not JsonValue version || !version.TryGetValue<int>(out var schema) || schema != 1
            || Text(frame["request_id"], 64) != _requestId || Text(frame["repository"], 255) != _repository
            || Text(frame["mode"], 32) != "supervisor_launch"
            || frame["sequence"] is not JsonValue sequence || !sequence.TryGetValue<long>(out var number) || number != _sequence)
            throw new InvalidDataException("Investigation frame identity or order differs from the requested job.");
        var kind = Text(frame["event"], 32);
        switch (kind)
        {
            case "starting":
                Keys(frame, "schema_version", "request_id", "repository", "mode", "sequence", "event", "status");
                if (_started || _sequence != 0 || Text(frame["status"], 16) != "ok")
                    throw new InvalidDataException("Investigation startup is invalid.");
                _started = true;
                break;
            case "permission_preview":
                Keys(frame, "schema_version", "request_id", "repository", "mode", "sequence", "event", "request", "preview");
                if (!_started || _permissions.Count >= 200)
                    throw new InvalidDataException("Investigation preview has no bounded active launch.");
                var request = frame["request"] as JsonObject ?? throw new InvalidDataException("Access request is missing.");
                Keys(request, "job_id", "context_digest", "connection_id", "permission_id", "thread_id", "turn_id", "item_id", "digest");
                var job = Id(request["job_id"], "^job_[a-f0-9]{32}$");
                var context = Id(request["context_digest"], "^[a-f0-9]{64}$");
                var connection = Id(request["connection_id"], "^connection_[a-f0-9]{32}$");
                var permission = Id(request["permission_id"], "^permission_[a-f0-9]{32}$");
                foreach (var name in new[] { "thread_id", "turn_id", "item_id" }) Id(request[name], "^[a-zA-Z0-9_-]{1,128}$");
                Id(request["digest"], "^[a-f0-9]{64}$");
                if (!_permissions.Add(connection + "/" + permission) || (_job is not null && (_job != job || _context != context)))
                    throw new InvalidDataException("Access preview repeats or changes the active job.");
                _job = job; _context = context;
                ValidatePreview(frame["preview"] as JsonObject ?? throw new InvalidDataException("Access preview is missing."));
                break;
            case "finished":
                Keys(frame, "schema_version", "request_id", "repository", "mode", "sequence", "event", "status",
                    "job_id", "context_digest", "job_status", "reused");
                var finishedJob = Id(frame["job_id"], "^job_[a-f0-9]{32}$");
                var finishedContext = Id(frame["context_digest"], "^[a-f0-9]{64}$");
                if (!_started || Text(frame["status"], 16) != "ok"
                    || Text(frame["job_status"], 20) is not ("prepared" or "running" or "completed" or "superseded" or "failed" or "interrupted")
                    || frame["reused"] is not JsonValue reused || !reused.TryGetValue<bool>(out _)
                    || (_job is not null && (_job != finishedJob || _context != finishedContext)))
                    throw new InvalidDataException("Investigation terminal identity is invalid.");
                _ended = true;
                break;
            case "error":
                Keys(frame, "schema_version", "request_id", "repository", "mode", "sequence", "event", "status", "message");
                if (Text(frame["status"], 16) != "error") throw new InvalidDataException("Invalid investigation refusal.");
                Text(frame["message"], 8192);
                _ended = true;
                break;
            default: throw new InvalidDataException("Unsupported investigation event.");
        }
        _sequence++; _bytes += bytes.Length + 1;
        return new NativeLaunchFrame(kind, frame);
    }

    internal void RequireTerminal()
    {
        if (!_ended) throw new InvalidDataException("Investigation ended without a terminal receipt. Check saved recovery state; do not retry automatically.");
    }

    internal static bool MatchesPermission(JsonObject frame, NativePermission permission, string expectedContext)
    {
        var request = frame["request"];
        var choices = frame["preview"]?["choices"]?.AsArray().Select(value => value!.GetValue<string>()).ToArray();
        return request?["context_digest"]?.GetValue<string>() == expectedContext
            && request?["job_id"]?.GetValue<string>() == permission.JobId
            && request?["connection_id"]?.GetValue<string>() == permission.ConnectionId
            && request?["permission_id"]?.GetValue<string>() == permission.PermissionId
            && request?["thread_id"]?.GetValue<string>() == permission.ThreadId
            && request?["turn_id"]?.GetValue<string>() == permission.TurnId
            && request?["item_id"]?.GetValue<string>() == permission.ItemId
            && request?["digest"]?.GetValue<string>() == permission.Digest
            && choices is not null && choices.SequenceEqual(permission.Choices);
    }

    internal static string DescribePreview(JsonObject frame)
    {
        var preview = frame["preview"]!.AsObject();
        var network = preview["network"] as JsonObject;
        var scope = network is not null ? $"Network: {network["protocol"]}://{network["host"]}"
            : $"Command:\n{preview["command"]}\nWorking directory: {preview["cwd"]}";
        if (network is not null && preview["command"] is not null)
            scope += $"\nRelated command:\n{preview["command"]}";
        if (network is not null && preview["cwd"] is not null)
            scope += $"\nWorking directory: {preview["cwd"]}";
        if (preview["reason"] is not null) scope += $"\nReason: {preview["reason"]}";
        if (preview["additional_permissions"] is JsonObject extra)
            scope += "\nAdditional requested access:\n" + extra.ToJsonString(new JsonSerializerOptions { WriteIndented = true });
        // TextBlock is non-executable, but invisible/bidirectional controls can
        // disguise a command's scope. Show them explicitly, without raw logs.
        return NativeDisplayText.Escape(scope);
    }

    private static void ValidatePreview(JsonObject preview)
    {
        Keys(preview, "kind", "command", "cwd", "reason", "network", "additional_permissions", "choices");
        var kind = Text(preview["kind"], 16);
        foreach (var name in new[] { "command", "cwd", "reason" })
            if (preview[name] is not null) Text(preview[name], 16384);
        if (kind == "network")
        {
            var network = preview["network"] as JsonObject ?? throw new InvalidDataException("Network destination is missing.");
            Keys(network, "host", "protocol"); Text(network["host"], 1024);
            if (Text(network["protocol"], 16) is not ("http" or "https" or "socks5Tcp" or "socks5Udp"))
                throw new InvalidDataException("Network protocol is unsupported.");
        }
        else if (kind != "command" || preview["network"] is not null || preview["command"] is null || preview["cwd"] is null)
            throw new InvalidDataException("Command preview lacks its exact scope.");
        if (preview["choices"] is not JsonArray choices || choices.Count is < 1 or > 4)
            throw new InvalidDataException("Access preview lacks valid choices.");
        var values = choices.Select(value => Text(value, 20)).ToArray();
        if (values.Distinct().Count() != values.Length || values.Any(value => value is not ("allow_once" or "allow_session" or "deny" or "cancel")))
            throw new InvalidDataException("Access preview offers unsupported decisions.");
        if (preview["additional_permissions"] is not null)
        {
            var extra = preview["additional_permissions"] as JsonObject ?? throw new InvalidDataException("Invalid access overlay.");
            AllowedKeys(extra, "fileSystem", "network");
            if (extra["fileSystem"] is not null)
            {
                var files = extra["fileSystem"] as JsonObject ?? throw new InvalidDataException("Invalid filesystem access.");
                AllowedKeys(files, "read", "write", "entries", "globScanMaxDepth");
                if (files["entries"] is not null || files["globScanMaxDepth"] is not null)
                    throw new InvalidDataException("Unsupported filesystem access overlay.");
                foreach (var access in new[] { "read", "write" })
                {
                    if (files[access] is null) continue;
                    if (files[access] is not JsonArray paths || paths.Count > 32)
                        throw new InvalidDataException("Invalid filesystem path list.");
                    foreach (var path in paths)
                        if (!Text(path, 4096).StartsWith('/')) throw new InvalidDataException("Access path is not absolute.");
                }
            }
            if (extra["network"] is not null)
            {
                var network = extra["network"] as JsonObject ?? throw new InvalidDataException("Invalid network access overlay.");
                AllowedKeys(network, "enabled");
                if (network["enabled"] is not null && (network["enabled"] is not JsonValue value || !value.TryGetValue<bool>(out _)))
                    throw new InvalidDataException("Invalid network access toggle.");
            }
        }
    }

    internal static void RequireUniqueKeys(JsonElement element)
    {
        if (element.ValueKind == JsonValueKind.Object)
        {
            var keys = new HashSet<string>();
            foreach (var property in element.EnumerateObject())
            {
                if (!keys.Add(property.Name)) throw new InvalidDataException("Duplicate investigation JSON field.");
                RequireUniqueKeys(property.Value);
            }
        }
        else if (element.ValueKind == JsonValueKind.Array)
            foreach (var item in element.EnumerateArray()) RequireUniqueKeys(item);
    }

    private static void Keys(JsonObject value, params string[] keys)
    {
        if (value.Count != keys.Length || value.Any(pair => !keys.Contains(pair.Key)))
            throw new InvalidDataException("Investigation frame has missing or unsupported fields.");
    }

    private static void AllowedKeys(JsonObject value, params string[] keys)
    {
        if (value.Any(pair => !keys.Contains(pair.Key))) throw new InvalidDataException("Unsupported access scope.");
    }

    private static string Text(JsonNode? node, int bytes)
    {
        if (node is not JsonValue value || !value.TryGetValue<string>(out var text) || string.IsNullOrWhiteSpace(text)
            || text.Contains('\0') || Encoding.UTF8.GetByteCount(text) > bytes)
            throw new InvalidDataException("Invalid bounded investigation text.");
        return text;
    }

    private static string Id(JsonNode? node, string pattern)
    {
        var value = Text(node, 128);
        return Regex.IsMatch(value, pattern) ? value : throw new InvalidDataException("Invalid investigation identifier.");
    }
}
