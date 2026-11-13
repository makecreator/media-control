<<<<<<< HEAD
# 媒体控制（MediaControl）

Windows 11 上的独立媒体控制小窗。它读取系统的 SMTC（System Media Transport Controls）会话，把「当前谁在放什么」显示成一个小窗，并提供上一首 / 播放暂停 / 下一首等常用操作。

相比系统自带的媒体浮层，它更适合常驻桌面角落：可以拉伸、可以调透明度、可以置顶、能收进托盘，也能从任务栏缩略图直接控制播放。

## 功能

- **媒体信息显示**：来源应用、标题、艺术家、专辑封面（异步加载，失败时回退为占位图标）
- **多 App 切换**：系统里有多个媒体会话时，标题栏右上角出现 `›` 箭头，点击循环切换当前控制的 App（如 foobar2000 / QQ音乐）；切过去的 App 即使暂停也不会被其他正在播放的会话抢走，其会话消失后自动回到「优先正在播放」的默认策略
- **播放控制**：上一首 / 播放暂停 / 下一首
- **自绘标题栏**：不显示系统标题，两行布局——第一行左侧为来源应用名，右侧为 `📌` 最小化 关闭；第二行贴在关闭按钮下方，依次为 `透明度%` `A−` `A+` 和多 App 切换 `›`
  - `A−` / `A+` 或 `Ctrl + 滚轮`：调整正文字号（9–26），可持久化
  - `透明度%`：点开滑块面板，40%–100% 可调，点滑槽直接跳位
  - `📌`：窗口置顶开关
  - 关闭按钮：**收进托盘，不退出进程**
  - 顶部空白处可拖动窗口；四边四角可拉伸（最小 300×170）
- **托盘图标**：单击唤回窗口，双击播放 / 暂停，右键菜单「显示窗口 / 播放 暂停 / 退出」
  - 托盘 tooltip 优先显示媒体标题；没有标题时显示 `媒体控制 - 艺术家`；都没有则为 `媒体控制`
- **任务栏缩略图按钮**：任务栏预览里直接出现上一首 / 播放暂停 / 下一首
- **状态持久化**：窗口大小位置、字号、透明度写入 `media_control_config.json`；换显示器导致窗口跑到屏幕外时自动回退到默认位置

## 环境要求

- Windows 10 1809+ / Windows 11（依赖 SMTC 与 `ITaskbarList3`）
- Python 3.10（开发时使用 3.10）
- 依赖：

```bash
pip install winrt-runtime winrt-Windows.Media.Control winrt-Windows.Foundation winrt-Windows.Storage.Streams
```

界面使用标准库 `tkinter`，其余系统交互（托盘、任务栏按钮、窗口子类化）全部通过 `ctypes` 直接调用 Win32 API，没有额外第三方 GUI 依赖。

## 运行

```bash
python media_control.py
```

程序启动后默认隐藏在托盘，单击托盘图标即可唤出窗口。

## 打包

使用 PyInstaller 的 onefile + windowed 模式：

```bash
pyinstaller --onefile --windowed --name MediaControl ^
  media_control.py custom_title.py tray_icon.py taskbar_controls.py
```

产物为 `dist/MediaControl.exe`。首次启动单文件包需要解压，托盘图标会晚一两秒出现，属正常现象。配置文件写在 exe 同目录。

## 配置文件

`media_control_config.json`（与脚本或 exe 同目录，运行时自动生成）：

```json
{
  "geometry": "320x260+266+226",
  "title_size": 10,
  "opacity": 100
}
```

| 字段 | 说明 |
| --- | --- |
| `geometry` | 上次关闭时的窗口大小与位置 |
| `title_size` | 正文字号，9–26 |
| `opacity` | 窗口透明度百分比，40–100 |

删除该文件即可恢复全部默认值。

## 代码结构

| 文件 | 职责 |
| --- | --- |
| `media_control.py` | 主程序：窗口与布局、SMTC 轮询、播放控制、配置读写、托盘菜单与退出流程 |
| `custom_title.py` | 自绘标题栏：去掉 `WS_CAPTION` 保留 `WS_THICKFRAME`，子类化窗口过程处理 `WM_NCHITTEST` 的边框缩放热区 |
| `tray_icon.py` | 托盘图标：独立线程 + message-only 窗口接收 `Shell_NotifyIcon` 消息，事件入队交 Tk 主线程处理 |
| `taskbar_controls.py` | 任务栏缩略图工具栏：`ThumbBarAddButtons` 三个按钮，图标用 GDI + Segoe Fluent Icons 现画 |

## 实现要点（踩过的坑）

- **绝不在 explorer / 系统回调线程里碰 tkinter。** 托盘和任务栏按钮的消息都来自系统线程，在其中调用 Tcl 会触发 `PyEval_RestoreThread: GIL is released` 崩溃。统一做法是：回调只把事件塞进队列，由 Tk 主线程轮询消费。
- **托盘右键菜单用原生 `TrackPopupMenu`。** `tk.Menu.tk_popup` 在托盘场景下点击菜单项只收起菜单、不触发命令（实测 `tk_popup` 正常返回但 command 从未执行），且在回调里销毁窗口会让 grab 循环永不返回。`TPM_RETURNCMD` 是同步返回命令 ID 的，退出流程因此变得可靠。
- **拖拽用客户端实现。** 无边框窗口下 Tk 会吞掉 `WM_NCLBUTTONDOWN`，`DefWindowProc` 收不到消息，系统移动循环根本不启动（改投 `SC_MOVE` 同样无效）。因此标题栏绑定 `<Button-1>` / `<B1-Motion>`，直接 `geometry("+x+y")` 移动；`WM_NCHITTEST` 只保留边框缩放热区。
- **自绘标题栏必须在窗口真正映射之后安装。** `withdraw` 状态下 `wm_frame()` 拿不到有效的 wrapper 句柄。
- **几何信息用 `<Configure>` 缓存。** 最小化 / 收托盘时窗口已非 normal 状态，此时再读 `geometry()` 会拿到错误值。

## 已知限制

- 仅支持 Windows，且依赖系统的 SMTC；播放器本身不上报 SMTC 时窗口不会显示内容。
- 只跟随系统当前活跃的媒体会话，不做多会话切换。
- 托盘单/双击区分依赖系统双击时间，慢速双击可能被识别为两次单击。
=======
# media-control
>>>>>>> d9bf3f47d0853063c1911848fbd2d4af92342c3d
