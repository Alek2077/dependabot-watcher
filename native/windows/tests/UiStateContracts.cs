using System;
using System.IO;
using System.Reflection;
using System.Threading;

internal static class UiStateContracts
{
    internal static void Run(Assembly assembly)
    {
        Exception? failure = null;
        var thread = new Thread(() => {
            try {
                var clientType = assembly.GetType("WatcherNativeProof.BackendClient", true)!;
                var client = Activator.CreateInstance(clientType, BindingFlags.Instance | BindingFlags.NonPublic,
                    null, new object[] { Array.Empty<string>(), true }, null)!;
                var type = assembly.GetType("WatcherNativeProof.BackendQueueWindow", true)!;
                var window = Activator.CreateInstance(type, BindingFlags.Instance | BindingFlags.NonPublic,
                    null, new object[] { client }, null)!;
                object Field(string name) => type.GetField(name, BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(window)!;
                foreach (var name in new[] { "_outcome", "_detail", "_savedMessages" })
                    Field(name).GetType().GetProperty("Text")!.SetValue(Field(name), "Stale item content");
                var attention = Field("_permissionAttention");
                var visibility = attention.GetType().GetProperty("Visibility")!;
                visibility.SetValue(attention, Enum.Parse(visibility.PropertyType, "Visible"));
                type.GetMethod("SelectProposal", BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(window,null);
                foreach (var name in new[] { "_outcome", "_detail", "_savedMessages" })
                    if ((string)Field(name).GetType().GetProperty("Text")!.GetValue(Field(name))! != "")
                        throw new InvalidDataException("Retired selection kept stale item-specific content.");
                if (visibility.GetValue(attention)!.ToString() != "Collapsed")
                    throw new InvalidDataException("Retired selection kept stale access CTA.");
                // Never Show/initialize: no Loaded event or backend invocation.
            } catch (Exception exception) { failure = exception; }
        });
        thread.SetApartmentState(ApartmentState.STA); thread.Start(); thread.Join();
        if (failure is not null) throw new InvalidDataException("UI state transition contract failed.", failure);
        Console.WriteLine("UI retirement contracts passed without backend initialization.");
    }
}
