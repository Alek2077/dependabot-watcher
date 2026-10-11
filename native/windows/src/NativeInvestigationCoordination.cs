using System;
using System.Threading.Tasks;

namespace WatcherNativeProof;

// Presentation busy state is released for permission controls. Investigation
// ownership must therefore be rechecked after every asynchronous idle wait.
internal static class NativeInvestigationCoordination
{
    internal static async Task<bool> WaitAsync(Func<Task> waitForIdle,
        Func<Task?> investigation, Func<bool> stopping)
    {
        while (!stopping())
        {
            if (investigation() is Task running) await running;
            if (stopping()) return false;
            await waitForIdle();
            if (stopping()) return false;
            if (investigation() is null) return true;
        }
        return false;
    }
}
