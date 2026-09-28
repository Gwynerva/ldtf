"""Windows tray icon of LDTF (pure ctypes, no third-party packages).

The message loop runs in the main thread; the HTTP server, sync workers and the scheduler run in background
threads. Left click opens LDTF in the browser, right click shows the menu. The icon's status light follows the
app: green = idle, blue = syncing, red = the last sync of some archive failed.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import queue
import threading
from pathlib import Path
from typing import Any, Callable

from .util import log

BRAND = Path(__file__).resolve().parent / "assets" / "brand"

WM_NULL, WM_DESTROY, WM_CLOSE, WM_QUERYENDSESSION, WM_ENDSESSION = 0x0000, 0x0002, 0x0010, 0x0011, 0x0016
WM_TIMER, WM_LBUTTONUP, WM_RBUTTONUP, WM_USER, WM_APP = 0x0113, 0x0202, 0x0205, 0x0400, 0x8000
WM_TRAY, WM_WAKE = WM_APP + 1, WM_APP + 2
NIN_BALLOONUSERCLICK = WM_USER + 5
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_USER, NIIF_LARGE_ICON = 0x4, 0x20
MF_STRING, MF_GRAYED, MF_CHECKED, MF_SEPARATOR = 0x0, 0x1, 0x8, 0x800
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x2, 0x100, 0x80
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x10
SM_CXSMICON, SM_CXICON = 49, 11

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", w.UINT), ("style", w.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", w.HINSTANCE), ("hIcon", w.HICON), ("hCursor", w.HANDLE),
                ("hbrBackground", w.HBRUSH), ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR),
                ("hIconSm", w.HICON)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD), ("Data4", w.BYTE * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT), ("uFlags", w.UINT),
                ("uCallbackMessage", w.UINT), ("hIcon", w.HICON), ("szTip", w.WCHAR * 128), ("dwState", w.DWORD),
                ("dwStateMask", w.DWORD), ("szInfo", w.WCHAR * 256), ("uVersion", w.UINT),
                ("szInfoTitle", w.WCHAR * 64), ("dwInfoFlags", w.DWORD), ("guidItem", GUID),
                ("hBalloonIcon", w.HICON)]


def _api() -> tuple[Any, Any, Any]:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
    user32.RegisterClassExW.restype = w.ATOM
    user32.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID]
    user32.CreateWindowExW.restype = w.HWND
    user32.LoadImageW.argtypes = [w.HINSTANCE, w.LPCWSTR, w.UINT, ctypes.c_int, ctypes.c_int, w.UINT]
    user32.LoadImageW.restype = w.HANDLE
    user32.AppendMenuW.argtypes = [w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR]
    user32.TrackPopupMenu.argtypes = [w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.LPVOID]
    user32.TrackPopupMenu.restype = ctypes.c_int
    user32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    user32.SetTimer.argtypes = [w.HWND, ctypes.c_size_t, w.UINT, w.LPVOID]
    user32.SetTimer.restype = ctypes.c_size_t
    user32.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
    user32.DispatchMessageW.restype = LRESULT
    user32.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
    user32.DestroyIcon.argtypes = [w.HICON]
    user32.SetMenuDefaultItem.argtypes = [w.HMENU, w.UINT, w.UINT]
    user32.DestroyMenu.argtypes = [w.HMENU]
    user32.SetForegroundWindow.argtypes = [w.HWND]
    user32.DestroyWindow.argtypes = [w.HWND]
    user32.RegisterWindowMessageW.argtypes = [w.LPCWSTR]
    user32.RegisterWindowMessageW.restype = w.UINT
    shell32.Shell_NotifyIconW.argtypes = [w.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    shell32.Shell_NotifyIconW.restype = w.BOOL
    kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]
    kernel32.GetModuleHandleW.restype = w.HMODULE
    return user32, shell32, kernel32


def available() -> bool:
    try:
        _api()
        return True
    except (AttributeError, OSError):  # not Windows
        return False


class MenuItem:
    def __init__(self, text: str, action: Callable[[], Any] | None = None, checked: bool = False,
                 enabled: bool = True, default: bool = False):
        self.text, self.action, self.checked, self.enabled, self.default = text, action, checked, enabled, default


SEP = None


class Tray:
    """`status()` -> {"state": idle|sync|error, "tip": str}; `menu()` -> [MenuItem | SEP]; `on_click` opens the app."""

    def __init__(self, status: Callable[[], dict], menu: Callable[[], list], on_click: Callable[[], Any],
                 on_end_session: Callable[[], Any] | None = None):
        self.status_cb, self.menu_cb, self.on_click = status, menu, on_click
        self.on_end_session = on_end_session
        self.user32, self.shell32, self.kernel32 = _api()
        self.hwnd: Any = None
        self._proc = WNDPROC(self._wndproc)   # keep a reference: the window calls it
        self._icons: dict[str, Any] = {}
        self._state = ""
        self._tip = ""
        self._notes: queue.Queue = queue.Queue()
        self._balloon_action: Callable[[], Any] | None = None
        self._taskbar_created = 0
        self.closing = threading.Event()

    # ------------------------------------------------------------------ public (any thread)
    def notify(self, title: str, text: str, on_click: Callable[[], Any] | None = None) -> None:
        self._notes.put((title, text, on_click))
        self._post(WM_WAKE)

    def refresh(self) -> None:
        self._post(WM_WAKE)

    def close(self) -> None:
        self.closing.set()
        self._post(WM_CLOSE)

    def _post(self, msg: int) -> None:
        if self.hwnd:
            self.user32.PostMessageW(self.hwnd, msg, 0, 0)

    # ------------------------------------------------------------------ loop (main thread)
    def run(self) -> None:
        u = self.user32
        hinst = self.kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = "LDTFTrayWindow"
        u.RegisterClassExW(ctypes.byref(wc))
        # a hidden top-level window (message-only windows miss the TaskbarCreated broadcast)
        self.hwnd = u.CreateWindowExW(0, "LDTFTrayWindow", "LDTF", 0, 0, 0, 0, 0, None, None, hinst, None)
        if not self.hwnd:
            raise OSError(f"CreateWindowEx: {ctypes.get_last_error()}")
        self._taskbar_created = u.RegisterWindowMessageW("TaskbarCreated")
        self._add_icon()
        u.SetTimer(self.hwnd, 1, 2000, None)
        msg = w.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))

    # ------------------------------------------------------------------ icon
    def _icon(self, state: str) -> Any:
        if state not in self._icons:
            name = {"sync": "ldtf-sync.ico", "error": "ldtf-error.ico"}.get(state, "ldtf.ico")
            size = self.user32.GetSystemMetrics(SM_CXSMICON) or 16
            self._icons[state] = self.user32.LoadImageW(None, str(BRAND / name), IMAGE_ICON, size, size, LR_LOADFROMFILE)
        return self._icons[state]

    def _nid(self, flags: int) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = flags
        nid.uCallbackMessage = WM_TRAY
        return nid

    def _add_icon(self) -> None:
        st = self._safe_status()
        self._state, self._tip = st.get("state", "idle"), st.get("tip", "LDTF")[:127]
        nid = self._nid(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        nid.hIcon = self._icon(self._state)
        nid.szTip = self._tip
        if not self.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            log.warning("[трей] не удалось добавить значок")

    def _update(self) -> None:
        st = self._safe_status()
        state, tip = st.get("state", "idle"), st.get("tip", "LDTF")[:127]
        if state == self._state and tip == self._tip:
            return
        self._state, self._tip = state, tip
        nid = self._nid(NIF_ICON | NIF_TIP)
        nid.hIcon = self._icon(state)
        nid.szTip = tip
        self.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def _show_notes(self) -> None:
        while True:
            try:
                title, text, action = self._notes.get_nowait()
            except queue.Empty:
                return
            nid = self._nid(NIF_INFO)
            nid.szInfoTitle = title[:63]
            nid.szInfo = text[:255]
            size = self.user32.GetSystemMetrics(SM_CXICON) or 32
            nid.hBalloonIcon = self.user32.LoadImageW(None, str(BRAND / "ldtf.ico"), IMAGE_ICON, size, size,
                                                      LR_LOADFROMFILE)
            nid.dwInfoFlags = NIIF_USER | NIIF_LARGE_ICON
            self._balloon_action = action
            self.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def _safe_status(self) -> dict:
        try:
            return self.status_cb() or {}
        except Exception:  # noqa: BLE001
            log.exception("[трей] статус")
            return {"state": "idle", "tip": "LDTF"}

    # ------------------------------------------------------------------ menu
    def _show_menu(self) -> None:
        u = self.user32
        try:
            items = self.menu_cb()
        except Exception:  # noqa: BLE001
            log.exception("[трей] меню")
            return
        hmenu = u.CreatePopupMenu()
        actions: dict[int, Callable[[], Any]] = {}
        for i, it in enumerate(items, start=1):
            if it is SEP:
                u.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
                continue
            flags = MF_STRING | (MF_CHECKED if it.checked else 0) | (0 if it.enabled else MF_GRAYED)
            u.AppendMenuW(hmenu, flags, i, it.text)
            if it.action:
                actions[i] = it.action
            if it.default:
                u.SetMenuDefaultItem(hmenu, i, 0)
        pt = w.POINT()
        u.GetCursorPos(ctypes.byref(pt))
        u.SetForegroundWindow(self.hwnd)   # otherwise the menu does not close on an outside click
        cmd = u.TrackPopupMenu(hmenu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY, pt.x, pt.y, 0, self.hwnd, None)
        u.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        u.DestroyMenu(hmenu)
        if cmd in actions:
            self._run(actions[cmd])

    @staticmethod
    def _run(action: Callable[[], Any]) -> None:
        # actions may block (e.g. stopping the app waits for syncs): never block the message loop
        threading.Thread(target=action, name="tray-action", daemon=True).start()

    # ------------------------------------------------------------------ window procedure
    def _wndproc(self, hwnd: Any, msg: int, wparam: int, lparam: int) -> int:
        try:
            if msg == WM_TRAY:
                event = lparam & 0xFFFF
                if event == WM_LBUTTONUP:
                    self._run(self.on_click)
                elif event == WM_RBUTTONUP:
                    self._show_menu()
                elif event == NIN_BALLOONUSERCLICK and self._balloon_action:
                    self._run(self._balloon_action)
                return 0
            if msg == WM_TIMER or msg == WM_WAKE:
                self._update()
                self._show_notes()
                return 0
            if msg == self._taskbar_created and self._taskbar_created:
                self._add_icon()   # Explorer restarted: the icon must be registered again
                return 0
            if msg == WM_QUERYENDSESSION:
                return 1
            if msg == WM_ENDSESSION and wparam and self.on_end_session:
                self.on_end_session()   # Windows is logging off: save progress now, the process ends right after
                return 0
            if msg == WM_CLOSE:
                self.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid(0)))
                self.user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                for h in self._icons.values():
                    if h:
                        self.user32.DestroyIcon(h)
                self.user32.PostQuitMessage(0)
                return 0
        except Exception:  # noqa: BLE001 - an exception here would kill the message loop
            log.exception("[трей] обработка сообщения")
        return self.user32.DefWindowProcW(hwnd, msg, wparam, lparam)
