#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
harp_dfh.py —— 三角洲行动 · 口琴自动演奏（只吃成品站导出的三行式谱面）

谱面长这样（小节头 + 三行）:
    小节   1 (4/4)
      简谱  6     #4    6     7
      键位  N     V#    N     M
      节奏  0.45b 0.45b 0.45b 0.9b

  键位 = 按哪个键 + 同时按住哪个鼠标键:
      N   = 按 N 键（= 简谱 6），不按鼠标
      V#  = 按 V 键 + 按住中键(半音)
      X+  = 按 X 键 + 按住右键(升调=高音区)
      V+# = 按 V 键 + 同时按住右键和中键
      '+' 升调(鼠标右键)   '-' 降调(鼠标左键)   '#' 半音(鼠标中键)
  节奏:
      0.45b / 2.3b = 0.45 / 2.3 拍        ('b' = 拍)
      4 / 8 / 16   = 四分 / 八分 / 十六分音符  (= 1 / 0.5 / 0.25 拍)
      8· / 4·      = 附点八分 / 附点四分      (= 0.75 / 1.5 拍)

用法:
    python harp_dfh.py 谱子.txt --dry-run        # 先干跑！只打印, 不发按键
    python harp_dfh.py 谱子.txt --plan           # 打印逐音符时间表(核对节奏)
    python harp_dfh.py 谱子.txt                  # 正式演奏(Esc 急停)
    python harp_dfh.py 谱子.txt --from-bar 17    # 从第 17 小节开始(练一句)
    python harp_dfh.py 谱子.txt --speed 0.8      # 整体放慢

依赖: pip install pynput

★ 必须【以管理员身份运行】。三角洲行动(ACE)是管理员权限, 而 Windows 的 UIPI 会把普通权限
  进程发出的按键【静默丢弃】—— 表现就是"游戏里毫无反应, 也不报错"。脚本启动时会自检并提示。
★ 演奏前把输入法切英文, 并在游戏里先打开口琴界面。
"""

import argparse
import atexit
import os
import re
import subprocess
import sys
import time
import threading

try:                      # 让"输出被重定向/打包成 exe 被管道接走"时也不乱码
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

# ============================== 配置区 ==============================
RELEASE_GAP = 0.02      # 音符过长时留的最小间隙(秒); 正常情况下靠绝对时间轴对齐, 不累积
LINE_PAUSE = 0.0        # 行末停顿(本格式靠 小节 对齐, 一般不用)
COUNTDOWN = 3           # 倒计时秒数
WAIT_FOR_GAME = True    # 倒计时后等游戏窗口到前台再开始
SPEED = 1.0             # >1 更快, <1 更慢
DEFAULT_BPM = 120.0     # 谱面头部没写 BPM 时的默认值
MOD_BUTTON = {'+': 'right', '-': 'left', '#': 'middle'}
# 休止符: 这些"键位"不按键, 只推进时间(简谱用 0 表示休止)
REST_KEYS = {'0', 'r', 'rest', '~'}
MIN_NOTE = 0.03         # 一个音最短按住多久(秒), 防止 0 长度
# 打包成 exe(PyInstaller)后 __file__ 会指向临时解包目录, 所以基准目录要特殊处理
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 停止文件: GUI 点"急停"时写它 —— 让我们体面退出(会松开所有键),
# 而不是直接 taskkill。强杀时进程没机会松开鼠标键 = 鼠标卡住。
STOP_FILE = os.path.join(BASE_DIR, '.stop')
# ★ 修饰键只在"这个音"期间按住, 音一结束立刻松开。
#   若改成 False(跨音符一直按住), 脚本一旦异常退出/被强杀, 鼠标键就会卡在按下状态,
#   表现为"左右键失灵" —— 所以默认 True, 安全优先。
MOD_HOLD_WITH_NOTE = True
MOD_LEAD = 0.02         # 比音符提前多久按下修饰键(秒)
# ===================================================================

# ---------------------------------------------------------- 安全兜底
# 不管怎么退出(异常 / 关窗口 / 被杀 / Esc), 都必须把鼠标键松开。
# ★ 但只松开"确实按着"的键: 无条件松开会产生落单的"抬起"事件,
#   有些程序会把它当成一次点击 —— 用户就报告过"莫名自己点右键"。
VK_BUTTON = {'left': 0x01, 'right': 0x02, 'middle': 0x04}
BTN_UP_FLAG = {'left': 0x0004, 'right': 0x0010, 'middle': 0x0020}


def button_down(name):
    """这个鼠标键此刻是否真的处于按下状态(注入的事件也算)。"""
    if not sys.platform.startswith('win'):
        return False
    try:
        import ctypes
        return bool(ctypes.windll.user32.GetAsyncKeyState(VK_BUTTON[name]) & 0x8000)
    except Exception:
        return False


def emergency_release(force=False):
    """松开鼠标键。

    force=False(默认) 只松开**确实按着**的键；force=True 才无脑全松。
    """
    if not sys.platform.startswith('win'):
        return
    names = list(VK_BUTTON) if force else [n for n in VK_BUTTON if button_down(n)]
    if not names:
        return
    try:
        from pynput.mouse import Controller as Ms, Button
        m = Ms()
        for n in names:
            try:
                m.release(getattr(Button, n))
            except Exception:
                pass
    except Exception:
        try:                                    # pynput 都不可用就走 Win32 API
            import ctypes
            u = ctypes.windll.user32
            for n in names:
                u.mouse_event(BTN_UP_FLAG[n], 0, 0, 0, 0)
        except Exception:
            pass


atexit.register(emergency_release)

if sys.platform.startswith('win'):
    try:
        import ctypes
        _CTRL = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

        def _on_console_event(_evt):
            emergency_release()             # 关窗口/注销/关机也要松开
            return False                    # 继续走默认处理

        _ctrl_handler = _CTRL(_on_console_event)    # 必须留引用, 否则会被 GC
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_ctrl_handler, True)
    except Exception:
        pass


IS_WINDOWS = sys.platform.startswith('win')
if IS_WINDOWS:
    import ctypes
    _u32 = ctypes.windll.user32
    _k32 = ctypes.windll.kernel32
    _adv = ctypes.windll.advapi32
    try:
        _shell32 = ctypes.windll.shell32
    except Exception:
        _shell32 = None

stop_flag = threading.Event()
_DRY_RUN = False          # 干跑：不按真实时间轴走，直接把按键序列打印完


# ============================================================ 解析
def rhythm_beats(tok):
    """节奏记号 -> 拍数。"""
    t = tok.strip().lower()
    if not t:
        return 1.0
    if t.endswith('b'):                                  # 0.45b / 2.3b
        try:
            return float(t[:-1])
        except ValueError:
            return 1.0
    dot = ('·' in t) or ('.' in t)
    num = t.replace('·', '').replace('.', '')
    if num.isdigit() and int(num) > 0:                   # 4=1拍 8=0.5拍 16=0.25拍
        return (4.0 / float(num)) * (1.5 if dot else 1.0)
    return 1.0


def split_key(tok):
    """'V+#' -> 键 'v' + 修饰符 {'+','#'}"""
    t = tok.strip()
    mods = set()
    while t and t[-1] in MOD_BUTTON:
        mods.add(t[-1])
        t = t[:-1]
    key = t.lower()
    return (key if key else '?'), frozenset(mods)


ROW_LABELS = ('简谱', '键位', '节奏')
BAR_RE = re.compile(r'^小节\s*(\d+)\s*(?:\((\d+)\s*/\s*(\d+)\))?')


def parse_score(text):
    """解析三行式谱面 -> (notes, info)

    notes: [{'bar','beat','sec_start','sec','key','mods','beats'}]   已按绝对时间轴排好
    info : {'bpm','bars','ts','notes','title','transpose'}
    """
    title = ''
    bpm = None
    declared_notes = None
    declared_bars = None
    transpose = 0
    for line in text.splitlines()[:6]:                   # 头部信息
        if not title and line.strip() and '小节' not in line:
            title = line.strip()
        m = re.search(r'BPM\s*([0-9.]+)', line)
        if m:
            bpm = float(m.group(1))
        m = re.search(r'([0-9]+)\s*音符', line)
        if m:
            declared_notes = int(m.group(1))
        m = re.search(r'([0-9]+)\s*小节', line)
        if m:
            declared_bars = int(m.group(1))
        m = re.search(r'移调\s*(-?[0-9]+)', line)
        if m:
            transpose = int(m.group(1))
    if bpm is None:
        bpm = DEFAULT_BPM

    bars = []            # [{'no':1,'beats':4.0,'keys':[...],'rhy':[...]}]
    cur = None
    last_row = None

    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        m = BAR_RE.match(s)
        if m:
            if cur:
                bars.append(cur)
            ts = (int(m.group(2)), int(m.group(3))) if m.group(2) else (4, 4)
            cur = {'no': int(m.group(1)), 'beats': ts[0] * 4.0 / ts[1],
                   'keys': [], 'rhy': []}
            last_row = None
            continue
        if cur is None:
            continue
        hit = None
        for lab in ROW_LABELS:
            if s.startswith(lab):
                hit = lab
                rest = s[len(lab):]
                break
        if hit:                                          # 新的一行
            last_row = hit
            words = rest.split()
        else:                                            # 没标签 => 上一行的续行(排版折行)
            words = s.split()
            if last_row is None or not words:
                continue
            if any(c in ''.join(words) for c in '：:，。'):
                continue
        if last_row == '键位':
            cur['keys'].extend(words)
        elif last_row == '节奏':
            cur['rhy'].extend(words)
    if cur:
        bars.append(cur)

    # ---- 排到绝对时间轴: 每小节从头算, 音符尾不许越过下一个音 ----
    notes = []
    beat_cursor = 0.0                                    # 全局拍位置
    for b in bars:
        t = 0.0                                          # 小节内相对拍
        for i, k in enumerate(b['keys']):
            beats = rhythm_beats(b['rhy'][i]) if i < len(b['rhy']) else 1.0
            key, mods = split_key(k)
            notes.append({'bar': b['no'], 'beat_in_bar': t,
                          'abs_beat': beat_cursor + t,
                          'beats': beats, 'key': key, 'mods': mods})
            t += beats
        beat_cursor += b['beats']                        # 下一小节从网格点开始
    # 后一个音开始之前, 前一个音必须结束(单音口琴不能重叠)
    for i in range(len(notes) - 1):
        limit = notes[i + 1]['abs_beat'] - notes[i]['abs_beat']
        if notes[i]['beats'] > limit:
            notes[i]['beats'] = max(limit, 0.05)
    return notes, {'bpm': bpm, 'bars': len(bars), 'title': title,
                   'declared_notes': declared_notes, 'declared_bars': declared_bars,
                   'transpose': transpose}


# ============================================================ 系统检查
def window_pid(hwnd):
    try:
        pid = ctypes.c_ulong(0)
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def pid_elevated(pid):
    if not IS_WINDOWS or not pid:
        return False
    h = _k32.OpenProcess(0x1000, False, int(pid))
    if not h:
        return None
    try:
        tok = ctypes.c_void_p()
        if not _adv.OpenProcessToken(h, 0x0008, ctypes.byref(tok)):
            return None
        try:
            info = ctypes.c_int(0)
            ret = ctypes.c_ulong(0)
            ok = _adv.GetTokenInformation(tok, 20, ctypes.byref(info), 4, ctypes.byref(ret))
            return bool(info.value) if ok else None
        finally:
            _k32.CloseHandle(tok)
    finally:
        _k32.CloseHandle(h)


def is_admin():
    if not IS_WINDOWS:
        return True
    state = pid_elevated(os.getpid())
    if state is not None:
        return state
    try:
        return bool(_shell32.IsUserAnAdmin()) if _shell32 else False
    except Exception:
        return False


def relaunch_as_admin():
    """用管理员身份重新启动自己(会弹 UAC)。因为游戏是管理员权限, 不提权按键必被丢弃。"""
    if not IS_WINDOWS:
        return False
    try:
        params = subprocess.list2cmdline([os.path.abspath(__file__)] + sys.argv[1:])
        rc = ctypes.windll.shell32.ShellExecuteW(None, 'runas', sys.executable,
                                                params, os.getcwd(), 1)
        return int(rc) > 32          # >32 = 成功
    except Exception as e:
        print('提权失败: %s' % e)
        return False


def preflight():
    if not IS_WINDOWS:
        return
    try:
        if _u32.GetKeyboardLayout(0) & 0xFFFF in (0x0804, 0x0404, 0x0C04, 0x1004):
            print('[!] 检测到中文输入法 —— 按键会被吃掉, 开工前按 Shift 切英文。')
    except Exception:
        pass
    if is_admin():
        print('[i] 已是管理员权限 ✓ 模拟按键可以送达提权窗口。')
    else:
        print('[!] 当前【不是管理员】—— 这会让游戏里完全没有反应。')
        print('    三角洲行动是管理员权限启动的, Windows 的 UIPI 会静默丢弃普通权限进程的按键。')
        print('    请关掉本窗口, 改为【以管理员身份运行】再试。')


# ============================================================ 演奏
def stop_requested():
    """停止条件: Esc/急停置位, 或者 GUI 写了停止文件(这样它能请我们体面退出,
    而不是直接 taskkill —— 强杀会让鼠标键卡在按下状态)。"""
    return stop_flag.is_set() or os.path.exists(STOP_FILE)


def wait_until(t_target):
    if _DRY_RUN:             # 干跑不睡，直接把整首歌的按键序列打完
        return not stop_requested()
    while True:
        if stop_requested():
            return False
        left = t_target - time.perf_counter()
        if left <= 0:
            return True
        time.sleep(min(left, 0.05))


class HoldWatchdog(threading.Thread):
    """看门狗: 万一主循环卡住/抛异常, 按住超过上限就强行松开, 绝不把鼠标键留在按下状态。"""

    def __init__(self, state, max_hold, on_release):
        threading.Thread.__init__(self, daemon=True)
        self.state = state          # {'name': 按钮名 或 None, 'since': 时间}
        self.max_hold = max_hold
        self.on_release = on_release
        self._done = False

    def stop(self):
        self._done = True

    def run(self):
        while not self._done and not stop_flag.is_set():
            name = self.state.get('name')
            if name and (time.perf_counter() - self.state.get('since', 0)) > self.max_hold:
                try:
                    self.on_release(name)
                    print('\n[看门狗] 鼠标键 %s 按住超过 %.1f 秒, 已强制松开' % (name, self.max_hold))
                except Exception:
                    pass
                self.state['name'] = None
            time.sleep(0.1)


def start_panic_hotkey():
    """注册全局热键 F12 = 立刻松开所有鼠标键并停止(鼠标卡住时键盘还能用)。"""
    if not IS_WINDOWS:
        return
    try:
        import ctypes.wintypes as wt
    except Exception:
        return

    def worker():
        u = ctypes.windll.user32
        if not u.RegisterHotKey(None, 1, 0, 0x7B):        # VK_F12
            return
        msg = wt.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:                     # WM_HOTKEY
                emergency_release()
                stop_flag.set()
                print('\n[F12] 已松开所有鼠标键并停止')
                return

    threading.Thread(target=worker, daemon=True).start()


def play(notes, kb, mouse, dry_run, speed, bpm):
    """绝对时间轴演奏: 每个音按谱面时间点起音, 误差不累积。"""
    from pynput.mouse import Button
    beat_sec = 60.0 / max(bpm, 1.0) / max(speed, 0.01)
    t0 = time.perf_counter()
    held_keys = set()
    held_btns = set()
    n_switch = 0
    state = {'name': None, 'since': 0.0}          # 给看门狗看的"当前按住的键"

    def set_mods(mods):
        nonlocal n_switch
        want = set(MOD_BUTTON[m] for m in mods)
        if want == held_btns:
            return
        n_switch += 1
        if dry_run:
            held_btns.clear()
            held_btns.update(want)
            return
        for name in list(held_btns - want):
            try:
                mouse.release(getattr(Button, name))
            except Exception:
                pass
        for name in (want - held_btns):
            try:
                mouse.press(getattr(Button, name))
            except Exception:
                pass
        held_btns.clear()
        held_btns.update(want)

    def mods_on(mods):
        """按下这个音需要的修饰键。"""
        if dry_run:
            return
        for m in mods:
            name = MOD_BUTTON[m]
            try:
                mouse.press(getattr(Button, name))
            except Exception:
                pass
            held_btns.add(name)
        if mods:
            state['name'] = MOD_BUTTON[sorted(mods)[0]]
            state['since'] = time.perf_counter()

    def mods_off(mods):
        """立刻松开修饰键 —— 音一结束就松, 绝不长期霸占鼠标。"""
        if dry_run:
            return
        for m in mods:
            name = MOD_BUTTON[m]
            try:
                mouse.release(getattr(Button, name))
            except Exception:
                pass
            held_btns.discard(name)
        state['name'] = None

    global _DRY_RUN
    _DRY_RUN = bool(dry_run)
    if dry_run:
        print('（干跑：立即打印全部按键，不按真实时间走）')
    emergency_release()          # 进演奏前先清一次, 防止上一轮残留

    watchdog = None
    if not dry_run and notes:
        # 上限 = 最长音的时长 ×2 + 1 秒; 正常绝不触发, 只在卡住/异常时兜底
        max_hold = max(3.0, max(n['beats'] for n in notes) * beat_sec * 2 + 1.0)
        watchdog = HoldWatchdog(state, max_hold,
                                lambda nm: mouse.release(getattr(Button, nm)))
        watchdog.start()

    # 演奏期间盯住"前台窗口还是不是原来那个程序":
    # 我们的"右键按下→抬起"在系统看来就是一次真右键点击, 一旦你切走了窗口,
    # 这些点击就会落到别的程序上(弹右键菜单之类)。所以一变就立刻停。
    target_pid = [0]

    def foreground_changed():
        if dry_run or not IS_WINDOWS:
            return False
        fg = _u32.GetForegroundWindow()
        pid = window_pid(fg) if fg else 0
        if not pid:
            return False
        if not target_pid[0]:            # 第一次发鼠标动作时记下目标(此刻应已在游戏里)
            target_pid[0] = pid
            return False
        return pid != target_pid[0]

    try:
        for n in notes:
            if stop_flag.is_set():
                break
            # ★ 只在"要发鼠标键"的时候检查前台窗口：一旦你切走了, 立刻停,
            #   否则这些右键点击会落到别的程序上(用户报过"莫名自己点右键")
            if n['mods'] and not dry_run and foreground_changed():
                print('')
                print('[!] 前台窗口已经不是开始演奏时那个程序了(你把窗口切走了?)')
                print('    立即停止 —— 继续发鼠标键会点到别的窗口上(弹右键菜单之类)')
                stop_flag.set()
                break
            start = t0 + n['abs_beat'] * beat_sec
            hold = max(n['beats'] * beat_sec - RELEASE_GAP, MIN_NOTE)
            mods = n['mods']
            if not MOD_HOLD_WITH_NOTE:
                set_mods(mods)                          # 旧行为: 跨音符一直按住
                if not wait_until(start):
                    break
            else:                                       # 安全行为: 只在这个音期间按住
                if mods:
                    if not wait_until(start - MOD_LEAD):
                        break
                    mods_on(mods)
                if not wait_until(start):
                    break
            if mods:
                n_switch += 1
            if dry_run:
                mark = ''.join(sorted(mods))
                sys.stdout.write(('[%s]' % mark if mark else '') +
                                 ('~' if n['key'] in REST_KEYS else n['key']))
                sys.stdout.flush()
                continue
            if n['key'] in REST_KEYS:            # 休止: 不按键, 只走时间
                wait_until(start + hold)
                continue
            kb.press(n['key'])
            held_keys.add(n['key'])
            wait_until(start + hold)
            kb.release(n['key'])
            held_keys.discard(n['key'])
            if MOD_HOLD_WITH_NOTE and mods:
                mods_off(mods)                          # ← 立刻松开鼠标键
    finally:
        if watchdog is not None:
            watchdog.stop()
        if not dry_run:                             # 任何情况下都不能把键留在按下状态
            for k in list(held_keys):
                try:
                    kb.release(k)
                except Exception:
                    pass
            for name in list(held_btns):
                try:
                    mouse.release(getattr(Button, name))
                except Exception:
                    pass
            state['name'] = None
            emergency_release()                     # 最后再无条件清一次
    used = time.perf_counter() - t0
    expect = (notes[-1]['abs_beat'] + notes[-1]['beats']) * beat_sec if notes else 0
    return n_switch, used, expect


def countdown_and_wait(countdown):
    my_hwnd = 0
    if IS_WINDOWS:
        try:
            my_hwnd = _k32.GetConsoleWindow() or _u32.GetForegroundWindow()
        except Exception:
            my_hwnd = 0
    for i in range(countdown, 0, -1):
        print('  %d ...' % i)
        time.sleep(1)

    if IS_WINDOWS and WAIT_FOR_GAME:
        def fg_is_other():
            fg = _u32.GetForegroundWindow()
            return bool(fg) and fg != my_hwnd

        if not fg_is_other():
            print('等你切到游戏窗口...(最多 60 秒)')
            waited = 0.0
            while not fg_is_other() and waited < 60:
                time.sleep(0.2)
                waited += 0.2
            if fg_is_other():
                time.sleep(0.8)

    if IS_WINDOWS and not is_admin():
        fg = _u32.GetForegroundWindow()
        state = pid_elevated(window_pid(fg)) if fg else False
        if state is not False:
            print('')
            print('[X] 前台窗口属于【%s】的进程, 而本脚本是普通权限 —— 按键会被 UIPI 丢弃。'
                  % ('管理员' if state else '无法读取权限(多半更高)'))
            print('    请关掉本窗口, 右键 → 以管理员身份运行, 再来一次。')
            return False
    return True


# ============================================================ main
def build_stamp():
    """这个 exe / 脚本是什么时候构建的 —— 用来确认"我跑的是不是最新版"。"""
    try:
        import datetime
        p = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)
        t = datetime.datetime.fromtimestamp(os.path.getmtime(p))
        return t.strftime('%Y-%m-%d %H:%M')
    except Exception:
        return '?'


def brand():
    """署名 —— 控制台/日志开头那几行。"""
    print('=' * 52)
    print('  明晚一定早睡 · 口琴自动演奏    (三角洲行动)')
    print('  构建 %s%s' % (build_stamp(), '   [打包版]' if getattr(sys, 'frozen', False) else ''))
    print('=' * 52)


def main():
    brand()
    ap = argparse.ArgumentParser(description='明晚一定早睡 · 三角洲行动口琴自动演奏')
    ap.add_argument('score', nargs='?', help='谱子文件(UTF-8)')
    ap.add_argument('--song', help='直接给谱面文本')
    ap.add_argument('--dry-run', action='store_true', help='只打印按键序列, 不真的发按键')
    ap.add_argument('--plan', action='store_true', help='打印逐音符时间表(核对节奏用)')
    ap.add_argument('--bpm', type=float, help='覆盖谱面里的 BPM')
    ap.add_argument('--speed', type=float, default=SPEED, help='整体速度倍率, >1 更快')
    ap.add_argument('--countdown', type=int, default=COUNTDOWN)
    ap.add_argument('--from-bar', type=int, help='从第 N 小节开始(练一句用)')
    ap.add_argument('--to-bar', type=int, help='到第 N 小节结束')
    ap.add_argument('--no-wait', action='store_true', help='不等游戏窗口')
    ap.add_argument('--no-elevate', action='store_true',
                    help='不要自动用管理员身份重启(默认会自动提权, 因为游戏是管理员权限)')
    ap.add_argument('--release', action='store_true',
                    help='急救: 只松开鼠标左中右三键然后退出(万一键卡住了, 先跑这个)')
    ap.add_argument('--hold-mods', action='store_true',
                    help='修饰键跨音符一直按住(旧行为, 更快但异常退出可能卡住鼠标键)')
    a = ap.parse_args()

    global WAIT_FOR_GAME, MOD_HOLD_WITH_NOTE
    if a.no_wait:
        WAIT_FOR_GAME = False
    if a.hold_mods:
        MOD_HOLD_WITH_NOTE = False

    if a.release:
        emergency_release()
        print('已松开鼠标左键/右键/中键(以及可能的键盘残留)。')
        print('如果鼠标还是失灵, 对着任意窗口单击一下那个键, 或按 Ctrl+Alt+Del 再 Esc。')
        return 0

    if a.song:
        text, src = a.song, '(命令行)'
    elif a.score:
        if not os.path.isfile(a.score):
            sys.exit('找不到谱子文件: %s' % a.score)
        with open(a.score, 'r', encoding='utf-8-sig') as f:
            text = f.read()
        src = a.score
    else:
        sys.exit('用法: python harp_dfh.py 谱子.txt --dry-run')

    notes, info = parse_score(text)
    if a.bpm:
        info['bpm'] = a.bpm
    if a.from_bar:
        notes = [n for n in notes if n['bar'] >= a.from_bar]
    if a.to_bar:
        notes = [n for n in notes if n['bar'] <= a.to_bar]
    if not notes:
        sys.exit('没解析出音符 —— 确认这是"小节/简谱/键位/节奏"三行式谱面?')

    if notes:                                   # 截取片段后重新对齐时间轴
        base = notes[0]['abs_beat']
        for n in notes:
            n['abs_beat'] -= base

    beat_sec = 60.0 / max(info['bpm'], 1.0) / max(a.speed, 0.01)
    total = (notes[-1]['abs_beat'] + notes[-1]['beats']) * beat_sec
    print('谱子: %s' % src)
    if info['title']:
        print('       %s' % info['title'])
    print('       BPM %g   小节 %d   音符 %d   时长 %.1f 秒   移调 %d'
          % (info['bpm'], info['bars'], len(notes), total, info['transpose']))
    if info['declared_notes'] and info['declared_notes'] != len(notes) and not a.from_bar:
        print('[i] 谱面头部写的是 %d 个音符, 实际解析出 %d 个(可能有折行或省略)'
              % (info['declared_notes'], len(notes)))
    if info['transpose']:
        print('[!] 谱面移调 %d —— 本脚本不额外移调, 请确认游戏里的调性设置' % info['transpose'])

    if a.plan:
        print('\n  小节  拍位   键   修饰  时长(拍)  时长(秒)')
        for n in notes[:400]:
            print('  %4d  %5.2f  %-3s  %-4s  %7.2f  %7.3f'
                  % (n['bar'], n['beat_in_bar'], n['key'],
                     ''.join(sorted(n['mods'])) or '-', n['beats'], n['beats'] * beat_sec))
        print('')

    if a.dry_run:
        print('== DRY RUN, 不会真的按键 ==')
        print('按键序列: ', end='')
        sw, used, expect = play(notes, None, None, True, a.speed, info['bpm'])
        print('\n音区动作 %d 次' % sw)
        return 0

    preflight()

    # 游戏是管理员权限, 普通权限发按键会被 UIPI 静默丢弃 —— 所以正式演奏前自动提权重启
    if IS_WINDOWS and not a.no_elevate and not is_admin():
        print('')
        print('需要管理员权限才能把按键送进游戏(否则游戏里毫无反应)。正在提权重启...')
        print('请在 UAC 弹窗上点“是”。')
        if relaunch_as_admin():
            return 0
        print('自动提权没成功, 请手动: 右键这个脚本所在窗口 → 以管理员身份运行。')
        return 2

    from pynput.keyboard import Controller as Kb, Key, Listener
    from pynput.mouse import Controller as Ms
    kb, mouse = Kb(), Ms()

    if os.path.exists(STOP_FILE):            # 清掉上一轮可能留下的停止文件
        try:
            os.remove(STOP_FILE)
        except Exception:
            pass

    def on_press(key):
        if key == Key.esc:
            stop_flag.set()
            print('\n[Esc] 急停!')
            return False

    listener = Listener(on_press=on_press)
    listener.start()
    start_panic_hotkey()                     # F12 = 立刻松开所有鼠标键并停止
    if not countdown_and_wait(a.countdown):
        listener.stop()
        return 2

    print('▶ 开始演奏 (Esc 或 F12 急停；鼠标万卡住就按 F12)')
    try:
        sw, used, expect = play(notes, kb, mouse, False, a.speed, info['bpm'])
        print('结束: 音符 %d / 音区动作 %d 次 / 用时 %.1fs (谱面 %.1fs, 偏差 %+.2fs)'
              % (len(notes), sw, used, expect, used - expect))
    finally:
        emergency_release()                  # 正常结束也再清一次, 图个安心
        listener.stop()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        print('\n已中断')
