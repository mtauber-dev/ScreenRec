# Builds dist\ScreenRec\ScreenRec.exe and a zip of that folder to share.
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File packaging\build.ps1
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'

# The real ffmpeg folder (scoop puts shims on PATH; winutil.ffmpeg_exe resolves them).
$env:SCREENREC_FFMPEG_DIR = Split-Path (& $py -c "import sys; sys.path.insert(0, r'$root'); from screenrec import winutil; print(winutil.ffmpeg_exe())")

& $py (Join-Path $PSScriptRoot 'make_icon.py') (Join-Path $PSScriptRoot 'ScreenRec.ico')
& $py -m PyInstaller --noconfirm --clean `
    --distpath (Join-Path $root 'dist') --workpath (Join-Path $root 'build') `
    (Join-Path $PSScriptRoot 'ScreenRec.spec')
if ($LASTEXITCODE) { throw "PyInstaller failed ($LASTEXITCODE)" }

$zip = Join-Path $root 'dist\ScreenRec.zip'
Remove-Item $zip -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $root 'dist\ScreenRec') -DestinationPath $zip
Write-Host "Built: $(Join-Path $root 'dist\ScreenRec\ScreenRec.exe')"
Write-Host "Zip:   $zip ($([math]::Round((Get-Item $zip).Length / 1MB)) MB)"
