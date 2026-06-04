# install-windows.ps1 — One-shot setup for mangrove-agent on Windows.
# Checks for each dependency, installs if missing, verifies, then moves on.
# Run this from PowerShell — it will auto-elevate to Administrator if needed.

#Requires -Version 5.1

# ─── Version requirements ─────────────────────────────────────────────────────
# Change these if the application's minimum requirements change.
$PYTHON_MIN = [version]"3.11"
$NODE_MIN_MAJOR = 18

# ─── Step 0: Execution policy ─────────────────────────────────────────────────
Set-ExecutionPolicy Bypass -Scope Process -Force

# ─── Step 1: Auto-elevate to Administrator ────────────────────────────────────
# Some installs require admin rights. If not already admin, relaunch as admin.
if (-NOT ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "  Requesting Administrator permissions..." -ForegroundColor Yellow
    Start-Process PowerShell -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$($MyInvocation.MyCommand.Path)`""
    Exit
}

# After elevation PowerShell opens in C:\Windows\System32 by default.
# Go back to where the script actually lives.
if ($PSScriptRoot) { Set-Location $PSScriptRoot }

# ─── Output helpers ───────────────────────────────────────────────────────────

function ok   { param($msg) Write-Host "  [OK] $msg" -ForegroundColor Green }
function info { param($msg) Write-Host "  --> $msg" -ForegroundColor Yellow }
function fail { param($msg) Write-Host "  [X] $msg" -ForegroundColor Red; Read-Host "`nPress Enter to close"; Exit 1 }

# Reads the updated PATH from the Windows registry and applies it to the
# current session so newly installed tools are available without restarting.
function Refresh-Path {
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path","User")
}

# ─── Welcome ──────────────────────────────────────────────────────────────────

Clear-Host
Write-Host ""
Write-Host "  ==========================================" -ForegroundColor Cyan
Write-Host "    Mangrove Agent — Windows Setup" -ForegroundColor Cyan
Write-Host "  ==========================================" -ForegroundColor Cyan
Write-Host "  This script installs everything you need" -ForegroundColor White
Write-Host "  and gets you running. Running as Admin." -ForegroundColor White
Write-Host ""

# ─── Step 1: Check winget ─────────────────────────────────────────────────────

Write-Host "`n  ── Step 1 — Checking winget ──────────────" -ForegroundColor Cyan

if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    fail "winget not found. Update Windows to version 1809 or newer, or install 'App Installer' from the Microsoft Store, then re-run this script."
}
ok "winget is available"

# ─── Step 2: Git ──────────────────────────────────────────────────────────────

Write-Host "`n  ── Step 2 — Git ──────────────────────────" -ForegroundColor Cyan

if (Get-Command git -ErrorAction SilentlyContinue) {
    ok "Git already installed — $(git --version)"
} else {
    info "Installing Git..."
    winget install --id Git.Git -e --source winget --silent --accept-package-agreements --accept-source-agreements
    Refresh-Path
    if (Get-Command git -ErrorAction SilentlyContinue) {
        ok "Git installed — $(git --version)"
    } else {
        fail "Git installation failed. Install manually from https://git-scm.com/download/win then re-run."
    }
}

# ─── Step 3: Python ───────────────────────────────────────────────────────────

Write-Host "`n  ── Step 3 — Python ───────────────────────" -ForegroundColor Cyan

# The app requires Python $PYTHON_MIN or newer.
function Find-Python {
    foreach ($cmd in @("python", "python3", "py")) {
        if (Get-Command $cmd -ErrorAction SilentlyContinue) {
            $ver = & $cmd --version 2>&1
            if ($ver -match "(\d+\.\d+\.\d+)") {
                $parsed = [version]$Matches[1]
                if ($parsed -ge $PYTHON_MIN) { return $cmd }
            }
        }
    }
    return $null
}

$pythonCmd = Find-Python

if ($pythonCmd) {
    ok "Python $(& $pythonCmd --version) already meets the requirement (>= $PYTHON_MIN) — skipping"
} else {
    $existing = foreach ($cmd in @("python", "python3", "py")) {
        if (Get-Command $cmd -ErrorAction SilentlyContinue) { & $cmd --version 2>&1; break }
    }
    if ($existing) {
        info "Python $existing is below the required $PYTHON_MIN — installing 3.12..."
    } else {
        info "Python not found — installing 3.12..."
    }
    winget install --id Python.Python.3.12 -e --source winget --silent --accept-package-agreements --accept-source-agreements
    Refresh-Path
    $pythonCmd = Find-Python
    if ($pythonCmd) {
        ok "Python $(& $pythonCmd --version) installed and meets the requirement"
    } else {
        fail "Python 3.12 was installed but does not meet the requirement (>= $PYTHON_MIN). Close PowerShell and re-run."
    }
}

# Create a python3 alias in Git Bash so setup.sh can call python3.
# setup.sh expects `python3` — on Windows Python installs as `python` or `py`.
$gitBashProfileDir = "C:\Program Files\Git\etc\profile.d"
if (Test-Path $gitBashProfileDir) {
    $aliasFile = "$gitBashProfileDir\python3-alias.sh"
    if (-not (Test-Path $aliasFile)) {
        Set-Content -Path $aliasFile -Value "alias python3='python'" -Encoding UTF8
        ok "Created python3 alias for Git Bash"
    }
}

# ─── Step 4: Node.js ──────────────────────────────────────────────────────────

Write-Host "`n  ── Step 4 — Node.js ──────────────────────" -ForegroundColor Cyan

# The app requires Node.js $NODE_MIN_MAJOR or newer.
function Node-MeetsRequirement {
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) { return $false }
    $major = [int]((node --version) -replace "v(\d+)\..*", '$1')
    return $major -ge $NODE_MIN_MAJOR
}

if (Node-MeetsRequirement) {
    ok "Node.js $(node --version) already meets the requirement (>= v$NODE_MIN_MAJOR) — skipping"
} else {
    if (Get-Command node -ErrorAction SilentlyContinue) {
        info "Node.js $(node --version) is below the required v$NODE_MIN_MAJOR — installing LTS..."
    } else {
        info "Node.js not found — installing LTS..."
    }
    winget install --id OpenJS.NodeJS.LTS -e --source winget --silent --accept-package-agreements --accept-source-agreements
    Refresh-Path
    if (Node-MeetsRequirement) {
        ok "Node.js $(node --version) installed and meets the requirement"
    } else {
        fail "Node.js was installed but does not meet the requirement (>= v$NODE_MIN_MAJOR). Close PowerShell and re-run."
    }
}

# Define npm global path and Unix path helper upfront.
# $npmGlobal is used in both the Claude install block AND Step 9 — must be set before either.
$npmGlobal = "$env:APPDATA\npm"

# Converts a Windows path to a Unix-style path for use in Git Bash.
# Handles any drive letter, not just C:.
function Convert-ToUnixPath($winPath) {
    $p = $winPath -replace "\\", "/"
    if ($p -match "^([A-Za-z]):") {
        $p = "/" + $Matches[1].ToLower() + $p.Substring(2)
    }
    return $p
}

# ─── Step 5: VS Code ──────────────────────────────────────────────────────────

Write-Host "`n  ── Step 5 — VS Code ──────────────────────" -ForegroundColor Cyan

# Check PATH and known install locations — VS Code may be installed without PATH registration
$vsCodeFound = (Get-Command code -ErrorAction SilentlyContinue) -or
               (Test-Path "$env:LOCALAPPDATA\Programs\Microsoft VS Code\bin\code.cmd") -or
               (Test-Path "$env:ProgramFiles\Microsoft VS Code\bin\code.cmd")

if ($vsCodeFound) {
    ok "VS Code already installed"
} else {
    info "Installing VS Code..."
    winget install --id Microsoft.VisualStudioCode -e --source winget --silent --accept-package-agreements --accept-source-agreements
    Refresh-Path
    $vsCodeFound = (Get-Command code -ErrorAction SilentlyContinue) -or
                   (Test-Path "$env:LOCALAPPDATA\Programs\Microsoft VS Code\bin\code.cmd") -or
                   (Test-Path "$env:ProgramFiles\Microsoft VS Code\bin\code.cmd")
    if ($vsCodeFound) {
        ok "VS Code installed"
    } else {
        ok "VS Code installed — open it from the Start menu"
    }
}

# ─── Step 6: Claude Code CLI ──────────────────────────────────────────────────

Write-Host "`n  ── Step 6 — Claude Code CLI ──────────────" -ForegroundColor Cyan

# Also check the npm global folder directly — claude may be installed but npm global not on PATH
$claudeFound = (Get-Command claude -ErrorAction SilentlyContinue) -or
               (Test-Path "$npmGlobal\claude.cmd")

if ($claudeFound) {
    ok "Claude Code CLI already installed"
} else {
    # Verify npm is available before calling it — Node may not have registered on PATH yet
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        fail "npm not found. Node.js may not have registered on PATH yet. Close PowerShell and re-run this script."
    }

    info "Installing Claude Code CLI..."
    npm install -g @anthropic-ai/claude-code

    if ($env:Path -notlike "*$npmGlobal*") {
        $currentUserPath = [Environment]::GetEnvironmentVariable("Path", "User")
        [Environment]::SetEnvironmentVariable("Path", "$currentUserPath;$npmGlobal", "User")
        $env:Path += ";$npmGlobal"
        ok "Added npm global folder to PATH"
    }

    # Add to Git Bash PATH via .bashrc so setup.sh finds claude inside Git Bash
    $bashrc = "$env:USERPROFILE\.bashrc"
    $npmUnix = Convert-ToUnixPath $npmGlobal
    $exportLine = "export PATH=`"`$PATH`:$npmUnix`""
    if (-not (Test-Path $bashrc) -or -not (Select-String -Path $bashrc -Pattern "AppData/Roaming/npm" -Quiet -ErrorAction SilentlyContinue)) {
        Add-Content -Path $bashrc -Value "`n$exportLine" -Encoding UTF8
        ok "Added npm folder to Git Bash PATH"
    }

    Refresh-Path

    if (Get-Command claude -ErrorAction SilentlyContinue) {
        ok "Claude Code CLI installed — $(claude --version)"
    } else {
        fail "Claude Code CLI installation failed. Try running: npm install -g @anthropic-ai/claude-code"
    }
}

# ─── Step 7: Mangrove API Key ─────────────────────────────────────────────────

Write-Host "`n  ── Step 7 — Mangrove API Key ─────────────" -ForegroundColor Cyan

Write-Host "  Get your key at: https://mangrovedeveloper.ai" -ForegroundColor Gray
Write-Host ""
$MANGROVE_API_KEY = Read-Host "  Paste your Mangrove API key and press Enter (or just press Enter to skip)"

if ([string]::IsNullOrWhiteSpace($MANGROVE_API_KEY)) {
    info "No API key entered — skipping server setup for now"
    $SkipSetup = $true
} else {
    ok "API key captured"
    $SkipSetup = $false
}

# ─── Step 8: Clone the repo ───────────────────────────────────────────────────

Write-Host "`n  ── Step 8 — Clone the repo ───────────────" -ForegroundColor Cyan

$repoPath = "$env:USERPROFILE\Desktop\mangrove-agent"

if (Test-Path "$repoPath\scripts\setup.sh") {
    ok "Repo already exists at $repoPath — skipping clone"
} else {
    info "Cloning repo to Desktop..."
    Set-Location "$env:USERPROFILE\Desktop"
    git clone https://github.com/MangroveTechnologies/mangrove-agent.git mangrove-agent
    ok "Repo cloned to $repoPath"
}

Set-Location $repoPath

# ─── Step 9: Run setup via Git Bash ──────────────────────────────────────────

Write-Host "`n  ── Step 9 — Running setup ────────────────" -ForegroundColor Cyan

$gitBash = "C:\Program Files\Git\bin\bash.exe"

if (-not (Test-Path $gitBash)) {
    fail "Git Bash not found at expected location. Git may not have installed correctly. Re-run the script."
}

# Convert Windows paths to Unix-style paths for Git Bash
$repoUnix = Convert-ToUnixPath $repoPath
$npmUnix  = Convert-ToUnixPath $npmGlobal

if ($SkipSetup) {
    info "Skipping server setup — no API key provided"
} else {
    info "Installing Python dependencies, starting the server, and registering with Claude Code..."
    info "This takes about 60 seconds the first time..."
    Write-Host ""

    & $gitBash -c "export PATH=`"`$PATH`:$npmUnix`" && cd '$repoUnix' && ./scripts/setup.sh --api-key '$MANGROVE_API_KEY' --yes"

    if ($LASTEXITCODE -ne 0) {
        fail "setup.sh failed. Check the output above for errors."
    }

    ok "Setup complete"
}

# ─── Done ─────────────────────────────────────────────────────────────────────

Write-Host ""

if ($SkipSetup) {
    Write-Host "  ==========================================" -ForegroundColor Yellow
    Write-Host "    Almost there — one step remaining!" -ForegroundColor Yellow
    Write-Host "  ==========================================" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Everything is installed:" -ForegroundColor White
    Write-Host "    [OK] winget" -ForegroundColor Green
    Write-Host "    [OK] Git" -ForegroundColor Green
    Write-Host "    [OK] Python" -ForegroundColor Green
    Write-Host "    [OK] Node.js" -ForegroundColor Green
    Write-Host "    [OK] VS Code" -ForegroundColor Green
    Write-Host "    [OK] Claude Code CLI" -ForegroundColor Green
    Write-Host "    [OK] mangrove-agent repo cloned" -ForegroundColor Green
    Write-Host ""
    Write-Host "  To finish setup, you need a Mangrove API key:" -ForegroundColor White
    Write-Host ""
    Write-Host "    1. Go to: https://mangrovedeveloper.ai" -ForegroundColor White
    Write-Host "    2. Sign up and create an API key" -ForegroundColor White
    Write-Host "    3. Open Git Bash and run:" -ForegroundColor White
    Write-Host ""
    Write-Host "         cd ~/Desktop/mangrove-agent" -ForegroundColor Cyan
    Write-Host "         ./scripts/setup.sh" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "    The script will prompt you to paste your key." -ForegroundColor White
    Write-Host ""
} else {
    Write-Host "  ==========================================" -ForegroundColor Green
    Write-Host "    You are all set!" -ForegroundColor Green
    Write-Host "  ==========================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Everything installed and verified:" -ForegroundColor White
    Write-Host "    [OK] winget" -ForegroundColor Green
    Write-Host "    [OK] Git" -ForegroundColor Green
    Write-Host "    [OK] Python" -ForegroundColor Green
    Write-Host "    [OK] Node.js" -ForegroundColor Green
    Write-Host "    [OK] VS Code" -ForegroundColor Green
    Write-Host "    [OK] Claude Code CLI" -ForegroundColor Green
    Write-Host "    [OK] mangrove-agent server running" -ForegroundColor Green
    Write-Host ""
    Write-Host "  To start:" -ForegroundColor White
    Write-Host ""
    Write-Host "    1. Open VS Code" -ForegroundColor White
    Write-Host "    2. Open the folder: $repoPath" -ForegroundColor Cyan
    Write-Host "    3. Open the terminal (Ctrl + \``) and switch to Git Bash" -ForegroundColor White
    Write-Host "    4. Run: claude" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  The agent will greet you and run a platform tour." -ForegroundColor White
    Write-Host "  Then say: 'Build me a momentum strategy on ETH'" -ForegroundColor White
    Write-Host ""
}

Read-Host "Press Enter to close"
