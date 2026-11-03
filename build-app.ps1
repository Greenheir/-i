#  build-app.ps1 -- package the BRANDED app: GUI + player in one exe.
#  ASCII only on purpose (PowerShell 5.1 reads BOM-less .ps1 as ANSI).
#
#  Double-click the exe        -> graphical UI (console auto-hidden)
#  Drag a score .txt onto it   -> plays that score
#  exe score.txt --dry-run     -> dry run
#
#  Usage:
#     powershell -ExecutionPolicy Bypass -File build-app.ps1
#     powershell -ExecutionPolicy Bypass -File build-app.ps1 -Name "MyBrand-App"

param(
    [string]$Name = 'HarpApp',
    [string]$Icon = 'logo.png',
    [string]$Root = ''
)
$ErrorActionPreference = 'Continue'
if (-not $Root) {
    $Root = $PSScriptRoot
    if (-not $Root) { $Root = (Get-Location).Path }
}
$py = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $py)) { Write-Host "[X] venv not found: $py" -ForegroundColor Red; exit 1 }

# Resolve the icon path: relative paths expand against $Root, and it MUST be a
# regular file (passing a directory to PyInstaller makes CopyIcons fail with
# "Permission denied" on the folder).
if (-not $Icon) { $Icon = 'logo.png' }
if (-not [IO.Path]::IsPathRooted($Icon)) { $Icon = Join-Path $Root $Icon }
$icon = $Icon
if (-not (Test-Path -LiteralPath $icon -PathType Leaf)) {
    Write-Host "[i] icon not found ($icon), generating logo.png ..." -ForegroundColor Yellow
    & $py (Join-Path $Root 'make-logo.py')
    $icon = Join-Path $Root 'logo.png'
}
if (-not (Test-Path -LiteralPath $icon -PathType Leaf)) {
    Write-Host "[X] icon still missing: $icon" -ForegroundColor Red
    exit 1
}
Write-Host "[i] icon = $icon" -ForegroundColor DarkGray

Write-Host '[1/3] install PyInstaller ...' -ForegroundColor Cyan
& $py -m pip install --no-cache-dir pyinstaller pillow -i https://pypi.tuna.tsinghua.edu.cn/simple --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Write-Host '[X] pip failed' -ForegroundColor Red; exit 1 }

Write-Host "[2/3] building $Name.exe (GUI + player, branded icon) ..." -ForegroundColor Cyan
Push-Location $Root
& $py -m PyInstaller --onefile --console --clean --noconfirm `
    --name $Name `
    --icon $icon `
    --hidden-import harp_gui `
    --hidden-import harp_dfh `
    --hidden-import harp_lib `
    --hidden-import harp_icons `
    --hidden-import harp_update `
    --hidden-import harp2 `
    --hidden-import harp_cli `
    --hidden-import harp_app `
    --hidden-import pynput.keyboard._win32 `
    --hidden-import pynput.mouse._win32 `
    --exclude-module torch --exclude-module torchaudio --exclude-module tensorflow `
    --exclude-module demucs --exclude-module librosa --exclude-module numpy `
    --exclude-module scipy --exclude-module sklearn --exclude-module numba `
    --exclude-module matplotlib --exclude-module pandas --exclude-module PIL.ImageQt `
    --distpath (Join-Path $Root 'dist') `
    --workpath (Join-Path $Root 'build') `
    --specpath (Join-Path $Root 'build') `
    (Join-Path $Root 'harp_app.py')
$rc = $LASTEXITCODE
Pop-Location
if ($rc -ne 0) { Write-Host '[X] build failed' -ForegroundColor Red; exit 1 }

$exe = Join-Path $Root ("dist\{0}.exe" -f $Name)
if (-not (Test-Path -LiteralPath $exe)) { Write-Host '[X] exe not found' -ForegroundColor Red; exit 1 }

Write-Host '[3/3] smoke test ...' -ForegroundColor Cyan
Write-Host '  --release :' -NoNewline; & $exe --release | Select-Object -First 1
Write-Host '  --dry-run :' -NoNewline
& $exe (Join-Path $Root 'lantingxu-dfh.txt') --dry-run | Select-Object -Last 2 | Select-Object -First 1

$mb = [math]::Round((Get-Item -LiteralPath $exe).Length / 1MB, 1)
Write-Host ''
Write-Host "DONE: $exe  ($mb MB)" -ForegroundColor Green
Write-Host 'Double-click = GUI ; drag a score .txt onto it = play'
