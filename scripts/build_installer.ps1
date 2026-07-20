# Build EXE + Windows installer for 万国觉醒题库
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $Root "app.py"))) {
  $Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
Set-Location $Root
$env:PYTHONIOENCODING = "utf-8"

Write-Host "==> Installing build deps..." -ForegroundColor Cyan
python -m pip install -q pyinstaller pillow numpy rapidfuzz flask rapidocr-onnxruntime onnxruntime pywebview

Write-Host "==> Cleaning old build..." -ForegroundColor Cyan
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue build, dist\万国觉醒题库, dist\installer

Write-Host "==> Building desktop EXE with PyInstaller..." -ForegroundColor Cyan
python -m PyInstaller --noconfirm --clean packaging\rok_quiz.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$exeDir = Join-Path $Root "dist\万国觉醒题库"
$exeName = "万国觉醒题库.exe"
$exe = Join-Path $exeDir $exeName
# Avoid PowerShell encoding issues with Chinese paths
$exeOk = python -c "from pathlib import Path; import sys; sys.exit(0 if Path(r'''$exe''').is_file() else 1)"
if ($LASTEXITCODE -ne 0) { throw "EXE not found after build (check dist folder)" }
Write-Host "EXE ready" -ForegroundColor Green
python -c "from pathlib import Path; p=Path(r'''$exe'''); print(p, round(p.stat().st_size/1024/1024,2), 'MB')"

function Find-ISCC {
  $candidates = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
    "${env:LocalAppData}\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 7\ISCC.exe",
    "${env:ProgramFiles}\Inno Setup 7\ISCC.exe"
  )
  foreach ($c in $candidates) {
    if (Test-Path $c) { return $c }
  }
  $cmd = Get-Command iscc -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  return $null
}

$iscc = Find-ISCC
if (-not $iscc) {
  Write-Host "==> Installing Inno Setup via winget..." -ForegroundColor Cyan
  winget install --id JRSoftware.InnoSetup -e --accept-package-agreements --accept-source-agreements
  $iscc = Find-ISCC
}

if (-not $iscc) {
  Write-Host "Inno Setup not found. Creating portable ZIP instead..." -ForegroundColor Yellow
  New-Item -ItemType Directory -Force -Path "dist\installer" | Out-Null
  $zip = "dist\installer\万国觉醒题库_便携版_v1.0.0.zip"
  if (Test-Path $zip) { Remove-Item $zip -Force }
  Compress-Archive -Path "dist\万国觉醒题库\*" -DestinationPath $zip -Force
  Write-Host "Portable package: $((Resolve-Path $zip).Path)" -ForegroundColor Green
  exit 0
}

Write-Host "==> Building installer with Inno Setup..." -ForegroundColor Cyan
& $iscc "packaging\setup.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }

$setup = Get-ChildItem "dist\installer\*.exe" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
Write-Host "Installer ready: $($setup.FullName)" -ForegroundColor Green
