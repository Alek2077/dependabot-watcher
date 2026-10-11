using System;
using System.IO;
using System.Reflection;
using System.Threading.Tasks;

internal static class InvestigationCoordinationContracts
{
    internal static void Run(Assembly assembly)
    {
        var method = assembly.GetType("WatcherNativeProof.NativeInvestigationCoordination", true)!
            .GetMethod("WaitAsync", BindingFlags.Static | BindingFlags.NonPublic)!;
        Task<bool> Wait(Func<Task> idle, Func<Task?> active, Func<bool> stopped) =>
            (Task<bool>)method.Invoke(null, new object[] { idle, active, stopped })!;
        var finished = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        Task? investigation = null;
        var reads = 0;
        var waiting = Wait(() => {
            if (++reads == 1) investigation = finished.Task;
            return Task.CompletedTask;
        }, () => investigation, () => false);
        if (waiting.IsCompleted) throw new InvalidDataException("Idle presentation allowed an overlapping investigation claim.");
        investigation = null;
        finished.SetResult(true);
        if (!waiting.GetAwaiter().GetResult() || reads != 2)
            throw new InvalidDataException("Investigation completion did not recheck idle ownership.");
        var stopped = false;
        if (Wait(() => { stopped = true; return Task.CompletedTask; }, () => null, () => stopped)
            .GetAwaiter().GetResult())
            throw new InvalidDataException("Shutdown allowed a scheduled claim after an idle wait.");
        if (Wait(() => throw new InvalidOperationException("must not wait"), () => null, () => true)
            .GetAwaiter().GetResult())
            throw new InvalidDataException("Stopped coordination reported ownership.");
        Console.WriteLine("Investigation coordination contracts passed: post-await ownership and shutdown.");
    }
}
