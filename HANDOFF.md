# 交接文档：桌面待办清单（desktop-todo）

> 这是**会话交接**：某一段开发做了什么、为什么这么做、现在是什么状态、下一步注意什么。
> 目标读者是"下一个接手的人/AI 会话"（很可能是家里 Windows 那台，或这台工作机上的新会话）。
>
> - 仓库：<https://github.com/284022719/desktop-todo>（公开，分支 `main`，SSH 推送）
> - 覆盖时段：**2026-10-12**（Linux / Deepin 23 工作机这一侧）
> - 起点 `db95f6e`（Windows 托盘点击修正）→ **终点 `bfe34b6`**
> - 产物：单文件 `desktop_todo.py`（约 3400 行）+ `README.md` + 两张预览图；本会话 4 个文件、+1224 / −84 行

---

## 0. 一句话现状

Linux/Deepin 侧的功能这一轮补齐了：**三态（未完成 / 等待中 / 已完成）+ 默认分段排序 + 优先级旗子 + 「今天要办的」+ 子步骤 + 全局快捷键唤回 + 开机自启（含 startdde 的坑）+ 窗口可改大小并记住尺寸**。
Deepin 的托盘图标这条路**已确认走不通**（三条路都试到底，见 §3.1），改用一个全局快捷键解决"唤回"这个真实需求。

---

## 1. 项目与两台机器

| | 工作机（本文档所在） | 家里那台 |
| --- | --- | --- |
| 系统 | Deepin 23 / X11 / KWin，1920×1080 | Windows 11（Python 3.13） |
| 角色 | **目标平台**；平台专属行为在这里验 | Windows 专属行为在那里验；共享逻辑也可在那儿写 |
| 脚本 | `/home/haier/dsh/desktop-todo/desktop_todo.py`（另一个克隆在 Windows 上） | 同一份文件 |
| 数据 | `~/.local/share/desktop-todo/todos.json` | `%APPDATA%\desktop-todo\todos.json` |
| 自启 | `~/.config/autostart/desktop-todo.desktop` **＋** `~/.local/share/applications/desktop-todo.desktop`（见 §3.2） | `HKCU\...\Run`，用 `pythonw.exe` |
| 唤回 | 全局快捷键 `Super+T`（`--hotkey` 可改）／再运行一次／应用菜单 | 右下托盘图标（左键唤回、右键菜单） |
| 依赖 | Python 3 + tkinter + `libX11.so.6`（ctypes），零第三方包 | Python 3 + tkinter |

**开发与验证分工**（README 里也有同一张表）：共享逻辑哪台机器都行，靠 `--check` 兜底；
平台专属行为只能在对应平台验，靠 `--doctor` 回读结果。**每段工作结束 push，下次动手前 fetch。**

---

## 2. 本会话的提交（8 个，按时间）

| 提交 | 内容 | 关键点 |
| --- | --- | --- |
| `144a3d1` | 修双击改文字 | 勾选以前会整表重建行 → Tk 判定双击要求两次点击在同一控件上，**永远进不了编辑**。改成只重画那一行（`_draw_row` / `_redraw_row`），自检用"行控件对象同一性"卡住这条 |
| `ca30f61` | 开机自启 | Deepin 上只写 autostart 会被 `startdde` 删掉；要**同时**写 `~/.local/share/applications/<app-id>.desktop`（见 §3.2） |
| `412c90f` | 三态 + 全局快捷键 | `state: todo/waiting/done`（同时写 `done` 保持兼容）；`XGrabKey` 抓根窗口 + 120ms 轮询；自检新增多处覆盖 |
| `9c392a6` | doctor 措辞 | "快捷键没注册上"最常见的原因就是**自己的实例正占着它** |
| `da366bf` | README 截图 | 三态预览图 |
| `7dc4968` | 默认排序 | 未完成 → 等待中 → 已完成；稳定排序保留段内手拖顺序；**延迟 1.2 秒归位**（见 §3.3） |
| `a00ab4e` | Windows 开发分工 | 自检补快捷键解析用例；`--hotkey` 在 Windows 上说实话；README 新增分工表 |
| `bfe34b6` | 窗口可改大小 | 宽度自适应重排、拉高多显示、尺寸写进 `ui.width/height` |
| `348245b` | 交接文档 | 本文件 + README 里指向它的那一行 |
| `0997674` | 子步骤 | 主文字下逐行展开；**不占右侧图标区**（进度内联在正文行尾、编辑在右键菜单里） |
| `7c89c7e` | 优先级 + 「今天要办的」 | 右侧多两面小标记；排序键变成「段 → 今天 → 优先级 → 手动」；拖拽按"桶"夹住（见 §3.4） |

新增的主要代码：`X11Hotkey`（含 `parse_hotkey` / `format_hotkey`）、`Store.resort/toggle_waiting/state_of`、
`TodoWidget._apply_metrics/_list_space/_on_root_configure/_apply_resize/_apply_saved_size/_draw_wait_icon`、
`X11Backend.hotkey_*` / `app_entry_target` / `_desktop_entry`。

---

## 3. 重要结论（别再重复试）

### 3.1 Deepin 23（dde-dock 5.10.16）上，第三方托盘图标**显示不了**

三条路都试到底，证据都在提交说明/本会话记录里：

1. **自己实现 SNI（StatusNotifierItem，纯 dbus）**：注册成功、托盘管理器逐个读走 13 个属性，
   **dock 不画**。
2. **GTK 的 `GtkStatusIcon`（能显示的 `xfce4-notes` 用的同一个 API）**：图标窗口进了
   `com.deepin.dde.TrayManager` 的 `TrayIcons`，X 属性与 `xfce4-notes` **逐字段一致**
   （16×16、`IsViewable`、`_XEMBED_INFO=0x1,0x1`、`_NET_WM_PID` 都在），GTK 自己也报 `embedded=True`
   —— **dock 依然不画**，它从没为这个项建过 `XEmbedTrayWidget`。
   （踩坑：客户端必须有一个**真实窗口**，哪怕是个藏在屏幕外的小窗；只有裸图标窗口时 daemon 会
   Add 完立刻 remove。）
3. **dock 的"持有列表"**：dconf `[dde/dock] plugin-settings` → `tray.holded_window:<名字>=true`
   才是显示的必要条件（`xfce4-notes` 是 true、微信是 false）。往里写 `desktop-todo`、
   调 dock 的 `ReloadPlugins()`、甚至**重启整个 dock** —— 还是不画。

**结论**：这一版 dock 只画它自己认可的托盘项，第三方注册得再规范也没用。
所以 Linux 侧的"唤回"用**全局快捷键**实现（§2 `412c90f`）。如果哪天升到别的桌面环境，
可以再花几十行试试"GTK 小进程 + `GtkStatusIcon`"这条路（协议侧已证明可行）。

### 3.2 `startdde` 会删掉"解析不出应用 id"的自启项

- 现象：只写 `~/.config/autostart/desktop-todo.desktop`，**约 30 秒后文件消失**；
  journal 里只有一句 `startmanager.go: RemoveAutostart failed: failed to get app id`。
- 规矩：自启项的 app id（= 文件名）要能解析到**已安装的应用**，所以安装时同时写
  `~/.local/share/applications/desktop-todo.desktop`（带 `X-Deepin-AppID`、`StartupWMClass`），
  脚本的 `wm class` 也用 `desktop-todo`。
- 验证方式：**装完盯 30 秒以上**（`--doctor` 里也有一条"自启项 + 应用条目是否配套"的检查）。

### 3.3 Tk 的两个"顺序陷阱"

- **双击**：`<Double-Button-1>` 要求两次点击落在同一个控件上。所以任何"点一下就重建/挪走整行"
  的行为都会让双击改名失效。
- **默认排序**：勾选/标记等待后**不立刻**把行挪到别的段，而是 `REGROUP_DELAY_MS=1200` 之后
  再归位（拖动中、编辑中顺延）。想换段只能点勾选框或小钟；拖拽落点被 `_clamp_to_group()`
  夹在本段内。这几条都有自检用例，别顺手"优化"掉。

### 3.5 子步骤：为什么**没有**放在右侧图标区

- 右侧那五个按钮每个都吃行宽（`del 30 + wait 30 + today 26 + prio 26`，再加一个 steps 30），
  默认 300 宽时正文只剩 ~90px。所以子步骤没在图标区加格子：进度 `2/3` **内联在正文最后一行行尾**
  （这一行换行时让出它的宽度，自检有一条"进度小字不许压到右侧图标区"卡着），
  编辑入口放**右键条目的菜单**里（`编辑子步骤…` / `清空子步骤`）。
- **只有点小方框才勾选**，点步骤那一行的文字什么也不做 —— 否则会顺手把整条待办标成完成（自检卡这条）。
- 编辑器是多行框（**回车＝保存**、Ctrl+回车＝换行、点别处也算保存、Esc 取消），保存时按**行位置**保留原有勾选。
- 两个坑（都是这次踩的）：
  1. **UI 用例里别用 `toggle` 去"置为全完成"** —— 它是翻转语义，本来就勾着的那一步会被翻掉，
     结果变成 2/3 而不是 3/3，断言会以"颜色不对"的形式失败（我因此多花了一轮）。
  2. **检查要有区分度**：验"进度小字有没有预留宽度"时，测试正文必须**顶满一行**
     （我用 `font.measure` 按字宽算了一条），否则它压不到图标区，反例抓不到。
- 又一次证明"点击前先看指针下是哪个窗口"是必要的：这次盖住测试窗口的是 DSH 自己的 Chrome 窗口，
  指针检查拦住了那一下点击（没有点到别的东西上）。

### 3.6 右侧图标区从五个格子收到两个（2026-10-14 YanH 定）

- 现在**只有 `≡`/`2/3`（子步骤）与 `×`（删除）**。小钟/今/旗三个格子删掉，动作搬进**右键条目的菜单**
  （等待中 / 今天要办的 / 优先级子菜单 / 编辑子步骤 / 清空子步骤 / 删除这条）。
- **状态改用零宽度的画法**（不再吃正文宽度）：等待中＝整行琥珀文字；今天＝正文前的蓝点；
  优先级＝行最左边一条橙竖条（高＝整行、中＝中间半行）。
- 教训一：**断言要覆盖"尺寸表"，不能只看"画出来的图元"**。我最初只断言 `canvas._markers` 的键，
  把 `("wait", …)` 加回 `self.markers` 时反例溜过去了 —— 因为画的还是两个，但点击区与折行宽度已经算错。
  现在同时断言 `widget.markers` 表 + 三个老格子位置上的 `_zone_at()` 判定。
- 教训二：`tk.Menu` 的**分隔线没有 `-label` 选项**，`entrycget(i, "label")` 会抛 TclError；
  遍历菜单项要先 try/except（自检里抽了个 `menu_label()`）。
- 教训三：新画的图元要用到 `row_h` 时，**必须放在 `row_h` 算完之后** —— 我先把它插在函数开头，
  自检立刻报 `UnboundLocalError: row_h`（这条是自检抓到的，不是人眼）。
- 反面：进度一度**同时**显示在格子（`1/2`）和正文行尾（内联小字）。既然要了格子，行尾那份已删，
  正文换行宽度也回到满宽。

### 3.8 情境标签 @… 与顶部筛选（2026-10-14）

- 数据：条目加 `tags`（最多 3 个、每个 8 字）；`add` / `rename` 都走 `Store.parse_tags()`，
  正则 `(?:^|\s)@([\w\u4e00-\u9fff]{1,8})(?=\s|$)` —— **两头都要边界**，所以邮箱和网址不会被吃掉；
  重复标签去掉、超出的**留在正文里**（不悄悄吃字）。
- 显示：行尾小蓝字（`_tag_item`），换行时用 `row_wrap - tag_w` 让出宽度 —— **不占右侧图标区**（同子步骤的教训）。
- 筛选：`self.filter_tag`（**只活在本次会话**，不写进 `ui`）＋顶部 chip 条（`_refresh_tags` 重建，没标签时是空框）。
- 坑一：`TAG_RE` 是模块级常量，我却写成 `cls.TAG_RE` → `AttributeError`（自检立刻报）。
  坑二：`self.filter_tag` 忘了在 `__init__` 里初始化 → 构造窗口时就炸。
  坑三：用例收尾时按文字前缀 `startswith(("界面", "长长"))` 清条目，把**前面用例的固定条目**也删光了，
  导致后面的归档用例 `widget.store.items[0]` 越界 —— 收尾**只删自己造的那两条 id**。

### 3.7 归档：过 00:00 收走已完成的；删除先进归档（2026-10-14）

- 数据：条目加 `done_at`（勾成完成时记、取消时清）；`todos.json` 加 `archive` 段
  （`{item, archived_at, reason}`，reason ∈ `done` / `cleared` / `deleted`，最多 500 条）。
- **过 00:00 自动归档**：`archive_completed()` 只收 `state==done` 且 `done_at < 今天 00:00` 的；
  **只在启动时看一眼**（`_archive_on_start()` → `_archive_tick()`，`_archived_day` 保证一天最多一次）。
  2026-10-14 YanH 定：**不要周期性检查** —— 每分钟那次的成本其实近乎为零（一次 `localtime`+`mktime` 比较，
  跨天才动数据），但行为上东西会在眼前 00:00 消失，不如「第二天打开时收」可预期，也少一个活动部件。
  自检里有一条结构断言卡着：不许再出现 `_archive_job` / `_archive_timer_tick`。
- **删除 = 进归档**：`pop()` 先 `_archive_item(..., "deleted")`，`restore()` 再 `_unarchive()` ——
  所以删除不可能丢数据，底部 8 秒的「撤销删除」只是顺手。
- **「清除已完成」改名「归档已完成」**，行为也跟着从"真删"改成"归档"（`reason: cleared`）。
- 坑：`time.mktime()` 要 **9 元组**（年,月,日,时,分,秒,周几,一年第几天,夏令时）；
  我给了 8 个，自检立刻报 `TypeError: mktime(): illegal time tuple argument`。
  自检里那个构造 `TodoWidget` 的用例会走到 `day_start()`，所以这类错误跑 `--check` 就能撞出来。
- 未做（诚实标注）：**真实鼠标点「× → 撤销删除」这一次没验成** —— 测试窗口先被 DSH 的浏览器盖住、
  后来又被我一次误拖改成 592×182，坐标全错。逻辑由自检覆盖（含"删除必进归档""撤销后归档里不许留"），
  但**人眼那一下还欠着**。

### 3.4 排序/拖拽的"桶"，以及重画后的图元 id

- 排序键是 `(状态段, 是否今天, -优先级, 手动顺序)`：三段顺序不变，段内先「今天」再优先级，
  最后才是拖出来的顺序。点旗子/「今」之后**延迟归位**（和勾选同一个理由，见 §3.3）。
- 拖拽落点用 `_group_span()` **夹在同一个桶里**（桶 = 状态 + 是否今天 + 优先级）。
  想跨桶就点勾选框 / 小钟 / 今 / 旗子 —— 否则拖过去也会被下一次归位弹回来。
- **坑**：`_draw_row()` 开头是 `canvas.delete("all")`，所以**重画之后图元 id 全变了**
  （而且 Tk 会复用旧 id）。写自检断言时必须在每次重画之后**重新取** id，
  否则会读到别的图元、断言莫名其妙地失败。
- **坑**：行画布左边有 12px 内边距（`pad`），点击坐标 = `pad + 画布内坐标`。
  从截图量出标记位置再反推点击点时，别忘了加这 12px（这次就因此白点了几下）。

---

## 4. 当前机器状态（2026-10-12 写文档这一刻的快照）

- 挂件在跑：pid 见 `ps -eo pid,cmd | grep desktop_todo.py`；窗口 **362×512 @ (1569,15)**（尺寸被记住）。
- 数据：`todos.json` 12 条（未完成 10 / 等待中 1 / 已完成 1）—— 用户随时会改，别拿这个数字当断言。
- `--doctor`：`OK 9 / WARN 1 / FAIL 0`。**那条 WARN 是正常的**：挂件在跑，`Super+T` 被它自己占着，
  第二个进程自然抓不到。
- 自启：两个 `.desktop` 都在。
- 快捷键：`Super+T`（`ui.hotkey` 已记录）。
- 条目字段：`id / text / done / state / priority / today / steps / created`（`steps` = `[{text, done}]`，最多 20 条）
  （`priority`：0 无 / 1 中 / 2 高；`today`：布尔。老数据缺这两个字段按 0/False 处理）。
- 右侧按钮（从右往左）：`×`(30px) → 小钟(30) → 今(26) → 旗子(26)，只在 `self.markers` 一处定义，
  绘制 / 点击判定 / 折行宽度都从它推。
- 右侧图标区现在只有 `delete` 与 `steps` 两格（`self.markers` 表就是点击判定与折行宽度的唯一来源）\n- 关键常量（改布局/时常常用）：`PANEL_W=300`、`PAD=12`、`LIST_MAX_H=268`、`TEXT_X=36`、
  `DEL_W=30`、`WAIT_W=30`、`TODAY_W=26`、`PRIO_W=26`、`STEP_BOX=11`、`STEP_INDENT=20`、`STEP_MAX=20`、`ROW_PAD_Y=6`、
  `DOUBLE_CLICK_REVERT_S=0.8`、`REGROUP_DELAY_MS=1200`、`X11Hotkey.POLL_MS=120`、
  `TRAY_POLL_MS=40`（Windows）。

---

## 5. 怎么验（可复现的配方）

```bash
cd /home/haier/dsh/desktop-todo
python3 desktop_todo.py --check     # 跨平台自检：数据/界面/拖拽/排序/三态/快捷键解析。提交前必跑
python3 desktop_todo.py --doctor    # 环境与平台能力，每项都是"写进去再回读"
```

**隔离实例**（测试一律别碰真实数据 `/home/haier/.local/share/desktop-todo`）：

```bash
rm -rf /tmp/t && mkdir -p /tmp/t/data /tmp/t/run && chmod 700 /tmp/t/run
# 需要的话先写一份测试数据到 /tmp/t/data/desktop-todo/todos.json
XDG_DATA_HOME=/tmp/t/data XDG_RUNTIME_DIR=/tmp/t/run DESKTOP_TODO_VERBOSE=1 \
  setsid nohup python3 desktop_todo.py > /tmp/t/log 2>&1 &
xdotool search --class desktop-todo   # 会列出多个窗口：按位置/尺寸挑出测试实例，别用 head -1
```

**快捷键 A/B**（严格的证法：先让焦点在别的窗口上，再按键）：

```bash
other=$(xdotool getactivewindow)         # 或者随便点一个别的窗口
xdotool windowactivate "$other"; sleep 1
xdotool getwindowfocus                   # 负对照：应仍是 other
xdotool key super+t; sleep 1
xdotool getwindowfocus                   # 应变成挂件窗口
```

**改大小**：`xdotool windowsize <win> 480 620` → 截图看右侧图标是否移到新宽度边缘、
可见行数是否变多；`xdotool windowclose <win>` 再重启，看尺寸是否被记住。

**给自检加新用例之后，顺手做"反例测试"**：把实现改坏（比如让 `resort()` 不排序、
让 `_apply_metrics()` 忽略宽度），确认 `--check` 会失败、并打印出对得上的那句话。

---

## 6. 已知小瑕疵 / 明确没做的

1. 启动时窗口位置可能有 **±10px 漂移**（KWin 摆放 + 先设位置后套尺寸的顺序）。
2. 比设计尺寸更小的窗口**不做归一化**（"只增不减"是有意的）：想缩回 300px 宽得手动拉。
3. 折叠模式与改大小的交互**没专门验过**。
4. "拖拽不跨段"是**有意设计**（换段请点勾选框/小钟）。如果想要"拖进已完成区就标记完成"，
   那是一个新功能，不是 bug。
5. Linux 托盘：不做（§3.1）。
6. Windows 专属代码（`Shell_NotifyIcon` 托盘、DPI 声明、`pythonw`、`WS_EX_TOOLWINDOW`）
   本会话**一行没动**，但**自 10-11 之后没在 Windows 上重跑过**，回家建议先跑一遍
   `python desktop_todo.py --check` + `--doctor` 并点一次托盘确认。

---

## 7. 下一步建议（按优先级）

1. **回家在 Windows 上验一遍**：`--check`（共享逻辑）+ `--doctor`（窗口样式/DPI/托盘回读）+ 点托盘唤回。
   注意 Windows 侧**没有**全局快捷键（`--hotkey` 只会把值记下来）。
2. 若要继续打磨 Linux 侧（都在这一台机器能验）：
   - 折叠时的标题顺便显示「N 等待 / 今天 x/y」；
   - 情境标签（`@电话` / `@外出` / `@等人`）+ 顶部筛选：对"碎片时间能推进哪几条"最有用，
     是目前评估下来下一个最值得加的管理维度（其余方法多半需要日历或团队场景，不适合这个挂件）；
   - 「等待中」的筛选/一键折叠成一组；
   - 窗口位置漂移（§6.1）与"缩回设计尺寸"的支持。
3. 若换到别的桌面环境（比如 Deepin 25 / GNOME / KDE），可以重试托盘（§3.1 给了成本估计）。

---

## 8. 工作纪律（都是踩过的坑换来的）

- **提交前跑 `--check`**；给自检加了新用例就做反例测试，别写"永远为真"的断言。
- **不要动用户的真实数据**：测试一律用隔离的 `XDG_DATA_HOME` / `XDG_RUNTIME_DIR`。
- **合成点击前先确认指针下是哪个窗口**：
  `xdotool mousemove X Y; xdotool getmouselocation --shell`，看到 `WINDOW=` 是测试窗口再点。
  （本会话有一次误点：有别的窗口全屏盖着，点击落进了用户的聊天窗口。没造成改动，但很险。）
- **杀进程按进程名**（`ps -eo pid,comm | awk '$2=="python3"…'` 再 `kill`），
  别用 `pkill -f "python3 xxx.py"` —— 它会匹配到你自己那条 `bash -c`，把自己也杀了。
- 排障时用 `python3 -u` / `DESKTOP_TODO_VERBOSE=1`：重定向到文件时 stdout 是块缓冲，
  被缓冲掉的 traceback 会让人白查半天。
- 断言要有**真机证据**：像素 diff（`compare -metric AE`）、`xprop` 回读、真实点击、
  存档 JSON 对照。代码"看起来对"不算。
- 改完代码**程序要重启才生效**（自启指向脚本所在目录）；在 Linux 上重启还会顺手把
  默认排序写回数据文件。
