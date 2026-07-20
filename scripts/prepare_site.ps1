# Thin wrapper — real work is in prepare_site.py (Unicode-safe paths)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path (Join-Path $Root "app.py"))) {
  $Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
Set-Location $Root
$env:PYTHONIOENCODING = "utf-8"
python scripts\prepare_site.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
