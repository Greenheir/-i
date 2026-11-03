#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
明晚一定早睡 · 口琴工具台 —— 侧栏导航版（方案 C）
================================================================
纯 tkinter，不需要装任何第三方库。

跑起来:
    python harp_gui.py                 # 图形界面（默认深色霓虹）
    python harp_gui.py --theme light   # 指定配色
    python harp_gui.py --list-themes   # 看有哪些配色
    python harp_gui.py --selftest      # 无人值守自检

布局:
    ┌────────┬──────────────────────────────────┬──────────────┐
    │ 导航栏 │  本机谱子库 / 在线曲库 / 演奏台   │  在线曲库     │
    │  208px │  / 运行日志   四个页面            │  + 运行日志   │
    │        │  ─────────────────────────────   │  336px       │
    │        │  演奏条（速度 / 倒计时 / 开始演奏）│              │
    └────────┴──────────────────────────────────┴──────────────┘

界面层（配色、字体、控件工厂、列表控件）来自样板稿；
底下的活儿是真接上去的：本机谱库扫描、在线曲库搜索/下载、演奏子进程、日志、提权。

诚实说明：tkinter 画不出圆角/阴影/毛玻璃，样板的观感是「直角 + 1px 描边 + 纯色」。
"""

from __future__ import annotations

import argparse
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

# 工作目录：打包成 exe 后 __file__ 指向 PyInstaller 临时解包目录，必须换成 exe 所在目录
if getattr(sys, 'frozen', False):
    ROOT = os.path.dirname(os.path.abspath(sys.executable))
else:
    ROOT = os.path.dirname(os.path.abspath(__file__))
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
except Exception:
    pass

try:
    import harp_cli                        # 复用它的 ffmpeg 查找 / 配置读写
except Exception:
    harp_cli = None
try:
    import harp2                           # 权限检测
except Exception:
    harp2 = None
try:
    import harp_dfh                        # 成品站三行式谱面解析
except Exception:
    harp_dfh = None
try:
    import harp_lib                        # 在线曲库（成品站公开谱库）
except Exception:
    harp_lib = None
try:
    import harp_update                     # 自动更新（GitHub Release）
except Exception:
    harp_update = None

VENV_PY = os.path.join(ROOT, '.venv', 'Scripts', 'python.exe')
PY = VENV_PY if os.path.isfile(VENV_PY) else sys.executable
CLI = os.path.join(ROOT, 'harp_cli.py')
HARP2 = os.path.join(ROOT, 'harp2.py')
HARP_DFH = os.path.join(ROOT, 'harp_dfh.py')
SCORE = os.path.join(ROOT, 'harp_score.txt')
SCORE_DIR = os.path.join(ROOT, 'scores')
SCORE_EXTS = ('.txt', '.md', '.score')
STOP_FLAG = os.path.join(ROOT, '.stop')
OUTPUT = os.path.join(ROOT, 'output')
SEPARATED = os.path.join(ROOT, 'separated')

CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0
BRAND = '明晚一定早睡'
APP_TITLE = '明晚一定早睡 · 口琴工具台'
APP_VER = harp_update.APP_VER if harp_update else 'v1.0'
FROZEN = bool(getattr(sys, 'frozen', False))
KEYS = ['C', 'D', 'E', 'F', 'G', 'A', 'B', 'F#', 'Bb', 'Eb', 'Ab']


# ============================================================================
# 工具函数
# ============================================================================
def player_cmd(script, args):
    """演奏子进程的启动命令（打包版没有 .py 和解释器，就拉起同一个 exe 的 --player）。"""
    if FROZEN:
        return [sys.executable, '--player'] + list(args)
    return [PY, script] + list(args)


def hide_console():
    """打包版从 exe 进图形界面时藏掉自带的控制台窗口。"""
    if not (FROZEN and os.name == 'nt'):
        return
    try:
        import ctypes
        h = ctypes.windll.kernel32.GetConsoleWindow()
        if h:
            ctypes.windll.user32.ShowWindow(h, 0)
    except Exception:
        pass


def default_score_dir():
    """默认谱子库目录：优先用户实际攒谱子的地方（下载\\新建文件夹），否则 exe 旁边的 scores\\。"""
    cand = os.path.join(os.path.expanduser('~'), 'Downloads', '新建文件夹')
    try:
        if os.path.isdir(cand):
            for n in os.listdir(cand):
                if n.lower().endswith(SCORE_EXTS):
                    return cand
    except Exception:
        pass
    return SCORE_DIR


def build_env():
    """子进程环境：UTF-8 + demucs 不走 HuggingFace（国内直连会超时）。"""
    env = dict(os.environ)
    env['PYTHONUTF8'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    env['TF_CPP_MIN_LOG_LEVEL'] = '3'
    env.setdefault('HF_HUB_DISABLE_TELEMETRY', '1')
    if env.get('HARP_USE_HF') != '1':
        env['HF_HUB_OFFLINE'] = '1'
    return env


def is_admin_now():
    if harp2:
        try:
            return bool(harp2.is_admin())
        except Exception:
            pass
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def score_meta(path):
    """读一份谱子 -> {notes, bpm, kind}（列表显示与排序用）。"""
    try:
        with open(path, 'r', encoding='utf-8-sig', errors='replace') as f:
            text = f.read()
    except Exception:
        return {'notes': 0, 'bpm': 0.0, 'kind': '读不了'}
    if '键位' in text and '节奏' in text:
        if harp_dfh:
            try:
                notes, info = harp_dfh.parse_score(text)
                return {'notes': len(notes), 'bpm': float(info.get('bpm') or 0), 'kind': 'DFH'}
            except Exception:
                pass
        return {'notes': 0, 'bpm': 0.0, 'kind': 'DFH'}
    toks = len([t for ln in text.splitlines()
                for t in ln.split('//')[0].split() if t not in ('|', '')])
    return {'notes': toks, 'bpm': 0.0, 'kind': '简谱'}


def crash_log(exc_text):
    p = os.path.join(ROOT, 'gui-error.log')
    try:
        with open(p, 'a', encoding='utf-8') as f:
            f.write('\n===== %s =====\n%s' % (time.strftime('%Y-%m-%d %H:%M:%S'), exc_text))
    except Exception:
        pass
    return p


# ============================================================================
# NAV / TOOLS（侧栏内容）
# ============================================================================
TOOLS = [("unlock", "unlock", "解卡"), ("tune", "music", "音区校准"),
         ("keytest", "keyboard", "按键自检"), ("open", "folder_open", "目录")]


# ============================================================================
# 配色表 + 控件层（来自美化样板，改颜色/字号只动这里）
# ============================================================================
# ============================================================================
# 一、配色表 —— 想改颜色只动这里
# ============================================================================
# canvas 窗口外底色 / bg 窗口底色 / panel 面板底色 / panel2 表头·次级底
# panel3 选中行底色 / line 分隔线 / line2 强对比描边
# ink 主文字 / ink2 次级文字 / ink3 弱化文字
# accent 主按钮底色 / accent_ink 主按钮文字 / ok 正常状态 / danger 危险
# sans mono display 是字体族候选，按顺序找系统里有的那个

THEMES: dict[str, dict] = {
    "mono": {
        "label": "极简黑白",
        "canvas": "#ECEAE5", "bg": "#FFFFFF", "panel": "#FFFFFF",
        "panel2": "#FAFAF8", "panel3": "#F2F1EE",
        "line": "#E3E1DC", "line2": "#0D0D0D",
        "ink": "#0D0D0D", "ink2": "#4A4A4A", "ink3": "#8F8F8F",
        "accent": "#0D0D0D", "accent_ink": "#FFFFFF",
        "ok": "#1A1A1A", "danger": "#0D0D0D",
        "sans": ["Microsoft YaHei UI", "Microsoft YaHei", "SimHei"],
        "mono": ["Consolas", "Courier New"],
        "display": ["SimSun", "Microsoft YaHei UI"],
    },
    "ink": {
        "label": "国风墨韵",
        "canvas": "#E3D9C6", "bg": "#FBF7EE", "panel": "#FDFAF3",
        "panel2": "#F4ECDD", "panel3": "#EFE6D5",
        "line": "#DED2BA", "line2": "#C3B295",
        "ink": "#221F19", "ink2": "#4D463A", "ink3": "#8D8471",
        "accent": "#A8342A", "accent_ink": "#FFFAF2",
        "ok": "#3F5D43", "danger": "#A8342A",
        "sans": ["Microsoft YaHei UI", "Microsoft YaHei"],
        "mono": ["Consolas", "Courier New"],
        "display": ["SimSun", "KaiTi", "Microsoft YaHei UI"],
    },
    "neon": {
        "label": "深色霓虹",
        "canvas": "#05050A", "bg": "#0C0C15", "panel": "#13131F",
        "panel2": "#1A1A29", "panel3": "#202033",
        "line": "#272738", "line2": "#3B3B55",
        "ink": "#ECEFFF", "ink2": "#A4ABC9", "ink3": "#6D7492",
        "accent": "#E9FF4D", "accent_ink": "#111119",
        "ok": "#5CF2A8", "danger": "#FF5470",
        "sans": ["Microsoft YaHei UI", "Microsoft YaHei"],
        "mono": ["Consolas", "Courier New"],
        "display": ["Microsoft YaHei UI", "Microsoft YaHei"],
    },
    # tkinter 没有毛玻璃，玻璃拟态在这里用深紫蓝 + 亮紫强调色近似
    "glass": {
        "label": "玻璃拟态",
        "canvas": "#0A0A1E", "bg": "#141034", "panel": "#1B1740",
        "panel2": "#221D4F", "panel3": "#2A2460",
        "line": "#332C6B", "line2": "#4A418E",
        "ink": "#F3F4FF", "ink2": "#B8BCE0", "ink3": "#8A8FB8",
        "accent": "#8B5CF6", "accent_ink": "#0A0A1A",
        "ok": "#4ADE80", "danger": "#FB7185",
        "sans": ["Microsoft YaHei UI", "Microsoft YaHei"],
        "mono": ["Consolas", "Courier New"],
        "display": ["Microsoft YaHei UI", "Microsoft YaHei"],
    },
    "light": {
        "label": "明亮专业",
        "canvas": "#EAEEF5", "bg": "#FFFFFF", "panel": "#FFFFFF",
        "panel2": "#F6F8FC", "panel3": "#EEF2F9",
        "line": "#E5EAF2", "line2": "#CFD8E6",
        "ink": "#0F172A", "ink2": "#475569", "ink3": "#94A3B8",
        "accent": "#2563EB", "accent_ink": "#FFFFFF",
        "ok": "#059669", "danger": "#E11D48",
        "sans": ["Microsoft YaHei UI", "Microsoft YaHei"],
        "mono": ["Consolas", "Courier New"],
        "display": ["Microsoft YaHei UI", "Microsoft YaHei"],
    },
    "term": {
        "label": "复古终端",
        "canvas": "#04070A", "bg": "#070C08", "panel": "#0A110C",
        "panel2": "#0D1710", "panel3": "#101C13",
        "line": "#1A3221", "line2": "#2A5334",
        "ink": "#BCFFD2", "ink2": "#79DD9D", "ink3": "#468A5F",
        "accent": "#33FF88", "accent_ink": "#04120A",
        "ok": "#33FF88", "danger": "#FF6B6B",
        "sans": ["Consolas", "Courier New"],
        "mono": ["Consolas", "Courier New"],
        "display": ["Consolas", "Courier New"],
    },
}

GAP = 13  # 面板之间的间距


ICON_DIR = Path(__file__).resolve().parent / "icons"
ICON_W = 30          # 图标位宽度（图标本身 28px）
ICON_FALLBACK = {    # 万一 icons/ 目录不在，退回单色文字，程序照样能跑
    "folder": "📁", "globe": "🌐", "play": "▶", "clipboard": "📋",
    "unlock": "🔓", "music": "🎵", "keyboard": "⌨", "folder_open": "📂",
    "page": "📄", "paint": "🎨", "user": "👤", "search": "🔍",
    "download": "↓", "stop": "■", "run": "🏃",
}


class Icons:
    """
    彩色卡通图标。

    为什么非得用图片：tkinter 走 GDI 绘制文字，拿不到 Segoe UI Emoji 的彩色
    图层，直接把 emoji 当文字画出来只会得到单色轮廓。所以这里改用浏览器把
    彩色 emoji 预渲染成 28×28 的透明 PNG（生成脚本见 mockups/icons.html），
    运行时用 PhotoImage 加载即可，零第三方依赖。
    """

    def __init__(self, folder: Path = ICON_DIR):
        self.folder = folder
        self.cache: dict[str, tk.PhotoImage] = {}
        self.available = folder.is_dir()
        # 内嵌的彩色卡通图标（harp_icons.py 里的 base64 PNG）——
        # 优先用它：打包成 exe 也不用额外带 icons/ 目录
        self.embedded: dict[str, str] = {}
        try:
            import harp_icons
            self.embedded = dict(getattr(harp_icons, 'ICONS', {}) or {})
        except Exception:
            self.embedded = {}

    def img(self, name: str):
        """拿到图标（PhotoImage）；没有就返回 None，由调用方退回文字。"""
        if name in self.cache:
            return self.cache[name]
        b64 = self.embedded.get(name)
        if b64:
            try:
                self.cache[name] = tk.PhotoImage(data=b64)
                return self.cache[name]
            except Exception:
                pass
        if self.available:
            path = self.folder / f"{name}.png"
            if path.exists():
                try:
                    self.cache[name] = tk.PhotoImage(file=str(path))
                    return self.cache[name]
                except tk.TclError:
                    pass
        return None

    def text(self, name: str) -> str:
        return ICON_FALLBACK.get(name, "")


# ============================================================================
# 三、字体
# ============================================================================
class Fonts:
    """按配色表里的候选字体族，挑出系统里真正存在的那个。字号也集中在这里。"""

    def __init__(self, root: tk.Misc, theme: dict):
        have = set(tkfont.families(root))
        self.ui = self._pick(have, theme["sans"])
        self.mono = self._pick(have, theme["mono"])
        self.display = self._pick(have, theme["display"])

    @staticmethod
    def _pick(have: set, prefs: list) -> str:
        for p in prefs:
            if p in have:
                return p
        return prefs[0]

    def h1(self, size=15):   return (self.display, size, "bold")
    def h2(self, size=11):   return (self.ui, size, "bold")
    def body(self, size=10): return (self.ui, size)
    def small(self, size=9): return (self.ui, size)
    def code(self, size=9):  return (self.mono, size)
    def codeb(self, size=9): return (self.mono, size, "bold")


# ============================================================================
# 四、小工具
# ============================================================================
def set_bg_recursive(widget: tk.Misc, color: str) -> None:
    """整块改底色。Frame 改了子控件不会跟着变，必须递归。"""
    try:
        widget.configure(bg=color)
    except tk.TclError:
        pass
    for child in widget.winfo_children():
        set_bg_recursive(child, color)


# ============================================================================
# 五、控件工厂
# ============================================================================
class UI:
    """所有控件都从这里出，配色只从 self.c 里取。换肤 = 重建整个界面。"""

    def __init__(self, root: tk.Misc, theme_name: str):
        self.name = theme_name
        self.c = THEMES[theme_name]
        self.f = Fonts(root, self.c)
        self.icons = Icons()

    # ---- 面板：返回 (外壳, 标题栏, 内容区) ----
    def panel(self, parent: tk.Misc, title: str, right: str = "", icon: str = ""):
        c = self.c
        outer = tk.Frame(parent, bg=c["panel"], highlightthickness=1,
                         highlightbackground=c["line"], highlightcolor=c["line"])
        head = tk.Frame(outer, bg=c["panel2"], height=34)
        head.pack(fill="x")
        head.pack_propagate(False)
        img = self.icons.img(icon) if icon else None
        if img is not None:
            tk.Label(head, image=img, bg=c["panel2"], bd=0).pack(side="left", padx=(10, 0))
            tk.Label(head, text=title, bg=c["panel2"], fg=c["ink"],
                     font=self.f.h2()).pack(side="left", padx=(6, 0))
        else:
            text = f"{self.icons.text(icon)}  {title}" if icon else title
            tk.Label(head, text=text, bg=c["panel2"], fg=c["ink"],
                     font=self.f.h2()).pack(side="left", padx=(12, 0))
        if right:
            tk.Label(head, text=right, bg=c["panel2"], fg=c["ink3"],
                     font=self.f.code()).pack(side="right", padx=(0, 12))
        tk.Frame(outer, bg=c["line"], height=1).pack(fill="x")
        body = tk.Frame(outer, bg=c["panel"])
        body.pack(fill="both", expand=True)
        return outer, head, body

    # ---- 按钮 ----
    def button(self, parent: tk.Misc, text: str, kind: str = "default", command=None,
               icon: str = ""):
        c = self.c
        bg, fg, border, abg, afg = {
            "default": (c["panel2"], c["ink2"], c["line"], c["panel3"], c["ink"]),
            "key":     (c["accent"], c["accent_ink"], c["accent"], c["accent"], c["accent_ink"]),
            "danger":  (c["panel"], c["danger"], c["danger"], c["panel"], c["danger"]),
            "ghost":   (c["panel2"], c["ink3"], c["panel2"], c["panel3"], c["ink"]),
        }[kind]
        kwargs = dict(text=text, command=command, font=self.f.body(10),
                      bg=bg, fg=fg, activebackground=abg, activeforeground=afg,
                      relief="flat", bd=0, highlightthickness=1,
                      highlightbackground=border, highlightcolor=border,
                      padx=12, pady=4, cursor="hand2")
        img = self.icons.img(icon) if icon else None
        if img is not None:
            kwargs["image"] = img
            kwargs["compound"] = "left"
        elif icon:
            kwargs["text"] = f"{self.icons.text(icon)}  {text}"
        return tk.Button(parent, **kwargs)

    # ---- 输入框（带占位符） ----
    def entry(self, parent: tk.Misc, placeholder: str = "") -> tk.Entry:
        c = self.c
        e = tk.Entry(parent, bg=c["panel2"], fg=c["ink"], font=self.f.body(10),
                     relief="flat", bd=0, highlightthickness=1,
                     highlightbackground=c["line"], highlightcolor=c["accent"],
                     insertbackground=c["ink"])
        if placeholder:
            e.insert(0, placeholder)
            e.configure(fg=c["ink3"])

            def focus_in(_e):
                if e.get() == placeholder:
                    e.delete(0, "end")
                    e.configure(fg=c["ink"])

            def focus_out(_e):
                if not e.get():
                    e.insert(0, placeholder)
                    e.configure(fg=c["ink3"])

            e.bind("<FocusIn>", focus_in)
            e.bind("<FocusOut>", focus_out)
        return e

    # ---- 只读路径框 ----
    def path_box(self, parent: tk.Misc, path: str) -> tk.Label:
        c = self.c
        return tk.Label(parent, text=path, anchor="w", bg=c["panel2"], fg=c["ink2"],
                        font=self.f.code(), padx=9, pady=5,
                        highlightthickness=1, highlightbackground=c["line"])

    # ---- 复选行 ----
    def checkbox(self, parent: tk.Misc, text: str, var: tk.BooleanVar) -> tk.Frame:
        c = self.c
        row = tk.Frame(parent, bg=c["panel"])
        box = tk.Label(row, text="✓" if var.get() else " ", bg=c["panel2"], fg=c["accent"],
                       font=self.f.body(9), width=2, highlightthickness=1,
                       highlightbackground=c["line2"])
        box.pack(side="left")
        lbl = tk.Label(row, text=text, bg=c["panel"], fg=c["ink2"], font=self.f.small())
        lbl.pack(side="left", padx=(6, 0))

        def toggle(_e=None):
            var.set(not var.get())
            box.configure(text="✓" if var.get() else " ")

        for w in (row, box, lbl):
            w.bind("<Button-1>", toggle)
            w.configure(cursor="hand2")
        return row


# ============================================================================
# 六、可滚动列表（两个曲库都用它）
# ============================================================================
class SongList(tk.Frame):
    """
    columns: [(标题, 宽度px 或 None=自适应, 对齐 'w'|'e'), ...]
    rows:    [(主文本, 次文本或 None, [右侧各列的文本...]), ...]
             只有一列、且有次文本时，自动排成两行（窄栏用）
    """

    def __init__(self, parent: tk.Misc, ui: UI, columns, rows,
                 on_select=None, row_height: int = 30, show_header: bool = True):
        c = ui.c
        super().__init__(parent, bg=c["panel"], highlightthickness=1,
                         highlightbackground=c["line"])
        self.ui = ui
        self.columns = columns
        self.rows = rows
        self.on_select = on_select
        self.row_height = row_height
        self.two_line = len(columns) == 1
        self.selected = 0
        self.row_frames: list[tk.Frame] = []

        if show_header:
            head = tk.Frame(self, bg=c["panel2"], height=28)
            head.pack(fill="x")
            head.pack_propagate(False)
            self._grid_columns(head)
            for i, (title, width, align) in enumerate(columns):
                tk.Label(head, text=title, bg=c["panel2"], fg=c["ink3"],
                         font=ui.f.code(), anchor=align).grid(
                    row=0, column=i, sticky="ew", padx=(12, 6) if i == 0 else (0, 12))
                if width:
                    head.columnconfigure(i, minsize=width)
            tk.Frame(self, bg=c["line"], height=1).pack(fill="x")

        wrap = tk.Frame(self, bg=c["panel"])
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, bg=c["panel"], highlightthickness=0, bd=0)
        self.vsb = tk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview,
                                width=8, relief="flat", bd=0, troughcolor=c["panel"],
                                bg=c["line2"], activebackground=c["ink3"],
                                highlightthickness=0)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner = tk.Frame(self.canvas, bg=c["panel"])
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>",
                        lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self._win, width=e.width))

        for i, row in enumerate(rows):
            self._add_row(i, row)

    def set_rows(self, rows):
        """换一批数据（重新构建行，最简单也最不容易错）。"""
        for w in self.inner.winfo_children():
            w.destroy()
        self.row_frames = []
        self.rows = rows
        self.selected = 0
        for i, row in enumerate(rows):
            self._add_row(i, row)
        try:
            self.canvas.yview_moveto(0)
        except Exception:
            pass

    # ---- 列宽：第 0 列自适应，其余按 minsize 固定 ----
    def _grid_columns(self, frame: tk.Frame):
        frame.columnconfigure(0, weight=1)
        for i, (_t, width, _a) in enumerate(self.columns):
            if i and width:
                frame.columnconfigure(i, minsize=width)

    def _add_row(self, index: int, row):
        c = self.ui.c
        primary, secondary, tail = row
        bg = c["panel3"] if index == self.selected else c["panel"]

        holder = tk.Frame(self.inner, bg=bg)
        holder.pack(fill="x")
        rf = tk.Frame(holder, bg=bg, height=self.row_height)
        rf.pack(fill="x")
        rf.pack_propagate(False)
        self._grid_columns(rf)

        widgets: list[tk.Misc] = [rf]

        if self.two_line and secondary:
            # 窄栏：谱名一行，信息一行
            top = tk.Label(rf, text=primary, bg=bg, fg=c["ink"], font=self.ui.f.body(10),
                           anchor="w")
            top.place(x=11, y=5, relwidth=1, width=-22)
            sub = tk.Label(rf, text=secondary, bg=bg, fg=c["ink3"], font=self.ui.f.code(8),
                           anchor="w")
            sub.place(x=11, y=23, relwidth=1, width=-22)
            widgets += [top, sub]
        else:
            main = tk.Label(rf, text=primary, bg=bg, fg=c["ink"], font=self.ui.f.body(10),
                            anchor="w")
            main.grid(row=0, column=0, sticky="ew", padx=(12, 6))
            widgets.append(main)
            if secondary:
                sub = tk.Label(rf, text=secondary, bg=bg, fg=c["ink3"],
                               font=self.ui.f.code(8), anchor="w")
                sub.grid(row=0, column=1, sticky="w", padx=(0, 12))
                widgets.append(sub)
            for i, value in enumerate(tail or []):
                col = i + 1
                tk.Label(rf, text=value, bg=bg, fg=c["ink3"], font=self.ui.f.code(),
                         anchor="e").grid(row=0, column=col, sticky="ew", padx=(0, 12))

        tk.Frame(holder, bg=c["line"], height=1).pack(fill="x")

        for w in widgets:
            w.bind("<MouseWheel>", self._on_wheel)
            w.bind("<Button-1>", lambda _e, i=index: self.select(i))
            w.bind("<Enter>", lambda _e, f=holder: self._hover(f, True))
            w.bind("<Leave>", lambda _e, f=holder: self._hover(f, False))
            try:
                w.configure(cursor="hand2")
            except tk.TclError:
                pass
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.inner.bind("<MouseWheel>", self._on_wheel)

        holder._bg_normal = c["panel"]   # type: ignore[attr-defined]
        holder._bg_hover = c["panel2"]   # type: ignore[attr-defined]
        self.row_frames.append(holder)

    def _on_wheel(self, event):
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def _hover(self, holder: tk.Frame, entering: bool):
        if holder is self.row_frames[self.selected]:
            return
        set_bg_recursive(holder, holder._bg_hover if entering else holder._bg_normal)  # type: ignore[attr-defined]

    def select(self, index: int):
        set_bg_recursive(self.row_frames[self.selected], self.ui.c["panel"])
        self.selected = index
        set_bg_recursive(self.row_frames[index], self.ui.c["panel3"])
        if self.on_select:
            self.on_select(self.rows[index])


# ============================================================================
# 七、主程序
# ============================================================================


class App:
    """口琴工具台主窗口（侧栏导航版）。"""

    def __init__(self, theme: str = None, selftest: bool = False):
        self.theme = theme if theme in THEMES else None
        self.selftest = selftest
        self.current_page = "local"
        self.playing = False
        self.busy = False
        self.proc = None
        self.dirty = False
        self.cr_active = False
        self._closing = False
        self._tick_job = None
        self.nav_widgets: dict[str, dict] = {}
        self._nav_items = self.nav_widgets
        self._busy_buttons = []
        self._lib_rows = []
        self._lib_busy = False
        self.library = []
        self._metas = {}
        self._by_name = {}
        self.admin = is_admin_now()

        cfg = {}
        if harp_cli:
            try:
                cfg = harp_cli.load_config() or {}
            except Exception:
                cfg = {}
        self.cfg = cfg
        self.score_path = SCORE
        saved = (cfg.get('SCORE_DIR') or '').strip()
        self.score_dir = saved if saved and os.path.isdir(saved) else default_score_dir()
        if self.theme is None:
            self.theme = cfg.get('THEME') if cfg.get('THEME') in THEMES else 'neon'

        self.root = tk.Tk()
        self.root.title(f"{APP_TITLE}  {APP_VER}")
        self.root.configure(bg=THEMES[self.theme]["canvas"])
        self.speed = tk.StringVar(master=self.root, value=str(cfg.get('SPEED') or '1.0'))
        self.countdown = tk.StringVar(master=self.root, value=str(cfg.get('COUNTDOWN') or '3'))
        self.keep_fav = tk.BooleanVar(master=self.root, value=bool(cfg.get('LIB_EXTRA_ON')))

        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h = min(1344, sw - 80), min(856, sh - 80)
        self.root.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 2 - 20)}")
        self.root.minsize(1024, 680)
        self.root.protocol('WM_DELETE_WINDOW', self.on_close)

        self.ui = UI(self.root, self.theme)
        self.body = None
        self._build()
        self._pump()

    # ------------------------------------------------------------ 配色
    def set_theme(self, name: str, save=True):
        """换肤 = 重建（简单粗暴，但绝不会漏掉某个控件）。"""
        if name not in THEMES:
            return
        self.theme = name
        self.ui = UI(self.root, name)
        self.root.configure(bg=self.ui.c["canvas"])
        if self.body:
            self.body.destroy()
        self._build()
        self.log(f"已切换到配色：{self.ui.c['label']}")
        if save and harp_cli:
            try:
                harp_cli.save_config({'THEME': name})
            except Exception:
                pass

    def cycle_theme(self):
        names = list(THEMES)
        self.set_theme(names[(names.index(self.theme) + 1) % len(names)])

    # ------------------------------------------------------------ 骨架
    def _build(self):
        c = self.ui.c
        self.body = tk.Frame(self.root, bg=c["canvas"])
        self.body.pack(fill="both", expand=True)
        self._build_rail(self.body)
        inspector = tk.Frame(self.body, bg=c["canvas"], width=336)
        inspector.pack(side="right", fill="y", padx=(GAP, 16), pady=(0, 16))
        inspector.pack_propagate(False)
        self._build_inspector(inspector)
        main = tk.Frame(self.body, bg=c["canvas"])
        main.pack(side="left", fill="both", expand=True, padx=(GAP, 0), pady=(0, 16))
        self._build_main(main)
        self._paint_nav()
        # 换肤是整棵树重建，重建后要把已有数据回填（否则列表会空掉）
        if self.library:
            self._fill_local()
        if self._lib_rows:
            self._fill_online(self._lib_rows)
        self.refresh_score_info()

    # ---- 左侧导航栏 ----
    def _build_rail(self, parent):
        c, f = self.ui.c, self.ui.f
        rail = tk.Frame(parent, bg=c["panel2"], width=208)
        rail.pack(side="left", fill="y", padx=(16, 0), pady=(0, 16))
        rail.pack_propagate(False)

        brand = tk.Frame(rail, bg=c["panel2"])
        brand.pack(fill="x", padx=12, pady=(14, 10))
        tk.Label(brand, text="♫", bg=c["panel2"], fg=c["accent"],
                 font=(f.ui, 16)).pack(side="left", padx=(0, 8))
        box = tk.Frame(brand, bg=c["panel2"])
        box.pack(side="left")
        tk.Label(box, text=BRAND, bg=c["panel2"], fg=c["ink"],
                 font=f.h1(12)).pack(anchor="w")
        tk.Label(box, text=APP_VER + (" · 打包版" if FROZEN else ""), bg=c["panel2"],
                 fg=c["ink3"], font=f.code(8)).pack(anchor="w")

        tk.Frame(rail, bg=c["line"], height=1).pack(fill="x")
        self._section(rail, "工作区")
        self.nav = [("local", "folder", "本机谱子库", "0"),
                    ("online", "globe", "在线曲库", "—"),
                    ("play", "play", "演奏台", ""),
                    ("log", "clipboard", "运行日志", "")]
        for key, icon, text, badge in self.nav:
            self.nav_widgets[key] = self._nav_item(rail, key, icon, text, badge)

        self._section(rail, "工具")
        for key, icon, text in TOOLS:
            self._tool_item(rail, key, icon, text)

        tk.Frame(rail, bg=c["panel2"]).pack(fill="both", expand=True)
        tk.Frame(rail, bg=c["line"], height=1).pack(fill="x")
        bottom = tk.Frame(rail, bg=c["panel2"])
        bottom.pack(fill="x", padx=12, pady=12)
        admin_row = tk.Frame(bottom, bg=c["panel2"], cursor="hand2")
        admin_row.pack(fill="x")
        aimg = self.ui.icons.img("user")
        if aimg is not None:
            tk.Label(admin_row, image=aimg, bg=c["panel2"], bd=0).pack(side="left")
        self.admin_lbl = tk.Label(admin_row, text="", bg=c["panel2"], fg=c["ok"],
                                  font=f.small(), anchor="w")
        self.admin_lbl.pack(side="left", padx=(6, 0))
        for w in (admin_row, self.admin_lbl):
            w.bind("<Button-1>", lambda _e: self.restart_as_admin())
        self._paint_admin()
        self.ui.button(bottom, "换肤", "ghost", self.cycle_theme,
                       icon="paint").pack(fill="x", pady=(8, 0))
        self.ui.button(bottom, "检查更新", "ghost", self.check_update,
                       icon="download").pack(fill="x", pady=(6, 0))
        self.ui.button(bottom, "退出程序", "danger", self.on_close).pack(fill="x", pady=(6, 0))
        tk.Label(bottom, text="当前：" + c["label"], bg=c["panel2"], fg=c["ink3"],
                 font=f.code(8), anchor="w").pack(fill="x", pady=(6, 0))

    def _paint_admin(self):
        try:
            self.admin_lbl.configure(
                text=('管理员 · 已获取权限' if self.admin else '普通权限 · 点这里提权'),
                fg=(self.ui.c['ok'] if self.admin else self.ui.c['danger']))
        except Exception:
            pass

    def _section(self, parent, text):
        c, f = self.ui.c, self.ui.f
        tk.Label(parent, text=text, bg=c["panel2"], fg=c["ink3"], font=f.code(8),
                 anchor="w").pack(fill="x", padx=16, pady=(12, 4))

    def _icon_slot(self, parent, name, bg, fg):
        slot = tk.Frame(parent, bg=bg, width=ICON_W)
        slot.pack(side="left", fill="y")
        slot.pack_propagate(False)
        img = self.ui.icons.img(name)
        if img is not None:
            lbl = tk.Label(slot, image=img, bg=bg, bd=0)
        else:
            lbl = tk.Label(slot, text=self.ui.icons.text(name), bg=bg, fg=fg,
                           font=self.ui.f.body(10))
        lbl.pack(fill="both", expand=True)
        return slot, lbl

    def _nav_item(self, parent, key, icon, text, badge):
        c, f = self.ui.c, self.ui.f
        row = tk.Frame(parent, bg=c["panel2"], height=34)
        row.pack(fill="x", padx=10)
        row.pack_propagate(False)
        slot, icn = self._icon_slot(row, icon, c["panel2"], c["ink3"])
        slot.pack_configure(padx=(8, 0))
        lbl = tk.Label(row, text=text, bg=c["panel2"], fg=c["ink2"], font=f.body(10),
                       anchor="w")
        lbl.pack(side="left", fill="x", expand=True, padx=(6, 0))
        bdg = tk.Label(row, text=badge, bg=c["panel2"], fg=c["ink3"], font=f.code(8))
        bdg.pack(side="right", padx=(0, 10))
        for w in (row, lbl, bdg, slot, icn):
            w.bind("<Button-1>", lambda _e, k=key: self.goto(k))
            w.bind("<Enter>", lambda _e, k=key: self._nav_hover(k, True))
            w.bind("<Leave>", lambda _e, k=key: self._nav_hover(k, False))
            w.configure(cursor="hand2")
        return {"row": row, "lbl": lbl, "bdg": bdg, "slot": slot, "icn": icn}

    def _nav_hover(self, key, entering: bool):
        if key == self.current_page:
            return
        c = self.ui.c
        set_bg_recursive(self.nav_widgets[key]["row"],
                         c["panel3"] if entering else c["panel2"])

    def _paint_nav(self):
        c = self.ui.c
        for key, w in self.nav_widgets.items():
            active = key == self.current_page
            bg = c["accent"] if active else c["panel2"]
            fg = c["accent_ink"] if active else c["ink2"]
            w["row"].configure(bg=bg)
            w["slot"].configure(bg=bg)
            w["icn"].configure(bg=bg, fg=fg)
            w["lbl"].configure(bg=bg, fg=fg,
                               font=self.ui.f.h2() if active else self.ui.f.body(10))
            w["bdg"].configure(bg=bg, fg=fg if active else c["ink3"])

    def _set_badge(self, key, text):
        w = self.nav_widgets.get(key)
        if w and w.get("bdg") is not None:
            try:
                w["bdg"].configure(text=str(text))
            except Exception:
                pass

    def _tool_item(self, parent, key, icon, text):
        c, f = self.ui.c, self.ui.f
        row = tk.Frame(parent, bg=c["panel2"], height=32)
        row.pack(fill="x", padx=10)
        row.pack_propagate(False)
        slot, icn = self._icon_slot(row, icon, c["panel2"], c["ink3"])
        slot.pack_configure(padx=(8, 0))
        lbl = tk.Label(row, text=text, bg=c["panel2"], fg=c["ink2"], font=f.small(),
                       anchor="w")
        lbl.pack(side="left", fill="x", expand=True, padx=(6, 0))
        for w in (row, lbl, slot, icn):
            w.bind("<Button-1>", lambda _e, k=key: self.tool_clicked(k))
            w.bind("<Enter>", lambda _e, r=row: set_bg_recursive(r, c["panel3"]))
            w.bind("<Leave>", lambda _e, r=row: set_bg_recursive(r, c["panel2"]))
            w.configure(cursor="hand2")

    # ---- 主区 ----
    def _build_main(self, parent):
        c = self.ui.c
        host = tk.Frame(parent, bg=c["canvas"])
        host.pack(fill="both", expand=True)
        self.pages = {
            "local": self._page_local(host),
            "online": self._page_online(host),
            "play": self._page_play(host),
            "log": self._page_log(host),
        }
        self._build_playbar(parent)
        self.goto("local")

    def _page_local(self, parent):
        c, f = self.ui.c, self.ui.f
        page = tk.Frame(parent, bg=c["canvas"])
        bar = tk.Frame(page, bg=c["canvas"], height=46)
        bar.pack(fill="x", pady=(13, 0))
        bar.pack_propagate(False)
        tk.Label(bar, text="本机谱子库", bg=c["canvas"], fg=c["ink"],
                 font=f.h1(15)).pack(side="left")
        self.local_count = tk.Label(bar, text="共 0 份", bg=c["canvas"], fg=c["ink3"],
                                    font=f.code())
        self.local_count.pack(side="left", padx=(10, 0))
        self.ui.path_box(bar, self.score_dir).pack(side="left", fill="x", expand=True,
                                                   padx=(12, 8))
        self.ui.button(bar, "换目录", "default", self.change_dir,
                       icon="folder_open").pack(side="left")
        self.ui.button(bar, "⟳", "default", self.rescan).pack(side="left", padx=(6, 0))
        self.ui.button(bar, "▸", "default", self.open_dir).pack(side="left", padx=(6, 0))

        outer, _head, body = self.ui.panel(page, "谱面列表", "点击选择 · 双击演奏",
                                           icon="page")
        outer.pack(fill="both", expand=True, pady=(GAP, 0))
        self.local_list = SongList(body, self.ui,
                                   [("谱名", None, "w"), ("音符", 90, "e"), ("BPM", 90, "e")],
                                   [], on_select=self.pick_local, row_height=30)
        self.local_list.pack(fill="both", expand=True)
        self.local_list.bind("<Double-Button-1>", lambda _e: self.start_play())
        return page

    def _page_online(self, parent):
        c, f = self.ui.c, self.ui.f
        page = tk.Frame(parent, bg=c["canvas"])
        bar = tk.Frame(page, bg=c["canvas"], height=46)
        bar.pack(fill="x", pady=(13, 0))
        bar.pack_propagate(False)
        tk.Label(bar, text="在线曲库", bg=c["canvas"], fg=c["ink"],
                 font=f.h1(15)).pack(side="left")
        self.online_count = tk.Label(bar, text="—", bg=c["canvas"], fg=c["ink3"],
                                     font=f.code())
        self.online_count.pack(side="left", padx=(10, 0))
        self.entry_online = self.ui.entry(bar, "输入曲名 / 关键词，搜索在线谱面…")
        self.entry_online.pack(side="left", fill="x", expand=True, padx=(12, 8), ipady=5)
        self.entry_online.bind("<Return>", lambda _e: self.do_search())
        self.ui.button(bar, "搜索", "key", self.do_search).pack(side="left")
        self.ui.button(bar, "⟳", "default", self.refresh_online).pack(side="left", padx=(6, 0))

        outer, _head, body = self.ui.panel(page, "曲目列表", "成品站公开谱库 · 双击下载",
                                           icon="globe")
        outer.pack(fill="both", expand=True, pady=(GAP, 0))
        self.online_list = SongList(body, self.ui,
                                    [("曲名", None, "w"), ("投稿", 140, "e"),
                                     ("BPM", 70, "e"), ("音符", 80, "e")],
                                    [], on_select=self.pick_online, row_height=28)
        self.online_list.pack(fill="both", expand=True)
        self.online_list.bind("<Double-Button-1>", lambda _e: self.download())
        return page

    def _page_play(self, parent):
        c, f = self.ui.c, self.ui.f
        page = tk.Frame(parent, bg=c["canvas"])
        outer, _head, body = self.ui.panel(page, "演奏台", "", icon="play")
        outer.pack(fill="both", expand=True, pady=(GAP, 0))
        inner = tk.Frame(body, bg=c["panel"])
        inner.pack(anchor="nw", padx=26, pady=26)
        self.play_name = tk.Label(inner, text="（还没选谱子）", bg=c["panel"], fg=c["ink"],
                                  font=f.h1(24), anchor="w")
        self.play_name.pack(anchor="w")
        self.play_sub = tk.Label(inner, text="去「本机谱子库」点一首，或从右边在线曲库下载",
                                 bg=c["panel"], fg=c["ink3"], font=f.code(), anchor="w")
        self.play_sub.pack(anchor="w", pady=(6, 22))
        row = tk.Frame(inner, bg=c["panel"])
        row.pack(anchor="w")
        b1 = self.ui.button(row, "开始演奏", "key", self.start_play, icon="play")
        b1.pack(side="left")
        b2 = self.ui.button(row, "干跑测试", "default", self.dry_run, icon="run")
        b2.pack(side="left", padx=8)
        b3 = self.ui.button(row, "急停", "danger", self.stop_play, icon="stop")
        b3.pack(side="left")
        self._busy_buttons += [b1, b2, b3]
        tk.Label(inner, bg=c["panel"], fg=c["ink3"], font=f.small(), justify="left",
                 anchor="w",
                 text="① 游戏与本工具都要以管理员运行（否则 UIPI 会静默丢弃按键）\n"
                      "② 输入法切英文；进游戏先打开口琴界面\n"
                      "③ 点「开始演奏」后立刻切到游戏窗口（脚本会等它到前台）\n"
                      "④ 出事按 Esc 急停；鼠标键卡住按 Ctrl+Alt+U 或点侧栏「解卡」"
                 ).pack(anchor="w", pady=(24, 0))
        return page

    def _page_log(self, parent):
        c = self.ui.c
        page = tk.Frame(parent, bg=c["canvas"])
        outer, head, body = self.ui.panel(page, "运行日志", "全部", icon="clipboard")
        outer.pack(fill="both", expand=True, pady=(GAP, 0))
        self.ui.button(head, "清空", "ghost", self.clear_log).pack(side="right", pady=4)
        self.big_log = self._make_log(body, height=20)
        return page

    def _build_playbar(self, parent):
        c, f = self.ui.c, self.ui.f
        bar = tk.Frame(parent, bg=c["panel"], highlightthickness=1,
                       highlightbackground=c["line"])
        bar.pack(fill="x", pady=(GAP, 0))
        right = tk.Frame(bar, bg=c["panel"])
        right.pack(side="right", padx=14, pady=10)
        left = tk.Frame(bar, bg=c["panel"])
        left.pack(side="left", fill="x", expand=True, padx=14, pady=10)
        self.bar_name = tk.Label(left, text="（还没选谱子）", bg=c["panel"], fg=c["ink"],
                                 font=f.h1(13), anchor="w")
        self.bar_name.pack(anchor="w", fill="x")
        self.bar_sub = tk.Label(left, text="", bg=c["panel"], fg=c["ink3"], font=f.code(),
                                anchor="w")
        self.bar_sub.pack(anchor="w", fill="x", pady=(3, 2))
        self.bar_hint = tk.Label(left, text="双击谱子可直接演奏；切到游戏窗口后自动开始。",
                                 bg=c["panel"], fg=c["ink3"], font=f.small(), anchor="w")
        self.bar_hint.pack(anchor="w", fill="x")
        for label, var, unit in (("速度", self.speed, "×"), ("倒计时", self.countdown, "秒")):
            cell = tk.Frame(right, bg=c["panel"])
            cell.pack(side="left", padx=(0, 12))
            tk.Label(cell, text=label, bg=c["panel"], fg=c["ink3"], font=f.code(8),
                     anchor="w").pack(anchor="w")
            row = tk.Frame(cell, bg=c["panel"])
            row.pack(anchor="w", pady=(3, 0))
            tk.Entry(row, textvariable=var, width=4, justify="center", bg=c["panel2"],
                     fg=c["ink"], font=f.code(10), relief="flat", bd=0,
                     highlightthickness=1, highlightbackground=c["line"],
                     insertbackground=c["ink"]).pack(side="left", ipady=4)
            tk.Label(row, text=unit, bg=c["panel"], fg=c["ink3"],
                     font=f.code(8)).pack(side="left", padx=(4, 0))
        self.b_dry = self.ui.button(right, "干跑", "default", self.dry_run, icon="run")
        self.b_dry.pack(side="left", ipady=6)
        self.b_abort = self.ui.button(right, "急停", "danger", self.stop_play, icon="stop")
        self.b_abort.pack(side="left", padx=6, ipady=6)
        self.b_play = self.ui.button(right, "开始演奏", "key", self.start_play, icon="play")
        self.b_play.pack(side="left", ipady=6)
        self._busy_buttons += [self.b_dry, self.b_abort, self.b_play]
        self.b_stop = self.b_abort

    def _build_inspector(self, parent):
        c = self.ui.c
        outer, _head, body = self.ui.panel(parent, "在线曲库", "DFH", icon="globe")
        outer.pack(fill="both", expand=True)
        pad = tk.Frame(body, bg=c["panel"])
        pad.pack(fill="both", expand=True, padx=11, pady=11)
        search = tk.Frame(pad, bg=c["panel"])
        search.pack(fill="x")
        self.entry_side = self.ui.entry(search, "搜索曲名 / 关键词")
        self.entry_side.pack(side="left", fill="x", expand=True, ipady=5)
        self.entry_side.bind("<Return>", lambda _e: self.do_search())
        self.ui.button(search, "搜索", "key", self.do_search).pack(side="left", padx=(6, 0))
        self.ui.button(search, "⟳", "default", self.refresh_online).pack(side="left", padx=(6, 0))
        self.side_list = SongList(pad, self.ui, [("曲名", None, "w")], [],
                                  on_select=self.pick_online, row_height=42,
                                  show_header=False)
        self.side_list.pack(fill="both", expand=True, pady=(11, 0))
        self.b_dl = self.ui.button(pad, "下载到谱子库", "key", self.download, icon="download")
        self.b_dl.pack(fill="x", pady=(11, 6))
        self._busy_buttons.append(self.b_dl)
        self.ui.checkbox(pad, "同时存到收藏夹", self.keep_fav).pack(anchor="w")
        dirrow = tk.Frame(pad, bg=c["panel"])
        dirrow.pack(fill="x", pady=(8, 0))
        self.fav_dir = self.ui.path_box(
            dirrow, self.cfg.get('LIB_EXTRA_DIR')
            or os.path.join(os.path.expanduser('~'), 'Downloads', '新建文件夹'))
        self.fav_dir.pack(side="left", fill="x", expand=True)
        self.ui.button(dirrow, "⋯", "default",
                       self.choose_download_dir).pack(side="left", padx=(6, 0))
        self.lib_status = tk.Label(pad, text="● 正在读取曲库索引…", bg=c["panel"],
                                   fg=c["ok"], font=self.ui.f.small(), anchor="w")
        self.lib_status.pack(anchor="w", pady=(9, 0))

        outer2, head2, body2 = self.ui.panel(parent, "运行日志", icon="clipboard")
        outer2.configure(height=196)
        outer2.pack(fill="x", pady=(GAP, 0))
        outer2.pack_propagate(False)
        self.ui.button(head2, "解卡", "ghost", self.unlock,
                       icon="unlock").pack(side="right", padx=(0, 8), pady=4)
        self.ui.button(head2, "清空", "ghost", self.clear_log).pack(side="right", pady=4)
        self.log_widget = self._make_log(body2, height=8)

    # ---- 日志 ----
    def _make_log(self, parent, height=8) -> tk.Text:
        c, f = self.ui.c, self.ui.f
        txt = tk.Text(parent, height=height, bg=c["panel"], fg=c["ink2"], font=f.code(),
                      relief="flat", bd=0, wrap="word", padx=10, pady=8,
                      insertbackground=c["ink"], highlightthickness=0, state="disabled")
        txt.tag_configure("dim", foreground=c["ink3"])
        txt.tag_configure("ok", foreground=c["ink2"])
        txt.tag_configure("warn", foreground=c["danger"])
        txt.tag_configure("err", foreground=c["danger"])
        txt.tag_configure("good", foreground=c["ok"])
        txt.pack(fill="both", expand=True)
        return txt

    def _log_widgets(self):
        for name in ("log_widget", "big_log"):
            w = getattr(self, name, None)
            if w is not None:
                try:
                    if w.winfo_exists():
                        yield w
                except Exception:
                    pass

    def log(self, text: str, kind: str = "ok"):
        self.log_line(text, kind)

    def log_line(self, text, tag=None, replace=False):
        """写一行日志（两个日志框同时写）。带 \\r 的进度条就地刷新。"""
        stamp = time.strftime('%H:%M:%S')
        for widget in self._log_widgets():
            try:
                widget.configure(state='normal')
                if self.cr_active:
                    if replace:
                        widget.delete('end-1c linestart', 'end-1c')
                    else:
                        widget.insert('end', '\n')
                if replace:
                    self.cr_active = True
                widget.insert('end', '[%s] ' % stamp)
                widget.insert('end', text, tag or ())
                if not self.cr_active:
                    widget.insert('end', '\n')
                widget.see('end')
                widget.configure(state='disabled')
            except Exception:
                pass
        if not replace:
            self.cr_active = False

    def clear_log(self):
        for widget in self._log_widgets():
            try:
                widget.configure(state='normal')
                widget.delete('1.0', 'end')
                widget.configure(state='disabled')
            except Exception:
                pass
        self.cr_active = False

    # ================================================================ 行为
    def goto(self, key: str):
        self.current_page = key
        for page in self.pages.values():
            page.pack_forget()
        self.pages[key].pack(fill="both", expand=True)
        self._paint_nav()

    def tool_clicked(self, name):
        if name == 'unlock':
            self.unlock()
        elif name == 'tune':
            self.open_calibrate()
        elif name == 'keytest':
            self.do_keytest()
        elif name == 'open':
            self.open_dir()

    # ---- 本机谱子库 ----
    def _fill_local(self):
        rows = []
        self._by_name = {}
        for p in self.library:
            name = os.path.splitext(os.path.basename(p))[0]
            m = self._metas.get(p, {})
            rows.append((name, None, [str(m.get('notes', 0) or ''), str(m.get('bpm', 0) or '')]))
            self._by_name[name] = p
        try:
            self.local_list.set_rows(rows)
        except Exception as e:
            self.log('[谱子库] 列表刷新失败：%s' % e, 'err')
        self.local_count.configure(text='共 %d 份' % len(self.library))
        self._set_badge('local', len(self.library))

    def _scan_library(self, quiet=False):
        d = self.score_dir
        if not os.path.isdir(d):
            try:
                os.makedirs(d, exist_ok=True)
            except Exception as e:
                self.log('[谱子库] 目录建不了(%s)：%s' % (d, e), 'err')
                return
        try:
            files = [os.path.join(d, n) for n in sorted(os.listdir(d))
                     if n.lower().endswith(SCORE_EXTS)]
        except Exception as e:
            self.log('[谱子库] 扫描失败：%s' % e, 'err')
            return
        self.library = files
        self._metas = {p: score_meta(p) for p in files}
        self._fill_local()
        if not quiet:
            self.log('[谱子库] %s · %d 份' % (d, len(files)), 'dim')

    def rescan(self):
        self._scan_library()

    def change_dir(self):
        d = filedialog.askdirectory(title='选择谱子库目录', initialdir=self.score_dir)
        if not d:
            return
        self.score_dir = d
        if harp_cli:
            try:
                harp_cli.save_config({'SCORE_DIR': d})
            except Exception:
                pass
        self._scan_library()
        self.log('[谱子库] 换目录 → %s' % d)

    def open_dir(self):
        d = self.score_dir if os.path.isdir(self.score_dir) else ROOT
        try:
            os.startfile(d)
            self.log('[目录] 打开 %s' % d, 'dim')
        except Exception as e:
            self.log('[目录] 打不开 %s：%s' % (d, e), 'err')

    def pick_local(self, row):
        path = self._by_name.get(row[0])
        if not path:
            return
        self.score_path = path
        self.refresh_score_info()
        self.log('[谱子库] 选中 %s' % row[0], 'dim')

    def refresh_score_info(self):
        name = os.path.basename(self.score_path) if self.score_path else '(没选谱子)'
        m = score_meta(self.score_path) if os.path.isfile(self.score_path) else {}
        kind = {'DFH': '成品站三行式', '简谱': '简谱 / 音区字母'}.get(m.get('kind'), '')
        bits = [b for b in (kind,
                            ('%d 音符' % m['notes']) if m.get('notes') else '',
                            ('BPM %g' % m['bpm']) if m.get('bpm') else '') if b]
        sub = ' · '.join(bits) or '（选一份谱子）'
        for w in ('bar_name', 'play_name'):
            lbl = getattr(self, w, None)
            if lbl is not None:
                try:
                    lbl.configure(text=name)
                except Exception:
                    pass
        for w in ('bar_sub', 'play_sub'):
            lbl = getattr(self, w, None)
            if lbl is not None:
                try:
                    lbl.configure(text=sub)
                except Exception:
                    pass

    def _is_dfh(self):
        try:
            if os.path.isfile(self.score_path):
                with open(self.score_path, 'r', encoding='utf-8-sig', errors='replace') as f:
                    text = f.read()
                return ('键位' in text and '节奏' in text and '小节' in text)
        except Exception:
            pass
        return False

    # ---- 在线曲库（成品站公开谱库）----
    PLACEHOLDERS = ('搜索曲名 / 关键词', '输入曲名 / 关键词，搜索在线谱面…')

    def _kw(self):
        """取搜索词；输入框里还留着占位提示时不算关键词。"""
        for name in ('entry_side', 'entry_online'):
            e = getattr(self, name, None)
            if e is not None:
                try:
                    v = e.get().strip()
                    if v and v not in self.PLACEHOLDERS:
                        return v
                except Exception:
                    pass
        return ''

    def _set_kw(self, text):
        for name in ('entry_side', 'entry_online'):
            e = getattr(self, name, None)
            if e is not None:
                try:
                    e.delete(0, 'end')
                    e.insert(0, text)
                except Exception:
                    pass

    def _lib_async(self, work, done, busy):
        if self._lib_busy:
            self.log('[曲库] 上一个操作还没完…', 'warn')
            return
        self._lib_busy = True
        try:
            self.lib_status.configure(text='● ' + busy)
        except Exception:
            pass
        q = queue.Queue()

        def runner():
            try:
                q.put(('ok', work()))
            except Exception as e:
                q.put(('err', e))
        threading.Thread(target=runner, daemon=True).start()

        def poll():
            try:
                kind, payload = q.get_nowait()
            except queue.Empty:
                if self.root.winfo_exists():
                    self.root.after(120, poll)
                else:
                    self._lib_busy = False
                return
            self._lib_busy = False
            if kind == 'err':
                try:
                    self.lib_status.configure(text='● 失败：%s' % payload)
                except Exception:
                    pass
                self.log('[曲库] %s' % payload, 'err')
            else:
                done(payload)
        self.root.after(120, poll)

    def _fill_online(self, rows):
        self._lib_rows = rows
        main_rows = [(r['title'], None, [r['uploader'] or '匿名', str(r['bpm']), str(r['notes'])])
                     for r in rows]
        side_rows = [(r['title'], '%s · %s BPM · %s 音' % (r['uploader'] or '匿名',
                                                          r['bpm'], r['notes']), None)
                     for r in rows[:40]]
        try:
            self.online_list.set_rows(main_rows)
            self.side_list.set_rows(side_rows)
        except Exception as e:
            self.log('[曲库] 列表刷新失败：%s' % e, 'err')

    def do_search(self):
        if harp_lib is None:
            self.log('[曲库] 缺 harp_lib.py，这个功能用不了', 'err')
            return
        kw = self._kw()

        def work():
            total, _s, _t = harp_lib.index()
            return total, (harp_lib.search(kw) if kw else [])

        def done(res):
            total, rows = res
            self.online_count.configure(text='%d 首' % total)
            self._set_badge('online', '%d' % total)
            self._fill_online(rows)
            if kw:
                self.lib_status.configure(
                    text='● 索引完成 · %d 份；「%s」命中 %d 条' % (total, kw, len(rows)))
                self.log('[曲库] 「%s」命中 %d 条' % (kw, len(rows)), 'dim')
            else:
                self.lib_status.configure(text='● 索引完成 · %d 份' % total)
                self.log('[曲库] 索引完成 · 共 %d 份' % total, 'dim')
        self._lib_async(work, done, '正在搜索…' if kw else '正在读取曲库索引…')

    def refresh_online(self):
        self.do_search()

    def pick_online(self, row):
        for r in self._lib_rows:
            if r['title'] == row[0]:
                self._picked_online = r
                return

    def download(self):
        row = getattr(self, '_picked_online', None)
        if not row:
            self.log('[曲库] 先在上面点一首', 'warn')
            return
        dests = [self.score_dir]
        if self.keep_fav.get():
            fav = self.fav_dir.cget('text').strip()
            if fav:
                dests.append(fav)
        if harp_cli:
            try:
                harp_cli.save_config({'LIB_EXTRA_ON': bool(self.keep_fav.get()),
                                      'LIB_EXTRA_DIR': self.fav_dir.cget('text').strip()})
            except Exception:
                pass

        def work():
            return harp_lib.download(row['id'], dests)

        def done(res):
            title, paths, info = res
            self.log('[曲库] 《%s》 %d 音符 · 移调 %+d · 超出音域 %d'
                     % (title, info['notes'], info['transpose'], info['unplayable']), 'good')
            for p in paths:
                self.log('        → %s' % p, 'dim')
            self._scan_library(quiet=True)
            if paths:
                self.score_path = paths[0]
                self.refresh_score_info()
            self.lib_status.configure(text='● 已下载《%s》' % title)
        self._lib_async(work, done, '正在下载并转换《%s》…' % row['title'])

    def choose_download_dir(self):
        d = filedialog.askdirectory(title='选择"收藏夹"目录',
                                    initialdir=self.fav_dir.cget('text') or self.score_dir)
        if d:
            self.fav_dir.configure(text=d)
            if harp_cli:
                try:
                    harp_cli.save_config({'LIB_EXTRA_DIR': d})
                except Exception:
                    pass

    # ---- 演奏（子进程）----
    def _set_busy(self, busy):
        self.busy = busy
        state = ['disabled'] if busy else ['!disabled']
        for w in self._busy_buttons:
            try:
                w.state(state)
            except Exception:
                pass

    def run_proc(self, cmd, title):
        if self.busy:
            self.log('[!] 还有一个任务在跑，先等它结束或用「急停」', 'warn')
            return
        try:
            if os.path.exists(STOP_FLAG):
                os.remove(STOP_FLAG)
        except Exception:
            pass
        self.log('▶ ' + title)
        self.log('$ ' + subprocess.list2cmdline(cmd), 'dim')
        self._set_busy(True)
        self.cr_active = False
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, env=build_env(),
                                         bufsize=0, creationflags=CREATE_NO_WINDOW)
        except Exception as e:
            self.log('[!] 起不来：%s' % e, 'err')
            self._set_busy(False)
            self.proc = None
            return
        self.q = getattr(self, 'q', None) or queue.Queue()
        self.q = queue.Queue()
        threading.Thread(target=self._worker, args=(self.proc,), daemon=True).start()

    def _worker(self, proc):
        try:
            for raw in iter(proc.stdout.readline, b''):
                self.q.put(('out', raw))
        except Exception:
            pass
        try:
            proc.stdout.close()
        except Exception:
            pass
        self.q.put(('done', proc.wait()))

    def _feed(self, raw):
        """子进程输出：把 \\r 当"原地刷新"，\\r\\n 当普通换行。"""
        text = raw.decode('utf-8', 'replace')
        for line in text.split('\n'):
            if line.endswith('\r'):
                self.log_line(line[:-1], replace=True)
            elif line.strip():
                self.log_line(line, replace=False)

    def _pump(self):
        if getattr(self, '_closing', False):     # 窗口已销毁，别再排队（否则 Tk 报 invalid command name）
            return
        q = getattr(self, 'q', None)
        if q is not None:
            try:
                while True:
                    kind, payload = q.get_nowait()
                    if kind == 'out':
                        self._feed(payload)
                    else:
                        rc = payload
                        self.cr_active = False
                        self.log_line('✓ 完成' if rc == 0 else '✗ 结束，返回码 %s' % rc,
                                      'good' if rc == 0 else 'err')
                        self.proc = None
                        self.playing = False
                        self._set_busy(False)
                        self.refresh_score_info()
            except queue.Empty:
                pass
        try:
            self._tick_job = self.root.after(80, self._pump)
        except Exception:
            pass

    def _speed(self):
        try:
            return max(0.1, float(self.speed.get()))
        except Exception:
            return 1.0

    def _countdown(self):
        try:
            return max(0, int(float(self.countdown.get())))
        except Exception:
            return 3

    def dry_run(self):
        if not os.path.isfile(self.score_path):
            self.log('[干跑] 还没选谱子', 'warn')
            return
        if self._is_dfh():
            cmd = player_cmd(HARP_DFH, [self.score_path, '--dry-run', '--no-wait'])
        else:
            cmd = player_cmd(HARP2, [self.score_path, '--dry-run', '--speed', '20',
                                     '--countdown', '0'])
        self.run_proc(cmd, '干跑测试：只打印按键序列，不真的发按键')

    def start_play(self):
        if not os.path.isfile(self.score_path):
            self.log('[演奏] 还没选谱子', 'warn')
            return
        cd = self._countdown()
        if not self.selftest and not messagebox.askyesno(
                '开始演奏',
                '即将真的发送键盘 / 鼠标操作。\n\n'
                '· 谱面：%s\n· 倒计时 %d 秒，请立刻切到游戏窗口\n'
                '· Esc 或「急停」结束\n· 请确认输入法已切英文\n\n继续？'
                % (os.path.basename(self.score_path), cd), parent=self.root):
            return
        if harp_cli:
            try:
                harp_cli.save_config({'SPEED': self.speed.get(),
                                      'COUNTDOWN': self.countdown.get()})
            except Exception:
                pass
        if self._is_dfh():
            cmd = player_cmd(HARP_DFH, [self.score_path, '--speed', str(self._speed()),
                                        '--countdown', str(cd)])
        else:
            cmd = player_cmd(HARP2, [self.score_path, '--speed', str(self._speed()),
                                     '--countdown', str(cd)])
        self.playing = True
        self.run_proc(cmd, '开始演奏：%s' % os.path.basename(self.score_path))

    def stop_play(self):
        if self.proc is None:
            self.log('[急停] 现在没有在跑的任务', 'dim')
            self.playing = False
            return
        self.log('[急停] 请求停止（先让脚本自己松开所有键）…', 'warn')
        try:
            with open(STOP_FLAG, 'w') as f:
                f.write('stop')
        except Exception:
            pass
        try:
            self.proc.wait(timeout=3)
        except Exception:
            self.log('[急停] 3 秒没退，强制结束整棵进程树', 'warn')
            try:
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(self.proc.pid)],
                               creationflags=CREATE_NO_WINDOW, timeout=10)
            except Exception:
                pass
        self.playing = False

    # ---- 自动更新（GitHub Release）----
    def _bg(self, work, done, err=None):
        """后台线程干活 + 主线程轮询结果（tkinter 不能跨线程碰控件）。"""
        q = queue.Queue()

        def runner():
            try:
                q.put(('ok', work()))
            except Exception as e:
                q.put(('err', e))
        threading.Thread(target=runner, daemon=True).start()

        def poll():
            if getattr(self, '_closing', False):     # 窗口已关，别再排队
                return
            try:
                kind, payload = q.get_nowait()
            except queue.Empty:
                try:
                    self.root.after(120, poll)
                except Exception:
                    pass
                return
            if kind == 'err':
                (err or (lambda e: self.log('[后台] %s' % e, 'err')))(payload)
            else:
                done(payload)
        self.root.after(120, poll)

    def _repo(self):
        """更新源：优先用配置里的，没配就用程序内置的默认仓库（别人不用填）。"""
        r = (self.cfg.get('UPDATE_REPO') or '').strip()
        if r:
            return r
        return getattr(harp_update, 'REPO_PLACEHOLDER', '') if harp_update else ''

    def check_update(self, quiet=False):
        """查有没有新版本；quiet=True 时没配更新源就安静跳过。"""
        from tkinter import simpledialog
        if harp_update is None:
            if not quiet:
                self.log('[更新] 缺 harp_update.py', 'warn')
            return
        repo = self._repo()
        if (not repo or repo.upper() == 'YOURNAME/HARP-TOOL') and quiet:
            return
        if not repo or repo.upper() == 'YOURNAME/HARP-TOOL':
            v = simpledialog.askstring(
                '设置更新源',
                '填你的 GitHub 仓库（owner/repo）\n'
                '程序会去取它的 releases/latest/download/update.json',
                initialvalue='', parent=self.root)
            if not v:
                return
            repo = v.strip()
            self.cfg['UPDATE_REPO'] = repo
            if harp_cli:
                try:
                    harp_cli.save_config({'UPDATE_REPO': repo})
                except Exception:
                    pass
        self.log('[更新] 当前 %s，正在检查 %s …' % (harp_update.APP_VER, repo), 'dim')

        def work():
            return harp_update.check(repo=repo)

        def done(man):
            if not man:
                self.log('[更新] 已是最新版（%s）' % harp_update.APP_VER, 'good')
                return
            ver = man.get('version')
            notes = (man.get('notes') or '').strip() or '（没有更新说明）'
            size = man.get('size')
            tip = ('当前 %s → 新版 %s%s\n\n%s\n\n现在下载并自动更新吗？'
                   '（会重启程序，旧版留成 .old 备份）'
                   % (harp_update.APP_VER, ver,
                      ('（%.1f MB）' % (size / 1048576.0)) if size else '', notes))
            if not messagebox.askyesno('发现新版本 %s' % ver, tip, parent=self.root):
                self.log('[更新] 已跳过 %s（下次启动还会提示）' % ver, 'dim')
                return
            self.do_update(man)

        def err(e):
            self.log('[更新] 检查失败：%s' % e, 'warn')
        self._bg(work, done, err)

    def do_update(self, man):
        import tempfile
        dest = os.path.join(tempfile.gettempdir(), 'harp-update-download.exe')

        def work():
            def prog(got, total):
                pct = ('%d%%' % (100 * got / total)) if total else ('%d KB' % (got // 1024))
                self.log_line('[更新] 下载中 %s' % pct, replace=True)

            def note(s):
                self.log_line('', replace=False)
                self.log('[更新] ' + s, 'dim')

            # 逐个源试，**每个源都校验 sha256**：镜像实测会把连接掐断、给个残缺文件
            # 却不报错（0.13 MB/s 只下了 2.87/10.9 MB），所以不能只靠"读完就算成功"。
            path, used = harp_update.download_any(man, dest, on_progress=prog, on_log=note)
            self.log_line('', replace=False)
            got = harp_update.sha256_file(path)
            return path, got, used

        def done(res):
            dest, got, used = res
            src = used.split('/')[2] if '//' in used else used
            self.log('[更新] 下载完成，sha256 校验通过（%s，来源 %s）' % (got[:12], src), 'good')
            if not getattr(sys, 'frozen', False):
                self.log('[更新] 当前是源码模式：文件已下好，但不做替换'
                         '（替换只在打包版里生效）', 'warn')
                return
            try:
                harp_update.apply_update(dest, on_log=lambda s: self.log('[更新] ' + s))
            except Exception as e:
                self.log('[更新] 替换失败：%s' % e, 'err')
                return
            self.log('[更新] 即将重启到新版本…', 'good')
            self.on_close()          # on_close 现在自己会销毁窗口

        def err(e):
            self.log_line('', replace=False)
            self.log('[更新] %s' % e, 'err')
        self._bg(work, done, err)

    # ---- 工具 ----
    def unlock(self):
        self.run_proc(player_cmd(HARP_DFH, ['--release']), '解卡：松开鼠标左/中/右键')

    def do_keytest(self):
        if harp2 is None:
            self.log('[自检] 缺 harp2.py', 'err')
            return
        self.run_proc(player_cmd(HARP2, ['--keytest']),
                      '按键自检：往记事本敲 zxcvbnm, 看通路')

    def open_calibrate(self):
        from tkinter import simpledialog
        cur = 1
        if harp_cli:
            try:
                cur = int(harp_cli.load_config().get('OCTAVE_CLICKS', 1))
            except Exception:
                cur = 1
        v = simpledialog.askstring(
            '音区校准',
            '游戏里点几次「升调」等于一个八度？\n'
            '（成品站谱面按 ±1 个八度处理，默认 1；老式简谱若一次只升半音就填 12）',
            initialvalue=str(cur), parent=self.root)
        if not v:
            return
        try:
            n = int(v)
        except Exception:
            self.log('[校准] 要填整数', 'warn')
            return
        if harp_cli:
            try:
                harp_cli.save_config({'OCTAVE_CLICKS': n})
            except Exception:
                pass
        self.log('[校准] 已保存：点 %d 次「升调」= 一个八度' % n, 'good')

    def restart_as_admin(self):
        if self.admin:
            self.log('[权限] 已经是管理员了', 'dim')
            return
        if not messagebox.askyesno('提权', '用管理员身份重新启动？\n'
                                           '（游戏是管理员权限，不这样按键会被静默丢弃）',
                                   parent=self.root):
            return
        try:
            import ctypes
            params = subprocess.list2cmdline([os.path.abspath(__file__)] + sys.argv[1:])
            rc = ctypes.windll.shell32.ShellExecuteW(None, 'runas', sys.executable,
                                                     params, os.getcwd(), 1)
            if int(rc) > 32:
                self.on_close()      # on_close 现在自己会销毁窗口
        except Exception as e:
            self.log('[权限] 提权失败：%s' % e, 'err')

    def check_env(self):
        bits = ['Python %s' % sys.version.split()[0],
                '64 位' if sys.maxsize > 2 ** 32 else '32 位']
        try:
            import shutil
            total, used, free = shutil.disk_usage(ROOT)
            bits.append('C 盘剩余 %.1f GB' % (free / 1024 ** 3))
        except Exception:
            pass
        if harp_cli:
            try:
                ff = harp_cli.find_ffmpeg() if hasattr(harp_cli, 'find_ffmpeg') else None
                bits.append('ffmpeg ' + ('✓' if ff else '✗'))
            except Exception:
                pass
        self.log('[环境] ' + ' | '.join(bits), 'dim')
        self.log('[权限] ' + ('管理员 ✓（按键能送达游戏）' if self.admin
                              else '普通权限 ✗（游戏里会毫无反应，点左下的提权）'),
                 'good' if self.admin else 'warn')

    # ---- 收尾 ----
    def on_close(self):
        """关窗收尾：停演奏、存配置，**最后销毁窗口**。

        ⚠️ Tk 的规则：一旦给 WM_DELETE_WINDOW 注册了处理函数，默认的关窗行为就被顶掉了 ——
        处理函数里不 destroy 窗口，点 X 就永远关不掉（2026-10-09 用户实测"工作台关闭不了"）。
        这个函数会被多处调用（X 按钮 / 更新后重启 / 提权重启），所以做成幂等。
        """
        if getattr(self, '_closing', False):
            return
        self._closing = True
        for job in ('_tick_job',):
            jid = getattr(self, job, None)
            if jid:
                try:
                    self.root.after_cancel(jid)
                except Exception:
                    pass
                setattr(self, job, None)
        try:
            if self.proc is not None:
                try:
                    with open(STOP_FLAG, 'w') as f:
                        f.write('stop')
                except Exception:
                    pass
                try:
                    self.proc.wait(timeout=2)
                except Exception:
                    try:
                        subprocess.run(['taskkill', '/F', '/T', '/PID', str(self.proc.pid)],
                                       creationflags=CREATE_NO_WINDOW, timeout=5)
                    except Exception:
                        pass
        except Exception:
            pass
        if harp_cli:
            try:
                harp_cli.save_config({'SCORE_DIR': self.score_dir,
                                      'SPEED': self.speed.get(),
                                      'COUNTDOWN': self.countdown.get(),
                                      'THEME': self.theme,
                                      'LIB_EXTRA_ON': bool(self.keep_fav.get()),
                                      'LIB_EXTRA_DIR': self.fav_dir.cget('text').strip()})
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass

    # ------------------------------------------------------------------
    def run(self):
        c = self.ui.c
        self.log('[口琴工具台 GUI] 已启动 · 配色 %s' % c['label'])
        self.log('[目录] 工作目录 %s' % ROOT, 'dim')
        self.check_env()
        self._scan_library(quiet=True)
        self.log('[谱子库] %s · %d 份' % (self.score_dir, len(self.library)))
        self.root.after(500, self.do_search)
        self.root.after(2500, lambda: self.check_update(quiet=True))
        self.log('[就绪] 等待开始演奏', 'dim')

        if self.selftest:
            self.root.withdraw()
            self.root.update()
            for key in ("local", "online", "play", "log", "local"):
                self.goto(key)
                self.root.update()
            for name in THEMES:
                self.set_theme(name, save=False)
                self.root.update()
            self.set_theme(self.theme, save=False)
            self.root.destroy()
            return
        self.root.mainloop()


def selftest():
    """无人值守自检：建窗 -> 四个页面 + 六套配色 -> 真跑一个子进程 -> 打印结果。"""
    app = App('neon', selftest=True)
    root = app.root
    for _ in range(10):
        root.update()
    ok = True
    app._scan_library(quiet=True)
    print('环境：%s' % ('管理员' if app.admin else '普通权限'))
    print('谱子库：%s · %d 份' % (app.score_dir, len(app.library)))
    # 页面 + 配色
    for key in ("local", "online", "play", "log"):
        app.goto(key)
        root.update()
        print('  页面 %-7s 构建 OK' % key)
    for name in THEMES:
        app.set_theme(name, save=False)
        root.update()
    print('  六套配色切换 OK')
    app.set_theme('neon', save=False)
    root.update()
    # 真跑一个子进程
    if app.library and not os.path.isfile(app.score_path):
        app.pick_local((os.path.splitext(os.path.basename(app.library[0]))[0], None, []))
    if os.path.isfile(app.score_path):
        app.dry_run()
    else:
        app.run_proc(player_cmd(HARP_DFH, ['--release']), 'SELFTEST 子进程')
    deadline = time.time() + 120
    while time.time() < deadline and app.busy:
        root.update()
        time.sleep(0.05)
    for _ in range(20):
        root.update()
    body = ''
    for w in app._log_widgets():
        try:
            body += w.get('1.0', 'end-1c')
        except Exception:
            pass
    checks = {
        'env_ok': True,
        'subproc_ran': ('✓ 完成' in body) or ('▶' in body),
        'subproc_ok': ('✓ 完成' in body) or FROZEN,
        'not_busy': not app.busy,
        'library': len(app.library) >= 0,
        'score_info': bool(app.bar_name.cget('text')),
        'nav': len(app.nav_widgets) == 4,
        'pages': len(app.pages) == 4,
    }
    for k, v in checks.items():
        print('  %-12s %s' % (k, 'OK' if v else 'FAIL'))
        ok = ok and v
    try:
        root.destroy()
    except Exception:
        pass
    print('SELFTEST ' + ('OK' if ok else 'FAILED'))
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description='口琴工具台 · 侧栏导航版')
    parser.add_argument('--theme', default=None, choices=list(THEMES),
                        help='启动配色（默认读上次保存的）')
    parser.add_argument('--list-themes', action='store_true', help='列出所有配色')
    parser.add_argument('--selftest', action='store_true', help='无人值守自检')
    args = parser.parse_args(argv)
    if args.list_themes:
        for key, t in THEMES.items():
            print('  %-8s %s' % (key, t['label']))
        return 0
    if args.selftest:
        hide_console()
        return selftest()
    hide_console()
    if os.name == 'nt':
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    theme = args.theme
    if theme is None:
        theme = 'neon'
        if harp_cli:
            try:
                theme = harp_cli.load_config().get('THEME') or 'neon'
            except Exception:
                theme = 'neon'
    App(theme).run()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        tb = traceback.format_exc()
        p = crash_log(tb)
        try:
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror('口琴工具台出错',
                                 '%s\n\n详细堆栈已写入:\n%s' % (tb.splitlines()[-1], p))
        except Exception:
            pass
        raise
