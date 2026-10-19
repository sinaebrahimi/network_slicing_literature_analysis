# One-time setup on Windows (PowerShell):  powershell -ExecutionPolicy Bypass -File scripts\install.ps1
# Creates .venv, installs litsuite with every optional component, downloads the browser
# used for institutional downloads, and copies the example settings if you have none yet.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
python -m venv .venv
& .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[all]"
python -m playwright install chromium
if (-not (Test-Path config\settings.yaml)) {
    Copy-Item config\settings.example.yaml config\settings.yaml
    Write-Host "Created config\settings.yaml - edit it (or run: litsuite init)."
}
Write-Host ""
Write-Host "Done. Activate the environment in each new terminal with:  .\.venv\Scripts\Activate.ps1"
Write-Host "Then try:  litsuite doctor"
