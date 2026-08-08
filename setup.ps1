# NeuroDrive one-command setup (Windows / PowerShell).
#
#     .\setup.ps1
#
# Creates a virtual environment, installs the dependencies, and runs the
# test suite so you know the checkout is healthy before touching hardware.
#
# Should-Have item from plan section 10.2: "clean setup script for Python
# dependencies (one-command install)".

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $root ".venv"
$requirements = Join-Path $root "python_bridge\requirements.txt"

Write-Host ""
Write-Host "  NeuroDrive setup" -ForegroundColor Cyan
Write-Host "  ----------------------------------------"

# --- Python ----------------------------------------------------------------

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host "  Python was not found on PATH." -ForegroundColor Red
    Write-Host "  Install Python 3.10 or newer from https://python.org and"
    Write-Host "  tick 'Add python.exe to PATH' during installation."
    exit 1
}

$version = & python -c "import sys; print('%d.%d' % sys.version_info[:2])"
Write-Host "  Python $version found"

$tooOld = & python -c "import sys; print(1 if sys.version_info < (3, 10) else 0)"
if ($tooOld -eq "1") {
    Write-Host "  Python 3.10 or newer is required (found $version)." -ForegroundColor Red
    exit 1
}

# --- Virtual environment ---------------------------------------------------

if (-not (Test-Path $venv)) {
    Write-Host "  Creating virtual environment in .venv ..."
    & python -m venv $venv
} else {
    Write-Host "  Reusing the existing .venv"
}

$venvPython = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "  The virtual environment looks broken. Delete .venv and re-run." -ForegroundColor Red
    exit 1
}

# --- Dependencies ----------------------------------------------------------

Write-Host "  Installing dependencies ..."
& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install -r $requirements --quiet
Write-Host "  Dependencies installed"

# --- Verify ----------------------------------------------------------------

Write-Host "  Running the test suite (this takes about a minute) ..."
Push-Location $root
try {
    & $venvPython -m pytest tests -q
    $testsPassed = ($LASTEXITCODE -eq 0)
} finally {
    Pop-Location
}

Write-Host "  ----------------------------------------"
if ($testsPassed) {
    Write-Host "  Setup complete." -ForegroundColor Green
} else {
    Write-Host "  Setup finished, but some tests failed." -ForegroundColor Yellow
    Write-Host "  Report this to M7 before continuing."
}

Write-Host ""
Write-Host "  Next steps:"
Write-Host "    .\.venv\Scripts\Activate.ps1        activate the environment"
Write-Host "    cd python_bridge; python main.py    run the bridge (mock EEG)"
Write-Host "    python tests\fake_esp32.py          simulate the vehicle"
Write-Host ""
Write-Host "  See README.md for the hardware setup."
Write-Host ""

if (-not $testsPassed) { exit 1 }
