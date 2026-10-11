// LDTF.exe, two jobs:
//   LDTF.exe [arguments]           starts LDTF from the folder it lies in, without a console window:
//                                  runtime\pythonw.exe -X utf8 -m dtf_backup app <arguments>   (the tray app; a second
//                                  start shows the window of the running one)
//   LDTF.exe --window <url> [--pid N]   the window of LDTF: a WebView2 window on the local site (the app opens it),
//                                  one per installation; it closes when the app (process N) ends. Without the WebView2
//                                  Runtime: Microsoft Edge in app mode, else the default browser.
//
// Built by tools/build_release.py with csc.exe of .NET Framework 4.x (part of every Windows 10/11), so: C# 5 only.
// The WebView2 SDK (Microsoft.Web.WebView2, BSD license) lies in webview2\ next to LDTF.exe.
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Win32;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

static class Launcher
{
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern int MessageBoxW(IntPtr owner, string text, string caption, uint type);

    const uint MB_ICONERROR = 0x10;
    public const int REPORTED = 3;   // the app showed its own message about why it could not start

    public static string Dir { get { return AppDomain.CurrentDomain.BaseDirectory; } }

    public static void Error(string text)
    {
        MessageBoxW(IntPtr.Zero, text, "LDTF", MB_ICONERROR);
    }

    // one argument for CommandLineToArgvW (what Python parses): quotes and the backslashes before them escaped
    public static string Quote(string arg)
    {
        if (arg.Length > 0 && arg.IndexOfAny(new[] { ' ', '\t', '"' }) < 0)
            return arg;
        var sb = new StringBuilder("\"");
        int slashes = 0;
        foreach (char c in arg)
        {
            if (c == '\\') { slashes++; continue; }
            if (c == '"') sb.Append('\\', slashes * 2 + 1);
            else sb.Append('\\', slashes);
            slashes = 0;
            sb.Append(c);
        }
        sb.Append('\\', slashes * 2);
        return sb.Append('"').ToString();
    }

    [STAThread]
    static int Main(string[] args)
    {
        if (args.Length >= 2 && args[0] == "--window")
        {
            int pid = 0;
            for (int i = 2; i + 1 < args.Length; i++)
                if (args[i] == "--pid") int.TryParse(args[i + 1], out pid);
            return AppWindow.Run(args[1], pid);
        }
        return Start(args);
    }

    // Explorer runs a program from inside a zip after copying just it into %TEMP%; 7-Zip and WinRAR unpack into %TEMP%
    // too. LDTF must live in a folder of its own: its archives are kept next to it.
    static bool InTemp()
    {
        string temp = Path.GetFullPath(Path.GetTempPath()).TrimEnd('\\') + "\\";
        return Dir.StartsWith(temp, StringComparison.OrdinalIgnoreCase)
            || Dir.IndexOf(".zip\\", StringComparison.OrdinalIgnoreCase) >= 0;
    }

    static int Start(string[] args)
    {
        string pythonw = Path.Combine(Dir, "runtime", "pythonw.exe");
        if (InTemp())
        {
            Error("LDTF запущен прямо из zip-архива (или из временной папки).\n\n" +
                  "Распакуйте архив целиком в постоянную папку — например, C:\\LDTF — и запустите LDTF.exe оттуда. " +
                  "Архивы профилей LDTF хранит рядом с собой, во временной папке они бы пропали.");
            return 1;
        }
        if (!File.Exists(pythonw))
        {
            Error("Рядом с LDTF.exe нет папки runtime со встроенным Python.\n\n" +
                  "Скачайте с GitHub файл LDTF-…-windows-x64.zip (не «Source code»), распакуйте его целиком и " +
                  "запустите LDTF.exe из распакованной папки.");
            return 1;
        }
        var cmd = new StringBuilder("-X utf8 -m dtf_backup app");
        foreach (string a in args)
            cmd.Append(' ').Append(Quote(a));
        var psi = new ProcessStartInfo(pythonw, cmd.ToString());
        psi.WorkingDirectory = Dir;
        psi.UseShellExecute = false;
        Process p;
        try
        {
            p = Process.Start(psi);
        }
        catch (Exception e)
        {
            Error("Не удалось запустить LDTF: " + e.Message);
            return 1;
        }
        // Python that dies right away (damaged files of the app) has no window to say so: say it here. Errors the app
        // itself understands (a busy port, a folder it can't write to) it shows by itself and ends with REPORTED.
        if (p.WaitForExit(6000) && p.ExitCode != 0 && p.ExitCode != REPORTED)
        {
            Error("LDTF не запустился: встроенный Python завершился сразу после старта (код " + p.ExitCode + ").\n\n" +
                  "Похоже, файлы приложения повреждены или распакованы не все. Скачайте LDTF-…-windows-x64.zip с " +
                  "GitHub заново и распакуйте поверх (папка archive с вашими данными останется).");
            return 1;
        }
        return 0;
    }
}

static class AppWindow
{
    [DllImport("user32.dll")] static extern bool IsWindow(IntPtr hWnd);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("user32.dll")] static extern bool AllowSetForegroundWindow(int pid);
    [DllImport("user32.dll")] static extern IntPtr SendMessageW(IntPtr hWnd, int msg, IntPtr wParam, ref CopyData lParam);
    [DllImport("user32.dll")] static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr value);

    [StructLayout(LayoutKind.Sequential)]
    public struct CopyData { public IntPtr dwData; public int cbData; public IntPtr lpData; }
    public const int WM_COPYDATA = 0x004A;

    // %LOCALAPPDATA%\LDTF: the window's place and size, WebView2's data, and which window belongs to which installation
    public static string DataDir
    {
        get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "LDTF"); }
    }

    // FNV-1a of the installation folder (UTF-8, lower case, ending with \): the same key in dtf_backup/window.py
    public static string Key()
    {
        uint h = 2166136261;
        foreach (byte b in Encoding.UTF8.GetBytes(Launcher.Dir.ToLowerInvariant()))
        {
            h ^= b;
            h *= 16777619;
        }
        return h.ToString("x8");
    }

    public static string HandleFile { get { return Path.Combine(DataDir, "windows", Key() + ".txt"); } }

    public static int Run(string url, int pid)
    {
        bool created;
        using (var m = new Mutex(true, "Local\\LDTF-window-" + Key(), out created))
        {
            if (!created)   // another window is starting right now: give it the address once it is up
            {
                for (int i = 0; i < 50 && !SendToExisting(url); i++)
                    Thread.Sleep(100);
                return 0;
            }
            if (SendToExisting(url))
                return 0;
            try
            {
                if (!SetProcessDpiAwarenessContext(new IntPtr(-4)))   // per-monitor v2: sharp at any scale
                    SetProcessDPIAware();
            }
            catch (EntryPointNotFoundException)
            {
                SetProcessDPIAware();
            }
            string wv = Path.Combine(Launcher.Dir, "webview2");
            if (Environment.GetEnvironmentVariable("LDTF_NO_WEBVIEW2") == null
                && File.Exists(Path.Combine(wv, "Microsoft.Web.WebView2.Core.dll")))
            {
                AppDomain.CurrentDomain.AssemblyResolve += delegate(object s, ResolveEventArgs e)
                {
                    string p = Path.Combine(wv, new AssemblyName(e.Name).Name + ".dll");
                    return File.Exists(p) ? Assembly.LoadFrom(p) : null;
                };
                try
                {
                    if (WebViewHost.Available(wv))
                        return WebViewHost.Run(url, pid, wv, m);
                }
                catch (Exception)
                {
                    // no usable WebView2: the fallbacks below
                }
            }
            return Fallback(url);
        }
    }

    // the window of this installation that is already open (its process and handle are in HandleFile)
    static bool SendToExisting(string url)
    {
        string[] parts;
        try
        {
            parts = File.ReadAllText(HandleFile).Trim().Split(' ');
        }
        catch (Exception)
        {
            return false;
        }
        int pid;
        long hwnd;
        if (parts.Length < 2 || !int.TryParse(parts[0], out pid) || !long.TryParse(parts[1], out hwnd))
            return false;
        IntPtr h = new IntPtr(hwnd);
        uint owner;
        if (!IsWindow(h) || GetWindowThreadProcessId(h, out owner) == 0 || owner != (uint)pid)
            return false;
        AllowSetForegroundWindow(pid);
        IntPtr mem = Marshal.StringToHGlobalUni(url);
        try
        {
            var cd = new CopyData { dwData = new IntPtr(1), cbData = (url.Length + 1) * 2, lpData = mem };
            SendMessageW(h, WM_COPYDATA, IntPtr.Zero, ref cd);
        }
        finally
        {
            Marshal.FreeHGlobal(mem);
        }
        return true;
    }

    // Microsoft Edge (part of Windows 10/11) in app mode: a window without tabs and the address bar
    static int Fallback(string url)
    {
        string edge = null;
        foreach (RegistryKey root in new[] { Registry.CurrentUser, Registry.LocalMachine })
        {
            using (var k = root.OpenSubKey(@"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"))
            {
                if (k != null && k.GetValue("") is string && File.Exists((string)k.GetValue("")))
                {
                    edge = (string)k.GetValue("");
                    break;
                }
            }
        }
        try
        {
            if (edge != null && Environment.GetEnvironmentVariable("LDTF_NO_EDGE") == null)
                Process.Start(new ProcessStartInfo(edge, "--app=" + Launcher.Quote(url)) { UseShellExecute = false });
            else
                Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
        }
        catch (Exception e)
        {
            Launcher.Error("Не удалось открыть LDTF: " + e.Message + "\n\nАдрес: " + url);
            return 1;
        }
        return 0;
    }
}

// Everything that touches WebView2 types lives here: this class loads only after AppWindow set up the resolver.
static class WebViewHost
{
    public static bool Available(string wv)
    {
        CoreWebView2Environment.SetLoaderDllFolderPath(wv);
        try
        {
            return !string.IsNullOrEmpty(CoreWebView2Environment.GetAvailableBrowserVersionString());
        }
        catch (WebView2RuntimeNotFoundException)
        {
            return false;
        }
    }

    public static int Run(string url, int pid, string wv, Mutex m)
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new MainForm(url, pid));
        GC.KeepAlive(m);
        return 0;
    }
}

class MainForm : Form
{
    [DllImport("dwmapi.dll")] static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int value, int size);

    readonly WebView2 web = new WebView2();
    readonly string start;
    readonly int pid;
    readonly System.Windows.Forms.Timer watch = new System.Windows.Forms.Timer();
    Uri origin;
    bool dark;

    public MainForm(string url, int pid)
    {
        start = url;
        this.pid = pid;
        origin = new Uri(url);
        Text = "LDTF";
        try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch (Exception) { }
        StartPosition = FormStartPosition.Manual;
        dark = WindowsDark();
        BackColor = dark ? Color.FromArgb(17, 19, 24) : Color.FromArgb(243, 244, 249);
        web.DefaultBackgroundColor = BackColor;
        web.Dock = DockStyle.Fill;
        Controls.Add(web);
        Place();
        watch.Interval = 1500;
        watch.Tick += delegate { if (AppEnded()) Close(); };
    }

    bool AppEnded()
    {
        if (pid <= 0) return false;
        try
        {
            using (var p = Process.GetProcessById(pid)) return p.HasExited;
        }
        catch (ArgumentException)
        {
            return true;
        }
    }

    static bool WindowsDark()
    {
        try
        {
            using (var k = Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"))
            {
                object v = k == null ? null : k.GetValue("AppsUseLightTheme");
                return v is int && (int)v == 0;
            }
        }
        catch (Exception)
        {
            return false;
        }
    }

    void DarkTitle(bool on)
    {
        int v = on ? 1 : 0;
        try { DwmSetWindowAttribute(Handle, 20, ref v, 4); } catch (Exception) { }
    }

    static string PlaceFile { get { return Path.Combine(AppWindow.DataDir, "window.txt"); } }

    // the size and place it had last time, if that is still on a screen; else most of the screen, centered
    void Place()
    {
        Rectangle work = Screen.PrimaryScreen.WorkingArea;   // pixels of the screen: sizes below are scaled to its DPI
        float k = 1f;
        try { using (Graphics g = Graphics.FromHwnd(IntPtr.Zero)) k = Math.Max(1f, g.DpiX / 96f); } catch (Exception) { }
        MinimumSize = new Size(Math.Min((int)(420 * k), work.Width), Math.Min((int)(480 * k), work.Height));
        Rectangle r = new Rectangle(0, 0, Math.Min((int)(1360 * k), work.Width * 4 / 5),
                                    Math.Min((int)(900 * k), work.Height * 9 / 10));
        r.X = work.X + (work.Width - r.Width) / 2;
        r.Y = work.Y + (work.Height - r.Height) / 2;
        bool max = false;
        try
        {
            string[] p = File.ReadAllText(PlaceFile).Trim().Split(' ');
            var saved = new Rectangle(int.Parse(p[0]), int.Parse(p[1]), int.Parse(p[2]), int.Parse(p[3]));
            foreach (Screen s in Screen.AllScreens)
            {
                if (s.WorkingArea.IntersectsWith(saved) && saved.Width >= 300 && saved.Height >= 300)
                {
                    r = saved;
                    max = p.Length > 4 && p[4] == "1";
                    break;
                }
            }
        }
        catch (Exception)
        {
        }
        Bounds = r;
        if (max) WindowState = FormWindowState.Maximized;
    }

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        DarkTitle(dark);
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(AppWindow.HandleFile));
            File.WriteAllText(AppWindow.HandleFile, Process.GetCurrentProcess().Id + " " + Handle.ToInt64());
        }
        catch (Exception)
        {
        }
    }

    protected override async void OnLoad(EventArgs e)
    {
        base.OnLoad(e);
        watch.Start();
        try
        {
            var env = await CoreWebView2Environment.CreateAsync(null, Path.Combine(AppWindow.DataDir, "WebView2"), null);
            await web.EnsureCoreWebView2Async(env);
        }
        catch (Exception ex)
        {
            Launcher.Error("Не удалось открыть окно LDTF (WebView2): " + ex.Message + "\n\nLDTF откроется в браузере.");
            Process.Start(new ProcessStartInfo(start) { UseShellExecute = true });
            Close();
            return;
        }
        CoreWebView2 core = web.CoreWebView2;
        core.Settings.AreDevToolsEnabled = Environment.GetEnvironmentVariable("LDTF_DEVTOOLS") != null;
        core.Settings.IsStatusBarEnabled = false;
        core.NewWindowRequested += delegate(object s, CoreWebView2NewWindowRequestedEventArgs a)
        {
            a.Handled = true;
            Go(a.Uri);
        };
        core.NavigationStarting += delegate(object s, CoreWebView2NavigationStartingEventArgs a)
        {
            if (!Local(a.Uri))
            {
                a.Cancel = true;
                External(a.Uri);
            }
        };
        core.DocumentTitleChanged += delegate { Text = string.IsNullOrEmpty(core.DocumentTitle) ? "LDTF" : core.DocumentTitle; };
        core.WebMessageReceived += delegate(object s, CoreWebView2WebMessageReceivedEventArgs a)
        {
            string msg;
            try { msg = a.TryGetWebMessageAsString(); } catch (Exception) { return; }
            if (msg == "theme:dark" || msg == "theme:light")
            {
                dark = msg == "theme:dark";
                DarkTitle(dark);
            }
            else if (msg == "close")
            {
                Close();
            }
        };
        core.Navigate(start);
    }

    bool Local(string uri)
    {
        Uri u;
        if (!Uri.TryCreate(uri, UriKind.Absolute, out u)) return false;
        if (u.Scheme == "about" || u.Scheme == "data" || u.Scheme == "blob") return true;
        return (u.Scheme == Uri.UriSchemeHttp || u.Scheme == Uri.UriSchemeHttps)
            && string.Equals(u.Host, origin.Host, StringComparison.OrdinalIgnoreCase) && u.Port == origin.Port;
    }

    void Go(string uri)
    {
        if (Local(uri)) web.CoreWebView2.Navigate(uri);
        else External(uri);
    }

    // links to DTF and everything else outside the app: the default browser
    static void External(string uri)
    {
        Uri u;
        if (!Uri.TryCreate(uri, UriKind.Absolute, out u)) return;
        if (u.Scheme != Uri.UriSchemeHttp && u.Scheme != Uri.UriSchemeHttps && u.Scheme != Uri.UriSchemeMailto) return;
        try { Process.Start(new ProcessStartInfo(u.AbsoluteUri) { UseShellExecute = true }); } catch (Exception) { }
    }

    // another start of LDTF while this window is open: it sends the address to show (AppWindow.SendToExisting)
    protected override void WndProc(ref Message m)
    {
        if (m.Msg == AppWindow.WM_COPYDATA)
        {
            var cd = (AppWindow.CopyData)Marshal.PtrToStructure(m.LParam, typeof(AppWindow.CopyData));
            string url = Marshal.PtrToStringUni(cd.lpData);
            if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Normal;
            Activate();
            BringToFront();
            if (!string.IsNullOrEmpty(url) && web.CoreWebView2 != null)
            {
                Uri u;
                if (Uri.TryCreate(url, UriKind.Absolute, out u) && u.Port != origin.Port)
                    origin = u;   // the app started again on another port
                if (u == null || u.PathAndQuery != "/" || web.Source == null || !Local(web.Source.AbsoluteUri))
                    web.CoreWebView2.Navigate(url);
            }
            m.Result = new IntPtr(1);
            return;
        }
        base.WndProc(ref m);
    }

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        watch.Stop();
        try
        {
            Rectangle r = WindowState == FormWindowState.Normal ? Bounds : RestoreBounds;
            Directory.CreateDirectory(AppWindow.DataDir);
            File.WriteAllText(PlaceFile, r.X + " " + r.Y + " " + r.Width + " " + r.Height + " " +
                              (WindowState == FormWindowState.Maximized ? "1" : "0"));
            File.Delete(AppWindow.HandleFile);
        }
        catch (Exception)
        {
        }
        base.OnFormClosing(e);
    }
}
