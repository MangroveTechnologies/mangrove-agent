#!/usr/bin/env bash
# install-mac.sh — One-shot setup for mangrove-agent on Mac.
# Checks for each dependency, installs if missing, verifies, then moves on.
# Run this from anywhere — it clones the repo to ~/Desktop/mangrove-agent.

set -e

# ─── Version requirements ─────────────────────────────────────────────────────
# Change these if the application's minimum requirements change.
PYTHON_MIN_MAJOR=3
PYTHON_MIN_MINOR=11
NODE_MIN_MAJOR=18

# ─── Output helpers ───────────────────────────────────────────────────────────

GREEN="\033[32m"; YELLOW="\033[33m"; RED="\033[31m"; BOLD="\033[1m"; CLR="\033[0m"

ok()     { printf "${GREEN}  ✓  ${CLR}%s\n" "$1"; }
info()   { printf "${YELLOW}  →  ${CLR}%s\n" "$1"; }
fail()   { printf "${RED}  ✗  ${CLR}%s\n" "$1" >&2; exit 1; }
header() { printf "\n${BOLD}────────────────────────────────────────\n  %s\n────────────────────────────────────────${CLR}\n" "$1"; }

# ─── Welcome ──────────────────────────────────────────────────────────────────

clear
printf "${BOLD}
  Mangrove Agent — Mac Setup
  ──────────────────────────
  This script installs everything you need and gets you running.
  You may be asked for your Mac password once (for Xcode tools).
${CLR}\n"

# ─── Step 1: Xcode Command Line Tools ────────────────────────────────────────

header "Step 1 — Xcode Command Line Tools"

if xcode-select -p &>/dev/null; then
    ok "Xcode Command Line Tools already installed"
else
    info "Installing Xcode Command Line Tools..."
    info "A popup may appear — click Install and wait for it to finish"
    xcode-select --install 2>/dev/null || true
    until xcode-select -p &>/dev/null; do
        sleep 5
    done
    ok "Xcode Command Line Tools installed"
fi

# ─── Step 2: Homebrew ─────────────────────────────────────────────────────────

header "Step 2 — Homebrew"

if command -v brew &>/dev/null; then
    ok "Homebrew already installed"
else
    info "Installing Homebrew (package manager for Mac)..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    ok "Homebrew installed"
fi

# Source Homebrew into the current session so brew commands work immediately.
# Apple Silicon Macs install to /opt/homebrew, Intel Macs to /usr/local.
if [ -f /opt/homebrew/bin/brew ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
elif [ -f /usr/local/bin/brew ]; then
    eval "$(/usr/local/bin/brew shellenv)"
else
    fail "Homebrew installed but could not be found. Please restart your terminal and re-run."
fi

ok "Homebrew is ready and on PATH"

# ─── Step 3: Git ──────────────────────────────────────────────────────────────

header "Step 3 — Git"

if command -v git &>/dev/null; then
    ok "Git already installed — $(git --version)"
else
    info "Installing Git..."
    brew install git
    if command -v git &>/dev/null; then
        ok "Git installed — $(git --version)"
    else
        fail "Git installation failed. Check your internet connection and re-run."
    fi
fi

# ─── Step 4: Python ───────────────────────────────────────────────────────────

header "Step 4 — Python"

# The app requires Python $PYTHON_MIN_MAJOR.$PYTHON_MIN_MINOR or newer.
# Checks python3.12, python3, and python in order — brew installs the binary
# as python3.12 and may not override the system python3 symlink.
python_meets_requirement() {
    for cmd in python3.12 python3 python; do
        if command -v "$cmd" &>/dev/null; then
            if "$cmd" -c "import sys; exit(0 if sys.version_info >= ($PYTHON_MIN_MAJOR,$PYTHON_MIN_MINOR) else 1)" 2>/dev/null; then
                return 0
            fi
        fi
    done
    return 1
}

add_python312_to_path() {
    # Try brew --prefix first, then fall back to known keg locations
    for prefix in \
        "$(brew --prefix python@3.12 2>/dev/null)" \
        "/opt/homebrew/opt/python@3.12" \
        "/usr/local/opt/python@3.12"; do
        if [ -d "$prefix/bin" ]; then
            export PATH="$prefix/bin:$PATH"
            return 0
        fi
    done
}

if python_meets_requirement; then
    ok "Python already meets the requirement (>= $PYTHON_MIN_MAJOR.$PYTHON_MIN_MINOR) — skipping"
else
    if command -v python3 &>/dev/null; then
        info "Python $(python3 --version) is below the required $PYTHON_MIN_MAJOR.$PYTHON_MIN_MINOR — installing 3.12..."
    else
        info "Python not found — installing 3.12..."
    fi
    brew upgrade python@3.12 2>/dev/null || brew install python@3.12
    if [ -f /opt/homebrew/bin/brew ]; then eval "$(/opt/homebrew/bin/brew shellenv)"; fi
    add_python312_to_path
    if python_meets_requirement; then
        ok "Python 3.12 installed and meets the requirement"
    else
        fail "Python 3.12 installed but could not be found. Try opening a new terminal and re-running."
    fi
fi

# ─── Step 5: Node.js ──────────────────────────────────────────────────────────

header "Step 5 — Node.js"

# The app requires Node.js $NODE_MIN_MAJOR or newer.
node_meets_requirement() {
    command -v node &>/dev/null && \
    [ "$(node -e "process.stdout.write(process.version.split('.')[0].slice(1))")" -ge "$NODE_MIN_MAJOR" ]
}

if node_meets_requirement; then
    ok "Node.js $(node --version) already meets the requirement (>= $NODE_MIN_MAJOR) — skipping"
else
    if command -v node &>/dev/null; then
        info "Node.js $(node --version) is below the required v$NODE_MIN_MAJOR — upgrading..."
    else
        info "Node.js not found — installing LTS..."
    fi
    brew upgrade node 2>/dev/null || brew install node
    if node_meets_requirement; then
        ok "Node.js $(node --version) installed and meets the requirement"
    else
        fail "Node.js was installed but still does not meet the requirement (>= $NODE_MIN_MAJOR). Re-run the script."
    fi
fi

# ─── Step 6: VS Code ──────────────────────────────────────────────────────────

header "Step 6 — VS Code"

if command -v code &>/dev/null || [ -d "/Applications/Visual Studio Code.app" ]; then
    ok "VS Code already installed"
else
    info "Installing VS Code..."
    brew install --cask visual-studio-code
    if command -v code &>/dev/null || [ -d "/Applications/Visual Studio Code.app" ]; then
        ok "VS Code installed"
    else
        ok "VS Code installed — open it from Applications to use it"
    fi
fi

# ─── Step 7: Claude Code CLI ──────────────────────────────────────────────────

header "Step 7 — Claude Code CLI"

# Ensure npm global bin is on PATH before checking — it may not be in a fresh session
NPM_BIN="$(npm prefix -g 2>/dev/null)/bin"
if [ -d "$NPM_BIN" ] && [[ ":$PATH:" != *":$NPM_BIN:"* ]]; then
    export PATH="$NPM_BIN:$PATH"
fi

if command -v claude &>/dev/null || [ -x "$NPM_BIN/claude" ]; then
    ok "Claude Code CLI already installed — $(claude --version 2>/dev/null)"
else
    info "Installing Claude Code CLI..."
    npm install -g @anthropic-ai/claude-code
    # Re-check npm bin path after install
    NPM_BIN="$(npm prefix -g 2>/dev/null)/bin"
    export PATH="$NPM_BIN:$PATH"
    if command -v claude &>/dev/null; then
        ok "Claude Code CLI installed — $(claude --version)"
    else
        fail "Claude Code CLI installation failed. Try running: npm install -g @anthropic-ai/claude-code"
    fi
fi

# ─── Step 8: Mangrove API Key ─────────────────────────────────────────────────

header "Step 8 — Mangrove API Key"

printf "  Get your key at: https://mangrovedeveloper.ai\n\n"
printf "  Paste your Mangrove API key and press Enter (or just press Enter to skip): "
read -r MANGROVE_API_KEY
echo

if [ -z "$MANGROVE_API_KEY" ]; then
    info "No API key entered — skipping server setup for now"
    SKIP_SETUP=true
else
    ok "API key captured"
    SKIP_SETUP=false
fi

# ─── Step 9: Clone repo ───────────────────────────────────────────────────────

header "Step 9 — Clone the repo"

REPO_PATH="$HOME/Desktop/mangrove-agent"

if [ -f "./scripts/setup.sh" ]; then
    # Already running from inside the repo
    REPO_PATH="$(pwd)"
    ok "Already inside the mangrove-agent repo — skipping clone"
elif [ -d "$REPO_PATH" ]; then
    ok "Repo already exists at $REPO_PATH — skipping clone"
    cd "$REPO_PATH"
else
    info "Cloning repo to ~/Desktop/mangrove-agent..."
    cd ~/Desktop
    git clone https://github.com/MangroveTechnologies/mangrove-agent.git mangrove-agent
    cd mangrove-agent
    REPO_PATH="$(pwd)"
    ok "Repo cloned to $REPO_PATH"
fi

# ─── Step 10: Run setup ───────────────────────────────────────────────────────

header "Step 10 — Running setup"

if [ "$SKIP_SETUP" = true ]; then
    info "Skipping server setup — no API key provided"
else
    info "Installing Python dependencies, starting the server and registering with Claude Code..."
    info "This takes about 60 seconds the first time..."
    echo
    ./scripts/setup.sh --api-key "$MANGROVE_API_KEY" --yes
    ok "Setup complete"
fi

# ─── Done ─────────────────────────────────────────────────────────────────────

if [ "$SKIP_SETUP" = true ]; then
printf "\n${GREEN}${BOLD}
  ════════════════════════════════════════
    Almost there — one step remaining!
  ════════════════════════════════════════${CLR}

  Everything is installed:
    ✓  Xcode Command Line Tools
    ✓  Homebrew
    ✓  Git
    ✓  Python
    ✓  Node.js
    ✓  VS Code
    ✓  Claude Code CLI
    ✓  mangrove-agent repo cloned

  To finish setup, you need a Mangrove API key:

    1. Go to: https://mangrovedeveloper.ai
    2. Sign up and create an API key
    3. Open Terminal and run:

         cd ~/Desktop/mangrove-agent
         ./scripts/setup.sh

    The script will prompt you to paste your key.

"
else
printf "\n${GREEN}${BOLD}
  ════════════════════════════════════════
    You are all set!
  ════════════════════════════════════════${CLR}

  Everything installed and verified:
    ✓  Xcode Command Line Tools
    ✓  Homebrew
    ✓  Git
    ✓  Python
    ✓  Node.js
    ✓  VS Code
    ✓  Claude Code CLI
    ✓  mangrove-agent server running

  To start:

    1. Open a new terminal
    2. Run:  cd ~/Desktop/mangrove-agent
    3. Run:  claude

  The agent will greet you and run a platform tour.
  Then say: \"Build me a momentum strategy on ETH\"

"
fi
