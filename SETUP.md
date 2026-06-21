# Setup Guide

Get from zero to a running trading agent. Every step has Mac and Windows instructions. Every command has a verification step so you know it worked before moving on.

**What you will end up with:**
- A local trading agent running at `http://localhost:9080`
- Claude Code connected to it with 41 trading tools
- A working paper strategy you can watch evaluate in real time

---

**Jump to:**
- [Quick Install](#quick-install-recommended) — fresh machine, installs everything in one command
- [Coming back](#already-set-up-and-coming-back) — already installed, just need to re-run setup
- [Manual steps](#before-you-start--what-you-need) — step-by-step if the script fails or you prefer doing it yourself

---

## Quick Install (recommended)

One command installs everything automatically — Git, Python, Node.js, VS Code, Claude Code CLI — then runs setup for you.

> **Before you run:** you will need a Mangrove API key. Get one at [mangrovedeveloper.ai](https://mangrovedeveloper.ai) and have it ready — the script will pause and ask for it.

> **What does this command actually do?** It downloads the install script directly from the [official Mangrove repo on GitHub](https://github.com/MangroveTechnologies/mangrove-agent/blob/main/scripts/install-mac.sh) and runs it. Nothing is hidden — you can open that link and read every line before running. The script only installs standard developer tools (Git, Python, Node.js, VS Code) and the Mangrove agent. It does not collect any personal data.

**Mac** — open Terminal (`Cmd + Space`, type "terminal", press Enter) and run:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/MangroveTechnologies/mangrove-agent/main/scripts/install-mac.sh)
```

**Windows** — open **PowerShell as Administrator** (search "PowerShell" in Start menu → right-click → Run as administrator) and run:

```powershell
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/MangroveTechnologies/mangrove-agent/main/scripts/install-windows.ps1 | iex"
```

## Already set up and coming back?

Everything is already installed — you just need to restart the agent server. This happens after a reboot, waking your laptop, or coming back the next day.

Open your terminal (Mac) or Git Bash (Windows) and run:

```bash
cd ~/Desktop/mangrove-agent
./scripts/setup.sh --yes
```

> If you cloned the repo somewhere other than the Desktop, replace `~/Desktop/mangrove-agent` with the correct path.

Once it finishes, run `claude` from the same folder to start a session.

---

## Before you start — what you need

Here is how all the pieces fit together: **Claude Code** is an AI assistant that runs in your terminal. You talk to it in plain English — "build me a strategy on ETH" — and it calls the **Mangrove agent** running on your machine to do the actual work (fetching market data, running backtests, placing paper trades). VS Code is just the window you work in. Git, Python, and Node.js are background requirements that the agent and Claude Code need to run.

| Tool | Why | Required? |
|---|---|---|
| **Git** | Downloads the repo. On Windows it also installs Git Bash, the terminal you will use. | Yes |
| **Python 3.11+** | The trading agent is built in Python. Versions older than 3.11 will not work. | Yes |
| **Node.js 18+** | Required by the Claude Code CLI to run. | Yes |
| **Claude Code CLI** | The AI chat interface you use to talk to the agent. | Yes |
| **Claude Pro subscription** | Claude Code requires a paid Claude plan (Pro, Max, Team, or Enterprise). | Yes |
| **VS Code** | Recommended editor with a built-in terminal. Not strictly required but makes everything easier. | Recommended |
| **Mangrove API key** | Needed for market data, signals, and backtesting. Free at [mangrovedeveloper.ai](https://mangrovedeveloper.ai). | Yes |

Do these in order — each one depends on the previous.

---

## Step 1 — Install Git

Git downloads the repo. On Windows it also installs Git Bash, which is the terminal you will use for every command in this guide.

### Mac

Open Terminal (`Cmd + Space`, type "terminal", press Enter).

Check if already installed:

```bash
git --version
```

If you see `git version 2.x.x` → already installed, skip to Step 2.

If not, you have two options — pick one:

**Option A — Terminal (recommended):**

First install Homebrew if you don't have it:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Then install Git:

```bash
brew install git
```

**Option B — Download installer:**

Go to https://git-scm.com/download/mac, download the `.pkg`, and run it.

Verify either way:

```bash
git --version
```

### Windows

Open **PowerShell as Administrator** (search "PowerShell" in Start menu → right-click → Run as administrator).

You have two options — pick one:

**Option A — Terminal (recommended):**

```powershell
winget install --id Git.Git -e --source winget
```

**Option B — Download installer:**

Go to https://git-scm.com/download/win, download the `.exe`, and run it. When you reach the **"Adjusting your PATH environment"** screen, select **"Git from the command line and also from 3rd-party software"** — this is the step most people miss.

> **Close PowerShell after this.** Open **Git Bash** from the Start menu for all remaining steps.
> PowerShell and CMD will not run the `.sh` scripts this repo uses.

Verify in Git Bash:

```bash
git --version
```

Expected: `git version 2.x.x`

> **After any install on Windows:** always close and reopen your terminal before verifying.
> PATH changes are not picked up by terminals that were already open.

---

## Step 2 — Install Python 3.11 or newer

### Mac

You have two options — pick one:

**Option A — Terminal (recommended):**

```bash
brew install python@3.12
```

**Option B — Download installer:**

Go to https://www.python.org/downloads/macos/, download the latest `.pkg`, and run it. At the end, double-click `Install Certificates.command` in the Finder window that appears.

Open a new Terminal window after installing, then verify:

```bash
python3 --version
```

Expected: `Python 3.12.x` (or any 3.11+)

### Windows

You have two options — pick one:

**Option A — Terminal (recommended):**

In Git Bash:

```bash
winget install --id Python.Python.3.12 -e --source winget
```

**Option B — Download installer:**

Go to https://www.python.org/downloads/windows/, download the `.exe`. **Before clicking Install Now, check the box at the bottom that says "Add python.exe to PATH"** — without this Python won't work in your terminal.

Close and reopen Git Bash after installing, then verify:

```bash
python --version
```

Expected: `Python 3.12.x` (or any 3.11+). If `python` doesn't work, try `python3`.

> **If this fails on Windows:** close and reopen Git Bash — PATH changes require a fresh terminal.

---

## Step 3 — Install Node.js

Node.js is required by the Claude Code CLI. Check if you already have it:

```bash
node --version
npm --version
```

If both print version numbers and Node is 18 or higher → skip to Step 4.

### Mac

You have two options — pick one:

**Option A — Terminal (recommended):**

```bash
brew install node
```

**Option B — Download installer:**

Go to https://nodejs.org, download the LTS version `.pkg`, and run it.

### Windows

You have two options — pick one:

**Option A — Terminal (recommended):**

In Git Bash:

```bash
winget install --id OpenJS.NodeJS.LTS -e --source winget
```

**Option B — Download installer:**

Go to https://nodejs.org, download the LTS version `.msi`, and run it. Defaults are fine.

After installing, close and reopen your terminal, then verify:

```bash
node --version
npm --version
```

---

## Step 4 — Install VS Code

VS Code gives you a good editor and an integrated terminal in one window. It is optional but strongly recommended — the integrated terminal avoids a lot of the Mac/Windows terminal confusion.

### Mac

You have two options — pick one:

**Option A — Terminal (recommended):**

```bash
brew install --cask visual-studio-code
```

**Option B — Download installer:**

Go to https://code.visualstudio.com/download, download the Mac `.zip`, open it, and drag `Visual Studio Code.app` to your Applications folder.

After installing, add the `code` command to your terminal:

- Open VS Code.
- Press `Cmd + Shift + P` to open the Command Palette.
- Type `Shell Command: Install 'code' command in PATH` and press Enter.

### Windows

You have two options — pick one:

**Option A — Terminal (recommended):**

In Git Bash:

```bash
winget install --id Microsoft.VisualStudioCode -e --source winget
```

**Option B — Download installer:**

Go to https://code.visualstudio.com/download, download the Windows `.exe`, and run it. Defaults are fine.

After installing, close and reopen Git Bash.

**Switch VS Code's built-in terminal to Git Bash:**

- Open VS Code.
- Open the terminal with `` Ctrl + ` ``
- Click the dropdown arrow next to the `+` icon in the terminal panel.
- Select **Git Bash**.

### Verify (Mac and Windows)

```bash
code --version
```

> From this point on, use VS Code's integrated terminal for all commands.
> Open it with `` Ctrl + ` `` (Windows) or `` Cmd + ` `` (Mac).

---

## Step 5 — Install Claude Code CLI

Claude Code is the terminal-based AI assistant you will use to drive the agent.

### Mac and Windows

```bash
npm install -g @anthropic-ai/claude-code
```

Close and reopen your terminal. Then verify:

```bash
claude --version
```

Expected: `claude X.Y.Z`

> **Windows: "claude: command not found" after install**
>
> npm installs global packages to a directory that may not be on your PATH.
> Find out where:
> ```bash
> npm config get prefix
> ```
> The output is a path like `C:\Users\YourName\AppData\Roaming\npm`.
> Add that path to your Windows PATH:
> 1. Open Windows Settings → search "Environment Variables".
> 2. Under "User variables", select `Path` and click Edit.
> 3. Click New and paste the path from above.
> 4. Click OK on all dialogs.
> 5. Close and reopen Git Bash.
> 6. Run `claude --version` again.

---

## Step 6 — Get a Mangrove API key

1. Go to https://mangrovedeveloper.ai
2. Click **Sign up** and create an account.
3. After signing in, navigate to the **API Keys** section of your dashboard.
4. Create a new API key. It will look like `dev_a1b2c3...`
5. Copy it somewhere safe — you will paste it during Step 8. Treat it like a password.

---

## Step 7 — Clone the repo

1. Open VS Code's terminal (`` Ctrl + ` `` on Windows, `` Cmd + ` `` on Mac).
2. Navigate to where you want the project folder. Most people use the Desktop:

   ```bash
   cd ~/Desktop
   ```

   > **Do this before cloning.** If you skip this step, the repo clones into your
   > home directory (`~`) and you will have to move it manually.

3. Clone the repo:

   ```bash
   git clone https://github.com/MangroveTechnologies/mangrove-agent.git mangrove-agent
   ```

4. Move into the folder:

   ```bash
   cd mangrove-agent
   ```

5. Verify you are in the right place:

   ```bash
   ls
   ```

   You should see: `README.md`, `SETUP.md`, `scripts/`, `server/`, `tutorials/`, `docs/`, `docker-compose.yml`

6. Open the folder in VS Code:

   ```bash
   code .
   ```

> **Windows Git Bash: commands fail with strange characters after pasting**
>
> Copy-pasting from some browsers or PDF readers can inject invisible unicode
> characters that break commands silently. If a pasted command errors in a
> confusing way, delete it and type it manually character by character.

---

## Step 8 — Run setup

In VS Code's terminal (inside the `mangrove-agent` folder):

```bash
./scripts/setup.sh
```

**The script will pause and ask for your Mangrove API key** (from Step 6) — paste it and press Enter. It will also ask for a server URL — press Enter to accept the default.

After that it runs automatically and does the following:

1. Copies the example config to `server/src/config/local-config.json`
2. Creates `agent-data/` for local state (database, logs, pid file)
3. Creates a Python virtual environment at `.venv/` and installs dependencies
4. Starts the server in the background at `http://localhost:9080`
5. Waits for the server to be healthy
6. Registers the MCP server with Claude Code
7. Runs a quick verify pass

When it finishes successfully you will see:

```
==> Done. Setup complete.
    Agent: http://localhost:9080
    PID:   12345  (agent-data/bare.pid)
    Logs:  agent-data/bare.log

    Restart Claude Code in this directory to load the 41 mangrove-agent
    tools. Then try: "Status check. List my wallets and strategies."
```

---

## Step 9 — Verify it works

### Check the server is up

```bash
curl -s http://localhost:9080/health
```

Expected:

```json
{"status":"healthy","timestamp":"2026-..."}
```

### Check Claude Code can see the tools

```bash
claude mcp list
```

Expected:

```
mangrove-agent: http://localhost:9080/mcp/ (HTTP) - ✓ Connected
```

### Start a Claude Code session

```bash
claude
```

Wait 10–20 seconds. The agent should run a **platform tour automatically** — five live tool calls (status, list_tools, market data, KB search, reference strategies) followed by an offer to build a strategy. This is your signal that everything is wired correctly.

**If you see the tour → setup is complete. You are ready.**

---

## You are all set

If you made it this far and the tour fired, everything is working — the server is running, Claude Code is connected, and all 41 trading tools are loaded.

Here is what you can do right now:

- **Build your first strategy** — type something like "Build me a momentum strategy on ETH" and the agent will generate candidates, backtest them, and propose the best one.
- **Paper trade it** — once a strategy is created, say "Promote it to paper" and it will start running on a schedule with simulated trades. No wallet or real money needed.
- **Explore the tools** — ask "What tools do you have?" and the agent will walk you through everything available.
- **Go live when ready** — when you want to trade with real funds, follow `tutorials/trading-app/06-wallet-setup.md` for the wallet setup flow.

The full step-by-step tutorial lives in `tutorials/trading-app/` — 8 chapters from orientation through live trading. Start with `00-index.md` for an overview.

---

## Troubleshooting

### "python3 not on PATH" during setup

Python is not installed or is an older version. Redo Step 2.

### "permission denied: ./scripts/setup.sh"

The script needs execute permission:

```bash
chmod +x ./scripts/setup.sh
./scripts/setup.sh
```

### "MANGROVE_API_KEY is still the placeholder"

The prompt during setup did not capture your key. Fix it manually:

1. Open `server/src/config/local-config.json` in VS Code.
2. Find the `MANGROVE_API_KEY` field.
3. Replace the placeholder with your key from Step 6.
4. Save the file.
5. Rerun: `./scripts/setup.sh --yes --no-mcp --no-verify`

### "Health did not respond within 30s"

The setup/verify scripts wait up to 30s for the agent to answer on
`http://localhost:9080/health`. This message means the uvicorn process
never bound the port — so the cause is in the **daemon log**, not the
script output. Read it first:

```bash
tail -50 agent-data/bare.log
```

Look for a Python traceback (most common: port already in use, a missing
dependency, or a bad value in `local-config.json`). Fix what the
traceback names, then rerun `./scripts/setup.sh`.

> The agent does **not** depend on reaching the external x402 payment
> facilitator to start. If your network blocks it you'll see a harmless
> `x402.hello_mangrove.facilitator_unavailable` warning in `bare.log`
> (the paid demo tool is disabled), but `/health` and all free + API-key
> features still come up. That warning is not a startup failure.

### Port 9080 is already in use

Another program on your machine is already using the port the agent needs. These commands find it and stop it:

**Mac** — finds whatever is using port 9080 and force-stops it:

```bash
lsof -ti:9080 | xargs kill -9
```

**Windows** — lists all programs using port 9080 so you can identify the right one:

```bash
netstat -ano | grep 9080
```

Look at the output and note the number in the last column (that is the process ID). Then stop it (replace `<pid>` with that number):

```bash
taskkill /PID <pid> /F
```

Then rerun `./scripts/setup.sh`

### `claude mcp list` shows "Failed to connect"

The agent's address is registered incorrectly with Claude Code. These commands remove the bad registration and add the correct one:

```bash
claude mcp remove mangrove-agent
claude mcp add -s local -t http mangrove-agent http://localhost:9080/mcp/ \
  --header "X-API-Key: dev-key-1"
```

Then close and reopen Claude Code.

### The tour didn't fire when I ran `claude`

Three possible causes:

1. **MCP not connected** — run `claude mcp list`. If it shows "Failed to connect", fix the registration (see above).
2. **Already onboarded** — the tour only runs once. If you have run `claude` here before, it is suppressed. To reset it, run `rm .claude/.onboarded` then restart Claude Code.
3. **Wrong directory** — Claude Code only connects to the agent when you are inside the `mangrove-agent` folder. Make sure you ran `cd ~/Desktop/mangrove-agent` first.

### Nuclear reset — wipe everything and start over

Use this only if nothing else works. It deletes all your strategies and trades and returns the agent to a clean state. Run these commands one at a time:

```bash
# 1. Stop the running agent
kill $(cat agent-data/bare.pid) 2>/dev/null

# 2. Delete the local data folder (strategies, trades, logs — this cannot be undone)
rm -rf agent-data/

# 3. Remove the Claude Code registration
claude mcp remove mangrove-agent 2>/dev/null

# 4. Run setup again from scratch
./scripts/setup.sh
```

