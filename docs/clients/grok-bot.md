# Grok Bot

This page covers host mechanics only: where the checkout lives, how access is
configured, how tools are called and what keeps the agent running. Policy stays
in [`SAGE.md`](../../SAGE.md) and the three mandatory rule documents,
[`trading-bot-workflow.md`](../../.claude/rules/trading-bot-workflow.md),
[`wallet-presentation.md`](../../.claude/rules/wallet-presentation.md) and
[`risk-management.md`](../../.claude/rules/risk-management.md). Where this page
and those sources differ, the sources win.

Grok Bot is a hosted agent with its own persistent Linux box. It runs this
repository on that box, not on the owner's computer. It does not discover
`AGENTS.md`, `CLAUDE.md` or `.claude/skills/` on its own, and it cannot attach
the agent's MCP server as native tools. It uses the command client instead, as
described in [Mangrove agent across AI clients](../clients.md). Claude hooks and
the restricted launcher's permissions do not run on this host. The agent itself
still refuses wallet secrets in tool arguments, withholds them from tool results
and refuses a live swap from a wallet with no input-token balance, because those
checks run in the agent for every client. See
[guardrails across clients](../clients.md#guardrails-across-clients).

## First run

Use `/workspace/mangrove-agent` unless the owner names another path.

```bash
git clone https://github.com/MangroveTechnologies/mangrove-agent /workspace/mangrove-agent
cd /workspace/mangrove-agent
```

If the checkout already exists, update it instead of cloning a second copy:

```bash
cd /workspace/mangrove-agent && git pull --ff-only
```

Run setup from a shell without `MANGROVE_AGENT_HOME` set; `setup.sh` refuses to
run when that plugin override is present.

## Access

Choose one access mode. Set it up once; reruns preserve the saved mode.

**API key.** The owner stores the upstream MangroveAI key as a Grok Bot secret
named `MANGROVE_API_KEY`, available to the box shell as an environment variable.
The key is never pasted into chat or passed as a command argument. Pipe it to
setup:

```bash
printf '%s' "$MANGROVE_API_KEY" | ./scripts/setup.sh --auth api-key --api-key-stdin --yes --no-mcp
```

This upstream key is not the local agent credential (`API_KEYS`); the command
client reads that one from the protected local config.

**x402 wallet.** A fresh install with `--yes` defaults to keyless x402 mode:

```bash
./scripts/setup.sh --yes --no-mcp
```

To switch an existing API-key install, add `--auth x402`. Setup creates no
wallet and makes no payment. Any wallet step follows
[`wallet-presentation.md`](../../.claude/rules/wallet-presentation.md).

`--no-mcp` skips Claude Code registration, which this host cannot use.

## Required watchdog routine

Create a Grok Bot routine that runs every 15 minutes. A box restart stops the
agent process; without this routine, its schedules stop without any message.

1. Run the free `status` tool:

   ```bash
   cd /workspace/mangrove-agent && .venv/bin/python3 scripts/local_mcp_bridge.py --call status
   ```

2. If it exits nonzero, run `./scripts/setup.sh --yes --no-mcp`, which keeps the
   saved access mode and starts the agent. Then run `status` again.
3. Message the owner only when that second `status` call also fails. Include the
   failed step and its error, without credentials or config contents.

The routine must not call any other tool. Schedules survive a restart, but
evaluations run only while the agent runs, and missed intervals are skipped
rather than replayed (see [paper scheduling](../paper-scheduling.md#recovery-and-limits)).

## Every session

1. Load the shared policy once by reading the complete output of:

   ```bash
   .venv/bin/python3 scripts/agent_context.py --stdout
   ```

   It contains `SAGE.md` and all three rule documents. Do not also read those
   files separately. If the output is truncated, follow the
   [context rules](../clients.md#context-at-session-start) before operating.
2. Follow the startup contract in `SAGE.md`, including the free `status` call
   before naming.
3. Call tools through the command client. List the catalog with
   `--list-tools`; call one tool with a JSON object on stdin:

   ```bash
   printf '%s' '<json>' | .venv/bin/python3 scripts/local_mcp_bridge.py --call <tool> --arguments-file -
   ```

   Without `--arguments-file`, the arguments default to `{}`. The command exits
   nonzero on any tool failure.
4. Use `discover_tools` and `load_skill` as the rules require; see
   [progressive discovery](../clients.md#progressive-discovery-of-managed-services).

## Repository skills

Grok Bot does not load `.claude/skills/` automatically. List every `SKILL.md`
under that directory at any depth, and read a skill when its task comes up, as
`SAGE.md` directs. Skip the developer skills `audit-security`,
`check-alignment` and `tool-spec`.

## Host limits

When a capability is missing, report it as described in
[Mangrove agent across AI clients](../clients.md); a missing capability is not
permission to work around a control. Never echo, log or save a credential, key,
recovery phrase or vault token in chat, routine output or files outside the
protected config.

These guards are not enforced on this host and remain Grok Bot's duty:

- A key or recovery phrase the owner types into the Grok Bot chat reaches the
  host transcript before any tool runs. If one arrives, do not pass it to any
  tool or file; tell the owner to run `./scripts/stash-secret.sh` in their own
  terminal. A tool call that carries one is refused with `SECRET_IN_ARGUMENTS`.
- Shell output on the box is not checked for secrets. Do not run commands that
  print keys, phrases, vault tokens or config contents.
- For repository maintenance, work on a feature branch and open a pull request;
  never commit or push to `main`. Nothing on this host blocks it.
