# -*- coding: utf-8 -*-
"""
Win11 独立媒体控制窗口（可拉伸）
- 显示当前系统正在播放的媒体（标题 / 作者 / 专辑封面 / 来源应用）
- 上一首 / 播放暂停 / 下一首
- 窗口可自由拉伸，布局自适应
依赖: pip install winrt-runtime winrt-Windows.Media.Control winrt-Windows.Foundation winrt-Windows.Storage.Streams
"""

import asyncio
import base64
import ctypes
import json
import os
import re
import sys
import threading
import tkinter as tk
from tkinter import font as tkfont

from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus,
)
import winrt.windows.foundation.collections  # noqa: F401  get_sessions() 运行时需要


def _wrap(op):
    """把 winrt 的 IAsyncOperation 包装成协程，供 run_coroutine_threadsafe 使用"""
    async def run():
        return await op
    return run()


def get_manager_sync(loop):
    fut = asyncio.run_coroutine_threadsafe(_wrap(MediaManager.request_async()), loop)
    return fut.result(timeout=10)


def config_path():
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "media_control_config.json")


def load_config():
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f)
    except Exception:
        pass


GEOMETRY_RE = re.compile(r"^(\d+)x(\d+)(?:([+-]\d+)([+-]\d+))?$")
DEFAULT_SIZE = (316, 235)


def virtual_screen():
    """整个虚拟桌面（多显示器合并）的坐标范围，坐标体系与 tkinter 一致"""
    try:
        u = ctypes.windll.user32
        # 76/77 = X/YVIRTUALSCREEN, 78/79 = CXVIRTUALSCREEN/CYVIRTUALSCREEN
        return (u.GetSystemMetrics(76), u.GetSystemMetrics(77),
                u.GetSystemMetrics(78), u.GetSystemMetrics(79))
    except Exception:
        return None


def is_on_screen(x, y, w, h):
    """窗口是否还有足够可见部分落在屏幕内（防止换显示器后窗口跑到屏幕外）"""
    vs = virtual_screen()
    if not vs:
        return True
    vx, vy, vw, vh = vs
    # 需要至少 100px 宽 / 40px 高的标题栏区域在可见范围内
    if x + w - 100 < vx or x + 100 > vx + vw:
        return False
    if y + h - 40 < vy or y + 40 > vy + vh:
        return False
    return True


def restore_geometry(root, cfg):
    """按配置恢复窗口大小与位置；尺寸或位置不合法时回退到默认"""
    geo = str(cfg.get("geometry", "") or "")
    m = GEOMETRY_RE.match(geo)
    if not m:
        root.geometry(f"{DEFAULT_SIZE[0]}x{DEFAULT_SIZE[1]}")
        return
    w = max(int(m.group(1)), 300)
    h = max(int(m.group(2)), 170)
    if m.group(3) is None or m.group(4) is None:
        root.geometry(f"{w}x{h}")
        return
    x, y = int(m.group(3)), int(m.group(4))
    if not is_on_screen(x, y, w, h):
        # 保存的位置已经不在任何屏幕里，只恢复大小
        root.geometry(f"{w}x{h}")
        return
    root.geometry(f"{w}x{h}{x:+d}{y:+d}")


class MediaControlApp:
    POLL_MS = 800

    def __init__(self, root: tk.Tk, loop, manager):
        self.root = root
        self.loop = loop
        self.manager = manager
        self.session = None
        self.art_img = None  # 保持引用防止被 GC
        self._art_key = None
        self.pinned = False
        # 多 App 会话切换：None 表示自动（优先正在播放的会话）；
        # 用户点过 › 箭头后固定到该 App，直到其会话消失
        self._pinned_app_id = None
        self._app_ids = []          # 最近一次轮询到的全部会话 app_id
        self._current_app_id = None
        self._switch_shown = False  # 切换按钮当前是否已显示

        cfg = load_config()
        self.cfg = cfg
        self.title_size = max(9, min(26, int(cfg.get("title_size", 10))))
        self.opacity = max(40, min(100, int(cfg.get("opacity", 100))))
        if self.opacity < 100:
            root.attributes("-alpha", self.opacity / 100.0)

        root.title("媒体控制")
        root.configure(bg="#f9f9f9")
        root.minsize(300, 170)
        # 默认窗口大小/位置；若配置里存有上次关闭时的几何信息则沿用
        restore_geometry(root, cfg)

        # 记录正常状态下的几何，供最小化/关闭（此时已非 normal）时保存
        self._last_geo = None
        root.bind("<Configure>", self._cache_geometry)
        # 最小化/还原后重新映射时补刷一次命中区域
        root.bind("<Map>", lambda e: self.root.after(100, self._refresh_hit_regions)
                  if e.widget is self.root else None)

        # 让整体在拉伸时跟随缩放
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        self._build_ui()
        # Ctrl+滚轮 调节字号
        root.bind("<Control-MouseWheel>",
                  lambda e: self.change_size(1 if e.delta > 0 else -1))
        self.tick()

        # 任务栏缩略图按钮：等窗口第一次显示、真正有任务栏按钮后再挂
        self._taskbar = None
        # 自绘标题栏（去系统标题栏，接管拖拽/拉伸命中）
        self._ctitle = None
        # 透明度弹出面板
        self._op_win = None
        # 托盘图标（启动即驻留，窗口默认隐藏）
        self.tray = None
        self._init_tray()
        root.withdraw()

    def _init_tray(self):
        try:
            import tray_icon
            self.tray = tray_icon.TrayIcon(
                self.root, tooltip="媒体控制",
                on_single=self.show_window,
                on_double=self.on_play_pause,
                on_menu=self._show_tray_menu,
            )
        except Exception as e:
            self.tray = None
            print(f"托盘图标初始化失败: {e}", file=sys.stderr)

    # ---------------- 显示 / 隐藏 ----------------
    def show_window(self):
        try:
            self.root.deiconify()
            # 必须先真正映射出窗口，wm_frame() 才能拿到带标题栏的 wrapper 句柄
            self.root.update_idletasks()
        except Exception:
            pass
        # 第一次显示后去掉系统标题栏（自绘标题栏）
        if self._ctitle is None:
            try:
                import custom_title
                hwnd = int(self.root.wm_frame(), 16)
                self._ctitle = custom_title.CustomTitle(hwnd)
            except Exception as e:
                print(f"自绘标题栏初始化失败: {e}", file=sys.stderr)
        try:
            self.root.lift()
            self.root.focus_force()
            if not self.pinned:
                # 短暂置顶，确保从托盘唤出时不会被别的窗口压住
                self.root.attributes("-topmost", True)
                self.root.after(150, lambda: self.root.attributes("-topmost", False))
        except Exception:
            pass
        self._refresh_hit_regions()
        self.root.after(80, self._refresh_hit_regions)
        if self._taskbar is None:
            self.root.after(350, self._init_taskbar)

    def _refresh_hit_regions(self):
        """兼容旧调用：命中区域现在只处理边框，无需刷新任何缓存"""
        return

    # ---------------- 标题栏拖拽（客户端实现） ----------------
    def _drag_start(self, event):
        self._drag_off = (event.x_root - self.root.winfo_x(),
                          event.y_root - self.root.winfo_y())

    def _drag_move(self, event):
        off = getattr(self, "_drag_off", None)
        if not off:
            return
        self.root.geometry(f"+{event.x_root - off[0]}+{event.y_root - off[1]}")

    def _toggle_min(self):
        self._close_opacity_popup()
        self.root.iconify()

    def hide_window(self):
        """关闭按钮 -> 收进托盘（不退出）；最小化仍为正常任务栏最小化"""
        self._close_opacity_popup()
        self._save_geometry()
        self.root.withdraw()

    # ---------------- 透明度 ----------------
    def _toggle_opacity_popup(self):
        if self._op_win is not None and self._op_win.winfo_exists():
            self._close_opacity_popup()
            return
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        frm = tk.Frame(win, bg="#f9f9f9", highlightthickness=1,
                       highlightbackground="#c9c9c9")
        frm.pack(fill="both", expand=True)
        tk.Label(frm, text="窗口透明度", bg="#f9f9f9", fg="#6d6d6d",
                 font=self.tool_font).grid(row=0, column=0, sticky="w",
                                           padx=10, pady=(8, 0))
        val = tk.IntVar(value=self.opacity)
        pct = tk.Label(frm, text=f"{self.opacity}%", bg="#f9f9f9", fg="#1a1a1a",
                       font=self.tool_font)
        pct.grid(row=0, column=1, sticky="e", padx=10, pady=(8, 0))
        scale = tk.Scale(frm, from_=40, to=100, orient="horizontal",
                         variable=val, showvalue=False, length=150,
                         bg="#f9f9f9", highlightthickness=0,
                         troughcolor="#dcdcdc", activebackground="#1a73e8",
                         command=lambda v: self._set_opacity(int(v), pct))
        scale.grid(row=1, column=0, columnspan=2, padx=10, pady=(0, 8))
        self._op_win = win
        self._op_scale = scale

        # 点击滑槽直接跳到点击位置（默认只 ±1，体验差）；点手柄仍走默认拖拽
        def _trough_jump(ev, s=scale):
            try:
                if s.identify(ev.x, ev.y) in ("trough1", "trough2"):
                    x40 = s.coords(40)[0]
                    x100 = s.coords(100)[0]
                    if x100 <= x40:
                        return None
                    v = 40 + (ev.x - x40) / (x100 - x40) * 60
                    v = max(40, min(100, int(round(v))))
                    s.set(v)
                    return "break"
            except Exception:
                pass
            return None

        scale.bind("<Button-1>", _trough_jump)

        # 定位到透明度按钮正下方
        self.root.update_idletasks()
        bx, by = self.opacity_btn.winfo_rootx(), self.opacity_btn.winfo_rooty()
        x = min(max(bx - 60, 0),
                self.root.winfo_screenwidth() - win.winfo_reqwidth() - 8)
        y = by + self.opacity_btn.winfo_height() + 6
        win.geometry(f"+{x}+{y}")
        win.deiconify()
        win.focus_force()
        win.bind("<FocusOut>", lambda e: self._close_opacity_popup())
        win.bind("<Escape>", lambda e: self._close_opacity_popup())

    def _close_opacity_popup(self):
        if self._op_win is not None:
            try:
                self._op_win.destroy()
            except Exception:
                pass
            self._op_win = None

    def _set_opacity(self, value, pct_label=None):
        self.opacity = max(40, min(100, int(value)))
        try:
            self.root.attributes("-alpha", self.opacity / 100.0)
        except Exception:
            pass
        self.opacity_btn.config(text=f"{self.opacity}%")
        if pct_label is not None:
            pct_label.config(text=f"{self.opacity}%")
        self.cfg["opacity"] = self.opacity
        save_config(self.cfg)

    def _cache_geometry(self, event):
        """记住正常状态下的窗口大小/位置（最小化前先存好）"""
        if event.widget is not self.root:
            return
        try:
            if self.root.state() != "normal":
                return
            w, h = event.width, event.height
            if w < 10 or h < 10:
                return
            m = re.search(r"([+-]\d+[+-]\d+)$", self.root.geometry())
            self._last_geo = f"{w}x{h}{m.group(1)}" if m else f"{w}x{h}"
        except Exception:
            pass
        # 同步自绘标题栏的命中区域（窗口位置/大小变了）
        self._refresh_hit_regions()

    def _menu_owner_hwnd(self):
        """弹菜单需要一个真实的顶层窗口句柄（message-only 窗口不能当所有者）"""
        for getter in (lambda: int(self.root.wm_frame(), 16), self.root.winfo_id):
            try:
                h = getter()
                if h:
                    return h
            except Exception:
                continue
        return 0

    def _show_tray_menu(self, x, y):
        """原生 Win32 弹出菜单。

        不用 tk.Menu.tk_popup：那套 grab 循环在托盘场景下点击菜单项只收起菜单、
        不触发命令（实测 tk_popup 正常返回但 command 从未执行），而且回调里
        销毁窗口会让 grab 循环永远不返回。
        原生 TrackPopupMenu 是同步返回的，选中项直接拿命令 ID，可靠得多。
        """
        u = ctypes.windll.user32
        u.CreatePopupMenu.restype = ctypes.c_void_p
        u.AppendMenuW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                  ctypes.c_size_t, ctypes.c_wchar_p]
        u.TrackPopupMenu.restype = ctypes.c_int
        u.TrackPopupMenu.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                                     ctypes.c_void_p]
        u.DestroyMenu.argtypes = [ctypes.c_void_p]
        u.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        u.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                   ctypes.c_size_t, ctypes.c_ssize_t]

        MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
        TPM_RETURNCMD, TPM_RIGHTBUTTON = 0x0100, 0x0002

        hmenu = u.CreatePopupMenu()
        if not hmenu:
            return
        owner = self._menu_owner_hwnd()
        try:
            u.AppendMenuW(hmenu, MF_STRING, 1, "显示窗口")
            u.AppendMenuW(hmenu, MF_STRING, 2, "播放 / 暂停")
            u.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            u.AppendMenuW(hmenu, MF_STRING, 3, "退出")
            try:
                u.SetForegroundWindow(owner)
            except Exception:
                pass
            cmd = u.TrackPopupMenu(hmenu, TPM_RETURNCMD | TPM_RIGHTBUTTON,
                                   int(x), int(y), 0, owner, None)
            # 点菜单外部关闭时需要这条空消息，否则菜单可能“粘”住
            try:
                u.PostMessageW(owner, 0x0000, 0, 0)
            except Exception:
                pass
        finally:
            u.DestroyMenu(hmenu)

        if cmd == 1:
            self.show_window()
        elif cmd == 2:
            self.on_play_pause()
        elif cmd == 3:
            self.quit_app()

    def _init_taskbar(self):
        try:
            import taskbar_controls
            hwnd = int(self.root.wm_frame(), 16)
            self._taskbar = taskbar_controls.TaskbarControls(
                hwnd, self.root,
                on_prev=self.on_prev,
                on_play_pause=self.on_play_pause,
                on_next=self.on_next,
            )
        except Exception as e:
            self._taskbar = None
            print(f"任务栏按钮初始化失败: {e}", file=sys.stderr)

    # ---------------- UI ----------------
    def _build_ui(self):
        ts = self.title_size
        self.big_font = tkfont.Font(family="Microsoft YaHei UI", size=ts)
        self.sub_font = tkfont.Font(family="Microsoft YaHei UI", size=max(ts - 4, 8))
        self.app_font = tkfont.Font(family="Microsoft YaHei UI", size=max(ts - 5, 8))
        self.btn_font = tkfont.Font(family="Segoe UI Symbol", size=ts + 3)
        self.tool_font = tkfont.Font(family="Microsoft YaHei UI", size=9)

        # 标题栏分两行：
        #   第一行（top）：左侧来源应用名，右侧 置顶 / 最小化 / 关闭
        #   第二行（tools）：关闭按钮下方一行的 透明度% / A− / A+ / 多 App 切换 ›
        # 拖拽由 Tk 客户端实现（_drag_start/_drag_move）：无边框窗口下
        # 依赖 Win32 的 HTCAPTION 移动循环会被 Tk 吞掉 NC 消息，不可靠
        top = tk.Frame(self.root, bg="#f9f9f9")
        top.grid(row=0, column=0, sticky="ew", padx=10, pady=(6, 0))
        self.title_bar = top

        tools = tk.Frame(self.root, bg="#f9f9f9")
        tools.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 2))
        self.tools_bar = tools

        self.app_label = tk.Label(
            top, text="没有正在播放的媒体", anchor="w",
            bg="#f9f9f9", fg="#8a8a8a", font=self.app_font,
        )
        self.app_label.pack(side="left")

        # 标题栏任意空白/文字处都能拖动窗口
        for w in (top, tools, self.app_label):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

        def _hover(label, normal_fg, hover_fg, normal_bg="#f9f9f9",
                   hover_bg="#e8e8e8"):
            def _enter(e):
                label.config(fg=hover_fg, bg=hover_bg)

            def _leave(e):
                fg = normal_fg() if callable(normal_fg) else normal_fg
                label.config(fg=fg, bg=normal_bg)

            label.bind("<Enter>", _enter)
            label.bind("<Leave>", _leave)

        # 关闭（最右）：悬停红底，点击收进托盘
        self.close_btn = tk.Label(
            top, text="\ue8bb", cursor="hand2", width=3,
            bg="#f9f9f9", fg="#5a5a5a", font=("Segoe Fluent Icons", 9),
        )
        self.close_btn.pack(side="right", padx=(2, 0))
        self.close_btn.bind("<Button-1>", lambda e: self.hide_window())
        _hover(self.close_btn, "#5a5a5a", "#ffffff", "#f9f9f9", "#e81123")

        # 最小化
        self.min_btn = tk.Label(
            top, text="\ue921", cursor="hand2", width=3,
            bg="#f9f9f9", fg="#5a5a5a", font=("Segoe Fluent Icons", 9),
        )
        self.min_btn.pack(side="right", padx=(2, 0))
        self.min_btn.bind("<Button-1>", lambda e: self._toggle_min())
        _hover(self.min_btn, "#5a5a5a", "#1a1a1a")

        # 置顶
        self.pin_btn = tk.Label(
            top, text="📌", cursor="hand2",
            bg="#f9f9f9", fg="#c0c0c0", font=("Segoe UI Emoji", 10),
        )
        self.pin_btn.pack(side="right", padx=(8, 2))
        self.pin_btn.bind("<Button-1>", self._toggle_pin)
        _hover(self.pin_btn,
               lambda: "#1a73e8" if self.pinned else "#c0c0c0", "#1a1a1a")

        # ---- 第二行：贴在关闭按钮下方，右对齐 ----
        # 多 App 切换（›）：有多个媒体会话时才显示（见 _apply_state），
        # 点击循环切换当前控制的 App。放在最右侧，紧挨关闭按钮下方
        self.switch_btn = tk.Label(
            tools, text="›", cursor="hand2",
            bg="#f9f9f9", fg="#9a9a9a", font=("Segoe UI Symbol", 11, "bold"),
        )
        self.switch_btn.bind("<Button-1>", lambda e: self._cycle_session())
        _hover(self.switch_btn, "#9a9a9a", "#1a1a1a")

        # 透明度（显示当前百分比）
        self.opacity_btn = tk.Label(
            tools, text=f"{self.opacity}%", cursor="hand2", width=5,
            bg="#f9f9f9", fg="#9a9a9a", font=self.tool_font,
        )
        self.opacity_btn.pack(side="right", padx=(2, 0))
        self.opacity_btn.bind("<Button-1>", lambda e: self._toggle_opacity_popup())
        _hover(self.opacity_btn, "#9a9a9a", "#1a1a1a")

        self.plus_btn = tk.Label(
            tools, text="A+", cursor="hand2", width=3,
            bg="#f9f9f9", fg="#9a9a9a", font=self.tool_font,
        )
        self.plus_btn.pack(side="right", padx=2)
        self.plus_btn.bind("<Button-1>", lambda e: self.change_size(1))
        _hover(self.plus_btn, "#9a9a9a", "#1a1a1a")

        self.minus_btn = tk.Label(
            tools, text="A−", cursor="hand2", width=3,
            bg="#f9f9f9", fg="#9a9a9a", font=self.tool_font,
        )
        self.minus_btn.pack(side="right", padx=2)
        self.minus_btn.bind("<Button-1>", lambda e: self.change_size(-1))
        _hover(self.minus_btn, "#9a9a9a", "#1a1a1a")

        # 中部：封面 + 文本
        body = tk.Frame(self.root, bg="#f9f9f9")
        body.grid(row=2, column=0, sticky="nsew", padx=16, pady=8)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        self.art_label = tk.Label(body, text="📕", bg="#f9f9f9",
                                  font=("Segoe UI Emoji", 28))
        self.art_label.grid(row=0, column=0, sticky="w", padx=(0, 12))

        # 文本区单独成帧，换行宽度按它自己的实际宽度计算，避免文字被裁
        text_frame = tk.Frame(body, bg="#f9f9f9")
        text_frame.grid(row=0, column=1, sticky="nsew")
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)

        def _on_text_resize(event):
            wl = max(event.width - 2, 80)
            self.title_label.config(wraplength=wl)
            self.sub_label.config(wraplength=wl)
        text_frame.bind("<Configure>", _on_text_resize)

        self.title_label = tk.Label(
            text_frame, text="—", anchor="w", justify="left",
            bg="#f9f9f9", fg="#1a1a1a", font=self.big_font,
        )
        self.title_label.grid(row=0, column=0, sticky="ew")

        self.sub_label = tk.Label(
            text_frame, text="", anchor="w", justify="left",
            bg="#f9f9f9", fg="#6d6d6d", font=self.sub_font,
        )
        self.sub_label.grid(row=1, column=0, sticky="ew")

        # 底部：控制按钮
        controls = tk.Frame(self.root, bg="#f9f9f9")
        controls.grid(row=3, column=0, sticky="ew", pady=(0, 12))

        self.btn_prev = self._make_btn(controls, "⏮", self.on_prev)
        self.btn_play = self._make_btn(controls, "⏸", self.on_play_pause)
        self.btn_next = self._make_btn(controls, "⏭", self.on_next)

    def _make_btn(self, parent, glyph, cmd):
        btn = tk.Label(
            parent, text=glyph, cursor="hand2",
            bg="#f9f9f9", fg="#333333", font=self.btn_font,
            padx=18, pady=6,
        )
        btn.pack(side="left", expand=True)
        btn.bind("<Button-1>", lambda e: cmd())
        btn.bind("<Enter>", lambda e: btn.config(fg="#000000"))
        btn.bind("<Leave>", lambda e: btn.config(fg="#333333"))
        return btn

    def _toggle_pin(self, _event=None):
        self.pinned = not self.pinned
        self.root.attributes("-topmost", self.pinned)
        self.pin_btn.config(fg="#1a73e8" if self.pinned else "#c0c0c0")

    def change_size(self, delta):
        self.title_size = max(9, min(26, self.title_size + delta))
        self.big_font.config(size=self.title_size)
        self.sub_font.config(size=max(self.title_size - 4, 8))
        self.app_font.config(size=max(self.title_size - 5, 8))
        self.btn_font.config(size=self.title_size + 3)
        self.cfg["title_size"] = self.title_size
        save_config(self.cfg)

    def current_geometry(self):
        """当前窗口几何（大小+位置），形如 316x235+130+130"""
        self.root.update_idletasks()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        # 最大化/最小化时不用当前状态覆盖用户正常状态下的位置
        if self.root.state() != "normal":
            return None
        # tk 的 geometry() 给出的是窗口框左上角坐标，与设置时用的一致，不会逐次漂移
        m = re.search(r"([+-]\d+[+-]\d+)$", self.root.geometry())
        if m:
            return f"{w}x{h}{m.group(1)}"
        return f"{w}x{h}"

    def _save_geometry(self):
        # 记住窗口大小和位置（最小化/隐藏时状态已非 normal，用缓存的正常状态几何）
        try:
            geo = self.current_geometry() or getattr(self, "_last_geo", None)
            if geo:
                self.cfg["geometry"] = geo
            save_config(self.cfg)
        except Exception:
            pass

    def quit_app(self):
        """托盘菜单退出：清理用户可见资源后直接结束进程。

        不能在菜单命令回调里 root.destroy()——tk_popup 的内部 grab 循环会
        永远不返回，进程卡死退不出（窗口和托盘都没了但进程还活着）；
        延迟销毁也不可行（命令执行期间 grab_current 检测不到 popup）。
        托盘图标/缩略图按钮/窗口子类化都已还原、配置已保存，
        直接 os._exit 是托盘应用的标准做法。
        """
        self._close_opacity_popup()
        self._save_geometry()
        # 先还原窗口过程，避免销毁过程中回调进已失效的 Python 对象
        try:
            if self._taskbar:
                self._taskbar.close()
        except Exception:
            pass
        try:
            if self._ctitle:
                self._ctitle.close()
        except Exception:
            pass
        try:
            if self.tray:
                self.tray.close()
        except Exception:
            pass
        os._exit(0)

    # ---------------- 异步任务投递 ----------------
    def _submit(self, coro, callback=None):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)

        def _done(f):
            if not f.cancelled():
                exc = f.exception()
                if exc is None:
                    if callback:
                        self.root.after(0, callback, f.result())
                else:
                    # 不再静默吞错，把错误显示到界面上便于排查
                    msg = f"读取媒体状态出错: {exc}"
                    print(msg, file=sys.stderr)
                    self.root.after(0, self.title_label.config, {"text": msg})
        if callback:
            fut.add_done_callback(_done)
        else:
            # 即使没有回调也要取一次异常，否则失败会被静默丢弃
            def _log(f):
                if f.cancelled():
                    return
                exc = f.exception()
                if exc is not None:
                    print(f"媒体控制操作失败: {exc}", file=sys.stderr)
            fut.add_done_callback(_log)

    # ---------------- 状态轮询 ----------------
    def tick(self):
        self._submit(self._get_state(), self._apply_state)
        self.root.after(self.POLL_MS, self.tick)

    async def _get_state(self):
        sessions = self.manager.get_sessions()
        if not sessions:
            return None

        # 收集全部会话，供多 App 切换使用
        entries = []
        for s in sessions:
            try:
                entries.append((s.source_app_user_model_id or "", s))
            except Exception:
                continue

        # 用户点过 › 箭头后固定在某个 App；该 App 会话消失则自动回退
        target = None
        pinned = getattr(self, "_pinned_app_id", None)
        if pinned:
            for aid, s in entries:
                if aid == pinned:
                    target = s
                    break

        if target is None:
            # 自动模式：优先选“正在播放”的会话，否则用第一个
            target = entries[0][1] if entries else None
            for aid, s in entries:
                try:
                    if s.get_playback_info().playback_status == \
                            GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING:
                        target = s
                        break
                except Exception:
                    continue

        info = {
            "session": target,
            "app_id": target.source_app_user_model_id,
            "app_ids": [aid for aid, _ in entries],
            "playing": target.get_playback_info().playback_status ==
                       GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING,
        }
        try:
            props = await target.try_get_media_properties_async()
            info["title"] = props.title
            info["artist"] = props.artist or props.subtitle
            # 封面
            try:
                stream = await props.thumbnail.open_read_async()
                size = stream.size
                from winrt.windows.storage.streams import Buffer, InputStreamOptions
                b = Buffer(size)
                await stream.read_async(b, size, InputStreamOptions.NONE)
                data = bytes(b)
                info["art"] = base64.b64encode(data).decode("ascii")
            except Exception:
                info["art"] = None
        except Exception:
            info["title"] = "无法读取媒体信息"
            info["artist"] = ""
            info["art"] = None
        return info

    def _apply_state(self, info):
        if info is None:
            self._app_ids = []
            if self._switch_shown:
                self.switch_btn.pack_forget()
                self._switch_shown = False
            self.app_label.config(text="没有正在播放的媒体")
            self.title_label.config(text="—")
            self.sub_label.config(text="")
            self.btn_play.config(text="⏸")
            if getattr(self, "tray", None):
                self.tray.set_tooltip("媒体控制 - 没有正在播放的媒体")
            return

        self.session = info["session"]
        app_id = info["app_id"] or ""
        self._current_app_id = app_id
        # "C:\\path\\App.exe" → "App"；"com.vendor.app" → "app"
        if "\\" in app_id:
            app_name = app_id.rsplit("\\", 1)[-1]
        else:
            app_name = app_id.split("!")[0].rstrip(".").rsplit(".", 1)[-1]
        self.app_label.config(text=f"🎧 {app_name}")

        # 多 App 切换按钮：系统里有 ≥2 个媒体会话时才显示，插在关闭按钮左边
        app_ids = info.get("app_ids") or []
        self._app_ids = app_ids
        pin = getattr(self, "_pinned_app_id", None)
        if pin and app_ids and pin not in app_ids:
            self._pinned_app_id = None  # 固定的 App 会话已消失，回到自动模式
        if len(app_ids) > 1:
            if not self._switch_shown:
                # tools 行的最右侧（透明度按钮右方），和关闭按钮同一列
                self.switch_btn.pack(side="right", before=self.opacity_btn,
                                     padx=(0, 2))
                self._switch_shown = True
        elif self._switch_shown:
            self.switch_btn.pack_forget()
            self._switch_shown = False

        title = (info.get("title") or "").strip() or "未知标题"
        artist = (info.get("artist") or "").strip()
        self.title_label.config(text=title)
        self.sub_label.config(text=artist)

        self.btn_play.config(text="⏸" if info["playing"] else "▶")

        # 同步任务栏缩略图按钮的播放/暂停图标
        if getattr(self, "_taskbar", None):
            self._taskbar.set_playing(info["playing"])

        # 托盘悬浮提示：正文（中间大字，SMTC 的 title）存在就只显示正文；
        # 没有正文时显示“窗口标题 - 媒体标题”（下面的小字/章节名）
        if getattr(self, "tray", None):
            if title != "未知标题":
                tip = title
            elif artist:
                tip = f"媒体控制 - {artist}"
            else:
                tip = "媒体控制"
            self.tray.set_tooltip(tip)

        # 封面：tk.PhotoImage 只认 PNG/GIF，其他格式（如 JPEG）用 Pillow 转成 PNG
        art_key = (info.get("title"), info.get("artist"), bool(info.get("art")))
        art_b64 = info.get("art")
        if art_b64 and art_key != self._art_key:
            img = None
            try:
                img = tk.PhotoImage(data=art_b64)
            except Exception:
                try:
                    import io
                    from PIL import Image
                    raw = base64.b64decode(art_b64)
                    png = io.BytesIO()
                    Image.open(io.BytesIO(raw)).convert("RGBA").save(png, "PNG")
                    img = tk.PhotoImage(
                        data=base64.b64encode(png.getvalue()).decode("ascii"))
                except Exception:
                    img = None
            if img is not None:
                # 等比缩小到不超过 72px
                w, h = img.width(), img.height()
                factor = max(w, h) // 72 or 1
                if factor > 1:
                    img = img.subsample(factor, factor)
                self.art_img = img
                self.art_label.config(image=img, text="", font=self.big_font)
                self._art_key = art_key
            else:
                self.art_label.config(image="", text="📕")
        elif not art_b64:
            self.art_label.config(image="", text="📕")
            self._art_key = art_key

    # ---------------- 控制 ----------------
    def _cycle_session(self):
        """右上角 › 箭头：在多个媒体会话（App）间循环切换。

        固定到选中的 App 后，即使它暂停了也不会被其他正在播放的会话抢走；
        该 App 的会话消失时自动回到“优先正在播放”的默认策略。
        """
        ids = list(getattr(self, "_app_ids", []) or [])
        if len(ids) < 2:
            return
        cur = getattr(self, "_current_app_id", None)
        try:
            idx = ids.index(cur)
        except ValueError:
            idx = len(ids) - 1
        self._pinned_app_id = ids[(idx + 1) % len(ids)]
        # 立即刷新一次，不等下一轮 800ms 轮询
        self._submit(self._get_state(), self._apply_state)

    def _need_session(self):
        if self.session is None:
            sessions = self.manager.get_sessions()
            if sessions:
                self.session = sessions[0]
        return self.session

    def on_prev(self):
        s = self._need_session()
        if s:
            self._submit(_wrap(s.try_skip_previous_async()))

    def on_next(self):
        s = self._need_session()
        if s:
            self._submit(_wrap(s.try_skip_next_async()))

    def on_play_pause(self):
        s = self._need_session()
        if s:
            self._submit(_wrap(s.try_toggle_play_pause_async()))


def main():
    root = tk.Tk()
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()

    manager = get_manager_sync(loop)
    app = MediaControlApp(root, loop, manager)
    root.protocol("WM_DELETE_WINDOW", app.hide_window)
    root.mainloop()


if __name__ == "__main__":
    main()
