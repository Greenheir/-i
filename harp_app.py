# -*- coding: utf-8 -*-
"""
harp_app.py —— 双模式入口（打包成 exe 后靠它分流）

    harp_app.exe                       → 打开图形界面（口琴工具台）
    harp_app.exe 谱子.txt               → 直接演奏（拖拽到 exe 上就是这个）
    harp_app.exe 谱子.txt --dry-run     → 只打印按键序列
    harp_app.exe 谱子.txt --plan        → 逐音符时间表
    harp_app.exe --release              → 急救：松开鼠标左/右/中键
    harp_app.exe --player ...           → 强制按命令行播放器处理
    harp_app.exe --gui                  → 强制打开界面

界面里点「▶ 开始演奏 / 干跑测试」时，会用 `--player` 拉起**同一个 exe** 的子进程，
所以打包版不需要额外脚本或 Python 环境。
"""

import os
import sys

BRAND = '明晚一定早睡'

# 这些参数出现 => 走命令行播放器
CLI_FLAGS = {
    '--player', '--dry-run', '--plan', '--release', '--song', '--bpm', '--speed',
    '--countdown', '--from-bar', '--to-bar', '--no-wait', '--no-elevate',
    '--hold-mods', '--probe', '--probe-hold', '--keytest',
}


def main():
    argv = list(sys.argv[1:])

    if '--gui' in argv:                      # 明确要界面
        argv = [a for a in argv if a != '--gui']
        return run_gui()

    if '--libcheck' in argv:                 # 诊断：在线曲库模块有没有打进来
        return libcheck()

    # 第一个参数是"存在的文件" => 拖拽/命令行演奏
    score_like = bool(argv) and not argv[0].startswith('-') and os.path.isfile(argv[0])
    cli_like = any(a in CLI_FLAGS for a in argv)

    if score_like or cli_like:
        argv = [a for a in argv if a != '--player']
        sys.argv = [sys.argv[0]] + argv
        # --probe / --probe-hold / --keytest 这几个自检功能在 harp2.py 里
        if any(a in ('--probe', '--probe-hold', '--keytest') for a in argv):
            import harp2
            return harp2.main()
        import harp_dfh
        return harp_dfh.main()

    return run_gui()


def run_gui():
    import harp_gui
    return harp_gui.main()


def libcheck():
    """自检：在线曲库模块在不在、索引读不读得到（有缓存就不用联网）。"""
    try:
        import harp_lib
    except Exception as e:
        print('[X] harp_lib 没打进来：%s' % e)
        return 1
    print('[i] harp_lib: %s' % getattr(harp_lib, '__file__', '?'))
    print('[i] 缓存目录: %s' % harp_lib.CACHE_DIR)
    try:
        n, shards, t = harp_lib.index()
        print('[i] 曲库索引：%d 首 / %d 个分片' % (n, len(shards)))
    except Exception as e:
        print('[!] 取索引失败（没网也能用缓存）：%s' % e)
    try:
        rows = harp_lib.search('兰亭序')
        print('[i] 试搜「兰亭序」：%d 条' % len(rows))
    except Exception as e:
        print('[!] 搜索失败：%s' % e)
    # ---- 自动更新 ----
    try:
        import harp_update
        print('[i] 更新模块：%s（当前版本 %s）'
              % (getattr(harp_update, '__file__', '?'), harp_update.APP_VER))
        url = harp_update.manifest_url(getattr(harp_update, 'REPO_PLACEHOLDER', None))
        print('[i] 更新源  ：%s' % url)
        try:
            man = harp_update.check(current=harp_update.APP_VERSION)
            if man:
                print('[i] 有新版本：%s（%s）' % (man['version'], man.get('notes') or ''))
            else:
                print('[i] 已经是最新版')
        except Exception as e:
            print('[!] 检查更新失败（发 Release 之前这很正常）：%s' % e)
    except Exception as e:
        print('[X] harp_update 没打进来：%s' % e)
    print('LIBCHECKOK')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        pass
