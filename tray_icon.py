# -*- coding: utf-8 -*-
"""
系统托盘图标（Shell_NotifyIcon）
- 独立线程 + message-only 窗口接收托盘消息，绝不在回调里调用 tkinter
- 单击 / 双击 / 右键事件入队，由 Tk 主线程轮询消费后再执行界面动作
  （原因同 taskbar_controls：在 explorer 触发的窗口过程里调用 Tcl 会导致
   "PyEval_RestoreThread: ... GIL is released" 崩溃）
"""

import collections
import ctypes
import threading
import traceback
from ctypes import byref, wintypes

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32

# ---------------- 常量 ----------------
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_TIMER = 0x0113
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
TIMER_SINGLE_CLICK = 1

NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
HWND_MESSAGE = ctypes.c_void_p(0xFFFFFFFFFFFFFFFD)  # (HWND)-3

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256),
                ("uVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", GUID),
                ("hBalloonIcon", wintypes.HICON)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                ("hbmColor", wintypes.HBITMAP)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


# ---------------- 签名（64 位句柄必须显式声明） ----------------
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                   wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.RegisterClassW.restype = wintypes.ATOM
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFO),
                                   wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   wintypes.HANDLE, wintypes.DWORD]
gdi32.SelectObject.restype = wintypes.HANDLE
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
gdi32.CreateSolidBrush.argtypes = [wintypes.DWORD]
gdi32.CreatePen.restype = wintypes.HPEN
gdi32.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.CreateBitmap.restype = wintypes.HBITMAP
gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT,
                               wintypes.UINT, ctypes.c_void_p]
gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.Ellipse.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, ctypes.c_int]
gdi32.Polygon.argtypes = [wintypes.HDC, ctypes.POINTER(POINT), ctypes.c_int]
user32.CreateIconIndirect.restype = wintypes.HICON
user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.DestroyIcon.argtypes = [wintypes.HICON]
UINT_PTR = ctypes.c_size_t  # ctypes.wintypes 里没有 UINT_PTR
user32.SetTimer.restype = UINT_PTR
user32.SetTimer.argtypes = [wintypes.HWND, UINT_PTR, wintypes.UINT, wintypes.LPVOID]
user32.KillTimer.argtypes = [wintypes.HWND, UINT_PTR]
user32.GetDoubleClickTime.restype = wintypes.UINT
shell32.Shell_NotifyIconW.restype = wintypes.BOOL
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
user32.RegisterWindowMessageW.restype = wintypes.UINT
# 所有收 HWND/HANDLE 的函数都必须声明 argtypes，否则 64 位句柄会被截断成 32 位
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.GetMessageW.restype = ctypes.c_int
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]


def make_icon(size=32, accent=(0x1A, 0x73, 0xE8)):
    """画一个「蓝色圆底 + 白色播放三角」的托盘图标，返回 HICON"""
    hdc_screen = user32.GetDC(None)
    memdc = gdi32.CreateCompatibleDC(hdc_screen)
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = size
    bmi.bmiHeader.biHeight = -size
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0
    bits = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(memdc, byref(bmi), 0, byref(bits), None, 0)
    if not hbmp or not bits:
        gdi32.DeleteDC(memdc)
        user32.ReleaseDC(None, hdc_screen)
        return None
    gdi32.SelectObject(memdc, hbmp)
    ctypes.memset(bits, 0, size * size * 4)  # 全透明

    r, g, b = accent
    colorref = r | (g << 8) | (b << 16)  # COLORREF 是 0x00BBGGRR
    brush = gdi32.CreateSolidBrush(colorref)
    pen = gdi32.CreatePen(8, 0, 0)  # PS_NULL
    old_brush = gdi32.SelectObject(memdc, brush)
    old_pen = gdi32.SelectObject(memdc, pen)
    gdi32.Ellipse(memdc, 1, 1, size, size)

    # 白色播放三角
    white = gdi32.CreateSolidBrush(0xFFFFFF)
    gdi32.SelectObject(memdc, white)
    s = size
    pts = (POINT * 3)(POINT(int(s * 0.38), int(s * 0.28)),
                      POINT(int(s * 0.72), int(s * 0.50)),
                      POINT(int(s * 0.38), int(s * 0.72)))
    gdi32.Polygon(memdc, pts, 3)

    # GDI 不写 alpha：按亮度反推覆盖率
    buf = (ctypes.c_ubyte * (size * size * 4)).from_address(bits.value)
    for i in range(0, size * size * 4, 4):
        lum = max(buf[i], buf[i + 1], buf[i + 2])
        buf[i + 3] = min(255, int(lum * 1.15))

    mask = gdi32.CreateBitmap(size, size, 1, 1, None)
    ii = ICONINFO(True, 0, 0, mask, hbmp)
    hicon = user32.CreateIconIndirect(byref(ii))

    gdi32.SelectObject(memdc, old_brush)
    gdi32.SelectObject(memdc, old_pen)
    gdi32.DeleteObject(white)
    gdi32.DeleteObject(brush)
    gdi32.DeleteObject(pen)
    gdi32.DeleteObject(mask)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(None, hdc_screen)
    return hicon


class TrayIcon:
    """托盘图标。on_single/on_double/on_menu 都在 Tk 主线程被调用。"""

    def __init__(self, root, tooltip="媒体控制", on_single=None,
                 on_double=None, on_menu=None):
        self.root = root
        self.on_single = on_single
        self.on_double = on_double
        self.on_menu = on_menu
        self._events = collections.deque()
        self._hwnd = None
        self._hicon = None
        self._closed = False
        self._poll_id = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._timer_on = False
        self._suppress_up = False
        self._tip = tooltip
        self._nid = NOTIFYICONDATAW()
        self._nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        self._nid.uID = 1
        self._nid.uCallbackMessage = WM_TRAY
        self._nid.szTip = tooltip

        self._taskbar_created_msg = user32.RegisterWindowMessageW("TaskbarCreated")
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)
        self._poll()

    # ---------- 托盘线程 ----------
    def _run(self):
        try:
            self._hicon = make_icon(32)
            self._proc = WNDPROC(self._wndproc)  # 保持引用
            hinst = kernel32.GetModuleHandleW(None)
            wc = WNDCLASSW()
            wc.lpfnWndProc = self._proc
            wc.hInstance = hinst
            wc.lpszClassName = "MediaControlTrayWnd"
            if not user32.RegisterClassW(byref(wc)):
                err = kernel32.GetLastError()
                if err != 1410:  # 1410 = 类已注册
                    raise ctypes.WinError(err)
            self._hwnd = user32.CreateWindowExW(
                0, "MediaControlTrayWnd", "MediaControlTray", 0,
                0, 0, 0, 0, HWND_MESSAGE, None, hinst, None)
            if not self._hwnd:
                raise ctypes.WinError(kernel32.GetLastError())
            self._nid.hWnd = self._hwnd
            self._nid.hIcon = self._hicon
            self._add_icon()
        except Exception:
            traceback.print_exc()
        finally:
            self._ready.set()

        if not self._hwnd:
            return
        msg = wintypes.MSG()
        while True:
            r = user32.GetMessageW(byref(msg), None, 0, 0)
            if r in (0, -1):
                break
            user32.TranslateMessage(byref(msg))
            user32.DispatchMessageW(byref(msg))

    def _add_icon(self):
        with self._lock:
            self._nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            self._nid.szTip = self._tip
            ok = shell32.Shell_NotifyIconW(NIM_ADD, byref(self._nid))
        if not ok:
            print("Shell_NotifyIcon(NIM_ADD) 失败", flush=True)

    def _wndproc(self, h, msg, wp, lp):
        try:
            if msg == self._taskbar_created_msg:
                # explorer 重启后图标会丢失，重新添加
                self._add_icon()
                return 0
            if msg == WM_TRAY:
                if lp == WM_LBUTTONUP:
                    if self._suppress_up:
                        self._suppress_up = False
                    elif not self._timer_on:
                        user32.SetTimer(h, TIMER_SINGLE_CLICK,
                                        user32.GetDoubleClickTime(), None)
                        self._timer_on = True
                    return 0
                if lp == WM_LBUTTONDBLCLK:
                    if self._timer_on:
                        user32.KillTimer(h, TIMER_SINGLE_CLICK)
                        self._timer_on = False
                    self._suppress_up = True
                    self._events.append(("double", None))
                    return 0
                if lp in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    pt = POINT()
                    user32.GetCursorPos(byref(pt))
                    self._events.append(("menu", (pt.x, pt.y)))
                    return 0
                return 0
            if msg == WM_TIMER and wp == TIMER_SINGLE_CLICK:
                user32.KillTimer(h, TIMER_SINGLE_CLICK)
                self._timer_on = False
                self._events.append(("single", None))
                return 0
            if msg == WM_CLOSE:
                user32.DestroyWindow(h)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            traceback.print_exc()
        return user32.DefWindowProcW(h, msg, wp, lp)

    # ---------- 主线程侧 ----------
    def _poll(self):
        if self._closed:
            return
        try:
            while self._events:
                kind, arg = self._events.popleft()
                try:
                    if kind == "single" and self.on_single:
                        self.on_single()
                    elif kind == "double" and self.on_double:
                        self.on_double()
                    elif kind == "menu" and self.on_menu:
                        self.on_menu(arg[0], arg[1])
                except Exception:
                    traceback.print_exc()
        finally:
            if not self._closed:
                self._poll_id = self.root.after(60, self._poll)

    def set_tooltip(self, text):
        text = (text or "").replace("\n", " ")
        if len(text) > 120:
            text = text[:117] + "..."
        if text == self._tip:
            return
        self._tip = text
        if not self._hwnd:
            return
        with self._lock:
            self._nid.uFlags = NIF_TIP
            self._nid.szTip = text
            shell32.Shell_NotifyIconW(NIM_MODIFY, byref(self._nid))

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._poll_id is not None:
            try:
                self.root.after_cancel(self._poll_id)
            except Exception:
                pass
            self._poll_id = None
        try:
            if self._hwnd:
                with self._lock:
                    self._nid.uFlags = 0
                    shell32.Shell_NotifyIconW(NIM_DELETE, byref(self._nid))
                user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
        except Exception:
            traceback.print_exc()
        if self._hicon:
            try:
                user32.DestroyIcon(self._hicon)
            except Exception:
                pass
            self._hicon = None
