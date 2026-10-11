# 桌面待办清单挂件

> 常驻桌面右上角的小窗口：**无标题栏、可拖动**，回车就能记一条待办，
> **条目可以按住上下拖动排序**，所有改动立即存盘，重开自动恢复待办内容、
> 顺序和窗口位置。
>
> 同一份代码在 **统信 UOS / Deepin（X11）** 与 **Windows 11** 上都跑：单文件、
> 只用 Python 标准库、零第三方依赖。
>
> **默认不置顶**：它就是普通窗口，别的窗口盖上来就把它盖住，不会一直挡在前面。
> **它也不在任务栏 / dock 里显示图标**；被盖住时用 `Alt+Tab`（Windows 上见
> 「平台差异」一节），或者**再运行一次同样的命令**，就能把它叫到最前面。
> 确实想让它一直浮在最上面时，在右键菜单里勾「始终置顶」（勾选状态会记住）。

![预览](preview.png)

拖动排序时，被拖的条目会高亮（浅底 + 左侧蓝条），跟着指针实时换位：

![拖动排序](preview-drag.png)

## 快速开始

只需要 Python 3 + tkinter（统信 UOS / Deepin 自带；Windows 上随 Python 安装包自带），
单文件、无第三方依赖。

**统信 UOS / Deepin（X11）**：

```bash
cd desktop-todo          # 本项目所在目录
python3 desktop_todo.py
```

**Windows 11**：

```powershell
cd D:\个人项目\desktop-todo
python desktop_todo.py
```

- **再运行一次同样的命令 = 把已经在跑的窗口叫到最前面**（不会开出第二个窗口）。
  它不在任务栏里，所以这就是最顺手的唤回方式；`Alt+Tab` 里能不能找到它取决于平台
  （Windows 上 `WS_EX_TOOLWINDOW` 会把它从 `Alt+Tab` 里也拿掉，见「平台差异」）。
- 关掉它：点右上角 `×`；Linux 上也可以 `pkill -f desktop_todo.py`。

开机自启（Linux 写 `~/.config/autostart/desktop-todo.desktop`；Windows 写
`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`，用 `pythonw.exe` 启动、不弹黑框）：

```bash
python3 desktop_todo.py --install-autostart     # 打开
python3 desktop_todo.py --uninstall-autostart   # 关闭
```

自检（数据读写 + 界面构造 + 拖拽路径，窗口不会显示）：

```bash
python3 desktop_todo.py --check        # 输出 SELFTEST OK 表示正常
```

诊断（换机器、换平台排障用；打印环境/路径/字体/缩放，并把「去标题栏」「不进任务栏」
「单实例」逐条**写进去再读回来**验证——只读，不改数据也不改配置）：

```bash
python3 desktop_todo.py --doctor
```

## 操作

| 操作 | 效果 |
| --- | --- |
| 输入框回车 / 点 `+` | 添加一条待办（新条目排在最前） |
| **按住条目上下拖动** | **调整顺序**（拖过谁就跟谁换位，松手即保存） |
| 点条目文字或左边方框 | 标记完成 / 取消完成（完成后文字加删除线） |
| 点条目右侧 `×` | 删除该条 |
| 点底部「清除已完成」 | 删除所有已完成条目 |
| 拖标题栏 | 移动窗口（位置自动记住） |
| 双击标题栏 / 点标题栏箭头 | 折叠 / 展开成一条细标题 |
| 右键标题栏 | 折叠、**始终置顶开关（默认关闭）**、窗口透明度、退出 |
| `Esc` | 清空输入框 |

标题栏右侧会实时显示「*N* 项未完成」，全部完成时显示「全部完成」。
条目多的时候列表区最高 268px，超出可用滚轮滚动；拖动时拖到列表上下边缘会自动滚动。

被别的窗口挡住时：**再运行一次 `python3 desktop_todo.py`**（已开的那个窗口会浮到最前面，
不会有第二个实例）；或者用 `Alt+Tab` / 点它露出来的部分。

## 数据

- 待办与窗口状态：Linux 是 `~/.local/share/desktop-todo/todos.json`，Windows 是
  `%APPDATA%\desktop-todo\todos.json`（用 `python3 desktop_todo.py --data-file`
  打印当前平台的实际路径）
- 结构：`items[]` 保存条目（`id / text / done / created`），`ui` 保存窗口
  `x / y / collapsed / alpha / topmost`。
- 写入是「临时文件 + 原子替换」，不会写坏；数据文件损坏时自动备份为
  `todos.json.broken` 并以空清单启动。

## 平台差异

平台相关的东西只有三处，都收在 `PlatformBackend` / `X11Backend` / `WindowsBackend`
里，业务代码只认接口（`get_backend()`）：

| | 统信 UOS / Deepin（X11） | Windows 11 |
| --- | --- | --- |
| 去标题栏 | `_MOTIF_WM_HINTS` 的 `decorations=0`（窗口仍由 WM 托管，输入框能拿焦点） | 清掉 `WS_CAPTION` + `WS_THICKFRAME`（同样保留系统托管） |
| 不进任务栏 | EWMH 的 `_NET_WM_STATE_SKIP_TASKBAR` + `SKIP_PAGER` | `WS_EX_TOOLWINDOW` |
| 唤回（再运行一次） | unix 套接字发 `raise`，窗口 `lift()` + 短暂置顶 + `focus_force()` | 命名互斥体判重，第二个进程直接 `FindWindowW` + `SetForegroundWindow` |
| 数据目录 | `$XDG_DATA_HOME` 或 `~/.local/share/desktop-todo` | `%APPDATA%\desktop-todo` |
| 开机自启 | `~/.config/autostart/desktop-todo.desktop` | `HKCU\...\Run`，用 `pythonw.exe` 启动 |
| 中文字体 | Noto Sans CJK SC / 思源黑体 / 文泉驿微米黑 | Microsoft YaHei UI |
| 缩放 | 交给 Tk 与桌面环境 | 进程先声明 DPI 感知（必须在创建窗口之前），避免高缩放下模糊 |
| `Alt+Tab` | 仍能找到（故意不发 `SKIP_SWITCHER`） | **找不到**（`WS_EX_TOOLWINDOW` 的代价），靠「再运行一次」唤回 |

## 实现说明

- **为什么不用 `overrideredirect`（无边框窗口）**：那种窗口不受窗口管理器管理，
  Deepin 下点击它拿不到键盘焦点，输入框打不了字。这里改成让 WM 托管窗口，
  Linux 用 `_MOTIF_WM_HINTS`、Windows 用窗口样式清掉标题栏——既保持无边框外观，
  又能正常获得焦点、正常被盖住/唤回。
- **不在任务栏 / dock 里显示图标**：窗口类型仍是普通窗口（这样点击能正常拿焦点），
  但按 EWMH 设了 `_NET_WM_STATE_SKIP_TASKBAR` + `SKIP_PAGER`：映射前直接写
  `_NET_WM_STATE` 属性，映射后再补一条标准的 ClientMessage（KWin 只对已托管的
  窗口处理这条消息）。故意不发 KDE 的 `_KDE_NET_WM_STATE_SKIP_SWITCHER`——
  那样连 `Alt+Tab` 都没有它了。Windows 上对应的是 `WS_EX_TOOLWINDOW`。
- **单实例 + 唤回**：Linux 上程序在 `$XDG_RUNTIME_DIR/desktop-todo.sock` 上监听一个
  unix 套接字；第二次启动时先连它，连上就发一句 `raise` 然后自己退出，
  于是 `lift()` + 短暂置顶 + `focus_force()` 把已有窗口叫到前面。Windows 上 Python
  没有 `AF_UNIX`、Tk 也没有 `createfilehandler`，所以改用命名互斥体判重 + 按标题
  找窗口直接激活。任务栏里没有入口的情况下，这就是最可靠的找回方式。
- **Windows 的窗口句柄**：`winfo_id()` 拿到的是 Tk 的子窗口，真正的顶层窗口要
  `GetAncestor(GA_ROOT)` 再上一层——等价于 X11 那边用 `XQueryTree` 找 WM frame。
- `topmost` 默认 `false`：只在右键菜单里显式勾选时才会置顶（Linux 走
  `_NET_WM_STATE_ABOVE`，Windows 走 `-topmost`）。
- 条目行是画在 `Canvas` 上的（勾选框、折行文字、删除按钮都是 Canvas 图元），
  折行宽度用字体 `measure()` 自己算，避免 Tk `Label` 折行在本机渲染左右不齐。
- 输入框的提示文字在**第一次点击输入框时**才清除，并且会吞掉那次点击，
  免得插入点落进提示文字中间、把提示和输入内容混在一起。
- **拖拽排序**：按住条目移动超过 5px 才算拖动（否则仍按点击处理，避免误触），
  拖到哪一行就实时跟那一行换位，松手才写盘；被拖条目用 Canvas 图元画高亮
  （浅底 + 左侧蓝条），贴到列表上下边缘会自动滚动。顺序就是
  `todos.json` 里 `items[]` 的顺序，没有额外的排序列。
- `--check` 自检除了数据读写和界面构造，还会把「开始拖动 → 移动 → 落下」
  的代码路径跑一遍（隐藏窗口），所以像漏定义常量这类笔误在自检里就会失败。
- `--doctor` 诊断把每个平台能力的**结果**打出来（X11 是写完属性再回读
  `_MOTIF_WM_HINTS` / `_NET_WM_STATE`，Windows 是改完样式再回读 style/exstyle），
  所以「调用没报错、但窗口管理器没接受」这种情况也能看出来。
- 依赖：Python 3 + tkinter（统信 UOS / Deepin 自带；Windows 随 Python 安装包自带）。
  Linux 额外需要 X11 的 `libX11.so.6`（取不到时退化成「带标题栏、任务栏里有图标」
  的普通窗口，待办功能不受影响）；Windows 不需要任何额外依赖，全部走标准库
  `ctypes` 调 user32/kernel32。

## 可选：加到应用菜单

把下面内容存成 `~/.local/share/applications/desktop-todo.desktop`
（`Exec` 里换成你放这个脚本的绝对路径）：

```ini
[Desktop Entry]
Type=Application
Name=桌面待办清单
Comment=常驻桌面的待办清单挂件
Exec=/usr/bin/python3 /path/to/desktop-todo/desktop_todo.py
Icon=checkbox-checked
Terminal=false
Categories=Utility;
```

## 许可证

MIT License，见 [LICENSE](LICENSE)。
