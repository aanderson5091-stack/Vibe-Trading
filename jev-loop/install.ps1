# jev-loop local installer (Windows PowerShell).
#
# Copies this skill into $HOME\.claude\skills\jev-loop\ so Claude Code can load
# it, then creates the venv and runs the test suite. Paper-trading by default;
# going live is a separate, deliberate three-gate opt-in documented in SKILL.md.
#
# Usage:  powershell -ExecutionPolicy Bypass -File install.ps1
$ErrorActionPreference = "Stop"

$Src  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Dest = Join-Path $HOME ".claude\skills\jev-loop"

Write-Host "jev-loop installer"
Write-Host "  source: $Src"
Write-Host "  dest:   $Dest"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Write-Host "note: 'uv' not found. Install it first:"
  Write-Host '      powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
  Write-Host "      then re-run this script."
}

New-Item -ItemType Directory -Force -Path (Join-Path $HOME ".claude\skills") | Out-Null

if (Test-Path $Dest) {
  $Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
  $Bak = Join-Path $HOME ".claude\skills\.jev-loop.bak.$Stamp"
  Move-Item $Dest $Bak
  Write-Host "previous install backed up to $Bak"
}

New-Item -ItemType Directory -Force -Path $Dest | Out-Null

$exclude = @('.venv','__pycache__','*.pyc','*.egg-info','.env','install.sh','install.ps1')
Get-ChildItem -Path $Src -Force | Where-Object {
  $_.Name -notin @('.venv','.env','install.sh','install.ps1') -and
  $_.Name -notlike '*.egg-info'
} | ForEach-Object {
  Copy-Item $_.FullName -Destination $Dest -Recurse -Force
}
# Prune any __pycache__ / pyc that slipped through.
Get-ChildItem -Path $Dest -Recurse -Force -Include '__pycache__','*.pyc','*.egg-info' -ErrorAction SilentlyContinue |
  Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "copied skill -> $Dest"

if (Get-Command uv -ErrorAction SilentlyContinue) {
  Push-Location $Dest
  uv venv | Out-Null
  uv pip install -q -e '.[dev]'
  uv run python -m pytest -q
  Pop-Location
  Write-Host ""
  Write-Host "installed and tests pass."
} else {
  Write-Host "skill files in place; install 'uv' then run:  cd `"$Dest`"; uv venv; uv pip install -e '.[dev]'; uv run python -m pytest -q"
}

Write-Host ""
Write-Host "Next:"
Write-Host "  1. Copy .env.example to .env in $Dest and add your Alpaca *paper* keys"
Write-Host "  2. cd `"$Dest`""
Write-Host "  3. uv run --env-file .env python -m jevloop run --paper --symbol BTC/USD --ticks 30"
Write-Host "  4. uv run --env-file .env python -m jevloop serve     # live dashboard"
Write-Host ""
Write-Host "Paper by default. Read SKILL.md before considering live trading."
