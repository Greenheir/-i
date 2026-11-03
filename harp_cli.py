# -*- coding: utf-8 -*-
"""
harp_cli.py —— 口琴工具链统一入口(四步流水线都从这里走)

用 harp.cmd 调用, 也可以直接用 venv 里的 python:
    .venv\\Scripts\\python.exe harp_cli.py <子命令> ...

子命令:
    env                     检查环境(版本/ffmpeg/磁盘/输入法)
    split  <音频>           ① 分离人声   demucs  -> separated\\htdemucs\\<名>\\vocals.wav
    midi   <wav>            ② 人声转MIDI basic-pitch -> output\\<名>_basic_pitch.mid
    score  <mid> [调性]     ③ 转口琴谱   midi2harp -> harp_score.txt
    play   <谱面> [参数...] ④ 自动演奏   harp2
    all    <音频> [调性]    ①→②→③ 一条龙, 最后打印④的命令
    calibrate [N|--set N|--show]
                            音区校准: 测出"点几次升调 = 一个八度", 存进 harp_config.json
    test                    用内置示例跑一次 dry-run, 确认环境通

为什么不用手册里的原命令:
  * basic-pitch 的 TF SavedModel 在 TensorFlow 2.16+ 上加载会报
    "_UserObject object has no attribute add_slot", 所以这里强制用内置的
    TFLite 模型(--model-serialization tflite), 实测可用。
  * basic-pitch 要求输出目录必须已存在, 这里自动建。
  * basic-pitch 会往控制台打 ✨, 中文区控制台(GBK)会 UnicodeEncodeError,
    这里统一设置 PYTHONUTF8/PYTHONIOENCODING。
  * ffmpeg 由 winget 装在 WinGet\\Packages 下, 新开的终端才有 PATH, 这里自己找。
"""

import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
HARP2 = os.path.join(ROOT, 'harp2.py')
CONFIG = os.path.join(ROOT, 'harp_config.json')
SEPARATED = os.path.join(ROOT, 'separated')
OUTPUT = os.path.join(ROOT, 'output')

# --------------------------------------------------------------- 网络设置
# demucs 4.1.0 的 get_model() 会【先试 HuggingFace，失败才回落到 AWS 源】：
#     try:    bag = get_hf_model(name)      # huggingface.co
#     except: ...                           # 回落到 dl.fbaipublicfiles.com
# 本机直连 huggingface.co 会连接超时（WinError 10060）并重试 5 次，卡很久；
# 而 AWS 源可用、htdemucs 模型也已缓存在
# %USERPROFILE%\.cache\torch\hub\checkpoints\955717e8-8726e21a.th。
# 所以默认直接把 HF 置为离线 —— demucs 会瞬间走回落分支，不再干等。
# 想真的用 HuggingFace（例如 hf:// 开头的模型）：设 HARP_USE_HF=1，并走国内镜像。
if os.environ.get('HARP_USE_HF') != '1':
    os.environ.setdefault('HF_HUB_OFFLINE', '1')
    os.environ.setdefault('HF_HUB_DISABLE_TELEMETRY', '1')
os.environ.setdefault('HF_ENDPOINT', 'https://hf-mirror.com')
os.environ.setdefault('HF_HUB_ETAG_TIMEOUT', '3')      # 万一真去连, 也别等太久


def load_config():
    try:
        with open(CONFIG, 'r', encoding='utf-8-sig') as f:   # utf-8-sig: 容忍记事本的 BOM
            return json.load(f)
    except Exception:
        return {}


def save_config(updates):
    cfg = load_config()
    cfg.update(updates)
    with open(CONFIG, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


def say(msg):
    print(msg, flush=True)


def find_ffmpeg():
    """winget 装完新终端才有 PATH; 这里主动找一遍, 找到就加到本次进程的 PATH。"""
    if shutil.which('ffmpeg'):
        return shutil.which('ffmpeg')
    local = os.environ.get('LOCALAPPDATA', '')
    base = os.path.join(local, 'Microsoft', 'WinGet', 'Packages')
    if os.path.isdir(base):
        for d in os.listdir(base):
            if d.lower().startswith('gyan.ffmpeg'):
                for sub in os.listdir(os.path.join(base, d)):
                    exe = os.path.join(base, d, sub, 'bin', 'ffmpeg.exe')
                    if os.path.isfile(exe):
                        os.environ['PATH'] = os.path.dirname(exe) + os.pathsep + os.environ['PATH']
                        return exe
    return None


def run(cmd):
    say('> ' + ' '.join('"%s"' % c if ' ' in c else c for c in cmd))
    return subprocess.call(cmd, cwd=ROOT)


def stem(path):
    return os.path.splitext(os.path.basename(path))[0]


def cmd_env(_args):
    say('Python : %s' % sys.version.split()[0])
    say('64 位  : %s' % (sys.maxsize > 2 ** 32))
    ff = find_ffmpeg()
    say('ffmpeg : %s' % (ff or '未找到(只影响 mp3/m4a 输入, wav 不受影响)'))
    try:
        import torch
        say('torch  : %s (%s)' % (torch.__version__, 'cuda' if torch.cuda.is_available() else 'cpu'))
    except Exception as e:
        say('torch  : 缺失 %s' % e)
    for name in ('mido', 'pynput', 'soundfile', 'librosa', 'basic_pitch', 'demucs'):
        try:
            __import__(name)
            say('%-11s: OK' % (name + ' ' * max(0, 10 - len(name))))
        except Exception as e:
            say('%-11s: 缺失 %s' % (name, e))
    try:
        total, used, free = shutil.disk_usage(ROOT)
        say('磁盘   : 剩余 %.1f GB' % (free / 2 ** 30))
    except Exception:
        pass
    return 0


def cmd_split(args):
    if not args:
        say('用法: harp.cmd split <音频文件>')
        return 2
    find_ffmpeg()
    return run([PY, '-m', 'demucs.separate', '--two-stems=vocals',
                '-o', SEPARATED, args[0]])


def vocals_path(audio):
    return os.path.join(SEPARATED, 'htdemucs', stem(audio), 'vocals.wav')


def cmd_midi(args):
    if not args:
        say('用法: harp.cmd midi <wav>')
        return 2
    os.makedirs(OUTPUT, exist_ok=True)          # basic-pitch 要求目录已存在
    # basic-pitch 发现同名 .mid 就直接跳过(不覆盖), 重跑前先删掉旧产物
    mid = os.path.join(OUTPUT, stem(args[0]) + '_basic_pitch.mid')
    if os.path.isfile(mid):
        say('删除同名旧产物: %s' % mid)
        os.remove(mid)
    rc = run([PY, '-m', 'basic_pitch.predict', '--model-serialization', 'tflite',
              '--save-midi', OUTPUT, args[0]])
    if rc != 0:
        return rc
    if os.path.isfile(mid):
        say('MIDI: %s' % mid)
    return 0


def cmd_score(args):
    if not args:
        say('用法: harp.cmd score <mid> [调性, 默认 C]')
        return 2
    key = args[1] if len(args) > 1 else 'C'
    return run([PY, os.path.join(ROOT, 'midi2harp.py'), args[0],
                '--key', key, '-o', os.path.join(ROOT, 'harp_score.txt')])


def cmd_play(args):
    if not args:
        args = [os.path.join(ROOT, 'harp_score.txt')]
    return run([PY, HARP2] + args)


def cmd_calibrate(args):
    """音区校准: 让游戏自己回答"点几次升调 = 一个八度"。"""
    if args and args[0] == '--set':
        if len(args) < 2 or not args[1].isdigit():
            say('用法: harp.cmd calibrate --set <次数>')
            return 2
        n = int(args[1])
        save_config({'OCTAVE_CLICKS': n})
        say('已保存: 点 %d 次「升调」= 一个八度   (%s)' % (n, CONFIG))
        return 0
    if args and args[0] == '--show':
        say('校准文件: %s' % CONFIG)
        say(json.dumps(load_config(), ensure_ascii=False, indent=2) if load_config() else '(还没有校准值)')
        return 0
    n = int(args[0]) if (args and args[0].isdigit()) else int(load_config().get('OCTAVE_CLICKS', 1))
    say('试听第 %d 组:' % n)
    say('  第 1 组【参考】do → 高音 do  (= 键盘自己弹出的一个八度)')
    say('  第 2/3 组【测试】do → 点 %d 次「升调」→ do' % n)
    say('  两组一样高 → 就是这个值。确定后执行:  harp.cmd calibrate --set %d' % n)
    return run([PY, HARP2, '--probe', str(n), '--countdown', '3'])


def cmd_all(args):
    if not args:
        say('用法: harp.cmd all <音频文件> [调性, 默认 C]')
        return 2
    audio = args[0]
    key = args[1] if len(args) > 1 else 'C'
    if not os.path.isfile(audio):
        say('找不到文件: %s' % audio)
        return 2
    rc = cmd_split([audio])
    if rc != 0:
        say('demucs 失败, 中止')
        return rc
    voc = vocals_path(audio)
    if not os.path.isfile(voc):
        say('没找到人声文件: %s' % voc)
        return 1
    say('人声: %s' % voc)
    rc = cmd_midi([voc])
    if rc != 0:
        say('basic-pitch 失败, 中止')
        return rc
    mid = os.path.join(OUTPUT, stem(voc) + '_basic_pitch.mid')
    rc = cmd_score([mid, key])
    if rc != 0:
        return rc
    say('')
    say('谱面已生成: %s' % os.path.join(ROOT, 'harp_score.txt'))
    say('自动演奏  : harp.cmd play harp_score.txt      (先加 --dry-run 试)')
    say('急停      : 演奏中按 Esc')
    return 0


def cmd_lib(args):
    """在线曲库：搜歌 / 下载并转成谱面（转发给 harp_lib.py）。"""
    if not args:
        say('用法:')
        say('  harp.cmd lib search <关键词>            在成品站曲库(663 首)里搜歌')
        say('  harp.cmd lib get "<歌名或id>" [目录]    下载并转成谱面(默认存 scores\\)')
        say('  harp.cmd lib index                      看曲库总数/快照时间')
        return 2
    return run([PY, os.path.join(ROOT, 'harp_lib.py')] + list(args))


def cmd_test(_args):
    return run([PY, os.path.join(ROOT, 'harp2.py'), '--dry-run', '--countdown', '0'])


def main():
    if len(sys.argv) < 2:
        say(__doc__)
        return 0
    cmd, args = sys.argv[1], sys.argv[2:]
    table = {'env': cmd_env, 'split': cmd_split, 'midi': cmd_midi,
             'score': cmd_score, 'play': cmd_play, 'all': cmd_all, 'test': cmd_test,
             'calibrate': cmd_calibrate, 'lib': cmd_lib, 'library': cmd_lib}
    fn = table.get(cmd)
    if not fn:
        say('未知子命令: %s' % cmd)
        say(__doc__)
        return 2
    return fn(args)


if __name__ == '__main__':
    sys.exit(main() or 0)
