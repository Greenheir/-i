# -*- coding: utf-8 -*-
"""
midi2harp.py —— MIDI → 游戏口琴「简谱」谱面

用法:
    python midi2harp.py output\\vocals_basic_pitch.mid
    python midi2harp.py song.mid --key D --unit 0.12 -o harp_score.txt

谱面格式(harp2.py 可直接读):
    1 2 3 5 6 5 3 -     简谱数字 1-7, i = 高八度 do   (对应按键 Z X C V B N M ,)
    _                   休止(一个 token = 一个 UNIT 的时长)
    -                   延长前一个音(不重新按键, 只等一个 UNIT)
    ^  /  v             升八度 / 降八度   → 演奏时点鼠标右键 / 左键
    #                   半音开关         → 演奏时点鼠标中键(默认不生成)
    // 开头             注释行, harp2.py 自动忽略

节奏怎么对上原曲:
    每个 token(音符/休止/延长)都等于 UNIT 秒。所以把 harp2.py 的
    NOTE_TIME 设成和 --unit 一样的值, 播放节奏就与原曲一致。

为什么会有 ^ 和 v:
    键盘只有 8 个音(一个八度). 人声常常跨一个半八度, 所以超出当前八度的音,
    用"调音"方式整体挪一个八度再按同一个键 —— 这就是游戏里鼠标左/右键的用途。
    生成器会把总调音次数作为代价来搜索起始八度(见输出头部的"调音次数")。

常用参数:
    --key C        口琴调性. 游戏里是 1=D 就写 --key D (这时简谱的 1 = D)
    --unit 0.15    节奏量化粒度(秒). 越小越细, 但会把长音切碎
    --max-shift 1  相邻音之间最多允许调几个八度(防止旋律抖动时来回狂点鼠标)
    --line-len 16  每行最多几个 token(换行只影响阅读和换气位置)
    --transpose n  整体升/降 n 个半音
"""

import argparse
import os
import sys
from collections import defaultdict

try:                      # 让重定向到文件时也是 UTF-8, 不再乱码
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

try:
    import mido
except ImportError:
    sys.exit("缺少 mido。请用本目录的虚拟环境: .venv\\Scripts\\python.exe midi2harp.py ...")

# ----------------------------------------------------------------- 音乐常量
NOTE_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
MAJOR = [0, 2, 4, 5, 7, 9, 11]                 # 大调音阶(半音)
DIGITS = ['1', '2', '3', '4', '5', '6', '7', 'i']
KEY_OFFSETS = MAJOR + [12]                     # 8 个键相对主音的半音数
MAX_REST_TOKENS = 8                            # 一个静音段最多用几个 '_' 表示
MAX_HOLD = 8                                   # 一个音最多持续几拍(防止异常长音撑爆)


def parse_key(name):
    """'C' / 'F#' / 'Bb' -> 0..11"""
    s = name.strip()
    if not s:
        raise ValueError('空调性')
    base = s[0].upper()
    if base not in 'CDEFGAB':
        raise ValueError('无法识别的调性: %s' % name)
    pc = NOTE_NAMES.index(base)
    for ch in s[1:]:
        if ch == '#':
            pc += 1
        elif ch == 'b':
            pc -= 1
        else:
            raise ValueError('无法识别的调性: %s' % name)
    return pc % 12


# ------------------------------------------------------------------ 读 MIDI
def load_notes(path):
    """返回 [(pitch, start_sec, dur_sec, velocity), ...]"""
    mid = mido.MidiFile(path)
    notes = []
    for track in mid.tracks:
        t = 0.0
        tempo = 500000                       # 默认 120bpm
        pending = defaultdict(list)
        for msg in track:
            t += mido.tick2second(msg.time, mid.ticks_per_beat, tempo)
            if msg.type == 'set_tempo':
                tempo = msg.tempo
            elif msg.type == 'note_on' and msg.velocity > 0:
                pending[msg.note].append((t, msg.velocity))
            elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                if pending.get(msg.note):
                    st, vel = pending[msg.note].pop(0)
                    notes.append((msg.note, st, max(t - st, 0.0), vel))
    notes.sort(key=lambda n: (n[1], n[0]))
    return notes


def quantize(notes, unit):
    """把音符铺到时间网格上, 每个格子取"最长且最高"的那个音(旋律化处理)。"""
    grid = defaultdict(list)
    for pitch, st, dur, vel in notes:
        i0 = int(round(st / unit))
        i1 = max(i0, int(round((st + dur) / unit)) - 1)
        i1 = min(i1, i0 + 8)                 # 单音最多占 8 格, 防止异常长音撑爆
        for i in range(i0, i1 + 1):
            grid[i].append((round(dur, 4), -pitch))
    picked = {}
    for i, lst in grid.items():
        lst.sort()
        # 取"最长"的那个音; 同长时取更低的音 —— 人声扒谱里高一个八度的常常是
        # 泛音(2 倍频), 取最高音会让旋律整段飘到高八度去
        picked[i] = -lst[-1][1]
    return picked


# --------------------------------------------------------------- 谱面生成
REG_LETTERS = [('L', -1), ('N', 0), ('R', 1)]      # 低音区 / 中音区 / 高音区


def fold_note(p, key_pc, base_oct, cur_reg):
    """给一个音高找最合适的 (音区, 键位)。

    优先"不用换音区"(同误差下不换), 这样整首曲子音区切换最少 ——
    游戏里换音区是按住鼠标键, 越少切换越稳。
    返回 (误差, 是否要换音区, 键位序号, 音区字母)。
    """
    cands = []
    for reg, shift in REG_LETTERS:
        base = key_pc + 12 * (base_oct + shift)
        for d, off in enumerate(KEY_OFFSETS):
            cands.append((abs(p - (base + off)), 0 if reg == cur_reg else 1, d, reg))
    cands.sort()
    return cands[0]


def build_tokens(picked, key_pc, base_oct):
    """格号->音高 编译成谱面 token(音区字母只在变化时出现)。

    返回 (tokens, 音区切换次数, 总音准误差) —— 误差用来挑基准八度:
    三个音区合起来只覆盖三个八度, 基准选错会让高/低音被硬折回来。
    """
    if not picked:
        return [], 0, 0

    keys = sorted(picked)
    tokens = []
    cur_reg = 'N'
    switches = 0
    err_sum = 0
    i, last = keys[0], keys[-1]

    while i <= last:
        if i not in picked:
            gap = 0
            while i + gap <= last and (i + gap) not in picked:
                gap += 1
            tokens.extend(['_'] * min(gap, MAX_REST_TOKENS))
            i += gap
            continue

        p = picked[i]
        run = 0                                     # 这个音持续几格
        while i + run <= last and picked.get(i + run) == p:
            run += 1
        run = min(run, MAX_HOLD)

        err, _changed, d, reg = fold_note(p, key_pc, base_oct, cur_reg)
        err_sum += err
        if reg != cur_reg:
            tokens.append(reg)
            cur_reg = reg
            switches += 1
        tokens.append(DIGITS[d] + '-' * (run - 1))
        i += run

    return tokens, switches, err_sum


def layout(tokens, line_len):
    """换行: 优先在休止符后断句(那也是自然的换气点)。"""
    lines, cur = [], []
    for tok in tokens:
        cur.append(tok)
        if (tok == '_' and len(cur) >= max(6, line_len // 2)) or len(cur) >= line_len:
            lines.append(' '.join(cur))
            cur = []
    if cur:
        lines.append(' '.join(cur))
    return lines


def main():
    ap = argparse.ArgumentParser(description='MIDI -> 游戏口琴简谱')
    ap.add_argument('midi', help='输入 .mid 文件')
    ap.add_argument('-o', '--out', default='harp_score.txt', help='输出谱面文件')
    ap.add_argument('--key', default='C', help='口琴调性, 如 C / D / G / F# / Bb')
    ap.add_argument('--unit', type=float, default=0.15, help='量化粒度(秒)')
    ap.add_argument('--max-shift', type=int, default=1, help='相邻音最多调几个八度')
    ap.add_argument('--line-len', type=int, default=16, help='每行最多 token 数')
    ap.add_argument('--transpose', type=int, default=0, help='整体移调(半音)')
    a = ap.parse_args()

    if not os.path.exists(a.midi):
        sys.exit('找不到文件: %s' % a.midi)
    key_pc = parse_key(a.key)

    notes = load_notes(a.midi)
    if not notes:
        sys.exit('这个 MIDI 里没有音符: %s' % a.midi)
    if a.transpose:
        notes = [(p + a.transpose, st, du, v) for p, st, du, v in notes]

    picked = quantize(notes, a.unit)
    if not picked:
        sys.exit('量化后没有音符, 试试把 --unit 调大一点')

    # 中音区的基准八度: 先保证音准(误差最小), 误差相同再取音区切换少的
    octaves = [int((p - key_pc) // 12) for p in picked.values()]
    best = None
    for octv in range(min(octaves) - 1, max(octaves) + 2):
        toks, sw, err = build_tokens(picked, key_pc, octv)
        score = (err, sw)
        if best is None or score < best[0]:
            best = (score, toks, sw, err, octv)
    _score, tokens, switches, err_sum, base_oct = best
    lines = layout(tokens, a.line_len)

    lo, hi = min(p for p, *_ in notes), max(p for p, *_ in notes)
    dur = max(st + du for _, st, du, _ in notes)
    header = [
        '// 由 midi2harp.py 生成',
        '// 源文件   : %s' % os.path.basename(a.midi),
        '// 调性     : %s (1=%s)' % (a.key, a.key),
        '// 量化粒度 : %ss   token: %d   音域: %s%d ~ %s%d   时长: %.1fs' % (
            a.unit, len(tokens),
            NOTE_NAMES[lo % 12], lo // 12 - 1, NOTE_NAMES[hi % 12], hi // 12 - 1, dur),
        '// 中音区   : 1=%s%d   (L=低音区 按住左键 / R=高音区 按住右键)' % (a.key, base_oct - 1),
        '// 音区切换 : %d 次(L/N/R 是音区字母, 只在切换时出现)  音准误差合计: %d 个半音' % (
            switches, err_sum),
        '// 记号     : 音符后跟 - 表示多按 1 拍(如 5-- = 按住 3 拍); _ = 停顿; v = 换气',
        '// 节奏     : 1 拍 = %ss, 想让节奏对上原曲就把 harp2.py 的 NOTE_TIME 也设成 %s' % (a.unit, a.unit),
        '// 演奏     : python harp2.py %s' % os.path.basename(a.out),
        '',
    ]
    text = '\n'.join(header + lines) + '\n'
    with open(a.out, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)

    print(text)
    print('已写入: %s   (%d 个 token, %d 行, %d 次音区切换)' % (
        os.path.abspath(a.out), len(tokens), len(lines), switches))


if __name__ == '__main__':
    main()
