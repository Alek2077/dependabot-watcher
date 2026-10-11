using System;
using System.Threading.Tasks;

namespace WatcherNativeProof;

// Only retries a read-only scope snapshot. It never retries an investigation,
// permission decision or repository action, and failure never grants access.
internal static class NativeScopeRefresh
{
    internal static async Task<bool> RunAsync(Func<Task> refresh, Action<Exception, bool> failed,
        Func<bool> stopped, Func<TimeSpan, Task> delay)
    {
        for (var attempt = 0; attempt < 4 && !stopped(); attempt++)
        {
            try { await refresh(); return !stopped(); }
            catch (Exception exception)
            {
                var retry = attempt < 3 && !stopped();
                failed(exception, retry);
                if (!retry) return false;
                await delay(TimeSpan.FromSeconds(1 << attempt));
            }
        }
        return false;
    }
}
