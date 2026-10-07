using System;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;

// The console hosts the existing component's registration-free SDK manifest.
// It does not launch that executable, register COM, or duplicate its runtime.
internal sealed class NativeSdkContext : IDisposable
{
    private readonly IntPtr _handle;
    private readonly UIntPtr _cookie;

    internal NativeSdkContext(string executable)
    {
        var context = new ActivationContext {
            Size = (uint)Marshal.SizeOf<ActivationContext>(), Flags = 0x004 | 0x008,
            Source = executable, AssemblyDirectory = Path.GetDirectoryName(executable),
            ResourceName = new IntPtr(1)
        };
        _handle = CreateActCtxW(ref context);
        if (_handle == new IntPtr(-1)) throw new Win32Exception(Marshal.GetLastWin32Error());
        if (!ActivateActCtx(_handle, out _cookie))
        {
            var error = Marshal.GetLastWin32Error();
            ReleaseActCtx(_handle);
            throw new Win32Exception(error);
        }
    }

    public void Dispose()
    {
        try {
            if (!DeactivateActCtx(0, _cookie)) throw new Win32Exception(Marshal.GetLastWin32Error());
        } finally { ReleaseActCtx(_handle); }
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct ActivationContext
    {
        public uint Size;
        public uint Flags;
        public string Source;
        public ushort ProcessorArchitecture;
        public ushort LanguageId;
        public string? AssemblyDirectory;
        public IntPtr ResourceName;
        public string? ApplicationName;
        public IntPtr Module;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
    private static extern IntPtr CreateActCtxW(ref ActivationContext context);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool ActivateActCtx(IntPtr context, out UIntPtr cookie);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool DeactivateActCtx(uint flags, UIntPtr cookie);
    [DllImport("kernel32.dll")]
    private static extern void ReleaseActCtx(IntPtr context);
}
