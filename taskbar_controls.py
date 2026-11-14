# -*- coding: utf-8 -*-
"""
Windows 任务栏缩略图工具栏（Taskbar Thumbnail Toolbar）
通过 ITaskbarList3::ThumbBarAddButtons 在任务栏预览缩略图上添加
上一首 / 播放暂停 / 下一首 三个按钮，图标用 GDI 现画（Segoe Fluent Icons）。
"""

import ctypes
import collections
import traceback
from ctypes import c_void_p, wintypes, byref

# 注意：这里必须用 WinDLL() 创建私有实例而不是 windll 缓存——windll 全局共享，
# 多个模块对同名函数（如 CreateDIBSection）设置不同 argtypes 会互相覆盖：
# 本模块和 tray_icon 各自定义了 BITMAPINFO，谁后 import 谁的签名生效，
# 另一个模块的调用就会报 "expected LP_BITMAPINFO instance" 直接失败
user32 = ctypes.WinDLL("user32")
gdi32 = ctypes.WinDLL("gdi32")
ole32 = ctypes.WinDLL("ole32")

# ---------------- 常量 ----------------
WM_COMMAND = 0x0111
THBN_CLICKED = 0x1800
GWLP_WNDPROC = -4
THB_ICON = 0x0002
THB_TOOLTIP = 0x0004
THB_FLAGS = 0x0008
THBF_ENABLED = 0x0000
COINIT_APARTMENTTHREADED = 0x2
DT_CENTER, DT_VCENTER, DT_SINGLELINE = 0x1, 0x4, 0x20
TRANSPARENT = 1

# vtable 索引（IUnknown 3 个 + ITaskbarList/2 若干）
IDX_HRINIT = 3
IDX_ADDBUTTONS = 15
IDX_UPDATEBUTTONS = 16
IDX_SETTOOLTIP = 19


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


class THUMBBUTTON(ctypes.Structure):
    _fields_ = [("dwMask", wintypes.DWORD),
                ("iId", wintypes.UINT),
                ("iBitmap", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 260),
                ("dwFlags", wintypes.DWORD)]


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


# ---------------- 显式声明 64 位句柄签名 ----------------
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFO),
                                   wintypes.UINT, ctypes.POINTER(c_void_p),
                                   wintypes.HANDLE, wintypes.DWORD]
gdi32.CreateFontW.restype = wintypes.HANDLE
gdi32.CreateFontW.argtypes = [ctypes.c_int] * 5 + [wintypes.DWORD] * 8 + [ctypes.c_wchar_p]
gdi32.CreateBitmap.restype = wintypes.HBITMAP
gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT,
                               wintypes.UINT, c_void_p]
gdi32.SelectObject.restype = wintypes.HANDLE
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.DWORD]
gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.DrawTextW.restype = ctypes.c_int
user32.DrawTextW.argtypes = [wintypes.HDC, ctypes.c_wchar_p, ctypes.c_int,
                             ctypes.POINTER(wintypes.RECT), wintypes.UINT]
user32.CreateIconIndirect.restype = wintypes.HICON
user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
user32.SetWindowLongPtrW.restype = c_void_p
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, c_void_p]
user32.CallWindowProcW.restype = ctypes.c_ssize_t
user32.CallWindowProcW.argtypes = [c_void_p, wintypes.HWND, wintypes.UINT,
                                   wintypes.WPARAM, wintypes.LPARAM]
user32.SetPropW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, wintypes.HANDLE]

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


def make_icon(glyph, size=32, color=(45, 45, 45)):
    """用 Segoe Fluent Icons 画一个字形图标，返回 HICON"""
    hdc_screen = user32.GetDC(None)
    memdc = gdi32.CreateCompatibleDC(hdc_screen)

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = size
    bmi.bmiHeader.biHeight = -size  # 自上而下
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0  # BI_RGB

    bits = c_void_p()
    hbmp = gdi32.CreateDIBSection(memdc, byref(bmi), 0, byref(bits), None, 0)
    if not hbmp or not bits:
        gdi32.DeleteDC(memdc)
        user32.ReleaseDC(None, hdc_screen)
        return None
    gdi32.SelectObject(memdc, hbmp)

    # 白底（之后用亮度反推 alpha）
    ctypes.memset(bits, 0xFF, size * size * 4)

    font = gdi32.CreateFontW(-int(size * 0.68), 0, 0, 0, 400,
                             0, 0, 0, 1, 0, 0, 5, 0, "Segoe Fluent Icons")
    old_font = gdi32.SelectObject(memdc, font)
    gdi32.SetTextColor(memdc, 0x000000)
    gdi32.SetBkMode(memdc, TRANSPARENT)
    rect = wintypes.RECT(0, 0, size, size)
    user32.DrawTextW(memdc, glyph, -1, byref(rect), DT_CENTER | DT_VCENTER | DT_SINGLELINE)

    # DIB 像素：BGRA。GDI 画图不写 alpha，这里按“白底黑字”反推覆盖率
    buf = (ctypes.c_ubyte * (size * size * 4)).from_address(bits.value)
    r, g, b = color
    n = size * size * 4
    for i in range(0, n, 4):
        lum = (buf[i] + buf[i + 1] + buf[i + 2]) // 3
        buf[i] = b
        buf[i + 1] = g
        buf[i + 2] = r
        buf[i + 3] = 255 - lum

    mask = gdi32.CreateBitmap(size, size, 1, 1, None)
    ii = ICONINFO(True, 0, 0, mask, hbmp)
    hicon = user32.CreateIconIndirect(byref(ii))

    gdi32.SelectObject(memdc, old_font)
    gdi32.DeleteObject(font)
    gdi32.DeleteObject(mask)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(None, hdc_screen)
    return hicon


def _vfunc(ptr, index, restype, argtypes):
    """从 COM 对象 vtable 取方法"""
    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    return proto(vtable[index])


CLSID_TaskbarList = GUID(0x56FDF344, 0xFD6D, 0x11D0,
                         (0x95, 0x8A, 0x00, 0x60, 0x97, 0xC9, 0xA0, 0x90))
# 来自 SDK shobjidl_core.h: MIDL_INTERFACE("ea1afb91-9e28-4b86-90e9-9e9f8a5eefaf")
IID_ITaskbarList3 = GUID(0xEA1AFB91, 0x9E28, 0x4B86,
                         (0x90, 0xE9, 0x9E, 0x9F, 0x8A, 0x5E, 0xEF, 0xAF))


class TaskbarControls:
    """在任务栏缩略图上挂三个按钮"""

    ID_PREV, ID_PLAY, ID_NEXT = 101, 102, 103

    def __init__(self, hwnd, root, on_prev, on_play_pause, on_next, playing=False):
        self.hwnd = hwnd
        self.root = root
        self.on_prev = on_prev
        self.on_play_pause = on_play_pause
        self.on_next = on_next
        self._old_proc = None
        self._queue = collections.deque()  # 窗口过程间传递的点击队列
        self._poll_id = None
        self._closed = False
        self.last_add_hr = None

        ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)

        self._p = c_void_p()
        hr = ole32.CoCreateInstance(byref(CLSID_TaskbarList), None, 1,
                                    byref(IID_ITaskbarList3), byref(self._p))
        if hr != 0 or not self._p:
            raise RuntimeError(f"CoCreateInstance 失败: 0x{hr & 0xFFFFFFFF:08X}")
        _vfunc(self._p, IDX_HRINIT, ctypes.c_long, [])(self._p)

        self.icons = {
            "prev": make_icon("\uE100"),
            "play": make_icon("\uE768"),
            "pause": make_icon("\uE769"),
            "next": make_icon("\uE101"),
        }
        self._playing = None
        self._buttons = (THUMBBUTTON * 3)()
        self._fill(playing)
        self._add_buttons()
        self._subclass()

    # ---------- 按钮数据 ----------
    def _fill(self, playing):
        specs = [
            (self.ID_PREV, self.icons["prev"], "上一首"),
            (self.ID_PLAY, self.icons["pause" if playing else "play"], "播放 / 暂停"),
            (self.ID_NEXT, self.icons["next"], "下一首"),
        ]
        for i, (bid, hicon, tip) in enumerate(specs):
            b = self._buttons[i]
            b.dwMask = THB_ICON | THB_TOOLTIP | THB_FLAGS
            b.iId = bid
            b.iBitmap = 0
            b.hIcon = hicon
            b.szTip = tip
            b.dwFlags = THBF_ENABLED
        self._playing = playing

    def _add_buttons(self):
        f = _vfunc(self._p, IDX_ADDBUTTONS, ctypes.c_long,
                   [wintypes.HWND, wintypes.UINT, ctypes.POINTER(THUMBBUTTON)])
        hr = f(self._p, self.hwnd, 3, self._buttons)
        self.last_add_hr = hr
        if hr != 0:
            raise RuntimeError(f"ThumbBarAddButtons 失败: 0x{hr & 0xFFFFFFFF:08X}")

    def readd(self):
        """窗口收进托盘再唤回后，任务栏按钮会被 explorer 销毁重建，
        原来挂在上面的缩略图按钮随之丢失，需要重新添加。

        幂等：按钮还在（Add 返回失败）时退化为 UpdateButtons 刷新图标状态。
        在 Tk 主线程调用；任务栏按钮重建需要一点时间，调用方应延迟几百 ms。
        """
        if self._closed:
            return None
        try:
            f = _vfunc(self._p, IDX_ADDBUTTONS, ctypes.c_long,
                       [wintypes.HWND, wintypes.UINT,
                        ctypes.POINTER(THUMBBUTTON)])
            hr = f(self._p, self.hwnd, 3, self._buttons)
            self.last_add_hr = hr
            if hr != 0:
                self._update_all()
            return hr
        except Exception:
            return None

    def _update_all(self):
        try:
            f = _vfunc(self._p, IDX_UPDATEBUTTONS, ctypes.c_long,
                       [wintypes.HWND, wintypes.UINT,
                        ctypes.POINTER(THUMBBUTTON)])
            f(self._p, self.hwnd, 3, self._buttons)
        except Exception:
            pass

    def set_playing(self, playing):
        """播放状态变化时更新中间按钮图标"""
        if playing == self._playing or not self._buttons:
            return
        self._buttons[1].hIcon = self.icons["pause" if playing else "play"]
        self._playing = playing
        try:
            f = _vfunc(self._p, IDX_UPDATEBUTTONS, ctypes.c_long,
                       [wintypes.HWND, wintypes.UINT, ctypes.POINTER(THUMBBUTTON)])
            f(self._p, self.hwnd, 1, ctypes.cast(byref(self._buttons[1]),
                                                  ctypes.POINTER(THUMBBUTTON)))
        except Exception:
            pass

    # ---------- 点击消息 ----------
    # 注意：窗口过程里绝对不能调用 tkinter/Tcl（root.after、控件操作等）。
    # 该回调是 explorer 发消息时在 Tk 的 Tcl 事件循环内部被调用的，ctypes 会为回调
    # 建立线程状态，而 _tkinter 调用 Tcl 前会恢复它自己保存的主线程状态，两者冲突会
    # 触发 "PyEval_RestoreThread: ... GIL is released" 并 fail-fast 崩溃。
    # 所以这里只做纯 Python 入队，由 Tk 主线程的轮询去消费。
    def _on_click(self, bid):
        fn = {self.ID_PREV: self.on_prev,
              self.ID_PLAY: self.on_play_pause,
              self.ID_NEXT: self.on_next}.get(bid)
        if not fn:
            return
        try:
            fn()
        except Exception:
            traceback.print_exc()

    def _drain_clicks(self):
        """在 Tk 主线程里消费窗口过程记下的点击"""
        if self._closed:
            return
        try:
            while self._queue:
                self._on_click(self._queue.popleft())
        except Exception:
            traceback.print_exc()
        finally:
            if not self._closed:
                self._poll_id = self.root.after(50, self._drain_clicks)

    def _subclass(self):
        def proc(h, msg, wp, lp):
            if msg == WM_COMMAND and ((wp >> 16) & 0xFFFF) == THBN_CLICKED:
                # 只入队，不调用任何 Tcl/tkinter 接口
                try:
                    self._queue.append(wp & 0xFFFF)
                except Exception:
                    pass
                return 0
            return user32.CallWindowProcW(self._old_proc, h, msg, wp, lp)

        self._proc = WNDPROC(proc)
        self._old_proc = user32.SetWindowLongPtrW(
            self.hwnd, GWLP_WNDPROC, ctypes.cast(self._proc, c_void_p))
        self._poll_id = self.root.after(50, self._drain_clicks)

    def close(self):
        """还原窗口过程、停掉轮询。必须在窗口销毁前调用。"""
        if self._closed:
            return
        self._closed = True
        if self._poll_id is not None:
            try:
                self.root.after_cancel(self._poll_id)
            except Exception:
                pass
            self._poll_id = None
        if self._old_proc:
            try:
                user32.SetWindowLongPtrW(self.hwnd, GWLP_WNDPROC,
                                         self._old_proc)
            except Exception:
                pass
            self._old_proc = None
        self._queue.clear()
