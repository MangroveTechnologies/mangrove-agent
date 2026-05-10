# Mangrove Agent Plugin

Claude Code plugin for interacting with your mangrove-agent application.

## Installation

Install this plugin into Claude Code:

```bash
claude plugin install /path/to/mangrove-agent/plugin
```

## Commands

| Command | Description |
|---------|-------------|
| /help | Show available commands |

## MCP Tools

Connect to the app's MCP server for tool access. The server runs at the URL configured in `.mcp.json`.

## Configuration

Update `.mcp.json` with your server's URL:

```json
{
  "mcpServers": {
    "mangrove-agent": {
      "type": "streamableHttp",
      "url": "https://your-server.com/mcp/"
    }
  }
}
```

## Customization

- Add commands in `commands/` (one `.md` file per command)
- Update `skills/app/SKILL.md` with your tool descriptions
- Update `hooks/context.json` with your command list
- Update `.claude-plugin/plugin.json` with your project details
