using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;

namespace WatcherNativeProof;

internal sealed class BackendClient
{
    private readonly IReadOnlyList<string> _arguments;
    private readonly object _operationGate = new();
    private int _operations;
    private bool _closing;
    private TaskCompletionSource<bool> _drained = new(TaskCreationOptions.RunContinuationsAsynchronously);
    internal bool DiagnosticTransport { get; }

    internal BackendClient(IReadOnlyList<string> arguments, bool diagnosticTransport = false)
    { _arguments = arguments; DiagnosticTransport = diagnosticTransport; }

    // Shutdown belongs to the same client that owns every child. Counting only
    // the model stream misses independent feedback/permission/collection calls.
    internal Task StopAcceptingAndDrainAsync()
    {
        lock (_operationGate) {
            _closing = true;
            return _operations == 0 ? Task.CompletedTask : _drained.Task;
        }
    }

    private IDisposable OwnOperation()
    {
        lock (_operationGate) {
            if (_closing) throw new InvalidOperationException("Watcher is closing; no new backend request was started.");
            if (_operations++ == 0) _drained = new(TaskCreationOptions.RunContinuationsAsynchronously);
        }
        return new OperationLease(() => {
            lock (_operationGate) { if (--_operations == 0) _drained.TrySetResult(true); }
        });
    }

    private sealed class OperationLease(Action release) : IDisposable
    {
        private Action? _release = release;
        public void Dispose() => Interlocked.Exchange(ref _release, null)?.Invoke();
    }

    internal static BackendClient FromLaunchFile(string launchFile)
    {
        // This is a trusted local launch configuration, never a notification
        // argument or a backend-response-selected executable.
        var information = new FileInfo(launchFile);
        if (!information.Exists || information.Length is < 1 or > 65536)
            throw new InvalidDataException("Invalid bounded backend launch file.");
        var bytes = File.ReadAllBytes(launchFile);
        if (bytes.Length is < 1 or > 65536) throw new InvalidDataException("Backend launch file changed beyond its byte limit.");
        using var document = JsonDocument.Parse(bytes, new JsonDocumentOptions { MaxDepth = 8 });
        NativeLaunchStream.RequireUniqueKeys(document.RootElement);
        var launch = JsonNode.Parse(bytes) as JsonObject
            ?? throw new InvalidDataException("Invalid backend launch configuration.");
        var diagnostic = launch["diagnostic_transport"]?.GetValue<bool>() ?? false;
        if (!diagnostic)
            throw new InvalidDataException("Normal review requires its bound component/profile entry, not the diagnostic transport.");
        var args = launch["arguments"] as JsonArray
            ?? throw new InvalidDataException("Missing backend arguments.");
        var arguments = new List<string>();
        if (args.Count is < 1 or > 32) throw new InvalidDataException("Backend arguments exceed their bound.");
        foreach (var value in args) {
            var argument = value?.GetValue<string>() ?? throw new InvalidDataException("Invalid backend argument.");
            if (string.IsNullOrWhiteSpace(argument) || argument.Contains('\0') || System.Text.Encoding.UTF8.GetByteCount(argument) > 8192)
                throw new InvalidDataException("Invalid bounded backend argument.");
            arguments.Add(argument);
        }
        return new BackendClient(arguments, diagnostic);
    }

    internal Task<JsonObject> SendAsync(JsonObject request) => SendCoreAsync(request, CancellationToken.None);

    internal Task<JsonObject> ObserveLocalSyncAsync(JsonObject request, CancellationToken cancellation)
    {
        if (request["operation"]?.GetValue<string>() != "local_sync_status")
            throw new InvalidOperationException("Only optional checkout observation accepts cancellation.");
        return SendCoreAsync(request, cancellation);
    }

    private async Task<JsonObject> SendCoreAsync(JsonObject request, CancellationToken cancellation)
    {
        cancellation.ThrowIfCancellationRequested();
        using var operationOwner = OwnOperation();
        var operation = request["operation"]?.GetValue<string>();
        if (operation is not ("snapshot" or "reject" or "defer" or "resume" or "feedback"
            or "prepare_action" or "confirm_action" or "check_action" or "check_completion" or "action_history" or "collect_queue" or "supervisor_status"
            or "supervisor_permissions" or "supervisor_permission_decide" or "notification_attention"
            or "local_sync_targets" or "local_sync_status" or "prepare_local_sync" or "confirm_local_sync" or "recover_local_sync"
            or "scheduled_supervisor_claim" or "prepare_branch_refresh" or "confirm_branch_refresh" or "check_branch_refresh" or "continue_branch_refresh" or "release_branch_refresh"))
            throw new InvalidOperationException("Unsupported backend operation.");
        var mutating = operation is "confirm_action" or "confirm_local_sync" or "confirm_branch_refresh";
        var liveOperation = operation is "prepare_action" or "confirm_action" or "check_action" or "check_completion" or "collect_queue"
            or "prepare_local_sync" or "confirm_local_sync" or "recover_local_sync"
            or "prepare_branch_refresh" or "confirm_branch_refresh" or "check_branch_refresh" or "continue_branch_refresh";
        var requestId = request["request_id"]?.GetValue<string>()
            ?? throw new InvalidDataException("Request ID is required.");
        var payload = JsonSerializer.SerializeToUtf8Bytes(request);
        if (payload.Length > 65536) throw new InvalidDataException("Request exceeds its byte limit.");
        var start = new ProcessStartInfo {
            FileName = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "wsl.exe"),
            UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true
        };
        foreach (var argument in _arguments) start.ArgumentList.Add(argument);
        using var process = Process.Start(start) ?? throw new IOException("Backend did not start.");
        var output = ReadBoundedAsync(process.StandardOutput.BaseStream, 10000000);
        var error = ReadBoundedAsync(process.StandardError.BaseStream, 65536);
        try
        {
            await process.StandardInput.BaseStream.WriteAsync(payload);
            process.StandardInput.Close(); // EOF is the one-request frame boundary.
            var completion = Task.WhenAll(output, error, process.WaitForExitAsync());
            // Python enforces the configured timeout for each GitHub command.
            // Live operations can contain multiple bounded commands; an unrelated
            // native deadline must not override that policy. An approved action
            // also must not be killed or retried after a response fault.
            if (liveOperation) await completion;
            else await completion.WaitAsync(TimeSpan.FromSeconds(30), cancellation);
            var response = JsonNode.Parse(await output) as JsonObject
                ?? throw new InvalidDataException("Backend response is not a JSON object.");
            if (response["schema_version"]?.GetValue<int>() != 1
                || response["request_id"]?.GetValue<string>() != requestId)
                throw new InvalidDataException("Backend response version or request ID does not match.");
            if (response["status"]?.GetValue<string>() != "ok" || process.ExitCode != 0)
                throw new InvalidDataException(response["message"]?.GetValue<string>()
                    ?? "Backend request did not complete successfully; refresh saved state.");
            return response;
        }
        catch
        {
            if (!process.HasExited)
            {
                process.StandardInput.Close();
                if (!mutating) process.Kill(entireProcessTree: true);
                await process.WaitForExitAsync();
            }
            throw;
        }
        finally
        {
            // Observe both readers even after write, timeout or response faults.
            // The operation lease remains held until owned readers finish.
            try { await output; } catch { }
            try { await error; } catch { }
        }
    }

    internal async Task<JsonObject> StartSupervisorAsync(JsonObject request, Action<NativeLaunchFrame> receive,
        CancellationToken cancellation = default)
    {
        using var operationOwner = OwnOperation();
        var parser = SupervisorParser(request);
        var payload = JsonSerializer.SerializeToUtf8Bytes(request);
        if (payload.Length > 65536) throw new InvalidDataException("Investigation request is too large.");
        var start = new ProcessStartInfo {
            FileName = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "wsl.exe"),
            UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true
        };
        foreach (var argument in _arguments) start.ArgumentList.Add(argument);
        using var process = Process.Start(start) ?? throw new IOException("Investigation backend did not start.");
        var error = ReadBoundedAsync(process.StandardError.BaseStream, 65536);
        // Reading/validation never waits on a UI permission decision. The caller
        // posts bounded events to its dispatcher and uses independent exchanges.
        var output = Task.Run(() => ReadLaunchAsync(process.StandardOutput.BaseStream, parser, receive, cancellation));
        try
        {
            await process.StandardInput.BaseStream.WriteAsync(payload, cancellation);
            process.StandardInput.Close();
            var terminal = await output;
            await process.WaitForExitAsync(cancellation).WaitAsync(TimeSpan.FromSeconds(5));
            await error;
            if (terminal.Event != "finished" || process.ExitCode != 0)
                throw new InvalidDataException(terminal.Event == "error"
                    ? terminal.Value["message"]!.GetValue<string>()
                    : "Investigation did not complete successfully. Check the saved job; it was not retried.");
            return terminal.Value;
        }
        catch
        {
            // Closing the owned reader is the backend's loss signal. It stops
            // the provider and invalidates consent even if WSL host termination
            // alone does not kill a separately owned POSIX provider process.
            process.StandardOutput.Close();
            process.StandardInput.Close();
            // Let Python observe pipe loss and tear down its separately owned
            // provider before forcibly ending the Windows-side WSL launcher.
            try { await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(2)); }
            catch (TimeoutException)
            {
                if (!process.HasExited) process.Kill(entireProcessTree: true);
                try { await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(5)); }
                catch (TimeoutException) { }
            }
            process.StandardError.Close();
            try { await output; } catch { }
            try { await error; } catch { }
            throw;
        }
    }

    internal static NativeLaunchStream SupervisorParser(JsonObject request)
    {
        var required = new[] { "schema_version", "request_id", "operation", "repository", "context_digest" };
        if (required.Any(key => !request.ContainsKey(key))
            || request.Any(field => !required.Contains(field.Key) && field.Key is not ("retry_job_id" or "scheduled_run_id"))
            || request["schema_version"]?.GetValue<int>() != 1
            || request["operation"]?.GetValue<string>() != "supervisor_start"
            || request["context_digest"]?.GetValue<string>() is not string digest
            || !System.Text.RegularExpressions.Regex.IsMatch(digest, "^[a-f0-9]{64}$")
            || (request.ContainsKey("retry_job_id") && (request["retry_job_id"] is not JsonValue retry
                || !retry.TryGetValue<string>(out var retryId)
                || !System.Text.RegularExpressions.Regex.IsMatch(retryId, "^job_[a-f0-9]{32}$")))
            || (request.ContainsKey("scheduled_run_id") && (request.ContainsKey("retry_job_id")
                || request["scheduled_run_id"] is not JsonValue scheduled || !scheduled.TryGetValue<string>(out var runId)
                || !System.Text.RegularExpressions.Regex.IsMatch(runId, "\\Arun_[a-f0-9]{20}\\z"))))
            throw new InvalidDataException("Investigation requires the saved exact context, not launch overrides.");
        return new NativeLaunchStream(request["request_id"]!.GetValue<string>(), request["repository"]!.GetValue<string>());
    }

    internal static async Task<NativeLaunchFrame> ReadLaunchAsync(Stream stream, NativeLaunchStream parser,
        Action<NativeLaunchFrame> receive, CancellationToken cancellation)
    {
        var buffer = new byte[8192];
        using var line = new MemoryStream();
        NativeLaunchFrame? last = null;
        int count;
        while ((count = await stream.ReadAsync(buffer, cancellation)) != 0)
        {
            for (var index = 0; index < count; index++)
            {
                if (buffer[index] == (byte)'\n')
                {
                    last = parser.Accept(line.ToArray()); line.SetLength(0);
                    receive(last);
                }
                else
                {
                    if (line.Length >= 65535) throw new InvalidDataException("Investigation frame exceeds its byte limit.");
                    line.WriteByte(buffer[index]);
                }
            }
        }
        if (line.Length != 0) throw new InvalidDataException("Investigation ended with an incomplete frame.");
        parser.RequireTerminal();
        return last ?? throw new InvalidDataException("Investigation returned no terminal frame.");
    }

    private static async Task<byte[]> ReadBoundedAsync(Stream stream, int limit)
    {
        using var content = new MemoryStream();
        var buffer = new byte[8192];
        var exceeded = false;
        int count;
        while ((count = await stream.ReadAsync(buffer)) != 0)
        {
            if (content.Length + count > limit) exceeded = true;
            // Drain excess bytes without retaining them. Stopping the reader
            // could block an approved backend action on a full output pipe.
            if (!exceeded) content.Write(buffer, 0, count);
        }
        if (exceeded) throw new InvalidDataException("Backend output exceeded its byte limit.");
        return content.ToArray();
    }

    internal static async Task ProbeAsync(string launchFile)
    {
        var client = FromLaunchFile(launchFile);
        long nextSequence = 0;
        JsonObject Request(string operation) => new() {
            ["schema_version"] = 1,
            ["request_id"] = operation == "snapshot" ? "read_" + Guid.NewGuid().ToString("N")
                : $"decision_{nextSequence}_" + Guid.NewGuid().ToString("N"),
            ["operation"] = operation
        };
        JsonObject First(JsonObject view)
        {
            if (view["capabilities"]?["github_mutations"]?.GetValue<bool>() != false
                || view["capabilities"]?["model_calls"]?.GetValue<bool>() != false)
                throw new InvalidDataException("Proof requires a nonmutating, model-off backend.");
            var item = view["proposals"]?[0] as JsonObject
                ?? throw new InvalidDataException("Proof proposal missing.");
            if (item["repository"]?.GetValue<string>() != "example-org/example-app")
                throw new InvalidDataException("Only the synthetic proof repository is permitted.");
            return item;
        }
        var view = await client.SendAsync(Request("snapshot"));
        nextSequence = view["next_request_sequence"]!.GetValue<long>();
        var first = First(view);
        var proposalId = first["proposal_id"]!.GetValue<string>();
        foreach (var operation in new[] { "feedback", "reject", "defer", "resume" })
        {
            var request = Request(operation);
            request["proposal_id"] = proposalId;
            request["version"] = first["version"]!.GetValue<int>();
            if (operation == "feedback") request["message"] = "Native transport proof: explain this recommendation.";
            if (operation == "defer") request["seconds"] = 86400;
            view = await client.SendAsync(request);
            nextSequence = view["next_request_sequence"]!.GetValue<long>();
            first = First(view);
            var expected = operation switch { "reject" => "rejected", "defer" => "deferred", _ => "pending" };
            if (first["handling"]?.GetValue<string>() != expected)
                throw new InvalidDataException("Backend handling differs from the acknowledged operation.");
        }
        // Each exchange launched a fresh Python child; a further read verifies
        // persisted state rather than the previous process's in-memory response.
        first = First(await client.SendAsync(Request("snapshot")));
        if (first["feedback"]?.AsArray().Count != 1
            || first["feedback"]?[0]?["status"]?.GetValue<string>() != "saved_not_submitted"
            || first["version"]?.GetValue<int>() != 4)
            throw new InvalidDataException("Backend restart did not preserve proof feedback and decisions.");
        App.Record("backend_bridge_verified", new {
            exchanges = 6, persistedLocalVersion = 4, feedbackSubmittedToAgent = false,
            githubAccess = false, modelCalls = 0
        });
    }
}
