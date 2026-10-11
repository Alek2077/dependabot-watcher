using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Threading.Tasks;

internal static class ScopeRefreshContracts
{
    internal static void Run(Assembly assembly)
    {
        var method = assembly.GetType("WatcherNativeProof.NativeScopeRefresh", true)!
            .GetMethod("RunAsync", BindingFlags.Static | BindingFlags.NonPublic)!;
        bool Run(Func<Task> refresh, Action<Exception, bool> failed, Func<bool> stopped, Func<TimeSpan, Task> delay) =>
            ((Task<bool>)method.Invoke(null, new object[] { refresh, failed, stopped, delay })!).GetAwaiter().GetResult();
        var reads = 0;
        var delays = new List<double>();
        var failures = new List<bool>();
        Task Wait(TimeSpan duration) { delays.Add(duration.TotalSeconds); return Task.CompletedTask; }
        if (!Run(() => ++reads < 3 ? Task.FromException(new IOException("contended")) : Task.CompletedTask,
            (_, retry) => failures.Add(retry), () => false, Wait)
            || reads != 3 || string.Join(",", delays) != "1,2" || failures.Count != 2 || failures.Contains(false))
            throw new InvalidDataException("A transient scope failure did not recover through bounded read-only retries.");
        reads = 0; delays.Clear(); failures.Clear();
        if (Run(() => { reads++; return Task.FromException(new IOException("unavailable")); },
            (_, retry) => failures.Add(retry), () => false, Wait)
            || reads != 4 || string.Join(",", delays) != "1,2,4" || failures.Count != 4 || failures[3])
            throw new InvalidDataException("Persistent scope failure did not stop with access unavailable.");
        reads = 0;
        var stopped = false;
        if (Run(() => { reads++; return Task.FromException(new IOException("contended")); },
            (_, _) => { }, () => stopped, _ => { stopped = true; return Task.CompletedTask; }) || reads != 1)
            throw new InvalidDataException("Shutdown allowed another scope read.");
        if (Run(() => throw new InvalidOperationException("must not read"), (_, _) => { }, () => true, Wait))
            throw new InvalidDataException("Stopped scope check reported success.");
        stopped = false;
        if (Run(async () => { await Task.Yield(); stopped = true; },
            (_, _) => throw new InvalidDataException("Successful read was treated as failure."), () => stopped, Wait))
            throw new InvalidDataException("An in-flight successful read reported success after shutdown.");
        Console.WriteLine("Scope refresh contracts passed: recovery, exhaustion and shutdown without action replay.");
    }
}
