using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json;
using Microsoft.Win32.SafeHandles;

// Uses the Windows process-loopback API described in Microsoft's ApplicationLoopback sample.
// The target process is never injected, modified or suspended.
namespace MeetingAudio;

static class Program
{
    static volatile bool stopping;
    static readonly object outputLock = new();
    static void Emit(object value)
    {
        lock (outputLock) { Console.WriteLine(JsonSerializer.Serialize(value)); Console.Out.Flush(); }
    }
    static void Check(int hr) { if (hr < 0) Marshal.ThrowExceptionForHR(hr); }

    [MTAThread]
    static int Main(string[] args)
    {
        try
        {
            if (args.Length != 2 || args[0] != "--pid" || !uint.TryParse(args[1], out uint pid) || pid == 0)
                throw new ArgumentException("Usage: MeetingAudio --pid <process-id>");
            using var target = Process.GetProcessById((int)pid);
            Console.CancelKeyPress += (_, e) => { e.Cancel = true; stopping = true; };
            _ = Task.Run(() => { Console.ReadLine(); stopping = true; });
            return Capture(pid, target);
        }
        catch (Exception e)
        {
            Emit(new { type = "error", message = e.Message, hresult = $"0x{e.HResult:X8}" });
            return 1;
        }
    }

    static int Capture(uint pid, Process target)
    {
        var parameters = new ActivationParameters { ActivationType = 1, ProcessId = pid, LoopbackMode = 0 };
        IntPtr blob = Marshal.AllocHGlobal(Marshal.SizeOf<ActivationParameters>());
        IActivateAudioInterfaceAsyncOperation? operation = null;
        IAudioClient? client = null;
        IAudioCaptureClient? capture = null;
        try
        {
            Marshal.StructureToPtr(parameters, blob, false);
            var prop = new PropVariant { Type = 65, BlobSize = Marshal.SizeOf<ActivationParameters>(), BlobData = blob };
            var completion = new Completion();
            Guid iid = typeof(IAudioClient).GUID;
            Check(ActivateAudioInterfaceAsync("VAD\\Process_Loopback", ref iid, ref prop, completion, out operation));
            if (!completion.Done.Wait(TimeSpan.FromSeconds(12)))
                throw new TimeoutException("Process audio activation did not complete in 12 seconds.");
            Check(completion.HResult);
            client = (IAudioClient)(completion.AudioClient ?? throw new InvalidOperationException("No audio interface returned."));
            // Fixed shared PCM format; Windows performs sample-rate conversion.
            var format = new WaveFormat { Tag = 1, Channels = 2, SampleRate = 48000, BytesPerSecond = 192000, BlockAlign = 4, Bits = 16, ExtraSize = 0 };
            const uint loopback = 0x00020000, eventCallback = 0x00040000, autoConvert = 0x80000000;
            Check(client.Initialize(0, loopback | eventCallback | autoConvert, 200000, 0, ref format, IntPtr.Zero));
            using var signal = new AutoResetEvent(false);
            Check(client.SetEventHandle(signal.SafeWaitHandle.DangerousGetHandle()));
            Guid captureId = typeof(IAudioCaptureClient).GUID;
            Check(client.GetService(ref captureId, out object service));
            capture = (IAudioCaptureClient)service;
            Check(client.Start());
            Emit(new { type = "ready", pid, sample_rate = 48000, channels = 2, sample_format = "s16le" });
            while (!stopping)
            {
                if (target.HasExited) throw new InvalidOperationException("The selected meeting process exited. Select the restarted process and begin again.");
                signal.WaitOne(100);
                Check(capture.GetNextPacketSize(out uint size));
                while (size > 0)
                {
                    Check(capture.GetBuffer(out IntPtr data, out uint frames, out uint flags, out ulong devicePosition, out ulong qpcPosition));
                    try
                    {
                        var pcm = new byte[checked((int)frames * 4)];
                        if ((flags & 2) == 0 && data != IntPtr.Zero) Marshal.Copy(data, pcm, 0, pcm.Length);
                        Emit(new { type = "audio", frames, flags, qpc_100ns = qpcPosition, pcm = Convert.ToBase64String(pcm) });
                    }
                    finally { Check(capture.ReleaseBuffer(frames)); }
                    Check(capture.GetNextPacketSize(out size));
                }
            }
            Check(client.Stop());
            Emit(new { type = "stopped" });
            return 0;
        }
        finally
        {
            if (capture != null && Marshal.IsComObject(capture)) Marshal.ReleaseComObject(capture);
            if (client != null && Marshal.IsComObject(client)) Marshal.ReleaseComObject(client);
            if (operation != null && Marshal.IsComObject(operation)) Marshal.ReleaseComObject(operation);
            Marshal.FreeHGlobal(blob);
        }
    }

    [DllImport("Mmdevapi.dll", ExactSpelling = true, CharSet = CharSet.Unicode)]
    static extern int ActivateAudioInterfaceAsync(string device, ref Guid iid, ref PropVariant activation, IActivateAudioInterfaceCompletionHandler completion, out IActivateAudioInterfaceAsyncOperation operation);

    [StructLayout(LayoutKind.Sequential)]
    struct ActivationParameters { public int ActivationType; public uint ProcessId; public int LoopbackMode; }
    [StructLayout(LayoutKind.Explicit, Size = 24)]
    struct PropVariant
    {
        [FieldOffset(0)] public ushort Type;
        [FieldOffset(8)] public int BlobSize;
        [FieldOffset(16)] public IntPtr BlobData;
    }
    [StructLayout(LayoutKind.Sequential, Pack = 2)]
    struct WaveFormat
    {
        public ushort Tag, Channels;
        public uint SampleRate, BytesPerSecond;
        public ushort BlockAlign, Bits, ExtraSize;
    }

    [ComVisible(true), ClassInterface(ClassInterfaceType.None)]
    sealed class Completion : IActivateAudioInterfaceCompletionHandler, IAgileObject
    {
        public readonly ManualResetEventSlim Done = new(false);
        public int HResult;
        public object? AudioClient;
        public int ActivateCompleted(IActivateAudioInterfaceAsyncOperation operation)
        {
            try { int result = operation.GetActivateResult(out int hr, out object client); HResult = result < 0 ? result : hr; AudioClient = client; }
            catch (Exception e) { HResult = e.HResult; }
            finally { Done.Set(); }
            return 0;
        }
    }

    [ComImport, Guid("94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAgileObject { }
    [ComImport, Guid("72A22D78-CDE4-431D-B8CC-843A71199B6D"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IActivateAudioInterfaceAsyncOperation
    {
        [PreserveSig] int GetActivateResult(out int hr, [MarshalAs(UnmanagedType.IUnknown)] out object activated);
    }
    [ComImport, Guid("41D949AB-9862-444A-80F6-C261334DA5EB"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IActivateAudioInterfaceCompletionHandler
    {
        [PreserveSig] int ActivateCompleted(IActivateAudioInterfaceAsyncOperation operation);
    }
    [ComImport, Guid("1CB9AD4C-DBFA-4C32-B178-C2F568A703B2"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioClient
    {
        [PreserveSig] int Initialize(int mode, uint flags, long duration, long periodicity, ref WaveFormat format, IntPtr session);
        [PreserveSig] int GetBufferSize(out uint size);
        [PreserveSig] int GetStreamLatency(out long latency);
        [PreserveSig] int GetCurrentPadding(out uint padding);
        [PreserveSig] int IsFormatSupported(int mode, IntPtr format, out IntPtr closest);
        [PreserveSig] int GetMixFormat(out IntPtr format);
        [PreserveSig] int GetDevicePeriod(out long def, out long minimum);
        [PreserveSig] int Start();
        [PreserveSig] int Stop();
        [PreserveSig] int Reset();
        [PreserveSig] int SetEventHandle(IntPtr handle);
        [PreserveSig] int GetService(ref Guid iid, [MarshalAs(UnmanagedType.IUnknown)] out object service);
    }
    [ComImport, Guid("C8ADBD64-E71E-48A0-A4DE-185C395CD317"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IAudioCaptureClient
    {
        [PreserveSig] int GetBuffer(out IntPtr data, out uint frames, out uint flags, out ulong devicePosition, out ulong qpcPosition);
        [PreserveSig] int ReleaseBuffer(uint frames);
        [PreserveSig] int GetNextPacketSize(out uint size);
    }
}
