<#
    setup-env.ps1 - build a 64-bit Python venv for the harmonica pipeline
    (demucs / basic-pitch / mido / pynput / soundfile).

    What this machine actually has (measured):
      py -3.12        64-bit  <- usable
      py -3.12-32     32-bit  <- unusable (no torch/tensorflow wheels)
      python          32-bit Python 3.10 (first on PATH!) <- unusable
    So this script always picks a 64-BIT interpreter and never trusts `python`.

    About basic-pitch: its metadata pins "tensorflow<2.15.1" on Python >= 3.11,
    and TensorFlow 2.15.x has no CPython 3.12 wheels, so a normal
    `pip install basic-pitch` on 3.12 dies with ResolutionImpossible.
    We therefore install it with --no-deps and then install its dependencies
    (including a current TensorFlow) explicitly.

    Default = CPU-only torch (~1.5 GB). Use -Cuda for the GPU build (+5 GB).

    Usage:
      powershell -ExecutionPolicy Bypass -File setup-env.ps1
      powershell -ExecutionPolicy Bypass -File setup-env.ps1 -Cuda

    Pure ASCII on purpose (Windows PowerShell 5.1 + BOM-less files).
#>
[CmdletBinding()]
param(
    [switch]$Cuda,
    [string]$Root = '',
    [int]$MinFreeGB = 6,
    [switch]$SkipBasicPitch
)

# 'Continue' on purpose: native commands (python/pip) write progress to stderr,
# and with 'Stop' PowerShell turns that into a terminating NativeCommandError.
$ErrorActionPreference = 'Continue'
$Mirror = 'https://pypi.tuna.tsinghua.edu.cn/simple'

function Info($m) { Write-Host "[setup] $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "[warn ] $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "[FAIL ] $m" -ForegroundColor Red; exit 1 }

if (-not $Root) {
    $Root = $PSScriptRoot
    if (-not $Root) { $Root = Split-Path -Parent $MyInvocation.MyCommand.Path }
    if (-not $Root) { $Root = (Get-Location).Path }
}
Info "work dir : $Root"

# ---------------------------------------------------------------- 1. disk space
$driveName = 'C'
try {
    $driveName = (Get-Item -LiteralPath $Root).PSDrive.Name
    $di = New-Object System.IO.DriveInfo($driveName)
    $freeGB = [math]::Round($di.AvailableFreeSpace / 1GB, 2)
} catch { $freeGB = -1 }
if ($freeGB -ge 0) {
    Info "free space on ${driveName}: $freeGB GB"
    if ($freeGB -lt $MinFreeGB) {
        Die "only $freeGB GB free (< $MinFreeGB GB). Free space first, or pass -MinFreeGB <n>."
    }
}

# ---------------------------------------------------------- 2. locate python64
function Get-Python64 {
    $cands = @(
        @{ Exe = 'py';     Pre = @('-3.12') },
        @{ Exe = 'py';     Pre = @('-3.11') },
        @{ Exe = 'py';     Pre = @('-3.10') },
        @{ Exe = 'python'; Pre = @() }
    )
    foreach ($c in $cands) {
        if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $out = & $c.Exe @($c.Pre) -c "import struct,sys;print(str(struct.calcsize('P')*8)+'|'+sys.version.split()[0])" 2>$null
        } catch { continue }
        if ($LASTEXITCODE -ne 0 -or -not $out) { continue }
        $txt = ("$out" | Select-Object -First 1).Trim()
        $bits, $ver = $txt.Split('|')
        $label = ($c.Exe + ' ' + ($c.Pre -join ' ')).Trim()
        if ($bits -ne '64') {
            Warn "$label is $bits-bit ($ver) - skipped (torch/tensorflow ship no 32-bit wheels)"
            continue
        }
        if ([version]$ver -lt [version]'3.10') {
            Warn "$label is $ver - too old, skipped"
            continue
        }
        Info "found 64-bit Python $ver  ($label)"
        $c.Version = $ver
        return $c
    }
    return $null
}

$py = Get-Python64
if (-not $py) { Die "no 64-bit Python >= 3.10 found (try: py -0p)" }
$want = '{0}.{1}' -f ([version]$py.Version).Major, ([version]$py.Version).Minor

# ------------------------------------------------------------- 3. create venv
$venv = Join-Path $Root '.venv'
$vpy  = Join-Path $venv 'Scripts\python.exe'
if (Test-Path -LiteralPath $vpy) {
    $existing = (& $vpy -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null | Select-Object -First 1)
    if ("$existing".Trim() -ne $want) {
        Warn "existing venv runs Python $existing but we need $want - recreating"
        Remove-Item -LiteralPath $venv -Recurse -Force
    } else {
        Info "venv already exists: $venv"
    }
}
if (-not (Test-Path -LiteralPath $vpy)) {
    Info "creating venv: $venv"
    & $py.Exe @($py.Pre) -m venv $venv
    if ($LASTEXITCODE -ne 0) { Die "venv creation failed" }
}
& $vpy -c "import sys;assert sys.maxsize > 2**32, 'venv is 32-bit'" 2>$null
if ($LASTEXITCODE -ne 0) { Die "venv interpreter is not 64-bit" }

# ------------------------------------------------------------------- 4. pip up
Info "upgrading pip"
& $vpy -m pip install --upgrade pip -i $Mirror --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Warn "pip upgrade failed - continuing" }

# ------------------------------------------------------------------ 5. pytorch
if ($Cuda) {
    Info "installing CUDA torch (large: ~5 GB on disk)"
    $torchIndex = 'https://download.pytorch.org/whl/cu124'
} else {
    Info "installing CPU-only torch (small: ~1.5 GB on disk)"
    $torchIndex = 'https://download.pytorch.org/whl/cpu'
}
& $vpy -m pip install --no-cache-dir torch torchaudio --index-url $torchIndex --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Die "torch install failed" }

# --------------------------------------------------------------- 6. the rest
# numpy is capped: numba (via resampy, via basic-pitch) lags behind the newest
# numpy, and without the cap pip backtracks for minutes and then tries to BUILD
# old sdists. --only-binary=:all: forbids source builds (no compiler on this box).
Info "installing demucs / mido / pynput / soundfile"
$pkgs = @('demucs', 'mido', 'pynput', 'soundfile', 'numpy<2.3')
& $vpy -m pip install --no-cache-dir --only-binary=:all: @pkgs -i $Mirror --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Die "core dependency install failed (see log above)" }

if (-not $SkipBasicPitch) {
    Info "installing basic-pitch (pin relaxed: --no-deps, then deps by hand)"
    & $vpy -m pip install --no-cache-dir --only-binary=:all: --no-deps basic-pitch -i $Mirror --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) { Die "basic-pitch install failed" }
    # basic-pitch's real dependency set, with a current TensorFlow instead of <2.15.1
    $bpDeps = @('librosa', 'mir_eval', 'pretty_midi', 'resampy', 'scikit-learn',
                'scipy', 'smart_open', 'tensorflow')
    & $vpy -m pip install --no-cache-dir --only-binary=:all: @bpDeps -i $Mirror --disable-pip-version-check
    if ($LASTEXITCODE -ne 0) { Die "basic-pitch dependencies failed (see log above)" }
}

# ---------------------------------------------------------------- 7. verify
Info "verifying imports"
$check = @'
import sys
mods = []
import torch;            mods.append(('torch', torch.__version__, 'cuda' if torch.cuda.is_available() else 'cpu'))
import torchaudio;       mods.append(('torchaudio', torchaudio.__version__, ''))
import demucs;           mods.append(('demucs', getattr(demucs, '__version__', '?'), ''))
import mido;             mods.append(('mido', mido.__version__, ''))
import pynput;           mods.append(('pynput', getattr(pynput, '__version__', '?'), ''))
import soundfile;        mods.append(('soundfile', soundfile.__version__, ''))
try:
    import tensorflow as tf; mods.append(('tensorflow', tf.__version__, ''))
except Exception as e:
    mods.append(('tensorflow', 'MISSING: %s' % e, ''))
try:
    import basic_pitch;      mods.append(('basic-pitch', getattr(basic_pitch, '__version__', '?'), ''))
except Exception as e:
    mods.append(('basic-pitch', 'MISSING: %s' % e, ''))
width = max(len(m[0]) for m in mods)
for name, ver, tag in mods:
    print('  {0:<{1}}  {2}  {3}'.format(name, width, ver, tag))
print('ENV OK')
'@
& $vpy -c $check
if ($LASTEXITCODE -ne 0) { Die "some import failed - read the traceback above" }

Info "python for this env: $vpy"
Info "done"
