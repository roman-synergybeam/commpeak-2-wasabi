# c2w transcription agent -- Windows installer.
#
# Run in PowerShell (no admin needed):
#   powershell -ExecutionPolicy Bypass -c "irm __C2W_SERVER__/api/agent/v1/download/install.ps1 | iex"
#
# Installs into %LOCALAPPDATA%\c2w-agent: a private Python environment with
# faster-whisper and the CUDA libraries, the agent itself, and your token.
# Re-running it upgrades the agent and keeps the token unless you replace it.

$ErrorActionPreference = "Stop"
$Server = "__C2W_SERVER__"
$AgentHome = Join-Path $env:LOCALAPPDATA "c2w-agent"
$Venv = Join-Path $AgentHome "venv"
$Py = Join-Path $Venv "Scripts\python.exe"
$Config = Join-Path $AgentHome "config.json"

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }

New-Item -ItemType Directory -Force $AgentHome | Out-Null

Step "Checking the NVIDIA driver"
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($smi) {
    & nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
} else {
    Write-Warning "nvidia-smi was not found. Install the current NVIDIA driver (GeForce Experience or nvidia.com) before running the agent."
}

Step "Finding Python 3.12"
$python = $null
try { $python = (& py -3.12 -c "import sys; print(sys.executable)" 2>$null) } catch {}
if (-not $python) {
    $candidate = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
    if (Test-Path $candidate) { $python = $candidate }
}
if (-not $python) {
    Write-Host "Installing Python 3.12 for this user with winget..."
    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
    $python = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
    if (-not (Test-Path $python)) { throw "Python did not install. Install Python 3.12 from python.org and run this again." }
}
Write-Host "Using $python"

Step "Creating the agent's own Python environment"
if (-not (Test-Path $Py)) { & $python -m venv $Venv }
& $Py -m pip install --quiet --upgrade pip
Write-Host "Installing faster-whisper and the CUDA 12 libraries (about 1.5 GB, a few minutes)..."
# av below 19: PyAV 19 removed the `metadata_errors` argument faster-whisper
# 1.2.1 passes when it opens audio, and every decode fails with a TypeError.
& $Py -m pip install --quiet --upgrade "faster-whisper==1.2.1" "av>=11,<19" requests numpy nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
if ($LASTEXITCODE -ne 0) { throw "pip could not install the packages." }

Step "Downloading the agent"
Invoke-WebRequest -UseBasicParsing "$Server/api/agent/v1/download/c2w_agent.py" -OutFile (Join-Path $AgentHome "c2w_agent.py")

Step "Your agent token"
$existing = $null
if (Test-Path $Config) { $existing = (Get-Content $Config -Raw | ConvertFrom-Json).token }
if ($existing) {
    $answer = Read-Host "A token is already saved. Press Enter to keep it, or paste a new one"
    $token = if ($answer.Trim()) { $answer.Trim() } else { $existing }
} else {
    $token = (Read-Host "Paste the token from 'c2w-admin agent create'").Trim()
}
if (-not $token.StartsWith("c2wa_")) { throw "That does not look like an agent token (they start with c2wa_)." }
# Written without a byte-order mark: PowerShell 5.1's "-Encoding UTF8" adds one.
$json = @{ server = $Server; token = $token; model = "turbo" } | ConvertTo-Json
[System.IO.File]::WriteAllText($Config, $json, (New-Object System.Text.UTF8Encoding $false))
# Only this Windows user can read the token.
icacls $Config /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null

Step "Making a shortcut"
$Run = Join-Path $AgentHome "run.cmd"
@"
@echo off
title c2w transcription agent
"$Py" "$AgentHome\c2w_agent.py" %*
pause
"@ | Set-Content -Encoding ASCII $Run
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Desktop")) "c2w transcriber.lnk"))
$link.TargetPath = $Run
$link.WorkingDirectory = $AgentHome
$link.Save()

$auto = Read-Host "Start the agent automatically when you sign in to Windows? [y/N]"
$startup = Join-Path ([Environment]::GetFolderPath("Startup")) "c2w transcriber.lnk"
if ($auto -match "^[yY]") {
    $link = $shell.CreateShortcut($startup)
    $link.TargetPath = $Run
    $link.WorkingDirectory = $AgentHome
    $link.WindowStyle = 7   # minimised
    $link.Save()
    Write-Host "It will start minimised at sign-in."
} elseif (Test-Path $startup) {
    Remove-Item $startup
}

Step "Checking the GPU, the model and the token (first run downloads the model)"
& $Py (Join-Path $AgentHome "c2w_agent.py") --check
if ($LASTEXITCODE -ne 0) { throw "The check failed; see the message above." }

Write-Host "`nDone. Start it from the 'c2w transcriber' shortcut on your desktop." -ForegroundColor Green
