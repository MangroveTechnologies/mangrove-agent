# Setup guide

This guide takes you from a bare machine to a running local trading agent, with Mac and Windows instructions and a verification step after each command.

Most people should start with the [user guide](docs/user-guide.md), which covers installing through Claude Code, Codex, Grok Build or Perplexity Computer, naming the agent, returning to it and switching clients. The [AI client workflow](docs/clients.md#install-from-an-agent) is the technical reference for an installing agent. Use this page when you want to install the prerequisites and run setup yourself.

The walkthrough below uses Claude Code in the terminal as its example client. Claude, Node.js and a Claude subscription are not required for the Python agent when you use another client; skip Steps 3 and 5 in that case. A MangroveAI API key is optional: `setup.sh` also offers x402 wallet payments, and a fresh `--yes` installation defers payment-wallet setup.

**What you will end up with:**

- A local agent running at `http://127.0.0.1:9080`.
- Your chosen client connected to the agent's tool catalog. Remote tools depend on the upstream services being available.
- Local tools ready for onboarding. Paid data and backtests need an API key or completed x402 payment setup.

**Jump to:** [Optional machine installer](#optional-machine-installer) | [Coming back](#already-set-up-and-coming-back) | [Manual steps](#before-you-start-what-you-need) | [Troubleshooting](#troubleshooting)

---

## Optional machine installer

One command installs the whole toolchain and then runs setup for you. On a Mac it installs Xcode Command Line Tools, Homebrew, Git, Python, Node.js, VS Code and the Claude Code CLI. On Windows it uses winget for Git, Python, Node.js and VS Code, then installs the Claude Code CLI.

The installer downloads its script from the [Mangrove repository](https://github.com/MangroveTechnologies/mangrove-agent/blob/main/scripts/install-mac.sh), so you can read every line before running it. It installs standard developer tools and the Mangrove agent, clones the repository to `~/Desktop/mangrove-agent` (on Windows, `Desktop\mangrove-agent` in your user profile), and does not collect personal data.

These installers ask you to paste a MangroveAI API key (the input is visible) and run `./scripts/setup.sh --api-key-stdin --yes`. If you press Enter without a key, they install the tools and stop before server setup. They are not the x402 entry point. For x402, or to type your key at a hidden prompt, install the prerequisites with the manual steps below and run `./scripts/setup.sh` yourself (Step 8).

**Mac:** open Terminal (`Cmd + Space`, type "terminal", press Enter) and run:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/MangroveTechnologies/mangrove-agent/main/scripts/install-mac.sh)
```

**Windows:** open PowerShell as Administrator (search "PowerShell" in the Start menu, right-click, Run as administrator) and run:

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/MangroveTechnologies/mangrove-agent/main/scripts/install-windows.ps1 | iex"
```

## Already set up and coming back

First check whether the existing server is still running. Switching clients or returning the next day does not need a restart. If the server stopped, for example after a reboot, start it from the existing checkout. Open your terminal (Mac) or Git Bash (Windows) and run:

```bash
cd ~/Desktop/mangrove-agent
./scripts/setup.sh --yes
```

If you cloned somewhere other than the Desktop, use that path instead. `--yes` keeps your saved access mode and settings. Add `--no-mcp` if you do not use Claude Code. Then follow [returning or switching clients](docs/user-guide.md#come-back-or-switch-clients). The saved agent name and records come from the same installation, and Claude users can start `./scripts/chat.sh`.

---

## Before you start: what you need

**Claude Code** is an AI assistant that runs in your terminal. You talk to it in plain English ("build me a strategy on ETH") and it calls the **Mangrove agent** running on your machine, which fetches market data, runs backtests and manages paper trading. VS Code is the window you work in. Git, Python and Node.js are background requirements.

| Tool | Why | Required? |
|---|---|---|
| **Git** | Downloads the repository. On Windows it also installs Git Bash, the terminal used in this guide. | Yes |
| **Python 3.11+** | The agent is a Python process. Older versions do not work. | Yes |
| **Node.js 18+** | Needed for the npm-based Claude Code install shown below. | Only for that install method |
| **Claude Code CLI** | The chat interface used in this walkthrough. Other supported clients work too. | Only for the Claude path |
| **AI provider access** | Sign in with your chosen client's account or credential flow. | For your chosen client |
| **VS Code** | A recommended editor with a built-in terminal. | Recommended |
| **MangroveAI API key** | An alternative to x402 wallet payments. | Optional |

Do the steps in order; each depends on the previous one.

---

## Step 1: Install Git

### Mac

Open Terminal and check whether Git is installed:

```bash
git --version
```

If you see `git version 2.x.x`, skip to Step 2. Otherwise pick one option.

**Option A, Terminal (recommended).** Install Homebrew if you do not have it, then Git:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install git
```

**Option B, installer.** Download the `.pkg` from https://git-scm.com/download/mac and run it.

Verify with `git --version`.

### Windows

Open PowerShell as Administrator and pick one option.

**Option A, Terminal (recommended):**

```powershell
winget install --id Git.Git -e --source winget
```

**Option B, installer.** Download the `.exe` from https://git-scm.com/download/win and run it. On the "Adjusting your PATH environment" screen, choose "Git from the command line and also from 3rd-party software". This is the step most people miss.

Close PowerShell afterwards and use **Git Bash** (from the Start menu) for every remaining step. PowerShell and CMD cannot run this repository's `.sh` scripts. Verify in Git Bash:

```bash
git --version
```

After any install on Windows, close and reopen your terminal before verifying, because already-open terminals do not pick up PATH changes.

---

## Step 2: Install Python 3.11 or newer

### Mac

**Option A, Terminal (recommended):**

```bash
brew install python@3.12
```

**Option B, installer.** Download the `.pkg` from https://www.python.org/downloads/macos/ and run it. At the end, double-click `Install Certificates.command` in the Finder window that appears.

Open a new Terminal window, then verify:

```bash
python3 --version
```

Expected: `Python 3.12.x` (any 3.11 or newer is fine). Homebrew installs the versioned binary (`python3.12`), and `setup.sh` looks for `python3.13` down to `python3.11` before falling back to `python3` and `python`, so it finds a suitable interpreter even when the default `python3` is older.

### Windows

**Option A, Terminal (in Git Bash):**

```bash
winget install --id Python.Python.3.12 -e --source winget
```

**Option B, installer.** Download the `.exe` from https://www.python.org/downloads/windows/. Before clicking Install Now, check "Add python.exe to PATH"; without it Python does not work in your terminal.

Close and reopen Git Bash, then verify:

```bash
python --version
```

Expected: `Python 3.12.x` (any 3.11 or newer). If `python` does not work, try `python3`.

---

## Step 3: Install Node.js (Claude Code path only)

Claude Code's npm install needs Node.js 18 or newer. Check whether you have it:

```bash
node --version
npm --version
```

If both print versions and Node is 18 or higher, skip to Step 4. Otherwise:

- **Mac:** `brew install node`, or download the LTS `.pkg` from https://nodejs.org.
- **Windows (Git Bash):** `winget install --id OpenJS.NodeJS.LTS -e --source winget`, or download the LTS `.msi` from https://nodejs.org and accept the defaults.

Reopen your terminal and verify with `node --version` and `npm --version`.

---

## Step 4: Install VS Code (optional)

VS Code gives you an editor and an integrated terminal in one window, which avoids a lot of Mac and Windows terminal confusion.

- **Mac:** `brew install --cask visual-studio-code`, or download it from https://code.visualstudio.com/download and drag the app to Applications. To get the `code` command, open VS Code, press `Cmd + Shift + P` and run `Shell Command: Install 'code' command in PATH`.
- **Windows (Git Bash):** `winget install --id Microsoft.VisualStudioCode -e --source winget`, or use the installer from the same page. Reopen Git Bash afterwards. Then make VS Code's terminal use Git Bash: open the terminal with `` Ctrl + ` ``, click the dropdown next to the `+` icon and select Git Bash.

Verify with `code --version`. From here on, use VS Code's integrated terminal (`` Ctrl + ` `` on Windows, `` Cmd + ` `` on Mac) if you installed it.

---

## Step 5: Install the Claude Code CLI (Claude path only)

```bash
npm install -g @anthropic-ai/claude-code
```

Reopen your terminal and verify:

```bash
claude --version
```

> **Windows: "claude: command not found" after install.** npm installs global packages to a directory that may not be on your PATH. Find it with `npm config get prefix`; the output looks like `C:\Users\YourName\AppData\Roaming\npm`. Open Windows Settings, search "Environment Variables", select `Path` under "User variables", click Edit, click New, paste that path, and click OK on all dialogs. Reopen Git Bash and run `claude --version` again.

---

## Step 6: Choose access, API key or x402

For x402, skip this step and select wallet payments in Step 8. For API-key access:

1. Go to https://mangrovedeveloper.ai and sign up.
2. After signing in, open **Settings > API Keys** and create a key. It looks like `dev_a1b2c3...`.
3. Copy it somewhere safe. You paste it into a hidden prompt in Step 8. Treat it like a password.

---

## Step 7: Clone the repository

1. Open your terminal and go to where you want the project folder. Most people use the Desktop. Do this before cloning, or the repository lands in your home directory.

   ```bash
   cd ~/Desktop
   ```

2. Clone and enter the repository:

   ```bash
   git clone https://github.com/MangroveTechnologies/mangrove-agent.git mangrove-agent
   cd mangrove-agent
   ```

3. Check you are in the right place with `ls`. You should see `README.md`, `SETUP.md`, `scripts/`, `server/`, `tutorials/`, `docs/` and `docker-compose.yml`.
4. Optionally open the folder in VS Code with `code .`.

> **Windows Git Bash: commands fail with strange characters after pasting.** Copying from some browsers or PDF readers can inject invisible Unicode characters. If a pasted command fails in a confusing way, delete it and type it by hand.

---

## Step 8: Run setup

In the `mangrove-agent` folder:

```bash
./scripts/setup.sh
```

On an interactive run, setup asks how you want to access MangroveAI: with an API key, or by paying with a wallet (x402, no signup). The menu highlights the API-key option; move the selection to switch modes. API-key entry is hidden, and if a key is already saved it asks whether to keep it or enter a new one. `--yes` skips the menu and keeps the saved mode (a fresh install selects x402). For x402, the final menu prints create, import and saved-wallet instructions for you to run yourself. Setup never creates a wallet or spends money.

Setup then runs on its own:

1. Creates or updates `server/src/config/local-config.json` and a unique local credential for your AI client.
2. Creates `agent-data/` for local state (database, logs, pid file, master key).
3. Creates a virtual environment at `.venv/` and installs the dependencies.
4. Starts the server in the background at `http://127.0.0.1:9080`.
5. Verifies authenticated local access and that unauthenticated wallet requests are refused.
6. Registers the MCP server with Claude Code, if the `claude` CLI is installed and you did not pass `--no-mcp`.
7. Runs the verification pass (`scripts/verify_quickstart.sh`).

When it succeeds you see, in addition to the wallet instructions or API-key message:

```
Claude MCP registration saved. Start ./scripts/chat.sh when ready.
Done. Local agent authenticated at http://127.0.0.1:9080. Payment readiness is separate.
```

If Claude registration was skipped, setup prints `Local agent ready. Claude MCP registration was skipped.` followed by three commands that let any command-capable AI client load the shared instructions and use the agent: `agent_context.py --stdout`, `local_mcp_bridge.py --call discover_tools` and `local_mcp_bridge.py --call status`. Run them with the interpreter path setup prints.

---

## Step 9: Verify it works

### Check the server is up

```bash
curl -s http://127.0.0.1:9080/health
```

Expected:

```json
{"scheduler_running":true,"status":"healthy","timestamp":"2026-..."}
```

### Check Claude Code can see the agent

```bash
claude mcp list
```

Expected a line like:

```
mangrove-agent: http://127.0.0.1:9080/mcp/ (HTTP) - Connected
```

Registration uses a dynamic header helper, so Claude Code may ask you to approve it the first time.

### Start your client

Follow the [user guide](docs/user-guide.md#come-back-or-switch-clients). For the restricted Claude session:

```bash
./scripts/chat.sh
```

The client loads the shared instructions, discovers tools and calls free `status`. If `agent_name` is unset, it asks what you want to name your agent and saves only your choice. Returning sessions use the saved name. Startup does not need a paid market-data tour, a wallet, a backtest or a scheduled strategy.

**Success means a real `status` call passed with all shared policy loaded.** Saved connection settings or an automatic greeting alone are not proof.

---

## You are all set

If free `status` succeeded, the local connection works. Remote capabilities depend on the tools the server actually advertises, and an upstream outage is a separate matter from a local failure. Here is what you can do next:

- **Build your first strategy.** Say "Build me a momentum strategy on ETH". The agent builds and backtests it on MangroveAI.
- **Paper trade it.** Say "Promote it to paper" to start a paper schedule with simulated trades. API-key users need no wallet for simulated trades. In x402 mode, paid data and backtests still need a funded payment wallet.
- **Explore the tools.** Ask "What tools do you have?"
- **Go live when ready.** Follow `tutorials/trading-app/06-wallet-setup.md` for wallet setup.

The full tutorial is in `tutorials/trading-app/`; start with `00-index.md`.

---

## Troubleshooting

### "Python 3.11+ required"

Python is missing or older than 3.11. Redo Step 2, then rerun setup.

### "permission denied: ./scripts/setup.sh"

```bash
chmod +x ./scripts/setup.sh
./scripts/setup.sh
```

### Choose or change the access mode

Run `./scripts/setup.sh --auth api-key` for API-key access or `./scripts/setup.sh --auth x402` for wallet payments. Setup replaces the example placeholder key and keeps local authentication enabled. An invalid API key never falls back to wallet payment; the modes are exclusive. See [setup with an API key or x402](docs/setup-x402.md).

### "Local authenticated readiness failed" or "Agent exited during startup"

Setup waits up to 60 seconds for authenticated local access, and verification is read-only. These messages mean the uvicorn process did not come up, so the cause is in the server log, not the script output:

```bash
tail -50 agent-data/bare.log
```

Look for a Python traceback. The usual causes are a port already in use, a missing dependency or a bad value in `local-config.json`. Fix what the traceback names, then rerun `./scripts/setup.sh`.

The agent does not need to reach the external x402 facilitator to start. If your network blocks it you may see a harmless `x402.hello_mangrove.facilitator_unavailable` warning in `bare.log` (the paid demo tool is disabled), but `/health` and all other features still come up.

### Port 9080 is already in use

`setup.sh` refuses to start when 9080 is taken. Either use a different port, or free 9080.

**Different port** (for example when VS Code's helper holds 9080):

```bash
BARE_PORT=9085 ./scripts/setup.sh
```

`BARE_PORT` applies to the server, the readiness check, the MCP registration and the verification, and later runs reuse it.

**Free 9080.** First find out what holds it.

- **Mac:** `lsof -nP -iTCP:9080 -sTCP:LISTEN` shows the process. If it is safe to stop, run `kill <pid>`.
- **Windows (Git Bash):** `netstat -ano | grep 9080`. The last column is the process ID. Stop it with `taskkill /PID <pid> /F`.

Then rerun `./scripts/setup.sh`.

### Claude's saved connection fails

From the existing checkout, verify local authentication and refresh the saved Claude registration without copying a credential into any command:

```bash
./scripts/setup-mcp.sh
```

Then reconnect Claude from `/mcp`. This does not start a stopped server. Other clients use [their connection instructions](docs/clients.md); do not add a second server or delete data to repair a client connection.

### The first greeting did not check status or ask for a name

Ask the client to load the shared instructions and read free `status`. An unset name should prompt for your choice, and a saved name should be reused. A greeting alone is not proof that tools work; if `status` fails, inspect the connection.

### Setup fails: "agent-data/ exists but is not writable"

You ran the agent under Docker before and are now using bare-metal. Docker creates `agent-data/` owned by `root`, which a non-root process cannot write. Reclaim it with a throwaway container, then rerun setup:

```bash
docker run --rm -v "$PWD":/r alpine chown -R $(id -u):$(id -g) /r/agent-data
./scripts/setup.sh
```

Or, with sudo: `sudo chown -R $(id -u):$(id -g) agent-data`.

### "Another setup is active"

Setup serializes runs with the `agent-data/.setup.lock` directory. After a hard interruption, check the recorded PID in `agent-data/.setup.lock/pid` and remove the lock directory only after confirming no setup is running.

### Keep your state while troubleshooting

Do not delete `agent-data/` to fix setup or a chat connection. It holds the saved agent name, trading history and wallet material, including the master key. Use the server log and the exact failing check to diagnose the problem, and preserve the config, database and key before any recovery you requested explicitly. A missing upstream tool is different from a stopped local server.
