# -*- coding: utf-8 -*-
"""
harp2.py —— 用模拟键鼠自动演奏口琴谱(游戏内口琴)

用法:
    python harp2.py                        演奏内置示例 SONG
    python harp2.py harp_score.txt         直接读谱文件(推荐)
    python harp2.py harp_score.txt --dry-run    只打印不按键(先验证!)
    python harp2.py harp_score.txt --speed 0.8  整体放慢(>1 更快)
    python harp2.py harp_score.txt --no-wait    不做"等游戏窗口到前台"

流程: 打印倒计时 → (可选)等游戏窗口成为前台 → 开始按键 → 按 Esc 随时急停

谱面 token:
    L N R M           切音区 —— 低音区(按住左键) / 中音区(不按) / 高音区(按住右键) / 半音(按住中键)
                      按住期间一直生效, 松开就回中音区
    1 2 3 4 5 6 7 i   音符(按键 Z X C V B N M ,)
    <音符>--          音符后跟 '-' 多按 1 拍, '.' 多按 0.5 拍 (如 z- / 5-- / 3.)
    v                 换气(短停顿)
    _ 或 0            停顿
    - 或 .(单独)      给上一个音延长 1 拍 / 0.5 拍
    z x c n m b ,     也直接支持原始按键字母(v 除外, v 是换气记号)
    |                 小节线, 忽略        // 开头 = 注释
"""

import argparse
import atexit
import json
import os
import re
import sys
import time
import threading

try:                      # 让重定向到文件时也是 UTF-8, 不再乱码
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

# ============================ 可调参数 ============================
NOTE_TIME = 0.35        # 一个音(1 拍)的时长(秒)。按住时间 = NOTE_TIME * 拍的倍数
RELEASE_GAP = 0.03      # 松开音符键后的微小间隔, 防连音
LINE_PAUSE = 0.45       # 行末换气
BREATH_PAUSE = 0.20     # 谱面里 'v' 的换气停顿
PAUSE_TIME = 0.35       # 谱面里 '_' 的停顿
COUNTDOWN = 3           # 倒计时秒数
HOLD_NOTE = True        # True=按住音符键直到时值结束(游戏里音会延续); False=只点一下
WAIT_FOR_FOREGROUND = True   # 倒计时后等"游戏窗口到前台"再开始
MOUSE_PAUSE = 0.08      # 按下/松开"音区键"后的稳定时间(秒)
FOREGROUND_GRACE = 0.8  # 窗口切过来后额外等多久(秒)
SPEED = 1.0             # >1 更快, <1 更慢
REST_TIME = None        # 兼容旧参数: None = 用 PAUSE_TIME

# --- 音区: 靠【按住鼠标键】切换, 按住期间一直生效(不是点一下换一次!) ---
#   L = 低音区(按住左键)   N = 中音区(什么都不按)   R = 高音区(按住右键)
#   M = 半音(按住中键)
REGISTERS = {
    'L': ('left', -1),
    'N': (None, 0),
    'R': ('right', 1),
    'M': ('middle', 0),
}
DEFAULT_REGISTER = 'N'

# --- 下面这几个只给"诊断/校准"工具用(探测游戏到底是点击式还是按住式) ---
OCTAVE_UP = 'right'
OCTAVE_DOWN = 'left'
SEMITONE = 'middle'
OCTAVE_CLICKS = 1
SEMITONE_CLICKS = 1
TUNE_MODE = 'click'          # 'click' 点一下 / 'hold' 按住滑音
HOLD_MS_PER_OCTAVE = 400
HOLD_MS_PER_SEMITONE = 33
TUNE_DURING_PAUSE = True

# 校准结果存放处(harp.cmd calibrate 或 GUI 的"音区校准"会写它)
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'harp_config.json')
_TUNABLE = ('OCTAVE_UP', 'OCTAVE_DOWN', 'SEMITONE', 'OCTAVE_CLICKS', 'SEMITONE_CLICKS',
            'TUNE_MODE', 'HOLD_MS_PER_OCTAVE', 'HOLD_MS_PER_SEMITONE',
            'NOTE_TIME', 'KEY_HOLD', 'MOUSE_PAUSE', 'LINE_PAUSE', 'PAUSE_TIME',
            'BREATH_PAUSE', 'RELEASE_GAP', 'HOLD_NOTE')


def load_config():
    """把校准结果覆盖到上面的默认值; 文件不存在就什么也不做。"""
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        # utf-8-sig: 记事本另存会加 BOM, 直接 utf-8 读会解析失败
        with open(CONFIG_PATH, 'r', encoding='utf-8-sig') as f:
            cfg = json.load(f)
    except Exception as e:
        print('[!] 校准文件读不了, 已忽略(用默认值): %s' % e)
        print('    文件: %s' % CONFIG_PATH)
        return {}
    if not isinstance(cfg, dict):
        print('[!] 校准文件格式不对(应该是一个 JSON 对象), 已忽略')
        return {}
    for name in _TUNABLE:
        # 兼容大小写两种写法: 写 "OCTAVE_CLICKS" 或 "octave_clicks" 都认
        for key in (name, name.lower()):
            if key in cfg:
                globals()[name] = cfg[key]
                break
    return cfg

# 简谱数字 -> 键盘
KEYMAP = {
    '1': 'z', '2': 'x', '3': 'c', '4': 'v',
    '5': 'b', '6': 'n', '7': 'm', 'i': ',',
    '8': ',',            # 有些谱子把高音 do 写成 8
}
RAW_KEYS = set('zxcnmb,')   # 注意: 不含 v —— v 是"换气"记号(v 键请用数字 4)
NOTE_CHARS = dict(KEYMAP)
NOTE_CHARS.update({k: k for k in RAW_KEYS})
MOUSE_ACTIONS = ('left', 'right', 'middle')
WHEEL_ACTIONS = ('wheel_up', 'wheel_down')
TUNE_MARKS = {'right': '<R>', 'left': '<L>', 'middle': '<M>',
              'wheel_up': '<W+>', 'wheel_down': '<W->'}

# 内置示例(没给文件时演奏这个; 也可替换成自己的谱)
SONG = """
1 2 3 5 | 6 5 3 - | 2 3 5 6 | i - - -
"""
# ==================================================================

# -------------------------------------------------------- 安全兜底
# 音区是"按住鼠标键"实现的 —— 万一脚本异常退出/被强杀, 鼠标键会卡在按下状态,
# 用户会感觉"鼠标左右键失灵"。所以任何退出路径都必须松开鼠标键。
# 停止文件: GUI 点"急停"时写它, 让我们体面退出(会松开所有键), 而不是直接 taskkill。
STOP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.stop')


# ★ 只松开"确实按着"的键: 无条件松开会产生落单的"抬起"事件,
#   有些程序会把它当成一次点击 —— 用户报过"莫名自己点右键"。
VK_BUTTON = {'left': 0x01, 'right': 0x02, 'middle': 0x04}
BTN_UP_FLAG = {'left': 0x0004, 'right': 0x0010, 'middle': 0x0020}


def button_down(name):
    """这个鼠标键此刻是否真的按着(注入的事件也算)。"""
    if not sys.platform.startswith('win'):
        return False
    try:
        import ctypes
        return bool(ctypes.windll.user32.GetAsyncKeyState(VK_BUTTON[name]) & 0x8000)
    except Exception:
        return False


def emergency_release(force=False):
    """松开鼠标键；默认只松开确实按着的那些。"""
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
        try:
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
            emergency_release()
            return False

        _ctrl_handler = _CTRL(_on_console_event)      # 留引用, 别被 GC
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
        _shell32 = ctypes.windll.shell32      # IsUserAnAdmin 在这里, 不在 user32!
    except Exception:
        _shell32 = None
else:
    _u32 = _k32 = _adv = _shell32 = None

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION = 20


def window_pid(hwnd):
    """某个窗口属于哪个进程。"""
    try:
        pid = ctypes.c_ulong(0)
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def pid_is_elevated(pid):
    """那个进程是否已提权。返回 True / False / None(=判断不了)。

    打不开句柄通常就意味着"对方权限比我们高"(UIPI), 这时返回 None,
    由调用方决定当成"疑似提权"处理 —— 绝不静默放过。
    """
    if not IS_WINDOWS or not pid:
        return False
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return None
    try:
        tok = ctypes.c_void_p()
        if not _adv.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(tok)):
            return None
        try:
            info = ctypes.c_int(0)
            ret = ctypes.c_ulong(0)
            ok = _adv.GetTokenInformation(tok, TOKEN_ELEVATION,
                                          ctypes.byref(info), 4, ctypes.byref(ret))
            return bool(info.value) if ok else None
        finally:
            _k32.CloseHandle(tok)
    finally:
        _k32.CloseHandle(h)


def is_admin():
    """本进程是否已提权(管理员)。

    以"自己进程令牌的提权位"为准 —— 这是唯一可靠的判据。
    (坑: user32 里没有 IsUserAnAdmin, 它在 shell32; 而且判断失败时绝不能
     默认当成"是管理员", 否则 UIPI 检查会被静默跳过, 用户就只见"游戏没反应"。)
    """
    if not IS_WINDOWS:
        return True
    state = pid_is_elevated(os.getpid())
    if state is not None:
        return state
    try:
        return bool(_shell32.IsUserAnAdmin()) if _shell32 else False
    except Exception:
        return False


def elevated_windows_count():
    """有几个"有窗口且已提权"的进程 —— 用来提示用户该用管理员运行。"""
    if not IS_WINDOWS:
        return 0
    n = 0
    seen = set()
    try:
        for pid in _enum_window_pids():
            if pid in seen or pid == os.getpid():
                continue
            seen.add(pid)
            if pid_is_elevated(pid) is True:
                n += 1
    except Exception:
        pass
    return n


def _enum_window_pids():
    """枚举所有顶层窗口所属的进程号。"""
    pids = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _lparam):
        if _u32.IsWindowVisible(hwnd):
            pid = ctypes.c_ulong(0)
            _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                pids.append(int(pid.value))
        return True

    _u32.EnumWindows(WNDENUMPROC(cb), 0)
    return pids


def self_window_handle():
    """本进程"自己的窗口"的句柄。

    控制台启动时就是控制台窗口; 由 GUI(pythonw, 没有控制台)启动时,
    启动瞬间的前台窗口就是那个 GUI 窗口 —— 所以两种方式都能正确判断
    "用户是否已经切走"。返回 0 表示无法判断。
    """
    if not IS_WINDOWS:
        return 0
    try:
        h = _k32.GetConsoleWindow()
        if not h:
            h = _u32.GetForegroundWindow()
        return h
    except Exception:
        return 0


def foreground_is_other(ref):
    """前台窗口既不是控制台也不是启动它的 GUI 窗口 => 认为用户已经切到游戏。"""
    if not IS_WINDOWS:
        return True
    try:
        fg = _u32.GetForegroundWindow()
        if not fg:
            return False
        if not ref:
            return True
        return fg != ref
    except Exception:
        return True


def preflight():
    """一些会让人白忙一场的环境检查。"""
    if not IS_WINDOWS:
        return
    try:
        if _u32.GetKeyboardLayout(0) & 0xFFFF in (0x0804, 0x0404, 0x0C04, 0x1004):
            print('[!] 检测到中文输入法 —— 按键会被输入法吃掉。开工前请按 Shift 切到英文。')
    except Exception:
        pass
    if not is_admin():
        n = elevated_windows_count()
        print('[!] 本工具当前【不是管理员】。')
        if n:
            print('    检测到 %d 个"有窗口且已提权"的进程(游戏很可能就是其中之一)。' % n)
        print('    这种情况下 Windows 的 UIPI 会**静默丢弃**我们发出的所有模拟键鼠 ——')
        print('    游戏里会完全没反应, 而且不报任何错。')
        print('    解决: 关掉本窗口, 改为【以管理员身份运行】(三角洲行动/ACE 必须以管理员启动)。')
    else:
        print('[i] 已是管理员权限, 模拟按键可以送达提权窗口。')


class Player(object):
    def __init__(self, dry_run=False, speed=1.0, no_wait=False):
        self.dry_run = dry_run
        self.speed = speed if speed > 0 else 1.0
        self.no_wait = no_wait
        self.stop = threading.Event()
        self.kb = None
        self.mouse = None
        self.count = {'note': 0, 'rest': 0, 'hold': 0, 'tune': 0}
        self.tune_time = 0.0
        self.cur_reg = DEFAULT_REGISTER     # 当前音区 L/N/R/M
        self.cur_btn = None                 # 当前按住的鼠标键(音区键)
        self.held_keys = set()              # 当前按住的音符键(退出时要松开)
        if not dry_run:
            from pynput.keyboard import Controller as Kb, Listener, Key
            from pynput.mouse import Controller as Ms, Button
            self.kb = Kb()
            self.mouse = Ms()
            self.Button = Button
            self.Key = Key
            self.Listener = Listener

    # ------------------------------------------------------------ 工具
    def wait(self, seconds):
        """可被 Esc / 停止文件打断的 sleep"""
        end = time.time() + seconds
        while time.time() < end:
            if self.stop.is_set() or os.path.exists(STOP_FILE):
                return
            time.sleep(min(0.02, max(0.0, end - time.time())))

    def sleep(self, seconds):
        self.wait(seconds / self.speed)

    def press(self, ch):
        if self.dry_run:
            sys.stdout.write(ch)
            sys.stdout.flush()
            return
        self.kb.press(ch)
        time.sleep(KEY_HOLD)
        self.kb.release(ch)

    def click(self, act, times=1):
        """换音区一次动作, 返回占用时长(秒, 未按 speed 缩放)。

        TUNE_MODE='hold' 时是"按住指定毫秒" —— 对应有些游戏"按住持续滑音"的手法。
        """
        if TUNE_MODE == 'hold':
            return self.hold(act)
        done = 0
        for _ in range(max(1, times)):
            if self.stop.is_set():
                break
            if self.dry_run:
                sys.stdout.write(TUNE_MARKS.get(act, '[%s]' % act))
                sys.stdout.flush()
            elif act in MOUSE_ACTIONS:
                self.mouse.click(getattr(self.Button, act), 1)
            elif act in WHEEL_ACTIONS:
                # 有些游戏的"半音"其实是滚轮滚动, 而不是按下滚轮
                self.mouse.scroll(0, 1 if act == 'wheel_up' else -1)
            else:                                   # 键盘切音区
                self.kb.press(act)
                time.sleep(KEY_HOLD)
                self.kb.release(act)
            self.count['tune'] += 1
            done += 1
            self.sleep(MOUSE_PAUSE)                 # 已按 speed 缩放
        return MOUSE_PAUSE * done

    def hold(self, act, ms=None):
        """按住某个键/鼠标键 ms 毫秒再松开(持续滑音)。返回占用时长(秒)。"""
        if ms is None:
            ms = HOLD_MS_PER_OCTAVE if act in (OCTAVE_UP, OCTAVE_DOWN) else HOLD_MS_PER_SEMITONE
        mark = TUNE_MARKS.get(act, '[%s]' % act)
        if self.dry_run:
            sys.stdout.write('%s%s' % (mark, int(ms)))
            sys.stdout.flush()
        else:
            if act in MOUSE_ACTIONS:
                btn = getattr(self.Button, act)
                self.mouse.press(btn)
                time.sleep(ms / 1000.0)
                self.mouse.release(btn)
            else:
                self.kb.press(act)
                time.sleep(ms / 1000.0)
                self.kb.release(act)
        self.count['tune'] += 1
        return ms / 1000.0

    def do_tune(self, actions):
        """执行一组换音区动作(按 OCTAVE_CLICKS / SEMITONE_CLICKS 展开)。"""
        if not actions:
            return 0.0
        if TUNE_MODE == 'hold':
            total = 0.0
            for act in actions:
                total += self.hold(act)
            return total
        total = 0.0
        for act in actions:
            times = OCTAVE_CLICKS if act in (OCTAVE_UP, OCTAVE_DOWN) else SEMITONE_CLICKS
            total += self.click(act, times)
        return total

    # ------------------------------------------------- 音区(按住鼠标键)
    def set_register(self, reg):
        """切换音区: 松开旧的音区键, 按住新的 —— 按住期间一直生效。"""
        if reg == self.cur_reg:
            return
        self.cur_reg = reg
        btn_name = REGISTERS[reg][0]
        if self.dry_run:
            sys.stdout.write('[%s]' % reg)
            sys.stdout.flush()
            return
        if self.cur_btn is not None:
            try:
                self.mouse.release(self.cur_btn)
            except Exception:
                pass
        self.cur_btn = getattr(self.Button, btn_name) if btn_name else None
        if self.cur_btn is not None:
            self.mouse.press(self.cur_btn)
        self.sleep(MOUSE_PAUSE)

    def release_all(self):
        """松掉所有按住的东西 —— 急停/出错时绝不能把鼠标键留在按下状态。"""
        if self.dry_run:
            return
        for ch in list(self.held_keys):
            try:
                self.kb.release(ch)
            except Exception:
                pass
        self.held_keys.clear()
        if self.cur_btn is not None:
            try:
                self.mouse.release(self.cur_btn)
            except Exception:
                pass
            self.cur_btn = None
        self.cur_reg = DEFAULT_REGISTER

    # ------------------------------------------------------------ 播放
    def play_note(self, ch, beats):
        """按一个音: 按住 ch 直到时值结束再松开(所以音会延续)。"""
        dur = NOTE_TIME * beats
        if self.dry_run:
            sys.stdout.write(ch + '~' * max(0, int(round(beats)) - 1))
            sys.stdout.flush()
            self.count['note'] += 1
            return
        self.kb.press(ch)
        self.held_keys.add(ch)
        self.wait(dur)
        self.kb.release(ch)
        self.held_keys.discard(ch)
        self.wait(RELEASE_GAP)
        self.count['note'] += 1

    def play(self, events):
        """演奏事件序列。register 事件=按住/松开音区键, note 事件=按住音符键。"""
        emergency_release()          # 先清一次, 防止上一轮残留把鼠标键卡住
        for e in events:
            if self.stop.is_set():
                break
            kind = e['kind']
            if kind == 'register':
                self.set_register(e['reg'])
            elif kind == 'breath':
                self.sleep(BREATH_PAUSE)
                self.count['rest'] += 1
            elif kind == 'rest':
                self.sleep(PAUSE_TIME)
                self.count['rest'] += 1
            elif kind == 'hold':
                self.sleep(NOTE_TIME)
                self.count['hold'] += 1
            elif kind == 'note':
                self.play_note(e['key'], e['beats'])
            elif kind == 'linebreak':
                if e.get('pause'):
                    self.sleep(LINE_PAUSE)
            elif kind == 'unknown':
                print('[?] 看不懂的 token: %r (已跳过)' % e['raw'])
        self.release_all()


NOTE_RE = re.compile(r"^(1'|[1-8]|i|[zxcnmb,])([-.]*)$", re.IGNORECASE)


def build_events(lines):
    """把谱面文本编译成事件序列。

    支持两种写法(可混用):
      简谱+音区字母 : R 1 2 3 -  /  N 1 2 3  /  L 1 2 3
      原始键位      : R z- R x R c  /  N b n    (和你 v4.5 脚本的谱面一样)

    记号:
      L / N / R / M  切音区(低/中/高/半音) —— 按住鼠标键, 按住期间一直生效
      1..7  i(或 8, 1')  音符(对应 z x c v b n m ,)
      <音符><后缀>   后缀里每个 '-' 多 1 拍, 每个 '.' 多 0.5 拍 (如 z- / 5-- / 3.)
      v              换气(短停顿)
      _ 或 0         停顿
      - 或 .(单独出现) 给上一个音延长 1 拍 / 0.5 拍
      // 开头        注释        | 小节线(忽略)
    """
    events = []
    cur_reg = None
    for raw in lines:
        toks = [t for t in raw.split('//')[0].split() if t not in ('|', '(', ')')]
        if not toks:
            continue
        had_note = had_rest = False
        for tok in toks:
            if tok in REGISTERS:
                if tok != cur_reg:                  # 只在真的换了音区时才发指令
                    events.append({'kind': 'register', 'reg': tok})
                    cur_reg = tok
                had_rest = True                     # 换音区算"有停顿", 免得行末再多喘一口气
                continue
            if tok == 'v':
                events.append({'kind': 'breath'})
                had_rest = True
                continue
            if tok in ('_', '0'):
                events.append({'kind': 'rest'})
                had_rest = True
                continue
            if tok and set(tok) <= set('-.'):       # 纯延长记号
                add = tok.count('-') + 0.5 * tok.count('.')
                if events and events[-1]['kind'] == 'note':
                    events[-1]['beats'] += add      # 给上一个音加时值
                else:
                    events.append({'kind': 'hold'})
                continue
            m = NOTE_RE.match(tok)
            if m:
                ch = m.group(1)
                key = ',' if ch == "1'" else NOTE_CHARS.get(ch.lower())
                if key is None:
                    events.append({'kind': 'unknown', 'raw': tok})
                    continue
                beats = 1.0 + m.group(2).count('-') + 0.5 * m.group(2).count('.')
                events.append({'kind': 'note', 'key': key, 'beats': beats})
                had_note = True
                continue
            events.append({'kind': 'unknown', 'raw': tok})
        events.append({'kind': 'linebreak', 'pause': had_note and not had_rest})
    return events


def read_score(path):
    with open(path, 'r', encoding='utf-8-sig') as f:
        return f.read().splitlines()


def do_probe(p, n):
    """音区校准。

    参考组  : 按 1 → 按 i   (键盘自己就能弹出"正好高一个八度", 不用猜)
    测试组  : 按 1 → 升调 n 次 → 按 1
    两组听起来一样高 => n 就是"点几下 = 一个八度"。
    """
    print('')
    print('=== 音区校准: 测试 "点 %d 次 %s = 一个八度?" ===' % (n, OCTAVE_UP))
    print('第 1 组【参考】: 键盘的 do → 高音 do  (= 正好一个八度)')
    p.press('z')
    p.wait(0.6)
    p.press(',')
    p.wait(1.6)

    for round_no in (1, 2):
        print('第 %d 组【测试】: do → 点 %d 次 %s → do' % (round_no + 1, n, OCTAVE_UP))
        p.press('z')
        p.wait(0.6)
        p.click(OCTAVE_UP, n)
        p.wait(0.3)
        p.press('z')
        p.wait(1.6)

    print('')
    print('两次"测试组"的和"参考组"一样高(同一个音名, 只是更高) → 校准值就是 %d' % n)
    print('听起来偏高/偏低 → 换个 n 再试(比如 1 / 2 / 6 / 12)')
    print('确定后在 GUI 里点"保存校准值", 或执行:  harp.cmd calibrate --set %d' % n)


def do_probe_hold(p, ms):
    """按住式校准: 有些游戏是"按住鼠标键持续滑音", 那就该测按住多少毫秒 = 一个八度。"""
    print('')
    print('=== 音区校准(按住式): 按住 %s %d 毫秒 是不是一个八度? ===' % (OCTAVE_UP, ms))
    print('第 1 组【参考】: 键盘的 do → 高音 do  (= 正好一个八度)')
    p.press('z')
    p.wait(0.6)
    p.press(',')
    p.wait(1.6)

    for round_no in (1, 2):
        print('第 %d 组【测试】: do → 按住 %s %d ms → do' % (round_no + 1, OCTAVE_UP, ms))
        p.press('z')
        p.wait(0.6)
        p.hold(OCTAVE_UP, ms)
        p.wait(0.3)
        p.press('z')
        p.wait(1.6)

    print('')
    print('和参考组一样高 → 就用这个毫秒值; 偏高就把毫秒调小, 偏低就调大。')
    print('保存: GUI 的「🎯 音区校准」里切到"按住式"再填毫秒; 或写进 harp_config.json:')
    print('      { "TUNE_MODE": "hold", "HOLD_MS_PER_OCTAVE": %d }' % ms)


def do_keytest(p):
    """按键自检: 往当前前台窗口敲 zxcvbnm, —— 用来分辨"模拟按键通不通"。"""
    print('')
    print('=== 按键自检 ===')
    print('马上依次发送: z x c v b n m ,')
    print('如果前台窗口(记事本 / 地址栏)里出现 "zxcvbnm," → 模拟按键是通的。')
    print('建议先用【以管理员身份运行】的记事本试一次 —— 那能同时验证权限是否够。')
    p.wait(0.5)
    for ch in 'zxcvbnm,':
        if p.stop.is_set():
            break
        p.press(ch)
        p.wait(0.35)
    print('')
    print('看到了内容 = 通的; 什么都没有 = 系统层面就被拦了(权限不足 / 被杀软拦)。')


def arm_esc(p, dry_run):
    """按 Esc 急停。"""
    if dry_run:
        return None
    listen = getattr(p, 'Listener', None)
    Key = getattr(p, 'Key', None)
    if not (listen and Key):
        return None

    def on_press(key):
        if key == Key.esc:
            p.stop.set()
            print('\n[Esc] 急停!')
            return False

    listener = listen(on_press=on_press)
    listener.start()
    return listener


def countdown_and_wait(a, dry_run):
    """倒计时 → 等用户切到游戏窗口 → 检查"够不够得着"。

    返回 (ok, hwnd)。ok=False 表示前台窗口属于提权进程、而我们是普通权限 ——
    这种情况下按键会被 UIPI 静默丢弃, 与其让你干等, 不如直接说清楚。
    """
    if dry_run:
        return True, 0
    self_hwnd = self_window_handle()
    for i in range(a.countdown, 0, -1):
        print('  %d ...' % i)
        time.sleep(1)
    if WAIT_FOR_FOREGROUND and not a.no_wait:
        if not foreground_is_other(self_hwnd):
            print('等你切到游戏窗口...(最多 60 秒)')
            waited = 0.0
            while not foreground_is_other(self_hwnd) and waited < 60:
                time.sleep(0.2)
                waited += 0.2
            if not foreground_is_other(self_hwnd):
                print('窗口一直没切走 —— 直接开始, 祝你好运')
            else:
                time.sleep(FOREGROUND_GRACE)

    if not is_admin():
        fg = _u32.GetForegroundWindow()
        fg_pid = window_pid(fg) if fg else 0
        state = pid_is_elevated(fg_pid) if fg_pid else False
        if state is not False:
            print('')
            print('[X] 前台窗口属于一个【%s】的进程, 而本工具是普通权限。'
                  % ('已提权(管理员)' if state else '无法读取权限(多半更高)'))
            print('    Windows 的 UIPI 会把我们发出的按键全部丢掉 —— 表现就是"游戏里毫无反应、也不报错"。')
            print('    请关掉本窗口, 右键 harp-gui.cmd / harp.cmd →【以管理员身份运行】, 再来一次。')
            return False, self_hwnd
    return True, self_hwnd


def main():
    ap = argparse.ArgumentParser(description='游戏口琴自动演奏')
    ap.add_argument('score', nargs='?', help='谱面文件(默认用脚本里的 SONG)')
    ap.add_argument('--dry-run', action='store_true', help='只打印, 不真的按键')
    ap.add_argument('--speed', type=float, default=SPEED, help='整体速度倍率, >1 更快')
    ap.add_argument('--countdown', type=int, default=COUNTDOWN)
    ap.add_argument('--no-wait', action='store_true', help='不等游戏窗口到前台')
    ap.add_argument('--probe', type=int, metavar='N',
                    help='音区校准(点击式): 对比播放 "do→高音do" 和 "do→升调N次→do"')
    ap.add_argument('--probe-hold', type=int, metavar='MS',
                    help='音区校准(按住式): 对比播放 "do→高音do" 和 "do→按住升调MS毫秒→do"')
    ap.add_argument('--keytest', action='store_true',
                    help='按键自检: 倒计时后往当前窗口敲 zxcvbnm,（用记事本验证模拟按键通不通）')
    ap.add_argument('--release', action='store_true',
                    help='急救: 只松开鼠标左中右三键然后退出(万一键卡住了先跑这个)')
    a = ap.parse_args()
    load_config()

    if a.release:
        emergency_release()
        print('已松开鼠标左键/右键/中键。')
        print('若鼠标仍失灵: 对着任意窗口单击一下那个键, 或按 Ctrl+Alt+Del 再 Esc。')
        return 0

    # ---------------------------------------------------------- 校准/自检模式
    if a.probe is not None or a.probe_hold is not None or a.keytest:
        preflight()
        p = Player(dry_run=a.dry_run, speed=1.0, no_wait=a.no_wait)
        arm_esc(p, a.dry_run)
        ok, _hwnd = countdown_and_wait(a, a.dry_run)
        if not ok:
            return 2
        if a.keytest:
            do_keytest(p)
        elif a.probe_hold is not None:
            do_probe_hold(p, a.probe_hold)
        else:
            do_probe(p, a.probe)
        return 0

    lines = read_score(a.score) if a.score else SONG.splitlines()
    tokens = [t for ln in lines for t in ln.split('//')[0].split() if t != '|']
    if not tokens:
        sys.exit('谱面是空的: %s' % (a.score or 'SONG'))
    events = build_events(lines)

    preflight()
    notes = [e for e in events if e['kind'] == 'note']
    est = sum(NOTE_TIME * e['beats'] for e in notes) + PAUSE_TIME * sum(
        1 for e in events if e['kind'] == 'rest')
    print('谱面: %d 个 token (音符 %d / 停顿 %d / 换气 %d / 换音区 %d), 预计 %.1f 秒' % (
        len(tokens), len(notes),
        sum(1 for e in events if e['kind'] == 'rest'),
        sum(1 for e in events if e['kind'] == 'breath'),
        sum(1 for e in events if e['kind'] == 'register'),
        est / max(a.speed, 0.01)))
    print('参数: 1拍=%.2fs 按住音区键=%s 音区键=%s(低)/%s(高) 换气%.2fs 停顿%.2fs' % (
        NOTE_TIME, '是' if HOLD_NOTE else '否',
        REGISTERS['L'][0], REGISTERS['R'][0], BREATH_PAUSE, PAUSE_TIME))
    if a.dry_run:
        print('== DRY RUN, 不会真的按键 ==')

    p = Player(dry_run=a.dry_run, speed=a.speed, no_wait=a.no_wait)
    arm_esc(p, a.dry_run)
    ok, _hwnd = countdown_and_wait(a, a.dry_run)
    if not ok:
        return 2

    t0 = time.time()
    try:
        p.play(events)
    finally:
        p.release_all()          # 急停/异常时也必须松开鼠标键, 不能留在按下状态
    used = time.time() - t0
    print('结束: 音符 %(note)d / 停顿 %(rest)d / 延续 %(hold)d / 换音区 %(tune)d, 用时 %(used).1fs'
          % dict(p.count, used=used))
    if p.stop.is_set():
        print('(被 Esc 中断)')
    return 0


if __name__ == '__main__':
    sys.exit(main() or 0)
