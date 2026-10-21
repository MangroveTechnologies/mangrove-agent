# Sage across AI clients

Every supported client receives the same project-wide Sage instructions. The
entry file varies by client; the shared policy does not. This setup does not
change the machine owner's global assistant settings or copy personal identity
files into this public repository. Host policies and explicit user instructions
still apply.

## Shared source and entry points

- `SAGE.md`: identity, project context, startup checks, and repository conventions.
- `.claude/rules/trading-bot-workflow.md`, `wallet-presentation.md`, and
  `risk-management.md`: mandatory local operating policy. Their directory name
  is historical; every client reads them.
- `AGENTS.md`: Codex, Grok Build, and other hosts that discover this filename.
  It explicitly directs the client to the shared sources.
- `CLAUDE.md`: Claude Code's native entry point, importing `SAGE.md`.
- `docs/clients/sage-instructions.md`: complete generated export for Perplexity,
  Grok web, or another host without repository file access.

Edit persona and personalization in `SAGE.md`, not in the entry files or export.
Then run `python3 scripts/agent_context.py` to regenerate the export. CI checks
that it matches its sources. Server-owned strategy skills are never bundled;
the assistant loads those live with Mangrove's `load_skill` tool.

A Markdown link is an instruction to read another file, not a universal import
mechanism. Claude's `@SAGE.md` is a native import. Codex and compatible clients
must follow `AGENTS.md` and read the referenced files. Hosts without filesystem
access need the complete bundle injected or uploaded. A client that cannot load
all mandatory sources must report that limitation before operating.

## Codex and Grok Build

Run `./scripts/setup.sh` first using the existing setup flow. It starts the
local agent; setup alone does not connect every AI client. From this checkout:

```bash
python3 scripts/client_session.py codex
```

Or:

```bash
python3 scripts/client_session.py grok
```

The helper uses the existing installation's loopback URL and local `API_KEYS`
credential. It creates the client's project config from the checked-in example
only when that config does not exist, supplies the credential in the child
process environment, and starts the client at the repo root. No credential is
printed, placed in command-line arguments, or saved in client configuration.
The generated configs are gitignored. The upstream `MANGROVE_API_KEY` is a
different credential and must not be substituted for the local one.

If a project config already exists, it is preserved. Merge the `mangrove-agent`
table from `.codex/config.toml.example` or `.grok/config.toml.example` into it,
matching the URL printed by setup for Codex, then rerun the helper. Do not replace
other configured servers. Codex project config requires a trusted project. An
already-open desktop session will need to reconnect or start a fresh session;
running the helper cannot change another process's environment.

These are normal developer sessions with the client's own tools and policies.
They do not inherit the restricted Claude launcher's shell/file restrictions or
Claude hooks. The shared instructions and server authorization checks still
apply, but they are not a claim of equivalent client-side enforcement.

## Claude Code

Repository sessions read `CLAUDE.md`, which imports `SAGE.md`; Claude also loads
the repository's `.claude/rules/`. The installed plugin's SessionStart hook
renders and injects the same complete policy before adding plugin path and
namespace adaptations.

`./scripts/chat.sh` remains the restricted user-facing launcher. It cannot read
local files through assistant tools, so the launcher renders the full policy
into a temporary private file and passes `--append-system-prompt-file`. Missing
mandatory policy prevents launch. The file is removed when the client exits.
Its existing tool permissions, secret hooks, and explicit confirmation gates
remain in place. See [restricted chat](restricted-chat.md).

## Perplexity and Grok web

These interfaces do not automatically walk this local repository looking for
`AGENTS.md` or `CLAUDE.md`.

For Perplexity, create a dedicated Sage Project (called a Space in older
interfaces). Under Settings -> Context, put the contents of
`docs/clients/sage-instructions.md` in its custom instructions if the interface
accepts the complete text. Otherwise upload that file under Files and add:

```text
Before answering, read the attached sage-instructions.md completely, including
all four SOURCE sections, and follow it as this project's operating instructions.
If you cannot retrieve all sections, say so and do not perform trading actions.
Load strategy workflows live from Mangrove with load_skill. Do not claim a
working Mangrove connection until tool discovery and the free status call pass.
```

An uploaded source may be retrieved selectively. Verify that the assistant can
identify all four SOURCE sections before relying on it. If it cannot, use a
client that accepts the complete prompt rather than claiming the pointer loaded.
Use the same complete bundle in Grok web's project/custom instructions where
supported by the active interface; a repository path on its own is insufficient.

Configure Mangrove's MCP connection separately using that host's connector UI.
Perplexity's custom remote MCP connectors require Pro, Max, or Enterprise; a
Free account can save the project instructions and file but cannot complete the
custom connector setup. Open Plugins (or Customize -> Connectors), then Add
custom -> Connector -> Add MCP connector -> Remote. Use a verified HTTPS MCP
endpoint and its actual authentication and transport settings. Do not upload
credentials as project files or put them in chat. See
[Perplexity's connector setup](https://www.perplexity.ai/help-center/en/articles/13915507-adding-custom-remote-connectors)
and [plan availability](https://www.perplexity.ai/en-GB/changelog/what-we-shipped---march-13-2026).

Perplexity also documents local MCP through its Mac app. The Mac local setup is
command-based, so the HTTP-only local agent
may need a separately configured compatible bridge. Grok web requires a
publicly reachable MCP URL. This change does not deploy a remote endpoint or
expose the local wallet service through a tunnel. Such a connection needs its
own authenticated deployment/bridge and explicit configuration. Instructions
are ready to import even when the tool connection is not yet available.

## Fresh-session acceptance check

Verify each client separately; passing in one does not prove another is ready.

1. Start a new session in the intended checkout or configured project. Ask which
   instructions are active. It should name `SAGE.md` and all three mandatory
   rule sources, or the four corresponding sections in the complete bundle.
2. Discover the advertised Mangrove MCP tools. Use the actual namespace and
   schema, then call only the free `status` tool. Report tool availability and
   the real result; stop on failure. This must not call paid market data,
   evaluate a strategy, create/import a wallet, place an order, or reset a budget.
3. Ask where strategy/backtest instructions come from. The answer is the live
   server's `load_skill`, not a local copy or remembered strategy template.
4. Ask how to import a private key. The answer must direct the user to their
   private terminal's stash flow, never invite them to paste the key into chat.

The offline test suite verifies exported policy, credential handling, config
preservation, and instructions passed to fake client processes. It does not
claim a paid model session, web account setup, or cross-client behavioral proof.

## Official client references

- [Codex instruction discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
- [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [Claude CLI prompt files](https://code.claude.com/docs/en/cli-reference)
- [Grok Build instruction compatibility](https://docs.x.ai/build/features/skills-plugins-marketplaces)
- [Grok Build MCP configuration](https://docs.x.ai/build/features/mcp-servers)
- [Grok web connectors](https://docs.x.ai/grok/connectors)
- [Perplexity Space instructions](https://www.perplexity.ai/enterprise/videos/how-to-use-create-spaces)
- [Perplexity MCP connections](https://www.perplexity.ai/help-center/en/articles/11502712-local-and-remote-mcps-for-perplexity)
