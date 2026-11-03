# 明晚一定早睡 · 口琴工具台

Windows 游戏口琴工具，提供本机谱库、在线曲库、干跑测试和自动演奏。

现成程序见 [Releases](https://github.com/Greenheir/-i/releases)。

## 运行源码

使用 Windows 和 64 位 Python 3.10 或更高版本。图形界面使用 Python 自带的 tkinter；基础演奏依赖 pynput。

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install pynput
.\harp-gui.cmd
```

在界面中选择本机谱子，或搜索并下载在线谱面。演奏参数、谱面解析和按键发送沿用原有实现。

## 验证

干跑只打印按键序列，不发送按键：

```powershell
.venv\Scripts\python.exe harp2.py demo-score.txt --dry-run
.venv\Scripts\python.exe harp_gui.py --selftest
```

实际演奏前，将输入法切至英文，在游戏中打开口琴界面，并按原有流程获取管理员权限。Esc 急停，侧栏「解卡」用于松开卡住的按键。

## 可选工具

- 完整音频分离、MIDI 转谱环境：`setup-env.ps1`。
- 打包 GUI 与播放器：`build-app.ps1 -Name HarpTool`。

`harp_config.json` 是本机配置，不提交到仓库。谱子、缓存、日志、虚拟环境和构建产物也不纳入版本控制；仓库仅附通用示范谱。
