// LDTF.exe: starts LDTF from the folder it lies in, without a console window, and exits at once.
// runtime\pythonw.exe -X utf8 -m dtf_backup app <arguments of LDTF.exe>   (the tray app; a second start opens the browser)
//
// Built by tools/build_release.py with csc.exe of .NET Framework 4.x (part of every Windows 10/11), so: C# 5 only.
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

static class Launcher
{
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern int MessageBoxW(IntPtr owner, string text, string caption, uint type);

    const uint MB_ICONERROR = 0x10;

    // one argument for CommandLineToArgvW (what Python parses): quotes and the backslashes before them escaped
    static string Quote(string arg)
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
        string dir = AppDomain.CurrentDomain.BaseDirectory;
        string pythonw = Path.Combine(dir, "runtime", "pythonw.exe");
        if (!File.Exists(pythonw))
        {
            MessageBoxW(IntPtr.Zero, "Рядом с LDTF.exe нет папки runtime со встроенным Python.\n\n" +
                        "Распакуйте архив релиза LDTF целиком и запустите LDTF.exe из распакованной папки.",
                        "LDTF", MB_ICONERROR);
            return 1;
        }
        var cmd = new StringBuilder("-X utf8 -m dtf_backup app");
        foreach (string a in args)
            cmd.Append(' ').Append(Quote(a));
        var psi = new ProcessStartInfo(pythonw, cmd.ToString());
        psi.WorkingDirectory = dir;
        psi.UseShellExecute = false;
        try
        {
            Process.Start(psi);
        }
        catch (Exception e)
        {
            MessageBoxW(IntPtr.Zero, "Не удалось запустить LDTF: " + e.Message, "LDTF", MB_ICONERROR);
            return 1;
        }
        return 0;
    }
}
