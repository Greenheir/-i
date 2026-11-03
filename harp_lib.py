# -*- coding: utf-8 -*-
"""harp_lib.py —— 连接"三角洲口琴谱"成品站的公开曲库（663 首）。

功能：
    search("关键词")            -> 搜歌（本地缓存索引，秒回）
    download(id 或 歌名, 目录)  -> 下载 + 解码 + 转成我们工具吃的"成品站导出稿"文本

实现说明（照抄成品站前端 bundle 里的算法，已用 10 首原稿逐行比对验证）：
    * 曲库是静态 CDN：index.js（663 首分 16 个分片）+ catalog/<分片>.js + score/<id>.js
    * payload = base64url(raw-deflate(DFHS 二进制))
    * DFHS:  magic'DFHS' + ver + varint(ppq) + zigzag(移调) + 速度事件 + 拍号 + 小节线 + 音符
             音符 = varint(起始tick增量) + varint(时长tick) + u8(MIDI音高)
    * 口琴可吹范围 MIDI 48~85（基准 60=C4=简谱1），选指法用 Viterbi（Kg/CN 两个代价函数）

只依赖标准库，打包进 exe 也没有额外体积。
"""

from __future__ import print_function

import base64
import io
import json
import os
import re
import sys
import time
import urllib.request
import zlib

CDN = 'https://656e-endfield-d3gdy9wg4afba9d16-1474357318.tcb.qcloud.la/harmonica'
UA = {'User-Agent': 'Mozilla/5.0 (harp-tool)'}
try:                       # 打包成 exe/被管道接走时，中文输出也不乱码
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass
if getattr(sys, 'frozen', False):
    ROOT = os.path.dirname(os.path.abspath(sys.executable))
else:
    ROOT = os.path.dirname(os.path.abspath(__file__))
# 曲库索引缓存放在用户目录，别丢在桌面/exe 旁边（exe 放桌面时 ROOT 就是桌面）
CACHE_DIR = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'),
                         'harp-tool', 'dfh_cache')
TOL = 0.04
# 键位表：键, 简谱音级, 相对 do 的半音, 自带的八度数, 键序号
KEYS = [('Z', 1, 0, 0, 0), ('X', 2, 2, 0, 1), ('C', 3, 4, 0, 2), ('V', 4, 5, 0, 3),
        ('B', 5, 7, 0, 4), ('N', 6, 9, 0, 5), ('M', 7, 11, 0, 6), (',', 1, 12, 1, 7)]
RHY = [(4, '1'), (3, '2·'), (2, '2'), (1.5, '4·'), (1, '4'), (.75, '8·'), (.5, '8'),
       (.375, '16·'), (.25, '16'), (.125, '32')]


# ----------------------------------------------------------------- 网络
class LibError(Exception):
    pass


def _get(url, retry=4, timeout=25):
    last = None
    for _ in range(retry):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode('utf-8', 'replace')
        except Exception as e:                      # CDN 偶尔掐连接
            last = e
            time.sleep(1.0)
    raise LibError('取 %s 失败：%s' % (url, last))


def _unwrap(txt):
    m = re.match(r'^__dfh\("[^"]+",(.*)\);?\s*$', txt.strip(), re.S)
    if not m:
        raise LibError('曲库文件格式不认识')
    return json.loads(m.group(1))


def _cache_path(name):
    if not os.path.isdir(CACHE_DIR):
        os.makedirs(CACHE_DIR)
    return os.path.join(CACHE_DIR, name)


def _cached(name, url, refresh=False):
    fp = _cache_path(name)
    if os.path.exists(fp) and not refresh:
        try:
            return io.open(fp, encoding='utf-8').read()
        except Exception:
            pass
    txt = _get(url)
    try:
        io.open(fp, 'w', encoding='utf-8').write(txt)
    except Exception:
        pass
    return txt


def index(refresh=False):
    """曲库索引 -> (总数, 分片字典, 快照时间)"""
    obj = _unwrap(_cached('index.js', CDN + '/index.js', refresh))
    return obj.get('n', 0), obj.get('s', {}), obj.get('t', 0)


def catalog(shard, refresh=False):
    obj = _unwrap(_cached('catalog_%s.js' % shard, CDN + '/catalog/%s.js' % shard, refresh))
    return obj['r'] if isinstance(obj, dict) and 'r' in obj else obj


def search(keyword, limit=300, refresh=False):
    """按歌名（或投稿人）搜，返回 [{id,title,uploader,bpm,notes,ms,tags}]"""
    kw = (keyword or '').strip()
    if not kw:
        return []
    total, shards, _t = index(refresh)
    out = []
    for shard in shards:
        for row in catalog(shard, refresh):
            title = str(row[1])
            if kw.lower() in title.lower() or kw in str(row[3]):
                out.append({'id': row[0], 'title': title, 'composer': row[2],
                            'uploader': row[3], 'tags': row[5], 'bpm': row[7],
                            'ms': row[8], 'notes': row[9], 'updated': row[10] if len(row) > 10 else ''})
                if len(out) >= limit:
                    return out
    return out


# ----------------------------------------------------------------- DFHS 解码
def _varint(b, i):
    val, shift = 0, 0
    while True:
        c = b[i]
        i += 1
        val |= (c & 0x7F) << shift
        if not (c & 0x80):
            return val, i
        shift += 7


def _zigzag(v):
    return v // 2 if v % 2 == 0 else -(v + 1) // 2


def decode_dfhs(blob):
    if blob[:4] != b'DFHS':
        raise LibError('不是 DFHS 数据')
    i = 5
    ppq, i = _varint(blob, i)
    tr, i = _varint(blob, i)
    n_t, i = _varint(blob, i)
    y = 0
    for _ in range(n_t):
        d, i = _varint(blob, i); y += d
        _, i = _varint(blob, i)
    n_ts, i = _varint(blob, i)
    y, ts = 0, []
    for _ in range(n_ts):
        d, i = _varint(blob, i); y += d
        num, i = _varint(blob, i)
        den, i = _varint(blob, i)
        ts.append({'beat': y / float(ppq), 'num': num, 'den': den})
    n_m, i = _varint(blob, i)
    y, ms = 0, []
    for _ in range(n_m):
        d, i = _varint(blob, i); y += d
        ms.append(y / float(ppq))
    n_n, i = _varint(blob, i)
    y, notes = 0, []
    for _ in range(n_n):
        d, i = _varint(blob, i); y += d
        dur, i = _varint(blob, i)
        pitch = blob[i]; i += 1
        notes.append({'beat': y / float(ppq), 'dur': dur / float(ppq), 'pitch': pitch})
    return {'ppq': ppq, 'transpose': _zigzag(tr), 'ts': ts or [{'beat': 0, 'num': 4, 'den': 4}],
            'measures': ms, 'notes': notes}


def meta(sid):
    """取一首的元信息 + 解码后的音符（未选指法）"""
    obj = _unwrap(_get(CDN + '/score/%s.js' % sid))
    p = obj.get('p') or ''
    blob = zlib.decompress(base64.urlsafe_b64decode(p + '=' * (-len(p) % 4)), -15)
    return obj, decode_dfhs(blob)


# ----------------------------------------------------------------- 选指法
def candidates(pitch):
    out = []
    for key, degree, semi, intr, kidx in KEYS:
        for c in (-1, 0, 1):
            for sharp in (0, 1):
                if 60 + semi + c * 12 + sharp == pitch:
                    out.append({'key': key, 'degree': degree, 'keyIndex': kidx,
                                'intrinsicOctave': intr, 'octaveModifier': c,
                                'sharp': bool(sharp)})
    return out


def _kg(c):
    return (0.45 if c['sharp'] else 0.0) + (0.0 if c['octaveModifier'] == 0 else 0.22)


def _cn(a, b):
    o = abs(a['keyIndex'] - b['keyIndex']) * 0.16
    if a['sharp'] != b['sharp']:
        o += 1.9
    if a['octaveModifier'] != b['octaveModifier']:
        o += 2.5 * abs(a['octaveModifier'] - b['octaveModifier'])
    return o


def finger(notes, transpose=0):
    """Viterbi 选整条最省力的指法 -> (可用音符, 超出音域的音, 代价)"""
    seq, bad = [], []
    for n in notes:
        cs = candidates(n['pitch'] + transpose)
        if cs:
            seq.append({'beat': n['beat'], 'dur': n['dur'], 'cands': cs})
        else:
            bad.append(n['pitch'] + transpose)
    if not seq:
        return [], bad, 0.0
    dp = [[(_kg(c), -1) for c in seq[0]['cands']]]
    for i in range(1, len(seq)):
        prev, row = dp[i - 1], []
        for c in seq[i]['cands']:
            best, bi = None, -1
            for k, (pc, _) in enumerate(prev):
                v = pc + _cn(seq[i - 1]['cands'][k], c) + _kg(c)
                if best is None or v < best:
                    best, bi = v, k
            row.append((best, bi))
        dp.append(row)
    j = min(range(len(dp[-1])), key=lambda k: dp[-1][k][0])
    cost = dp[-1][j][0]
    chosen = [None] * len(seq)
    for i in range(len(seq) - 1, -1, -1):
        chosen[i] = seq[i]['cands'][j]
        j = dp[i][j][1]
    return [dict(seq[i], **chosen[i]) for i in range(len(seq))], bad, cost


# ----------------------------------------------------------------- 文本
def degree_str(n):
    u = n['intrinsicOctave'] + n['octaveModifier']
    return ('#' if n['sharp'] else '') + str(n['degree']) + (("'" * u) if u > 0 else ("." * -u))


def key_str(n):
    u = '+' if n['octaveModifier'] > 0 else ('-' if n['octaveModifier'] < 0 else '')
    return n['key'] + u + ('#' if n['sharp'] else '')


def rhythm_str(d):
    d = max(0.0, d)
    for v, tok in RHY:
        if abs(v - d) < TOL:
            return tok
    return '%gb' % (round(d * 100) / 100.0)


def bars_of(notes, ts, measures):
    if not notes:
        return []
    end = max(n['beat'] + n['dur'] for n in notes)
    num, den = ts[0]['num'], ts[0]['den']
    blen = num * 4.0 / den
    starts = list(measures)
    if not starts:
        x = 0.0
        while x < end - 1e-9:
            starts.append(x)
            x += blen
    out = []
    for i, h in enumerate(starts):
        t = starts[i + 1] if i + 1 < len(starts) else max(h + blen, end)
        out.append({'no': i + 1, 'num': num, 'den': den,
                    'notes': [n for n in notes if h - 1e-9 <= n['beat'] < t - 1e-9]})
    return out


def render(title, info, notes, unplayable, transpose, bpm):
    bars = bars_of(notes, info['ts'], info['measures'])
    L = ['%s — 三角洲口琴谱' % title,
         'BPM %d · %d 音符 · %d 小节 · 移调 %s' %
         (round(bpm), len(notes), len(bars), ('%+d' % transpose) if transpose else '0')]
    if unplayable:
        L.append('超出音域被略过：%d 个音符' % len(unplayable))
    L += ['', '键位标记：+ 升调（鼠标右键） / - 降调（鼠标左键） / # 半音（鼠标中键）', '']
    for b in bars:
        head = '小节 %3d (%d/%d)' % (b['no'], b['num'], b['den'])
        if not b['notes']:
            L += [head + '  —', '']
            continue
        cols = []
        for n in b['notes']:
            g, k, r = degree_str(n), key_str(n), rhythm_str(n['dur'])
            w = max(len(g), len(k), len(r))
            cols.append((g.ljust(w), k.ljust(w), r.ljust(w)))
        L.append(head)
        L.append('  简谱  ' + ' '.join(c[0] for c in cols))
        L.append('  键位  ' + ' '.join(c[1] for c in cols))
        L.append('  节奏  ' + ' '.join(c[2] for c in cols))
        L.append('')
    return '\n'.join(L)


def to_text(sid_or_title, auto_transpose=False):
    """一首 -> (标题, 文本, 元信息)。传歌名时会先用曲库第一条命中。"""
    sid, title = sid_or_title, sid_or_title
    if not re.match(r'^[A-Za-z0-9_\-]{8,}$', sid_or_title):
        hits = search(sid_or_title, limit=1)
        if not hits:
            raise LibError('曲库里没搜到「%s」' % sid_or_title)
        sid, title = hits[0]['id'], hits[0]['title']
    obj, info = meta(sid)
    title = obj.get('t') or title
    bpm = obj.get('b') or 120
    t = info['transpose']
    notes, bad, _c = finger(info['notes'], t)
    if auto_transpose:
        best = None
        for cand in range(-12, 13):
            _n, b2, cost = finger(info['notes'], cand)
            sc = len(b2) * 1e4 + cost + abs(cand) * 0.08
            if best is None or sc < best[0]:
                best = (sc, cand, _n, b2)
        t, notes, bad = best[1], best[2], best[3]
    text = render(title, info, notes, bad, t, bpm)
    return title, text, {'id': sid, 'bpm': bpm, 'transpose': t, 'notes': len(notes),
                         'unplayable': len(bad), 'uploader': obj.get('u', '')}


def safe_name(name):
    return re.sub(r'[\\/:*?"<>|]', '_', name).strip() or 'score'


def _same(path, text):
    try:
        return io.open(path, encoding='utf-8-sig').read() == text
    except Exception:
        return False


def unique_path(d, name, ext='.txt'):
    """不覆盖别人的文件：重名就加 (2)(3)...（若已有内容相同的副本则复用它）"""
    i = 2
    while True:
        p = os.path.join(d, '%s(%d)%s' % (name, i, ext))
        if not os.path.exists(p):
            return p
        i += 1


def download(sid_or_title, dest_dirs, auto_transpose=False, overwrite=False):
    """下载并转成一个 .txt（可写多个目录） -> (标题, 写出的路径列表, 元信息)

    ★ 默认【绝不覆盖】已存在的文件：内容相同就跳过，内容不同就写成 名字(2).txt。
      （这条是被自己坑出来的：自动化测试直接写进了用户的收藏夹，把人家原来的稿覆盖了。）
    """
    title, text, info = to_text(sid_or_title, auto_transpose)
    name = safe_name(title)
    paths = []
    for d in ([dest_dirs] if isinstance(dest_dirs, str) else dest_dirs):
        if not d:
            continue
        if not os.path.isdir(d):
            os.makedirs(d)
        p = os.path.join(d, name + '.txt')
        if os.path.exists(p) and not overwrite:
            hit, free = None, None
            for i in range(1, 400):
                q = p if i == 1 else os.path.join(d, '%s(%d).txt' % (name, i))
                if not os.path.exists(q):
                    free = q
                    break
                if _same(q, text):          # 之前下过同一份 -> 复用，别堆 (2)(3)
                    hit = q
                    break
            if hit:
                paths.append(hit)
                continue
            p = free or unique_path(d, name)
        io.open(p, 'w', encoding='utf-8', newline='\n').write(text)
        paths.append(p)
    return title, paths, info


# ----------------------------------------------------------------- CLI
def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 0
    cmd = argv[1]
    if cmd in ('index', 'info'):
        n, shards, t = index(refresh='--refresh' in argv)
        print('曲库共 %d 首，分 %d 个分片；快照 %s' %
              (n, len(shards), time.strftime('%Y-%m-%d %H:%M', time.localtime(t / 1000))))
        return 0
    if cmd in ('search', 'find'):
        if len(argv) < 3:
            print('用法: harp_lib.py search 关键词')
            return 2
        rows = search(argv[2], refresh='--refresh' in argv)
        if not rows:
            print('没搜到「%s」' % argv[2])
            return 1
        for r in rows:
            print('%-28s %4s 音符  BPM %-6s 投稿 %-14s id=%s' %
                  (r['title'][:28], r['notes'], r['bpm'], (r['uploader'] or '')[:14], r['id']))
        print('共 %d 条' % len(rows))
        return 0
    if cmd in ('get', 'download'):
        if len(argv) < 3:
            print('用法: harp_lib.py get "歌名或id" [输出目录]')
            return 2
        out = argv[3] if len(argv) > 3 else os.path.join(ROOT, 'scores')
        title, paths, info = download(argv[2], out, auto_transpose='--auto' in argv)
        print('《%s》 %d 音符 · 移调 %+d · 超出音域 %d' %
              (title, info['notes'], info['transpose'], info['unplayable']))
        for p in paths:
            print('写出', p)
        return 0
    print('未知命令：%s' % cmd)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))
