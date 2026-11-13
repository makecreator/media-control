# -*- coding: utf-8 -*-
"""
自绘标题栏支持：
- 去掉系统标题栏（WS_CAPTION），保留 WS_THICKFRAME（可拉伸边框）
- Win11 圆角 + 阴影（DWM）
- 子类化窗口过程处理 WM_NCHITTEST：
  * 标题栏区域（除按钮外）→ HTCAPTION，实现原生拖拽/双击最大化
  * 窗口四边 → HTLEFT/RIGHT/TOP/BOTTOM 等，保留拉伸
  * 其余 → 交给原窗口过程

注意：窗口过程里绝不调用 tkinter/Tcl（同 taskbar_controls 的教训），
这里只读取主线程缓存的纯数字坐标。
"""

import ctypes
from ctypes import c_void_p, wintypes

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi

GWL_STYLE = -16
GWLP_WNDPROC = -4
WS_CAPTION = 0x00C00000
WM_NCHITTEST = 0x0084
WM_NCLBUTTONDOWN = 0x00A1

HTCLIENT = 1
HTCAPTION = 2
HTLEFT = 10
HTRIGHT = 11
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14
HTBOTTOM = 15
HTBOTTOMLEFT = 16
HTBOTTOMRIGHT = 17

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020

DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2

BORDER = 6  # 拉伸热区宽度（物理像素）

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)

user32.SetWindowLongPtrW.restype = c_void_p
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, c_void_p]
user32.GetWindowLongPtrW.restype = LRESULT
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.CallWindowProcW.restype = LRESULT
user32.CallWindowProcW.argtypes = [c_void_p, wintypes.HWND, wintypes.UINT,
                                   wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                wintypes.UINT]
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]


def _signed16(v):
    return v - 0x10000 if v >= 0x8000 else v


class CustomTitle:
    """去掉系统标题栏并接管命中测试。须在窗口创建后调用。"""

    def __init__(self, hwnd):
        self.hwnd = hwnd
        self._old_proc = None

        self._remove_caption()
        self._subclass()

    # ---------- 样式 ----------
    def _remove_caption(self):
        style = user32.GetWindowLongPtrW(self.hwnd, GWL_STYLE)
        style &= ~WS_CAPTION  # 去标题栏，保留 THICKFRAME/SYSMENU/MINMAXBOX
        user32.SetWindowLongPtrW(self.hwnd, GWL_STYLE, style)
        user32.SetWindowPos(self.hwnd, None, 0, 0, 0, 0,
                            SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER
                            | SWP_NOACTIVATE | SWP_FRAMECHANGED)
        # Win11 圆角
        pref = wintypes.DWORD(DWMWCP_ROUND)
        dwmapi.DwmSetWindowAttribute(wintypes.HWND(self.hwnd),
                                     DWMWA_WINDOW_CORNER_PREFERENCE,
                                     ctypes.byref(pref), ctypes.sizeof(pref))

    # ---------- 子类化 ----------
    def _subclass(self):
        def proc(h, msg, wp, lp):
            if msg == WM_NCHITTEST:
                r = self._hit_test(lp)
                if r is not None:
                    return r
            elif msg == WM_NCLBUTTONDOWN:
                # Tk 的窗口过程会吞掉 NC 按下消息，导致 DefWindowProc 永远拿不到，
                # 系统缩放循环根本不会启动（表现为拉不动边框）。
                # 边框热区直接交给 DefWindowProc 处理。
                if wp in (HTLEFT, HTRIGHT, HTTOP, HTBOTTOM,
                          HTTOPLEFT, HTTOPRIGHT, HTBOTTOMLEFT, HTBOTTOMRIGHT):
                    return user32.DefWindowProcW(h, msg, wp, lp)
            return user32.CallWindowProcW(self._old_proc, h, msg, wp, lp)

        self._proc = WNDPROC(proc)  # 保持引用防 GC
        self._old_proc = user32.SetWindowLongPtrW(
            self.hwnd, GWLP_WNDPROC, ctypes.cast(self._proc, c_void_p))

    def _hit_test(self, lp):
        """返回命中码；None 表示交给系统默认处理

        只处理四边/四角的拉伸热区。标题栏拖拽由 Tk 客户端实现
        （见 media_control._drag_move）：Tk 的窗口过程会吞掉 WM_NCLBUTTONDOWN，
        靠 HTCAPTION + 系统移动循环在无边框窗口上并不可靠。
        """
        sx = _signed16(lp & 0xFFFF)
        sy = _signed16((lp >> 16) & 0xFFFF)
        pt = wintypes.POINT(0, 0)
        if not user32.ClientToScreen(self.hwnd, ctypes.byref(pt)):
            return None
        cr = wintypes.RECT()
        if not user32.GetClientRect(self.hwnd, ctypes.byref(cr)):
            return None
        x = sx - pt.x
        y = sy - pt.y
        w, h = cr.right, cr.bottom
        if w <= 0 or h <= 0:
            return None

        b = BORDER
        left, right, top, bottom = x < b, x >= w - b, y < b, y >= h - b
        if top and left:
            return HTTOPLEFT
        if top and right:
            return HTTOPRIGHT
        if bottom and left:
            return HTBOTTOMLEFT
        if bottom and right:
            return HTBOTTOMRIGHT
        if left:
            return HTLEFT
        if right:
            return HTRIGHT
        if top:
            return HTTOP
        if bottom:
            return HTBOTTOM
        return None

    # ---------- 主线程接口 ----------
    def close(self):
        """还原窗口过程。必须在窗口销毁前调用。"""
        if self._old_proc:
            try:
                user32.SetWindowLongPtrW(self.hwnd, GWLP_WNDPROC, self._old_proc)
            except Exception:
                pass
            self._old_proc = None
