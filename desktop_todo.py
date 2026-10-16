#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""桌面待办清单挂件（Deepin / X11 + tkinter）。

一个常驻桌面、无边框的小窗口：回车添加、点勾选完成、点右侧小钟标记「等待中」，
点 × 删除，所有改动立即写入 JSON，重开自动恢复待办与窗口位置。

每件事有三种状态：未完成 / 等待中（已着手，但在等别人反馈，自己现在动不了）/ 已完成。

默认**不置顶**：它就是普通窗口，会被别的窗口盖住，不会一直挡在前面；
需要时在右键菜单里勾「始终置顶」（勾选状态会记住）。

它也**不在任务栏 / dock 里显示图标**（EWMH 的 `SKIP_TASKBAR`/`SKIP_PAGER`）。
被别的窗口盖住后，按全局快捷键（Linux，默认 `Super+T`）、用 `Alt+Tab`，
或者**再运行一次本程序**，都能把它叫到最前面。

用法::

    python3 desktop_todo.py                    # 启动挂件；已在运行则把窗口叫到前面
    python3 desktop_todo.py --check            # 自检（数据读写 + 界面构造）后退出
    python3 desktop_todo.py --doctor           # 打印环境/窗口能力诊断报告后退出
    python3 desktop_todo.py --hotkey "Ctrl+Alt+K"    # 改全局唤回快捷键
    python3 desktop_todo.py --install-autostart     # 开机自启
    python3 desktop_todo.py --uninstall-autostart   # 取消开机自启

交互::

    回车 / 「+」      添加待办
    点文字或方框      标记完成 / 取消完成
    点右侧小钟        标记 / 取消「等待中」
    按住条目上下拖     调整顺序（松手即保存）
    双击条目          改文字
    点 ×             删除该条
    拖标题栏          移动窗口
    双击标题栏        折叠 / 展开（右键标题栏还有菜单）
    Esc              清空输入框

数据文件: ``~/.local/share/desktop-todo/todos.json``
"""

import argparse
import json
import re
import ctypes
import os
import shutil
import socket
import sys
import tempfile
import time

try:
    import tkinter as tk
    from tkinter import font as tkfont
except ImportError:            # 没装 python3-tk 时，--doctor / --data-file 仍要能跑出信息
    tk = None
    tkfont = None

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
UNDO_SECONDS = 8       # 删除后「撤销」保留的秒数
ARCHIVE_MAX = 500      # 归档最多留多少条（超了丢最老的）
TAG_MAX = 3            # 一条最多几个情境标签
TAG_LEN = 8            # 单个标签最长几个字
TAG_RE = re.compile(r"(?:^|\s)@([\w\u4e00-\u9fff]{1,%d})(?=\s|$)" % TAG_LEN)
DOUBLE_CLICK_REVERT_S = 0.8   # 双击进入编辑时，把第一次单击的勾选撤回的时间窗

# 条目状态：未完成 / 等待中 / 已完成
# 「等待中」= 已经着手了，但在等别人反馈（现在自己动不了），所以既不算完成、也不该被当成待办催自己。
STATE_TODO = "todo"
STATE_WAITING = "waiting"
STATE_DONE = "done"
# 列表默认排序：未完成 → 等待中 → 已完成（同组内保持手动拖出来的顺序）
STATE_ORDER = (STATE_TODO, STATE_WAITING, STATE_DONE)
WAIT_W = 30             # 条目右侧「等待」按钮的点击区宽度
TODAY_W = 26            # 条目右侧「今天」按钮的点击区宽度
PRIO_W = 26             # 条目右侧「优先级」按钮的点击区宽度
STEPS_W = 30            # 条目右侧「子步骤」按钮的点击区宽度
STEP_MAX = 20           # 一条待办最多几个子步骤（防止数据被灌爆）
STEP_BOX = 11           # 子步骤小方框边长
STEP_INDENT = 20        # 子步骤文字相对条目文字的缩进

# 优先级：0 无 / 1 中 / 2 高（值越大越靠前）；点一下按这个顺序循环
PRIORITY_NONE, PRIORITY_MID, PRIORITY_HIGH = 0, 1, 2
PRIORITY_CYCLE = (PRIORITY_NONE, PRIORITY_HIGH, PRIORITY_MID)

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
C_WAIT = "#e0a33e"     # 等待中文字 / 图标
C_HIGH = "#ff7a59"     # 高优先级旗子

PLACEHOLDER = "添加一条待办，回车确认"
FONT_STACK = (
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "WenQuanYi Micro Hei",
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "Droid Sans Fallback",
    "DejaVu Sans",
)


# --------------------------------------------------------------------------- #
# 界面缩放（高 DPI / 系统缩放）
#
# Tk 的字号单位是「点」，会随显示器 DPI 自动放大；而本程序的窗口尺寸是像素，
# 不会跟着变。于是 125% / 150% 缩放下会出现「字变大、面板不变」：文字挤、
# 列表变窄、标题栏发紧。这里用统一的缩放系数把像素尺寸按同样比例放大。
#
# 系数取自 Tk 自己算的 tk scaling（X11 依据 Xft.dpi，Windows 依据系统 DPI），
# 与字号用的是同一套依据，不会出现两套比例打架。缩放为 1.0 时，
# 下面所有 _scaled() 的返回值都与基准常量逐像素相同——即默认观感一点不变。
# --------------------------------------------------------------------------- #
TK_SCALING_AT_96DPI = 96.0 / 72.0     # Tk 的 scaling 就是「每点多少像素」


def ui_scale(root):
    """返回界面缩放系数（1.0 = 96dpi / 100%）。"""
    try:
        scaling = float(root.tk.call("tk", "scaling"))
    except (tk.TclError, ValueError, TypeError, AttributeError):
        return 1.0
    if scaling <= 0:
        return 1.0
    return max(1.0, min(3.0, scaling / TK_SCALING_AT_96DPI))


def _scaled(value, scale):
    """基准像素值 → 当前缩放下的像素值。

    scale=1.0 时**原样返回**（不改动、也不取整）：像勾选框的 6.5 / 21.5 这种半像素
    值一旦走 round() 会被银行家舍入（6.5→6、21.5→22），在 100% 缩放下反而比原来
    偏 0.5~1px，那就违反了「默认观感一点不变」。
    """
    if scale == 1.0:
        return value
    return int(round(value * scale))


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
# 全局快捷键（X11：把键抓在根窗口上，不需要 WM 配合）
#
# 为什么需要它：这个窗口刻意不进任务栏、也没有托盘图标（Deepin 23 的 dde-dock
# 只画它自己「持有列表」里的托盘项，第三方注册得再规范也不显示），
# 于是「把窗口叫回来」就只剩「再运行一次本程序」。全局快捷键把这个入口补上：
# 无论当前焦点在哪个窗口，按一下就能把它提到最前。
#
# XGrabKey 抓在根窗口上、owner_events=False：按键事件直接进我们这个 X 连接的队列，
# 所以用一个 Tk 定时器轮询就够了，不需要线程，也不需要动 Tk 自己的事件循环。
# --------------------------------------------------------------------------- #
HOTKEY_DEFAULT = "Super+T"

_HOTKEY_MODS = {
    "shift": 0x0001, "lock": 0x0002, "ctrl": 0x0004, "control": 0x0004,
    "mod1": 0x0008, "alt": 0x0008, "meta": 0x0008, "mod2": 0x0010,
    "mod3": 0x0020, "mod4": 0x0040, "super": 0x0040, "win": 0x0040,
    "cmd": 0x0040, "mod5": 0x0080,
}
_HOTKEY_KEYS = {
    "space": "space", "esc": "Escape", "escape": "Escape", "enter": "Return",
    "return": "Return", "tab": "Tab", "backspace": "BackSpace", "insert": "Insert",
    "delete": "Delete", "home": "Home", "end": "End", "up": "Up", "down": "Down",
    "left": "Left", "right": "Right", "pageup": "Prior", "pagedown": "Next",
    "comma": "comma", "period": "period", "slash": "slash", "semicolon": "semicolon",
    "quote": "apostrophe", "backslash": "backslash", "minus": "minus", "equal": "equal",
    "grave": "grave", "bracketleft": "bracketleft", "bracketright": "bracketright",
}
_HOTKEY_LOCK_MASKS = (0x0000, 0x0002, 0x0010, 0x0002 | 0x0010)   # 无 / 大小写锁 / 数字锁 / 两者


def parse_hotkey(spec):
    """把 ``"Super+T"`` / ``"Ctrl+Alt+K"`` / ``"F5"`` 解析成 (修饰位掩码, keysym 名)。

    认不出来就返回 None（调用方据此退回默认值），不抛异常。
    """
    text = str(spec or "").replace(" ", "")
    if not text:
        return None
    mods, key = 0, None
    for piece in text.replace("-", "+").split("+"):
        piece = piece.strip().lower()
        if not piece:
            continue
        if piece in _HOTKEY_MODS:
            mods |= _HOTKEY_MODS[piece]
            continue
        if key is not None:
            return None                       # 出现了第二个主键，认不出来
        if piece in _HOTKEY_KEYS:
            key = _HOTKEY_KEYS[piece]
        elif len(piece) == 1 and ord(piece) < 128:
            key = piece
        elif len(piece) <= 3 and piece[0] == "f" and piece[1:].isdigit() \
                and 1 <= int(piece[1:]) <= 24:
            key = piece.upper()
        else:
            return None
    if key is None:
        return None
    return mods, key


def format_hotkey(spec):
    """把快捷键写成统一好看的样子（认不出来就原样返回）。"""
    parsed = parse_hotkey(spec)
    if parsed is None:
        return str(spec or "")
    mods, key = parsed
    names = []
    for name, mask in (("Ctrl", 0x0004), ("Alt", 0x0008), ("Shift", 0x0001),
                       ("Super", 0x0040)):
        if mods & mask:
            names.append(name)
    if key.startswith("F") and key[1:].isdigit():
        shown = key
    elif len(key) == 1:
        shown = key.upper()
    else:
        shown = {"space": "Space", "Return": "Enter", "Escape": "Esc", "Prior": "PageUp",
                 "Next": "PageDown", "BackSpace": "Backspace"}.get(key, key)
    return "+".join(names + [shown])


class _XKeyEvent(ctypes.Structure):
    """XKeyEvent 的前半段（XEvent 是联合体，这里只按 KeyPress 解释）。"""

    _fields_ = [
        ("type", ctypes.c_int), ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong), ("root", ctypes.c_ulong),
        ("subwindow", ctypes.c_ulong), ("time", ctypes.c_ulong),
        ("x", ctypes.c_int), ("y", ctypes.c_int),
        ("x_root", ctypes.c_int), ("y_root", ctypes.c_int),
        ("state", ctypes.c_uint), ("keycode", ctypes.c_uint),
        ("same_screen", ctypes.c_int),
    ]


class X11Hotkey(object):
    """X11 全局快捷键：抓根窗口 + Tk 定时器轮询。"""

    POLL_MS = 120
    KEY_PRESS = 2
    GRAB_MODE_ASYNC = 1

    def __init__(self):
        self.display = None
        self.root_window = 0
        self.keycode = 0
        self.masks = []
        self.callback = None
        self.job = None
        self.spec = None

    # ---- 注册 / 注销 ----
    def install(self, root, spec, callback):
        parsed = parse_hotkey(spec)
        if parsed is None:
            return False
        mods, key = parsed
        x11 = self._open()
        if x11 is None:
            return False
        keysym = x11.XStringToKeysym(key.encode("ascii"))
        if not keysym:
            self._close(x11)
            return False
        keycode = x11.XKeysymToKeycode(ctypes.c_void_p(self.display), ctypes.c_ulong(keysym))
        if not keycode:
            self._close(x11)
            return False

        self.root_window = x11.XDefaultRootWindow(ctypes.c_void_p(self.display))
        self.keycode = int(keycode)
        self.masks = [mods | extra for extra in _HOTKEY_LOCK_MASKS]
        # 抓的时候把 X 的默认错误处理器换掉：键被别的程序占了是 BadAccess，
        # 默认处理器会直接把进程打掉，必须自己收下这次错误。
        failed = []
        handler = self._handler_type(lambda display, event: failed.append(True) or 0)
        self._handler = handler                    # 别让回调被回收
        previous = x11.XSetErrorHandler(ctypes.cast(handler, ctypes.c_void_p))
        try:
            for mask in self.masks:
                x11.XGrabKey(ctypes.c_void_p(self.display), ctypes.c_int(self.keycode),
                             ctypes.c_uint(mask), ctypes.c_ulong(self.root_window),
                             ctypes.c_int(0), ctypes.c_int(self.GRAB_MODE_ASYNC),
                             ctypes.c_int(self.GRAB_MODE_ASYNC))
            x11.XSync(ctypes.c_void_p(self.display), ctypes.c_int(0))
        finally:
            x11.XSetErrorHandler(previous)
        if failed:
            self.remove()
            self._close(x11)
            return False
        self.callback = callback
        self.spec = spec
        self.job = (root, root.after(self.POLL_MS, self._poll, root))
        return True

    def remove(self):
        """注销（幂等）：先撤 after 任务，再解抓、关连接。"""
        if self.job is not None:
            root, job_id = self.job
            self.job = None
            try:
                root.after_cancel(job_id)
            except Exception:
                pass
        if self.display is None:
            return
        x11 = self._x11
        if x11 is not None and self.keycode:
            for mask in self.masks:
                try:
                    x11.XUngrabKey(ctypes.c_void_p(self.display), ctypes.c_int(self.keycode),
                                   ctypes.c_uint(mask), ctypes.c_ulong(self.root_window))
                except Exception:
                    pass
            try:
                x11.XSync(ctypes.c_void_p(self.display), ctypes.c_int(0))
            except Exception:
                pass
        self._close(x11)

    # ---- 内部 ----
    _x11 = None
    _handler_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)

    def _open(self):
        """开一条自己的 X 连接（必须活着，抓的键才有效）。"""
        try:
            x11 = ctypes.CDLL("libX11.so.6")
            x11.XOpenDisplay.restype = ctypes.c_void_p
            x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
            x11.XDefaultRootWindow.restype = ctypes.c_ulong
            x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
            x11.XStringToKeysym.restype = ctypes.c_ulong
            x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
            x11.XKeysymToKeycode.restype = ctypes.c_ubyte
            x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            x11.XSetErrorHandler.restype = ctypes.c_void_p
            x11.XSetErrorHandler.argtypes = [ctypes.c_void_p]
            x11.XPending.restype = ctypes.c_int
            x11.XPending.argtypes = [ctypes.c_void_p]
        except (OSError, AttributeError):
            return None
        display = x11.XOpenDisplay(None)
        if not display:
            return None
        self.display = display
        self._x11 = x11
        return x11

    def _close(self, x11):
        if self.display is not None and x11 is not None:
            try:
                x11.XCloseDisplay(ctypes.c_void_p(self.display))
            except Exception:
                pass
        self.display = None
        self.keycode = 0
        self.masks = []

    def _poll(self, root):
        """定时器回调：把队列里的事件读掉，命中快捷键就回调。"""
        self.job = None
        x11 = self._x11
        if x11 is not None and self.display is not None:
            try:
                buffer = ctypes.create_string_buffer(192)
                while x11.XPending(ctypes.c_void_p(self.display)):
                    x11.XNextEvent(ctypes.c_void_p(self.display), buffer)
                    event = ctypes.cast(buffer, ctypes.POINTER(_XKeyEvent)).contents
                    if event.type == self.KEY_PRESS and self.callback is not None:
                        try:
                            self.callback()
                        except Exception:
                            pass
            except Exception:
                pass
        try:
            self.job = (root, root.after(self.POLL_MS, self._poll, root))
        except tk.TclError:
            self.job = None


# --------------------------------------------------------------------------- #
# Win32 助手（纯 ctypes，零第三方依赖；只在 os.name == "nt" 上被调用）
#
# 与上面 X11 那套的对应关系：
#   * winfo_id() 给的是 Tk 的子窗口，真正的顶层窗口要用 GetAncestor(GA_ROOT)
#     —— 等价于 X11 那边用 XQueryTree 往上找 WM frame；
#   * 去标题栏 = 清掉 WS_CAPTION | WS_THICKFRAME，窗口仍由系统托管
#     —— 等价于 _MOTIF_WM_HINTS 的 decorations=0，输入框照样能拿到键盘焦点；
#   * 不进任务栏 = 加 WS_EX_TOOLWINDOW —— 等价于 EWMH 的 SKIP_TASKBAR
#     （代价是 Alt+Tab 里也看不到它，所以唤回仍然靠「再运行一次本程序」）。
# 以上三条已于 2026-10-11 在 Win11 + Python 3.13 实测有效。
# --------------------------------------------------------------------------- #
WIN_GWL_STYLE = -16
WIN_GWL_EXSTYLE = -20
WIN_WS_CAPTION = 0x00C00000
WIN_WS_THICKFRAME = 0x00040000
WIN_WS_EX_TOOLWINDOW = 0x00000080
WIN_WS_EX_APPWINDOW = 0x00040000
WIN_SWP_NOSIZE = 0x0001
WIN_SWP_NOMOVE = 0x0002
WIN_SWP_NOZORDER = 0x0004
WIN_SWP_NOACTIVATE = 0x0010
WIN_SWP_FRAMECHANGED = 0x0020
WIN_GA_ROOT = 2
WIN_SW_RESTORE = 9
WIN_ERROR_ALREADY_EXISTS = 183


def _win_api():
    """加载 user32/kernel32 并声明函数签名；非 Windows 或失败时返回 None。"""
    if os.name != "nt":
        return None
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except Exception:
        return None
    try:
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetAncestor.restype = ctypes.c_void_p
        user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.GetWindowLongW.restype = ctypes.c_long
        user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
        user32.SetWindowLongW.restype = ctypes.c_long
        user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_uint]
        user32.SetWindowPos.restype = ctypes.c_int
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.ShowWindow.restype = ctypes.c_int
        user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        user32.FindWindowW.restype = ctypes.c_void_p
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        user32.SetForegroundWindow.restype = ctypes.c_int
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
    except Exception:
        return None
    return user32, kernel32


def win32_top_window(root):
    """返回 Tk toplevel 对应的顶层窗口句柄；非 Windows / 取不到时返回 0。"""
    api = _win_api()
    if api is None:
        return 0
    try:
        return int(api[0].GetAncestor(int(root.winfo_id()), WIN_GA_ROOT) or 0)
    except Exception:
        return 0


def win32_window_state(root):
    """读取顶层窗口样式，返回 hwnd/style/exstyle/caption/thickframe/toolwindow/appwindow。"""
    state = {"hwnd": 0, "style": 0, "exstyle": 0, "caption": False,
             "thickframe": False, "toolwindow": False, "appwindow": False}
    api = _win_api()
    if api is None:
        return state
    hwnd = win32_top_window(root)
    state["hwnd"] = hwnd
    if not hwnd:
        return state
    user32 = api[0]
    style = int(user32.GetWindowLongW(hwnd, WIN_GWL_STYLE)) & 0xFFFFFFFF
    exstyle = int(user32.GetWindowLongW(hwnd, WIN_GWL_EXSTYLE)) & 0xFFFFFFFF
    state["style"] = style
    state["exstyle"] = exstyle
    state["caption"] = bool(style & WIN_WS_CAPTION)
    state["thickframe"] = bool(style & WIN_WS_THICKFRAME)
    state["toolwindow"] = bool(exstyle & WIN_WS_EX_TOOLWINDOW)
    state["appwindow"] = bool(exstyle & WIN_WS_EX_APPWINDOW)
    return state


def win32_make_borderless(root):
    """去标题栏 + 不进任务栏；返回 True 表示 Win32 调用都成功（不保证一定被系统接受）。"""
    api = _win_api()
    if api is None:
        return False
    user32 = api[0]
    hwnd = win32_top_window(root)
    if not hwnd:
        return False
    style = int(user32.GetWindowLongW(hwnd, WIN_GWL_STYLE))
    exstyle = int(user32.GetWindowLongW(hwnd, WIN_GWL_EXSTYLE))
    user32.SetWindowLongW(hwnd, WIN_GWL_STYLE,
                          style & ~(WIN_WS_CAPTION | WIN_WS_THICKFRAME))
    user32.SetWindowLongW(hwnd, WIN_GWL_EXSTYLE,
                          (exstyle | WIN_WS_EX_TOOLWINDOW) & ~WIN_WS_EX_APPWINDOW)
    flags = (WIN_SWP_NOMOVE | WIN_SWP_NOSIZE | WIN_SWP_NOZORDER
             | WIN_SWP_NOACTIVATE | WIN_SWP_FRAMECHANGED)
    return bool(user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, flags))


def win32_find_window(title):
    """按标题找顶层窗口；找不到返回 0。"""
    api = _win_api()
    if api is None:
        return 0
    try:
        return int(api[0].FindWindowW(None, title) or 0)
    except Exception:
        return 0


def win32_activate_hwnd(hwnd):
    """把指定窗口恢复并切到前台；返回是否成功。"""
    api = _win_api()
    if api is None or not hwnd:
        return False
    user32 = api[0]
    user32.ShowWindow(hwnd, WIN_SW_RESTORE)
    return bool(user32.SetForegroundWindow(hwnd))


def win32_activate(root):
    """把已有窗口叫到最前面（「再运行一次本程序」的唤回动作用它）。"""
    return win32_activate_hwnd(win32_top_window(root))


def win32_instance_mutex(name):
    """返回 (互斥体句柄, 是否已有实例在跑)。句柄交给调用方 CloseHandle。"""
    api = _win_api()
    if api is None:
        return 0, False
    handle = api[1].CreateMutexW(None, 0, name)
    return int(handle or 0), ctypes.get_last_error() == WIN_ERROR_ALREADY_EXISTS


def win32_set_dpi_aware():
    """必须在创建 Tk() 之前调用：声明进程 DPI 感知，避免高缩放下的模糊与布局错位。"""
    if os.name != "nt":
        return False
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        return shcore.SetProcessDpiAwareness(1) == 0
    except Exception:
        return False


def _win_has_dpi_api():
    """shcore 的 DPI 感知接口是否可用（只用来报告，不会真的改进程状态）。"""
    if os.name != "nt":
        return False
    try:
        return hasattr(ctypes.WinDLL("shcore", use_last_error=True),
                       "SetProcessDpiAwareness")
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# 托盘图标（Win32 Shell_NotifyIcon，纯 ctypes，零第三方依赖）
#
# 为什么需要：窗口默认不进任务栏，Windows 上也不在 Alt+Tab 里（WS_EX_TOOLWINDOW
# 的代价），被别的窗口盖住后不好找。挂一个托盘图标后：左键唤回窗口，右键弹出与本
# 程序一致的菜单（含「隐藏窗口」「退出」）。
#
# 为什么用「轮询」而不是 Shell_NotifyIcon 的回调消息：
#   回调消息是发给某个窗口的，正常做法是把 Tk 顶层窗口的 WndProc 子类化，再从
#   C 回调里回到 Python。2026-10-11 实测：那样会在 Tk 的 update() 内部触发
#       Fatal Python error: PyEval_RestoreThread: ... the GIL is released
#   直接把进程打崩。所以改成：挂图标时**不要**回调消息（不设 NIF_MESSAGE），
#   在 Tk 自己的定时器里每 TRAY_POLL_MS 毫秒查一次「光标是否落在本图标矩形内 +
#   左/右键是否刚松开」——全程只有 Tk 的事件循环，没有任何 C→Python 回调。
#
# 只在 Windows 上启用；Linux 侧仍是「再运行一次本程序」唤回（见 X11Backend）。
# --------------------------------------------------------------------------- #
TRAY_UID = 1
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_ICON, NIF_TIP = 0x00000002, 0x00000004
IDI_APPLICATION = 32512
TRAY_TIP = "桌面待办清单（左键显示 / 右键菜单）"
TRAY_POLL_MS = 40                       # 轮询托盘点击的间隔（快点击的按下阶段很短，别太稀）
VK_LBUTTON, VK_RBUTTON = 0x01, 0x02


class _NotifyIconData(ctypes.Structure):
    """NOTIFYICONDATAW（用 v2 的大小，够用；不含 v3 的 guidItem）。"""

    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("hWnd", ctypes.c_void_p),
        ("uID", ctypes.c_uint),
        ("uFlags", ctypes.c_uint),
        ("uCallbackMessage", ctypes.c_uint),
        ("hIcon", ctypes.c_void_p),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", ctypes.c_uint),
        ("dwStateMask", ctypes.c_uint),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", ctypes.c_uint),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", ctypes.c_uint),
    ]


class _NotifyIconIdentifier(ctypes.Structure):
    """NOTIFYICONIDENTIFIER：给 Shell_NotifyIconGetRect 定位图标用。"""

    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("hWnd", ctypes.c_void_p),
        ("uID", ctypes.c_uint),
        ("guidItem", ctypes.c_byte * 16),
    ]


class _TrayRect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def _shell32():
    if os.name != "nt":
        return None
    try:
        return ctypes.WinDLL("shell32", use_last_error=True)
    except Exception:
        return None


def win32_tray_icon():
    """取一个托盘图标句柄：暂时用系统默认图标（自绘图标以后再说）。"""
    api = _win_api()
    if api is None:
        return 0
    user32 = api[0]
    user32.LoadIconW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.LoadIconW.restype = ctypes.c_void_p
    return int(user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION)) or 0)


def _notify_data(hwnd, hicon=None, tip=None):
    data = _NotifyIconData()
    data.cbSize = ctypes.sizeof(_NotifyIconData)
    data.hWnd = ctypes.c_void_p(hwnd)
    data.uID = TRAY_UID
    if hicon:
        data.uFlags |= NIF_ICON
        data.hIcon = ctypes.c_void_p(hicon)
    if tip:
        data.uFlags |= NIF_TIP
        data.szTip = tip
    # 故意不设 NIF_MESSAGE：不用回调消息（原因见本节顶部注释）
    return data


def win32_tray_add(hwnd, hicon, tip=TRAY_TIP):
    """把图标挂进托盘；返回是否成功。"""
    shell32 = _shell32()
    if shell32 is None or not hwnd:
        return False
    shell32.Shell_NotifyIconW.argtypes = [ctypes.c_uint,
                                          ctypes.POINTER(_NotifyIconData)]
    shell32.Shell_NotifyIconW.restype = ctypes.c_int
    data = _notify_data(hwnd, hicon, tip)
    return bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)))


def win32_tray_remove(hwnd):
    """从托盘摘掉图标（窗口销毁前必须调用）。"""
    shell32 = _shell32()
    if shell32 is None or not hwnd:
        return False
    shell32.Shell_NotifyIconW.argtypes = [ctypes.c_uint,
                                          ctypes.POINTER(_NotifyIconData)]
    shell32.Shell_NotifyIconW.restype = ctypes.c_int
    data = _notify_data(hwnd)
    return bool(shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data)))


def win32_tray_rect(hwnd):
    """图标在屏幕上的矩形：返回 (是否可见, left, top, right, bottom)。

    图标被收进托盘溢出区、当前没显示时会返回「不可见」——那种情况下本来就点不到它。
    """
    shell32 = _shell32()
    if shell32 is None or not hwnd:
        return (False, 0, 0, 0, 0)
    identifier = _NotifyIconIdentifier()
    identifier.cbSize = ctypes.sizeof(_NotifyIconIdentifier)
    identifier.hWnd = ctypes.c_void_p(hwnd)
    identifier.uID = TRAY_UID
    rect = _TrayRect()
    shell32.Shell_NotifyIconGetRect.argtypes = [ctypes.POINTER(_NotifyIconIdentifier),
                                               ctypes.POINTER(_TrayRect)]
    shell32.Shell_NotifyIconGetRect.restype = ctypes.c_long        # HRESULT
    if shell32.Shell_NotifyIconGetRect(ctypes.byref(identifier),
                                       ctypes.byref(rect)) != 0:   # S_OK == 0
        return (False, 0, 0, 0, 0)
    return (True, int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


def win32_mouse_buttons():
    """(左键按下, 右键按下, 左键刚按过, 右键刚按过)。

    前两项取 GetAsyncKeyState 的高位（当前状态）；后两项取最低位，含义是「自上次查询
    以来按过」——**快点击的按下阶段可能只有几十毫秒，整个落在两次轮询之间，只看当前
    状态会漏掉**（2026-10-11 实测漏过一次）。系统允许别的程序把这个位抢先读走，所以
    它只当补充信号，不当唯一判据。
    """
    api = _win_api()
    if api is None:
        return (False, False, False, False)
    user32 = api[0]
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
    left_state = int(user32.GetAsyncKeyState(VK_LBUTTON))
    right_state = int(user32.GetAsyncKeyState(VK_RBUTTON))
    return (bool(left_state & 0x8000), bool(right_state & 0x8000),
            bool(left_state & 0x0001), bool(right_state & 0x0001))


def win32_cursor_pos():
    """鼠标当前位置（托盘右键菜单要弹在那儿）。取不到返回 (0, 0)。"""
    api = _win_api()
    if api is None:
        return (0, 0)
    user32 = api[0]

    class _Point(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    point = _Point()
    user32.GetCursorPos.argtypes = [ctypes.POINTER(_Point)]
    user32.GetCursorPos.restype = ctypes.c_int
    if not user32.GetCursorPos(ctypes.byref(point)):
        return (0, 0)
    return (int(point.x), int(point.y))


def win32_has_tray_api():
    """shell32 里有没有 Shell_NotifyIconW / Shell_NotifyIconGetRect（给 --doctor 用）。"""
    shell32 = _shell32()
    return bool(shell32 is not None and hasattr(shell32, "Shell_NotifyIconW")
                and hasattr(shell32, "Shell_NotifyIconGetRect"))


# --------------------------------------------------------------------------- #
# 平台后端
#
# 平台差异只有三处：窗口外观、数据与自启位置、单实例唤回。都收在这里：
#   * X11Backend   —— 就是本项目原来那套 X11 / EWMH 代码，逐条对齐，不改写行为；
#   * WindowsBackend —— 纯 ctypes 的 Win32 调用（2026-10-11 在 Win11 上实测过）。
# 业务代码只认 PlatformBackend 的接口，不再直接碰平台 API。
# --------------------------------------------------------------------------- #
WINDOW_TITLE = "桌面待办清单"
AUTOSTART_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


class PlatformBackend(object):
    """平台能力接口；没实现的地方给保守默认值。"""

    name = "base"

    def prepare_process(self):
        """在创建 Tk() 之前调用（Windows 的 DPI 感知必须在 Tk 之前声明）。"""

    def strip_decorations(self, root):
        """去掉标题栏。返回是否成功。"""
        return False

    def set_skip_taskbar(self, root):
        """让窗口不出现在任务栏 / dock 里。返回是否成功。"""
        return False

    def tray_install(self, root, on_activate, on_menu):
        """挂托盘图标：on_activate() 左键唤回，on_menu() 右键弹菜单。返回是否成功。"""
        return False

    def tray_remove(self, root):
        """摘掉托盘图标（窗口销毁前调用）。"""

    def hotkey_install(self, root, spec, callback):
        """注册全局快捷键（按下时调用 callback()）。返回是否成功。"""
        return False

    def hotkey_remove(self):
        """注销全局快捷键（退出前调用）。"""

    def activate_window(self, root, topmost):
        """把窗口叫到最前面，且不改变用户设置的「始终置顶」状态。"""
        try:
            root.deiconify()
            root.lift()
            root.attributes("-topmost", True)      # 短暂置顶，确保能从别的窗口后面冒出来
            root.focus_force()
            root.after(400, lambda: root.attributes("-topmost", bool(topmost)))
        except tk.TclError:
            pass

    def single_instance_start(self, root, on_raise):
        """接管单实例：第二个进程需要唤回时调用 on_raise()。"""

    def single_instance_stop(self, root):
        """释放单实例占用的资源。"""

    def raise_running_instance(self):
        """已有实例在跑就把它叫到最前面并返回 True。"""
        return False

    def data_dir(self):
        """数据目录（Linux 遵循 XDG_DATA_HOME）。"""
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "share")
        return os.path.join(base, APP_ID)

    def autostart_target(self):
        """开机自启的落点（给人看的字符串）。"""
        return "(当前平台未实现)"

    def app_entry_target(self):
        """应用菜单条目的落点（Linux 需要它来解析自启项的应用 id）。"""
        return "(当前平台不适用)"

    def autostart_install(self):
        """写入开机自启，返回退出码。"""
        sys.stderr.write("当前平台未实现开机自启\n")
        return 1

    def autostart_uninstall(self):
        """移除开机自启，返回退出码。"""
        sys.stderr.write("当前平台未实现开机自启\n")
        return 1


class X11Backend(PlatformBackend):
    """Linux / X11：与原实现逐条等价。"""

    name = "x11"

    def __init__(self):
        self._server = None
        self._hotkey = None

    def strip_decorations(self, root):
        return strip_window_decorations(root)

    def set_skip_taskbar(self, root):
        return set_skip_taskbar(root)

    # ---- 单实例：unix 套接字 + Tk 文件事件（原 _start_ipc / _on_ipc / _stop_ipc）----
    def single_instance_start(self, root, on_raise):
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
        self._server = server

        def on_readable(_server, _mask):
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
            on_raise()

        try:
            root.tk.createfilehandler(server, tk.READABLE, on_readable)
        except (tk.TclError, AttributeError):
            self._server = None

    def single_instance_stop(self, root):
        if self._server is None:
            return
        path = ipc_socket_path()
        try:
            root.tk.deletefilehandler(self._server)
        except (tk.TclError, AttributeError):
            pass
        try:
            self._server.close()
        except OSError:
            pass
        self._server = None
        try:
            if os.path.exists(path):
                os.unlink(path)
        except OSError:
            pass

    def raise_running_instance(self):
        return raise_running_instance()

    # ---- 全局快捷键：XGrabKey 抓根窗口（不占任务栏、也没有托盘时的唤回入口）----
    def hotkey_install(self, root, spec, callback):
        self._hotkey = X11Hotkey()
        if self._hotkey.install(root, spec, callback):
            return True
        self._hotkey = None
        return False

    def hotkey_remove(self):
        if getattr(self, "_hotkey", None) is not None:
            self._hotkey.remove()
            self._hotkey = None

    # ---- 开机自启：~/.config/autostart/*.desktop ----
    #
    # Deepin / UOS 上只写 autostart 那条是不够的：startdde 会盯着这个目录，
    # 新建的条目如果解析不出「应用 id」就会被它删掉（journal 里是
    # `startmanager.go: RemoveAutostart failed: failed to get app id`），
    # 实测创建后约 30 秒消失。所以这里同时写一条同名的应用条目到
    # ~/.local/share/applications/，让 app id（= 文件名）能对上；顺带也给了
    # 用户一个「应用菜单里点一下就唤回」的入口。
    def autostart_target(self):
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config")
        return os.path.join(base, "autostart", "%s.desktop" % APP_ID)

    def app_entry_target(self):
        """应用菜单条目的路径（Deepin 靠它解析自启项的应用 id）。"""
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "share")
        return os.path.join(base, "applications", "%s.desktop" % APP_ID)

    def _desktop_entry(self, autostart):
        """autostart=True 时多带自启相关的键。"""
        script = os.path.abspath(__file__)
        entry = [
            "[Desktop Entry]",
            "Type=Application",
            "Name=桌面待办清单",
            "Name[en]=Desktop To-do",
            "Comment=常驻桌面的待办清单挂件",
            "Comment[en]=Always-on-top desktop to-do widget",
            "Exec=%s %s" % (sys.executable, script),
            "Icon=checkbox-checked",
            "Terminal=false",
            "Categories=Utility;",
            "StartupWMClass=%s" % APP_ID,
            "X-Deepin-AppID=%s" % APP_ID,
            "X-Deepin-Vendor=user-custom",
        ]
        if autostart:
            entry += ["StartupNotify=false", "X-GNOME-Autostart-enabled=true"]
        return "\n".join(entry) + "\n"

    def _write_entry(self, path, text):
        directory = os.path.dirname(path)
        if not os.path.isdir(directory):
            os.makedirs(directory)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def autostart_install(self):
        app_entry = self.app_entry_target()
        self._write_entry(app_entry, self._desktop_entry(autostart=False))
        target = self.autostart_target()
        self._write_entry(target, self._desktop_entry(autostart=True))
        print("已写入开机自启: %s" % target)
        print("已写入应用菜单条目: %s（Deepin/UOS 用它解析自启项的应用 id）" % app_entry)
        return 0

    def autostart_uninstall(self):
        target = self.autostart_target()
        if os.path.exists(target):
            os.remove(target)
            print("已移除开机自启: %s" % target)
        else:
            print("未找到开机自启项: %s" % target)
        app_entry = self.app_entry_target()
        if os.path.exists(app_entry):
            os.remove(app_entry)
            print("已移除应用菜单条目: %s" % app_entry)
        return 0


class WindowsBackend(PlatformBackend):
    """Windows 11：纯 ctypes 的 Win32 实现。

    去标题栏与「不进任务栏」是同一次样式修改（清 WS_CAPTION/WS_THICKFRAME +
    加 WS_EX_TOOLWINDOW），所以两个方法都走 win32_make_borderless，幂等。
    """

    name = "win32"

    def __init__(self):
        self._mutex = 0
        # 托盘状态（轮询式，不用回调消息；原因见「托盘图标」一节顶部）
        self._tray_added = False
        self._tray_hwnd = 0
        self._tray_icon = 0
        self._tray_job = None
        self._tray_left_down = False
        self._tray_right_down = False
        self._tray_press = None            # 已按在图标上的那个键（等到松开才触发）
        self._tray_menu_open = False

    def prepare_process(self):
        win32_set_dpi_aware()

    def strip_decorations(self, root):
        return win32_make_borderless(root)

    def set_skip_taskbar(self, root):
        return win32_make_borderless(root)

    def activate_window(self, root, topmost):
        PlatformBackend.activate_window(self, root, topmost)
        win32_activate(root)

    # ---- 单实例：命名互斥体（Windows 没有 AF_UNIX）----
    def single_instance_start(self, root, on_raise):
        handle, existing = win32_instance_mutex("%s-instance" % APP_ID)
        self._mutex = handle          # 持到进程结束，第二个实例才会看到 183
        return existing

    def single_instance_stop(self, root):
        api = _win_api()
        if self._mutex and api is not None:
            api[1].CloseHandle(self._mutex)
        self._mutex = 0

    def raise_running_instance(self):
        """第二个进程自己去找那个窗口并激活，不需要 IPC 服务端。

        判据以「那个窗口还在不在」为准，互斥体只当旁证：2026-10-11 实测到一次
        第二次启动没认出已有实例、又开出一个窗口（3 次受控复现试验均未再现，根因
        未确定）。只要「窗口在就唤回」，这类症状就不会再出现。
        """
        hwnd = win32_find_window(WINDOW_TITLE)
        if not hwnd:
            return False
        return win32_activate_hwnd(hwnd)

    # ---- 托盘图标 ----
    def tray_install(self, root, on_activate, on_menu):
        """挂托盘图标：左键唤回、右键弹菜单。

        不用 Shell_NotifyIcon 的回调消息（那要子类化 Tk 窗口过程，实测会把进程打崩，
        见「托盘图标」一节顶部注释），改成在 Tk 定时器里查「光标在不在图标上 +
        按键是不是刚松开」。
        """
        if _win_api() is None:
            return False
        hwnd = win32_top_window(root)
        if not hwnd:
            return False
        self._tray_hwnd = hwnd
        self._tray_icon = win32_tray_icon()
        self._tray_added = win32_tray_add(hwnd, self._tray_icon)
        if not self._tray_added:
            return False

        def poll():
            self._tray_job = None
            try:
                self._tray_poll(root, on_activate, on_menu)
            except Exception:
                pass
            try:
                if self._tray_added:
                    self._tray_job = root.after(TRAY_POLL_MS, poll)
            except Exception:
                self._tray_job = None

        self._tray_job = root.after(TRAY_POLL_MS, poll)
        return True

    def _tray_poll(self, root, on_activate, on_menu):
        """一次点击检测。

        判据分两步，不是只看「松开时在不在图标上」：
          ① **按下**（当前按下，或"自上次查询以来按过"）时，若光标在图标矩形内，
             就记下这次按压（`_tray_press`）；
          ② 该键**松开**时触发，不再重新查矩形。
        两条实测教训（2026-10-11）：
          * 按下之后 Shell_NotifyIconGetRect 会返回另一个矩形（实测从
            (1695,1032,1727,1080) 变成 (1631,975,1671,1015)），所以「松开时再查一次
            矩形」的判据永远不成立 —— 真实点击点了没反应，而桩测查不出来（桩把矩形
            固定住了）；
          * 快点击的按下阶段可能整个落在两次轮询之间（只查"当前是否按下"会漏掉），
            所以还要用 GetAsyncKeyState 的最低位兜住；那种情况按下与松开都发生在
            两次轮询之间，直接算一次完成。
        """
        left, right, left_recent, right_recent = win32_mouse_buttons()
        was_left, was_right = self._tray_left_down, self._tray_right_down
        self._tray_left_down, self._tray_right_down = left, right

        pressed_left = (left and not was_left) or left_recent
        pressed_right = (right and not was_right) or right_recent
        if self._tray_press is None and (pressed_left or pressed_right):
            if self._cursor_on_icon():
                self._tray_press = "left" if pressed_left else "right"
                already_up = not left if self._tray_press == "left" else not right
                if already_up:             # 快点击：按下与松开都没被单独看到
                    self._fire_tray(self._tray_press, on_activate, on_menu)
                    return

        if self._tray_press == "left" and was_left and not left:
            self._fire_tray("left", on_activate, on_menu)
        elif self._tray_press == "right" and was_right and not right:
            self._fire_tray("right", on_activate, on_menu)

    def _fire_tray(self, kind, on_activate, on_menu):
        """执行一次托盘动作（左键唤回 / 右键菜单），并清掉按压状态。"""
        self._tray_press = None
        if kind == "left":
            if os.environ.get("DESKTOP_TODO_VERBOSE"):
                print("托盘左键 → 唤回窗口", flush=True)
            on_activate()
            return
        if self._tray_menu_open:
            return
        if os.environ.get("DESKTOP_TODO_VERBOSE"):
            print("托盘右键 → 弹菜单", flush=True)
        self._tray_menu_open = True
        try:
            on_menu()
        finally:
            self._tray_menu_open = False

    def _cursor_on_icon(self):
        """光标是否落在托盘图标矩形内（图标收进溢出区、查不到矩形时一律 False）。"""
        visible, icon_left, icon_top, icon_right, icon_bottom = \
            win32_tray_rect(self._tray_hwnd)
        if not visible:
            return False
        x, y = win32_cursor_pos()
        return icon_left <= x <= icon_right and icon_top <= y <= icon_bottom

    def tray_remove(self, root):
        """摘掉图标并停掉轮询（窗口销毁前调用）。"""
        if self._tray_job is not None:
            try:
                root.after_cancel(self._tray_job)
            except (tk.TclError, ValueError):
                pass
            self._tray_job = None
        if self._tray_added and self._tray_hwnd:
            win32_tray_remove(self._tray_hwnd)
        self._tray_added = False
        self._tray_left_down = False
        self._tray_right_down = False
        self._tray_press = None
        self._tray_menu_open = False

    def data_dir(self):
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, APP_ID)

    # ---- 开机自启：HKCU 的 Run 键（用 pythonw.exe，避免每次开机弹黑框）----
    def autostart_target(self):
        return r"HKCU\%s\%s" % (AUTOSTART_RUN_KEY, APP_ID)

    def autostart_install(self):
        import winreg
        script = os.path.abspath(__file__)
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(pythonw):
            pythonw = sys.executable
        command = '"%s" "%s"' % (pythonw, script)
        try:
            key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, AUTOSTART_RUN_KEY)
            winreg.SetValueEx(key, APP_ID, 0, winreg.REG_SZ, command)
            winreg.CloseKey(key)
        except OSError as error:
            sys.stderr.write("写入开机自启失败: %r\n" % (error,))
            return 1
        print("已写入开机自启: %s（%s）" % (self.autostart_target(), command))
        return 0

    def autostart_uninstall(self):
        import winreg
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_RUN_KEY, 0,
                                 winreg.KEY_SET_VALUE)
        except OSError:
            print("未找到开机自启项: %s" % self.autostart_target())
            return 0
        try:
            winreg.DeleteValue(key, APP_ID)
            print("已移除开机自启: %s" % self.autostart_target())
        except OSError:
            print("未找到开机自启项: %s" % self.autostart_target())
        finally:
            winreg.CloseKey(key)
        return 0


_BACKEND = None


def get_backend():
    """当前平台的后端（进程内单例）。"""
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = WindowsBackend() if os.name == "nt" else X11Backend()
    return _BACKEND


# --------------------------------------------------------------------------- #
# 数据层
# --------------------------------------------------------------------------- #
def data_dir():
    """返回数据目录（Linux 遵循 XDG_DATA_HOME，Windows 用 %APPDATA%）。"""
    return get_backend().data_dir()


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
        self.ui = {"x": None, "y": None, "collapsed": False, "alpha": 0.94,
                   "topmost": False, "hotkey": HOTKEY_DEFAULT,
                   "width": None, "height": None}
        self.archive = []                 # 归档：完成的（过 00:00）/ 清掉的 / 删掉的都进这里
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

        archived = raw.get("archive")
        if isinstance(archived, list):
            for record in archived:
                if not isinstance(record, dict) or not isinstance(record.get("item"), dict):
                    continue
                self.archive.append({"item": record["item"],
                                     "archived_at": record.get("archived_at") or time.time(),
                                     "reason": record.get("reason") or "done"})
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
                # 新格式有 state（todo / waiting / done）；旧格式只有 done（bool）。
                # 两者都留着并保持一致：老版本程序（含 Windows 版）读 done 也不会看错。
                done = bool(entry.get("done"))
                state = str(entry.get("state") or "")
                if state not in (STATE_TODO, STATE_WAITING, STATE_DONE):
                    state = STATE_DONE if done else STATE_TODO
                try:
                    priority = int(entry.get("priority") or 0)
                except (TypeError, ValueError):
                    priority = PRIORITY_NONE
                self.items.append({
                    "id": item_id,
                    "text": text,
                    "steps": self._clean_steps(entry.get("steps")),
                    "tags": self.tags_of(entry),
                    "done_at": self._load_done_at(entry, state),
                    "done": state == STATE_DONE,
                    "state": state,
                    "priority": priority if priority in (PRIORITY_NONE, PRIORITY_MID,
                                                        PRIORITY_HIGH) else PRIORITY_NONE,
                    "today": bool(entry.get("today")),
                    "created": float(entry.get("created") or time.time()),
                })
        if self.items:
            self._next_id = max(item["id"] for item in self.items) + 1
            self.resort()                     # 默认排序：未完成 → 等待中 → 已完成

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
            hotkey = ui.get("hotkey")
            if isinstance(hotkey, str) and parse_hotkey(hotkey) is not None:
                self.ui["hotkey"] = hotkey
            for key in ("width", "height"):
                value = ui.get(key)
                if isinstance(value, (int, float)) and 80 <= float(value) <= 10000:
                    self.ui[key] = int(value)
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
                json.dump({"version": 1, "items": self.items,
                           "archive": self.archive, "ui": self.ui},
                          handle, ensure_ascii=False, indent=2)
            os.replace(temp, self.path)
        except (IOError, OSError):
            pass

    # ---- 条目操作 ----
    def add(self, text):
        """新增一条（插到最前）；空文本返回 None。"""
        text, tags = self.parse_tags(text)
        if not text:
            return None
        item = {"id": self._next_id, "text": text, "steps": [], "tags": tags,
                "done": False,
                "state": STATE_TODO, "priority": PRIORITY_NONE, "today": False,
                "done_at": None, "created": time.time()}
        self._next_id += 1
        self.items.insert(0, item)
        self.save()
        return item

    def _set_state(self, item, state):
        """统一改状态：state 为准，done 跟着同步，然后落盘。"""
        item["state"] = state
        item["done"] = state == STATE_DONE
        if state == STATE_DONE:
            if not item.get("done_at"):
                item["done_at"] = time.time()      # 记下"什么时候完成的"
        else:
            item["done_at"] = None                 # 又变回没完成了
        self.save()
        return item

    def toggle(self, item_id):
        """勾选 / 取消勾选：等待中或未完成 → 已完成；已完成 → 未完成。"""
        for item in self.items:
            if item["id"] == item_id:
                state = item.get("state", STATE_DONE if item["done"] else STATE_TODO)
                return self._set_state(
                    item, STATE_TODO if state == STATE_DONE else STATE_DONE)
        return None

    def toggle_waiting(self, item_id):
        """「等待中」开关：未完成/已完成 → 等待中，等待中 → 未完成。"""
        for item in self.items:
            if item["id"] == item_id:
                state = item.get("state", STATE_DONE if item["done"] else STATE_TODO)
                return self._set_state(
                    item, STATE_TODO if state == STATE_WAITING else STATE_WAITING)
        return None

    @classmethod
    def parse_tags(cls, text):
        """从文字里摘出 `@情境`，返回（去掉标签的正文, 标签列表）。

        规矩（都是为了不误伤普通文字）：`@` 前面必须是空白或开头；后面 1–8 个中英文/数字；
        再后面必须是空白或结尾。所以 `邮箱@qq.com`、`@qq.com` 这种都不会被当标签。
        重复的标签去掉；超过 TAG_MAX 个的留在正文里，不悄悄吃掉。
        """
        tags = []

        def take(match):
            tag = match.group(1)
            if tag in tags:
                return ""
            if len(tags) >= TAG_MAX:
                return match.group(0)
            tags.append(tag)
            return ""

        cleaned = " ".join(TAG_RE.sub(take, " " + str(text) + " ").split())
        return cleaned, tags

    @staticmethod
    def tags_of(item):
        """取标签（兼容老数据）：去重、限个数、限长度。"""
        raw = item.get("tags")
        tags = []
        if isinstance(raw, list):
            for entry in raw:
                tag = " ".join(str(entry).split())[:TAG_LEN]
                if tag and tag not in tags:
                    tags.append(tag)
                if len(tags) >= TAG_MAX:
                    break
        return tags

    def all_tags(self):
        """出现过的全部标签（按出现次数多的在前）。"""
        counts = {}
        for item in self.items:
            for tag in self.tags_of(item):
                counts[tag] = counts.get(tag, 0) + 1
        return [tag for tag, _count in sorted(counts.items(),
                                             key=lambda pair: (-pair[1], pair[0]))]

    @staticmethod
    def _clean_steps(raw):
        """把任意输入收拾成 [{text, done}]：去空行、限条数、text 限长。"""
        steps = []
        if not isinstance(raw, list):
            return steps
        for entry in raw:
            if isinstance(entry, dict):
                text = " ".join(str(entry.get("text", "")).split())
                done = bool(entry.get("done"))
            elif isinstance(entry, str):
                text = " ".join(entry.split())
                done = False
            else:
                continue                      # None / 数字之类一律丢掉，别变成 "None"
            if not text:
                continue
            steps.append({"text": text[:200], "done": done})
            if len(steps) >= STEP_MAX:
                break
        return steps

    def steps_of(self, item):
        """取子步骤（兼容老数据/坏数据）：总是返回一份新的干净列表。"""
        return self._clean_steps(item.get("steps"))

    def steps_progress(self, item):
        """(已完成数, 总数)。"""
        steps = self.steps_of(item)
        return len([step for step in steps if step["done"]]), len(steps)

    def set_steps(self, item_id, steps):
        """整体替换某条的子步骤（编辑器保存时用）。"""
        cleaned = self._clean_steps(steps)
        for item in self.items:
            if item["id"] == item_id:
                item["steps"] = cleaned
                self.save()
                return item
        return None

    def add_step(self, item_id, text="新步骤"):
        """在末尾追加一条子步骤（右键菜单用）；受 STEP_MAX 约束。"""
        for item in self.items:
            if item["id"] == item_id:
                steps = self.steps_of(item)
                if len(steps) >= STEP_MAX:
                    return None
                steps.append({"text": " ".join(str(text).split()) or "新步骤", "done": False})
                item["steps"] = steps
                self.save()
                return item
        return None

    def toggle_step(self, item_id, index):
        """勾选第 index 个子步骤（不动主条目的状态）。"""
        for item in self.items:
            if item["id"] == item_id:
                steps = self.steps_of(item)
                if not isinstance(index, int) or not 0 <= index < len(steps):
                    return None
                steps[index]["done"] = not steps[index]["done"]
                item["steps"] = steps
                self.save()
                return item
        return None

    def priority_of(self, item):
        """取优先级（兼容没有这个字段的老数据）。"""
        value = item.get("priority")
        return value if value in (PRIORITY_NONE, PRIORITY_MID, PRIORITY_HIGH) \
            else PRIORITY_NONE

    def set_priority(self, item_id, value):
        """直接设优先级（右键菜单用；cycle_priority 保留给自检与将来用）。"""
        if value not in (PRIORITY_NONE, PRIORITY_MID, PRIORITY_HIGH):
            return None
        for item in self.items:
            if item["id"] == item_id:
                item["priority"] = value
                self.save()
                return item
        return None

    def cycle_priority(self, item_id):
        """点旗子：无 → 高 → 中 → 无。"""
        for item in self.items:
            if item["id"] == item_id:
                current = self.priority_of(item)
                try:
                    index = PRIORITY_CYCLE.index(current)
                except ValueError:
                    index = 0
                item["priority"] = PRIORITY_CYCLE[(index + 1) % len(PRIORITY_CYCLE)]
                self.save()
                return item
        return None

    def toggle_today(self, item_id):
        """标记 / 取消「今天要办的」。"""
        for item in self.items:
            if item["id"] == item_id:
                item["today"] = not bool(item.get("today"))
                self.save()
                return item
        return None

    def today_stats(self):
        """(今天已做完数, 今天标记总数)。"""
        marked = [item for item in self.items if item.get("today")]
        done = len([item for item in marked if self.state_of(item) == STATE_DONE])
        return done, len(marked)

    def state_of(self, item):
        """取条目状态（兼容老数据：只认 done 的条目）。"""
        state = item.get("state")
        if state in (STATE_TODO, STATE_WAITING, STATE_DONE):
            return state
        return STATE_DONE if item.get("done") else STATE_TODO

    def resort(self, save=False):
        """按状态分组排序（未完成 → 等待中 → 已完成），返回顺序是否真的变了。

        用稳定排序：同一组里的相对顺序原样保留，所以「拖拽排出来的优先级」不会被它打乱，
        它只负责把三段归位。列表顺序变了才会写盘。
        """
        rank = dict((state, index) for index, state in enumerate(STATE_ORDER))

        def sort_key(pair):
            index, item = pair
            state = self.state_of(item)
            state_rank = rank.get(state, len(STATE_ORDER))
            # 段内：先「今天要办的」（已完成的不参与置顶），再优先级，最后是原来的手动顺序
            today_rank = 0 if (item.get("today") and state != STATE_DONE) else 1
            priority_rank = -self.priority_of(item)
            return (state_rank, today_rank, priority_rank, index)

        before = [item["id"] for item in self.items]
        order = sorted(enumerate(self.items), key=sort_key)
        self.items = [item for _, item in order]
        changed = [item["id"] for item in self.items] != before
        if changed and save:
            self.save()
        return changed

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

    def rename(self, item_id, text):
        """改条目文字（压掉首尾与多余空白）；空文本不生效。返回是否真的改了。"""
        cleaned, tags = self.parse_tags(text)
        if not cleaned:
            return False
        for item in self.items:
            if item["id"] == item_id:
                if item["text"] == cleaned and self.tags_of(item) == tags:
                    return False
                item["text"] = cleaned
                item["tags"] = tags
                self.save()
                return True
        return False

    def pop(self, item_id):
        """移除条目，并把 (条目, 原位置) 交出来供撤销用；没找到返回 None。"""
        for index, item in enumerate(self.items):
            if item["id"] == item_id:
                self.items.pop(index)
                self._archive_item(item, "deleted")   # 先归档，撤销=从归档里拿回来
                self.save()
                return {"item": item, "index": index}
        return None

    def restore(self, record):
        """把 pop() 出来的条目放回原位置（位置越界就夹到合法范围）。"""
        if not record or not record.get("item"):
            return False
        item = record["item"]
        for existing in self.items:
            if existing["id"] == item["id"]:
                return False              # 已经在列表里，别插重复
        index = max(0, min(int(record.get("index", 0)), len(self.items)))
        self.items.insert(index, item)
        self._unarchive(item["id"])
        self.save()
        return True

    @staticmethod
    def day_start(now=None):
        """今天 00:00 的时间戳（本地时区）。"""
        now = time.time() if now is None else now
        parts = time.localtime(now)[:3]
        # mktime 要 9 元组：(年,月,日,时,分,秒,周几,一年第几天,是否夏令时)
        return time.mktime(tuple(parts) + (0, 0, 0, 0, 0, -1))

    @staticmethod
    def _load_done_at(entry, state):
        """完成时间：老数据没这个字段就按"现在"算 —— 免得一升级就被自动归档掉。"""
        if state != STATE_DONE:
            return None
        try:
            value = float(entry.get("done_at") or 0)
        except (TypeError, ValueError):
            value = 0.0
        return value if value > 0 else time.time()

    def _archive_item(self, item, reason, now=None):
        """把一条从活动列表挪进归档（不丢数据）。"""
        self.archive.append({"item": item,
                             "archived_at": time.time() if now is None else now,
                             "reason": reason})
        self._trim_archive()

    def _trim_archive(self):
        """归档只留最近 ARCHIVE_MAX 条（按归档时间）。"""
        if len(self.archive) > ARCHIVE_MAX:
            self.archive.sort(key=lambda record: record.get("archived_at") or 0)
            self.archive = self.archive[-ARCHIVE_MAX:]

    def _unarchive(self, item_id):
        """把某条从归档里摘掉（撤销删除 / 恢复时用）。"""
        before = len(self.archive)
        self.archive = [record for record in self.archive
                        if record.get("item", {}).get("id") != item_id]
        return len(self.archive) != before

    def archive_completed(self, now=None):
        """把「今天 00:00 之前完成」的条目挪进归档；返回挪了几条。

        今天完成的还留着（当天能看到），过了 00:00 再看就不显示了。
        """
        now = time.time() if now is None else now
        boundary = self.day_start(now)
        keep, moved = [], []
        for item in self.items:
            done_at = item.get("done_at")
            if item.get("state") == STATE_DONE and done_at and done_at < boundary:
                moved.append(item)
            else:
                keep.append(item)
        if not moved:
            return 0
        for item in moved:
            self._archive_item(item, "done", now)
        self.items = keep
        self.save()
        return len(moved)

    def archive_stats(self):
        """归档条数（按原因分开数）。"""
        counts = {"done": 0, "cleared": 0, "deleted": 0}
        for record in self.archive:
            counts[record.get("reason") or "done"] = \
                counts.get(record.get("reason") or "done", 0) + 1
        return len(self.archive), counts

    def clear_done(self):
        before = len(self.items)
        moved = [item for item in self.items if item["done"]]
        self.items = [item for item in self.items if not item["done"]]
        for item in moved:
            self._archive_item(item, "cleared")      # 归档，不再真删
        if len(self.items) != before:
            self.save()
            return before - len(self.items)
        return 0

    def remaining(self, items=None):
        """还没完成的条数（等待中算没完成 —— 事情确实还没了）。items 给子集时就只数子集。"""
        items = self.items if items is None else items
        return len([item for item in items if self.state_of(item) != STATE_DONE])

    def waiting_count(self, items=None):
        items = self.items if items is None else items
        return len([item for item in items if self.state_of(item) == STATE_WAITING])

    def has_done(self):
        return any(item["done"] for item in self.items)


# --------------------------------------------------------------------------- #
# 界面层
# --------------------------------------------------------------------------- #
class TodoWidget(object):
    """桌面待办面板（无边框、默认不置顶；条目可拖拽排序）。"""

    REGROUP_DELAY_MS = 1200       # 状态变了多久之后把它归位（留出双击改名的窗口）

    def _apply_metrics(self, width=None):
        """按给定宽度（缺省用设计宽度）重算所有跟尺寸有关的量。

        正常启动算出来的数与老实现逐像素一致（这些值都能被自检卡住）；
        窗口被拉大之后才跟着变宽 —— 换行宽度、右侧图标位置都会跟着变。
        """
        scale = self.s
        panel_w = _scaled(PANEL_W, scale)
        if width:
            panel_w = max(panel_w, int(width))
        self.panel_w = panel_w
        self.pad = _scaled(PAD, scale)
        self.list_w = max(1, self.panel_w - 2 * self.pad - 2)
        self.list_max_h = _scaled(LIST_MAX_H, scale)
        self.text_x = _scaled(TEXT_X, scale)
        self.del_w = _scaled(DEL_W, scale)
        self.wait_w = _scaled(WAIT_W, scale)
        self.today_w = _scaled(TODAY_W, scale)
        self.prio_w = _scaled(PRIO_W, scale)
        self.steps_w = _scaled(STEPS_W, scale)
        self.step_box = _scaled(STEP_BOX, scale)
        self.step_indent = _scaled(STEP_INDENT, scale)
        # 右侧按钮：从右往左排（区名, 宽度）。绘制、点击判定、折行宽度共用这一张表
        # 右侧只留两个：子步骤（≡ / 2-3）与删除。等待中/今天/优先级搬进右键菜单
        self.markers = (("delete", self.del_w), ("steps", self.steps_w))
        self.marker_w = sum(width for _, width in self.markers)
        self.row_pad_y = _scaled(ROW_PAD_Y, scale)
        self.row_wrap = max(_scaled(40, scale),
                            self.list_w - self.text_x - self.marker_w - _scaled(4, scale))
        self.header_h = _scaled(34, scale)
        self.icon_box = _scaled(20, scale)
        self.check_box = _scaled(16, scale)

    def _list_space(self, height=None):
        """当前窗口高度里最多能给列表多少像素（多出来的高度就多显示几条）。"""
        try:
            if height is None:
                height = self.root.winfo_height()
            # 卡片高度减去列表高度 = 标题 + 输入框 + 底部 + 间距
            chrome = self.card.winfo_reqheight() - self.list_area.winfo_reqheight()
        except (tk.TclError, AttributeError):
            return _scaled(LIST_MAX_H, self.s)
        return max(0, int(height) - int(chrome) - self.px(4))

    def _on_root_configure(self, event):
        """窗口被拉大 / 拉高（WM 边缘拖拽）：防抖之后按新尺寸重排。"""
        if event.widget is not self.root:
            return
        if self._size_job is not None:
            try:
                self.root.after_cancel(self._size_job)
            except (tk.TclError, ValueError):
                pass
        try:
            self._size_job = self.root.after(160, self._apply_resize)
        except tk.TclError:
            self._size_job = None

    def _apply_resize(self):
        """按窗口现在的尺寸重排列宽与列表高度；没变化就不重画（避免来回震荡）。"""
        self._size_job = None
        try:
            width, height = self.root.winfo_width(), self.root.winfo_height()
        except tk.TclError:
            return
        if width <= 1 or height <= 1:
            return
        before = (self.panel_w, self.list_max_h)
        self._apply_metrics(width)
        space = self._list_space(height)
        if space > self.list_max_h:
            self.list_max_h = space
        if (self.panel_w, self.list_max_h) == before:
            return
        self._user_sized = True
        self.store.ui["width"] = self.panel_w
        self.store.ui["height"] = height
        self.store.save()
        self._refresh()

    def _apply_saved_size(self):
        """启动时把上次的窗口尺寸装回来（宽度影响排版，高度决定列表能显示几条）。"""
        width = self.store.ui.get("width")
        height = self.store.ui.get("height")
        if not width and not height:
            return
        self._apply_metrics(width)
        if height:
            self.list_max_h = max(self.list_max_h, self._list_space(height))
        self._user_sized = True
        try:                              # 把上次的尺寸真正装回窗口（宽 + 高）
            self.root.geometry("%dx%d" % (self.panel_w, int(height or self.root.winfo_height())))
        except tk.TclError:
            pass

    def __init__(self, root, store, visible=True):
        self.root = root
        self.store = store
        self.backend = get_backend()
        # 高 DPI：字号由 Tk 按「点」自动放大，窗口尺寸得按同一比例自己放大
        self.s = ui_scale(root)
        # 尺寸相关的量都走 _apply_metrics()：窗口被拉大/拉高之后要能跟着重排
        self.panel_w = self.pad = self.list_w = self.list_max_h = 0
        self.text_x = self.del_w = self.wait_w = self.row_pad_y = self.row_wrap = 0
        self.header_h = self.icon_box = self.check_box = 0
        self._size_job = None             # 尺寸变化的重排任务（防抖）
        self._user_sized = False          # 用户手动改过大小：之后不再把它缩回去
        self._apply_metrics(store.ui.get("width"))
        self.fonts = self._make_fonts()
        self._drag_offset = None          # 拖窗口的偏移
        self._placeholder_on = False
        # 条目拖拽排序状态
        self.row_widgets = []             # [(item_id, row_canvas)]，按显示顺序
        self._press = None                # 按下时记录 {id, y_root, zone}
        self._drag_id = None              # 正在被拖动的条目 id
        self._pointer_y = None            # 拖动中指针的屏幕 y（用于贴边自动滚动）
        self._autoscroll_job = None
        # 撤销与编辑状态
        self._undo = None                 # 刚删掉的 {item, index}，供撤销放回原位
        self._undo_job = None             # 撤销时限的 after 句柄
        self._edit_id = None              # 正在编辑的条目 id
        self._edit_entry = None           # 正在编辑的输入框（盖在那一行上）
        self._steps_edit = None           # 正在编辑子步骤的 (多行框, 条目 id)
        self._last_toggle = (None, 0.0)   # 最近一次勾选（条目 id, 时间），供双击回退
        self._regroup_job = None          # 归位（按状态分组排序）的待执行任务
        self._build()
        self._restore_position()
        self._apply_collapsed()
        self._refresh(keep_scroll=False)
        if visible:
            self.root.deiconify()
            self.root.update_idletasks()
            self._restore_position()      # 映射后再落一次，避免 WM 摆放时挪位
            self.backend.single_instance_start(self.root, self.raise_window)
            self.backend.set_skip_taskbar(self.root)   # 映射后再补一次（KWin 只对已托管的窗口处理）
            # 挂托盘图标：左键唤回、右键弹菜单（Windows 实现；其它平台是空操作）
            self._tray_on = self.backend.tray_install(
                self.root, self.raise_window, self._popup_menu_at_cursor)
            # 全局快捷键：不占任务栏、又没有托盘时，这是最可靠的唤回入口
            self._hotkey_spec = self.store.ui.get("hotkey") or HOTKEY_DEFAULT
            self._hotkey_on = self.backend.hotkey_install(
                self.root, self._hotkey_spec, self.raise_window)

    # ---- 单实例 / 唤回 ----
    def raise_window(self):
        """把窗口提到最前面并拿到焦点（不改变用户设置的「始终置顶」状态）。

        单实例监听本身在平台后端里：X11 是 unix 套接字 + Tk 文件事件，
        Windows 是命名互斥体；唤回动作由后端按平台实现。
        """
        self.backend.activate_window(self.root, self.store.ui["topmost"])

    def _popup_menu_at_cursor(self):
        """托盘右键：在鼠标位置弹出与本程序一致的菜单。"""
        x, y = win32_cursor_pos()
        try:
            self.menu.tk_popup(x, y)
        finally:
            try:
                self.menu.grab_release()
            except tk.TclError:
                pass

    def _diag_log(self, text):
        """把"唤回/隐藏"的判定写一行日志（只记录，不改行为）。

        用途：Super+D 之后 Super+T 唤不回来这类"只在 WM 特定状态下出现"的问题，
        靠猜没用 —— 让用户复现一次，看这行日志里的 viewable / wm_state 到底是什么。
        日志和数据文件同目录（~/.local/share/desktop-todo/hotkey.log）。
        """
        try:
            path = os.path.join(os.path.dirname(self.store.path), "hotkey.log")
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("%s\t%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
        except (IOError, OSError):
            pass

    def toggle_hidden(self):
        """隐藏 / 显示窗口：隐藏之后从托盘图标左键唤回（免得和「退出」混在一起）。"""
        try:
            viewable = bool(self.root.winfo_viewable())
            state = ""
            try:
                state = self.root.state()
            except tk.TclError:
                pass
            self._diag_log("hotkey viewable=%s wm_state=%s -> %s"
                           % (viewable, state, "withdraw" if viewable else "activate"))
            if viewable:
                self.root.withdraw()
            else:
                self.raise_window()
        except tk.TclError as error:
            self._diag_log("hotkey 失败: %r" % (error,))

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
            "marker": tkfont.Font(family=actual, size=8),
            "small_done": tkfont.Font(family=actual, size=9, overstrike=1),
        }

    # ---- 组装 ----
    def _build(self):
        root = self.root
        root.title("桌面待办清单")
        # 窗口交给系统托管（这样点击输入框才能拿到键盘焦点），去标题栏与
        # 「不进任务栏」都交给平台后端：X11 用 Motif 提示 + EWMH 的 SKIP_TASKBAR，
        # Windows 用清掉 WS_CAPTION/WS_THICKFRAME + WS_EX_TOOLWINDOW。
        # 被别的窗口盖住后，Alt+Tab 或「再运行一次本程序」都能把它叫回最前面。
        root.withdraw()                          # 先不映射，提示才能在首次建图前生效
        root.attributes("-topmost", bool(self.store.ui["topmost"]))
        root.attributes("-alpha", float(self.store.ui["alpha"]))
        root.update_idletasks()                  # 促使 Tk 先建出 toplevel 的窗口
        self.backend.strip_decorations(root)
        self.backend.set_skip_taskbar(root)      # 任务栏 / dock 里不出现这个图标
        root.configure(bg=C_BORDER)              # 卡片外层留出 1px 边框色
        root.protocol("WM_DELETE_WINDOW", self.quit)

        self.card = tk.Frame(root, bg=C_CARD, bd=0, highlightthickness=0)
        self.card.pack(fill="both", expand=True, padx=1, pady=1)

        self._build_header()
        self.filter_tag = None            # 顶部情境筛选：None = 看全部
        self._build_input()
        self._build_tags()
        self._build_list()
        self._build_footer()
        self.menu = self._build_menu()
        self._row_menu = None                  # 当前弹出的条目右键菜单
        # 只挂左键：右键那一下正是"要弹菜单"的动作，若也挂上全局收起，
        # 就是"先弹出来、紧接着被同一事件的全局处理收掉"——实测菜单根本弹不出来。
        # （右键点别的条目由 _popup_row_menu 自己先收起上一个。）
        self.root.bind_all("<Button-1>", self._dismiss_row_menu, add="+")
        self._archived_day = None
        self._archive_on_start()

    def _build_header(self):
        self.header = head = tk.Frame(self.card, bg=C_CARD, height=self.header_h)
        head.pack(fill="x", side="top")
        head.pack_propagate(False)

        dot_size = self.px(10)
        dot = tk.Canvas(head, width=dot_size, height=dot_size, bg=C_CARD,
                        highlightthickness=0)
        dot.create_oval(self.px(2), self.px(2), self.px(8), self.px(8),
                        fill=C_ACCENT, outline="")
        dot.pack(side="left", padx=(self.pad, self.px(6)))

        self.title = tk.Label(head, text="待办事项", bg=C_CARD, fg=C_TEXT, font=self.fonts["bold"])
        self.title.pack(side="left")

        self.count = tk.Label(head, text="", bg=C_CARD, fg=C_DIM, font=self.fonts["small"])
        self.count.pack(side="left", padx=(self.px(8), 0))

        close_btn = self._icon_button(head, "close", self.quit)
        close_btn.pack(side="right", padx=(0, max(0, self.pad - self.px(6))))
        self.collapse_btn = self._icon_button(head, "collapse", self.toggle_collapsed)
        self.collapse_btn.pack(side="right")

        for widget in (head, dot, self.title, self.count):
            widget.bind("<Button-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<ButtonRelease-1>", self._drag_end)
            widget.bind("<Button-3>", self._popup_menu)
        head.bind("<Double-Button-1>", lambda event: self.toggle_collapsed())

    def _build_tags(self):
        """顶部情境筛选条：只在有标签时才长出内容（没标签时是个空框，高度≈0）。"""
        self.tags_row = tk.Frame(self.card, bg=C_CARD)
        self.tags_row.pack(fill="x", padx=self.pad, pady=(0, self.px(2)))

    def _refresh_tags(self):
        for child in list(self.tags_row.winfo_children()):
            child.destroy()
        tags = self.store.all_tags()
        if not tags:
            self.filter_tag = None
            return
        self._tag_chip("全部", None)
        for tag in tags:
            self._tag_chip("@" + tag, tag)

    def _tag_chip(self, label, tag):
        active = (self.filter_tag == tag)
        chip = tk.Label(self.tags_row, text=label, bg=C_LIST,
                        fg=C_ACCENT if active else C_DIM,
                        font=self.fonts["small"], padx=self.px(6), pady=self.px(1))
        chip.pack(side="left", padx=(0, self.px(4)))
        if active:
            chip.configure(highlightbackground=C_ACCENT, highlightthickness=1)
        chip.bind("<Button-1>", lambda event, t=tag: self.set_filter(t))
        chip.bind("<Enter>", lambda event: chip.configure(fg=C_ACCENT))
        chip.bind("<Leave>", lambda event: chip.configure(
            fg=C_ACCENT if active else C_DIM))
        return chip

    def set_filter(self, tag):
        """切换「只看某个情境」；tag=None 表示看全部。"""
        self.filter_tag = tag
        self._refresh_tags()
        self._refresh(keep_scroll=False)
        self._update_counts()
        self._update_footer()

    def _visible_items(self):
        if not self.filter_tag:
            return list(self.store.items)
        return [item for item in self.store.items
                if self.filter_tag in self.store.tags_of(item)]

    def _build_input(self):
        self.input_row = row = tk.Frame(self.card, bg=C_CARD)

        box = tk.Frame(row, bg=C_LIST, highlightbackground=C_BORDER, highlightthickness=1)
        box.pack(side="left", fill="x", expand=True)

        self.entry = tk.Entry(box, bg=C_LIST, fg=C_TEXT, insertbackground=C_TEXT,
                              relief="flat", bd=0, font=self.fonts["base"], highlightthickness=0)
        self.entry.pack(side="left", fill="x", expand=True, padx=self.px(8), pady=self.px(5))
        self.entry.bind("<Return>", lambda event: self.add_from_entry())
        self.entry.bind("<Escape>", lambda event: self.entry.delete(0, "end"))
        self.entry.bind("<FocusIn>", lambda event: self._clear_placeholder())
        self.entry.bind("<FocusOut>", lambda event: self._set_placeholder())
        self.entry.bind("<Button-1>", self._focus_entry)
        self._set_placeholder()

        add_btn = tk.Label(row, text="+", bg=C_ACCENT, fg="#ffffff", font=self.fonts["bold"],
                           width=2, cursor="hand2")
        add_btn.pack(side="left", padx=(self.px(6), 0), ipady=self.px(5))
        add_btn.bind("<Button-1>", lambda event: self.add_from_entry())
        add_btn.bind("<Enter>", lambda event: add_btn.configure(bg="#5aa6ff"))
        add_btn.bind("<Leave>", lambda event: add_btn.configure(bg=C_ACCENT))

    def _build_list(self):
        self.list_area = tk.Frame(self.card, bg=C_LIST)
        self.canvas = tk.Canvas(self.list_area, bg=C_LIST, highlightthickness=0, bd=0,
                                width=self.list_w)
        self.canvas.pack(fill="x", expand=True)
        self.inner = tk.Frame(self.canvas, bg=C_LIST)
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.root.bind("<Configure>", self._on_root_configure)
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self._bind_wheel(self.canvas)
        self._bind_wheel(self.inner)

    def _build_footer(self):
        self.footer = foot = tk.Frame(self.card, bg=C_CARD)
        self.hint = tk.Label(foot, text="双击标题可折叠", bg=C_CARD, fg=C_MUTED, font=self.fonts["small"])
        self.hint.pack(side="left")
        # 「撤销」只在刚删掉一条时出现（见 _show_undo / _clear_undo），平时不占位置
        self.undo_btn = tk.Label(foot, text="撤销删除", bg=C_CARD, fg=C_ACCENT,
                                 font=self.fonts["small"], cursor="hand2")
        self.undo_btn.bind("<Button-1>", lambda event: self.undo_delete())
        self.undo_btn.bind("<Enter>", lambda event: self.undo_btn.configure(fg=C_TEXT))
        self.undo_btn.bind("<Leave>", lambda event: self.undo_btn.configure(fg=C_ACCENT))
        self.clear_btn = tk.Label(foot, text="归档已完成", bg=C_CARD, fg=C_DIM,
                                  font=self.fonts["small"], cursor="hand2")
        self.clear_btn.bind("<Button-1>", lambda event: self.clear_done())
        self.clear_btn.bind("<Enter>", lambda event: self.clear_btn.configure(fg=C_TEXT))
        self.clear_btn.bind("<Leave>", lambda event: self.clear_btn.configure(fg=C_DIM))

    def _build_menu(self):
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="折叠 / 展开", command=self.toggle_collapsed)
        menu.add_command(label="隐藏窗口（托盘左键唤回）", command=self.toggle_hidden)
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
    def px(self, value):
        """基准像素值 → 当前缩放下的像素值（缩放 1.0 时原样返回）。"""
        return _scaled(value, self.s)

    def _icon_button(self, parent, kind, command):
        canvas = tk.Canvas(parent, width=self.icon_box, height=self.icon_box, bg=C_CARD,
                           highlightthickness=0, cursor="hand2")
        canvas._kind = kind
        self._draw_icon(canvas, kind, C_DIM)
        canvas.bind("<Button-1>", lambda event: command())
        canvas.bind("<Enter>", lambda event: self._draw_icon(canvas, kind, C_TEXT))
        canvas.bind("<Leave>", lambda event: self._draw_icon(canvas, kind, C_DIM))
        return canvas

    def _draw_icon(self, canvas, kind, color):
        canvas.delete("all")
        line_w = 1.6 * self.s
        if kind == "close":
            canvas.create_line(self.px(6), self.px(6), self.px(14), self.px(14),
                               fill=color, width=line_w, capstyle="round")
            canvas.create_line(self.px(14), self.px(6), self.px(6), self.px(14),
                               fill=color, width=line_w, capstyle="round")
        else:  # 折叠箭头：展开时朝下，折叠时朝右
            if self.store.ui["collapsed"]:
                canvas.create_line(self.px(8), self.px(5), self.px(13), self.px(10),
                                   self.px(8), self.px(15), fill=color, width=line_w,
                                   capstyle="round", joinstyle="round")
            else:
                canvas.create_line(self.px(5), self.px(8), self.px(10), self.px(13),
                                   self.px(15), self.px(8), fill=color, width=line_w,
                                   capstyle="round", joinstyle="round")

    def _draw_check(self, canvas, done, top=0):
        """在 canvas 上画勾选框，top 为垂直偏移（尺寸按界面缩放）。"""
        canvas.delete("check")
        y = top
        box = self.check_box
        if done:
            canvas.create_rectangle(self.px(6), y, self.px(22), y + box, fill=C_ACCENT,
                                    outline="", tags="check")
            canvas.create_line(self.px(10), y + self.px(8), self.px(13), y + self.px(11.5),
                               self.px(18.5), y + self.px(4.5), fill="#ffffff",
                               width=2 * self.s, capstyle="round", joinstyle="round",
                               tags="check")
        else:
            canvas.create_rectangle(self.px(6.5), y + 0.5, self.px(21.5), y + box - 0.5,
                                    outline=C_BORDER, width=1.4 * self.s, tags="check")

    def _bind_wheel(self, widget):
        widget.bind("<Button-4>", self._on_wheel)
        widget.bind("<Button-5>", self._on_wheel)
        widget.bind("<MouseWheel>", self._on_wheel)

    def _on_wheel(self, event):
        if self.inner.winfo_reqheight() <= self.list_max_h:
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
        # 行会被重建，正在编辑的输入框随之销毁，别留悬空引用
        self._edit_id = None
        self._edit_entry = None
        self._steps_edit = None
        self.row_widgets = []
        self._press = None
        self._drag_id = None
        self._pointer_y = None
        self._stop_autoscroll()

        visible = self._visible_items()
        if not visible:
            empty = tk.Label(self.inner,
                             text=("没有 @%s 的条目" % self.filter_tag) if self.filter_tag
                             else "暂无待办，在上面输入框添加第一条",
                             bg=C_LIST, fg=C_MUTED, font=self.fonts["small"])
            empty.pack(fill="x", pady=self.px(20))
            self._bind_wheel(empty)
        else:
            for item in visible:
                self._make_row(item)

        self._update_counts()
        self._update_footer()
        self.root.update_idletasks()
        self._resize()
        if self.inner.winfo_reqheight() > self.list_max_h:
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

    def _draw_row(self, canvas, item):
        """把一条待办画进行画布（新建与原地重画共用）。

        视觉上的几个维度：
          状态：未完成＝白字空方框；等待中＝琥珀字 + 琥珀小钟；已完成＝灰字删除线 + 蓝勾；
          优先级：右侧旗子（实心橙＝高、空心橙＝中、淡灰＝无）；
          今天：右侧「今」（蓝色＝今天要办的）；
          删除：最右边那个 ×。

        返回 (删除按钮的图元 id, 行高)；行高变化时会同步 canvas 的 height。
        """
        canvas.delete("all")
        state = self.store.state_of(item)
        done = state == STATE_DONE
        canvas._waiting = state == STATE_WAITING
        canvas._prio = self.store.priority_of(item)
        canvas._today = bool(item.get("today"))
        font = self.fonts["done"] if done else self.fonts["base"]
        line_h = font.metrics("linespace")
        steps = self.store.steps_of(item)
        done_steps, total_steps = self.store.steps_progress(item)
        tags = self.store.tags_of(item)
        tag_text = " ".join("@" + tag for tag in tags)
        tag_font = self.fonts["marker"]
        tag_w = (tag_font.measure(tag_text) + self.px(6)) if tag_text else 0
        lines = self._wrap_text(item["text"], font,
                               max(self.px(40), self.row_wrap - tag_w))
        step_h = self.fonts["small"].metrics("linespace") + self.px(2)
        step_wrap = max(self.px(40), self.row_wrap - self.step_indent)
        step_rows = 0
        for step in steps:
            step_font = self.fonts["small_done"] if step["done"] else self.fonts["small"]
            step_rows += len(self._wrap_text(step["text"], step_font, step_wrap))
        main_h = line_h * len(lines)
        row_h = main_h + step_h * step_rows + 2 * self.row_pad_y
        if int(canvas.cget("height")) != row_h:
            canvas.configure(height=row_h)

        # 优先级：行最左边一条竖条（高＝整条、中＝中间半条）。不占正文宽度
        if canvas._prio in (PRIORITY_HIGH, PRIORITY_MID):
            bar_w = max(2.0, self.px(3))
            if canvas._prio == PRIORITY_HIGH:
                bar_top, bar_bottom = 0.0, float(row_h)
            else:
                bar_top, bar_bottom = row_h * 0.25, row_h * 0.75
            canvas.create_rectangle(0, bar_top, bar_w, bar_bottom, fill=C_HIGH,
                                    outline="", tags="prio")
        # 今天：正文前面的小蓝点（在勾选框与文字之间的空档里，也不占宽度）
        if canvas._today:
            dot = max(2.0, self.px(3))
            dot_x, dot_y = self.text_x - self.px(8), self.row_pad_y + line_h / 2.0
            canvas.create_oval(dot_x - dot, dot_y - dot, dot_x + dot, dot_y + dot,
                               fill=C_ACCENT, outline="", tags="today")

        # 主勾选框对齐「主文字块」而不是整行：有子步骤时不会跑到中间去
        self._draw_check(canvas, done,
                         top=self.row_pad_y + (main_h - self.check_box) // 2)
        text_color = C_DONE if done else (C_WAIT if canvas._waiting else C_TEXT)
        for index, line in enumerate(lines):
            canvas.create_text(self.text_x, self.row_pad_y + index * line_h, text=line,
                               anchor="nw", fill=text_color, font=font, tags="text")
        canvas._tag_item = None
        if tag_text and lines:
            canvas._tag_item = canvas.create_text(
                self.text_x + font.measure(lines[-1]) + self.px(6),
                self.row_pad_y + (len(lines) - 1) * line_h + line_h / 2.0,
                text=tag_text, anchor="w", fill=C_ACCENT, font=tag_font, tags="tag")
        canvas._step_boxes = self._draw_steps(canvas, steps, self.row_pad_y + main_h, step_h)
        canvas._steps_done, canvas._steps_total = done_steps, total_steps
        canvas._markers = {
            "delete": self._draw_delete_marker(canvas, row_h),
            "steps": self._draw_steps_marker(canvas, row_h, done_steps, total_steps),
        }
        canvas._delete_item = canvas._markers["delete"][0][0]   # 兼容旧引用
        self._paint_markers(canvas, None)
        return canvas._delete_item, row_h

    def _draw_steps(self, canvas, steps, top, step_h):
        """在主文字下面逐行画子步骤，返回每行小方框的命中区。

        返回 [(index, y0, y1, x0, x1)]（canvas 坐标）。点小方框 = 勾这个步骤；
        点这一行的其它地方什么也不做 —— 免得误把整条待办标成完成。
        """
        boxes = []
        step_font = self.fonts["small"]
        step_wrap = max(self.px(40), self.row_wrap - self.step_indent)
        step_h = step_h * 1.0
        y = top
        for index, step in enumerate(steps):
            font = self.fonts["small_done"] if step["done"] else step_font
            text_lines = self._wrap_text(step["text"], font, step_wrap)
            block_h = step_h * len(text_lines)
            y0 = y
            cy = y0 + step_h / 2.0
            box = self.step_box
            left = self.text_x
            bx0, by0 = left, cy - box / 2.0
            bx1, by1 = left + box, cy + box / 2.0
            if step["done"]:
                canvas.create_rectangle(bx0, by0, bx1, by1, fill=C_ACCENT, outline="",
                                        tags="step")
                canvas.create_line(bx0 + box * 0.22, by0 + box * 0.52,
                                   bx0 + box * 0.44, by0 + box * 0.74,
                                   bx0 + box * 0.80, by0 + box * 0.26,
                                   fill="#ffffff", width=max(1.0, 1.6 * self.s),
                                   capstyle="round", joinstyle="round", tags="step")
            else:
                canvas.create_rectangle(bx0, by0, bx1, by1, outline=C_BORDER,
                                        width=max(1.0, 1.2 * self.s), tags="step")
            for line_index, line in enumerate(text_lines):
                canvas.create_text(self.text_x + self.step_indent,
                                   y0 + line_index * step_h,
                                   text=line, anchor="nw",
                                   fill=C_DONE if step["done"] else C_DIM,
                                   font=font, tags="step")
            boxes.append((index, y0, y0 + block_h,
                          bx0 - self.px(3), bx1 + self.px(4)))
            y += block_h
        canvas._step_top = top
        return boxes

    # ---- 右侧按钮：位置、绘制、上色 ----
    def _marker_center(self, zone, row_h):
        """某个右侧按钮的中心坐标（按钮从右往左排）。"""
        right = self.list_w
        for name, width in self.markers:
            if name == zone:
                return right - width / 2.0, row_h // 2.0 + self.px(1)
            right -= width
        return right, row_h // 2.0 + self.px(1)

    def _marker_zone_start(self):
        """右侧按钮区的左边界（点击落在这右边就不算"点条目本身"）。"""
        return self.list_w - self.marker_w

    def _zone_at(self, x):
        """x 落在哪个右侧按钮上；不在按钮区返回 None。"""
        right = self.list_w
        for name, width in self.markers:
            if x >= right - width:
                return name
            right -= width
        return None

    def _draw_steps_marker(self, canvas, row_h, done, total):
        """「子步骤」按钮：没有子步骤时画 ≡，有子步骤时画 已完成/总数。"""
        cx, cy = self._marker_center("steps", row_h)
        text = ("%d/%d" % (done, total)) if total else "≡"
        text_id = canvas.create_text(cx, cy, text=text, fill=C_MUTED,
                                     font=self.fonts["marker"], tags="marker")
        return ((text_id, "text"),)

    def _draw_delete_marker(self, canvas, row_h):
        """删除按钮：×（原来那个位置和判定都没变）。"""
        cx, cy = self._marker_center("delete", row_h)
        text_id = canvas.create_text(cx, cy, text="×", fill=C_MUTED,
                                     font=self.fonts["bold"], tags="marker")
        return ((text_id, "text"),)

    def _colorize(self, canvas, items, color, filled=False):
        """给一组图元上色：圆环改 outline、三角旗改 outline+fill、其余改 fill。"""
        for item_id, kind in items:
            try:
                if kind == "oval":
                    canvas.itemconfigure(item_id, outline=color)
                elif kind == "poly":
                    canvas.itemconfigure(item_id, outline=color,
                                         fill=color if filled else "")
                else:
                    canvas.itemconfigure(item_id, fill=color)
            except tk.TclError:
                return

    def _paint_markers(self, canvas, hover_zone):
        """给右侧两个按钮上色（子步骤格：无步骤＝≡灰、部分＝暗、全完成＝强调色）。"""
        markers = getattr(canvas, "_markers", None)
        if not markers:
            return
        total = getattr(canvas, "_steps_total", 0)
        done = getattr(canvas, "_steps_done", 0)
        if not total:
            color = C_MUTED
        elif done >= total:
            color = C_ACCENT
        else:
            color = C_DIM
        if hover_zone == "steps":
            color = C_ACCENT
        self._colorize(canvas, markers["steps"], color)
        self._colorize(canvas, markers["delete"],
                       C_DANGER if hover_zone == "delete" else C_MUTED)

    def _redraw_row(self, item_id):
        """只重画这一行（原地更新），不重建控件。

        勾选以前是 store.toggle() 后 _refresh() 整表重建；而 Tk 判定双击要求两次点击
        落在同一个控件上——重建后第二次点击只能算「新控件上的单击」，于是
        <Double-Button-1> 永远不触发，双击改文字在真机上永远进不去。
        改成原地重画之后，行控件活下来，双击才能按文档那样工作。
        """
        widget = self._row_widget(item_id)
        item = self._find_item(item_id)
        if widget is None or item is None:
            return False
        if self._edit_entry is not None and self._edit_id == item_id:
            return False                     # 正在编辑这一行，别把它盖掉
        if self._steps_edit is not None and self._steps_edit[1] == item_id:
            return False                     # 正在编辑子步骤，同理
        before = int(widget.cget("height"))
        self._draw_row(widget, item)
        if int(widget.cget("height")) != before:
            self.root.update_idletasks()
            self._resize()                   # 行高变了才需要重排窗口高度
        return True

    def _make_row(self, item):
        row = tk.Canvas(self.inner, bg=C_LIST, highlightthickness=0, bd=0,
                        width=self.list_w, height=self.px(20), cursor="hand2")
        row.pack(fill="x")
        self._draw_row(row, item)

        item_id = item["id"]

        def on_motion(event):
            self._paint_markers(row, self._zone_at(event.x))

        def on_leave(event):
            self._paint_markers(row, None)

        row.bind("<Button-1>", lambda event, i=item_id: self._row_press(event, i))
        row.bind("<B1-Motion>", self._row_motion)
        row.bind("<ButtonRelease-1>", self._row_release)
        row.bind("<Double-Button-1>", lambda event, i=item_id: self._begin_edit(i, event))
        row.bind("<Button-3>", lambda event, i=item_id: self._popup_row_menu(event, i))
        row.bind("<Motion>", on_motion)
        row.bind("<Leave>", on_leave)
        self._bind_wheel(row)

        self.row_widgets.append((item_id, row))

    # ---- 条目拖拽排序 ----
    def _row_press(self, event, item_id):
        """记住按下的位置：右侧按钮各自成区，子步骤小方框单独成区，其余当"点条目本身"。"""
        zone, step = self._zone_at(event.x), None
        event_y = getattr(event, "y", 0)
        widget = self._row_widget(item_id)
        if zone is None and widget is not None:
            for index, y0, y1, x0, x1 in getattr(widget, "_step_boxes", None) or []:
                if x0 <= event.x <= x1 and y0 <= event_y <= y1:
                    zone, step = "step", index
                    break
            if zone is None and event_y >= getattr(widget, "_step_top", 1e9):
                zone = "step-area"        # 子步骤那一行（但不是小方框）：什么也不做
        self._press = {"id": item_id, "y": event.y_root, "zone": zone or "body",
                       "step": step}

    def _row_motion(self, event):
        if self._press is None:
            return
        self._pointer_y = event.y_root
        if self._drag_id is None:
            if self._press["zone"] != "body":
                return
            if abs(event.y_root - self._press["y"]) < self.px(5):
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
        zone = press["zone"]
        if zone == "step-area":
            # 点子步骤那一行的文字（不是小方框）：直接进子步骤编辑，光标落在这一行。
            # 原来这里是空的 —— 用户点步骤文字毫无反应，看起来就像"子步骤编不了"。
            widget_now = self._row_widget(press["id"])
            if widget_now is not None:
                self._begin_steps_edit(press["id"],
                                       step_index=self._step_index_at(widget_now, event))
            return
        if zone == "step":
            widget = self._row_widget(press["id"])
            hit = None
            for index, y0, y1, x0, x1 in getattr(widget, "_step_boxes", None) or []:
                if x0 <= event.x <= x1 and y0 <= getattr(event, "y", 0) <= y1:
                    hit = index
                    break
            if hit is not None and hit == press.get("step"):
                self.toggle_step_item(press["id"], hit)
            return
        if zone == "body":
            self.toggle_item(press["id"])
            return
        if self._zone_at(event.x) != zone:
            return                       # 松手时指针得还在同一个按钮上才算数
        if zone == "delete":
            self.remove_item(press["id"])
        elif zone == "steps":
            self._begin_steps_edit(press["id"])

    def _begin_drag(self, item_id):
        self._cancel_regroup()               # 拖动期间别归位（归位会重建行，拖到一半会乱）
        self._drag_id = item_id
        widget = self._row_widget(item_id)
        if widget is not None:
            # 用 Canvas 图元画高亮底 + 左侧强调条：改 Canvas 的 bg 在这里不会重绘
            height = int(widget.cget("height"))
            widget.configure(cursor="fleur")
            widget.create_rectangle(0, 0, self.list_w, height, fill=C_HOVER, outline="",
                                    tags="dragbg")
            widget.create_rectangle(0, 0, self.px(3), height, fill=C_ACCENT, outline="",
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
        self._schedule_regroup()             # 万一之前有改过状态还没归位，顺手补上

    def _row_widget(self, item_id):
        for row_id, widget in self.row_widgets:
            if row_id == item_id:
                return widget
        return None

    def _drag_to(self, y_root):
        """把被拖动的条目挪到指针所在的那一行（实时换位，松手才落盘）。

        只在自己那一「段」里换位：默认排序是未完成 → 等待中 → 已完成，拖过界也只贴到
        本段的头/尾，免得一次拖动就把分组打乱（想换段就点勾选框或小钟）。
        """
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
        target = self._clamp_to_group(self._drag_id, target)
        if target == self._index_of(self._drag_id):
            return
        if self.store.move(self._drag_id, target, save=False):
            self._repack_rows()

    def _clamp_to_group(self, item_id, target):
        """把落点夹到该条目所在状态段内（默认排序是分段的，拖拽不跨段）。"""
        low, high = self._group_span(item_id)
        return max(low, min(int(target), high))

    def _bucket_of(self, item):
        """排序桶：状态 → 是否今天 → 优先级。排序键就是这三样，拖拽也只在桶内换位。"""
        state = self.store.state_of(item)
        today_rank = 0 if (item.get("today") and state != STATE_DONE) else 1
        return (state, today_rank, -self.store.priority_of(item))

    def _group_span(self, item_id):
        """被拖条目所在「桶」在 row_widgets 里的下标范围（含两端）。

        桶 = 状态 + 是否今天 + 优先级。拖拽只在这个范围里换位，免得松手一归位就被弹回去
        （想换桶就去点勾选框 / 小钟 / 今 / 旗子）。
        """
        last = max(0, len(self.row_widgets) - 1)
        item = self._find_item(item_id)
        if item is None:
            return 0, last
        key = self._bucket_of(item)
        indexes = []
        for index, (row_id, _) in enumerate(self.row_widgets):
            row_item = self._find_item(row_id)
            if row_item is not None and self._bucket_of(row_item) == key:
                indexes.append(index)
        if not indexes:
            return 0, last
        return min(indexes), max(indexes)

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
        if self.inner.winfo_reqheight() > self.list_max_h:
            top = self.canvas.winfo_rooty()
            bottom = top + self.canvas.winfo_height()
            if self._pointer_y < top + self.px(14):
                self.canvas.yview_scroll(-1, "units")
                self._drag_to(self._pointer_y)
            elif self._pointer_y > bottom - self.px(14):
                self.canvas.yview_scroll(1, "units")
                self._drag_to(self._pointer_y)
        self._start_autoscroll()

    def _update_counts(self):
        self._refresh_tags()
        visible = self._visible_items()
        remaining = self.store.remaining(visible)
        waiting = self.store.waiting_count(visible)
        if visible and remaining == 0:
            text = "全部完成"
        elif waiting:
            text = "%d 项未完成 · %d 等待" % (remaining, waiting)
        else:
            text = "%d 项未完成" % remaining
        self.count.configure(text=text)

    def _update_footer(self):
        self._update_hint()
        if self.store.has_done():
            self.clear_btn.pack(side="right")
        else:
            self.clear_btn.pack_forget()

    def _archive_on_start(self):
        """启动时看一眼：有没有「今天 00:00 之前完成」的，有就收进归档。

        刻意**不做**周期性检查 —— 免得东西在你眼前 00:00 那一下消失；
        只在打开它的时候收（一天最多一次，`_archived_day` 记着今天已经看过了）。
        """
        self._archive_tick()

    def _archive_tick(self, now=None):
        """跨天就把「今天 00:00 之前完成」的条目收进归档；返回收了几条（同一天再叫返回 0）。"""
        now = time.time() if now is None else now
        day = self.store.day_start(now)
        if self._archived_day == day:
            return 0
        moved = self.store.archive_completed(now)
        self._archived_day = day
        if moved:
            self._refresh(keep_scroll=False)
        return moved

    def _update_hint(self):
        """底部提示：折叠时提示展开，条目够多时提示可拖动排序 / 双击可改。"""
        if self._undo is not None:
            return                     # 正在显示「已删除…＋撤销」，别把它覆盖掉
        if self.filter_tag:
            self.hint.configure(text="只看 @%s（%d 条）· 点上面的「全部」取消"
                                     % (self.filter_tag, len(self._visible_items())),
                                fg=C_ACCENT)
            return
        if self.store.ui["collapsed"]:
            text = "双击标题展开"
        elif self.store.today_stats()[1]:
            done, total = self.store.today_stats()
            self.hint.configure(text="今天 %d/%d" % (done, total), fg=C_ACCENT)
            return
        elif len(self.store.items) >= 2:
            text = "拖动可排序，双击可改"
        else:
            text = "双击标题可折叠"
        self.hint.configure(text=text, fg=C_MUTED)

    def _show_undo(self, record):
        """底部临时显示「已删除「…」 + 撤销」，过一会儿自动收起。"""
        text = record["item"]["text"]
        limit = 14
        if len(text) > limit:
            text = text[:limit] + "…"
        self.hint.configure(text="已删除「%s」" % text, fg=C_TEXT)
        self.undo_btn.pack(side="left", padx=(self.px(6), 0))
        if self._undo_job is not None:
            try:
                self.root.after_cancel(self._undo_job)
            except (tk.TclError, ValueError):
                pass
        self._undo_job = self.root.after(int(UNDO_SECONDS * 1000), self._clear_undo)

    def _clear_undo(self):
        """撤销时限到了：收起按钮、恢复普通提示。"""
        self._undo_job = None
        self._undo = None
        self.undo_btn.pack_forget()
        self._update_hint()

    def _on_inner_configure(self, event=None):
        height = min(self.inner.winfo_reqheight(), self.list_max_h)
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
            height = self.header.winfo_reqheight() + self.px(2)
        else:
            height = self.card.winfo_reqheight() + self.px(2)
        width = self.panel_w
        if self._user_sized:
            # 用户自己拉过大小：只增不减，别把他的窗口缩回去
            try:
                width = max(width, self.root.winfo_width())
                height = max(height, self.root.winfo_height())
            except tk.TclError:
                pass
        self.root.geometry("%dx%d" % (width, height))

    def _restore_position(self):
        self.root.update_idletasks()
        self._apply_saved_size()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = self.store.ui["x"]
        y = self.store.ui["y"]
        if x is None or y is None:
            x = screen_w - self.panel_w - self.px(48)
            y = self.px(96)
        x = max(self.px(80) - self.panel_w, min(int(x), screen_w - self.px(80)))
        y = max(0, min(int(y), screen_h - self.px(40)))
        self.root.geometry("+%d+%d" % (x, y))

    def _apply_collapsed(self):
        if self.store.ui["collapsed"]:
            self.input_row.pack_forget()
            self.list_area.pack_forget()
            self.footer.pack_forget()
        else:
            self.input_row.pack(fill="x", padx=self.pad, pady=(self.px(2), self.px(8)))
            self.list_area.pack(fill="x", padx=self.pad, pady=(0, self.px(8)))
            self.footer.pack(fill="x", padx=self.pad,
                             pady=(0, max(0, self.pad - self.px(4))))
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
        if self._user_sized:
            self.store.ui["width"] = self.root.winfo_width()
            self.store.ui["height"] = self.root.winfo_height()
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
        """勾选 / 取消：只重画这一行，不整表重建（否则双击永远到不了编辑）。"""
        item = self.store.toggle(item_id)
        if item is None:
            return None
        self._last_toggle = (item_id, time.time())
        if not self._redraw_row(item_id):
            self._refresh()
        self._schedule_regroup()
        self._update_counts()
        self._update_footer()
        return item

    def toggle_waiting_item(self, item_id):
        """标记 / 取消「等待中」：同样只重画那一行。

        等待中不是完成：事情已经着手，但在等别人反馈，所以从「未完成」里分出来单列，
        计数里也单独显示，免得天天看到它以为自己在拖。
        """
        item = self.store.toggle_waiting(item_id)
        if item is None:
            return None
        if not self._redraw_row(item_id):
            self._refresh()
        self._schedule_regroup()
        self._update_counts()
        self._update_footer()
        return item

    def add_step_item(self, item_id):
        """右键菜单：加一条子步骤，然后重画那一行。"""
        item = self.store.add_step(item_id)
        if item is None:
            return None
        if not self._redraw_row(item_id):
            self._refresh()
        return item

    def set_priority_item(self, item_id, value):
        """右键菜单里选了优先级：重画那一行。"""
        item = self.store.set_priority(item_id, value)
        if item is None:
            return None
        if not self._redraw_row(item_id):
            self._refresh()
        return item

    def toggle_step_item(self, item_id, index):
        """勾选一个子步骤：只重画那一行；不动主条目的状态，也不参与排序。"""
        item = self.store.toggle_step(item_id, index)
        if item is None:
            return None
        if not self._redraw_row(item_id):
            self._refresh()
        self._update_footer()
        return item

    def cycle_priority_item(self, item_id):
        """点旗子：无 → 高 → 中 → 无。排序会变，所以照例延迟归位。"""
        item = self.store.cycle_priority(item_id)
        if item is None:
            return None
        if not self._redraw_row(item_id):
            self._refresh()
        self._schedule_regroup()
        return item

    def toggle_today_item(self, item_id):
        """标记 / 取消「今天要办的」：会浮到本段最前，所以也延迟归位。"""
        item = self.store.toggle_today(item_id)
        if item is None:
            return None
        if not self._redraw_row(item_id):
            self._refresh()
        self._schedule_regroup()
        self._update_footer()             # 底部「今天 x/y」要跟着变
        return item

    # ---- 归位（默认排序：未完成 → 等待中 → 已完成）----
    def _schedule_regroup(self):
        """状态变了先别急着把它挪到别的段去，等用户停手再归位。

        原因和「双击改名」直接相关：双击的第一下按单击处理会改状态，如果这一下就让整行
        飞到列表底部，第二下就落到别的行上了 —— Tk 判定双击要求两次点击在同一个控件上，
        编辑框再也打不开。所以归位推迟到 REGROUP_DELAY_MS 之后，且在拖动/编辑/按住时顺延。
        """
        self._cancel_regroup()
        try:
            self._regroup_job = self.root.after(self.REGROUP_DELAY_MS, self._regroup_now)
        except tk.TclError:
            self._regroup_job = None

    def _cancel_regroup(self):
        if self._regroup_job is not None:
            job, self._regroup_job = self._regroup_job, None
            try:
                self.root.after_cancel(job)
            except (tk.TclError, ValueError):
                pass

    def _regroup_now(self):
        """把列表按状态归位；用户正在操作时顺延，别抢他的鼠标。"""
        self._regroup_job = None
        if self._drag_id is not None or self._press is not None \
                or self._edit_entry is not None:
            self._schedule_regroup()
            return
        if self.store.resort(save=True):
            self._refresh()

    def remove_item(self, item_id):
        """删除一条：先记下它和原位置，底部留 8 秒撤销窗口。"""
        if self._edit_entry is not None:
            self._end_edit(save=True)      # 正在编辑就先收好，别改到已经删掉的行
        record = self.store.pop(item_id)
        if record is None:
            return
        self._undo = record
        self._refresh()
        self._show_undo(record)

    def undo_delete(self):
        """把刚删掉的那条放回它原来的位置。"""
        record, self._undo = self._undo, None
        if self._undo_job is not None:
            try:
                self.root.after_cancel(self._undo_job)
            except (tk.TclError, ValueError):
                pass
            self._undo_job = None
        self.undo_btn.pack_forget()
        if record is not None and self.store.restore(record):
            self._refresh()
        else:
            self._update_hint()
        return record

    # ---- 双击编辑 ----
    def _find_item(self, item_id):
        for item in self.store.items:
            if item["id"] == item_id:
                return item
        return None

    def _begin_edit(self, item_id, event=None):
        """双击条目：在那一行上盖一个输入框；回车保存、Esc 取消、点到别处也算保存。"""
        if event is not None and event.x >= self._marker_zone_start():
            return                         # 双击落在右侧按钮上，不当编辑
        if event is not None:
            widget_now = self._row_widget(item_id)
            if widget_now is not None and getattr(event, "y", 0) >= \
                    getattr(widget_now, "_step_top", 1e9):
                # 双击在子步骤那一行上：进"子步骤编辑"，光标落在双击的那一行，
                # 而不是什么都不做、也不是去改主文字（这两点都踩过）
                self._begin_steps_edit(item_id,
                                       step_index=self._step_index_at(widget_now, event))
                return
        self._cancel_regroup()             # 编辑期间不归位：_refresh() 会把输入框一起销毁
        self._end_steps_edit(save=True)    # 子步骤编辑器也先收掉
        if self._edit_entry is not None:
            self._end_edit(save=True)      # 先收掉上一个
        # 双击的第一下已经按「单击 = 勾选」处理过了；双击的语义是改文字，
        # 不该顺手改完成状态，所以这里把那次勾选撤回来。
        last_id, last_at = self._last_toggle
        if (event is not None and last_id == item_id
                and (time.time() - last_at) < DOUBLE_CLICK_REVERT_S):
            self._last_toggle = (None, 0.0)
            self.store.toggle(item_id)
            self._redraw_row(item_id)
            self._update_counts()
            self._update_footer()
        widget = self._row_widget(item_id)
        item = self._find_item(item_id)
        if widget is None or item is None:
            return
        entry = tk.Entry(widget, bg=C_BORDER, fg=C_TEXT, insertbackground=C_TEXT,
                         relief="flat", bd=0, font=self.fonts["base"],
                         highlightthickness=0)
        tags = self.store.tags_of(item)
        entry.insert(0, item["text"] + (" " + " ".join("@" + t for t in tags) if tags else ""))
        entry.select_range(0, "end")
        height = int(widget.cget("height"))
        entry.place(x=self.text_x - self.px(2), y=self.px(2),
                    width=max(self.px(40),
                              self.list_w - self.text_x - self.del_w - self.px(2)),
                    height=max(self.px(16), height - 2 * self.px(2)))
        entry.bind("<Return>", lambda event: self._end_edit(save=True))
        entry.bind("<KP_Enter>", lambda event: self._end_edit(save=True))   # 小键盘回车
        entry.bind("<Escape>", lambda event: self._end_edit(save=False))
        entry.bind("<FocusOut>", lambda event: self._end_edit(save=True))
        self._edit_id = item_id
        self._edit_entry = entry
        try:
            entry.focus_force()
        except tk.TclError:
            pass

    def _end_edit(self, save=True):
        """结束编辑：先把状态摘掉再销毁控件，免得回调打在被销毁的控件上。"""
        entry, item_id = self._edit_entry, self._edit_id
        if entry is None:
            return False
        self._edit_entry = None
        self._edit_id = None
        try:
            text = entry.get()
        except tk.TclError:
            return False
        try:
            entry.destroy()
        except tk.TclError:
            pass
        changed = self.store.rename(item_id, text) if save else False
        self._refresh()
        self._schedule_regroup()             # 编辑结束再归位（编辑中归位会把输入框销毁）
        return changed

    def _build_row_menu(self, item_id):
        """右键条目的菜单。右侧图标区删掉的动作都搬到这里（拆出来是为了自检能查它）。"""
        item = self._find_item(item_id)
        if item is None:
            return None
        menu = tk.Menu(self.root, tearoff=0)
        waiting = self.store.state_of(item) == STATE_WAITING
        menu.add_command(label="取消「等待中」" if waiting else "标记为「等待中」",
                         command=lambda: self.toggle_waiting_item(item_id))
        marked = bool(item.get("today"))
        menu.add_command(label="取消「今天要办的」" if marked else "标记为「今天要办的」",
                         command=lambda: self.toggle_today_item(item_id))
        priority = self.store.priority_of(item)
        prio_names = {PRIORITY_NONE: "无", PRIORITY_MID: "中", PRIORITY_HIGH: "高"}
        prio_menu = tk.Menu(menu, tearoff=0)
        prio_var = tk.IntVar(master=self.root, value=priority)
        menu._prio_var = prio_var                 # 别让 IntVar 被回收
        for label, value in (("高", PRIORITY_HIGH), ("中", PRIORITY_MID),
                             ("无", PRIORITY_NONE)):
            prio_menu.add_radiobutton(label=label, value=value, variable=prio_var,
                                      command=lambda v=value: self.set_priority_item(item_id, v))
        menu.add_cascade(label="优先级（现在是%s）" % prio_names.get(priority, "无"),
                         menu=prio_menu)
        done, total = self.store.steps_progress(item)
        menu.add_separator()
        menu.add_command(label=("编辑子步骤…" if not total
                                else "编辑子步骤（%d/%d 已完成）…" % (done, total)),
                         command=lambda: self._begin_steps_edit(item_id))
        menu.add_command(label="＋ 加一条子步骤",
                         command=lambda: self.add_step_item(item_id))
        if total:
            menu.add_command(label="清空子步骤",
                             command=lambda: self._clear_steps(item_id))
        menu.add_separator()
        menu.add_command(label="删除这条", command=lambda: self.remove_item(item_id))
        return menu

    def _popup_row_menu(self, event, item_id):
        menu = self._build_row_menu(item_id)
        if menu is None:
            return
        self._dismiss_row_menu()               # 先收起上一个，别堆着
        self._row_menu = menu
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _dismiss_row_menu(self, *args, **kwargs):
        """收起右键菜单：点别处、再右键、或菜单被选过之后都走这里。

        签名为什么收 *args：Tk 走 bind_all 时实测会**传两个参数**进来
        （TypeError: <lambda>() takes 1 positional argument but 2 were given）——
        收窄成 event 一个参数的话，回调会抛异常、菜单就收不掉了。
        Tk 自带的"点外部自动收起"在这套无边框窗口上也不可靠（实测点空白处菜单不走），
        所以额外挂全局绑定兜底，并且**销毁**菜单控件，免得每次右键都堆一个。
        """
        menu = self._row_menu
        if menu is None:
            return
        event = args[0] if args else None
        hit = getattr(event, "widget", None)
        if hit is not None and str(hit).startswith(str(menu)):
            return                         # 点在菜单自己身上：交给菜单处理，别销毁
        self._row_menu = None
        try:
            menu.unpost()
        except tk.TclError:
            pass
        try:
            menu.destroy()
        except tk.TclError:
            pass

    def _clear_steps(self, item_id):
        if self.store.set_steps(item_id, []) is not None:
            if not self._redraw_row(item_id):
                self._refresh()

    # ---- 子步骤编辑（右键条目 → 编辑子步骤…）----
    def _step_index_at(self, canvas, event):
        """双击落在第几条子步骤上（靠行内已记下的小方框命中区算）。"""
        y = getattr(event, "y", 0)
        for index, y0, y1, _x0, _x1 in getattr(canvas, "_step_boxes", None) or []:
            if y0 <= y < y1:
                return index
        return None

    def _begin_steps_edit(self, item_id, step_index=None):
        """在那一行上盖一个多行输入框，每行一个子步骤。

        回车是换行，所以保存用 **Ctrl+回车** 或点到别处，Esc 取消。
        完成标记按行位置保留：只改文字不删行的话，勾选状态不会丢。
        """
        self._cancel_regroup()
        self._end_steps_edit(save=True)
        if self._edit_entry is not None:
            self._end_edit(save=True)
        widget = self._row_widget(item_id)
        item = self._find_item(item_id)
        if widget is None or item is None:
            return None
        box = tk.Text(widget, bg=C_BORDER, fg=C_TEXT, insertbackground=C_TEXT,
                      relief="flat", bd=0, highlightthickness=0,
                      font=self.fonts["base"], wrap="none")
        steps = self.store.steps_of(item)
        for step in steps:
            box.insert("end", step["text"] + "\n")
        height = int(widget.cget("height"))
        box.place(x=self.text_x - self.px(2), y=self.px(2),
                  width=max(self.px(60), self._marker_zone_start() - self.text_x),
                  height=max(self.px(20), height - 2 * self.px(2)))
        box.bind("<Escape>", lambda event: self._end_steps_edit(save=False))
        box.bind("<Return>", self._steps_save_key)            # 回车＝保存退出
        box.bind("<KP_Enter>", self._steps_save_key)          # 小键盘回车同样＝保存
        box.bind("<Control-Return>", self._steps_newline_key)  # Ctrl+回车＝换行
        box.bind("<FocusOut>", lambda event: self._end_steps_edit(save=True))
        self.hint.configure(text="回车=保存 · Ctrl+回车=换行 · Esc=取消 · 点别处也算保存",
                            fg=C_ACCENT)
        self._steps_edit = (box, item_id)
        try:
            box.focus_force()
            if step_index is not None and 0 <= step_index < len(steps):
                box.mark_set("insert", "%d.0" % (step_index + 1))
            else:
                box.mark_set("insert", "end-1c")
        except tk.TclError:
            pass
        return box

    def _steps_save_key(self, event=None):
        """子步骤编辑器里按回车：保存退出。"""
        self._end_steps_edit(save=True)
        return "break"                     # 别再往已销毁的框里插换行

    def _steps_newline_key(self, event=None):
        """子步骤编辑器里 Ctrl+回车：在光标处换行（编辑器不关）。"""
        record = self._steps_edit
        if record is not None:
            try:
                record[0].insert("insert", "\n")
            except tk.TclError:
                pass
        return "break"

    def _end_steps_edit(self, save=True):
        """收掉子步骤编辑器：save=False 直接丢弃；save=True 按行写回（保留原勾选）。"""
        record, self._steps_edit = self._steps_edit, None
        if record is None:
            return False
        box, item_id = record
        try:
            raw = box.get("1.0", "end")
        except tk.TclError:
            return False
        try:
            box.destroy()
        except tk.TclError:
            pass
        changed = False
        if save:
            before = self.store.steps_of(self._find_item(item_id) or {})
            lines = [" ".join(line.split()) for line in raw.splitlines()]
            steps = []
            for index, line in enumerate(line for line in lines if line):
                done = before[index]["done"] if index < len(before) else False
                steps.append({"text": line, "done": done})
            changed = self.store.set_steps(item_id, steps) is not None
        self._refresh()
        self._update_hint()
        self._schedule_regroup()
        return changed

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
        self._cancel_regroup()
        self.backend.hotkey_remove()              # 解掉全局快捷键（不然退出后那个键还空占着）
        self.backend.tray_remove(self.root)       # 先摘托盘图标并还原窗口过程
        self.backend.single_instance_stop(self.root)
        self.store.resort()                       # 退出前把顺序归位，文件里也是分好组的
        self.store.save()
        self.root.destroy()


# --------------------------------------------------------------------------- #
# 自检 / 自启动
# --------------------------------------------------------------------------- #
def run_check():
    """数据读写与界面构造自检，成功打印 SELFTEST OK 并返回 0。"""
    import tempfile

    if tk is None:
        sys.stderr.write("自检失败: 缺少 tkinter（统信 UOS / Debian: "
                         "sudo apt install python3-tk）\n")
        return 1

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

        # ---- 三态：未完成 / 等待中 / 已完成 ----
        # 「等待中」= 已经着手，但在等别人反馈，自己现在动不了：既不算完成，也不该混在待办里催自己。
        tri = Store(path)
        tri.clear_done()
        todo_item = tri.add("还没开始的一条")
        wait_item = tri.add("等同事回字段的一条")
        tri.toggle_waiting(wait_item["id"])
        if tri.state_of(wait_item) != STATE_WAITING:
            problems.append("toggle_waiting 没有把状态改成等待中")
        if wait_item["done"]:
            problems.append("等待中不该被算成已完成")
        if tri.waiting_count() != 1:
            problems.append("waiting_count 计数不对")
        if tri.remaining() != len(tri.items):
            problems.append("等待中的条目仍应算作「未完成」")
        again = Store(path)
        reading = [item for item in again.items if item["id"] == wait_item["id"]]
        if not reading or reading[0].get("state") != STATE_WAITING or reading[0]["done"]:
            problems.append("等待中状态没有落盘或被读错")
        for item in again.items:            # done 与 state 必须始终一致（老版本只读 done）
            if bool(item["done"]) != (again.state_of(item) == STATE_DONE):
                problems.append("done 与 state 不同步: %r" % (item,))
        again.toggle_waiting(wait_item["id"])          # 再点一次 → 回到未完成
        if again.state_of([i for i in again.items if i["id"] == wait_item["id"]][0]) \
                != STATE_TODO:
            problems.append("取消「等待中」失败")
        again.toggle_waiting(wait_item["id"])          # 等待中 → 勾选应为已完成
        again.toggle(wait_item["id"])
        if again.state_of([i for i in again.items if i["id"] == wait_item["id"]][0]) \
                != STATE_DONE:
            problems.append("等待中的条目勾选后应为已完成")
        again.toggle_waiting(wait_item["id"])          # 回到未完成
        again.toggle_waiting(wait_item["id"])          # 再标记等待中
        again.toggle(todo_item["id"])                  # 另一条标记完成
        if again.clear_done() != 1 or not any(i["id"] == wait_item["id"] for i in again.items):
            problems.append("清除已完成时把「等待中」的条目也删了")

        # 旧数据（只有 done、没有 state）要能读：缺 state 时按 done 推断
        legacy_path = os.path.join(temp_dir, "legacy.json")
        with open(legacy_path, "w", encoding="utf-8") as handle:
            json.dump({"items": [{"id": 1, "text": "老格式已完成", "done": True},
                                 {"id": 2, "text": "老格式未完成", "done": False}]}, handle)
        legacy = Store(legacy_path)
        legacy_states = dict((item["text"], legacy.state_of(item)) for item in legacy.items)
        if legacy_states.get("老格式已完成") != STATE_DONE \
                or legacy_states.get("老格式未完成") != STATE_TODO:
            problems.append("旧格式数据没有正确推断状态: %r" % (legacy_states,))
        if [item["text"] for item in legacy.items] != ["老格式未完成", "老格式已完成"]:
            problems.append("旧格式数据读进来没有按状态归位: %r"
                            % ([item["text"] for item in legacy.items],))

        # ---- 全局快捷键的解析（纯逻辑，两平台共用；真机注册在各自后端里）----
        hotkey_cases = {"Super+T": (0x40, "t"), "ctrl+alt+k": (0x0C, "k"),
                        "F5": (0x00, "F5"), "Shift+Space": (0x01, "space")}
        for spec, expect in hotkey_cases.items():
            if parse_hotkey(spec) != expect:
                problems.append("快捷键解析不对: %r → %r（期望 %r）"
                                % (spec, parse_hotkey(spec), expect))
        for bad in ("", "Ctrl", "Ctrl++", "Ctrl+Alt", "Foo+Bar",
                    "Ctrl+Alt+Shift+Meta+1+2", "中"):
            if parse_hotkey(bad) is not None:
                problems.append("认不出的快捷键应当返回 None: %r → %r"
                                % (bad, parse_hotkey(bad)))
        if format_hotkey("super+t") != "Super+T" or format_hotkey("ctrl+alt+k") != "Ctrl+Alt+K":
            problems.append("快捷键格式化不对: %r / %r"
                            % (format_hotkey("super+t"), format_hotkey("ctrl+alt+k")))
        if parse_hotkey("Super+T") != parse_hotkey("super-t"):
            problems.append("快捷键对大小写和连字符应当等价")

        # ---- 子步骤 ----
        subs = Store(os.path.join(temp_dir, "steps.json"))
        st1 = subs.add("带子步骤的一条")
        if subs.steps_of(st1) != [] or subs.steps_progress(st1) != (0, 0):
            problems.append("新条目应当没有子步骤")
        subs.set_steps(st1["id"], ["第一", "", "  第二  ",
                                   {"text": "第三", "done": True}])
        got_steps = subs.steps_of(st1)
        if [x["text"] for x in got_steps] != ["第一", "第二", "第三"] \
                or [x["done"] for x in got_steps] != [False, False, True]:
            problems.append("set_steps 没把数据收拾干净: %r" % (got_steps,))
        if subs.steps_progress(st1) != (1, 3):
            problems.append("子步骤进度统计不对: %r" % (subs.steps_progress(st1),))
        subs.toggle_step(st1["id"], 0)
        if subs.steps_of(st1)[0]["done"] is not True or subs.steps_progress(st1) != (2, 3):
            problems.append("toggle_step 没生效: %r" % (subs.steps_of(st1),))
        if subs.toggle_step(st1["id"], 99) is not None or subs.toggle_step(999999, 0) is not None:
            problems.append("越界的子步骤索引应当返回 None")
        subs.set_steps(st1["id"], ["步骤%02d" % i for i in range(STEP_MAX + 5)])
        if len(subs.steps_of(st1)) != STEP_MAX:
            problems.append("子步骤条数没有被限制在 %d: %r"
                            % (STEP_MAX, len(subs.steps_of(st1))))
        # 子步骤既不改主条目状态，也不参与排序
        state_before = subs.state_of(st1)
        order_before = [item["id"] for item in subs.items]
        subs.toggle_step(st1["id"], 0)
        subs.resort(save=True)
        if subs.state_of(st1) != state_before or st1["done"]:
            problems.append("勾选子步骤不该改动主条目的状态")
        if [item["id"] for item in subs.items] != order_before:
            problems.append("子步骤不该影响排序: %r → %r"
                            % (order_before, [item["id"] for item in subs.items]))
        reloaded_subs = Store(os.path.join(temp_dir, "steps.json"))
        if reloaded_subs.steps_of(reloaded_subs.items[0])[0]["done"] is not True:
            problems.append("子步骤的勾选没有落盘")
        bad_steps = Store(os.path.join(temp_dir, "steps-bad.json"))
        bad_item = bad_steps.add("坏数据")
        bad_item["steps"] = "不是列表"
        if bad_steps.steps_of(bad_item) != []:
            problems.append("steps 不是列表时应当按空处理")
        bad_item["steps"] = [None, 12, {"text": "  "}, {"text": "好", "done": 1}]
        if [x["text"] for x in bad_steps.steps_of(bad_item)] != ["好"]:
            problems.append("混杂的 steps 数据没有被清理: %r" % (bad_steps.steps_of(bad_item),))

        # ---- 优先级 / 「今天要办的」----
        marks = Store(os.path.join(temp_dir, "marks.json"))
        m1 = marks.add("第一条")          # 新增插到最前，所以列表顺序是 [第二条, 第一条]
        m2 = marks.add("第二条")
        if marks.priority_of(m1) != PRIORITY_NONE or m1.get("today"):
            problems.append("新条目的优先级/今天标记默认值不对")
        marks.cycle_priority(m1["id"])     # 无 → 高
        if marks.priority_of(m1) != PRIORITY_HIGH:
            problems.append("点一下旗子应该是高: %r" % (marks.priority_of(m1),))
        marks.cycle_priority(m1["id"])     # 高 → 中
        if marks.priority_of(m1) != PRIORITY_MID:
            problems.append("再点一下应该是中: %r" % (marks.priority_of(m1),))
        marks.cycle_priority(m1["id"])     # 中 → 无
        if marks.priority_of(m1) != PRIORITY_NONE:
            problems.append("第三下应该回到无: %r" % (marks.priority_of(m1),))
        if marks.cycle_priority(999999) is not None:
            problems.append("给不存在的条目设优先级应返回 None")
        # 「今天」要把老的那条顶到本段最前
        marks.toggle_today(m1["id"])
        if not m1.get("today") or marks.today_stats() != (0, 1):
            problems.append("「今天」标记或统计不对: %r %r"
                            % (m1.get("today"), marks.today_stats()))
        marks.resort()
        if [i["text"] for i in marks.items] != ["第一条", "第二条"]:
            problems.append("「今天」没有排在段内最前: %r"
                            % ([i["text"] for i in marks.items],))
        # 同为今天时先看优先级：给第二条高，它应该回到最前
        marks.cycle_priority(m2["id"])
        marks.toggle_today(m2["id"])
        marks.resort()
        if [i["text"] for i in marks.items] != ["第二条", "第一条"]:
            problems.append("同是今天时应先按优先级: %r"
                            % ([(i["text"], i.get("priority")) for i in marks.items],))
        # 已完成的条目不参与「今天」置顶，但仍计入今天的进度
        marks.toggle(m2["id"])
        marks.resort(save=True)
        if marks.items[-1]["text"] != "第二条":
            problems.append("已完成的条目不该被「今天」顶到最前: %r"
                            % ([i["text"] for i in marks.items],))
        if marks.today_stats() != (1, 2):
            problems.append("今天的进度统计不对: %r" % (marks.today_stats(),))
        saved = Store(os.path.join(temp_dir, "marks.json"))
        by_text = dict((i["text"], i) for i in saved.items)
        if (saved.priority_of(by_text.get("第二条") or {}) != PRIORITY_HIGH
                or not (by_text.get("第一条") or {}).get("today")):
            problems.append("优先级/今天没有落盘: %r"
                            % ([(i["text"], i.get("priority"), i.get("today"))
                                for i in saved.items],))
        if [i["text"] for i in saved.items] != [i["text"] for i in marks.items]:
            problems.append("归位后的顺序没有落盘: %r" % ([i["text"] for i in saved.items],))

        # ---- 默认排序：未完成 → 等待中 → 已完成（同组内保持手动顺序）----
        order = Store(os.path.join(temp_dir, "order.json"))
        a = order.add("A")            # 顺序：A B C D（新增插到最前）
        b = order.add("B")
        c = order.add("C")
        d = order.add("D")
        order.toggle(d["id"])                    # D 已完成
        order.toggle_waiting(c["id"])            # C 等待中
        order.resort()
        got_order = [item["text"] for item in order.items]
        if got_order != ["B", "A", "C", "D"]:
            problems.append("归位后顺序不对（期望 未完成B,A → 等待中C → 已完成D）: %r" % (got_order,))
        if order.resort():
            problems.append("已经排好序的列表再排一次不该有变化")
        # 同组内手动顺序要保住：把 A 拖到 B 前面
        order.move(a["id"], 0)
        order.resort()
        if [item["text"] for item in order.items] != ["A", "B", "C", "D"]:
            problems.append("归位把同组内拖出来的顺序打乱了: %r"
                            % ([item["text"] for item in order.items],))
        order.save()
        if [item["text"] for item in Store(os.path.join(temp_dir, "order.json")).items] \
                != ["A", "B", "C", "D"]:
            problems.append("归位后的顺序没有落盘")
        messy_path = os.path.join(temp_dir, "messy.json")
        with open(messy_path, "w", encoding="utf-8") as handle:
            json.dump({"items": [
                {"id": 1, "text": "已完成", "done": True},
                {"id": 2, "text": "未完成1", "done": False},
                {"id": 3, "text": "已完成2", "done": True},
                {"id": 4, "text": "未完成2", "done": False},
            ]}, handle)
        messy = Store(messy_path)
        if [item["text"] for item in messy.items] != \
                ["未完成1", "未完成2", "已完成", "已完成2"]:
            problems.append("乱序的老数据读进来没有归位: %r"
                            % ([item["text"] for item in messy.items],))

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

        # 改名 + 删除后撤销：pop() 要交出原位置，restore() 要放回原位
        editing = Store(path)
        before_texts = [item["text"] for item in editing.items]
        keep = editing.add("会被删掉的一条")
        editing.add("后面的")
        record = editing.pop(keep["id"])
        if record is None:
            problems.append("pop() 没找到条目")
        elif [item["text"] for item in Store(path).items] != ["后面的"] + before_texts:
            problems.append("pop() 没有正确移除条目")
        elif not Store(path).restore(record):
            problems.append("restore() 没能把条目放回去")
        elif [item["text"] for item in Store(path).items] != \
                ["后面的", "会被删掉的一条"] + before_texts:
            problems.append("撤销删除后位置不对：%r"
                            % ([item["text"] for item in Store(path).items],))
        renamed = Store(path)
        first_item = renamed.items[0]
        if not renamed.rename(first_item["id"], "  改过的文字  "):
            problems.append("改名没有生效")
        elif Store(path).items[0]["text"] != "改过的文字":
            problems.append("改名未去空白或未落盘")
        if renamed.rename(first_item["id"], "   "):
            problems.append("改名不该接受空文本")
        if Store(path).restore(record):
            problems.append("restore() 不该把已经在列表里的条目再插一遍")

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
            # 勾选必须原地重画：行控件对象要活下来，否则 Tk 永远配不出 <Double-Button-1>
            # （这就是「双击改文字真机上没反应」的根因，用对象同一性在这里卡住）
            target = broken.items[0]
            row_before = widget._row_widget(target["id"])
            widget.toggle_item(target["id"])
            if widget._row_widget(target["id"]) is not row_before:
                problems.append("勾选后行控件被重建了（双击会因此失效）")
            # 原地重画还得真把内容改掉：勾选框在「已完成」时是 2 个图元（底 + 勾），
            # 未完成时 1 个（只有边框）。数量不对说明这一行没按新状态重画。
            expect_marks = 2 if target["done"] else 1
            if len(row_before.find_withtag("check")) != expect_marks:
                problems.append("勾选后行内容没有重画（原地更新失效）")
            if getattr(row_before, "_delete_item", None) is None:
                problems.append("重画后删除按钮的图元引用丢了")
            if widget._edit_entry is not None:
                problems.append("勾选不该进入编辑状态")
            # 双击手势：第一下按「单击＝勾选」处理，进编辑时要把那一下撤回来，
            # 且必须真的进入编辑状态（隐藏窗口下用桩事件走 _row_press/_row_release）
            class _StubEvent(object):
                def __init__(self, x, y_root=0, y=0):
                    self.x = x
                    self.y_root = y_root
                    self.y = y

            done_before = target["done"]
            body_x = widget.text_x
            widget._row_press(_StubEvent(x=body_x), target["id"])
            widget._row_release(_StubEvent(x=body_x))
            if target["done"] == done_before:
                problems.append("单击没有切换完成状态")
            widget._begin_edit(target["id"], _StubEvent(x=body_x))
            if target["done"] != done_before:
                problems.append("双击进编辑时没有撤回第一下的勾选")
            if widget._edit_entry is None:
                problems.append("双击编辑没有进入编辑状态")
            else:
                widget._edit_entry.delete(0, "end")
                widget._edit_entry.insert(0, "界面改名测试")
                widget._end_edit(save=True)
                edited = widget._find_item(target["id"])
                if edited is None or edited["text"] != "界面改名测试":
                    problems.append("界面改名没有生效")
            # 删除 + 撤销：要放回原位置
            order_before = [item["text"] for item in broken.items]
            victim = broken.items[0]
            widget.remove_item(victim["id"])
            if widget._undo is None or any(item["id"] == victim["id"]
                                           for item in broken.items):
                problems.append("删除后没有留下撤销记录")
            widget.undo_delete()
            if [item["text"] for item in broken.items] != order_before:
                problems.append("撤销删除后内容或顺序不一致")
            widget._refresh()
            # 「等待中」：格子已删，改从右键菜单切；切完那一行必须原地重画成琥珀字
            row_wait = widget._row_widget(target["id"])

            def menu_label(menu_now, index):
                """取菜单项标签；分隔线没有 -label 选项，返回空串。"""
                try:
                    return str(menu_now.entrycget(index, "label"))
                except tk.TclError:
                    return ""

            def menu_find(menu_now, needle):
                if menu_now is None:
                    return None
                for index in range(menu_now.index("end") + 1):
                    if needle in menu_label(menu_now, index):
                        return index
                return None

            def wait_menu_item(item_id):
                menu_now = widget._build_row_menu(item_id)
                return menu_now, menu_find(menu_now, "等待中")

            menu, wait_index = wait_menu_item(target["id"])
            if wait_index is None:
                problems.append("右键菜单里没有「等待中」这一项")
            else:
                menu.invoke(wait_index)
                if widget.store.state_of(target) != STATE_WAITING:
                    problems.append("从菜单标记「等待中」没有生效")
                if widget._row_widget(target["id"]) is not row_wait:
                    problems.append("标记等待中时行控件被重建了（双击会因此失效）")
                if target["done"]:
                    problems.append("等待中不该同时是已完成")
                fills = {str(row_wait.itemcget(i, "fill"))
                         for i in row_wait.find_withtag("text")}
                if fills != {C_WAIT}:
                    problems.append("等待中的那一行正文没有变成琥珀色: %r" % (fills,))
                menu, wait_index = wait_menu_item(target["id"])
                if wait_index is not None:
                    menu.invoke(wait_index)
                if widget.store.state_of(target) != STATE_TODO:
                    problems.append("从菜单再点一次「等待中」没有回到未完成")
                if Store(path).state_of(
                        [i for i in Store(path).items if i["id"] == target["id"]][0]) != STATE_TODO:
                    problems.append("等待状态没有落盘")

            # ---- 子步骤：渲染 + 小方框勾选 + 步骤行保护 + 右键编辑器 ----
            step_item = widget.store.add("带三个子步骤的界面自检条")
            widget.store.set_steps(step_item["id"], ["步骤甲", "步骤乙", "步骤丙"])
            widget._refresh()
            row_steps = widget._row_widget(step_item["id"])
            boxes = getattr(row_steps, "_step_boxes", None) or []
            if len(boxes) != 3:
                problems.append("子步骤没画出三行小方框: %r" % (boxes,))
            else:
                box_h_before = int(row_steps.cget("height"))

                def steps_cell(row_now):
                    items = (getattr(row_now, "_markers", None) or {}).get("steps") or ()
                    return items[0][0] if items else None

                cell = steps_cell(row_steps)
                if cell is None or str(row_steps.itemcget(cell, "text")) != "0/3":
                    problems.append("子步骤格子上没有显示 0/3: %r"
                                    % (row_steps.itemcget(cell, "text") if cell else None,))
                index, y0, y1, x0, x1 = boxes[1]          # 勾第二个
                cx = (x0 + x1) // 2
                cy = (y0 + y1) // 2
                widget._row_press(_StubEvent(x=cx, y=cy), step_item["id"])
                widget._row_release(_StubEvent(x=cx, y=cy))
                steps_now = widget.store.steps_of(step_item)
                if [s["done"] for s in steps_now] != [False, True, False]:
                    problems.append("点小方框没有勾上第二个子步骤: %r" % (steps_now,))
                if widget._row_widget(step_item["id"]) is not row_steps:
                    problems.append("勾子步骤时行控件被重建了（双击会因此失效）")
                if widget.store.state_of(step_item) == STATE_DONE:
                    problems.append("勾子步骤不该把主条目标成已完成")
                if int(row_steps.cget("height")) != box_h_before:
                    problems.append("勾子步骤不该改变行高")
                cell = steps_cell(row_steps)
                if cell is None or str(row_steps.itemcget(cell, "text")) != "1/3":
                    problems.append("勾完一个后格子上应是 1/3: %r"
                                    % (row_steps.itemcget(cell, "text") if cell else None,))
                # 点步骤文字（不是小方框）什么也不该发生
                step_text_x = widget.text_x + widget.step_indent + 12
                widget._row_press(_StubEvent(x=step_text_x, y=cy), step_item["id"])
                widget._row_release(_StubEvent(x=step_text_x, y=cy))
                if [s["done"] for s in widget.store.steps_of(step_item)] != [False, True, False]:
                    problems.append("点子步骤文字不该改动勾选")
                if widget.store.state_of(step_item) == STATE_DONE:
                    problems.append("点子步骤文字不该把主条目标成已完成")
                if widget._steps_edit is None:
                    problems.append("点子步骤文字应当打开子步骤编辑器（点了没反应＝编不了）")
                else:
                    line_now = int(widget._steps_edit[0].index("insert").split(".")[0])
                    if line_now != 2:
                        problems.append("点第 2 条子步骤文字时，光标应当落在第 2 行: %r"
                                        % (line_now,))
                    widget._end_steps_edit(save=False)
                # 双击落在子步骤那一行 → 进子步骤编辑（不是主文字编辑、也不是没反应）
                widget._begin_edit(step_item["id"], _StubEvent(x=step_text_x, y=cy))
                if widget._edit_entry is not None:
                    problems.append("双击子步骤那一行不该打开主文字编辑框")
                if widget._steps_edit is None:
                    problems.append("双击子步骤那一行应当打开子步骤编辑器（否则双击没反应）")
                else:
                    cursor_line = int(widget._steps_edit[0].index("insert").split(".")[0])
                    if cursor_line != 2:
                        problems.append("双击第 2 条子步骤时，光标应当落在第 2 行: %r"
                                        % (cursor_line,))
                    widget._end_steps_edit(save=False)     # 收掉，别影响后面的用例
                # 右键条目的绑定必须在（子步骤编辑器的入口）
                fresh_row = widget._row_widget(step_item["id"])   # 上面收编辑器时重画过，旧引用已销毁
                if fresh_row is None or not fresh_row.bind("<Button-3>"):
                    problems.append("条目没有绑定右键菜单，子步骤编辑器就没有入口")
            # 编辑器：内容、保存（按行保留勾选）、Esc 取消
            widget._begin_steps_edit(step_item["id"])
            record = widget._steps_edit
            if not record or record[0] is None:
                problems.append("没有打开子步骤编辑器")
            else:
                box, _ = record
                text_now = box.get("1.0", "end").strip()
                if text_now != "步骤甲\n步骤乙\n步骤丙":
                    problems.append("编辑器内容不对: %r" % (text_now,))
                if widget._redraw_row(step_item["id"]):
                    problems.append("子步骤编辑器开着时不该重画那一行")
                if "回车=保存" not in str(widget.hint.cget("text")):
                    problems.append("子步骤编辑器开着时，底部应当写成「回车=保存」: %r"
                                    % (widget.hint.cget("text"),))
                src = open(__file__, encoding="utf-8").read()
                # 要搜的串拼出来 —— 直接写字面量的话，检查自己的源码里就有一份，
                # 于是"永远找得到"、反例抓不到（这个坑实测过：swapped 绑定仍报 SELFTEST OK）。
                want_return = 'box.bind("<Return>", self.' + '_steps_save_key)'
                want_ctrl = 'box.bind("<Control-Return>", self.' + '_steps_newline_key)'
                if want_return not in src or want_ctrl not in src:
                    problems.append("按键接线不对：回车要接 _steps_save_key、"
                                    "Ctrl+回车要接 _steps_newline_key")
                chars_before = len(box.get("1.0", "end"))
                widget._steps_newline_key()                 # 等价于按 Ctrl+回车
                if widget._steps_edit is None:
                    problems.append("Ctrl+回车 不该退出编辑（它的活是换行）")
                elif len(box.get("1.0", "end")) != chars_before + 1:
                    problems.append("Ctrl+回车 没有换行: %r" % (box.get("1.0", "end"),))
                box.delete("1.0", "end")
                box.insert("end", "步骤甲改了\n步骤丙\n新加的第四步\n")
                widget._end_steps_edit(save=True)
                steps_saved = widget.store.steps_of(step_item)
                if [s["text"] for s in steps_saved] != ["步骤甲改了", "步骤丙", "新加的第四步"]:
                    problems.append("子步骤编辑器保存的文本不对: %r" % (steps_saved,))
                if [s["done"] for s in steps_saved] != [False, True, False]:
                    problems.append("按行位置保留勾选这条不对: %r" % (steps_saved,))
                if widget._steps_edit is not None:
                    problems.append("保存后编辑器引用没清掉")
                if "回车=保存" in str(widget.hint.cget("text")):
                    problems.append("编辑器收掉后，底部提示应当恢复: %r"
                                    % (widget.hint.cget("text"),))
            widget._begin_steps_edit(step_item["id"])
            box = (widget._steps_edit or (None, None))[0]
            if box is not None:
                box.delete("1.0", "end")
                box.insert("end", "不该被保存\n")
                widget._end_steps_edit(save=False)
                if [s["text"] for s in widget.store.steps_of(step_item)] != \
                        ["步骤甲改了", "步骤丙", "新加的第四步"]:
                    problems.append("Esc 取消不该改动子步骤")
            else:
                problems.append("第二次打开子步骤编辑器失败")
            # 回车＝保存（真按键）：改一行再按回车，编辑器收掉且改动落盘
            widget._begin_steps_edit(step_item["id"])
            box_enter = (widget._steps_edit or (None, None))[0]
            if box_enter is None:
                problems.append("回车用例打不开子步骤编辑器")
            else:
                box_enter.delete("1.0", "end")
                box_enter.insert("end", "回车保存的第一行\n回车保存的第二行\n")
                widget._steps_save_key()                    # 等价于按回车
                if widget._steps_edit is not None:
                    problems.append("子步骤编辑器里按回车应当保存并退出")
                saved_now = [s["text"] for s in widget.store.steps_of(step_item)]
                if saved_now != ["回车保存的第一行", "回车保存的第二行"]:
                    problems.append("按回车保存的内容不对: %r" % (saved_now,))

            # 小键盘回车 / 点别处收菜单 / 右键加子步骤
            src2 = open(__file__, encoding="utf-8").read()
            kp_steps = 'box.bind("<KP_Enter>", self.' + '_steps_save_key)'
            kp_entry = 'entry.bind("<KP_Enter>"'
            if kp_steps not in src2 or kp_entry not in src2:
                problems.append("小键盘回车没接上（子步骤框和主文字框都要接 KP_Enter）")
            src3 = open(__file__, encoding="utf-8").read()
            if not widget.root.bind_all("<Button-1>"):
                problems.append("没有装「点别处收起右键菜单」的全局绑定")
            # 拼串查，免得检查自己的字面量把自己喂饱（这个坑栽过一次）
            bad_global = 'bind_all("<Button-' + '3>"'
            if bad_global in src3:
                problems.append("不许全局绑 <Button-3>：右键那一下会把刚弹出的菜单立刻收掉")
            probe_menu = widget._build_row_menu(step_item["id"])
            widget._row_menu = probe_menu
            blank = _StubEvent(x=0)
            blank.widget = widget.inner                 # 模拟"点在空白处"
            widget._dismiss_row_menu(blank)
            if widget._row_menu is not None:
                problems.append("点空白处没有收起右键菜单（引用没清）")
            elif probe_menu.winfo_exists():
                problems.append("收起右键菜单后控件没销毁（会越堆越多）")
            probe_menu2 = widget._build_row_menu(step_item["id"])
            widget._row_menu = probe_menu2
            inside = _StubEvent(x=0)
            inside.widget = probe_menu2                 # 模拟"点在菜单自己身上"
            widget._dismiss_row_menu(inside)
            if widget._row_menu is None or not probe_menu2.winfo_exists():
                problems.append("点在菜单自己身上不该把它销毁（否则菜单项点不动）")
            widget._dismiss_row_menu()                  # 清干净
            if widget._row_menu is not None:
                problems.append("收起菜单后引用没清掉")
            steps_before_add = len(widget.store.steps_of(step_item))
            widget.add_step_item(step_item["id"])
            if len(widget.store.steps_of(step_item)) != steps_before_add + 1:
                problems.append("「＋ 加一条子步骤」没有加出一条")
            widget.store.set_steps(step_item["id"],
                                   [{"text": "步骤%d" % i, "done": False}
                                    for i in range(STEP_MAX)])
            widget.add_step_item(step_item["id"])
            if len(widget.store.steps_of(step_item)) != STEP_MAX:
                problems.append("加子步骤也要受 %d 条上限约束" % STEP_MAX)

            # 全勾上（显式设成完成，别用 toggle —— 有的本来就是勾着的）：进度变强调色
            widget.store.set_steps(step_item["id"],
                                   [{"text": st["text"], "done": True}
                                    for st in widget.store.steps_of(step_item)])
            widget._refresh()
            row_all = widget._row_widget(step_item["id"])
            cell_all = steps_cell(row_all)
            if cell_all is None or str(row_all.itemcget(cell_all, "fill")) != C_ACCENT:
                problems.append("子步骤全完成时格子应是强调色（text=%r fill=%r 进度=%r）"
                                % (row_all.itemcget(cell_all, "text") if cell_all else None,
                                   row_all.itemcget(cell_all, "fill") if cell_all else None,
                                   widget.store.steps_progress(step_item)))
            # 收尾：删掉这条测试条目，后面用例面对的还是原来的列表
            widget.store.pop(step_item["id"])
            widget._refresh()

            # ---- 右侧图标区只许有「子步骤」与「删除」（钟/今/旗已删）----
            mark_item = broken.items[0]
            row_mark = widget._row_widget(mark_item["id"])
            zones = set((getattr(row_mark, "_markers", None) or {}).keys())
            if zones != {"delete", "steps"}:
                problems.append("右侧图标区应当只有 子步骤+删除，实际画了: %r" % (zones,))
            table = [name for name, _width in widget.markers]
            if table != ["delete", "steps"]:
                problems.append("尺寸表/点击判定里的格子应当只有 子步骤+删除，实际: %r"
                                % (table,))
            # 行为面：老格子所在的位置不该再被判成 wait/today/prio 区
            probes = [widget.list_w - widget.del_w - widget.wait_w // 2,
                      widget.list_w - widget.del_w - widget.wait_w - widget.today_w // 2,
                      widget.list_w - widget.del_w - widget.wait_w - widget.today_w \
                      - widget.prio_w // 2]
            for probe_x in probes:
                got_zone = widget._zone_at(probe_x)
                if got_zone in ("wait", "today", "prio"):
                    problems.append("x=%s 还被判成已删除的按钮区: %r" % (probe_x, got_zone))
            # 优先级：菜单里选 → 行左侧竖条（高＝整行、中＝半行、无＝不画）
            menu = widget._build_row_menu(mark_item["id"])
            cascade = [i for i in range(menu.index("end") + 1)
                       if "优先级" in menu_label(menu, i)]
            if not cascade:
                problems.append("右键菜单里没有「优先级」子菜单")
            else:
                sub = menu.nametowidget(menu.entrycget(cascade[0], "menu"))
                sub.invoke(0)                                  # 第一项 ＝ 高
                if widget.store.priority_of(mark_item) != PRIORITY_HIGH:
                    problems.append("菜单里选「高」没有生效: %r"
                                    % (widget.store.priority_of(mark_item),))
                if widget._row_widget(mark_item["id"]) is not row_mark:
                    problems.append("改优先级时行控件被重建了（双击会因此失效）")
                bars = row_mark.find_withtag("prio")
                row_h_now = int(row_mark.cget("height"))
                if len(bars) != 1 or str(row_mark.itemcget(bars[0], "fill")) != C_HIGH:
                    problems.append("高优先级应当在行左侧画一条实心橙竖条: %r" % (bars,))
                elif row_mark.bbox(bars[0])[3] - row_mark.bbox(bars[0])[1] < row_h_now * 0.8:
                    problems.append("高优先级的竖条应当是整行高: %r"
                                    % (row_mark.bbox(bars[0]),))
                sub.invoke(1)                                  # 中
                if widget.store.priority_of(mark_item) != PRIORITY_MID:
                    problems.append("菜单里选「中」没有生效")
                bars = row_mark.find_withtag("prio")
                if len(bars) != 1:
                    problems.append("中优先级也该有一条竖条: %r" % (bars,))
                elif row_mark.bbox(bars[0])[3] - row_mark.bbox(bars[0])[1] > row_h_now * 0.7:
                    problems.append("中优先级的竖条应当只有半行高: %r"
                                    % (row_mark.bbox(bars[0]),))
                sub.invoke(2)                                  # 无
                if widget.store.priority_of(mark_item) != PRIORITY_NONE \
                        or row_mark.find_withtag("prio"):
                    problems.append("选「无」之后竖条应当消失")
            # 今天：菜单里标记 → 正文前一个蓝点；底部显示今天进度
            menu = widget._build_row_menu(mark_item["id"])

            today_index = menu_find(menu, "今天")
            if today_index is None:
                problems.append("右键菜单里没有「今天要办的」")
            else:
                menu.invoke(today_index)
                if not mark_item.get("today"):
                    problems.append("从菜单标记「今天要办的」没有生效")
                dots = row_mark.find_withtag("today")
                if len(dots) != 1 or str(row_mark.itemcget(dots[0], "fill")) != C_ACCENT:
                    problems.append("今天的条目前面应当有一个蓝点: %r" % (dots,))
                if "今天" not in str(widget.hint.cget("text")):
                    problems.append("底部没有显示今天进度: %r" % (widget.hint.cget("text"),))
                menu = widget._build_row_menu(mark_item["id"])
                today_index = menu_find(menu, "今天")
                if today_index is not None:
                    menu.invoke(today_index)
                if mark_item.get("today"):
                    problems.append("再从菜单点一次「今天要办的」应取消标记")
            # 菜单里该有的动作都在（删除不真的点，免得弹出撤销条影响后面的提示断言）
            menu = widget._build_row_menu(mark_item["id"])
            labels = " ".join(menu_label(menu, i)
                              for i in range(menu.index("end") + 1))
            for wanted in ("等待中", "今天要办的", "优先级", "编辑子步骤",
                           "＋ 加一条子步骤", "删除这条"):
                if wanted not in labels:
                    problems.append("右键菜单缺少「%s」: %r" % (wanted, labels))
            probe = {"id": -1, "text": "x", "done": False, "state": STATE_TODO,
                     "priority": PRIORITY_NONE, "today": False}
            if widget._bucket_of(probe) == widget._bucket_of(dict(probe, priority=PRIORITY_HIGH)):
                problems.append("优先级没有参与排序桶")
            if widget._bucket_of(probe) == widget._bucket_of(dict(probe, today=True)):
                problems.append("「今天」没有参与排序桶")

            # ---- 情境标签：@ 解析 / 行尾显示 / 顶部筛选 ----
            tg = Store(os.path.join(temp_dir, "tags.json"))
            if Store.parse_tags("写周报") != ("写周报", []):
                problems.append("没标签的文字不该被改动")
            if Store.parse_tags("给彭老师打电话 @电话 @外出") != ("给彭老师打电话",
                                                              ["电话", "外出"]):
                problems.append("@ 解析不对: %r"
                                % (Store.parse_tags("给彭老师打电话 @电话 @外出"),))
            t2_text, t2_tags = Store.parse_tags("@电话 交资料 @电话 @家 @单位 @超出的")
            if t2_tags != ["电话", "家", "单位"] or "@超出的" not in t2_text:
                problems.append("标签去重/上限不对: %r" % ((t2_text, t2_tags),))
            t3_text, t3_tags = Store.parse_tags("邮箱@qq.com 和 a.com/x@y")
            if t3_tags or "@qq.com" not in t3_text:
                problems.append("邮箱/网址不该被当成标签: %r" % ((t3_text, t3_tags),))
            ti = tg.add("催白内障补贴 @电话")
            if ti["text"] != "催白内障补贴" or Store.tags_of(ti) != ["电话"]:
                problems.append("add 没有把 @标签 摘进 tags: %r" % (ti,))
            tg.rename(ti["id"], "催白内障补贴 @电话 @外出")
            if Store.tags_of(ti) != ["电话", "外出"]:
                problems.append("rename 没有更新标签: %r" % (Store.tags_of(ti),))
            tg.rename(ti["id"], "催白内障补贴")
            if Store.tags_of(ti) != []:
                problems.append("删掉 @标签 后 tags 应当为空: %r" % (Store.tags_of(ti),))
            tg.rename(ti["id"], "催白内障补贴 @电话")
            tg.add("买菜 @外出")
            if sorted(tg.all_tags()) != ["外出", "电话"]:
                problems.append("all_tags 不对: %r" % (tg.all_tags(),))
            reload_tags = Store(os.path.join(temp_dir, "tags.json"))
            got_tags = sorted(tag for item in reload_tags.items
                              for tag in Store.tags_of(item))
            if got_tags != ["外出", "电话"]:
                problems.append("标签没有落盘: %r" % (got_tags,))
            # 界面：正文顶满一行时，行尾标签也不许压到右侧图标区
            base_char = widget.fonts["base"].measure("长") or 1
            long_text = "长" * max(4, int(widget.row_wrap // base_char))
            tagged = widget.store.add(long_text + " @电话")
            no_tag_item = widget.store.add("界面无标签条")
            widget._refresh()
            row_tag = widget._row_widget(tagged["id"])
            tag_item = getattr(row_tag, "_tag_item", None)
            if tag_item is None or str(row_tag.itemcget(tag_item, "text")) != "@电话":
                problems.append("行尾没有画出 @电话 标签: %r" % (tag_item,))
            elif row_tag.bbox(tag_item)[2] > widget._marker_zone_start():
                problems.append("行尾标签压到了右侧图标区")
            if not widget.tags_row.winfo_children():
                problems.append("有标签时顶部筛选条没有生成筛选项")
            widget.set_filter("电话")
            if [i["id"] for i in widget._visible_items()] != [tagged["id"]]:
                problems.append("筛选后可见条目不对: %r"
                                % ([i["text"][:6] for i in widget._visible_items()],))
            if "只看 @电话" not in str(widget.hint.cget("text")):
                problems.append("筛选时底部没有提示: %r" % (widget.hint.cget("text"),))
            if widget.store.remaining(widget._visible_items()) != 1:
                problems.append("筛选后的计数不对")
            widget.set_filter(None)
            if len(widget._visible_items()) != len(widget.store.items):
                problems.append("取消筛选后应当看到全部")
            widget._begin_edit(tagged["id"])
            shown = widget._edit_entry.get() if widget._edit_entry is not None else ""
            if "@电话" not in shown:
                problems.append("编辑框没有回填 @电话: %r" % (shown,))
            widget._end_edit(save=True)
            if Store.tags_of(tagged) != ["电话"]:
                problems.append("编辑保存后标签丢了: %r" % (Store.tags_of(tagged),))
            for extra_id in (tagged["id"], no_tag_item["id"]):
                widget.store.pop(extra_id)
            widget._refresh()

            # 诊断日志：能真写出文件（下次"唤不回来"靠它定位）
            widget._diag_log("selftest-probe")
            log_path = os.path.join(os.path.dirname(widget.store.path), "hotkey.log")
            try:
                with open(log_path, encoding="utf-8") as handle:
                    if "selftest-probe" not in handle.read():
                        problems.append("诊断日志没有真的写出来（下次没法定位唤回问题）")
            except IOError:
                problems.append("诊断日志文件不存在")

            # ---- 归档：过 00:00 自动收走已完成的；删除进归档、可撤销 ----
            arc_path = os.path.join(temp_dir, "archive.json")
            arc = Store(arc_path)
            midnight = Store.day_start()
            old_done = arc.add("昨天就完成了的")
            arc.toggle(old_done["id"])
            old_done["done_at"] = midnight - 60           # 昨天 23:59 完成
            today_done = arc.add("今天刚完成的")
            arc.toggle(today_done["id"])
            plain = arc.add("还没做的")
            moved_count = arc.archive_completed()
            if moved_count != 1:
                problems.append("自动归档应当只收走昨天完成的那一条，实际 %r" % (moved_count,))
            left_ids = [item["id"] for item in arc.items]
            if old_done["id"] in left_ids or today_done["id"] not in left_ids \
                    or plain["id"] not in left_ids:
                problems.append("自动归档收错了: %r" % (left_ids,))
            total, counts = arc.archive_stats()
            if total != 1 or counts.get("done") != 1:
                problems.append("归档统计不对: %r" % ((total, counts),))
            reload_arc = Store(arc_path)
            if len(reload_arc.archive) != 1 \
                    or reload_arc.archive[0]["item"]["id"] != old_done["id"]:
                problems.append("归档没有落盘")
            if [item["id"] for item in reload_arc.items] != left_ids:
                problems.append("归档后的活动列表没有落盘")
            # 删除 → 进归档；撤销 → 从归档里拿回来
            victim = arc.items[0]
            record = arc.pop(victim["id"])
            if record is None or arc.archive_stats()[0] != 2:
                problems.append("删除没有把条目送进归档（数据会丢）")
            if not arc.restore(record):
                problems.append("撤销删除失败")
            if arc.archive_stats()[0] != 1 or victim["id"] not in [i["id"] for i in arc.items]:
                problems.append("撤销删除后归档里还留着那条")
            # 「归档已完成」= 归档，不是真删
            arc.clear_done()
            total, counts = arc.archive_stats()
            if counts.get("cleared") != 1:
                problems.append("「归档已完成」没有按 cleared 记进归档: %r" % (counts,))
            if any(item["state"] == STATE_DONE for item in arc.items):
                problems.append("「归档已完成」没有把已完成的挪走")
            for index in range(ARCHIVE_MAX + 20):
                arc._archive_item({"id": 900000 + index, "text": "x", "done": True,
                                   "state": STATE_DONE}, "done")
            if len(arc.archive) != ARCHIVE_MAX:
                problems.append("归档没有限制在 %d 条: %r" % (ARCHIVE_MAX, len(arc.archive)))
            # done_at：勾选时记、取消时清；老数据读取时补
            dat = Store(os.path.join(temp_dir, "done-at.json"))
            dat_item = dat.add("看 done_at")
            if dat_item.get("done_at") is not None:
                problems.append("新条目的 done_at 应当是空的")
            dat.toggle(dat_item["id"])
            if not dat_item.get("done_at"):
                problems.append("勾成已完成时没有记 done_at")
            dat.toggle(dat_item["id"])
            if dat_item.get("done_at") is not None:
                problems.append("取消完成后没有清掉 done_at")
            legacy_path = os.path.join(temp_dir, "legacy-done.json")
            legacy = Store(legacy_path)
            legacy.items.append({"id": 1, "text": "老数据已完成", "done": True,
                                 "state": STATE_DONE, "priority": PRIORITY_NONE,
                                 "today": False, "created": time.time()})
            legacy.save()
            if not Store(legacy_path).items[0].get("done_at"):
                problems.append("老数据里已完成的条目读取时要补 done_at，否则一升级就被归档")
            # 界面：定时器在跑；跨天那一眼真的把行收走；删除可撤销
            if hasattr(widget, "_archive_job") or hasattr(widget, "_archive_timer_tick"):
                problems.append("不该再有周期性的归档定时器（只在启动时看一次）")
            if widget._archive_tick() != 0:
                problems.append("同一天里再叫一次归档应当什么都不做（一天最多一次）")
            widget.store.items.insert(0, {"id": 424242, "text": "昨天完成的界面条",
                                          "steps": [], "done": True, "state": STATE_DONE,
                                          "priority": PRIORITY_NONE, "today": False,
                                          "done_at": Store.day_start() - 3600,
                                          "created": time.time()})
            widget._archived_day = None
            moved_ui = widget._archive_tick()
            if moved_ui != 1 or widget._row_widget(424242) is not None:
                problems.append("跨天那一眼没有把已完成的行收走: %r" % (moved_ui,))
            if not any(rec["item"]["id"] == 424242 for rec in widget.store.archive):
                problems.append("界面归档的那条没有进 archive")
            undo_victim = widget.store.items[0]
            widget.remove_item(undo_victim["id"])
            if widget._undo is None or "已删除" not in str(widget.hint.cget("text")):
                problems.append("删掉之后没有出现撤销提示")
            if str(widget.undo_btn.cget("text")) != "撤销删除":
                problems.append("撤销按钮文案不对: %r" % (widget.undo_btn.cget("text"),))
            widget.undo_delete()
            if undo_victim["id"] not in [i["id"] for i in widget.store.items]:
                problems.append("点撤销没有把删除的条目放回来")
            if any(rec["item"]["id"] == undo_victim["id"] for rec in widget.store.archive):
                problems.append("撤销之后归档里还留着那条")

            # ---- 尺寸：默认排版不能变，拉大后要跟着重排 ----
            default_w = _scaled(PANEL_W, widget.s)
            default_h = _scaled(LIST_MAX_H, widget.s)
            if widget.panel_w != default_w:
                problems.append("默认面板宽度不对: %r（期望 %r）" % (widget.panel_w, default_w))
            if widget.list_max_h != default_h:
                problems.append("默认列表高度上限不对: %r（期望 %r）"
                                % (widget.list_max_h, default_h))
            narrow_wrap = widget.row_wrap
            widget._apply_metrics(default_w + 200)
            if widget.panel_w != default_w + 200:
                problems.append("拉宽后没有按新宽度重算: %r" % (widget.panel_w,))
            if widget.row_wrap <= narrow_wrap:
                problems.append("拉宽后换行宽度没有跟着变大: %r → %r"
                                % (narrow_wrap, widget.row_wrap))
            widget._apply_metrics()
            if widget.panel_w != default_w or widget.row_wrap != narrow_wrap:
                problems.append("还原默认尺寸失败: %r / %r"
                                % (widget.panel_w, widget.row_wrap))
            if widget._list_space(default_h * 3) <= default_h:
                problems.append("窗口拉高后没有把多出来的高度给列表")

            # ---- 默认排序：未完成 → 等待中 → 已完成 ----
            for entry in list(broken.items):          # 先把状态归一，免得受前面用例影响
                if broken.state_of(entry) == STATE_WAITING:
                    broken.toggle_waiting(entry["id"])
                elif broken.state_of(entry) == STATE_DONE:
                    broken.toggle(entry["id"])
            widget._refresh()
            widget._cancel_regroup()                  # 先清干净，免得受前面用例遗留的任务影响
            first_todo = broken.items[0]
            order_before = [entry["id"] for entry in broken.items]
            widget.toggle_item(first_todo["id"])
            if [entry["id"] for entry in broken.items] != order_before:
                problems.append("勾选后立刻就把行挪走了（双击改名会因此落空）")
            if widget._regroup_job is None:
                problems.append("勾选后没有安排归位")
            widget._regroup_now()
            if broken.state_of(broken.items[-1]) != STATE_DONE:
                problems.append("归位后已完成条目不在最后: %r"
                                % ([broken.state_of(e) for e in broken.items],))
            if widget._regroup_job is not None:
                problems.append("归位之后还留着待执行任务")
            # 拖拽不跨段：落点被夹在本状态段内
            widget._refresh()
            todo_items = [e for e in broken.items if broken.state_of(e) == STATE_TODO]
            done_indexes = [i for i, e in enumerate(broken.items)
                            if broken.state_of(e) == STATE_DONE]
            if todo_items and done_indexes:
                if widget._clamp_to_group(todo_items[0]["id"], 999) >= done_indexes[0]:
                    problems.append("拖拽落点没有被限制在本状态段内")
                if widget._clamp_to_group(todo_items[0]["id"], -5) != 0:
                    problems.append("拖拽落点下界没有被夹住")
                # 真拖一次：往「已完成」那一段里拖，不能跨过去（跨段只能靠勾选/小钟）
                dragged = todo_items[0]["id"]
                done_row = widget._row_widget(broken.items[done_indexes[0]]["id"])
                widget._begin_drag(dragged)
                widget._drag_to(done_row.winfo_rooty() + 1)
                widget._end_drag()
                after_items = list(broken.items)
                dragged_at = [i for i, e in enumerate(after_items) if e["id"] == dragged]
                done_first = [i for i, e in enumerate(after_items)
                              if broken.state_of(e) == STATE_DONE]
                if dragged_at and done_first and dragged_at[0] >= done_first[0]:
                    problems.append("拖拽把条目拖进了别的状态段: %r"
                                    % ([e["text"] for e in after_items],))
            # 编辑期间不归位（归位会重建行、把输入框一起销毁）
            edit_target = [e for e in broken.items if broken.state_of(e) == STATE_TODO]
            if edit_target:
                edit_id = edit_target[0]["id"]
                widget.toggle_item(edit_id)           # 变成已完成，安排归位
                widget._begin_edit(edit_id, _StubEvent(x=body_x))
                if widget._regroup_job is not None:
                    problems.append("进入编辑时没有取消失效的归位任务")
                widget._regroup_now()                 # 编辑中触发：应顺延，不该重画
                if widget._edit_entry is None:
                    problems.append("编辑中触发归位把输入框销毁了")
                if widget._regroup_job is None:
                    problems.append("编辑中触发归位后没有顺延")
                widget._end_edit(save=False)
            root.update()
            root.withdraw()
            root.update()
            root.destroy()
        except Exception as error:      # 不限于 TclError：代码里的笔误也要在这里暴露
            import traceback
            problems.append("界面/拖拽自检失败: %r\n%s"
                            % (error, traceback.format_exc()))
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    if problems:
        for problem in problems:
            sys.stderr.write("自检失败: %s\n" % problem)
        return 1
    print("SELFTEST OK")
    return 0


# --------------------------------------------------------------------------- #
# 诊断（--doctor）
#
# 用途：开发机（Windows）写代码、目标机（统信 UOS）跑程序时，把「必须盯着桌面看
# 一眼」才能确认的事，压成一段可以复制粘贴回来的文本。每个能力都尽量「写进去、
# 再读回来」验证，而不是「调用没报错就算成功」。
# 只读：不改数据、不写配置；只额外建一个隐藏窗口做探测。
# --------------------------------------------------------------------------- #
def _x11_read_property(x11, display, window, atom, length=8):
    """读回一个 32 位 X 属性，返回整数列表；读不到返回 []。"""
    actual_type = ctypes.c_ulong()
    actual_format = ctypes.c_int()
    nitems = ctypes.c_ulong()
    bytes_after = ctypes.c_ulong()
    prop = ctypes.POINTER(ctypes.c_ulong)()
    try:
        status = x11.XGetWindowProperty(
            ctypes.c_void_p(display), ctypes.c_ulong(window), ctypes.c_ulong(atom),
            0, length, 0, 0,
            ctypes.byref(actual_type), ctypes.byref(actual_format),
            ctypes.byref(nitems), ctypes.byref(bytes_after), ctypes.byref(prop))
    except Exception:
        return []
    if status != 0 or not prop:
        return []
    values = []
    if nitems.value:
        values = [int(prop[index]) for index in range(int(nitems.value))]
    try:
        x11.XFree(prop)
    except Exception:
        pass
    return values


def _doctor_x11(root, rows, caps):
    """X11 侧探测：把 Motif / EWMH 属性真的写进去，再读回来确认。"""
    seen = {}

    def probe_motif(x11, display, target, root_window):
        atom = x11.XInternAtom(ctypes.c_void_p(display), b"_MOTIF_WM_HINTS", 0)
        values = (ctypes.c_ulong * 5)(2, 0, 0, 0, 0)
        x11.XChangeProperty(ctypes.c_void_p(display), ctypes.c_ulong(target),
                            ctypes.c_ulong(atom), ctypes.c_ulong(atom), 32, 0,
                            ctypes.byref(values), 5)
        seen["target"] = int(target)
        seen["motif"] = _x11_read_property(x11, display, target, atom)

    def probe_state(x11, display, target, root_window):
        state_atom = x11.XInternAtom(ctypes.c_void_p(display), b"_NET_WM_STATE", 0)
        skip_taskbar = x11.XInternAtom(ctypes.c_void_p(display),
                                       b"_NET_WM_STATE_SKIP_TASKBAR", 0)
        skip_pager = x11.XInternAtom(ctypes.c_void_p(display),
                                     b"_NET_WM_STATE_SKIP_PAGER", 0)
        atoms = (ctypes.c_ulong * 2)(skip_taskbar, skip_pager)
        x11.XChangeProperty(ctypes.c_void_p(display), ctypes.c_ulong(target),
                            ctypes.c_ulong(state_atom), ctypes.c_ulong(4), 32, 0,
                            ctypes.byref(atoms), 2)
        seen["skip_wanted"] = [int(skip_taskbar), int(skip_pager)]
        seen["skip_read"] = _x11_read_property(x11, display, target, state_atom)

    if not _on_wm_window(root, probe_motif):
        rows.append("[x11]   连接不可用：libX11.so.6 缺失 / DISPLAY 无效 / 窗口还没建出来")
        caps.append(("去标题栏（Motif）", "N/A", "没有可用的 X 连接"))
        caps.append(("不进任务栏（EWMH）", "N/A", "没有可用的 X 连接"))
        return

    rows.append("[x11]   WM 顶层窗口 = 0x%x" % seen.get("target", 0))
    motif = seen.get("motif") or []
    if len(motif) >= 5 and motif[0] == 2 and motif[2] == 0:
        rows.append("[x11]   _MOTIF_WM_HINTS 回读 = %r（decorations=0）" % (motif,))
        caps.append(("去标题栏（Motif）", "OK", "回读确认 decorations=0"))
    else:
        rows.append("[x11]   _MOTIF_WM_HINTS 回读 = %r（空或不符合预期）" % (motif,))
        caps.append(("去标题栏（Motif）", "WARN", "回读 %r" % (motif,)))

    _on_wm_window(root, probe_state)
    wanted = seen.get("skip_wanted") or []
    got = seen.get("skip_read") or []
    if wanted and all(atom in got for atom in wanted):
        rows.append("[x11]   _NET_WM_STATE 回读含 SKIP_TASKBAR + SKIP_PAGER")
        caps.append(("不进任务栏（EWMH）", "OK", "SKIP_TASKBAR/SKIP_PAGER 已写入"))
    else:
        rows.append("[x11]   _NET_WM_STATE 回读 = %r（期望包含 %r）" % (got, wanted))
        caps.append(("不进任务栏（EWMH）", "WARN", "回读 %r" % (got,)))

    rows.append("[x11]   映射后 ClientMessage 补发 = %s" % set_skip_taskbar(root))
    filehandler = hasattr(root.tk, "createfilehandler")
    rows.append("[x11]   tk.createfilehandler 可用 = %s" % filehandler)
    caps.append(("单实例唤回（filehandler）", "OK" if filehandler else "WARN",
                 "unix 套接字 + Tk 文件事件"))


def _doctor_win32(root, rows, caps):
    """Win32 侧探测：在隐藏窗口上真的去边框、加 TOOLWINDOW，再读回样式确认。"""
    before = win32_window_state(root)
    if not before["hwnd"]:
        rows.append("[win]   取不到顶层窗口句柄（GetAncestor 失败）")
        caps.append(("去标题栏（Win32）", "N/A", "没有 HWND"))
        caps.append(("不进任务栏（TOOLWINDOW）", "N/A", "没有 HWND"))
        return

    rows.append("[win]   顶层窗口 = 0x%x，style=0x%08x，exstyle=0x%08x"
                % (before["hwnd"], before["style"], before["exstyle"]))
    rows.append("[win]   改之前 caption=%s thickframe=%s toolwindow=%s"
                % (before["caption"], before["thickframe"], before["toolwindow"]))
    applied = win32_make_borderless(root)
    root.update()
    after = win32_window_state(root)
    rows.append("[win]   改之后 caption=%s thickframe=%s toolwindow=%s（Win32 调用成功=%s）"
                % (after["caption"], after["thickframe"], after["toolwindow"], applied))

    removed = applied and not after["caption"] and not after["thickframe"]
    caps.append(("去标题栏（Win32）", "OK" if removed else "FAIL",
                 "WS_CAPTION/WS_THICKFRAME 已清除"))
    caps.append(("不进任务栏（TOOLWINDOW）", "OK" if after["toolwindow"] else "FAIL",
                 "WS_EX_TOOLWINDOW"))

    found = 0
    api = _win_api()
    if api is not None:
        found = int(api[0].FindWindowW(None, root.title()) or 0)
    rows.append("[win]   FindWindowW(标题) = 0x%x（命中顶层窗口=%s）"
                % (found, found == before["hwnd"]))
    caps.append(("唤回（按标题找窗口）", "OK" if found == before["hwnd"] else "WARN",
                 "FindWindowW + SetForegroundWindow"))
    rows.append("[win]   SetForegroundWindow = %s（窗口隐藏时仅供参考）"
                % win32_activate(root))

    try:
        dpi = ctypes.WinDLL("user32", use_last_error=True).GetDpiForSystem()
    except Exception:
        dpi = 0
    rows.append("[win]   系统 DPI = %s，DPI 感知 API 可用 = %s（正式版在 Tk() 之前调用）"
                % (dpi, _win_has_dpi_api()))


def _doctor_instance(rows, caps):
    """单实例与唤回机制的探测：Linux 看 unix 套接字，Windows 看命名互斥体。"""
    path = ipc_socket_path()
    if os.name == "nt":
        handle, exists = win32_instance_mutex("%s-instance" % APP_ID)
        rows.append("[ipc]   命名互斥体 %s-instance：%s"
                    % (APP_ID, "已存在（实例在跑）" if exists else "首次创建"))
        api = _win_api()
        if handle and api is not None:
            api[1].CloseHandle(handle)
        caps.append(("单实例", "OK",
                     "已有实例在跑（183）" if exists else "首次创建，当前无实例"))
        rows.append("[ipc]   唤回方式 = 第二个进程 FindWindowW + SetForegroundWindow")
        return

    rows.append("[ipc]   套接字路径 = %s（已存在=%s）" % (path, os.path.exists(path)))
    probe = path + ".doctor"
    try:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        if os.path.exists(probe):
            os.unlink(probe)
        server.bind(probe)
        server.listen(1)
        server.close()
        os.unlink(probe)
        caps.append(("单实例", "OK", "AF_UNIX 可绑定"))
    except Exception as error:
        caps.append(("单实例", "FAIL", repr(error)))


def run_doctor():
    """打印一份可粘贴的诊断报告（只读，不改数据/配置）。"""
    rows, caps = [], []

    def row(section, key, value):
        rows.append("[%s] %s = %s" % (section, key, value))

    # ---- 环境与路径 ----
    row("env", "os / sys.platform", "%s / %s" % (os.name, sys.platform))
    row("env", "窗口后端", get_backend().name)
    row("env", "托盘图标", "Shell_NotifyIcon 可用（左键唤回 / 右键菜单）"
        if win32_has_tray_api() else "当前平台未实现（Linux 用全局快捷键 / 再运行一次唤回）")
    row("env", "python", sys.version.split()[0])
    row("env", "脚本路径", os.path.abspath(__file__))
    row("env", "tk", "Tk %s" % tk.TkVersion if tk else "缺失（统信 UOS: apt install python3-tk）")
    row("env", "DISPLAY", os.environ.get("DISPLAY") or "(未设置)")
    row("env", "WAYLAND_DISPLAY", os.environ.get("WAYLAND_DISPLAY") or "(未设置)")
    row("env", "XDG_DATA_HOME", os.environ.get("XDG_DATA_HOME") or "(未设置)")
    row("env", "XDG_RUNTIME_DIR", os.environ.get("XDG_RUNTIME_DIR") or "(未设置)")
    row("path", "数据文件", data_file())
    if os.name == "nt":
        row("path", "单实例机制", "命名互斥体 %s-instance（Windows 没有 AF_UNIX）" % APP_ID)
    else:
        row("path", "单实例套接字", ipc_socket_path())
    row("path", "开机自启", autostart_path())
    if os.name != "nt":
        app_entry = get_backend().app_entry_target()
        row("path", "应用菜单条目", "%s（%s）" % (
            app_entry, "已存在" if os.path.exists(app_entry) else "不存在"))

    # ---- 数据文件（只读）----
    path = data_file()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            items = payload.get("items") or []

            def _state_of(entry):
                value = str(entry.get("state") or "")
                if value in (STATE_TODO, STATE_WAITING, STATE_DONE):
                    return value
                return STATE_DONE if entry.get("done") else STATE_TODO

            done = len([item for item in items if _state_of(item) == STATE_DONE])
            waiting = len([item for item in items if _state_of(item) == STATE_WAITING])
            row("data", "条目", "%d 条（未完成 %d / 等待中 %d / 已完成 %d）"
                % (len(items), len(items) - done - waiting, waiting, done))
            caps.append(("数据文件可读", "OK", "%d 条" % len(items)))
        except Exception as error:
            row("data", "读取", "失败：%r" % (error,))
            caps.append(("数据文件可读", "FAIL", repr(error)))
    else:
        row("data", "条目", "文件不存在（首次运行会创建）")
        caps.append(("数据文件可读", "N/A", "还没有数据文件"))

    directory = data_dir()
    if os.path.isdir(directory):
        caps.append(("数据目录可写", "OK" if os.access(directory, os.W_OK) else "FAIL",
                     directory))
    else:
        parent = os.path.dirname(directory)
        caps.append(("数据目录可写", "OK" if os.access(parent, os.W_OK) else "WARN",
                     "未建，父目录 %s" % parent))

    # ---- 单实例 / 唤回 ----
    _doctor_instance(rows, caps)

    # ---- 开机自启：Deepin/UOS 上两个文件必须配套，否则 startdde 会删掉自启项 ----
    if os.name != "nt" and getattr(get_backend(), "app_entry_target", None) is not None:
        autostart = autostart_path()
        app_entry = get_backend().app_entry_target()
        has_auto = os.path.exists(autostart)
        has_app = os.path.exists(app_entry)
        if has_auto and has_app:
            caps.append(("开机自启", "OK", "自启项 + 应用条目都在"))
        elif has_auto and not has_app:
            caps.append(("开机自启", "WARN",
                         "缺应用条目，Deepin/UOS 的 startdde 会把自启项删掉"))
        elif has_app and not has_auto:
            caps.append(("开机自启", "WARN", "有应用条目但还没装自启项"))
        else:
            caps.append(("开机自启", "N/A", "尚未安装（--install-autostart）"))

    # ---- 窗口能力（需要一个隐藏窗口）----
    root = None
    if tk is None:
        row("tk", "tkinter", "缺失：界面与窗口能力检查全部跳过")
        caps.append(("Tk 窗口", "FAIL",
                     "缺少 tkinter（统信 UOS / Debian: sudo apt install python3-tk）"))
        caps.append(("去标题栏", "N/A", "缺少 tkinter"))
        caps.append(("不进任务栏", "N/A", "缺少 tkinter"))
        caps.append(("中文字体", "N/A", "缺少 tkinter"))
    else:
        try:
            root = tk.Tk()
            root.title("desktop-todo doctor")
            root.withdraw()
            root.update_idletasks()
        except Exception as error:
            row("tk", "创建隐藏窗口", "失败：%r" % (error,))
            caps.append(("Tk 窗口", "FAIL", repr(error)))

    if root is not None:
        caps.append(("Tk 窗口", "OK", "Tk %s" % tk.TkVersion))
        try:
            scaling = float(root.tk.call("tk", "scaling"))
            factor = ui_scale(root)
            row("scale", "tk scaling", "%.3f" % scaling)
            row("scale", "界面缩放", "%.2f 倍（面板宽 %d px）"
                % (factor, _scaled(PANEL_W, factor)))
        except Exception:
            pass
        try:
            if os.name == "nt":
                _doctor_win32(root, rows, caps)
            else:
                _doctor_x11(root, rows, caps)
        except Exception as error:
            row("probe", "窗口能力探测", "异常：%r" % (error,))
        # ---- 全局快捷键：真去抓一次，再解开（抓不上通常是键被别的程序/已有实例占了）----
        hotkey_spec = HOTKEY_DEFAULT
        try:
            hotkey_spec = Store(data_file()).ui.get("hotkey") or HOTKEY_DEFAULT
        except Exception:
            pass
        backend = get_backend()
        installed = False
        try:
            installed = backend.hotkey_install(root, hotkey_spec, lambda: None)
        except Exception as error:
            row("hotkey", "注册", "异常：%r" % (error,))
        finally:
            try:
                backend.hotkey_remove()
            except Exception:
                pass
        if os.name == "nt":
            row("hotkey", "全局快捷键", "%s（Windows 后端未实现，靠托盘唤回）"
                % format_hotkey(hotkey_spec))
            caps.append(("全局快捷键", "N/A", "Windows 走托盘"))
        else:
            row("hotkey", "全局快捷键", "%s（%s）" % (
                format_hotkey(hotkey_spec),
                "注册成功" if installed else
                "没注册上：要么已经有实例在跑（它正占着这个键），要么被别的程序占了；"
                "换一个用 --hotkey Ctrl+Alt+K"))
            caps.append(("全局快捷键", "OK" if installed else "WARN",
                         format_hotkey(hotkey_spec)))
        try:
            families = set(tkfont.families())
            chosen = None
            for name in FONT_STACK:
                if name in families:
                    chosen = name
                    break
            missing = [name for name in FONT_STACK if name not in families]
            row("font", "命中", chosen or "(都没命中，用 Tk 默认字体)")
            row("font", "未命中", ", ".join(missing) or "无")
            caps.append(("中文字体", "OK" if chosen else "WARN", chosen or "回落 Tk 默认"))
        except Exception as error:
            row("font", "枚举", "失败：%r" % (error,))
            caps.append(("中文字体", "FAIL", repr(error)))
        try:
            root.destroy()
        except Exception:
            pass

    # ---- 报告 ----
    print("=" * 68)
    print("desktop-todo 诊断报告（--doctor）")
    print("=" * 68)
    for line in rows:
        print(line)
    print("-" * 68)
    counts = {"OK": 0, "WARN": 0, "FAIL": 0, "N/A": 0}
    for name, verdict, detail in caps:
        counts[verdict] = counts.get(verdict, 0) + 1
        print("%-4s %-26s %s" % (verdict, name, detail))
    print("-" * 68)
    print("结论：OK %d / WARN %d / FAIL %d / N/A %d"
          % (counts["OK"], counts["WARN"], counts["FAIL"], counts["N/A"]))
    print("DOCTOR DONE")
    return 0


def autostart_path():
    """开机自启的落点（Linux: .desktop 文件；Windows: 注册表 Run 键）。"""
    return get_backend().autostart_target()


def install_autostart():
    return get_backend().autostart_install()


def uninstall_autostart():
    return get_backend().autostart_uninstall()


def main(argv=None):
    parser = argparse.ArgumentParser(description="桌面待办清单挂件")
    parser.add_argument("--check", action="store_true", help="自检数据读写与界面构造后退出")
    parser.add_argument("--doctor", action="store_true", help="打印环境/窗口能力诊断报告后退出")
    parser.add_argument("--install-autostart", action="store_true", help="写入 ~/.config/autostart 开机自启")
    parser.add_argument("--uninstall-autostart", action="store_true", help="移除开机自启")
    parser.add_argument("--data-file", action="store_true", help="打印数据文件路径后退出")
    parser.add_argument("--hotkey", metavar="键位", help="设置全局唤回快捷键后退出（如 Super+T、Ctrl+Alt+K）")
    args = parser.parse_args(argv)

    if args.check:
        return run_check()
    if args.doctor:
        return run_doctor()
    if args.install_autostart:
        return install_autostart()
    if args.uninstall_autostart:
        return uninstall_autostart()
    if args.data_file:
        print(data_file())
        return 0
    if args.hotkey:
        if parse_hotkey(args.hotkey) is None:
            sys.stderr.write("认不出这个快捷键: %s（写成 Super+T / Ctrl+Alt+K / F5 这样）\n"
                             % args.hotkey)
            return 2
        store = Store(data_file())
        store.ui["hotkey"] = args.hotkey
        store.save()
        if os.name == "nt":
            print("全局唤回快捷键 = %s（Windows 后端还没实现全局快捷键，这里只是把这个值记下来；"
                  "Windows 上用托盘图标唤回）" % format_hotkey(args.hotkey))
        else:
            print("全局唤回快捷键 = %s（下次启动生效；冲突了可以换一个）"
                  % format_hotkey(args.hotkey))
        return 0

    if tk is None:
        sys.stderr.write("缺少 tkinter，无法启动界面（统信 UOS / Debian: "
                         "sudo apt install python3-tk）\n")
        return 1

    backend = get_backend()
    backend.prepare_process()      # Windows 的 DPI 感知必须在创建 Tk() 之前声明
    if backend.raise_running_instance():
        print("桌面待办清单已经在运行，已把它叫到最前面（任务栏里没有它，这是唤回方式）。")
        return 0

    # WM_CLASS 用 APP_ID，与 .desktop 的 StartupWMClass 对上（Deepin/UOS 靠它认应用）
    root = tk.Tk(className=APP_ID)
    widget = TodoWidget(root, Store(data_file()))
    if os.environ.get("DESKTOP_TODO_VERBOSE"):
        # 默认不输出；排障时设 DESKTOP_TODO_VERBOSE=1 就能看到托盘/快捷键挂上没有
        # （重定向到文件时 stdout 是块缓冲，所以每行都 flush）
        print("窗口后端 = %s" % get_backend().name, flush=True)
        print("托盘图标 = %s" % bool(getattr(widget, "_tray_on", False)), flush=True)
        print("全局快捷键 = %s（%s）" % (
            format_hotkey(getattr(widget, "_hotkey_spec", "")),
            "已注册" if getattr(widget, "_hotkey_on", False) else "没注册上"), flush=True)
        print("数据文件 = %s" % data_file(), flush=True)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
