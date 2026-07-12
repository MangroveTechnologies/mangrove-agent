# Workshop pre-requisites

Read this **before** the workshop. If all five items below work on
your machine, you'll spend the session actually building things
instead of fighting install scripts.

Total setup time if you start from scratch: 20–30 minutes.

## The five things you need

| Tool | Install | Test it works | Why |
|---|---|---|---|
| **VSCode** | https://code.visualstudio.com/download | Open it. See file tree, menu bar, integrated terminal. | Universal editor + integrated terminal. Identical on macOS / Linux / Windows. |
| **Python 3.11 or newer** | https://www.python.org/downloads/ (download the installer for your OS) | `python3 --version` → `Python 3.11.x` or higher | The agent is a Python FastAPI process. 3.10 and earlier will not work. |
| **Git** | macOS/Linux: pre-installed. Windows: https://git-scm.com/download/win (installs Git Bash too) | `git --version` → any version | Cloning the repo + running `.sh` scripts. |
| **Claude Code CLI** | `npm install -g @anthropic-ai/claude-code` (needs Node.js — install from https://nodejs.org if you don't have it) | `claude --version` → any version | The chat UX. The whole workshop runs through this. |
| **Mangrove API key** | Sign up free at https://mangrovedeveloper.ai | You'll have a string like `dev_a1b2c3...` | How the agent authenticates with Mangrove's hosted strategy / signals / KB APIs. |

If any command prints "command not found" or an unrecognized-tool
error, the tool isn't installed or isn't on your PATH. Fix that
first — nothing downstream will work without all five.

## Windows-specific: use Git Bash as VSCode's default terminal

The repo's scripts are shell scripts (`./scripts/setup.sh`, etc.).
On Windows, **PowerShell and CMD won't run them.** You need Git Bash.

1. Install Git for Windows (it bundles Git Bash).
2. Open VSCode → Command Palette (`Ctrl+Shift+P`) → "Terminal: Select
   Default Profile" → pick **Git Bash**.
3. Open a new terminal (`Ctrl+\``) and confirm the prompt shows a `$`
   rather than `>`.

macOS and Linux users skip this — your default shell (`zsh` or
`bash`) is already correct.

## 30 minutes before the workshop — the smoke test

Run this in a terminal. Every command should succeed.

```bash
python3 --version      # should be 3.11 or higher
git --version          # should be anything
claude --version       # should be anything
node --version         # should be 18 or higher (Claude Code needs it)
npm --version          # should be anything
curl --version         # should be anything
```

If any of them fail or return unexpectedly, fix that tool before the
session.

Then test you can clone:

```bash
cd ~/Desktop                  # or wherever you keep projects
git clone https://github.com/MangroveTechnologies/mangrove-agent.git mangrove-agent-dry-run
cd mangrove-agent-dry-run
ls
```

You should see `README.md`, `scripts/`, `server/`, `tutorials/`, and
others. If so, you're set — delete the test clone:

```bash
cd ..
rm -rf mangrove-agent-dry-run
```

You'll clone it fresh during the workshop.

## Common installation issues

### macOS: `python3` points to an old Python

macOS ships with Python, but it may be 3.9 or earlier. Install
3.11+ from python.org (or `brew install python@3.12`), then confirm
`python3 --version` picks up the new one. If it doesn't, your PATH
needs the Python install directory earlier. Restart your terminal
after the install.

### Windows: `claude --version` says "command not found"

npm installs global packages to a directory that may not be on your
PATH. Run:

```bash
npm config get prefix
```

Add the returned path + `\` to your PATH environment variable (in
Windows settings → Environment Variables), restart your terminal,
try `claude --version` again.

### Any OS: `pip install` fails during setup with compiler errors

Some Python packages (notably `cryptography`, which the wallet code
uses) need build tools. Install them:

- **macOS:** `xcode-select --install`
- **Ubuntu/Debian:** `sudo apt install build-essential python3-dev libssl-dev libffi-dev`
- **Windows:** Visual Studio Build Tools
  (https://visualstudio.microsoft.com/visual-cpp-build-tools/)

These are 300MB–2GB downloads. Do them ahead of time.

### Mangrove API key isn't arriving in email

Check spam. If it's still not there after 5 minutes, file a support
ticket at https://mangrovedeveloper.ai or ask the facilitator. They
can hand out a workshop key if needed.

### Port conflicts (rare, but)

The mangrove-agent listens on port 9080. Before the workshop, confirm
nothing else is bound there:

```bash
# macOS / Linux
lsof -iTCP:9080 -sTCP:LISTEN

# Windows Git Bash
netstat -an | grep 9080
```

If something is returned, figure out what and either shut it down
before the workshop or set `BARE_PORT=9082` in your environment
before running setup. (Note: 9081 is already used for the
`MANGROVEMARKETS_BASE_URL` self-host placeholder, so step up to 9082
if 9080 is taken.)

## If you're funding a live trade (chapters 06–08)

Optional. You can do chapters 01–05 without any of this.

- **Base-network USDC**, 1–5 USDC is enough.
- **A little Base ETH for gas** — $0.50 is plenty. Bridge from
  Ethereum mainnet at https://base.org/bridge if you need to.
- **A wallet you control to send from** — MetaMask or a CEX
  (Coinbase / Binance / Kraken) that supports Base-network
  withdrawals.

**Double-check the network is Base, not Ethereum mainnet or
Polygon.** Sending USDC on the wrong network means your funds are
stuck or lost. This is the #1 way workshop attendees lose money.

## What to bring to the session

- A laptop with all five prerequisites working
- Your Mangrove API key (in a password manager, not email)
- A password manager or a piece of paper (for saving a wallet secret
  if you do chapters 06–08)
- Reasonable internet (Base mainnet txs need ~30s of connectivity
  to land)

## What NOT to bring

- Your main crypto wallet. If you're doing live chapters, create a
  fresh wallet during the workshop (chapter 06) instead.
- Significant capital. First live allocation should be $1. Scale up
  later, not during the workshop.

See you there.
