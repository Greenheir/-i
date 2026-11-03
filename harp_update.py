# -*- coding: utf-8 -*-
"""harp_update.py —— 自动更新（零第三方依赖）。

思路（对"别人拿去用"的版本同样有效）：
    1. 启动时（或点「检查更新」）去读一个几十字节的清单 update.json：
       {"version":"1.2.0","exe":"HarpTool.exe","sha256":"...","size":11300000,"notes":"..."}
    2. 版本比本机新 → 弹窗问用户 → 下载到临时目录 → **比对 sha256** → 通过才替换
    3. 替换：Windows 允许重命名正在运行的 exe → 旧版改名 .old 备份 → 写新文件 → 重启
       用一个临时 .bat 做"等本进程退出→覆盖→启动"（正在跑的文件自己覆盖不了）

更新源（GitHub Release，latest 永久指向最新一版）：
    https://github.com/<owner>/<repo>/releases/latest/download/update.json
    https://github.com/<owner>/<repo>/releases/latest/download/HarpTool.exe
国内直连 GitHub 取 Release 附件实测 17~30 秒且会断，所以备了几个镜像前缀；
**这些源是并发同时问、谁先成功用谁**（见 fetch_manifest），不是依次回退。
私有仓库：把 token 放 harp_config.json 的 UPDATE_TOKEN（或用环境变量
HARP_UPDATE_TOKEN）—— 但**发给别人的副本没法自动更新**，所以对外的仓库必须是公开的。

命令行：
    python harp_update.py --check            看看有没有新版本
    python harp_update.py --url <清单地址>   临时指定更新源
    python harp_update.py --apply <exe路径>  装一个已经下好的 exe
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# --- 版本号：改版本只改这里（发布脚本也会读它） ---
APP_VERSION = (1, 1, 0)
APP_VER = 'v%d.%d' % (APP_VERSION[0], APP_VERSION[1])

CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0
UA = {'User-Agent': 'harp-tool-updater/1.0'}

# 国内直连 github.com 取 Release 附件实测 17~30 秒且会 SSL 握手超时，镜像 5~8 秒稳定。
# 所以下面这组源是**并发同时问**（谁先成功用谁），不是顺序回退 —— 见 fetch_manifest。
# 顺序回退的坏处：直连先撞满超时，用户要干等 12 秒 × 3 个源才看到失败。
MIRRORS = ('', 'https://ghproxy.net/', 'https://gh-proxy.com/')  # ghfast.top 证书有问题，别用
FETCH_TIMEOUT = 15          # 单个源的超时（并发下最坏总耗时≈这个值，不是 3 倍）
REPO_PLACEHOLDER = 'Greenheir/-i'


# ------------------------------------------------------------------ 版本号
def parse_ver(text):
    """'v1.2.3' / '1.2.3' / (1,2,3) -> (1,2,3)"""
    if isinstance(text, (tuple, list)):
        return tuple(int(x) for x in text)
    nums = re.findall(r'\d+', str(text or ''))
    if not nums:
        return (0,)
    return tuple(int(n) for n in nums[:4])


def is_newer(remote, local=APP_VERSION):
    a, b = parse_ver(remote), parse_ver(local)
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return a > b


# ------------------------------------------------------------------ 清单
TEMPLATE_REPO = 'YOURNAME/harp-tool'      # 只有这个字样才算"没配置"


def manifest_url(repo=None):
    """清单地址。repo 为空就用内置的默认仓库（别人拿到的副本什么都不用配）。"""
    repo = (repo or REPO_PLACEHOLDER or '').strip()
    if not repo or repo.upper() == TEMPLATE_REPO.upper():
        return None
    return 'https://github.com/%s/releases/latest/download/update.json' % repo


def _headers():
    """请求头。私有仓库要 token —— 只从**本机配置/环境变量**读，绝不写进 exe。"""
    h = dict(UA)
    tok = os.environ.get('HARP_UPDATE_TOKEN') or ''
    if not tok:
        try:
            import harp_cli
            tok = ((harp_cli.load_config() or {}).get('UPDATE_TOKEN') or '').strip()
        except Exception:
            tok = ''
    if tok:
        h['Authorization'] = 'Bearer ' + tok.strip()
    return h


def _sources(url, mirrors=MIRRORS):
    """[(前缀, 真实地址)]。镜像前缀只对 github.com 原生地址生效。"""
    out = [('', url)]
    if url.startswith('https://github.com/'):
        for pre in mirrors:
            if pre:
                out.append((pre, pre + url))
    return out


def _fetch_one(pre, target, timeout, box):
    try:
        req = urllib.request.Request(target, headers=_headers())
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode('utf-8', 'replace'))
        box.put(('ok', pre, target, data, None))
    except Exception as e:                                    # noqa: BLE001
        box.put(('err', pre, target, None, e))


def fetch_manifest(url, mirrors=MIRRORS, timeout=FETCH_TIMEOUT):
    """取 update.json -> dict；全失败抛异常。返回 (数据, 实际用的地址前缀)

    **并发同时问所有源，谁先成功用谁**（实测直连 17~30s 会超时、镜像 5~8s 稳定），
    所以最坏耗时≈单个源超时，而不是各源之和；胜出的前缀会用于随后的 exe 下载。
    """
    if not url:
        raise RuntimeError('还没有配置更新源（harp_config.json 里的 UPDATE_REPO）')
    sources = _sources(url, mirrors)
    box = queue.Queue()
    for pre, target in sources:
        threading.Thread(target=_fetch_one, args=(pre, target, timeout, box),
                         daemon=True).start()
    errors = {}
    for _ in sources:
        try:
            kind, pre, target, data, err = box.get(timeout=timeout + 5)
        except queue.Empty:
            errors.setdefault('', TimeoutError('等不到任何源的回应'))
            break
        if kind == 'ok':
            data['_from'] = target
            return data, pre
        errors[pre] = err
    # --- 全失败：给出每个源的原因，别只说第一个 ---
    direct = errors.get('')
    if isinstance(direct, urllib.error.HTTPError) and direct.code == 404:
        raise RuntimeError(
            '仓库里还没有发布 Release（或 Release 里没传 update.json）\n'
            '    发布方法：见 harmonica\\release\\发布说明.md')

    def _why(e):
        if isinstance(e, urllib.error.HTTPError):
            return 'HTTP %s' % e.code
        return '%s: %s' % (type(e).__name__, e)

    detail = '；'.join('%s → %s' % ('直连' if not p else p.rstrip('/'), _why(e))
                       for p, e in errors.items())
    raise RuntimeError('取更新清单失败（%d 个源都试过了）：%s' % (len(sources), detail or '?'))


def asset_url(man, mirror=''):
    name = man.get('exe') or 'HarpTool.exe'
    url = man.get('url')
    if not url:
        base = (man.get('_from') or '').rsplit('/', 1)[0]
        url = (base + '/' + name) if base else name
    if mirror and url.startswith('https://github.com/'):
        url = mirror + url
    return url


def check(repo=None, url=None, current=APP_VERSION, mirrors=MIRRORS):
    """查有没有新版本 -> None 或 {'version','notes','url','sha256','size',...}"""
    u = url or manifest_url(repo)
    man, mirror = fetch_manifest(u, mirrors)
    if not is_newer(man.get('version'), current):
        return None
    man['url'] = asset_url(man, mirror)
    return man


# ------------------------------------------------------------------ 下载
def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def download(url, dest, on_progress=None, timeout=30, expect_sha256=None):
    """下载到 dest（带进度回调 on_progress(已下字节, 总字节)）。

    **会校验完整性**（因为镜像实测会把连接掐断、给你一个残缺文件而不报错）：
      · 响应带 Content-Length 时，收到的字节数必须相符；
      · 给了 expect_sha256 就比对，不通过直接抛异常。
    """
    req = urllib.request.Request(url, headers=_headers())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        total = int(r.headers.get('Content-Length') or 0)
        got = 0
        with open(dest, 'wb') as f:
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if on_progress:
                    on_progress(got, total)
    if total and got != total:
        raise RuntimeError('下载不完整：只收到 %d/%d 字节（%s）'
                           % (got, total, urllib.parse.urlsplit(url).netloc))
    if expect_sha256:
        want = expect_sha256.strip().lower()
        got_sha = sha256_file(dest)
        if got_sha != want:
            raise RuntimeError('sha256 对不上（下载不完整或被改过）：%s ≠ %s'
                               % (got_sha[:12], want[:12]))
    return dest


def candidate_urls(man, mirrors=MIRRORS):
    """把清单里那条地址展开成**所有可用源**。

    取清单时胜出的镜像不一定适合下大文件（实测 gh-proxy 只给了 2.87/10.9 MB 就被掐断），
    所以下载阶段要把直连和所有镜像都列出来，逐个试到校验通过为止。
    """
    raw = (man.get('url') or asset_url(man) or '').strip()
    for pre in mirrors:
        if pre and raw.startswith(pre):
            raw = raw[len(pre):]
            break
    urls = [raw] if raw else []
    if raw.startswith('https://github.com/'):
        urls += [pre + raw for pre in mirrors if pre]
    return urls


def download_any(man, dest, on_progress=None, timeout=30, mirror_of=None, on_log=None):
    """依次尝试所有源下载，**每个源都校验 sha256**，第一个通过的就是结果。

    返回 (dest, 实际用的地址)。全失败抛异常。
    """
    want = (man.get('sha256') or '').strip().lower()
    urls = candidate_urls(man, mirror_of or MIRRORS)
    last = None
    for i, u in enumerate(urls):
        try:
            download(u, dest, on_progress=on_progress, timeout=timeout,
                     expect_sha256=want)
            return dest, u
        except Exception as e:                                    # noqa: BLE001
            last = e
            if on_log and i + 1 < len(urls):
                on_log('这个源不行（%s），换下一个' % e)
            continue
    raise RuntimeError('所有下载源都失败：%s' % last)


# ------------------------------------------------------------------ 替换 + 重启
def apply_update(new_exe, target_exe=None, restart=True, on_log=None):
    """把 new_exe 换成 target_exe（默认就是当前 exe），然后重启。

    正在运行的 exe 不能自己覆盖自己：
      · 先把当前 exe **改名** 成 .old（Windows 允许重命名运行中的 exe）
      · 再把 new_exe 复制到原位置
      · 用一个小 .bat 在本进程退出后清理 .old 并启动新版
    返回 .bat 路径（调用方随后退出即可）。
    """
    target = target_exe or (sys.executable if getattr(sys, 'frozen', False) else None)
    if not target:
        raise RuntimeError('源码模式下没有可替换的 exe（只在打包版里生效）')
    log = on_log or (lambda s: None)
    tmp_new = os.path.join(tempfile.gettempdir(), 'harp-update-new.exe')
    try:
        with open(new_exe, 'rb') as a, open(tmp_new, 'wb') as b:
            b.write(a.read())
    except Exception:
        tmp_new = new_exe
    old = target + '.old'
    try:
        if os.path.exists(old):
            os.remove(old)
    except Exception:
        pass
    log('备份旧版 -> %s' % os.path.basename(old))
    try:
        os.rename(target, old)                 # 运行中的 exe 可以改名
    except Exception as e:
        raise RuntimeError('旧版改名失败（可能没有写权限）：%s' % e)
    log('写入新版本…')
    try:
        with open(tmp_new, 'rb') as a, open(target, 'wb') as b:
            b.write(a.read())
    except Exception as e:
        try:
            os.rename(old, target)             # 失败就回滚
        except Exception:
            pass
        raise RuntimeError('写入新版本失败（已回滚）：%s' % e)
    bat = os.path.join(tempfile.gettempdir(), 'harp-update.bat')
    with io.open(bat, 'w', encoding='gbk', errors='replace', newline='\r\n') as f:
        f.write('@echo off\r\n')
        f.write('rem 等旧进程退出，清掉备份，然后启动新版\r\n')
        f.write('ping 127.0.0.1 -n 3 >nul\r\n')
        f.write('del /f /q "%s" >nul 2>nul\r\n' % old)
        f.write('del /f /q "%s" >nul 2>nul\r\n' % tmp_new)
        if restart:
            f.write('start "" "%s"\r\n' % target)
        f.write('del /f /q "%%~f0" >nul 2>nul\r\n')
    log('开始替换并重启…')
    if os.name == 'nt':
        subprocess.Popen(['cmd', '/c', bat], creationflags=CREATE_NO_WINDOW, close_fds=True)
    return bat


# ------------------------------------------------------------------ CLI
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    print('当前版本 %s' % APP_VER)
    url = None
    if '--url' in argv:
        url = argv[argv.index('--url') + 1]
    if '--apply' in argv:
        src = argv[argv.index('--apply') + 1]
        apply_update(src, on_log=lambda s: print('  ' + s))
        return 0
    if '--check' in argv or not argv:
        repo = None
        try:
            import harp_cli
            repo = (harp_cli.load_config() or {}).get('UPDATE_REPO')
        except Exception:
            pass
        try:
            man = check(repo=repo, url=url)
        except Exception as e:
            print('检查失败：%s' % e)
            return 1
        if not man:
            print('已经是最新版 ✓')
            return 0
        print('发现新版本 %s（%s）' % (man.get('version'), man.get('size') or '?'))
        print('  说明：%s' % (man.get('notes') or '（无）'))
        print('  地址：%s' % man.get('url'))
        return 0
    print(__doc__)
    return 0


if __name__ == '__main__':
    sys.exit(main())
