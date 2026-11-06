namespace CampusConnect;

internal static class Program
{
    [STAThread]
    static void Main(string[] args)
    {
        var startInTray = args.Contains("--startup", StringComparer.OrdinalIgnoreCase);
        using var instance = new Mutex(true, "Local\\CampusConnect.Windows", out var created);
        using var wakeEvent = new EventWaitHandle(false, EventResetMode.AutoReset, "Local\\CampusConnect.Restore");
        if (!created)
        {
            // Restore the already-running main window; do not open a second
            // instance or show an extra Windows message/notification.
            if (!startInTray) wakeEvent.Set();
            return;
        }

        var directories = (AppContext.GetData("NATIVE_DLL_SEARCH_DIRECTORIES") as string ?? "")
            .Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries).Prepend(AppContext.BaseDirectory);
        var loaderDirectory = directories.FirstOrDefault(dir => File.Exists(Path.Combine(dir, "WebView2Loader.dll")));
        if (loaderDirectory is not null)
            Microsoft.Web.WebView2.Core.CoreWebView2Environment.SetLoaderDllFolderPath(loaderDirectory);

        ApplicationConfiguration.Initialize();
        var verificationIndex = Array.IndexOf(args, "--verify-ui");
        var verificationPath = verificationIndex >= 0 && verificationIndex + 1 < args.Length ? args[verificationIndex + 1] : null;
        using var window = new MainForm(verificationPath, startInTray);
        // Have a UI-thread marshal target ready before subscribing to the
        // local wake event; Handle creation does not display the window.
        _ = window.Handle;
        using var watcher = new WakeWatcher(wakeEvent, window);
        Application.Run(window);
    }

    private sealed class WakeWatcher : IDisposable
    {
        private readonly RegisteredWaitHandle registration;

        internal WakeWatcher(EventWaitHandle wake, MainForm window)
        {
            registration = ThreadPool.RegisterWaitForSingleObject(wake, (_, _) => {
                if (window.IsDisposed || window.Disposing || !window.IsHandleCreated) return;
                try { window.BeginInvoke((Action)window.RestoreFromSecondLaunch); }
                catch (InvalidOperationException) { /* window is closing */ }
            }, null, Timeout.Infinite, false);
        }

        public void Dispose() => registration.Unregister(null);
    }
}
