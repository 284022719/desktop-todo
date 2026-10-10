#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""桌面待办清单挂件（Deepin / X11 + tkinter）。

一个常驻桌面、无边框的小窗口：回车添加、点勾选完成、点 × 删除，
所有改动立即写入 JSON，重开自动恢复待办与窗口位置。

默认**不置顶**：它就是普通窗口，会被别的窗口盖住，不会一直挡在前面；
需要时在右键菜单里勾「始终置顶」（勾选状态会记住）。

它也**不在任务栏 / dock 里显示图标**（EWMH 的 `SKIP_TASKBAR`/`SKIP_PAGER`）。
被别的窗口盖住后，用 `Alt+Tab`，或者**再运行一次本程序**，就能把它叫到最前面。

用法::

    python3 desktop_todo.py                    # 启动挂件；已在运行则把窗口叫到前面
    python3 desktop_todo.py --check            # 自检（数据读写 + 界面构造）后退出
    python3 desktop_todo.py --install-autostart     # 开机自启
    python3 desktop_todo.py --uninstall-autostart   # 取消开机自启

交互::

    回车 / 「+」      添加待办
    点文字或方框      标记完成 / 取消完成
    按住条目上下拖     调整顺序（松手即保存）
    点 ×             删除该条
    拖标题栏          移动窗口
    双击标题栏        折叠 / 展开（右键标题栏还有菜单）
    Esc              清空输入框

数据文件: ``~/.local/share/desktop-todo/todos.json``
"""

import argparse
import json
import ctypes
import os
import shutil
import socket
import sys
import tempfile
import time

import tkinter as tk
from tkinter import font as tkfont

APP_ID = "desktop-todo"


class _XClientMessageEvent(ctypes.Structure):
    """Xlib 的 XClientMessageEvent（用来给 WM 发 _NET_WM_STATE 消息）。"""

    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("data", ctypes.c_long * 5),
    ]


# ---------- 外观 ----------
PANEL_W = 300          # 面板宽度（像素）
PAD = 12               # 面板内边距
LIST_MAX_H = 268       # 列表区最大高度，超出后滚轮滚动
LIST_W = PANEL_W - 2 * PAD - 2          # 列表区宽度
TEXT_X = 36            # 条目文字起始 x（给勾选框留位置）
DEL_W = 30             # 条目右侧删除按钮的点击区宽度
ROW_PAD_Y = 6          # 条目上下内边距
ROW_WRAP = LIST_W - TEXT_X - DEL_W - 4  # 条目文字换行宽度

C_CARD = "#24272c"     # 卡片底色
C_LIST = "#1d2024"     # 输入框 / 列表底色
C_BORDER = "#3a3f47"   # 边框
C_TEXT = "#e9ebee"     # 主文字
C_DIM = "#98a0aa"      # 次要文字
C_MUTED = "#6d7480"    # 更淡的文字 / 图标
C_ACCENT = "#4c9aff"   # 强调色
C_DANGER = "#e5695f"   # 删除悬停
C_HOVER = "#2e333a"    # 拖动中的条目底色
C_DONE = "#7b828c"     # 已完成文字

PLACEHOLDER = "添加一条待办，回车确认"
FONT_STACK = (
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "WenQuanYi Micro Hei",
    "Microsoft YaHei",
    "Droid Sans Fallback",
    "DejaVu Sans",
)


# --------------------------------------------------------------------------- #
# 窗口管理器交互
# --------------------------------------------------------------------------- #
def _on_wm_window(root, action):
    """打开 X 连接，找到 Tk toplevel 的 WM 窗口，交给 action(x11, display, target)。

    找不到就返回 False；任何 X 错误只当这次没做成，绝不把程序带崩。
    """
    try:
        import ctypes
        x11 = ctypes.CDLL("libX11.so.6")
    except (ImportError, OSError):
        return False

    handler_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
    handler = handler_type(lambda display, event: 0)     # 忽略 X 错误，别让默认处理器退出进程
    previous = None
    display = None
    try:
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XInternAtom.restype = ctypes.c_ulong
        x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        x11.XDefaultRootWindow.restype = ctypes.c_ulong
        x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x11.XSetErrorHandler.restype = ctypes.c_void_p
        x11.XSetErrorHandler.argtypes = [handler_type]

        display = x11.XOpenDisplay(None)
        if not display:
            return False
        previous = x11.XSetErrorHandler(handler)

        window = ctypes.c_ulong(root.winfo_id())
        root_ret, parent = ctypes.c_ulong(), ctypes.c_ulong()
        children = ctypes.POINTER(ctypes.c_ulong)()
        count = ctypes.c_uint()
        x11.XQueryTree(ctypes.c_void_p(display), window, ctypes.byref(root_ret),
                       ctypes.byref(parent), ctypes.byref(children), ctypes.byref(count))
        root_window = x11.XDefaultRootWindow(ctypes.c_void_p(display))
        if parent.value and parent.value != root_window:
            target = parent.value                      # Tk 为 toplevel 建的 WM 窗口
        elif window.value and window.value != root_window:
            target = window.value
        else:
            return False                               # 窗口还没建出来
        action(x11, display, target, root_window)
        x11.XFlush(ctypes.c_void_p(display))
        return True
    except Exception:
        return False
    finally:
        if display:
            try:
                if previous:
                    x11.XSetErrorHandler(previous)
                x11.XCloseDisplay(ctypes.c_void_p(display))
            except Exception:
                pass


def strip_window_decorations(root):
    """请窗口管理器去掉标题栏，但保留 WM 托管。

    ``overrideredirect(True)`` 的无边框窗口不受 WM 管理，Deepin 下点它拿不到
    键盘焦点（输入框没法打字），所以这里走另一条路：窗口交给 WM 托管，
    再用 ``_MOTIF_WM_HINTS`` 的 decorations=0 把标题栏去掉。
    必须在窗口第一次映射前、且 Tk 已经建出该 toplevel 的 WM 窗口之后调用
    （先做一次 ``wm attributes`` 调用、再 ``update_idletasks()`` 即可）。
    """

    def apply(x11, display, target, root_window):
        atom = x11.XInternAtom(ctypes.c_void_p(display), b"_MOTIF_WM_HINTS", 0)
        values = (ctypes.c_ulong * 5)(2, 0, 0, 0, 0)     # flags=DECORATIONS, decorations=0
        x11.XChangeProperty(ctypes.c_void_p(display), ctypes.c_ulong(target),
                            ctypes.c_ulong(atom), ctypes.c_ulong(atom), 32, 0,
                            ctypes.byref(values), 5)

    return _on_wm_window(root, apply)


def set_skip_taskbar(root):
    """告诉 WM：这个窗口不要出现在任务栏 / dock / 分页器里（EWMH 标准做法）。

    用 ``_NET_WM_STATE`` 的 ClientMessage 发 ``SKIP_TASKBAR`` + ``SKIP_PAGER``；
    故意不发 KDE 的 ``SKIP_SWITCHER``——那样连 Alt+Tab 都找不到它了，
    而这个窗口默认不置顶，总得留个能把它叫回来的入口。
    """

    def apply(x11, display, target, root_window):
        state_atom = x11.XInternAtom(ctypes.c_void_p(display), b"_NET_WM_STATE", 0)
        skip_taskbar = x11.XInternAtom(ctypes.c_void_p(display),
                                       b"_NET_WM_STATE_SKIP_TASKBAR", 0)
        skip_pager = x11.XInternAtom(ctypes.c_void_p(display),
                                     b"_NET_WM_STATE_SKIP_PAGER", 0)

        # 1) 还没映射时：直接写 _NET_WM_STATE 属性，WM 建图时会读到（XA_ATOM = 4）
        atoms = (ctypes.c_ulong * 2)(skip_taskbar, skip_pager)
        x11.XChangeProperty(ctypes.c_void_p(display), ctypes.c_ulong(target),
                            ctypes.c_ulong(state_atom), ctypes.c_ulong(4), 32, 0,
                            ctypes.byref(atoms), 2)

        # 2) 已经映射时：再按 EWMH 发的标准 ClientMessage（_NET_WM_STATE_ADD）
        buf = ctypes.create_string_buffer(192)           # 足够放一个 XEvent
        event = ctypes.cast(buf, ctypes.POINTER(_XClientMessageEvent)).contents
        event.type = 33
        event.serial = 0
        event.send_event = 1
        event.display = display
        event.window = target
        event.message_type = state_atom
        event.format = 32
        event.data[0] = 1                                # _NET_WM_STATE_ADD
        event.data[1] = skip_taskbar
        event.data[2] = skip_pager
        event.data[3] = 1                                # source indication: application
        event.data[4] = 0
        mask = 0x00180000                                # SubstructureRedirect | SubstructureNotify
        x11.XSendEvent(ctypes.c_void_p(display), ctypes.c_ulong(root_window), 0, mask,
                       ctypes.byref(event))

    return _on_wm_window(root, apply)


# --------------------------------------------------------------------------- #
# 数据层
# --------------------------------------------------------------------------- #
def data_dir():
    """返回数据目录（遵循 XDG_DATA_HOME）。"""
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, APP_ID)


def data_file():
    """返回待办数据文件路径。"""
    return os.path.join(data_dir(), "todos.json")


def ipc_socket_path():
    """单实例套接字路径（放在 XDG_RUNTIME_DIR 或临时目录）。"""
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return os.path.join(base, "%s.sock" % APP_ID)


def raise_running_instance():
    """已有实例在跑时让它把窗口叫到最前面；成功返回 True。

    任务栏里看不到这个窗口，所以「再运行一次」就是最顺手的唤回方式。
    """
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(1.5)
    try:
        client.connect(ipc_socket_path())
    except OSError:
        return False
    try:
        client.sendall(b"raise\n")
        client.recv(16)
    except OSError:
        return False
    finally:
        client.close()
    return True


class Store(object):
    """待办条目 + 窗口状态的持久化，单文件 JSON，改动即落盘。"""

    def __init__(self, path):
        self.path = path
        self.items = []
        # topmost 默认关闭：不置顶，就不会一直挡在其他窗口前面。
        # 需要时在右键菜单里勾「始终置顶」，勾选状态会被记住。
        self.ui = {"x": None, "y": None, "collapsed": False, "alpha": 0.94, "topmost": False}
        self._next_id = 1
        self.load()

    # ---- 读写 ----
    def load(self):
        """读取数据；文件缺失按空清单处理，损坏则备份后从空清单开始。"""
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except IOError:
            return
        except ValueError:
            self._backup_broken()
            return
        if not isinstance(raw, dict):
            return

        entries = raw.get("items")
        if isinstance(entries, list):
            for index, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    continue
                text = " ".join(str(entry.get("text", "")).split())
                if not text:
                    continue
                try:
                    item_id = int(entry.get("id"))
                except (TypeError, ValueError):
                    item_id = index + 1
                self.items.append({
                    "id": item_id,
                    "text": text,
                    "done": bool(entry.get("done")),
                    "created": float(entry.get("created") or time.time()),
                })
        if self.items:
            self._next_id = max(item["id"] for item in self.items) + 1

        ui = raw.get("ui")
        if isinstance(ui, dict):
            for key in ("x", "y"):
                value = ui.get(key)
                if isinstance(value, (int, float)):
                    self.ui[key] = int(value)
            if isinstance(ui.get("collapsed"), bool):
                self.ui["collapsed"] = ui["collapsed"]
            if isinstance(ui.get("topmost"), bool):
                self.ui["topmost"] = ui["topmost"]
            alpha = ui.get("alpha")
            if isinstance(alpha, (int, float)) and 0.3 <= float(alpha) <= 1.0:
                self.ui["alpha"] = float(alpha)

    def _backup_broken(self):
        try:
            shutil.copy2(self.path, self.path + ".broken")
        except IOError:
            pass

    def save(self):
        """原子写入；磁盘不可写时静默跳过，不影响使用。"""
        directory = os.path.dirname(self.path)
        try:
            if directory and not os.path.isdir(directory):
                os.makedirs(directory)
            temp = self.path + ".tmp"
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "items": self.items, "ui": self.ui},
                          handle, ensure_ascii=False, indent=2)
            os.replace(temp, self.path)
        except (IOError, OSError):
            pass

    # ---- 条目操作 ----
    def add(self, text):
        """新增一条（插到最前）；空文本返回 None。"""
        text = " ".join(str(text).split())
        if not text:
            return None
        item = {"id": self._next_id, "text": text, "done": False, "created": time.time()}
        self._next_id += 1
        self.items.insert(0, item)
        self.save()
        return item

    def toggle(self, item_id):
        for item in self.items:
            if item["id"] == item_id:
                item["done"] = not item["done"]
                self.save()
                return item
        return None

    def move(self, item_id, index, save=True):
        """把某条挪到列表的第 index 个位置（index 按移动前的列表计算）。

        拖拽过程中先 ``save=False`` 只改内存顺序，松手时再落盘。
        """
        item = None
        for candidate in self.items:
            if candidate["id"] == item_id:
                item = candidate
                break
        if item is None:
            return False
        current = self.items.index(item)
        index = max(0, min(int(index), len(self.items) - 1))
        if index == current:
            return False
        self.items.pop(current)
        self.items.insert(index, item)
        if save:
            self.save()
        return True

    def remove(self, item_id):
        before = len(self.items)
        self.items = [item for item in self.items if item["id"] != item_id]
        if len(self.items) != before:
            self.save()
            return True
        return False

    def clear_done(self):
        before = len(self.items)
        self.items = [item for item in self.items if not item["done"]]
        if len(self.items) != before:
            self.save()
            return before - len(self.items)
        return 0

    def remaining(self):
        return len([item for item in self.items if not item["done"]])

    def has_done(self):
        return any(item["done"] for item in self.items)


# --------------------------------------------------------------------------- #
# 界面层
# --------------------------------------------------------------------------- #
class TodoWidget(object):
    """桌面待办面板（无边框、默认不置顶；条目可拖拽排序）。"""

    def __init__(self, root, store, visible=True):
        self.root = root
        self.store = store
        self.fonts = self._make_fonts()
        self._drag_offset = None          # 拖窗口的偏移
        self._placeholder_on = False
        # 条目拖拽排序状态
        self.row_widgets = []             # [(item_id, row_canvas)]，按显示顺序
        self._press = None                # 按下时记录 {id, y_root, zone}
        self._drag_id = None              # 正在被拖动的条目 id
        self._pointer_y = None            # 拖动中指针的屏幕 y（用于贴边自动滚动）
        self._autoscroll_job = None
        self._ipc_server = None            # 单实例套接字：第二次启动用来把窗口叫到前面
        self._build()
        self._restore_position()
        self._apply_collapsed()
        self._refresh(keep_scroll=False)
        if visible:
            self.root.deiconify()
            self.root.update_idletasks()
            self._restore_position()      # 映射后再落一次，避免 WM 摆放时挪位
            self._start_ipc()
            set_skip_taskbar(self.root)   # 映射后再发一次（KWin 只对已托管的窗口处理这个消息）

    # ---- 单实例：第二次启动 = 把已有窗口叫到最前面 ----
    def _start_ipc(self):
        """监听一个 unix 套接字；之后每启动一次本程序，都会让这个窗口浮到前面。"""
        path = ipc_socket_path()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            if os.path.exists(path):
                os.unlink(path)
            server.bind(path)
            server.listen(2)
            server.setblocking(False)
        except OSError:
            server.close()
            return
        self._ipc_server = server
        try:
            self.root.tk.createfilehandler(server, tk.READABLE, self._on_ipc)
        except (tk.TclError, AttributeError):
            self._ipc_server = None

    def _on_ipc(self, server, mask):
        try:
            connection, _ = server.accept()
        except OSError:
            return
        try:
            connection.recv(64)
            connection.sendall(b"ok\n")
        except OSError:
            pass
        finally:
            connection.close()
        self.raise_window()

    def raise_window(self):
        """把窗口提到最前面并拿到焦点（不改变用户设置的「始终置顶」状态）。"""
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)   # 短暂置顶一下，确保能从别的窗口后面冒出来
            self.root.focus_force()
            self.root.after(400, lambda: self.root.attributes(
                "-topmost", bool(self.store.ui["topmost"])))
        except tk.TclError:
            pass

    def _stop_ipc(self):
        if self._ipc_server is None:
            return
        path = ipc_socket_path()
        try:
            self.root.tk.deletefilehandler(self._ipc_server)
        except (tk.TclError, AttributeError):
            pass
        try:
            self._ipc_server.close()
        except OSError:
            pass
        self._ipc_server = None
        try:
            if os.path.exists(path):
                os.unlink(path)
        except OSError:
            pass

    # ---- 字体 ----
    def _make_fonts(self):
        available = set(tkfont.families())
        family = None
        for name in FONT_STACK:
            if name in available:
                family = name
                break
        base = tkfont.Font(family=family, size=10) if family else tkfont.Font(size=10)
        actual = base.cget("family")
        return {
            "base": base,
            "bold": tkfont.Font(family=actual, size=10, weight="bold"),
            "small": tkfont.Font(family=actual, size=9),
            "done": tkfont.Font(family=actual, size=10, overstrike=1),
        }

    # ---- 组装 ----
    def _build(self):
        root = self.root
        root.title("桌面待办清单")
        # 交给 WM 托管（这样点击输入框才能拿到键盘焦点），再用 Motif 提示去掉标题栏。
        # 窗口类型保持普通，但用 EWMH 的 SKIP_TASKBAR 不在任务栏/dock 里露面；
        # 被别的窗口盖住后，Alt+Tab 或「再运行一次本程序」都能把它叫回最前面。
        root.withdraw()                          # 先不映射，提示才能在首次建图前生效
        root.attributes("-topmost", bool(self.store.ui["topmost"]))
        root.attributes("-alpha", float(self.store.ui["alpha"]))
        root.update_idletasks()                  # 促使 Tk 先建出 toplevel 的 WM 窗口
        strip_window_decorations(root)
        set_skip_taskbar(root)                   # 任务栏 / dock 里不出现这个图标
        root.configure(bg=C_BORDER)              # 卡片外层留出 1px 边框色
        root.protocol("WM_DELETE_WINDOW", self.quit)

        self.card = tk.Frame(root, bg=C_CARD, bd=0, highlightthickness=0)
        self.card.pack(fill="both", expand=True, padx=1, pady=1)

        self._build_header()
        self._build_input()
        self._build_list()
        self._build_footer()
        self.menu = self._build_menu()

    def _build_header(self):
        self.header = head = tk.Frame(self.card, bg=C_CARD, height=34)
        head.pack(fill="x", side="top")
        head.pack_propagate(False)

        dot = tk.Canvas(head, width=10, height=10, bg=C_CARD, highlightthickness=0)
        dot.create_oval(2, 2, 8, 8, fill=C_ACCENT, outline="")
        dot.pack(side="left", padx=(PAD, 6))

        self.title = tk.Label(head, text="待办事项", bg=C_CARD, fg=C_TEXT, font=self.fonts["bold"])
        self.title.pack(side="left")

        self.count = tk.Label(head, text="", bg=C_CARD, fg=C_DIM, font=self.fonts["small"])
        self.count.pack(side="left", padx=(8, 0))

        close_btn = self._icon_button(head, "close", self.quit)
        close_btn.pack(side="right", padx=(0, PAD - 6))
        self.collapse_btn = self._icon_button(head, "collapse", self.toggle_collapsed)
        self.collapse_btn.pack(side="right")

        for widget in (head, dot, self.title, self.count):
            widget.bind("<Button-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<ButtonRelease-1>", self._drag_end)
            widget.bind("<Button-3>", self._popup_menu)
        head.bind("<Double-Button-1>", lambda event: self.toggle_collapsed())

    def _build_input(self):
        self.input_row = row = tk.Frame(self.card, bg=C_CARD)

        box = tk.Frame(row, bg=C_LIST, highlightbackground=C_BORDER, highlightthickness=1)
        box.pack(side="left", fill="x", expand=True)

        self.entry = tk.Entry(box, bg=C_LIST, fg=C_TEXT, insertbackground=C_TEXT,
                              relief="flat", bd=0, font=self.fonts["base"], highlightthickness=0)
        self.entry.pack(side="left", fill="x", expand=True, padx=8, pady=5)
        self.entry.bind("<Return>", lambda event: self.add_from_entry())
        self.entry.bind("<Escape>", lambda event: self.entry.delete(0, "end"))
        self.entry.bind("<FocusIn>", lambda event: self._clear_placeholder())
        self.entry.bind("<FocusOut>", lambda event: self._set_placeholder())
        self.entry.bind("<Button-1>", self._focus_entry)
        self._set_placeholder()

        add_btn = tk.Label(row, text="+", bg=C_ACCENT, fg="#ffffff", font=self.fonts["bold"],
                           width=2, cursor="hand2")
        add_btn.pack(side="left", padx=(6, 0), ipady=5)
        add_btn.bind("<Button-1>", lambda event: self.add_from_entry())
        add_btn.bind("<Enter>", lambda event: add_btn.configure(bg="#5aa6ff"))
        add_btn.bind("<Leave>", lambda event: add_btn.configure(bg=C_ACCENT))

    def _build_list(self):
        self.list_area = tk.Frame(self.card, bg=C_LIST)
        self.canvas = tk.Canvas(self.list_area, bg=C_LIST, highlightthickness=0, bd=0,
                                width=PANEL_W - 2 * PAD - 2)
        self.canvas.pack(fill="x", expand=True)
        self.inner = tk.Frame(self.canvas, bg=C_LIST)
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self._bind_wheel(self.canvas)
        self._bind_wheel(self.inner)

    def _build_footer(self):
        self.footer = foot = tk.Frame(self.card, bg=C_CARD)
        self.hint = tk.Label(foot, text="双击标题可折叠", bg=C_CARD, fg=C_MUTED, font=self.fonts["small"])
        self.hint.pack(side="left")
        self.clear_btn = tk.Label(foot, text="清除已完成", bg=C_CARD, fg=C_DIM,
                                  font=self.fonts["small"], cursor="hand2")
        self.clear_btn.bind("<Button-1>", lambda event: self.clear_done())
        self.clear_btn.bind("<Enter>", lambda event: self.clear_btn.configure(fg=C_TEXT))
        self.clear_btn.bind("<Leave>", lambda event: self.clear_btn.configure(fg=C_DIM))

    def _build_menu(self):
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="折叠 / 展开", command=self.toggle_collapsed)
        self._topmost_var = tk.BooleanVar(value=bool(self.store.ui["topmost"]))
        menu.add_checkbutton(label="始终置顶", variable=self._topmost_var, command=self._apply_topmost)
        self._alpha_var = tk.DoubleVar(value=float(self.store.ui["alpha"]))
        alpha_menu = tk.Menu(menu, tearoff=0)
        for label, value in (("完全不透明", 1.0), ("半透明 92%", 0.92),
                             ("更透明 82%", 0.82), ("最透明 70%", 0.70)):
            alpha_menu.add_radiobutton(label=label, variable=self._alpha_var,
                                       value=value, command=self._apply_alpha)
        menu.add_cascade(label="窗口透明度", menu=alpha_menu)
        menu.add_separator()
        menu.add_command(label="退出", command=self.quit)
        return menu

    # ---- 小控件 ----
    def _icon_button(self, parent, kind, command):
        canvas = tk.Canvas(parent, width=20, height=20, bg=C_CARD, highlightthickness=0,
                           cursor="hand2")
        canvas._kind = kind
        self._draw_icon(canvas, kind, C_DIM)
        canvas.bind("<Button-1>", lambda event: command())
        canvas.bind("<Enter>", lambda event: self._draw_icon(canvas, kind, C_TEXT))
        canvas.bind("<Leave>", lambda event: self._draw_icon(canvas, kind, C_DIM))
        return canvas

    def _draw_icon(self, canvas, kind, color):
        canvas.delete("all")
        if kind == "close":
            canvas.create_line(6, 6, 14, 14, fill=color, width=1.6, capstyle="round")
            canvas.create_line(14, 6, 6, 14, fill=color, width=1.6, capstyle="round")
        else:  # 折叠箭头：展开时朝下，折叠时朝右
            if self.store.ui["collapsed"]:
                canvas.create_line(8, 5, 13, 10, 8, 15, fill=color, width=1.6,
                                   capstyle="round", joinstyle="round")
            else:
                canvas.create_line(5, 8, 10, 13, 15, 8, fill=color, width=1.6,
                                   capstyle="round", joinstyle="round")

    def _draw_check(self, canvas, done, top=0):
        """在 canvas 上画勾选框，top 为垂直偏移。"""
        canvas.delete("check")
        y = top
        if done:
            canvas.create_rectangle(6, y, 22, y + 16, fill=C_ACCENT, outline="", tags="check")
            canvas.create_line(10, y + 8, 13, y + 11.5, 18.5, y + 4.5, fill="#ffffff",
                               width=2, capstyle="round", joinstyle="round", tags="check")
        else:
            canvas.create_rectangle(6.5, y + 0.5, 21.5, y + 15.5, outline=C_BORDER,
                                    width=1.4, tags="check")

    def _bind_wheel(self, widget):
        widget.bind("<Button-4>", self._on_wheel)
        widget.bind("<Button-5>", self._on_wheel)
        widget.bind("<MouseWheel>", self._on_wheel)

    def _on_wheel(self, event):
        if self.inner.winfo_reqheight() <= LIST_MAX_H:
            return "break"
        up = getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0
        self.canvas.yview_scroll(-2 if up else 2, "units")
        return "break"

    # ---- 输入框占位文字 ----
    def _set_placeholder(self):
        if not self.entry.get():
            self._placeholder_on = True
            self.entry.insert(0, PLACEHOLDER)
            self.entry.configure(fg=C_MUTED)

    def _clear_placeholder(self):
        """清掉占位文字，只删占位本身——用户已经敲进去的字一个都不能丢。"""
        if not self._placeholder_on:
            return
        self._placeholder_on = False
        text = self.entry.get()
        if text != PLACEHOLDER and PLACEHOLDER in text:
            self.entry.delete(0, "end")
            self.entry.insert(0, text.replace(PLACEHOLDER, ""))
        elif text == PLACEHOLDER:
            self.entry.delete(0, "end")
        self.entry.configure(fg=C_TEXT)

    def _focus_entry(self, event=None):
        if self._placeholder_on:
            # 第一次点进来：先删掉提示文字并吞掉这次点击，光标落到文字末尾，
            # 否则默认行为会把插入点放进提示文字中间，打字就打进提示里了。
            self._clear_placeholder()
            self.entry.icursor("end")
            self.entry.focus_force()      # 同步拿焦点，紧接着的第一次按键不能丢
            return "break"
        self.entry.focus_force()

    # ---- 列表刷新 ----
    def _refresh(self, keep_scroll=True):
        try:
            position = self.canvas.yview()[0] if keep_scroll else 0.0
        except tk.TclError:
            position = 0.0

        for child in list(self.inner.winfo_children()):
            child.destroy()
        self.row_widgets = []
        self._press = None
        self._drag_id = None
        self._pointer_y = None
        self._stop_autoscroll()

        if not self.store.items:
            empty = tk.Label(self.inner, text="暂无待办，在上面输入框添加第一条",
                             bg=C_LIST, fg=C_MUTED, font=self.fonts["small"])
            empty.pack(fill="x", pady=20)
            self._bind_wheel(empty)
        else:
            for item in self.store.items:
                self._make_row(item)

        self._update_counts()
        self._update_footer()
        self.root.update_idletasks()
        self._resize()
        if self.inner.winfo_reqheight() > LIST_MAX_H:
            self.canvas.yview_moveto(position)
        else:
            self.canvas.yview_moveto(0.0)

    def _wrap_text(self, text, font, limit):
        """按像素宽度折行（Tk 的 Label 折行在本机渲染不齐，这里自己算）。"""
        lines = []
        for paragraph in str(text).split("\n"):
            current = ""
            for char in paragraph:
                if current and font.measure(current + char) > limit:
                    lines.append(current.rstrip())
                    current = "" if char == " " else char
                else:
                    current += char
            lines.append(current.rstrip())
        return [line for line in lines if line] or [""]

    def _make_row(self, item):
        font = self.fonts["done"] if item["done"] else self.fonts["base"]
        line_h = font.metrics("linespace")
        lines = self._wrap_text(item["text"], font, ROW_WRAP)
        row_h = line_h * len(lines) + 2 * ROW_PAD_Y

        row = tk.Canvas(self.inner, bg=C_LIST, highlightthickness=0, bd=0,
                        width=LIST_W, height=row_h, cursor="hand2")
        row.pack(fill="x")

        self._draw_check(row, item["done"], top=(row_h - 16) // 2)
        for index, line in enumerate(lines):
            row.create_text(TEXT_X, ROW_PAD_Y + index * line_h, text=line, anchor="nw",
                            fill=C_DONE if item["done"] else C_TEXT, font=font)
        delete_id = row.create_text(LIST_W - 12, row_h // 2 + 1, text="×", anchor="e",
                                    fill=C_MUTED, font=self.fonts["bold"])

        item_id = item["id"]

        def on_motion(event):
            row.itemconfigure(delete_id, fill=C_DANGER if event.x >= LIST_W - DEL_W else C_MUTED)

        row.bind("<Button-1>", lambda event, i=item_id: self._row_press(event, i))
        row.bind("<B1-Motion>", self._row_motion)
        row.bind("<ButtonRelease-1>", self._row_release)
        row.bind("<Motion>", on_motion)
        row.bind("<Leave>", lambda event: row.itemconfigure(delete_id, fill=C_MUTED))
        self._bind_wheel(row)

        self.row_widgets.append((item_id, row))

    # ---- 条目拖拽排序 ----
    def _row_press(self, event, item_id):
        """记住按下的位置：按在右侧 × 上算删除，其余地方先当作可能的点击/拖拽。"""
        self._press = {"id": item_id, "y": event.y_root,
                       "zone": "delete" if event.x >= LIST_W - DEL_W else "body"}

    def _row_motion(self, event):
        if self._press is None:
            return
        self._pointer_y = event.y_root
        if self._drag_id is None:
            if self._press["zone"] != "body":
                return
            if abs(event.y_root - self._press["y"]) < 5:
                return                       # 还在阈值内，先不进入拖动，避免影响点击
            self._begin_drag(self._press["id"])
        self._drag_to(event.y_root)

    def _row_release(self, event):
        press, self._press = self._press, None
        if self._drag_id is not None:
            self._end_drag()
            return
        if press is None:
            return
        if press["zone"] == "delete":
            if event.x >= LIST_W - DEL_W:    # 松手时指针仍在 × 上才算删除
                self.remove_item(press["id"])
        else:
            self.toggle_item(press["id"])

    def _begin_drag(self, item_id):
        self._drag_id = item_id
        widget = self._row_widget(item_id)
        if widget is not None:
            # 用 Canvas 图元画高亮底 + 左侧强调条：改 Canvas 的 bg 在这里不会重绘
            height = int(widget.cget("height"))
            widget.configure(cursor="fleur")
            widget.create_rectangle(0, 0, LIST_W, height, fill=C_HOVER, outline="",
                                    tags="dragbg")
            widget.create_rectangle(0, 0, 3, height, fill=C_ACCENT, outline="",
                                    tags="dragbg")
            widget.tag_lower("dragbg")
        self._start_autoscroll()

    def _end_drag(self):
        item_id, self._drag_id = self._drag_id, None
        self._pointer_y = None
        self._stop_autoscroll()
        widget = self._row_widget(item_id)
        if widget is not None:
            widget.configure(cursor="hand2")
            widget.delete("dragbg")
        self.store.save()                    # 新顺序落盘
        self._refresh()                      # 重画，保持行高/滚动位置一致

    def _row_widget(self, item_id):
        for row_id, widget in self.row_widgets:
            if row_id == item_id:
                return widget
        return None

    def _drag_to(self, y_root):
        """把被拖动的条目挪到指针所在的那一行（实时换位，松手才落盘）。"""
        if not self.row_widgets:
            return
        target = None
        for index, (_, widget) in enumerate(self.row_widgets):
            top = widget.winfo_rooty()
            if top <= y_root < top + widget.winfo_height():
                target = index
                break
        if target is None:                   # 拖到列表外：贴到首/尾
            first_top = self.row_widgets[0][1].winfo_rooty()
            target = 0 if y_root < first_top else len(self.row_widgets) - 1
        if target == self._index_of(self._drag_id):
            return
        if self.store.move(self._drag_id, target, save=False):
            self._repack_rows()

    def _index_of(self, item_id):
        for index, (row_id, _) in enumerate(self.row_widgets):
            if row_id == item_id:
                return index
        return -1

    def _repack_rows(self):
        """按 store.items 的新顺序重新摆放行（只动顺序，不重建内容）。"""
        by_id = dict(self.row_widgets)
        ordered = [(item["id"], by_id[item["id"]])
                   for item in self.store.items if item["id"] in by_id]
        if len(ordered) != len(self.row_widgets):
            return
        for _, widget in ordered:
            widget.pack_forget()
        for _, widget in ordered:
            widget.pack(fill="x")
        self.row_widgets = ordered

    def _start_autoscroll(self):
        if self._autoscroll_job is None:
            self._autoscroll_job = self.root.after(70, self._autoscroll_tick)

    def _stop_autoscroll(self):
        if self._autoscroll_job is not None:
            try:
                self.root.after_cancel(self._autoscroll_job)
            except (tk.TclError, ValueError):
                pass
            self._autoscroll_job = None

    def _autoscroll_tick(self):
        """拖到列表上下边缘时持续滚动，方便把条目挪到看不见的位置。"""
        self._autoscroll_job = None
        if self._drag_id is None or self._pointer_y is None:
            return
        if self.inner.winfo_reqheight() > LIST_MAX_H:
            top = self.canvas.winfo_rooty()
            bottom = top + self.canvas.winfo_height()
            if self._pointer_y < top + 14:
                self.canvas.yview_scroll(-1, "units")
                self._drag_to(self._pointer_y)
            elif self._pointer_y > bottom - 14:
                self.canvas.yview_scroll(1, "units")
                self._drag_to(self._pointer_y)
        self._start_autoscroll()

    def _update_counts(self):
        remaining = self.store.remaining()
        if self.store.items and remaining == 0:
            text = "全部完成"
        else:
            text = "%d 项未完成" % remaining
        self.count.configure(text=text)

    def _update_footer(self):
        self._update_hint()
        if self.store.has_done():
            self.clear_btn.pack(side="right")
        else:
            self.clear_btn.pack_forget()

    def _update_hint(self):
        """底部提示：折叠时提示展开，条目够多时提示可拖动排序。"""
        if self.store.ui["collapsed"]:
            self.hint.configure(text="双击标题展开")
        elif len(self.store.items) >= 2:
            self.hint.configure(text="拖动条目可排序")
        else:
            self.hint.configure(text="双击标题可折叠")

    def _on_inner_configure(self, event=None):
        height = min(self.inner.winfo_reqheight(), LIST_MAX_H)
        if int(self.canvas.cget("height")) != height:
            self.canvas.configure(height=height)
        self.canvas.configure(scrollregion=(0, 0, self.inner.winfo_reqwidth(),
                                            self.inner.winfo_reqheight()))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfigure(self.canvas.find_all()[0], width=event.width)

    # ---- 尺寸 / 位置 ----
    def _resize(self):
        self.card.update_idletasks()
        if self.store.ui["collapsed"]:
            height = self.header.winfo_reqheight() + 2
        else:
            height = self.card.winfo_reqheight() + 2
        self.root.geometry("%dx%d" % (PANEL_W, height))

    def _restore_position(self):
        self.root.update_idletasks()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = self.store.ui["x"]
        y = self.store.ui["y"]
        if x is None or y is None:
            x = screen_w - PANEL_W - 48
            y = 96
        x = max(80 - PANEL_W, min(int(x), screen_w - 80))
        y = max(0, min(int(y), screen_h - 40))
        self.root.geometry("+%d+%d" % (x, y))

    def _apply_collapsed(self):
        if self.store.ui["collapsed"]:
            self.input_row.pack_forget()
            self.list_area.pack_forget()
            self.footer.pack_forget()
        else:
            self.input_row.pack(fill="x", padx=PAD, pady=(2, 8))
            self.list_area.pack(fill="x", padx=PAD, pady=(0, 8))
            self.footer.pack(fill="x", padx=PAD, pady=(0, PAD - 4))
        self._update_hint()
        self._draw_icon(self.collapse_btn, "collapse", C_DIM)
        self._resize()
    # ---- 拖动 ----
    def _drag_start(self, event):
        self._drag_offset = (event.x_root - self.root.winfo_x(),
                             event.y_root - self.root.winfo_y())

    def _drag_move(self, event):
        if self._drag_offset is None:
            return
        offset_x, offset_y = self._drag_offset
        self.root.geometry("+%d+%d" % (event.x_root - offset_x, event.y_root - offset_y))

    def _drag_end(self, event):
        if self._drag_offset is None:
            return
        self._drag_offset = None
        self.store.ui["x"] = self.root.winfo_x()
        self.store.ui["y"] = self.root.winfo_y()
        self.store.save()

    def _popup_menu(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    # ---- 动作 ----
    def add_from_entry(self):
        self._clear_placeholder()          # 只清占位文字，用户输入原样保留
        created = self.store.add(self.entry.get())
        self.entry.delete(0, "end")
        if created is not None:
            self._refresh(keep_scroll=False)
        self.entry.focus_force()      # 输入完留在输入框里，提示文字等失焦后再显示
        return created

    def toggle_item(self, item_id):
        self.store.toggle(item_id)
        self._refresh()

    def remove_item(self, item_id):
        self.store.remove(item_id)
        self._refresh()

    def clear_done(self):
        if self.store.clear_done():
            self._refresh(keep_scroll=False)

    def toggle_collapsed(self):
        self.store.ui["collapsed"] = not self.store.ui["collapsed"]
        self.store.save()
        self._apply_collapsed()

    def _apply_topmost(self):
        value = bool(self._topmost_var.get())
        self.store.ui["topmost"] = value
        self.root.attributes("-topmost", value)
        self.store.save()

    def _apply_alpha(self):
        value = float(self._alpha_var.get())
        self.store.ui["alpha"] = value
        self.root.attributes("-alpha", value)
        self.store.save()

    def quit(self):
        try:
            self.store.ui["x"] = self.root.winfo_x()
            self.store.ui["y"] = self.root.winfo_y()
        except tk.TclError:
            pass
        self._stop_autoscroll()
        self._stop_ipc()
        self.store.save()
        self.root.destroy()


# --------------------------------------------------------------------------- #
# 自检 / 自启动
# --------------------------------------------------------------------------- #
def run_check():
    """数据读写与界面构造自检，成功打印 SELFTEST OK 并返回 0。"""
    import tempfile

    problems = []
    temp_dir = tempfile.mkdtemp(prefix="desktop-todo-check-")
    try:
        path = os.path.join(temp_dir, "todos.json")

        store = Store(path)
        if store.items:
            problems.append("新数据文件应为空清单")
        first = store.add("  写周报  ")
        second = store.add("买牛奶")
        if not first or first["text"] != "写周报":
            problems.append("新增条目未去除首尾空白")
        if [item["text"] for item in store.items] != ["买牛奶", "写周报"]:
            problems.append("新增条目未插入到最前")

        reloaded = Store(path)
        if len(reloaded.items) != 2 or reloaded.remaining() != 2:
            problems.append("重新读取后条目不一致")

        reloaded.toggle(second["id"])
        if reloaded.remaining() != 1 or not reloaded.has_done():
            problems.append("切换完成状态失败")
        if Store(path).remaining() != 1:
            problems.append("完成状态未落盘")

        reloaded.ui.update({"x": 123, "y": 45, "collapsed": True, "alpha": 0.82})
        reloaded.save()
        restored = Store(path)
        if (restored.ui["x"], restored.ui["y"], restored.ui["collapsed"]) != (123, 45, True):
            problems.append("窗口状态未正确恢复")
        if abs(restored.ui["alpha"] - 0.82) > 1e-6:
            problems.append("透明度设置未正确恢复")

        if restored.clear_done() != 1 or len(Store(path).items) != 1:
            problems.append("清除已完成失败")
        if not restored.remove(first["id"]) or len(Store(path).items) != 0:
            problems.append("删除条目失败")

        # 拖拽排序：move() 让条目占据目标位置，其余顺序保持
        reorder = Store(path)
        for text in ("A", "B", "C", "D"):
            reorder.add(text)                       # 新增插到最前 → D C B A
        if [item["text"] for item in reorder.items] != ["D", "C", "B", "A"]:
            problems.append("排序前置顺序不对")
        reorder.move(reorder.items[0]["id"], 2)     # 把 D 放到第 3 个位置
        if [item["text"] for item in reorder.items] != ["C", "B", "D", "A"]:
            problems.append("向后拖动排序结果不对")
        reorder.move(reorder.items[2]["id"], 0)     # 把 D 提到最前
        if [item["text"] for item in reorder.items] != ["D", "C", "B", "A"]:
            problems.append("向前拖动排序结果不对")
        if [item["text"] for item in Store(path).items] != ["D", "C", "B", "A"]:
            problems.append("排序结果未落盘")

        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{ 这不是 JSON")
        broken = Store(path)
        if broken.items:
            problems.append("损坏的数据文件应回退为空清单")
        if not os.path.exists(path + ".broken"):
            problems.append("损坏的数据文件未备份")

        # 界面构造（窗口保持隐藏，跑一轮事件循环）
        try:
            root = tk.Tk()
            widget = TodoWidget(root, broken, visible=False)   # 自检不打扰桌面
            for text in ("界面自检一", "界面自检二", "界面自检三"):
                broken.add(text)
            widget._refresh()
            widget.toggle_collapsed()
            widget.toggle_collapsed()
            widget.clear_done()
            # 拖拽排序的代码路径（隐藏窗口下不校验落点几何，只验证能跑通、状态能收尾）
            widget._begin_drag(broken.items[0]["id"])
            widget._drag_to(widget.row_widgets[-1][1].winfo_rooty() + 1)
            if widget._drag_id is None:
                problems.append("拖动状态未建立")
            widget._end_drag()
            if widget._drag_id is not None:
                problems.append("拖动状态未清理")
            widget._refresh()
            root.update()
            root.withdraw()
            root.update()
            root.destroy()
        except Exception as error:      # 不限于 TclError：代码里的笔误也要在这里暴露
            problems.append("界面/拖拽自检失败: %r" % (error,))
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    if problems:
        for problem in problems:
            sys.stderr.write("自检失败: %s\n" % problem)
        return 1
    print("SELFTEST OK")
    return 0


def autostart_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "autostart", "%s.desktop" % APP_ID)


def install_autostart():
    script = os.path.abspath(__file__)
    entry = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=桌面待办清单\n"
        "Name[en]=Desktop To-do\n"
        "Comment=常驻桌面的待办清单挂件\n"
        "Comment[en]=Always-on-top desktop to-do widget\n"
        "Exec=%s %s\n"
        "Icon=checkbox-checked\n"
        "Terminal=false\n"
        "Categories=Utility;\n"
        "StartupNotify=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    ) % (sys.executable, script)
    target = autostart_path()
    directory = os.path.dirname(target)
    if not os.path.isdir(directory):
        os.makedirs(directory)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(entry)
    print("已写入开机自启: %s" % target)
    return 0


def uninstall_autostart():
    target = autostart_path()
    if os.path.exists(target):
        os.remove(target)
        print("已移除开机自启: %s" % target)
    else:
        print("未找到开机自启项: %s" % target)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="桌面待办清单挂件")
    parser.add_argument("--check", action="store_true", help="自检数据读写与界面构造后退出")
    parser.add_argument("--install-autostart", action="store_true", help="写入 ~/.config/autostart 开机自启")
    parser.add_argument("--uninstall-autostart", action="store_true", help="移除开机自启")
    parser.add_argument("--data-file", action="store_true", help="打印数据文件路径后退出")
    args = parser.parse_args(argv)

    if args.check:
        return run_check()
    if args.install_autostart:
        return install_autostart()
    if args.uninstall_autostart:
        return uninstall_autostart()
    if args.data_file:
        print(data_file())
        return 0

    if raise_running_instance():
        print("桌面待办清单已经在运行，已把它叫到最前面（任务栏里没有它，这是唤回方式）。")
        return 0

    root = tk.Tk()
    widget = TodoWidget(root, Store(data_file()))
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
